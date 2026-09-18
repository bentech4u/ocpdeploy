"""Connect to existing clusters (kubeconfig / username+password / token). RAM only."""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import imported

router = APIRouter(prefix="/api/imported", tags=["imported"])


class ProbeReq(BaseModel):
    method: str
    server: Optional[str] = None
    kubeconfig: Optional[str] = None
    context: Optional[str] = None


class ConnectReq(BaseModel):
    method: str
    server: Optional[str] = None
    kubeconfig: Optional[str] = None
    context: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    api_sha256: str
    oauth_sha256: Optional[str] = None
    name: Optional[str] = None
    read_only: bool = False


@router.post("/probe")
def probe(body: ProbeReq):
    """Certificates the user must accept before any credential is sent."""
    try:
        out = {}
        server = body.server or ""
        if body.method == "kubeconfig":
            k = imported.parse_kubeconfig(body.kubeconfig or "", body.context or None)
            server = k["server"]
            out.update(contexts=k["contexts"], context=k["context"])
        host, port = imported._host_port(server)
        out["server"] = server
        out["api"] = imported.cert_bundle(host, port)
        if body.method == "password":
            ep = imported.oauth_endpoint(server)
            oh, op = imported._host_port(ep)
            out["oauth"] = imported.cert_bundle(oh, op)
        return out
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    except Exception as ex:
        raise HTTPException(502, f"could not reach the cluster: {str(ex)[-200:]}")


@router.post("/connect")
def connect(body: ConnectReq, request: Request):
    try:
        return imported.connect(request.state.session_id, body.method, server=body.server or "", api_sha256=body.api_sha256,
                                oauth_sha256=body.oauth_sha256 or "", kubeconfig=body.kubeconfig or "", context=body.context or "",
                                username=body.username or "", password=body.password or "", token=body.token or "",
                                name=body.name or "", read_only=body.read_only)
    except PermissionError as ex:
        raise HTTPException(401, str(ex))
    except (ValueError, FileExistsError) as ex:
        raise HTTPException(422, str(ex))
    except Exception as ex:
        raise HTTPException(502, str(ex)[-300:])
    finally:
        body.password = None
        body.token = None


@router.delete("/{name}")
def disconnect(name: str, request: Request):
    c = imported.info(name)
    if not c or c["owner"] != request.state.session_id:
        raise HTTPException(404)
    try:
        imported.disconnect(name)
    except RuntimeError as ex:
        raise HTTPException(409, str(ex))
    return {"disconnected": name}
