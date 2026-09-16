"""Scale workers up and down on IPI clusters.

Static-IP clusters (the only kind this app installs) get their machine addresses
from IPAddressClaims against the installer's IPPool, which nothing serves after
the install. Scaling up therefore means: raise the MachineSet replicas, then
satisfy every new claim with an IPAddress object and point the claim at it."""
import ipaddress
import time
from typing import Dict, List

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, render, clusterops as ops
from .deploy import job_push_haproxy

NS = ops.MAPI_NS


def overview(store: ClusterStore, spec: ClusterSpec) -> Dict:
    ms = ops.machinesets(store, spec)
    m = ops.machines(store, spec)
    known = {n.ip: n for n in spec.nodes}
    for x in m:
        x["in_spec"] = x["ip"] in known
    claims = ops.get_opt(store, spec, "ipaddressclaims.ipam.cluster.x-k8s.io", ns=NS) or {}
    unbound = [c["metadata"]["name"] for c in claims.get("items", []) if not (c.get("status") or {}).get("addressRef")]
    used = {n.ip for n in spec.nodes} | {x["ip"] for x in m if x["ip"]}
    return {"machinesets": ms, "machines": m, "unbound_claims": unbound, "used_ips": sorted(used),
            "cidr": spec.network.machine_cidr, "gateway": spec.network.gateway}


def _bind_claim(store, spec, claim: Dict, ip: str, log):
    name = claim["metadata"]["name"]
    pool = claim["spec"].get("poolRef") or {"apiGroup": "installer.openshift.io", "kind": "IPPool", "name": "default-0"}
    doc = {"apiVersion": "ipam.cluster.x-k8s.io/v1beta1", "kind": "IPAddress",
           "metadata": {"name": name, "namespace": NS},
           "spec": {"address": ip, "claimRef": {"name": name}, "gateway": spec.network.gateway, "poolRef": pool, "prefix": render._prefix(spec)}}
    ops.apply(store, spec, [doc])
    ops.patch(store, spec, f"ipaddressclaim.ipam.cluster.x-k8s.io/{name}", {"status": {"addressRef": {"name": name}}}, ns=NS, subresource="status")
    log(f"claim {name} bound to {ip}")


def job_scale_up(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, machineset: str, ips: List[str]):
    ms = next((m for m in ops.machinesets(store, spec) if m["name"] == machineset), None)
    if not ms:
        raise RuntimeError(f"MachineSet {machineset} not found")
    net = ipaddress.ip_network(spec.network.machine_cidr, strict=False)
    ips = [i.strip() for i in ips if i.strip()]
    for ip in ips:
        if ipaddress.ip_address(ip) not in net:
            raise RuntimeError(f"{ip} is outside the machine network {net}")
    used = {n.ip for n in spec.nodes} | {m["ip"] for m in ops.machines(store, spec) if m["ip"]}
    dup = [i for i in ips if i in used]
    if dup:
        raise RuntimeError(f"already in use: {', '.join(dup)}")
    if ms["static_ip"] and not ips:
        raise RuntimeError("this MachineSet uses static IPs: give one IP per new node")
    add = len(ips) if ms["static_ip"] else 1
    target = ms["replicas"] + add
    ctx.log(f"Scaling {machineset} from {ms['replicas']} to {target}")
    kube.oc(store, spec, ["scale", f"machineset/{machineset}", f"--replicas={target}", "-n", NS])
    before = {m["name"] for m in ops.machines(store, spec)}
    new_machines: List[str] = []
    if ms["static_ip"]:
        remaining = list(ips)
        bound: Dict[str, str] = {}

        def bind_new():
            claims = ops.get(store, spec, "ipaddressclaims.ipam.cluster.x-k8s.io", ns=NS)
            for c in claims.get("items", []):
                nm = c["metadata"]["name"]
                if (c.get("status") or {}).get("addressRef") or nm in bound or not nm.startswith(machineset + "-"):
                    continue
                if not remaining:
                    break
                ip = remaining.pop(0)
                _bind_claim(store, spec, c, ip, ctx.log)
                bound[nm] = ip
            return not remaining
        ops.wait_for("IP claims of the new machines", bind_new, timeout=600, interval=10, log=ctx.log)
    ops.wait_for("new machines to appear", lambda: len({m["name"] for m in ops.machines(store, spec)} - before) >= add, timeout=300, interval=10, log=ctx.log)
    new_machines = sorted({m["name"] for m in ops.machines(store, spec)} - before)
    ctx.log("new machines: " + ", ".join(new_machines))

    def ready():
        ms_ = {m["name"]: m for m in ops.machines(store, spec)}
        nodes = {n["name"]: n for n in ops.nodes_summary(store, spec)}
        ok = 0
        for n in new_machines:
            node = ms_.get(n, {}).get("node")
            if node and nodes.get(node, {}).get("ready") == "True":
                ok += 1
        return ok >= len(new_machines)

    def tick():
        ops.approve_csrs(store, spec, ctx.log)
        ms_ = {m["name"]: m for m in ops.machines(store, spec)}
        return "phases: " + ", ".join(f"{n}={ms_.get(n, {}).get('phase', '?')}" for n in new_machines)
    ops.wait_for("new nodes Ready", ready, timeout=2400, interval=30, log=ctx.log, on_tick=tick)
    # record the nodes in the spec so the load balancer pools include them
    ms_ = {m["name"]: m for m in ops.machines(store, spec)}
    added = []
    for n in new_machines:
        ip = ms_.get(n, {}).get("ip")
        if ip and not any(x.ip == ip for x in spec.nodes):
            added.append({"name": n, "role": "worker", "ip": ip, "mac": None, "cpus": ms["cpus"] or 4, "memory_mb": ms["memory_mb"] or 16384,
                          "disk_gb": ms["disk_gb"] or 120, "failure_domain": "", "pool": "", "extra_disks_gb": []})
    if added:
        store.patch(lambda raw: raw["nodes"].extend(added))
        ctx.log(f"added {len(added)} node(s) to the cluster spec")
        spec = store.load()
    if spec.lb.mode == "haproxy":
        job_push_haproxy(ctx, store, spec)
    elif spec.lb.mode == "external":
        ctx.log("External LB: add the new workers to the apps pools for 80 and 443.")


