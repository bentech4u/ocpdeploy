"""Capacity: requests, usage and storage per node and namespace, with warnings."""
import json
import urllib.parse
from typing import Dict, List

from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, clusterops as ops
from .quantity import parse

WARN, CRIT = 0.85, 0.95


def _prom(store, spec, query: str) -> List[Dict]:
    """Instant query run inside a Prometheus pod (needs exec rights; empty when not allowed)."""
    pods = ops.get(store, spec, "pods", ns="openshift-monitoring", extra=["-l", "app.kubernetes.io/name=prometheus"]).get("items", [])
    running = [p["metadata"]["name"] for p in pods if (p.get("status") or {}).get("phase") == "Running"]
    if not running:
        return []
    url = "http://localhost:9090/api/v1/query?query=" + urllib.parse.quote(query, safe="")
    out = kube.oc(store, spec, ["exec", "-n", "openshift-monitoring", running[0], "-c", "prometheus", "--", "curl", "-s", "--max-time", "20", url], timeout=60)
    return (json.loads(out or "{}").get("data") or {}).get("result", [])


def _pod_requests(p: Dict):
    cpu = mem = 0.0
    for c in p["spec"].get("containers", []):
        r = (c.get("resources") or {}).get("requests") or {}
        cpu += parse(r.get("cpu"))
        mem += parse(r.get("memory"))
    icpu = max([parse(((c.get("resources") or {}).get("requests") or {}).get("cpu")) for c in p["spec"].get("initContainers") or []] or [0])
    imem = max([parse(((c.get("resources") or {}).get("requests") or {}).get("memory")) for c in p["spec"].get("initContainers") or []] or [0])
    return max(cpu, icpu), max(mem, imem)


