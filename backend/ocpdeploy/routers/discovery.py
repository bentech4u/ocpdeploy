"""Endpoints that do not belong to one cluster: versions, tools, vCenter probing, host info."""
import socket
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..models import VCenterSpec
from ..services import versions, tools, vcenter
from ..settings import DEFAULT_SSH_PUBKEY, ROOT, BIN_DIR, CLUSTERS_DIR
from .. import __version__

router = APIRouter(prefix="/api", tags=["discovery"])


@router.get("/system")
def system():
    import dns.resolver
    try:
        ns = list(dns.resolver.Resolver().nameservers)
    except Exception:
        ns = []
    return {
        "app_version": __version__,
        "hostname": socket.getfqdn(),
        "root": str(ROOT), "clusters_dir": str(CLUSTERS_DIR), "bin_dir": str(BIN_DIR),
        "ssh_public_key": DEFAULT_SSH_PUBKEY.read_text().strip() if DEFAULT_SSH_PUBKEY.exists() else "",
        "dns_servers": ns,
        "installed_tool_versions": tools.installed_versions(),
    }


@router.get("/versions")
def list_versions(refresh: bool = False):
    try:
        return versions.discover(force=refresh)
    except Exception as ex:
        raise HTTPException(502, f"could not query the OpenShift update graph: {ex}")


@router.get("/tools/{version}")
def tool_status(version: str):
    st = tools.status(version)
    st["on_mirror"] = versions.mirror_has(version)
    return st


class VCHost(BaseModel):
    host: str


@router.post("/vcenter/cert")
def vc_cert(body: VCHost):
    try:
        return vcenter.fetch_cert(body.host)
    except Exception as ex:
        raise HTTPException(502, f"could not fetch certificate from {body.host}: {ex}")


class VCCreds(BaseModel):
    host: str
    username: str
    password: str
    cluster: Optional[str] = None   # optional cluster name to pull stored password when password == MASK


def _creds(body: VCCreds) -> VCenterSpec:
    from ..secrets import MASK
    pw = body.password
    if pw == MASK and body.cluster:
        from ..store import get_store
        try:
            pw = get_store(body.cluster).load().vcenter.password
        except KeyError:
            raise HTTPException(404, "cluster not found")
    return VCenterSpec(host=body.host, username=body.username, password=pw)


@router.post("/vcenter/test")
def vc_test(body: VCCreds):
    try:
        return vcenter.about(_creds(body))
    except Exception as ex:
        raise HTTPException(502, f"vCenter login failed: {ex}")


@router.post("/vcenter/inventory")
def vc_inventory(body: VCCreds):
    try:
        return vcenter.inventory(_creds(body))
    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(502, f"inventory query failed: {ex}")
