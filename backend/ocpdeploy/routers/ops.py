"""Generate / deploy / destroy / jobs / day-2 / status endpoints."""
import asyncio
import json
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .. import jobs
from ..services import render, deploy, day2, kube, tools
from .clusters import _store

router = APIRouter(prefix="/api/clusters/{name}", tags=["ops"])


def _start(s, kind, fn, meta=None):
    try:
        return {"job_id": jobs.start(s, kind, fn, meta)}
    except RuntimeError as ex:
        raise HTTPException(409, str(ex))


@router.get("/config/preview")
def preview(name: str):
    spec = _store(name).load()
    try:
        ic = render.masked(render.install_config(spec))
        out = {"install-config.yaml": render.to_yaml(ic)}
        if spec.install_method == "agent":
            out["agent-config.yaml"] = render.to_yaml(render.agent_config(spec))
        return out
    except Exception as ex:
        raise HTTPException(422, str(ex))


@router.post("/tools/download")
def download_tools(name: str):
    s = _store(name)
    spec = s.load()
    if not spec.ocp_version:
        raise HTTPException(422, "no version selected")
    return _start(s, "download-tools", lambda ctx: tools.ensure(spec.ocp_version, ctx.log))


@router.post("/generate")
def generate(name: str):
    s = _store(name)
    spec = s.load()
    return _start(s, "generate", lambda ctx: deploy.job_generate(ctx, s, spec))


@router.post("/deploy")
def do_deploy(name: str):
    s = _store(name)
    spec = s.load()
    if spec.status == "installed":
        raise HTTPException(409, "cluster already installed; destroy it first")
    return _start(s, "deploy", lambda ctx: deploy.job_deploy(ctx, s, spec))


@router.post("/resume")
def do_resume(name: str):
    s = _store(name)
    spec = s.load()
    return _start(s, "resume", lambda ctx: deploy.job_resume(ctx, s, spec))


class Confirm(BaseModel):
    confirm: str


@router.post("/destroy")
def do_destroy(name: str, body: Confirm):
    if body.confirm != name:
        raise HTTPException(422, "type the cluster name to confirm")
    s = _store(name)
    spec = s.load()
    return _start(s, "destroy", lambda ctx: deploy.job_destroy(ctx, s, spec))


# ---- day 2
@router.post("/day2/remove-bootstrap")
def remove_bootstrap(name: str):
    s = _store(name)
    spec = s.load()
    return _start(s, "remove-bootstrap", lambda ctx: day2.job_remove_bootstrap(ctx, s, spec))


class InfraReq(BaseModel):
    nodes: Optional[List[str]] = None


@router.post("/day2/add-infra")
def add_infra(name: str, body: InfraReq):
    s = _store(name)
    spec = s.load()
    return _start(s, "add-infra", lambda ctx: day2.job_add_infra(ctx, s, spec, body.nodes))


class MoveReq(BaseModel):
    monitoring: bool = True
    registry: bool = True


@router.post("/day2/move-ingress")
def move_ingress(name: str, body: MoveReq):
    s = _store(name)
    spec = s.load()
    return _start(s, "move-ingress", lambda ctx: day2.job_move_ingress(ctx, s, spec, body.monitoring, body.registry))


# ---- status / credentials
@router.get("/status")
def status(name: str):
    s = _store(name)
    spec = s.load()
    out = {"status": spec.status, "running_job": jobs.running_job(s), "infra_id": s.kv_get("infra_id")}
    auth = s.install_dir / "auth"
    out["has_kubeconfig"] = (auth / "kubeconfig").exists()
    out["console_url"] = f"https://console-openshift-console.apps.{spec.domain}" if out["has_kubeconfig"] else None
    out["api_url"] = f"https://api.{spec.domain}:6443"
    if out["has_kubeconfig"] and spec.ocp_version and tools.tool_path(spec.ocp_version, "oc").exists():
        out["cluster"] = kube.cluster_status(s, spec)
    return out


@router.get("/credentials/kubeadmin-password", response_class=PlainTextResponse)
def kubeadmin(name: str):
    p = _store(name).install_dir / "auth" / "kubeadmin-password"
    if not p.exists():
        raise HTTPException(404, "not available yet")
    return p.read_text()


@router.get("/credentials/kubeconfig")
def kubeconfig(name: str):
    p = _store(name).install_dir / "auth" / "kubeconfig"
    if not p.exists():
        raise HTTPException(404, "not available yet")
    return FileResponse(p, filename=f"kubeconfig-{name}", media_type="text/plain")


@router.get("/files")
def files(name: str):
    s = _store(name)
    out = []
    for p in sorted(s.dir.rglob("*")):
        if p.is_file() and ".bak" not in p.name and "state.sqlite" not in p.name and p.suffix != ".tmp":
            rel = p.relative_to(s.dir)
            if str(rel).startswith("install/") and rel.name not in ("install-config.yaml", "agent-config.yaml", "metadata.json", ".openshift_install.log"):
                if not str(rel).startswith("install/auth"):
                    continue
            out.append({"path": str(rel), "size": p.stat().st_size})
    return out


# ---- jobs
@router.get("/jobs")
def list_jobs(name: str):
    return jobs.list_jobs(_store(name))


@router.get("/jobs/{job_id}")
def get_job(name: str, job_id: int):
    j = jobs.get_job(_store(name), job_id)
    if not j:
        raise HTTPException(404)
    return j


@router.get("/jobs/{job_id}/logs")
def job_logs(name: str, job_id: int, after: int = 0):
    return jobs.get_logs(_store(name), job_id, after)


@router.post("/jobs/{job_id}/cancel")
def cancel(name: str, job_id: int):
    return {"cancelled": jobs.cancel(_store(name), job_id)}


@router.get("/jobs/{job_id}/stream")
async def stream(name: str, job_id: int, after: int = 0):
    s = _store(name)

    async def gen():
        for l in jobs.get_logs(s, job_id, after):
            yield {"event": "log", "data": json.dumps(l)}
        q = jobs.subscribe(job_id)
        if q is None:
            j = jobs.get_job(s, job_id)
            yield {"event": "done", "data": json.dumps({"status": j["status"] if j else "unknown"})}
            return
        try:
            while True:
                try:
                    item = q.get_nowait()
                except Exception:
                    await asyncio.sleep(0.5)
                    yield {"event": "ping", "data": ""}
                    continue
                if item.get("done"):
                    yield {"event": "done", "data": json.dumps(item)}
                    return
                yield {"event": "log", "data": json.dumps(item)}
        finally:
            jobs.unsubscribe(job_id, q)

    return EventSourceResponse(gen())
