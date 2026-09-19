"""Dell PowerScale CSI: status, pre-checks, install/upgrade, replication and DR actions (installed and
connected clusters), plus the installer-wide offline bundle (/api/bundles/dell)."""
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import imported
from ..models import PowerScaleSpec
from ..services import powerscale as ps, powerscale_repl as repl, dellbundle, onefs
from ..store import list_clusters
from .day2 import _ready, _job, _read

router = APIRouter(prefix="/api/clusters/{name}/powerscale", tags=["powerscale"])
bundles = APIRouter(prefix="/api/bundles/dell", tags=["powerscale"])


class CfgReq(BaseModel):
    config: PowerScaleSpec
    passwords: Dict[str, str] = Field(default_factory=dict)   # array name -> password; never stored
    nodes: bool = True


class PathReq(BaseModel):
    config: PowerScaleSpec
    array: str
    password: str = ""


class UninstallReq(BaseModel):
    confirm: str = ""
    remove_classes: bool = False


class ActionReq(BaseModel):
    cluster: str
    group: str
    action: str
    confirm: str = ""


def _bad(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    except RuntimeError as ex:
        raise HTTPException(409, str(ex))
    except Exception as ex:
        raise HTTPException(502, str(ex)[-300:])


def _peer_allowed(request: Request, peer: str, write: bool):
    """A connected peer must belong to this session (and be writable for changes)."""
    if not peer:
        return
    if imported.exists(peer):
        c = imported.info(peer) or {}
        if c.get("owner") != getattr(request.state, "session_id", None):
            raise HTTPException(404, f"cluster {peer} not found")
        if write and c.get("read_only"):
            raise HTTPException(403, f"{peer} is a read-only connection")


def _blocking(rows):
    return [r for r in rows if r["status"] == "fail"]


# ------------------------------------------------------------------ status / config
@router.get("")
def get_status(name: str):
    s, spec = _ready(name)
    return _read(lambda: ps.status(s, spec))


@router.get("/config")
def get_config(name: str):
    s, spec = _ready(name)
    return _read(lambda: ps.prefill(s, spec))


@router.get("/peers")
def peers(name: str, request: Request):
    out = []
    for c in list_clusters():
        if c.get("name") != name and c.get("status") == "installed":
            out.append({"name": c["name"], "kind": "installed"})
    for c in imported.list_for(getattr(request.state, "session_id", "")):
        if c["name"] != name:
            out.append({"name": c["name"], "kind": "connected", "read_only": c["read_only"]})
    return out


@router.post("/check")
def check(name: str, body: CfgReq):
    s, spec = _ready(name)
    # the node test starts an oc debug pod, which a read-only connection must not do
    return _bad(lambda: ps.full_check(s, spec, body.config, body.passwords, with_nodes=body.nodes and not spec.read_only))


@router.post("/preview")
def preview(name: str, body: CfgReq):
    s, spec = _ready(name)
    return _bad(lambda: ps.preview(s, spec, body.config))


@router.post("/create-path")
def create_path(name: str, body: PathReq):
    s, spec = _ready(name)
    a = next((x for x in body.config.arrays if x.name == body.array), None)
    if not a:
        raise HTTPException(404, f"array {body.array} not in the form")
    pw = body.password
    if not pw:
        inst = ps.find_install(s, spec)
        if inst["installed"]:
            pw = next((c.get("password", "") for c in ps._creds(s, spec, inst["namespace"], body.config.release) if c.get("clusterName") == a.name), "")
    if not pw:
        raise HTTPException(422, "enter the array password")
    return {"output": _bad(lambda: onefs.create_path({"endpoint": a.endpoint, "port": a.port, "username": a.username, "password": pw,
                                                      "isi_path": a.isi_path}))}


@router.post("/install")
def install(name: str, body: CfgReq):
    s, spec = _ready(name)
    res = _bad(lambda: ps.full_check(s, spec, body.config, body.passwords, with_nodes=body.nodes))
    fails = _blocking(res["rows"])
    if fails:
        raise HTTPException(409, f"{len(fails)} pre-check(s) failed: " + "; ".join(f"{r['name']}: {r['actual']}" for r in fails[:4]))
    cfg, pws = body.config, dict(body.passwords)
    return _job(s, "powerscale-install", lambda ctx: ps.job_install(ctx, s, spec, cfg, pws))


@router.post("/uninstall")
def uninstall(name: str, body: UninstallReq):
    s, spec = _ready(name)
    inst = _read(lambda: ps.find_install(s, spec))
    if not inst["installed"]:
        raise HTTPException(409, "the driver is not installed")
    if body.confirm != inst["namespace"]:
        raise HTTPException(422, f"type the namespace ({inst['namespace']}) to confirm")
    if inst["method"] == "operator":
        cr = _read(lambda: ps._csm_state(s, spec, inst["namespace"], inst["release"])) or {}
        if any(m.get("name") == "replication" and m.get("enabled") for m in cr.get("modules", [])):
            rgs = _read(lambda: repl.groups(s, spec))["groups"]
            if any(g["cluster"] == name for g in rgs):
                raise HTTPException(409, "deleting this ContainerStorageModule would make the operator delete the replication CRDs "
                                         "and every replication group; remove the replicated volumes first")
    return _job(s, "powerscale-uninstall", lambda ctx: ps.job_uninstall(ctx, s, spec, body.remove_classes))


# ------------------------------------------------------------------ replication
@router.get("/replication")
def replication(name: str):
    s, spec = _ready(name)
    return _read(lambda: repl.groups(s, spec))


@router.post("/replication/check")
def replication_check(name: str, body: CfgReq, request: Request):
    _peer_allowed(request, body.config.replication.peer, write=False)
    s, spec = _ready(name)
    return _bad(lambda: repl.checks(s, spec, body.config))


@router.post("/replication/setup")
def replication_setup(name: str, body: CfgReq, request: Request):
    _peer_allowed(request, body.config.replication.peer, write=True)
    s, spec = _ready(name)
    res = _bad(lambda: repl.checks(s, spec, body.config))
    fails = _blocking(res["rows"])
    if fails:
        raise HTTPException(409, f"{len(fails)} pre-check(s) failed: " + "; ".join(f"{r['name']}: {r['actual']}" for r in fails[:4]))
    cfg = body.config
    return _job(s, "replication-setup", lambda ctx: repl.job_setup(ctx, s, spec, cfg))


@router.post("/replication/action")
def replication_action(name: str, body: ActionReq, request: Request):
    s, spec = _ready(name)
    _peer_allowed(request, spec.day2.powerscale.replication.peer, write=True)
    side, row = _bad(lambda: repl.check_action(s, spec, body.cluster, body.group, body.action))
    if repl.ACTIONS[body.action][2] and body.confirm != body.group:
        raise HTTPException(422, "type the replication group name to confirm")
    return _job(s, f"dr-{body.action.lower()}", lambda ctx: repl.job_action(ctx, s, spec, body.cluster, body.group, body.action))


# ------------------------------------------------------------------ bundle (installer-wide)
class ImportReq(BaseModel):
    component: str
    path: str


class FetchReq(BaseModel):
    component: str
    version: str


@bundles.get("")
def bundle(online: bool = True):
    out = dellbundle.summary()
    out["catalog"] = dellbundle.catalog(online)
    return out


@bundles.post("/upload")
async def upload(request: Request, component: str, filename: str = "", sha256: str = ""):
    if component not in dellbundle.COMPONENTS:
        raise HTTPException(422, "unknown component")
    tmp = dellbundle.incoming_file()
    size = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > dellbundle.MAX_UPLOAD:
                    raise HTTPException(413, "file too large")
                f.write(chunk)
        if not size:
            raise HTTPException(422, "empty upload")
        return await run_in_threadpool(lambda: _bad(lambda: dellbundle.add_file(component, tmp, filename=filename, expected_sha=sha256)))
    finally:
        if tmp.exists():
            tmp.unlink()


@bundles.post("/import")
def import_path(body: ImportReq):
    return _bad(lambda: dellbundle.import_path(body.component, body.path))


@bundles.post("/fetch")
def fetch(body: FetchReq):
    return _bad(lambda: dellbundle.fetch(body.component, body.version, lambda *_: None))


@bundles.delete("/{component}/{version}")
def delete(component: str, version: str):
    _bad(lambda: dellbundle.delete(component, version))
    return {"deleted": f"{component} {version}"}


@bundles.get("/{component}/{version}/download")
def download(component: str, version: str):
    p = _bad(lambda: dellbundle.path_of(component, version))
    name = p.name if p.suffix else f"{component}-{version}-linux-amd64"
    return FileResponse(p, filename=name, media_type="application/octet-stream")


@bundles.get("/images", response_class=PlainTextResponse)
def images(version: str = "", replication: str = ""):
    lines = []
    if version:
        lines += _bad(lambda: dellbundle.chart_images("csi-isilon", version))
    if replication:
        lines += _bad(lambda: dellbundle.chart_images("csm-replication", replication))
    return "\n".join(lines) + ("\n" if lines else "")
