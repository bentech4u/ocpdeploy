"""Cluster upgrades through the Cluster Version Operator."""
import json
import time
from typing import Dict, List, Optional

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, tools, clusterops as ops


def info(store: ClusterStore, spec: ClusterSpec) -> Dict:
    cv = ops.get(store, spec, "clusterversion", "version")
    c = ops.conditions(cv)
    st = cv.get("status", {})
    out = {
        "version": st.get("desired", {}).get("version", ""),
        "channel": cv["spec"].get("channel", ""),
        "channels": st.get("desired", {}).get("channels", []),
        "available_updates": [{"version": u.get("version"), "image": u.get("image")} for u in (st.get("availableUpdates") or [])],
        "conditional_updates": [{"version": u.get("release", {}).get("version"), "risks": [r.get("name") for r in u.get("risks", [])]} for u in (st.get("conditionalUpdates") or [])],
        "history": [{"version": h.get("version"), "state": h.get("state"), "started": h.get("startedTime"), "completed": h.get("completionTime")} for h in st.get("history", [])[:6]],
        "progressing": c.get("Progressing", {}).get("status") == "True",
        "message": c.get("Progressing", {}).get("message", ""),
        "failing": c.get("Failing", {}).get("status") == "True",
        "failing_message": c.get("Failing", {}).get("message", ""),
        "retrieved_updates": c.get("RetrievedUpdates", {}).get("status") == "True",
        "retrieved_message": c.get("RetrievedUpdates", {}).get("message", ""),
        "upgradeable": c.get("Upgradeable", {}).get("status") != "False",
        "upgradeable_reason": c.get("Upgradeable", {}).get("reason", ""),
        "upgradeable_message": c.get("Upgradeable", {}).get("message", ""),
        "mirror": spec.mirror.enabled,
    }
    out["available_updates"].sort(key=lambda u: [int(x) if x.isdigit() else x for x in (u["version"] or "").replace("-", ".").split(".")], reverse=True)
    gates = ops.get_opt(store, spec, "configmap", "admin-gates", ns="openshift-config-managed") or {}
    acks = ops.get_opt(store, spec, "configmap", "admin-acks", ns="openshift-config") or {}
    out["admin_gates"] = [{"key": k, "message": v, "acked": (acks.get("data") or {}).get(k) == "true"} for k, v in (gates.get("data") or {}).items()]
    return out


def set_channel(store: ClusterStore, spec: ClusterSpec, channel: str) -> str:
    return kube.oc(store, spec, ["adm", "upgrade", "channel", channel, "--allow-explicit-channel"], timeout=60)


def ack_gates(store: ClusterStore, spec: ClusterSpec) -> List[str]:
    gates = ops.get_opt(store, spec, "configmap", "admin-gates", ns="openshift-config-managed") or {}
    keys = list((gates.get("data") or {}).keys())
    if keys:
        ops.patch(store, spec, "configmap/admin-acks", {"data": {k: "true" for k in keys}}, ns="openshift-config")
    return keys


def job_upgrade(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, target: str, force: bool = False):
    if not target:
        raise RuntimeError("no target version given")
    cur = info(store, spec)
    ctx.log(f"Current version {cur['version']} on channel {cur['channel'] or '-'}; target {target}")
    if not cur["upgradeable"] and not force:
        raise RuntimeError(f"cluster reports Upgradeable=False ({cur['upgradeable_reason']}): {cur['upgradeable_message'][:300]}")
    cmd = ["adm", "upgrade"]
    if spec.mirror.enabled and spec.mirror.registry:
        image = f"{spec.mirror.registry}/openshift/release-images:{target}-x86_64"
        ctx.log(f"Disconnected cluster: upgrading by explicit release image {image}")
        cmd += ["--to-image", image, "--allow-explicit-upgrade"]
    else:
        cmd += ["--to", target]
    if force:
        cmd += ["--force", "--allow-upgrade-with-warnings"]
    ctx.log("$ oc " + " ".join(cmd))
    ctx.log(kube.oc(store, spec, cmd, timeout=120).strip())
    start = time.time()

    def tick() -> str:
        cv = ops.get(store, spec, "clusterversion", "version")
        c = ops.conditions(cv)
        cos = ops.co_states(store, spec)
        done = sum(1 for o in cos if o["version"] == target)
        mcps = ops.mcp_states(store, spec)
        mcp = " ".join(f"{m['name']} {m['updated']}/{m['machines']}" for m in mcps)
        fail = f" | FAILING: {c.get('Failing', {}).get('message', '')[:120]}" if c.get("Failing", {}).get("status") == "True" else ""
        return f"[{int((time.time() - start) // 60)} min] operators {done}/{len(cos)} at {target} · pools {mcp} · {c.get('Progressing', {}).get('message', '')[:140]}{fail}"

    def finished() -> bool:
        cv = ops.get(store, spec, "clusterversion", "version")
        hist = cv.get("status", {}).get("history", [{}])
        c = ops.conditions(cv)
        if hist and hist[0].get("version") == target and hist[0].get("state") == "Completed" and c.get("Progressing", {}).get("status") == "False":
            mcps = ops.mcp_states(store, spec)
            return all(m["updated"] == m["machines"] and not m["updating"] for m in mcps)
        return False

    ops.wait_for(f"upgrade to {target}", finished, timeout=5 * 3600, interval=60, log=ctx.log, on_tick=tick)
    ctx.log(f"Cluster is now at {target}")
    store.patch(lambda raw: raw.__setitem__("ocp_version", target))
    try:
        tools.ensure(target, ctx.log)
    except Exception as ex:
        ctx.log(f"note: could not download oc/openshift-install {target}: {ex}")
