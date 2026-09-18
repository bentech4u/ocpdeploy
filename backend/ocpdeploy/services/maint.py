"""Node maintenance: cordon, uncordon, drain, reboot, remove."""
import time
from typing import Dict, List, Optional

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, clusterops as ops


def overview(store: ClusterStore, spec: ClusterSpec) -> Dict:
    nodes = ops.get(store, spec, "nodes").get("items", [])
    pods = ops.get(store, spec, "pods", extra=["-A", "--field-selector", "status.phase!=Succeeded,status.phase!=Failed"]).get("items", [])
    per_node: Dict[str, int] = {}
    for p in pods:
        n = (p.get("spec") or {}).get("nodeName")
        if n:
            per_node[n] = per_node.get(n, 0) + 1
    try:
        machines = {m["node"]: m for m in ops.machines(store, spec) if m.get("node")}
    except Exception:
        machines = {}
    out = []
    for n in nodes:
        md, st = n["metadata"], n.get("status", {})
        labels = md.get("labels", {})
        roles = sorted(k.split("/")[1] for k in labels if k.startswith("node-role.kubernetes.io/"))
        conds = ops.conditions(n)
        out.append({"name": md["name"], "roles": roles, "ready": conds.get("Ready", {}).get("status", "Unknown"),
                    "unschedulable": bool((n.get("spec") or {}).get("unschedulable")),
                    "taints": [f"{t['key']}{('=' + t['value']) if t.get('value') else ''}:{t['effect']}" for t in (n.get("spec") or {}).get("taints", [])],
                    "pods": per_node.get(md["name"], 0), "ip": next((a["address"] for a in st.get("addresses", []) if a["type"] == "InternalIP"), ""),
                    "kubelet": st.get("nodeInfo", {}).get("kubeletVersion", ""), "boot_id": st.get("nodeInfo", {}).get("bootID", ""),
                    "machine": (machines.get(md["name"]) or {}).get("name", ""), "age": ops.age(md.get("creationTimestamp")),
                    "master": "master" in roles or "control-plane" in roles})
    out.sort(key=lambda x: (not x["master"], x["name"]))
    return {"nodes": out, "imported": spec.imported, "install_method": spec.install_method, "provider": spec.provider}


def _node(store, spec, name: str) -> Dict:
    for n in overview(store, spec)["nodes"]:
        if n["name"] == name:
            return n
    raise RuntimeError(f"node {name} not found")


def cordon(store, spec, name: str, on: bool) -> str:
    _node(store, spec, name)
    return kube.oc(store, spec, ["adm", "cordon" if on else "uncordon", name]).strip()


def drain_blockers(store, spec, name: str, force: bool = False) -> str:
    """Server-side dry run of the drain; returns the reason it would fail, or ''."""
    args = ["adm", "drain", name, "--ignore-daemonsets", "--delete-emptydir-data", "--dry-run=server"] + (["--force"] if force else [])
    import subprocess
    r = subprocess.run([kube.oc_bin(spec)] + args, env=kube.env(store), capture_output=True, text=True, timeout=120)
    return "" if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip())[-600:]


def _drain(ctx: JobContext, store, spec, name: str, timeout: int = 900, force: bool = False):
    blockers = drain_blockers(store, spec, name, force)
    if blockers:
        raise RuntimeError(f"drain of {name} would fail: {blockers}" + ("" if force else " (pods without a controller need the 'force' option; their data is lost)"))
    args = [kube.oc_bin(spec), "adm", "drain", name, "--ignore-daemonsets", "--delete-emptydir-data", f"--timeout={int(timeout)}s"]
    if force:
        args.append("--force")
    ctx.log(f"Draining {name} (pods are evicted respecting PodDisruptionBudgets; timeout {timeout}s{', force' if force else ''})")
    ctx.run(args, env=kube.min_env(store))


def job_drain(ctx: JobContext, store, spec, name: str, timeout: int = 900, force: bool = False):
    _node(store, spec, name)
    _drain(ctx, store, spec, name, timeout, force)
    ctx.log(f"{name} is drained and cordoned. Uncordon it when the maintenance is done.")


