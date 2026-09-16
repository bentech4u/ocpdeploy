"""Day-2 endpoints: health, upgrade, scaling, identity, certificates, storage, operators, backup, power."""
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..jobs import start
from ..services import health, upgrade, scale, identity, certs, storage, operators, backup, power, clusterops as ops
from .clusters import _store

router = APIRouter(prefix="/api/clusters/{name}", tags=["day2"])


def _ready(name: str):
    s = _store(name)
    spec = s.load()
    if not (s.install_dir / "auth" / "kubeconfig").exists():
        raise HTTPException(409, "cluster is not installed (no kubeconfig)")
    return s, spec


def _job(s, kind, fn):
    try:
        return {"job_id": start(s, kind, fn)}
    except RuntimeError as ex:
        raise HTTPException(409, str(ex))


def _read(fn):
    try:
        return fn()
    except Exception as ex:
        raise HTTPException(502, str(ex)[-300:])


# ---- health
@router.get("/health")
def get_health(name: str):
    s, spec = _ready(name)
    return health.snapshot(s, spec)


@router.post("/health/approve-csrs")
def approve_csrs(name: str):
    s, spec = _ready(name)
    return {"approved": _read(lambda: ops.approve_csrs(s, spec, lambda *_: None))}


# ---- upgrade
class ChannelReq(BaseModel):
    channel: str


class UpgradeReq(BaseModel):
    version: str
    force: bool = False


@router.get("/upgrade")
def get_upgrade(name: str):
    s, spec = _ready(name)
    return _read(lambda: upgrade.info(s, spec))


@router.post("/upgrade/channel")
def set_channel(name: str, body: ChannelReq):
    s, spec = _ready(name)
    return {"output": _read(lambda: upgrade.set_channel(s, spec, body.channel))}


@router.post("/upgrade/ack")
def ack(name: str):
    s, spec = _ready(name)
    return {"acked": _read(lambda: upgrade.ack_gates(s, spec))}


@router.post("/upgrade/start")
def start_upgrade(name: str, body: UpgradeReq):
    s, spec = _ready(name)
    return _job(s, "upgrade", lambda ctx: upgrade.job_upgrade(ctx, s, spec, body.version, body.force))


# ---- scaling
class ScaleUpReq(BaseModel):
    machineset: str
    ips: List[str] = []


class ScaleDownReq(BaseModel):
    machines: List[str]


class NameReq(BaseModel):
    name: str


@router.get("/scale")
def get_scale(name: str):
    s, spec = _ready(name)
    return _read(lambda: scale.overview(s, spec))


@router.post("/scale/up")
def scale_up(name: str, body: ScaleUpReq):
    s, spec = _ready(name)
    return _job(s, "scale-up", lambda ctx: scale.job_scale_up(ctx, s, spec, body.machineset, body.ips))


@router.post("/scale/down")
def scale_down(name: str, body: ScaleDownReq):
    s, spec = _ready(name)
    if not body.machines:
        raise HTTPException(422, "no machines selected")
    return _job(s, "scale-down", lambda ctx: scale.job_scale_down(ctx, s, spec, body.machines))


@router.post("/scale/delete-machineset")
def delete_machineset(name: str, body: NameReq):
    s, spec = _ready(name)
    return _job(s, "delete-machineset", lambda ctx: scale.job_delete_machineset(ctx, s, spec, body.name))


# ---- identity
class Confirm(BaseModel):
    confirm: str


@router.get("/identity")
def get_identity(name: str):
    s, spec = _ready(name)
    return _read(lambda: identity.status(s, spec))


@router.get("/identity/preview")
def identity_preview(name: str):
    s, spec = _ready(name)
    try:
        return identity.preview(s, spec)
    except Exception as ex:
        raise HTTPException(422, str(ex))


@router.post("/identity/apply")
def identity_apply(name: str):
    s, spec = _ready(name)
    try:
        identity.preview(s, spec)   # validate inputs first
    except Exception as ex:
        raise HTTPException(422, str(ex))
    return _job(s, "identity", lambda ctx: identity.job_apply(ctx, s, spec))


@router.post("/identity/disable-kubeadmin")
def disable_kubeadmin(name: str, body: Confirm):
    if body.confirm != name:
        raise HTTPException(422, "type the cluster name to confirm")
    s, spec = _ready(name)
    st = _read(lambda: identity.status(s, spec))
    if not st["identity_providers"]:
        raise HTTPException(422, "no identity provider configured; you would lock yourself out")
    if not [a for a in st["cluster_admins"] if a not in ("kube:admin", "system:admin")] and not st["cluster_admin_groups"]:
        raise HTTPException(422, "no user other than kubeadmin has cluster-admin; grant it first")
    return _job(s, "disable-kubeadmin", lambda ctx: identity.job_disable_kubeadmin(ctx, s, spec))


