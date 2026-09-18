"""Thin wrapper around `oc` against the cluster's kubeconfig."""
import json
import os
import subprocess
from typing import Any, List, Optional

from ..models import ClusterSpec
from ..store import ClusterStore
from . import tools


def kubeconfig(store: ClusterStore) -> str:
    return str(store.install_dir / "auth" / "kubeconfig")


def env(store: ClusterStore) -> dict:
    e = dict(os.environ)
    e["KUBECONFIG"] = kubeconfig(store)
    from ..settings import IMPORTED_DIR
    if store.dir.parent == IMPORTED_DIR:
        # connected cluster: oc's discovery/HTTP cache goes to its RAM directory, not ~/.kube
        e["HOME"] = str(store.dir)
        e["KUBECACHEDIR"] = str(store.dir / ".kube" / "cache")
    trust = store.dir / "ca-trust.pem"
    if trust.exists():
        # system CAs + vCenter / mirror registry CAs written by deploy.trust_env
        e["SSL_CERT_FILE"] = str(trust)
    return e


def min_env(store: ClusterStore) -> dict:
    """The few variables oc needs, for long commands run as transient units (ctx.run)."""
    e = {"KUBECONFIG": kubeconfig(store)}
    full = env(store)
    for k in ("HOME", "KUBECACHEDIR", "SSL_CERT_FILE"):
        if k in full and (k != "HOME" or "KUBECACHEDIR" in full):
            e[k] = full[k]
    return e


def oc_bin(spec: ClusterSpec) -> str:
    p = tools.tool_path(spec.ocp_version, "oc")
    if not p.exists():
        if spec.imported:
            # connected clusters may run a version whose client could not be downloaded
            alt = tools.any_oc()
            if alt:
                return str(alt)
        raise RuntimeError(f"oc for {spec.ocp_version} not downloaded")
    return str(p)


def oc(store: ClusterStore, spec: ClusterSpec, args: List[str], input_text: Optional[str] = None,
       check: bool = True, timeout: int = 120) -> str:
    cmd = [oc_bin(spec)] + args
    r = subprocess.run(cmd, env=env(store), input=input_text, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"oc {' '.join(args)} failed: {r.stderr.strip()[-400:]}")
    return r.stdout


def oc_json(store: ClusterStore, spec: ClusterSpec, args: List[str]) -> Any:
    out = oc(store, spec, args + ["-o", "json"])
    return json.loads(out) if out.strip() else {}


def apply(store: ClusterStore, spec: ClusterSpec, manifest_yaml: str) -> str:
    return oc(store, spec, ["apply", "-f", "-"], input_text=manifest_yaml)


def cluster_status(store: ClusterStore, spec: ClusterSpec) -> dict:
    """Snapshot for the dashboard; tolerant of an unreachable API."""
    out = {"reachable": False}
    if not os.path.exists(kubeconfig(store)):
        out["reason"] = "no kubeconfig yet"
        return out
    try:
        nodes = oc_json(store, spec, ["get", "nodes"])
        cos = oc_json(store, spec, ["get", "clusteroperators"])
        cv = oc_json(store, spec, ["get", "clusterversion", "version"])
    except Exception as ex:
        out["reason"] = str(ex)[-300:]
        return out
    out["reachable"] = True
    out["nodes"] = []
    for n in nodes.get("items", []):
        labels = n["metadata"].get("labels", {})
        roles = sorted(k.split("/")[1] for k in labels if k.startswith("node-role.kubernetes.io/"))
        ready = next((c["status"] for c in n["status"].get("conditions", []) if c["type"] == "Ready"), "Unknown")
        addr = next((a["address"] for a in n["status"].get("addresses", []) if a["type"] == "InternalIP"), "")
        out["nodes"].append({"name": n["metadata"]["name"], "roles": roles, "ready": ready, "ip": addr,
                             "version": n["status"].get("nodeInfo", {}).get("kubeletVersion", "")})
    out["operators"] = []
    for co in cos.get("items", []):
        conds = {c["type"]: c["status"] for c in co["status"].get("conditions", [])}
        out["operators"].append({"name": co["metadata"]["name"], "available": conds.get("Available"),
                                 "progressing": conds.get("Progressing"), "degraded": conds.get("Degraded"),
                                 "version": next((v["version"] for v in co["status"].get("versions", []) if v["name"] == "operator"), "")})
    hist = cv.get("status", {}).get("history", [{}])
    out["version"] = {"desired": cv.get("status", {}).get("desired", {}).get("version"),
                      "state": hist[0].get("state") if hist else None,
                      "conditions": {c["type"]: c["status"] for c in cv.get("status", {}).get("conditions", [])}}
    return out


def approve_pending_csrs(store: ClusterStore, spec: ClusterSpec, log) -> int:
    csrs = oc_json(store, spec, ["get", "csr"])
    n = 0
    for c in csrs.get("items", []):
        if not c.get("status", {}).get("conditions"):
            oc(store, spec, ["adm", "certificate", "approve", c["metadata"]["name"]], check=False)
            log(f"approved CSR {c['metadata']['name']}")
            n += 1
    return n
