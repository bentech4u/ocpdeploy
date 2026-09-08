"""Install / destroy job bodies for both install methods."""
import json
import time
from pathlib import Path

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import tools, render, vcenter, haproxy
from .macs import mac_for


def ensure_tools(ctx: JobContext, spec: ClusterSpec):
    st = tools.status(spec.ocp_version)
    if not (st["openshift-install"] and st["oc"]):
        tools.ensure(spec.ocp_version, ctx.log)
    else:
        ctx.log(f"Tools for {spec.ocp_version} present")


def installer(spec: ClusterSpec) -> str:
    return str(tools.tool_path(spec.ocp_version, "openshift-install"))


def vm_name(spec: ClusterSpec, node) -> str:
    return f"{spec.name}-{node.name}"


def ensure_macs(store: ClusterStore, spec: ClusterSpec, log) -> ClusterSpec:
    changed = False
    for n in spec.nodes:
        if not n.mac:
            n.mac = mac_for(spec.name, n.name)
            changed = True
    if changed:
        def _p(raw):
            for i, n in enumerate(raw["nodes"]):
                raw["nodes"][i]["mac"] = spec.nodes[i].mac
        store.patch(_p)
        log("Generated MAC addresses for nodes without one")
    return spec


def _finish(ctx: JobContext, store: ClusterStore):
    pw = store.install_dir / "auth" / "kubeadmin-password"
    md = store.install_dir / "metadata.json"
    if md.exists():
        meta = json.loads(md.read_text())
        store.kv_set("infra_id", meta.get("infraID"))
    if pw.exists():
        ctx.log("kubeadmin password stored in install/auth/kubeadmin-password")
    store.set_status("installed")


# ---------------------------------------------------------------- IPI
def job_generate(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    ensure_tools(ctx, spec)
    if spec.install_method == "agent":
        spec = ensure_macs(store, spec, ctx.log)
    render.write_install_dir(store, spec, ctx.log)
    store.set_status("configured")


def job_deploy(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    if spec.install_method == "ipi":
        return _deploy_ipi(ctx, store, spec)
    return _deploy_agent(ctx, store, spec)


def _deploy_ipi(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    ensure_tools(ctx, spec)
    render.write_install_dir(store, spec, ctx.log)
    store.set_status("deploying")
    ctx.log("Starting installer-provisioned vSphere installation. This takes 30-50 minutes.")
    try:
        ctx.run([installer(spec), "create", "cluster", "--dir", str(store.install_dir), "--log-level", "info"])
    except Exception:
        store.set_status("failed")
        _copy_log(ctx, store)
        raise
    _copy_log(ctx, store)
    _finish(ctx, store)
    ctx.log("Install complete. Remove the bootstrap entry from the API load balancer (Day-2 > Remove bootstrap).")


def _copy_log(ctx, store):
    src = store.install_dir / ".openshift_install.log"
    if src.exists():
        dst = store.logs_dir / f"openshift_install-{time.strftime('%Y%m%d-%H%M%S')}.log"
        dst.write_bytes(src.read_bytes())
        ctx.log(f"Installer log saved to logs/{dst.name}")


# ---------------------------------------------------------------- agent
def _iso_remote_path(spec: ClusterSpec, iso: Path) -> str:
    return f"ocpdeploy/{spec.name}/{iso.name}"


def create_node_vms(ctx: JobContext, spec: ClusterSpec, nodes, iso_local: Path, boot_only_missing: bool = True):
    remote = vcenter.upload_to_datastore(spec.vcenter, iso_local, _iso_remote_path(spec, iso_local), ctx.log)
    created = []
    for n in nodes:
        name = vm_name(spec, n)
        existing = vcenter.find_vm(spec.vcenter, name)
        if existing:
            ctx.log(f"VM {name} already exists ({existing['power']}); leaving it")
            continue
        vcenter.create_vm(spec.vcenter, name, n.cpus, n.memory_mb, n.disk_gb, n.mac, remote, ctx.log)
        created.append(name)
    for n in nodes:
        vcenter.power(spec.vcenter, vm_name(spec, n), "on", ctx.log)
    return created


def _deploy_agent(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    ensure_tools(ctx, spec)
    spec = ensure_macs(store, spec, ctx.log)
    render.write_install_dir(store, spec, ctx.log)
    store.set_status("deploying")
    d = str(store.install_dir)
    ctx.log("Building agent ISO (downloads the RHCOS base image on first use)")
    try:
        ctx.run([installer(spec), "agent", "create", "image", "--dir", d, "--log-level", "info"])
        iso = next(store.install_dir.glob("agent.*.iso"))
        nodes = spec.nodes_by_role("master", "worker")
        create_node_vms(ctx, spec, nodes, iso)
        ctx.log("Waiting for bootstrap (rendezvous host = " + spec.nodes_by_role("master")[0].name + ")")
        ctx.run([installer(spec), "agent", "wait-for", "bootstrap-complete", "--dir", d, "--log-level", "info"])
        ctx.log("Waiting for install-complete")
        ctx.run([installer(spec), "agent", "wait-for", "install-complete", "--dir", d, "--log-level", "info"])
        for n in nodes:
            try:
                vcenter.eject_cdrom(spec.vcenter, vm_name(spec, n), ctx.log)
            except Exception as ex:
                ctx.log(f"could not eject ISO from {vm_name(spec, n)}: {ex}")
    except Exception:
        store.set_status("failed")
        _copy_log(ctx, store)
        raise
    _copy_log(ctx, store)
    _finish(ctx, store)


# ---------------------------------------------------------------- destroy
def job_destroy(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    if spec.install_method == "ipi":
        if (store.install_dir / "metadata.json").exists():
            ctx.run([installer(spec), "destroy", "cluster", "--dir", str(store.install_dir), "--log-level", "info"])
        else:
            ctx.log("No metadata.json; nothing for the installer to destroy")
    else:
        for n in spec.nodes:
            try:
                vcenter.destroy_vm(spec.vcenter, vm_name(spec, n), ctx.log)
            except Exception as ex:
                ctx.log(f"{vm_name(spec, n)}: {ex}")
    for f in ("auth", "metadata.json", ".openshift_install_state.json"):
        p = store.install_dir / f
        if p.exists():
            if p.is_dir():
                import shutil
                shutil.rmtree(p)
            else:
                p.unlink()
    store.set_status("destroyed")


# ---------------------------------------------------------------- lb
def job_push_haproxy(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    if spec.lb.mode != "haproxy":
        raise RuntimeError("cluster uses an external load balancer")
    if not spec.lb.vms:
        raise RuntimeError("no HAProxy VMs configured")
    for vm in spec.lb.vms:
        r = haproxy.push(spec, vm, ctx.log)
        ctx.log(f"{vm.host}: " + ("; ".join(r["changed"]) if r["changed"] else "no changes"))