# ---- certificates
@router.get("/certs")
def get_certs(name: str):
    s, spec = _ready(name)
    return _read(lambda: certs.status(s, spec))


@router.post("/certs/custom")
def certs_custom(name: str):
    s, spec = _ready(name)
    return _job(s, "certs-custom", lambda ctx: certs.job_apply_custom(ctx, s, spec))


@router.post("/certs/acme")
def certs_acme(name: str):
    s, spec = _ready(name)
    try:
        certs.acme_docs(spec)
    except Exception as ex:
        raise HTTPException(422, str(ex))
    return _job(s, "certs-acme", lambda ctx: certs.job_acme(ctx, s, spec))


# ---- storage
@router.get("/storage")
def get_storage(name: str):
    s, spec = _ready(name)
    return _read(lambda: storage.status(s, spec))


@router.post("/storage/default-sc")
def default_sc(name: str, body: NameReq):
    s, spec = _ready(name)
    _read(lambda: storage.set_default_sc(s, spec, body.name))
    return {"default": body.name}


@router.post("/storage/nfs")
def storage_nfs(name: str):
    s, spec = _ready(name)
    try:
        storage.nfs_docs(spec)
    except Exception as ex:
        raise HTTPException(422, str(ex))
    return _job(s, "storage-nfs", lambda ctx: storage.job_nfs(ctx, s, spec))


@router.post("/storage/lvms")
def storage_lvms(name: str):
    s, spec = _ready(name)
    return _job(s, "storage-lvms", lambda ctx: storage.job_lvms(ctx, s, spec))


@router.post("/storage/odf")
def storage_odf(name: str):
    s, spec = _ready(name)
    return _job(s, "storage-odf", lambda ctx: storage.job_odf(ctx, s, spec))


@router.post("/storage/registry")
def storage_registry(name: str):
    s, spec = _ready(name)
    return _job(s, "registry-storage", lambda ctx: storage.job_registry(ctx, s, spec))


# ---- operators
class PackagesReq(BaseModel):
    packages: List[str]


class PackageReq(BaseModel):
    package: str


@router.get("/operators")
def get_operators(name: str):
    s, spec = _ready(name)
    return _read(lambda: operators.status(s, spec))


@router.post("/operators/install")
def operators_install(name: str, body: PackagesReq):
    s, spec = _ready(name)
    if not body.packages:
        raise HTTPException(422, "no packages selected")
    return _job(s, "operators-install", lambda ctx: operators.job_install(ctx, s, spec, body.packages))


@router.post("/operators/remove")
def operators_remove(name: str, body: PackageReq):
    s, spec = _ready(name)
    return _job(s, "operator-remove", lambda ctx: operators.job_remove(ctx, s, spec, body.package))


@router.post("/operators/mirror-resources")
def operators_mirror(name: str):
    s, spec = _ready(name)
    return _job(s, "mirror-resources", lambda ctx: operators.job_apply_mirror_resources(ctx, s, spec))


# ---- backup
class ScheduleReq(BaseModel):
    schedule: str = ""
    keep: int = 7


@router.get("/backup")
def get_backup(name: str):
    s = _store(name)
    spec = s.load()
    return {"backups": backup.list_backups(s), "timer": backup.timer_status(s), "schedule": spec.day2.backup.schedule, "keep": spec.day2.backup.keep}


@router.post("/backup/run")
def backup_run(name: str):
    s, spec = _ready(name)
    return _job(s, "etcd-backup", lambda ctx: backup.job_backup(ctx, s, spec))


@router.post("/backup/schedule")
def backup_schedule(name: str, body: ScheduleReq):
    s = _store(name)
    try:
        return backup.set_schedule(s, body.schedule.strip(), body.keep)
    except RuntimeError as ex:
        raise HTTPException(422, str(ex))


@router.get("/backup/download/{file}")
def backup_download(name: str, file: str):
    s = _store(name)
    p = backup.backups_dir(s) / file
    if "/" in file or not p.exists() or not file.startswith("etcd-"):
        raise HTTPException(404)
    return FileResponse(p, filename=f"{name}-{file}", media_type="application/gzip")


# ---- power
class ShutdownReq(BaseModel):
    backup: bool = True
    confirm: str = ""


@router.get("/power")
def get_power(name: str):
    s, spec = _ready(name)
    return _read(lambda: power.status(s, spec))


@router.post("/power/shutdown")
def power_shutdown(name: str, body: ShutdownReq):
    if body.confirm != name:
        raise HTTPException(422, "type the cluster name to confirm")
    s, spec = _ready(name)
    return _job(s, "shutdown", lambda ctx: power.job_shutdown(ctx, s, spec, body.backup))


@router.post("/power/startup")
def power_startup(name: str):
    s, spec = _ready(name)
    return _job(s, "startup", lambda ctx: power.job_startup(ctx, s, spec))
