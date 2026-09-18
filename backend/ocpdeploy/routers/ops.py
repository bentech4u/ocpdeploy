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
from ..services import render, deploy, day2, kube, tools, mirror
from .clusters import _store
from ..store import get_store

router = APIRouter(prefix="/api/clusters/{name}", tags=["ops"])
iso_router = APIRouter(prefix="/api/iso", tags=["iso"])


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
        if spec.mirror.enabled:
            out["imageset-config.yaml"] = render.to_yaml(render.imageset_config(spec))
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


@router.post("/day2/add-nodes")
def add_nodes(name: str, body: InfraReq):
    """Create infra nodes and node-pool members (all of them, or the names given)."""
    s = _store(name)
    spec = s.load()
    if not day2.add_targets(spec, body.nodes):
        raise HTTPException(422, "no infra or pool nodes to add; define them in the Nodes step")
    return _start(s, "add-nodes", lambda ctx: day2.job_add_nodes(ctx, s, spec, body.nodes))


# ---- mirror (disconnected installs)
class RegistryReq(BaseModel):
    registry: Optional[str] = None


@router.post("/mirror/cert")
def mirror_cert(name: str, body: RegistryReq):
    spec = _store(name).load()
    reg = body.registry or spec.mirror.registry
    if not reg:
        raise HTTPException(422, "no registry given")
    try:
        return mirror.fetch_cert(reg)
    except Exception as ex:
        raise HTTPException(502, f"could not fetch certificate from {reg}: {ex}")


@router.get("/mirror/imageset")
def mirror_imageset(name: str):
    spec = _store(name).load()
    try:
        return {"imageset-config.yaml": render.to_yaml(render.imageset_config(spec))}
    except Exception as ex:
        raise HTTPException(422, str(ex))


@router.post("/mirror/run")
def mirror_run(name: str):
    s = _store(name)
    spec = s.load()
    if not spec.mirror.enabled:
        raise HTTPException(422, "enable the mirror registry first")
    return _start(s, "mirror", lambda ctx: mirror.job_mirror(ctx, s, spec))


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


@router.get("/credentials/ingress-ca")
def ingress_ca(name: str):
    """The cluster's ingress (router) CA in PEM; import it into a browser/OS trust store
    to open the console and routes without warnings."""
    import base64
    s = _store(name)
    spec = s.load()
    try:
        out = kube.oc(s, spec, ["get", "secret", "router-ca", "-n", "openshift-ingress-operator",
                                "-o", "jsonpath={.data.tls\\.crt}"])
    except Exception as ex:
        raise HTTPException(502, f"could not read router-ca: {ex}")
    pem = base64.b64decode(out).decode()
    (s.dir / "ingress-ca.crt").write_text(pem)
    return PlainTextResponse(pem, headers={"Content-Disposition": f'attachment; filename="{name}-ingress-ca.crt"'})


@router.get("/iso/{file}")
def iso(name: str, file: str):
    """Agent / node ISO download for Redfish virtual media and manual booting."""
    s = _store(name)
    if "/" in file or not (file.startswith("agent.") or file.startswith("node.")) or not file.endswith(".iso"):
        raise HTTPException(404)
    for d in (s.install_dir, s.dir / "add-nodes"):
        p = d / file
        if p.exists():
            return FileResponse(p, filename=file, media_type="application/octet-stream")
    raise HTTPException(404, "ISO not built yet")


@iso_router.get("/{name}/{token}/{file}")
def iso_tokenized(name: str, token: str, file: str):
    """ISO for Redfish virtual media (BMCs cannot log in); the per-cluster token in the
    path is the credential."""
    import hmac
    try:
        s = get_store(name)
    except KeyError:
        raise HTTPException(404)
    want = s.kv_get("iso_token")
    if not want or not hmac.compare_digest(str(want), token):
        raise HTTPException(404)
    return iso(name, file)


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
