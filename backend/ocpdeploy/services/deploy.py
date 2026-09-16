"""Install / destroy job bodies for both install methods."""
import ipaddress
import json
import time
from pathlib import Path
from typing import Dict, List

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


SYSTEM_CA = "/etc/pki/tls/certs/ca-bundle.crt"


def proxy_env(spec: ClusterSpec) -> Dict[str, str]:
    """HTTP(S)_PROXY / NO_PROXY for processes on the installer host (openshift-install,
    oc-mirror). Empty when no proxy is configured."""
    p = spec.proxy
    if not (p.http_proxy or p.https_proxy):
        return {}
    env: Dict[str, str] = {}
    if p.http_proxy:
        env["HTTP_PROXY"] = env["http_proxy"] = p.http_proxy
    if p.https_proxy:
        env["HTTPS_PROXY"] = env["https_proxy"] = p.https_proxy
    no: List[str] = ["localhost", "127.0.0.1", ".svc", ".cluster.local"]
    for item in p.no_proxy.replace(" ", ",").split(","):
        if item.strip():
            no.append(item.strip())
    for extra in (spec.network.machine_cidr, spec.network.cluster_network, spec.network.service_network,
                  f".{spec.domain}" if spec.base_domain else "", spec.vcenter.host,
                  spec.mirror.registry.split(":")[0] if spec.mirror.enabled and spec.mirror.registry else ""):
        if extra and extra not in no:
            no.append(extra)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(no)
    return env


def trust_env(store: ClusterStore, spec: ClusterSpec, log=print) -> dict:
    """openshift-install validates vCenter TLS with the host trust store, not with
    additionalTrustBundle. Give it a combined bundle through SSL_CERT_FILE instead
    of touching the system store. Also carries the proxy variables when set."""
    env: Dict[str, str] = {}
    bundle = render.trust_bundle(spec)
    if bundle:
        combined = store.dir / "ca-trust.pem"
        parts = []
        if Path(SYSTEM_CA).exists():
            parts.append(Path(SYSTEM_CA).read_text())
        parts.append(bundle.strip() + "\n")
        combined.write_text("\n".join(parts))
        what = ["system CAs"]
        if spec.vcenter.cert_pem:
            what.append("vCenter CA")
        if spec.mirror.enabled and spec.mirror.ca_pem:
            what.append("mirror registry CA")
        if spec.additional_trust_bundle:
            what.append("additional CAs")
        log(f"Using trust bundle {combined.name} ({' + '.join(what)}) for the installer")
        env["SSL_CERT_FILE"] = str(combined)
    pe = proxy_env(spec)
    if pe:
        log(f"Proxy for the installer host: {pe.get('HTTPS_PROXY') or pe.get('HTTP_PROXY')} (no_proxy: {pe['NO_PROXY']})")
        env.update(pe)
    return env


def vm_name(spec: ClusterSpec, node) -> str:
    return f"{spec.name}-{node.name}"


def node_extra_disks(spec: ClusterSpec, node) -> List[int]:
    """Data disks for a node: its own list, else the default of its pool."""
    if node.extra_disks_gb:
        return list(node.extra_disks_gb)
    pool = spec.pool(node.pool) if node.pool else None
    return list(pool.extra_disks_gb) if pool else []


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


def finalize_after_restart(ctx: JobContext, store: ClusterStore, code: int):
    """Post-step for a deploy that was re-attached after an app restart."""
    _copy_log(ctx, store)
    if code == 0 and (store.install_dir / "auth" / "kubeconfig").exists():
        _finish(ctx, store)
        ctx.log("Install complete (recovered).")
    else:
        store.set_status("failed")


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


