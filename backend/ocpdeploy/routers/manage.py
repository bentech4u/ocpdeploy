"""Node maintenance, logs/events/must-gather, projects and capacity (installed and connected clusters)."""
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..services import maint, logsvc, projects, capacity, extranodes
from .day2 import _ready, _job, _read

router = APIRouter(prefix="/api/clusters/{name}", tags=["manage"])


def _bad(fn):
    try:
        return fn()
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    except RuntimeError as ex:
        raise HTTPException(409, str(ex))
    except Exception as ex:
        raise HTTPException(502, str(ex)[-300:])


# ---- maintenance
class NodeReq(BaseModel):
    node: str
    timeout: int = 900
    force: bool = False
    drain: bool = True
    delete_machine: bool = True
    confirm: str = ""


@router.get("/maintenance")
def maintenance(name: str):
    s, spec = _ready(name)
    return _read(lambda: maint.overview(s, spec))


@router.post("/maintenance/cordon")
def cordon(name: str, body: NodeReq):
    s, spec = _ready(name)
    return {"output": _bad(lambda: maint.cordon(s, spec, body.node, True))}


@router.post("/maintenance/uncordon")
def uncordon(name: str, body: NodeReq):
    s, spec = _ready(name)
    return {"output": _bad(lambda: maint.cordon(s, spec, body.node, False))}


@router.post("/maintenance/drain-check")
def drain_check(name: str, body: NodeReq):
    """Dry run: what would block draining this node (read-only)."""
    s, spec = _ready(name)
    return {"blockers": _bad(lambda: maint.drain_blockers(s, spec, body.node, body.force))}


@router.post("/maintenance/drain")
def drain(name: str, body: NodeReq):
    s, spec = _ready(name)
    return _job(s, "drain", lambda ctx: maint.job_drain(ctx, s, spec, body.node, body.timeout, body.force))


@router.post("/maintenance/reboot")
def reboot(name: str, body: NodeReq):
    s, spec = _ready(name)
    return _job(s, "reboot", lambda ctx: maint.job_reboot(ctx, s, spec, body.node, body.drain, body.timeout, body.force))


@router.post("/maintenance/remove")
def remove(name: str, body: NodeReq):
    if body.confirm != body.node:
        raise HTTPException(422, "type the node name to confirm")
    s, spec = _ready(name)
    node = next((n for n in _read(lambda: maint.overview(s, spec))["nodes"] if n["name"] == body.node), None)
    if not node:
        raise HTTPException(404, f"node {body.node} not found")
    if node["master"]:
        raise HTTPException(422, "control-plane nodes cannot be removed here")
    return _job(s, "remove-node", lambda ctx: maint.job_remove(ctx, s, spec, body.node, body.delete_machine, body.timeout, body.force))


# ---- logs and events
@router.get("/logs/namespaces")
def log_namespaces(name: str):
    s, spec = _ready(name)
    return _read(lambda: logsvc.namespaces(s, spec))


@router.get("/logs/pods")
def log_pods(name: str, ns: str):
    s, spec = _ready(name)
    return _bad(lambda: logsvc.pods(s, spec, ns))


@router.get("/logs/pod", response_class=PlainTextResponse)
def log_pod(name: str, ns: str, pod: str, container: str = "", tail: int = 500, previous: bool = False):
    s, spec = _ready(name)
    return PlainTextResponse(_bad(lambda: logsvc.pod_log(s, spec, ns, pod, container, tail, previous)))


@router.get("/logs/events")
def log_events(name: str, ns: str = "", warnings: bool = True, q: str = "", limit: int = 300):
    s, spec = _ready(name)
    return _bad(lambda: logsvc.events(s, spec, ns, warnings, q, limit))


# ---- must-gather (archives are kept like node ISOs, until deleted)
class MustGatherReq(BaseModel):
    since: str = ""
    images: List[str] = []


@router.get("/mustgather")
def mustgather_list(name: str):
    _ready(name)
    return [b for b in extranodes.list_builds(name) if b["kind"] == "mustgather"]


@router.post("/mustgather/run")
def mustgather_run(name: str, body: MustGatherReq):
    s, spec = _ready(name)
    return _job(s, "must-gather", lambda ctx: logsvc.job_mustgather(ctx, s, spec, body.since.strip(), [i.strip() for i in body.images if i.strip()]))


# ---- projects
class ProjectReq(BaseModel):
    name: str
    display: str = ""
    description: str = ""
    quota: Dict = {}
    bindings: List[Dict] = []


class QuotaReq(BaseModel):
    quota: Dict = {}


class BindingReq(BaseModel):
    role: str
    kind: str
    name: str


class ConfirmReq(BaseModel):
    confirm: str


@router.get("/projects")
def projects_list(name: str, system: bool = False):
    s, spec = _ready(name)
    return _read(lambda: projects.overview(s, spec, system))


@router.post("/projects")
def projects_create(name: str, body: ProjectReq):
    s, spec = _ready(name)
    return {"output": _bad(lambda: projects.create(s, spec, body.name, body.display, body.description, body.quota, body.bindings))}


@router.put("/projects/{project}/quota")
def projects_quota(name: str, project: str, body: QuotaReq):
    s, spec = _ready(name)
    return {"output": _bad(lambda: projects.set_quota(s, spec, project, body.quota))}


@router.post("/projects/{project}/bindings")
def projects_bind(name: str, project: str, body: BindingReq):
    s, spec = _ready(name)
    return {"output": _bad(lambda: projects.add_binding(s, spec, project, body.role, body.kind, body.name))}


@router.delete("/projects/{project}/bindings/{binding}")
def projects_unbind(name: str, project: str, binding: str):
    s, spec = _ready(name)
    return {"output": _bad(lambda: projects.remove_binding(s, spec, project, binding))}


@router.post("/projects/{project}/delete")
def projects_delete(name: str, project: str, body: ConfirmReq):
    if body.confirm != project:
        raise HTTPException(422, "type the project name to confirm")
    s, spec = _ready(name)
    return {"output": _bad(lambda: projects.delete(s, spec, project))}


# ---- capacity
@router.get("/capacity")
def capacity_view(name: str):
    s, spec = _ready(name)
    return _read(lambda: capacity.snapshot(s, spec))
