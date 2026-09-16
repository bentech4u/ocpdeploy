"""Application catalog endpoints."""
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..jobs import start
from ..services import apps
from .clusters import _store

router = APIRouter(prefix="/api/clusters/{name}/apps", tags=["apps"])


def _ready(name: str):
    s = _store(name)
    spec = s.load()
    if not (s.install_dir / "auth" / "kubeconfig").exists():
        raise HTTPException(409, "cluster is not installed (no kubeconfig)")
    return s, spec


class InstallReq(BaseModel):
    inputs: Optional[Dict] = None


@router.get("")
def list_apps(name: str):
    s, spec = _ready(name)
    try:
        st = apps.status(s, spec)
    except Exception as ex:
        raise HTTPException(502, str(ex)[-300:])
    return {"catalog": apps.catalog(spec), **st}


@router.get("/{app}/credentials")
def app_credentials(name: str, app: str):
    s, spec = _ready(name)
    if app not in apps.CATALOG:
        raise HTTPException(404)
    return apps.credentials(s, spec, app)


@router.post("/{app}/install")
def app_install(name: str, app: str, body: InstallReq):
    s, spec = _ready(name)
    if app not in apps.CATALOG:
        raise HTTPException(404)
    try:
        return {"job_id": start(s, f"app-{app}", lambda ctx: apps.job_install(ctx, s, spec, app, body.inputs))}
    except RuntimeError as ex:
        raise HTTPException(409, str(ex))


@router.post("/{app}/remove")
def app_remove(name: str, app: str):
    s, spec = _ready(name)
    if app not in apps.CATALOG:
        raise HTTPException(404)
    try:
        return {"job_id": start(s, f"app-remove-{app}", lambda ctx: apps.job_remove(ctx, s, spec, app))}
    except RuntimeError as ex:
        raise HTTPException(409, str(ex))
