"""Projects with quotas, limit ranges and role bindings."""
import re
from typing import Dict, List, Optional

from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, clusterops as ops

_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
SYSTEM = ("openshift", "kube-", "default", "openshift-")
ROLES = ("admin", "edit", "view")
QUOTA = "ocpdeploy-quota"
LIMITS = "ocpdeploy-limits"
_QTY = re.compile(r"^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|Ti|k|M|G|T)?$")


def is_system(ns: str) -> bool:
    return ns == "default" or ns == "openshift" or ns.startswith("openshift-") or ns.startswith("kube-")


def overview(store: ClusterStore, spec: ClusterSpec, include_system: bool = False) -> Dict:
    projs = ops.get(store, spec, "projects").get("items", [])
    quotas = ops.get_opt(store, spec, "resourcequotas", extra=["-A"]) or {}
    limits = ops.get_opt(store, spec, "limitranges", extra=["-A"]) or {}
    rbs = ops.get_opt(store, spec, "rolebindings", extra=["-A"]) or {}
    q_by: Dict[str, List] = {}
    for q in quotas.get("items", []):
        st = q.get("status") or {}
        q_by.setdefault(q["metadata"]["namespace"], []).append({"name": q["metadata"]["name"], "hard": st.get("hard") or q["spec"].get("hard", {}), "used": st.get("used", {})})
    l_by = {}
    for l in limits.get("items", []):
        l_by.setdefault(l["metadata"]["namespace"], []).append({"name": l["metadata"]["name"], "limits": l["spec"].get("limits", [])})
    b_by: Dict[str, List] = {}
    for b in rbs.get("items", []):
        role = b["roleRef"]["name"]
        if role not in ROLES:
            continue
        for sub in b.get("subjects") or []:
            if sub.get("kind") in ("User", "Group"):
                b_by.setdefault(b["metadata"]["namespace"], []).append({"binding": b["metadata"]["name"], "role": role, "kind": sub["kind"], "name": sub["name"]})
    out = []
    for p in projs:
        n = p["metadata"]["name"]
        if is_system(n) and not include_system:
            continue
        ann = p["metadata"].get("annotations") or {}
        out.append({"name": n, "display": ann.get("openshift.io/display-name", ""), "description": ann.get("openshift.io/description", ""),
                    "requester": ann.get("openshift.io/requester", ""), "phase": (p.get("status") or {}).get("phase", ""),
                    "age": ops.age(p["metadata"].get("creationTimestamp")), "system": is_system(n),
                    "quotas": q_by.get(n, []), "limits": l_by.get(n, []), "bindings": b_by.get(n, [])})
    out.sort(key=lambda x: (x["system"], x["name"]))
    users = [u["metadata"]["name"] for u in (ops.get_opt(store, spec, "users") or {}).get("items", [])]
    groups = [g["metadata"]["name"] for g in (ops.get_opt(store, spec, "groups") or {}).get("items", [])]
    return {"projects": out, "users": sorted(users), "groups": sorted(groups)}


def _qty(v: str, what: str) -> str:
    v = str(v).strip()
    if not _QTY.match(v):
        raise ValueError(f"{what}: '{v}' is not a quantity (e.g. 4, 500m, 8Gi)")
    return v


def quota_docs(ns: str, q: Dict) -> List[Dict]:
    docs = []
    hard = {}
    for key, field in (("requests.cpu", "cpu_requests"), ("limits.cpu", "cpu_limits"), ("requests.memory", "memory_requests"),
                       ("limits.memory", "memory_limits"), ("requests.storage", "storage"), ("pods", "pods"), ("persistentvolumeclaims", "pvcs")):
        v = (q or {}).get(field)
        if v not in (None, ""):
            hard[key] = _qty(v, key)
    if hard:
        docs.append({"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": QUOTA, "namespace": ns}, "spec": {"hard": hard}})
    lim = {}
    for key, field in (("defaultRequest", "default_request"), ("default", "default_limit")):
        ent = {}
        for res in ("cpu", "memory"):
            v = ((q or {}).get(field) or {}).get(res)
            if v not in (None, ""):
                ent[res] = _qty(v, f"{field}.{res}")
        if ent:
            lim[key] = ent
    if lim:
        docs.append({"apiVersion": "v1", "kind": "LimitRange", "metadata": {"name": LIMITS, "namespace": ns}, "spec": {"limits": [dict(type="Container", **lim)]}})
    return docs


def _binding_doc(ns: str, role: str, kind: str, name: str) -> Dict:
    if role not in ROLES:
        raise ValueError("role must be admin, edit or view")
    if kind not in ("User", "Group"):
        raise ValueError("kind must be User or Group")
    if not name or len(name) > 253 or not re.fullmatch(r"[A-Za-z0-9._@:+-]+", name):
        raise ValueError("bad user or group name")
    safe = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40] or "x"
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": f"ocpdeploy-{role}-{kind.lower()}-{safe}", "namespace": ns},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
            "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": kind, "name": name}]}


def create(store, spec, name: str, display: str = "", description: str = "", quota: Optional[Dict] = None, bindings: Optional[List[Dict]] = None) -> List[str]:
    if not _NAME.match(name or "") or is_system(name):
        raise ValueError("project name must be a lowercase DNS label and not a system name (openshift-*, kube-*, default)")
    docs = quota_docs(name, quota or {}) + [_binding_doc(name, b.get("role", ""), b.get("kind", ""), b.get("name", "")) for b in bindings or []]
    out = [kube.oc(store, spec, ["adm", "new-project", name] + ([f"--display-name={display}"] if display else []) + ([f"--description={description}"] if description else [])).strip()]
    if docs:
        out.append(ops.apply(store, spec, docs).strip())
    return out


def set_quota(store, spec, ns: str, quota: Dict) -> str:
    if not _NAME.match(ns or ""):
        raise ValueError("bad project")
    docs = quota_docs(ns, quota)
    msgs = []
    if not any(d["kind"] == "ResourceQuota" for d in docs):
        msgs.append(kube.oc(store, spec, ["delete", "resourcequota", QUOTA, "-n", ns, "--ignore-not-found"]).strip())
    if not any(d["kind"] == "LimitRange" for d in docs):
        msgs.append(kube.oc(store, spec, ["delete", "limitrange", LIMITS, "-n", ns, "--ignore-not-found"]).strip())
    if docs:
        msgs.append(ops.apply(store, spec, docs).strip())
    return "\n".join(m for m in msgs if m)


def add_binding(store, spec, ns: str, role: str, kind: str, name: str) -> str:
    if not _NAME.match(ns or ""):
        raise ValueError("bad project")
    return ops.apply(store, spec, [_binding_doc(ns, role, kind, name)]).strip()


def remove_binding(store, spec, ns: str, binding: str) -> str:
    if not _NAME.match(ns or "") or not re.fullmatch(r"[a-z0-9]([a-z0-9.:-]{0,251}[a-z0-9])?", binding or ""):
        raise ValueError("bad name")
    rb = ops.get(store, spec, "rolebinding", binding, ns=ns)
    if rb["roleRef"]["name"] not in ROLES:
        raise ValueError("only admin/edit/view bindings are managed here")
    return kube.oc(store, spec, ["delete", "rolebinding", binding, "-n", ns]).strip()


def delete(store, spec, ns: str) -> str:
    if not _NAME.match(ns or "") or is_system(ns):
        raise ValueError("system projects cannot be deleted here")
    return kube.oc(store, spec, ["delete", "project", ns, "--wait=false"]).strip()
