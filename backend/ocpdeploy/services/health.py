"""One-call cluster health snapshot for the Health page."""
import json
import subprocess
from typing import Dict, List

from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, clusterops as ops


def _alerts(store, spec) -> List[Dict]:
    out = kube.oc(store, spec, ["-n", "openshift-monitoring", "exec", "alertmanager-main-0", "-c", "alertmanager", "--",
                                "amtool", "--alertmanager.url=http://localhost:9093", "alert", "query", "-o", "json"], timeout=60)
    alerts = json.loads(out or "[]")
    res = []
    for a in alerts:
        lab = a.get("labels", {})
        if lab.get("alertname") == "Watchdog":
            continue
        res.append({"name": lab.get("alertname", ""), "severity": lab.get("severity", ""), "namespace": lab.get("namespace", ""),
                    "state": (a.get("status") or {}).get("state", ""), "since": a.get("startsAt", ""),
                    "summary": (a.get("annotations") or {}).get("summary") or (a.get("annotations") or {}).get("message") or (a.get("annotations") or {}).get("description", "")[:200]})
    sev = {"critical": 0, "warning": 1, "info": 2}
    res.sort(key=lambda x: (sev.get(x["severity"], 3), x["name"]))
    return res


def _events(store, spec, limit=25) -> List[Dict]:
    ev = ops.get(store, spec, "events", extra=["-A", "--field-selector", "type=Warning"])
    items = ev.get("items", [])
    items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or e["metadata"].get("creationTimestamp", ""), reverse=True)
    out = []
    for e in items[:limit]:
        o = e.get("involvedObject", {})
        out.append({"namespace": o.get("namespace", ""), "object": f"{o.get('kind', '')}/{o.get('name', '')}", "reason": e.get("reason", ""),
                    "message": (e.get("message") or "")[:220], "count": e.get("count", 1),
                    "last": e.get("lastTimestamp") or e.get("eventTime") or ""})
    return out


def snapshot(store: ClusterStore, spec: ClusterSpec) -> Dict:
    out: Dict = {"errors": {}}

    def part(key, fn):
        try:
            out[key] = fn()
        except Exception as ex:
            out["errors"][key] = str(ex)[-200:]

    part("operators", lambda: ops.co_states(store, spec))
    part("nodes", lambda: ops.nodes_summary(store, spec))
    part("mcps", lambda: ops.mcp_states(store, spec))
    part("machines", lambda: ops.machines(store, spec))
    part("csrs", lambda: ops.pending_csrs(store, spec))
    part("alerts", lambda: _alerts(store, spec))
    part("events", lambda: _events(store, spec))

    def version():
        cv = ops.get(store, spec, "clusterversion", "version")
        c = ops.conditions(cv)
        return {"version": cv["status"]["desired"]["version"], "channel": cv["spec"].get("channel", ""),
                "progressing": c.get("Progressing", {}).get("status") == "True", "message": c.get("Progressing", {}).get("message", ""),
                "failing": c.get("Failing", {}).get("status") == "True", "failing_message": c.get("Failing", {}).get("message", "")}
    part("version", version)

    def etcd():
        e = ops.get(store, spec, "etcd", "cluster")
        c = ops.conditions(e)
        return {"members_available": c.get("EtcdMembersAvailable", {}).get("message", ""),
                "degraded": c.get("EtcdMembersDegraded", {}).get("status") == "True",
                "degraded_message": c.get("EtcdMembersDegraded", {}).get("message", "")}
    part("etcd", etcd)

    def certs():
        from datetime import datetime, timezone
        out_c = {}
        s = ops.get(store, spec, "secret", "kube-apiserver-to-kubelet-signer", ns="openshift-kube-apiserver-operator")
        na = s["metadata"].get("annotations", {}).get("auth.openshift.io/certificate-not-after", "")
        if na:
            days = (datetime.fromisoformat(na.replace("Z", "+00:00")) - datetime.now(timezone.utc)).days
            out_c["kubelet_signer_not_after"] = na
            out_c["kubelet_signer_days"] = days
        return out_c
    part("certs", certs)

    def pvcs():
        p = ops.get(store, spec, "pvc", extra=["-A"])
        pend = [f"{i['metadata']['namespace']}/{i['metadata']['name']}" for i in p.get("items", []) if (i.get("status") or {}).get("phase") == "Pending"]
        return {"total": len(p.get("items", [])), "pending": pend}
    part("pvcs", pvcs)

    def pods():
        p = ops.get(store, spec, "pods", extra=["-A", "--field-selector", "status.phase!=Running,status.phase!=Succeeded"])
        return [{"namespace": i["metadata"]["namespace"], "name": i["metadata"]["name"], "phase": (i.get("status") or {}).get("phase", ""),
                 "reason": next((c["state"].get("waiting", {}).get("reason", "") for c in (i.get("status") or {}).get("containerStatuses", []) if c.get("state", {}).get("waiting")), "")}
                for i in p.get("items", [])][:40]
    part("unhealthy_pods", pods)
    return out