def snapshot(store: ClusterStore, spec: ClusterSpec) -> Dict:
    errors = {}
    nodes = ops.get(store, spec, "nodes").get("items", [])
    pods = ops.get(store, spec, "pods", extra=["-A", "--field-selector", "status.phase!=Succeeded,status.phase!=Failed"]).get("items", [])
    try:
        nm = {i["metadata"]["name"]: i["usage"] for i in json.loads(kube.oc(store, spec, ["get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes"])).get("items", [])}
    except Exception as ex:
        nm, errors["node_metrics"] = {}, str(ex)[-150:]
    try:
        pm = json.loads(kube.oc(store, spec, ["get", "--raw", "/apis/metrics.k8s.io/v1beta1/pods"])).get("items", [])
    except Exception as ex:
        pm, errors["pod_metrics"] = [], str(ex)[-150:]
    req_node: Dict[str, List[float]] = {}
    pods_node: Dict[str, int] = {}
    ns_req: Dict[str, List[float]] = {}
    ns_pods: Dict[str, int] = {}
    for p in pods:
        ns = p["metadata"]["namespace"]
        c, m = _pod_requests(p)
        node = p["spec"].get("nodeName")
        if node:
            r = req_node.setdefault(node, [0.0, 0.0])
            r[0] += c
            r[1] += m
            pods_node[node] = pods_node.get(node, 0) + 1
        r = ns_req.setdefault(ns, [0.0, 0.0])
        r[0] += c
        r[1] += m
        ns_pods[ns] = ns_pods.get(ns, 0) + 1
    ns_use: Dict[str, List[float]] = {}
    for p in pm:
        ns = p["metadata"]["namespace"]
        u = ns_use.setdefault(ns, [0.0, 0.0])
        for c in p.get("containers", []):
            u[0] += parse(c["usage"].get("cpu"))
            u[1] += parse(c["usage"].get("memory"))
    fs: Dict[str, float] = {}
    pvc_use: Dict[str, float] = {}
    try:
        for r in _prom(store, spec, 'max by (instance) (1 - node_filesystem_avail_bytes{mountpoint=~"/sysroot|/"} / node_filesystem_size_bytes{mountpoint=~"/sysroot|/"})'):
            fs[r["metric"].get("instance", "")] = float(r["value"][1])
        for r in _prom(store, spec, "max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes)"):
            pvc_use[f"{r['metric'].get('namespace')}/{r['metric'].get('persistentvolumeclaim')}"] = float(r["value"][1])
    except Exception as ex:
        errors["prometheus"] = str(ex)[-150:]
    warnings: List[Dict] = []

    def warn(level, what, msg):
        warnings.append({"level": level, "what": what, "message": msg})

    out_nodes = []
    tot = {"cpu_alloc": 0.0, "mem_alloc": 0.0, "cpu_req": 0.0, "mem_req": 0.0, "cpu_use": 0.0, "mem_use": 0.0, "pods": 0, "pods_cap": 0}
    for n in nodes:
        name = n["metadata"]["name"]
        labels = n["metadata"].get("labels", {})
        roles = sorted(k.split("/")[1] for k in labels if k.startswith("node-role.kubernetes.io/"))
        alloc = n["status"].get("allocatable", {})
        ca, ma, pa = parse(alloc.get("cpu")), parse(alloc.get("memory")), int(parse(alloc.get("pods")))
        cr, mr = req_node.get(name, [0.0, 0.0])
        cu, mu = parse((nm.get(name) or {}).get("cpu")), parse((nm.get(name) or {}).get("memory"))
        unsched = bool((n.get("spec") or {}).get("unschedulable"))
        row = {"name": name, "roles": roles, "cpu_alloc": ca, "mem_alloc": ma, "cpu_req": cr, "mem_req": mr, "cpu_use": cu, "mem_use": mu,
               "pods": pods_node.get(name, 0), "pods_cap": pa, "fs": fs.get(name), "unschedulable": unsched,
               "cpu_req_pct": cr / ca if ca else 0, "mem_req_pct": mr / ma if ma else 0, "cpu_use_pct": cu / ca if ca else 0, "mem_use_pct": mu / ma if ma else 0}
        out_nodes.append(row)
        if not unsched:
            for k in ("cpu_alloc", "mem_alloc", "cpu_req", "mem_req", "cpu_use", "mem_use"):
                tot[k] += row[k]
            tot["pods"] += row["pods"]
            tot["pods_cap"] += pa
        for key, label in (("mem_req_pct", "memory requests"), ("cpu_req_pct", "CPU requests"), ("mem_use_pct", "memory usage"), ("cpu_use_pct", "CPU usage")):
            if row[key] >= CRIT:
                warn("fail", name, f"{label} at {row[key]:.0%} of allocatable")
            elif row[key] >= WARN:
                warn("warn", name, f"{label} at {row[key]:.0%} of allocatable")
        if row["fs"] is not None and row["fs"] >= 0.8:
            warn("fail" if row["fs"] >= 0.9 else "warn", name, f"root filesystem {row['fs']:.0%} full (kubelet starts evicting pods around 85-90%)")
        if pa and row["pods"] / pa >= 0.9:
            warn("warn", name, f"{row['pods']} of {pa} pods")
    for key, label in (("mem", "memory"), ("cpu", "CPU")):
        a = tot[f"{key}_alloc"]
        if a and tot[f"{key}_req"] / a >= 0.8:
            warn("warn", "cluster", f"{label} requests at {tot[key + '_req'] / a:.0%} of schedulable capacity: new workloads may stay Pending; add nodes")
    out_ns = []
    for ns in sorted(set(ns_req) | set(ns_use)):
        cr, mr = ns_req.get(ns, [0.0, 0.0])
        cu, mu = ns_use.get(ns, [0.0, 0.0])
        out_ns.append({"namespace": ns, "pods": ns_pods.get(ns, 0), "cpu_req": cr, "mem_req": mr, "cpu_use": cu, "mem_use": mu})
    out_ns.sort(key=lambda x: x["mem_use"], reverse=True)
    pvcs = []
    for p in (ops.get_opt(store, spec, "pvc", extra=["-A"]) or {}).get("items", []):
        key = f"{p['metadata']['namespace']}/{p['metadata']['name']}"
        size = parse(((p.get("status") or {}).get("capacity") or {}).get("storage") or p["spec"].get("resources", {}).get("requests", {}).get("storage"))
        used = pvc_use.get(key)
        pvcs.append({"pvc": key, "class": p["spec"].get("storageClassName", ""), "phase": (p.get("status") or {}).get("phase", ""), "size": size, "used_pct": used})
        if used is not None and used >= WARN:
            warn("fail" if used >= CRIT else "warn", key, f"volume {used:.0%} full")
        if (p.get("status") or {}).get("phase") == "Pending":
            warn("warn", key, "PVC is Pending (no storage class or no capacity)")
    pvcs.sort(key=lambda x: (x["used_pct"] is None, -(x["used_pct"] or 0)))
    warnings.sort(key=lambda w: (w["level"] != "fail", w["what"]))
    return {"totals": tot, "nodes": out_nodes, "namespaces": out_ns[:60], "pvcs": pvcs, "warnings": warnings, "errors": errors}