def _describe(spec: ClusterSpec) -> str:
    m = len(spec.nodes_by_role("master"))
    w = len(spec.day1_workers())
    shape = "single-node" if m == 1 else ("compact three-node" if w == 0 else f"{m} masters + {w} workers")
    extras = []
    if spec.pooled_workers():
        extras.append(f"{len(spec.pooled_workers())} pool node(s) on day 2")
    if spec.vcenter.failure_domains:
        extras.append(f"{len(spec.vcenter.failure_domains)} failure domains")
    if spec.mirror.enabled:
        extras.append(f"mirror {spec.mirror.registry}")
    if spec.proxy.http_proxy or spec.proxy.https_proxy:
        extras.append("proxy")
    return shape + (f" ({', '.join(extras)})" if extras else "")


def _deploy_ipi(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    ensure_tools(ctx, spec)
    render.write_install_dir(store, spec, ctx.log)
    store.set_status("deploying")
    ctx.log(f"Starting installer-provisioned vSphere installation: {_describe(spec)}. This takes 30-50 minutes.")
    try:
        ctx.run([installer(spec), "create", "cluster", "--dir", str(store.install_dir), "--log-level", "info"],
                env=trust_env(store, spec, ctx.log))
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


def job_resume(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    """Continue an IPI install whose installer process ended early (timeout, crash,
    app restart) while the VMs kept bootstrapping: wait-for bootstrap-complete,
    remove the bootstrap VM, wait-for install-complete."""
    d = str(store.install_dir)
    if not (store.install_dir / ".openshift_install_state.json").exists():
        raise RuntimeError("no installer state in install/; nothing to resume")
    store.set_status("deploying")
    env = trust_env(store, spec, ctx.log)
    try:
        ctx.log("Resuming: waiting for bootstrap-complete")
        ctx.run([installer(spec), "wait-for", "bootstrap-complete", "--dir", d, "--log-level", "info"], env=env)
        if spec.install_method == "ipi":
            ctx.log("Bootstrap complete; removing bootstrap resources")
            ctx.run([installer(spec), "destroy", "bootstrap", "--dir", d, "--log-level", "info"], env=env, check=False)
        ctx.log("Waiting for install-complete")
        ctx.run([installer(spec), "wait-for", "install-complete", "--dir", d, "--log-level", "info"], env=env)
    except Exception:
        store.set_status("failed")
        _copy_log(ctx, store)
        raise
    _copy_log(ctx, store)
    _finish(ctx, store)


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
        vcenter.create_vm(spec.vcenter, name, n.cpus, n.memory_mb, n.disk_gb, n.mac, remote, ctx.log,
                          extra_disks_gb=node_extra_disks(spec, n))
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
    env = trust_env(store, spec, ctx.log)
    ctx.log(f"Agent-based installation: {_describe(spec)}")
    ctx.log("Building agent ISO (downloads the RHCOS base image on first use)")
    try:
        ctx.run([installer(spec), "agent", "create", "image", "--dir", d, "--log-level", "info"], env=env)
        iso = next(store.install_dir.glob("agent.*.iso"))
        nodes = spec.nodes_by_role("master") + spec.day1_workers()
        create_node_vms(ctx, spec, nodes, iso)
        ctx.log("Waiting for bootstrap (rendezvous host = " + spec.nodes_by_role("master")[0].name + ")")
        ctx.run([installer(spec), "agent", "wait-for", "bootstrap-complete", "--dir", d, "--log-level", "info"], env=env)
        ctx.log("Waiting for install-complete")
        ctx.run([installer(spec), "agent", "wait-for", "install-complete", "--dir", d, "--log-level", "info"], env=env)
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
            ctx.run([installer(spec), "destroy", "cluster", "--dir", str(store.install_dir), "--log-level", "info"],
                    env=trust_env(store, spec, ctx.log))
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
        raise RuntimeError("cluster uses an external load balancer" if spec.lb.mode == "external" else "cluster has no load balancer (single node)")
    if not spec.lb.vms:
        raise RuntimeError("no HAProxy VMs configured")
    for vm in spec.lb.vms:
        r = haproxy.push(spec, vm, ctx.log)
        ctx.log(f"{vm.host}: " + ("; ".join(r["changed"]) if r["changed"] else "no changes"))