def _masters_ok(store, spec, except_name: str):
    others = [n for n in overview(store, spec)["nodes"] if n["master"] and n["name"] != except_name]
    bad = [n["name"] for n in others if n["ready"] != "True"]
    if bad:
        raise RuntimeError(f"other control-plane nodes are not Ready ({', '.join(bad)}); reboot masters one at a time")
    try:
        etcd = ops.get(store, spec, "etcd", "cluster")
        if ops.conditions(etcd).get("EtcdMembersDegraded", {}).get("status") == "True":
            raise RuntimeError("etcd reports degraded members; not rebooting a master now")
    except RuntimeError:
        raise
    except Exception:
        pass


def job_reboot(ctx: JobContext, store, spec, name: str, drain: bool = True, timeout: int = 900, force: bool = False):
    n = _node(store, spec, name)
    if n["master"]:
        _masters_ok(store, spec, name)
    was_cordoned = n["unschedulable"]
    if drain:
        _drain(ctx, store, spec, name, timeout, force)
    boot = n["boot_id"]
    ctx.log(f"Rebooting {name} (boot ID {boot[:8]}…)")
    kube.oc(store, spec, ["debug", f"node/{name}", "--quiet", "--", "chroot", "/host", "shutdown", "-r", "+0"], check=False, timeout=120)

    def rebooted():
        cur = _node(store, spec, name)
        return cur["boot_id"] and cur["boot_id"] != boot and cur["ready"] == "True"

    ops.wait_for(f"{name} back and Ready", rebooted, timeout=1800, interval=15, log=ctx.log,
                 on_tick=lambda: f"{name}: ready={_node(store, spec, name)['ready']}")
    if drain and not was_cordoned:
        kube.oc(store, spec, ["adm", "uncordon", name])
        ctx.log(f"{name} uncordoned")
    ctx.log(f"{name} rebooted and schedulable again" if not was_cordoned else f"{name} rebooted (it was cordoned before; left cordoned)")


def job_remove(ctx: JobContext, store, spec, name: str, delete_machine: bool = True, timeout: int = 900, force: bool = False):
    n = _node(store, spec, name)
    if n["master"]:
        raise RuntimeError("control-plane nodes cannot be removed here")
    _drain(ctx, store, spec, name, timeout, force)
    if n["machine"] and delete_machine:
        if not spec.imported and spec.install_method == "ipi":
            from . import scale
            ctx.log(f"{name} belongs to Machine {n['machine']}; removing it (the VM is shut down and deleted)")
            scale.job_scale_down(ctx, store, spec, [n["machine"]])
            return
        ctx.log(f"Deleting Machine {n['machine']} (the machine API shuts down and deletes the VM)")
        kube.oc(store, spec, ["annotate", f"machine/{n['machine']}", "machine.openshift.io/delete-machine=true", "--overwrite", "-n", ops.MAPI_NS])
        kube.oc(store, spec, ["delete", f"machine/{n['machine']}", "-n", ops.MAPI_NS, "--wait=false"])
        ops.wait_for("the node to disappear", lambda: name not in {x["name"] for x in overview(store, spec)["nodes"]}, timeout=1800, interval=20, log=ctx.log)
        ctx.log(f"{name} removed")
        return
    ctx.log(f"Deleting node object {name}")
    kube.oc(store, spec, ["delete", "node", name])
    if not spec.imported and spec.install_method == "agent":
        from .providers import get_provider
        prov = get_provider(spec, store)
        target = next((x for x in spec.nodes if x.name == name or name.startswith(x.name + ".") or x.ip == n["ip"]), None)
        if target:
            try:
                prov.shutdown_guest(target, ctx.log)
                ctx.log(f"{name}: powered off through {prov.title}")
            except Exception as ex:
                ctx.log(f"{name}: could not power off ({str(ex)[-120:]}); do it yourself")
            store.patch(lambda raw: raw.__setitem__("nodes", [x for x in raw["nodes"] if x.get("name") != target.name]))
            spec = store.load()
            if spec.lb.mode == "haproxy":
                from .deploy import job_push_haproxy
                job_push_haproxy(ctx, store, spec)
            return
    ctx.log(f"{name} is out of the cluster. Power the machine off yourself; it would re-register if it boots again with the same identity.")
