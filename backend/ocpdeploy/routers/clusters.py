from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..models import ClusterSpec
from ..store import ClusterStore, list_clusters, get_store
from ..services.macs import mac_for
from ..settings import DEFAULT_SSH_PUBKEY

router = APIRouter(prefix="/api/clusters", tags=["clusters"])


def _store(name: str) -> ClusterStore:
    try:
        return get_store(name)
    except KeyError:
        raise HTTPException(404, f"cluster {name} not found")


class NewCluster(BaseModel):
    name: str
    base_domain: str = ""
    install_method: str = "ipi"


@router.get("")
def list_all():
    return list_clusters()


@router.post("", status_code=201)
def create(body: NewCluster):
    spec = ClusterSpec(name=body.name, base_domain=body.base_domain, install_method=body.install_method)
    if DEFAULT_SSH_PUBKEY.exists():
        spec.ssh_public_key = DEFAULT_SSH_PUBKEY.read_text().strip()
    try:
        import dns.resolver
        r = dns.resolver.Resolver()
        spec.network.dns_servers = list(r.nameservers)[:2]
    except Exception:
        pass
    s = ClusterStore(body.name)
    try:
        s.create(spec)
    except FileExistsError:
        raise HTTPException(409, "cluster already exists")
    return s.public()


@router.get("/{name}")
def get(name: str):
    return _store(name).public()


@router.put("/{name}")
def update(name: str, body: Dict[str, Any]):
    s = _store(name)
    try:
        s.update(body)
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    return s.public()


@router.delete("/{name}", status_code=204)
def delete(name: str):
    import shutil
    s = _store(name)
    from ..jobs import running_job
    if running_job(s):
        raise HTTPException(409, "a job is running for this cluster")
    shutil.rmtree(s.dir)


class NodePlan(BaseModel):
    masters: int = 3
    workers: int = 3
    infra: int = 0
    bootstrap: bool = True
    first_ip: str = ""
    master_size: Dict[str, int] = {"cpus": 4, "memory_mb": 16384, "disk_gb": 120}
    worker_size: Dict[str, int] = {"cpus": 4, "memory_mb": 16384, "disk_gb": 120}
    infra_size: Dict[str, int] = {"cpus": 4, "memory_mb": 16384, "disk_gb": 120}
    name_style: str = "master01"   # or master-0


@router.post("/{name}/nodes/plan")
def plan_nodes(name: str, body: NodePlan):
    """Generate a node table from counts + a starting IP; the UI lets the user edit it afterwards."""
    s = _store(name)
    raw = s.public()
    import ipaddress
    ip = ipaddress.ip_address(body.first_ip) if body.first_ip else None
    nodes: List[Dict] = []

    def nm(prefix, i):
        return f"{prefix}{i+1:02d}" if body.name_style == "master01" else f"{prefix}-{i}"

    def add(role, prefix, count, size):
        nonlocal ip
        for i in range(count):
            nodes.append({"name": nm(prefix, i) if role != "bootstrap" else "bootstrap", "role": role,
                          "ip": str(ip) if ip else "", "mac": mac_for(name, nm(prefix, i) if role != "bootstrap" else "bootstrap"), **size})
            if ip:
                ip += 1

    if body.bootstrap and raw.get("install_method") == "ipi":
        add("bootstrap", "bootstrap", 1, body.master_size)
    add("master", "master", body.masters, body.master_size)
    add("infra", "infra", body.infra, body.infra_size)
    add("worker", "worker", body.workers, body.worker_size)
    return nodes


@router.post("/{name}/nodes/macs")
def fill_macs(name: str):
    s = _store(name)

    def _p(raw):
        for n in raw["nodes"]:
            if not n.get("mac"):
                n["mac"] = mac_for(name, n["name"])
    s.patch(_p)
    return s.public()["nodes"]
