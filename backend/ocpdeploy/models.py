"""Cluster specification. This is the single source of truth a cluster folder
stores; every generated artefact (install-config, agent-config, haproxy.cfg,
DNS checklist, machinesets) is rendered from it."""
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator
import ipaddress
import re

Role = Literal["bootstrap", "master", "worker", "infra"]
InstallMethod = Literal["ipi", "agent"]
LBMode = Literal["haproxy", "external"]
HAProxyLayout = Literal["split", "ha"]


class NodeSpec(BaseModel):
    name: str
    role: Role
    ip: str
    mac: Optional[str] = None
    cpus: int = 4
    memory_mb: int = 16384
    disk_gb: int = 120

    @field_validator("ip")
    @classmethod
    def _ip(cls, v):
        ipaddress.ip_address(v)
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", v):
            raise ValueError("node name must be a lowercase DNS label")
        return v


class VCenterSpec(BaseModel):
    host: str = ""
    username: str = ""
    password: str = ""            # secret
    datacenter: str = ""
    cluster: str = ""
    datastore: str = ""
    network: str = ""
    resource_pool: str = ""
    folder: str = ""
    cert_thumbprint: str = ""     # SHA1 fingerprint accepted by the user
    cert_pem: str = ""            # PEM captured from the server, fed as trust bundle
    guest_id: str = "rhel9_64Guest"


class HAProxyVM(BaseModel):
    role: Literal["api", "apps", "both"] = "both"
    host: str = ""                # IP or FQDN used for SSH
    ssh_user: str = "root"
    ssh_password: str = ""        # secret
    ssh_port: int = 22
    ip: str = ""                  # address DNS points at (defaults to host if it is an IP)


class HAProxyHA(BaseModel):
    api_vip: str = ""
    apps_vip: str = ""
    interface: str = ""
    router_id: int = 51
    auth_pass: str = "ocpvrrp"


class ExternalLBEntry(BaseModel):
    fqdn: str = ""
    ip: str = ""


class LBSpec(BaseModel):
    mode: LBMode = "haproxy"
    layout: HAProxyLayout = "split"
    vms: List[HAProxyVM] = Field(default_factory=list)
    ha: HAProxyHA = Field(default_factory=HAProxyHA)
    external_api: ExternalLBEntry = Field(default_factory=ExternalLBEntry)
    external_apps: ExternalLBEntry = Field(default_factory=ExternalLBEntry)
    stats_port: int = 9000
    bootstrap_removed: bool = False
    ingress_on: Literal["all", "workers", "infra"] = "all"   # which pool serves *.apps


class NetworkSpec(BaseModel):
    machine_cidr: str = ""
    gateway: str = ""
    dns_servers: List[str] = Field(default_factory=list)
    ntp_servers: List[str] = Field(default_factory=list)
    cluster_network: str = "10.128.0.0/14"
    host_prefix: int = 23
    service_network: str = "172.30.0.0/16"
    interface_name: str = "ens192"   # NIC name inside RHCOS on vmxnet3

    @field_validator("machine_cidr")
    @classmethod
    def _cidr(cls, v):
        if v:
            ipaddress.ip_network(v, strict=False)
        return v


class ClusterSpec(BaseModel):
    name: str
    base_domain: str = ""
    ocp_version: str = ""
    install_method: InstallMethod = "ipi"
    vcenter: VCenterSpec = Field(default_factory=VCenterSpec)
    lb: LBSpec = Field(default_factory=LBSpec)
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    nodes: List[NodeSpec] = Field(default_factory=list)
    pull_secret: str = ""         # secret
    ssh_public_key: str = ""
    fips: bool = False
    status: str = "new"           # new | configured | deploying | installed | failed | destroyed

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?", v):
            raise ValueError("cluster name must be a lowercase DNS label")
        return v

    # ---- derived helpers -------------------------------------------------
    @property
    def domain(self) -> str:
        return f"{self.name}.{self.base_domain}"

    @property
    def api_ip(self) -> str:
        return self._lb_ip("api")

    @property
    def apps_ip(self) -> str:
        return self._lb_ip("apps")

    def _lb_ip(self, which: str) -> str:
        lb = self.lb
        if lb.mode == "external":
            return (lb.external_api if which == "api" else lb.external_apps).ip
        if lb.layout == "ha":
            return lb.ha.api_vip if which == "api" else lb.ha.apps_vip
        for vm in lb.vms:
            if vm.role in (which, "both"):
                return vm.ip or vm.host
        return ""

    def nodes_by_role(self, *roles: str) -> List[NodeSpec]:
        return [n for n in self.nodes if n.role in roles]

    @property
    def minor(self) -> int:
        try:
            return int(self.ocp_version.split(".")[1])
        except Exception:
            return 0


SECRET_PATHS = [
    ("pull_secret",),
    ("vcenter", "password"),
]
