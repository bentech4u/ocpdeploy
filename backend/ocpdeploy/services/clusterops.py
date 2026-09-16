"""Shared helpers for day-2 operations on a running cluster (thin layer over kube.oc)."""
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube

MAPI_NS = "openshift-machine-api"


def get(store: ClusterStore, spec: ClusterSpec, kind: str, name: str = "", ns: Optional[str] = None, extra: Optional[List[str]] = None) -> Any:
    args = ["get", kind] + ([name] if name else []) + (["-n", ns] if ns else []) + (extra or [])
    return kube.oc_json(store, spec, args)


def get_opt(store, spec, kind, name="", ns=None, extra=None) -> Optional[Any]:
    try:
        return get(store, spec, kind, name, ns, extra)
    except Exception:
        return None


def patch(store: ClusterStore, spec: ClusterSpec, target: str, body: Dict, ns: Optional[str] = None, ptype: str = "merge", subresource: Optional[str] = None) -> str:
    args = ["patch", target, "--type", ptype, "-p", json.dumps(body)] + (["-n", ns] if ns else [])
    if subresource:
        args += ["--subresource", subresource]
    return kube.oc(store, spec, args)


def apply(store: ClusterStore, spec: ClusterSpec, docs: List[Dict]) -> str:
    import yaml
    y = "---\n".join(yaml.safe_dump(d, sort_keys=False) for d in docs)
    return kube.apply(store, spec, y)


def dry_run(store: ClusterStore, spec: ClusterSpec, docs: List[Dict]) -> str:
    """Server-side dry run: validates against the live API without persisting anything."""
    import yaml
    y = "---\n".join(yaml.safe_dump(d, sort_keys=False) for d in docs)
    return kube.oc(store, spec, ["apply", "-f", "-", "--dry-run=server"], input_text=y)


def wait_for(what: str, check: Callable[[], bool], timeout: int, interval: int, log, on_tick: Optional[Callable[[], str]] = None):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            if check():
                log(f"{what}: done")
                return True
        except Exception as ex:
            msg = f"{what}: {str(ex)[-120:]}"
            if msg != last:
                log(msg)
                last = msg
            time.sleep(interval)
            continue
        if on_tick:
            try:
                msg = on_tick()
                if msg and msg != last:
                    log(msg)
                    last = msg
            except Exception:
                pass
        time.sleep(interval)
    raise RuntimeError(f"timed out after {timeout}s waiting for {what}")


def conditions(obj: Dict) -> Dict[str, Dict]:
    return {c["type"]: c for c in (obj.get("status") or {}).get("conditions", [])}


def cond_true(obj: Dict, ctype: str) -> bool:
    return conditions(obj).get(ctype, {}).get("status") == "True"


def co_states(store, spec) -> List[Dict]:
    cos = get(store, spec, "clusteroperators")
    out = []
    for co in cos.get("items", []):
        c = conditions(co)
        out.append({"name": co["metadata"]["name"],
                    "available": c.get("Available", {}).get("status"), "progressing": c.get("Progressing", {}).get("status"),
                    "degraded": c.get("Degraded", {}).get("status"),
                    "message": (c.get("Degraded", {}).get("message") if c.get("Degraded", {}).get("status") == "True" else c.get("Progressing", {}).get("message") if c.get("Progressing", {}).get("status") == "True" else "") or "",
                    "version": next((v["version"] for v in co["status"].get("versions", []) if v["name"] == "operator"), "")})
    return out


def co_healthy(store, spec) -> bool:
    return all(o["available"] == "True" and o["degraded"] != "True" and o["progressing"] != "True" for o in co_states(store, spec))


