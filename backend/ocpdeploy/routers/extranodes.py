"""Add Extra nodes (connected clusters): node ISO builds, monitor, CSR approval, labels."""
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..jobs import start, running_job
from ..services import extranodes as xn
from .clusters import _store

router = APIRouter(prefix="/api/clusters/{name}/extra-nodes", tags=["extra-nodes"])
isos = APIRouter(prefix="/api/extra-isos", tags=["extra-nodes"])


def _connected(name: str):
    s = _store(name)
    spec = s.load()
    if not spec.imported:
        raise HTTPException(409, "Add Extra nodes is for connected clusters")
    return s, spec


class BuildReq(BaseModel):
    hosts: List[Dict] = []
    nodes_config: Optional[str] = None


def _hosts(body: "BuildReq") -> List[Dict]:
    try:
        hosts = xn.parse_nodes_config(body.nodes_config) if (body.nodes_config or "").strip() else [xn.host_from_form(h) for h in body.hosts]
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    if not hosts:
        raise HTTPException(422, "add at least one host")
    names = [h.get("hostname") for h in hosts]
    if len(set(names)) != len(names):
        raise HTTPException(422, "hostnames must be unique")
    return hosts


class HostsReq(BaseModel):
    hostnames: List[str]


class MonitorReq(BaseModel):
    ips: List[str]


class LabelReq(BaseModel):
    node: str
    role: str
    hostnames: List[str]


@router.get("")
def overview(name: str):
    s, spec = _connected(name)
    return {**xn.support(s, spec), "builds": xn.list_builds(name), "running_job": running_job(s)}


@router.post("/build")
def build(name: str, body: BuildReq):
    s, spec = _connected(name)
    sup = xn.support(s, spec)
    if not sup["supported"]:
        raise HTTPException(409, sup["reason"] or "read-only connection")
    hosts = _hosts(body)
    rows = xn.precheck(s, spec, hosts)
    fails = [r for r in rows if r["status"] == "fail"]
    if fails:
        raise HTTPException(422, "pre-checks failed: " + "; ".join(f"{r['host']}: {r['name']} ({r['actual']})" for r in fails))
    try:
        return {"job_id": start(s, "node-image", lambda ctx: xn.job_build(ctx, s, spec, hosts))}
    except RuntimeError as ex:
        raise HTTPException(409, str(ex))


@router.post("/check")
def check(name: str, body: BuildReq):
    """Pre-checks for the hosts (read-only; allowed on read-only connections)."""
    s, spec = _connected(name)
    hosts = _hosts(body)
    try:
        return xn.precheck(s, spec, hosts)
    except Exception as ex:
        raise HTTPException(502, str(ex)[-300:])


@router.get("/hints")
def hints(name: str, refresh: bool = False):
    s, spec = _connected(name)
    if spec.read_only:
        raise HTTPException(403, "reading node settings starts a debug pod; not available on read-only connections")
    try:
        return xn.hints(s, spec, refresh)
    except Exception as ex:
        raise HTTPException(502, f"could not read an existing node: {str(ex)[-200:]}")


@router.post("/monitor")
def monitor(name: str, body: MonitorReq):
    s, spec = _connected(name)
    if not body.ips:
        raise HTTPException(422, "no IP addresses")
    try:
        return {"job_id": start(s, "node-monitor", lambda ctx: xn.job_monitor(ctx, s, spec, body.ips))}
    except RuntimeError as ex:
        raise HTTPException(409, str(ex))


@router.post("/csrs")
def csrs(name: str, body: HostsReq):
    """POST so the host list travels in the body; read-only by nature (allowed for GET-only sessions below)."""
    s, spec = _connected(name)
    try:
        return {**xn.pending_csrs(s, spec, body.hostnames), "nodes": xn.node_status(s, spec, body.hostnames)}
    except Exception as ex:
        raise HTTPException(502, str(ex)[-300:])


@router.post("/csrs/{csr}/approve")
def approve(name: str, csr: str, body: HostsReq):
    s, spec = _connected(name)
    try:
        return {"output": xn.approve_csr(s, spec, csr, body.hostnames)}
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    except PermissionError as ex:
        raise HTTPException(403, str(ex))


@router.post("/label")
def label(name: str, body: LabelReq):
    s, spec = _connected(name)
    try:
        return {"output": xn.label_node(s, spec, body.node, body.role, body.hostnames)}
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    except PermissionError as ex:
        raise HTTPException(403, str(ex))


# ---- ISOs outlive the connection: list, download and delete them without one
@isos.get("")
def list_isos():
    return xn.list_builds()


@isos.get("/{build_id}/download")
def download(build_id: str):
    try:
        p = xn.iso_path(build_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404)
    return FileResponse(p, filename=f"{build_id}.iso", media_type="application/octet-stream")


@isos.delete("/{build_id}")
def delete(build_id: str):
    try:
        if not xn.delete_build(build_id):
            raise HTTPException(404)
    except ValueError:
        raise HTTPException(404)
    return {"deleted": build_id}
