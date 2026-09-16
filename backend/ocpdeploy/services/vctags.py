"""vSphere tag categories and tags for failure domains.

The installer expects the `openshift-region` tag category attached to each
datacenter and `openshift-zone` attached to each compute cluster referenced by
a failure domain. pyVmomi cannot manage tags, so this uses the vSphere
Automation REST API (vCenter 7+)."""
from typing import Dict, List, Optional

import httpx
from pyVmomi import vim

from ..models import VCenterSpec, FailureDomain
from . import vcenter

REGION_CAT = "openshift-region"
ZONE_CAT = "openshift-zone"


class _Rest:
    def __init__(self, vc: VCenterSpec):
        self.c = httpx.Client(base_url=f"https://{vc.host}", verify=False, timeout=30)
        r = self.c.post("/api/session", auth=(vc.username, vc.password))
        if r.status_code != 201:
            raise RuntimeError(f"vCenter REST login failed: HTTP {r.status_code} {r.text[:120]}")
        self.c.headers["vmware-api-session-id"] = r.json()

    def close(self):
        try:
            self.c.delete("/api/session")
        finally:
            self.c.close()

    def _j(self, r: httpx.Response):
        if r.status_code >= 400:
            raise RuntimeError(f"vCenter REST {r.request.method} {r.request.url.path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else None

    def categories(self) -> Dict[str, Dict]:
        out = {}
        for cid in self._j(self.c.get("/api/cis/tagging/category")):
            cat = self._j(self.c.get(f"/api/cis/tagging/category/{cid}"))
            out[cat["name"]] = cat
        return out

    def tags_in(self, cat_id: str) -> Dict[str, str]:
        ids = self._j(self.c.post("/api/cis/tagging/tag", params={"action": "list-tags-for-category"}, json={"category_id": cat_id})) or []
        out = {}
        for tid in ids:
            t = self._j(self.c.get(f"/api/cis/tagging/tag/{tid}"))
            out[t["name"]] = tid
        return out

    def create_category(self, name: str, types: List[str], description: str) -> str:
        return self._j(self.c.post("/api/cis/tagging/category", json={
            "name": name, "description": description, "cardinality": "SINGLE", "associable_types": types}))

    def create_tag(self, cat_id: str, name: str, description: str) -> str:
        return self._j(self.c.post("/api/cis/tagging/tag", json={"category_id": cat_id, "name": name, "description": description}))

    def attached(self, tag_id: str) -> List[Dict]:
        return self._j(self.c.post(f"/api/cis/tagging/tag-association/{tag_id}", params={"action": "list-attached-objects"})) or []

    def attach(self, tag_id: str, obj_type: str, obj_id: str):
        self._j(self.c.post(f"/api/cis/tagging/tag-association/{tag_id}", params={"action": "attach"},
                            json={"object_id": {"type": obj_type, "id": obj_id}}))


def _objects(vc: VCenterSpec, fds: List[FailureDomain]) -> Dict[str, Optional[str]]:
    """Managed object IDs of every datacenter and cluster the failure domains use."""
    out: Dict[str, Optional[str]] = {}
    with vcenter.session(vc) as si:
        content = si.content
        for fd in fds:
            dc = vcenter._find(content, vim.Datacenter, fd.datacenter)
            out[f"dc:{fd.datacenter}"] = dc._moId if dc else None
            cl = vcenter._find(content, vim.ClusterComputeResource, fd.cluster, dc.hostFolder if dc else None) if dc else None
            out[f"cl:{fd.datacenter}/{fd.cluster}"] = cl._moId if cl else None
    return out


def check(vc: VCenterSpec, fds: List[FailureDomain]) -> List[Dict]:
    rows: List[Dict] = []
    objs = _objects(vc, fds)
    rest = _Rest(vc)
    try:
        cats = rest.categories()
        for cname in (REGION_CAT, ZONE_CAT):
            ok = cname in cats
            rows.append({"name": f"tag category {cname}", "status": "pass" if ok else "fail", "expected": "exists",
                         "actual": "found" if ok else "missing", "hint": "" if ok else "Press 'Create region/zone tags' in the vCenter step"})
        region_tags = rest.tags_in(cats[REGION_CAT]["id"]) if REGION_CAT in cats else {}
        zone_tags = rest.tags_in(cats[ZONE_CAT]["id"]) if ZONE_CAT in cats else {}
        for fd in fds:
            for kind, tags, tag_name, key, otype in (
                    ("region", region_tags, fd.region, f"dc:{fd.datacenter}", "Datacenter"),
                    ("zone", zone_tags, fd.zone, f"cl:{fd.datacenter}/{fd.cluster}", "ClusterComputeResource")):
                target = fd.datacenter if kind == "region" else fd.cluster
                moid = objs.get(key)
                if moid is None:
                    rows.append({"name": f"{fd.name}: {kind} tag {tag_name}", "status": "fail", "expected": f"attached to {target}",
                                 "actual": f"{otype} {target} not found", "hint": "Fix the failure domain first"})
                    continue
                tid = tags.get(tag_name)
                if not tid:
                    rows.append({"name": f"{fd.name}: {kind} tag {tag_name}", "status": "fail", "expected": f"attached to {target}",
                                 "actual": "tag missing", "hint": "Press 'Create region/zone tags' in the vCenter step"})
                    continue
                attached = any(o.get("id") == moid for o in rest.attached(tid))
                rows.append({"name": f"{fd.name}: {kind} tag {tag_name}", "status": "pass" if attached else "fail",
                             "expected": f"attached to {target}", "actual": "attached" if attached else "not attached",
                             "hint": "" if attached else "Press 'Create region/zone tags' in the vCenter step"})
    finally:
        rest.close()
    return rows


def ensure(vc: VCenterSpec, fds: List[FailureDomain], log=print) -> List[Dict]:
    """Create the categories/tags that are missing and attach them; returns the check rows afterwards."""
    objs = _objects(vc, fds)
    rest = _Rest(vc)
    try:
        cats = rest.categories()
        ids = {}
        for cname, types in ((REGION_CAT, ["Datacenter"]), (ZONE_CAT, ["ClusterComputeResource"])):
            if cname in cats:
                ids[cname] = cats[cname]["id"]
            else:
                ids[cname] = rest.create_category(cname, types, "OpenShift failure domains (created by ocpdeploy)")
                log(f"created tag category {cname}")
        region_tags = rest.tags_in(ids[REGION_CAT])
        zone_tags = rest.tags_in(ids[ZONE_CAT])
        for fd in fds:
            for kind, tags, tag_name, key, otype, cat in (
                    ("region", region_tags, fd.region, f"dc:{fd.datacenter}", "Datacenter", REGION_CAT),
                    ("zone", zone_tags, fd.zone, f"cl:{fd.datacenter}/{fd.cluster}", "ClusterComputeResource", ZONE_CAT)):
                if not tag_name:
                    raise RuntimeError(f"failure domain {fd.name} has no {kind}")
                moid = objs.get(key)
                if moid is None:
                    raise RuntimeError(f"{otype} for failure domain {fd.name} not found in vCenter")
                tid = tags.get(tag_name)
                if not tid:
                    tid = rest.create_tag(ids[cat], tag_name, f"OpenShift {kind} (ocpdeploy)")
                    tags[tag_name] = tid
                    log(f"created {kind} tag {tag_name}")
                if not any(o.get("id") == moid for o in rest.attached(tid)):
                    rest.attach(tid, otype, moid)
                    log(f"attached {kind} tag {tag_name} to {otype} {moid}")
    finally:
        rest.close()
    return check(vc, fds)