def age(ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        s = int((datetime.now(timezone.utc) - t).total_seconds())
        if s < 3600:
            return f"{s // 60}m"
        if s < 86400:
            return f"{s // 3600}h"
        return f"{s // 86400}d"
    except Exception:
        return ts


def nodes_summary(store, spec) -> List[Dict]:
    nodes = get(store, spec, "nodes")
    out = []
    for n in nodes.get("items", []):
        labels = n["metadata"].get("labels", {})
        roles = sorted(k.split("/")[1] for k in labels if k.startswith("node-role.kubernetes.io/"))
        c = conditions(n)
        out.append({"name": n["metadata"]["name"], "roles": roles,
                    "ready": c.get("Ready", {}).get("status", "Unknown"),
                    "memory_pressure": c.get("MemoryPressure", {}).get("status") == "True",
                    "disk_pressure": c.get("DiskPressure", {}).get("status") == "True",
                    "pid_pressure": c.get("PIDPressure", {}).get("status") == "True",
                    "unschedulable": bool(n["spec"].get("unschedulable")),
                    "ip": next((a["address"] for a in n["status"].get("addresses", []) if a["type"] == "InternalIP"), ""),
                    "version": n["status"].get("nodeInfo", {}).get("kubeletVersion", ""),
                    "age": age(n["metadata"].get("creationTimestamp")),
                    "cpu": n["status"].get("allocatable", {}).get("cpu"), "memory": n["status"].get("allocatable", {}).get("memory")})
    return out


def pending_csrs(store, spec) -> List[Dict]:
    csrs = get(store, spec, "csr")
    out = []
    for c in csrs.get("items", []):
        if not (c.get("status") or {}).get("conditions"):
            out.append({"name": c["metadata"]["name"], "requestor": c["spec"].get("username", ""),
                        "signer": c["spec"].get("signerName", ""), "age": age(c["metadata"].get("creationTimestamp"))})
    return out


def approve_csrs(store, spec, log=print) -> List[str]:
    names = [c["name"] for c in pending_csrs(store, spec)]
    for n in names:
        kube.oc(store, spec, ["adm", "certificate", "approve", n], check=False)
        log(f"approved CSR {n}")
    return names


def machinesets(store, spec) -> List[Dict]:
    mss = get(store, spec, "machinesets", ns=MAPI_NS)
    out = []
    for m in mss.get("items", []):
        pv = m["spec"]["template"]["spec"]["providerSpec"]["value"]
        devs = (pv.get("network") or {}).get("devices") or []
        static = any(d.get("addressesFromPools") or d.get("ipAddrs") for d in devs)
        st = m.get("status", {})
        out.append({"name": m["metadata"]["name"], "role": m["spec"]["template"]["metadata"]["labels"].get("machine.openshift.io/cluster-api-machine-role", ""),
                    "replicas": m["spec"].get("replicas", 0), "ready": st.get("readyReplicas", 0), "available": st.get("availableReplicas", 0),
                    "static_ip": static, "cpus": pv.get("numCPUs"), "memory_mb": pv.get("memoryMiB"), "disk_gb": pv.get("diskGiB"),
                    "template": pv.get("template", ""), "created_by_app": "-infra-" in m["metadata"]["name"] or m["spec"]["template"]["metadata"]["labels"].get("machine.openshift.io/cluster-api-machine-role", "") not in ("worker", "master")})
    return out


def machines(store, spec) -> List[Dict]:
    ms = get(store, spec, "machines", ns=MAPI_NS)
    out = []
    for m in ms.get("items", []):
        owner = (m["metadata"].get("ownerReferences") or [{}])[0]
        out.append({"name": m["metadata"]["name"], "phase": (m.get("status") or {}).get("phase", ""),
                    "role": m["metadata"].get("labels", {}).get("machine.openshift.io/cluster-api-machine-role", ""),
                    "machineset": owner.get("name", "") if owner.get("kind") == "MachineSet" else "",
                    "node": ((m.get("status") or {}).get("nodeRef") or {}).get("name", ""),
                    "ip": next((a["address"] for a in (m.get("status") or {}).get("addresses", []) if a.get("type") == "InternalIP"), ""),
                    "age": age(m["metadata"].get("creationTimestamp"))})
    return out


def mcp_states(store, spec) -> List[Dict]:
    mcps = get(store, spec, "mcp")
    out = []
    for p in mcps.get("items", []):
        st = p.get("status", {})
        c = conditions(p)
        out.append({"name": p["metadata"]["name"], "machines": st.get("machineCount", 0), "updated": st.get("updatedMachineCount", 0),
                    "ready": st.get("readyMachineCount", 0), "degraded": st.get("degradedMachineCount", 0),
                    "updating": c.get("Updating", {}).get("status") == "True", "is_degraded": c.get("Degraded", {}).get("status") == "True"})
    return out


def csv_states(store, spec) -> List[Dict]:
    csvs = get(store, spec, "csv", extra=["-A"])
    out = []
    for c in csvs.get("items", []):
        out.append({"name": c["metadata"]["name"], "namespace": c["metadata"]["namespace"], "display": c["spec"].get("displayName", ""),
                    "version": c["spec"].get("version", ""), "phase": (c.get("status") or {}).get("phase", ""),
                    "message": (c.get("status") or {}).get("message", "")[:200]})
    return out


def subscriptions(store, spec) -> List[Dict]:
    subs = get(store, spec, "subscriptions.operators.coreos.com", extra=["-A"])
    out = []
    for s in subs.get("items", []):
        st = s.get("status") or {}
        out.append({"name": s["metadata"]["name"], "namespace": s["metadata"]["namespace"], "package": s["spec"].get("name", ""),
                    "channel": s["spec"].get("channel", ""), "source": s["spec"].get("source", ""),
                    "installed_csv": st.get("installedCSV", ""), "current_csv": st.get("currentCSV", ""), "state": st.get("state", "")})
    return out


def label_node(store, spec, node: str, labels: Dict[str, str]):
    kube.oc(store, spec, ["label", "node", node] + [f"{k}={v}" for k, v in labels.items()] + ["--overwrite"])
