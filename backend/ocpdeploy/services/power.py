"""Graceful cluster shutdown and startup through vCenter (guest shutdown / power on)."""
import time
from typing import Dict, List

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import vcenter, clusterops as ops


def cluster_vms(store: ClusterStore, spec: ClusterSpec) -> List[Dict]:
    """VMs that belong to the cluster with their role, from vCenter."""
    if spec.install_method == "ipi":
        infra_id = store.kv_get("infra_id") or ""
        if not infra_id:
            raise RuntimeError("infra ID unknown; is the cluster installed?")
        vms = vcenter.list_vms(spec.vcenter, prefix=infra_id + "-")
        out = []
        for v in vms:
            if "-bootstrap" in v["name"] or "-rhcos-" in v["name"]:
                continue
            role = "master" if "-master-" in v["name"] else "worker"
            out.append({"name": v["name"], "role": role, "power": v["power"], "ip": v["ip"], "tools": v["tools"]})
        return out
    from .providers import get_provider
    prov = get_provider(spec, store)
    out = []
    for n in spec.nodes:
        if n.role == "bootstrap":
            continue
        out.append({"name": f"{spec.name}-{n.name}", "role": "master" if n.role == "master" else "worker", "power": prov.power_state(n), "ip": n.ip, "tools": prov.title, "node": n.name})
    return out


def _node_of(spec: ClusterSpec, vm: Dict):
    return next((n for n in spec.nodes if n.name == vm.get("node")), None)


def _shutdown_vm(spec: ClusterSpec, store, vm: Dict, log):
    if spec.install_method == "ipi":
        with vcenter.session(spec.vcenter) as si:
            v = vcenter._find(si.content, vcenter.vim.VirtualMachine, vm["name"])
            v.ShutdownGuest()
        return
    from .providers import get_provider
    get_provider(spec, store).shutdown_guest(_node_of(spec, vm), log)


def _states(spec: ClusterSpec, store, vms: List[Dict]) -> Dict[str, str]:
    if spec.install_method == "ipi":
        return vcenter.power_states(spec.vcenter, [v["name"] for v in vms])
    from .providers import get_provider
    prov = get_provider(spec, store)
    return {v["name"]: prov.power_state(_node_of(spec, v)) for v in vms}


def _power(spec: ClusterSpec, store, vm: Dict, state: str, log):
    if spec.install_method == "ipi":
        vcenter.power(spec.vcenter, vm["name"], state, log)
        return
    from .providers import get_provider
    prov = get_provider(spec, store)
    (prov.power_on if state == "on" else prov.power_off)(_node_of(spec, vm), log)


def status(store: ClusterStore, spec: ClusterSpec) -> Dict:
    vms = cluster_vms(store, spec)
    api = False
    try:
        ops.get(store, spec, "nodes")
        api = True
    except Exception:
        pass
    signer_days = None
    if api:
        try:
            from datetime import datetime, timezone
            s = ops.get(store, spec, "secret", "kube-apiserver-to-kubelet-signer", ns="openshift-kube-apiserver-operator")
            na = s["metadata"].get("annotations", {}).get("auth.openshift.io/certificate-not-after", "")
            if na:
                signer_days = (datetime.fromisoformat(na.replace("Z", "+00:00")) - datetime.now(timezone.utc)).days
        except Exception:
            pass
    return {"vms": vms, "api_reachable": api, "all_off": all(v["power"] == "poweredOff" for v in vms) if vms else False,
            "all_on": all(v["power"] == "poweredOn" for v in vms) if vms else False, "kubelet_signer_days": signer_days}


def job_shutdown(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, backup_first: bool = True):
    from . import backup
    vms = cluster_vms(store, spec)
    if not vms:
        raise RuntimeError("no cluster VMs found in vCenter")
    st = status(store, spec)
    if st["kubelet_signer_days"] is not None:
        ctx.log(f"kube-apiserver-to-kubelet-signer expires in {st['kubelet_signer_days']} days; start the cluster before then (CSRs are approved on startup anyway)")
    if backup_first and st["api_reachable"]:
        try:
            backup.run_backup(store, spec, ctx.log)
        except Exception as ex:
            ctx.log(f"warning: etcd backup failed ({ex}); continuing with the shutdown")
    workers = [v for v in vms if v["role"] != "master"]
    masters = [v for v in vms if v["role"] == "master"]
    for group, label in ((workers, "workers"), (masters, "masters")):
        on = [v for v in group if v["power"] == "poweredOn"]
        if not on:
            ctx.log(f"{label}: already off")
            continue
        ctx.log(f"Shutting down {label}: {', '.join(v['name'] for v in on)}")
        for v in on:
            try:
                _shutdown_vm(spec, store, v, ctx.log)
                ctx.log(f"{v['name']}: guest shutdown requested")
            except Exception as ex:
                ctx.log(f"{v['name']}: {str(ex)[-100:]}; will power off")
        deadline = time.time() + 600
        left = [v["name"] for v in on]
        while time.time() < deadline:
            states = _states(spec, store, on)
            left = [n for n, s in states.items() if s != "poweredOff"]
            if not left:
                break
            ctx.log(f"waiting for {len(left)} {label} to power off: {', '.join(left)}")
            time.sleep(15)
        else:
            for v in on:
                if v["name"] in left:
                    ctx.log(f"{v['name']}: forcing power off")
                    _power(spec, store, v, "off", ctx.log)
        ctx.log(f"{label}: all powered off")
    ctx.log("Cluster is shut down. Use 'Start cluster' to bring it back.")


def job_startup(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    vms = cluster_vms(store, spec)
    if not vms:
        raise RuntimeError("no cluster VMs found in vCenter")
    masters = [v for v in vms if v["role"] == "master"]
    workers = [v for v in vms if v["role"] != "master"]
    ctx.log("Powering on masters: " + ", ".join(v["name"] for v in masters))
    for v in masters:
        _power(spec, store, v, "on", ctx.log)

    def api_up():
        ops.get(store, spec, "nodes")
        return True
    ops.wait_for("Kubernetes API", api_up, timeout=1800, interval=20, log=ctx.log)
    ctx.log("Powering on workers: " + ", ".join(v["name"] for v in workers))
    for v in workers:
        _power(spec, store, v, "on", ctx.log)

    def nodes_ready():
        ops.approve_csrs(store, spec, ctx.log)
        ns = ops.nodes_summary(store, spec)
        return all(n["ready"] == "True" for n in ns) and len(ns) >= len(vms)

    def tick():
        ns = ops.nodes_summary(store, spec)
        return f"nodes ready: {sum(1 for n in ns if n['ready'] == 'True')}/{len(vms)}"
    ops.wait_for("all nodes Ready", nodes_ready, timeout=2400, interval=30, log=ctx.log, on_tick=tick)
    for n in ops.nodes_summary(store, spec):
        if n["unschedulable"]:
            from . import kube
            kube.oc(store, spec, ["adm", "uncordon", n["name"]], check=False)
            ctx.log(f"uncordoned {n['name']}")

    def cos_ok():
        return ops.co_healthy(store, spec)

    def co_tick():
        bad = [o["name"] for o in ops.co_states(store, spec) if not (o["available"] == "True" and o["degraded"] != "True" and o["progressing"] != "True")]
        return f"operators settling: {', '.join(bad[:8])}{'…' if len(bad) > 8 else ''}" if bad else "operators healthy"
    try:
        ops.wait_for("cluster operators healthy", cos_ok, timeout=1800, interval=30, log=ctx.log, on_tick=co_tick)
    except RuntimeError as ex:
        ctx.log(f"warning: {ex}; check the Health page")
    ctx.log("Cluster is up.")