def job_scale_down(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, machine_names: List[str]):
    all_m = {m["name"]: m for m in ops.machines(store, spec)}
    for n in machine_names:
        if n not in all_m:
            raise RuntimeError(f"machine {n} not found")
        if all_m[n]["role"] == "master":
            raise RuntimeError(f"{n} is a control-plane machine; refusing")
    workers_left = [m for m in all_m.values() if m["role"] != "master" and m["name"] not in machine_names]
    if not workers_left and not spec.is_sno and len(spec.nodes_by_role("master")) == 3 and spec.topology == "standard":
        ctx.log("warning: removing the last worker; the routers will have nowhere to run unless masters are schedulable")
    removed_ips = []
    for n in machine_names:
        m = all_m[n]
        if m["ip"]:
            removed_ips.append(m["ip"])
        # drop it from the load balancer first so traffic drains cleanly
    if removed_ips:
        store.patch(lambda raw: raw.__setitem__("nodes", [x for x in raw["nodes"] if x.get("ip") not in removed_ips]))
        spec = store.load()
        if spec.lb.mode == "haproxy":
            job_push_haproxy(ctx, store, spec)
    for n in machine_names:
        m = all_m[n]
        if m["machineset"]:
            ms = next((x for x in ops.machinesets(store, spec) if x["name"] == m["machineset"]), None)
            if ms and ms["replicas"] > 0:
                # annotate so the MachineSet removes exactly this machine, then lower replicas
                kube.oc(store, spec, ["annotate", f"machine/{n}", "machine.openshift.io/delete-machine=true", "--overwrite", "-n", NS])
                kube.oc(store, spec, ["scale", f"machineset/{m['machineset']}", f"--replicas={ms['replicas'] - 1}", "-n", NS])
                ctx.log(f"{n}: scaled {m['machineset']} to {ms['replicas'] - 1}")
                continue
        ctx.log(f"{n}: deleting machine (the node is drained first)")
        kube.oc(store, spec, ["delete", f"machine/{n}", "-n", NS, "--wait=false"])
    ops.wait_for("machines to disappear", lambda: not ({m["name"] for m in ops.machines(store, spec)} & set(machine_names)), timeout=1800, interval=20, log=ctx.log,
                 on_tick=lambda: "phases: " + ", ".join(f"{n}={ {m['name']: m for m in ops.machines(store, spec)}.get(n, {}).get('phase', 'gone')}" for n in machine_names))
    if spec.lb.mode == "external" and removed_ips:
        ctx.log("External LB: remove " + ", ".join(removed_ips) + " from the apps pools.")


def job_delete_machineset(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, name: str):
    ms = next((m for m in ops.machinesets(store, spec) if m["name"] == name), None)
    if not ms:
        raise RuntimeError(f"MachineSet {name} not found")
    if ms["role"] == "master":
        raise RuntimeError("refusing to delete a control-plane MachineSet")
    members = [m for m in ops.machines(store, spec) if m["machineset"] == name]
    ips = [m["ip"] for m in members if m["ip"]]
    if ips:
        store.patch(lambda raw: raw.__setitem__("nodes", [x for x in raw["nodes"] if x.get("ip") not in ips]))
        spec = store.load()
        if spec.lb.mode == "haproxy":
            job_push_haproxy(ctx, store, spec)
    ctx.log(f"Deleting MachineSet {name} and its {len(members)} machine(s)")
    kube.oc(store, spec, ["delete", f"machineset/{name}", "-n", NS, "--wait=false"])
    ops.wait_for("machines to disappear", lambda: not any(m["machineset"] == name for m in ops.machines(store, spec)), timeout=1800, interval=20, log=ctx.log)
