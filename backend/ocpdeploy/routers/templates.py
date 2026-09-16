"""Cluster templates: export, library, create-from."""
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..services import templates
from ..store import get_store
from .clusters import _store

router = APIRouter(prefix="/api", tags=["templates"])


@router.get("/clusters/{name}/export", response_class=PlainTextResponse)
def export_cluster(name: str, secrets: bool = False, hardware: bool = False):
    s = _store(name)
    text = templates.export_yaml(s, include_secrets=secrets, keep_hardware=hardware)
    return PlainTextResponse(text, media_type="application/yaml", headers={"Content-Disposition": f'attachment; filename="{name}-template.yaml"'})


@router.get("/templates")
def list_templates():
    return templates.list_templates()


class SaveReq(BaseModel):
    name: str
    yaml: Optional[str] = None      # template text, or
    from_cluster: Optional[str] = None   # export an existing cluster into the library
    secrets: bool = False


@router.post("/templates")
def save_template(body: SaveReq):
    try:
        text = body.yaml
        if body.from_cluster:
            text = templates.export_yaml(_store(body.from_cluster), include_secrets=body.secrets)
        if not text:
            raise ValueError("no template content")
        return templates.save_template(body.name, text)
    except ValueError as ex:
        raise HTTPException(422, str(ex))


@router.get("/templates/{tname}", response_class=PlainTextResponse)
def get_template(tname: str):
    try:
        return PlainTextResponse(templates.read_template(tname), media_type="application/yaml")
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)


@router.delete("/templates/{tname}", status_code=204)
def delete_template(tname: str):
    try:
        templates.delete_template(tname)
    except ValueError as ex:
        raise HTTPException(422, str(ex))


class CreateReq(BaseModel):
    name: str
    base_domain: Optional[str] = None
    first_ip: Optional[str] = None
    machine_cidr: Optional[str] = None
    gateway: Optional[str] = None
    template: Optional[str] = None       # library template name, or
    from_cluster: Optional[str] = None   # clone an existing cluster, or
    yaml: Optional[str] = None           # pasted / uploaded template


@router.post("/clusters/import", status_code=201)
def create_from_template(body: CreateReq):
    try:
        if body.template:
            raw = yaml.safe_load(templates.read_template(body.template))
        elif body.from_cluster:
            raw = yaml.safe_load(templates.export_yaml(_store(body.from_cluster), include_secrets=True))
        elif body.yaml:
            raw = yaml.safe_load(body.yaml)
        else:
            raise ValueError("give a template, a cluster to clone or YAML")
        if not isinstance(raw, dict):
            raise ValueError("template is not a mapping")
        store = templates.create_from(raw, body.name, base_domain=body.base_domain, first_ip=body.first_ip, machine_cidr=body.machine_cidr, gateway=body.gateway)
    except FileExistsError:
        raise HTTPException(409, "cluster already exists")
    except FileNotFoundError:
        raise HTTPException(404, "template not found")
    except (ValueError, yaml.YAMLError) as ex:
        raise HTTPException(422, str(ex)[:600])
    return store.public()
