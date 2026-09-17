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


def _installer_log_has(store: ClusterStore, needle: str) -> bool:
    log = store.install_dir / ".openshift_install.log"
    return log.exists() and needle in log.read_bytes()[-200000:].decode(errors="replace")


def install_complete(store: ClusterStore) -> bool:
    return _installer_log_has(store, "Install complete!")


def finalize_after_restart(ctx: JobContext, store: ClusterStore, code: int) -> int:
    """Post-step for a deploy/resume job re-attached after an app restart. The followed
    unit was one step of the install; run whatever is still missing, then decide."""
    spec = store.load()
    d = str(store.install_dir)
    try:
        if spec.install_method == "ipi" and (store.install_dir / ".openshift_install_state.json").exists() and not install_complete(store):
            env = trust_env(store, spec, ctx.log)
            if code != 0 and not _provisioning_timeout(store):
                raise RuntimeError(f"installer step failed with exit code {code}; see the log above")
            if code != 0:
                ctx.log("Installer gave up waiting for the machines to provision, but the VMs are still booting; continuing.")
            if not _installer_log_has(store, "Bootstrap status: complete"):
                ctx.log("Continuing: waiting for bootstrap-complete")
                ctx.run([installer(spec), "wait-for", "bootstrap-complete", "--dir", d, "--log-level", "info"], env=env)
            ctx.log("Removing bootstrap resources (no-op if already gone)")
            ctx.run([installer(spec), "destroy", "bootstrap", "--dir", d, "--log-level", "info"], env=env, check=False)
            ctx.log("Waiting for install-complete")
            ctx.run([installer(spec), "wait-for", "install-complete", "--dir", d, "--log-level", "info"], env=env)
            code = 0
    except Exception as ex:
        ctx.log("ERROR: " + str(ex))
        code = 1
    _copy_log(ctx, store)
    if code == 0 and (store.install_dir / "auth" / "kubeconfig").exists() and (spec.install_method != "ipi" or install_complete(store)):
        _finish(ctx, store)
        ctx.log("Install complete (recovered).")
        return 0
    store.set_status("failed")
    return 1


# ---------------------------------------------------------------- IPI
def job_generate(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    ensure_tools(ctx, spec)
    if spec.install_method == "agent":
        spec = _provider(spec, store).prepare_nodes(ctx.log)
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
    env = trust_env(store, spec, ctx.log)
    d = str(store.install_dir)
    try:
        try:
            ctx.run([installer(spec), "create", "cluster", "--dir", d, "--log-level", "info"], env=env)
        except Exception as first:
            # The installer gives the control-plane VMs 15 minutes to clone, boot and report
            # an IP. Slow hypervisors miss that while the VMs keep booting fine; instead of
            # stopping, carry on exactly like "Resume interrupted install" would.
            if not _provisioning_timeout(store):
                raise
            ctx.log("Installer gave up waiting for the machines to provision, but the VMs are still booting.")
            ctx.log("Continuing automatically with wait-for bootstrap-complete (this is what Resume does).")
            ctx.run([installer(spec), "wait-for", "bootstrap-complete", "--dir", d, "--log-level", "info"], env=env)
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
    ctx.log("Install complete. Remove the bootstrap entry from the API load balancer (Day-2 > Remove bootstrap).")


def _provisioning_timeout(store: ClusterStore) -> bool:
    """True when the installer's last error was the machine provisioning window, and the
    state needed to resume exists."""
    if not (store.install_dir / ".openshift_install_state.json").exists():
        return False
    log = store.install_dir / ".openshift_install.log"
    if not log.exists():
        return False
    tail = log.read_bytes()[-20000:].decode(errors="replace")
    return ("failed to provision control-plane machines" in tail or "machines are not ready" in tail) and "level=error" in tail


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
def _provider(spec: ClusterSpec, store: ClusterStore):
    from .providers import get_provider
    return get_provider(spec, store)


def create_node_vms(ctx: JobContext, spec: ClusterSpec, nodes, iso_local: Path, boot_only_missing: bool = True):
    """Put the ISO where the provider can boot from it, create the machines that do not
    exist yet, then power all of them on (vSphere, Proxmox, KVM, Redfish or manual)."""
    prov = _provider(spec, ctx.store)
    iso_ref = prov.upload_iso(iso_local, ctx.log)
    created = []
    for n in nodes:
        name = vm_name(spec, n)
        existing = prov.exists(n)
        if existing:
            ctx.log(f"VM {name} already exists ({existing.get('power')}); leaving it")
            continue
        prov.create_node(n, iso_ref, ctx.log, extra_disks=node_extra_disks(spec, n))
        created.append(name)
    for n in nodes:
        prov.power_on(n, ctx.log)
    text = prov.instructions(nodes, iso_ref)
    if text:
        for line in text.splitlines():
            ctx.log(line)
    return created


def _deploy_agent(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    ensure_tools(ctx, spec)
    prov = _provider(spec, store)
    spec = prov.prepare_nodes(ctx.log)
    render.write_install_dir(store, spec, ctx.log)
    store.set_status("deploying")
    d = str(store.install_dir)
    env = trust_env(store, spec, ctx.log)
    ctx.log(f"Agent-based installation on {prov.title}: {_describe(spec)}")
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
                prov.eject(n, ctx.log)
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
        prov = _provider(spec, store)
        for n in spec.nodes:
            if n.role == "bootstrap":
                continue
            try:
                prov.destroy(n, ctx.log)
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
