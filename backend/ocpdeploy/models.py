"""Cluster specification. This is the single source of truth a cluster folder
stores; every generated artefact (install-config, agent-config, haproxy.cfg,
DNS checklist, machinesets, imageset-config) is rendered from it.

Every field added after the first release has a default so older cluster.json
files keep loading unchanged."""
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator
import ipaddress
import re

Role = Literal["bootstrap", "master", "worker", "infra"]
InstallMethod = Literal["ipi", "agent"]
LBMode = Literal["haproxy", "external", "none"]
HAProxyLayout = Literal["split", "ha"]
Topology = Literal["standard", "compact", "sno"]
PoolKind = Literal["general", "gpu", "storage"]

_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"


class NodeSpec(BaseModel):
    name: str
    role: Role
    ip: str
    mac: Optional[str] = None
    cpus: int = 4
    memory_mb: int = 16384
    disk_gb: int = 120
    failure_domain: str = ""          # name of a VCenterSpec.failure_domains entry (IPI); "" = default placement
    pool: str = ""                    # name of a ClusterSpec.pools entry (workers only); "" = plain day-1 worker
    extra_disks_gb: List[int] = Field(default_factory=list)   # additional data disks (storage pools)

    @field_validator("ip")
    @classmethod
    def _ip(cls, v):
        ipaddress.ip_address(v)
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not re.fullmatch(_LABEL, v):
            raise ValueError("node name must be a lowercase DNS label")
        return v


class FailureDomain(BaseModel):
    """One vSphere placement (region/zone). Region tags go on the datacenter,
    zone tags on the compute cluster; the installer spreads machines across them."""
    name: str = ""
    region: str = ""
    zone: str = ""
    datacenter: str = ""
    cluster: str = ""
    datastore: str = ""
    network: str = ""
    resource_pool: str = ""
    folder: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if v and not re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?", v):
            raise ValueError("failure domain name may contain letters, digits, - and _")
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
    failure_domains: List[FailureDomain] = Field(default_factory=list)   # empty = single implicit domain from the fields above
    cluster_os_image: str = ""    # optional http(s) URL of the RHCOS OVA (disconnected IPI installs)


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


class Taint(BaseModel):
    key: str = ""
    value: str = ""
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"] = "NoSchedule"


class NodePool(BaseModel):
    """A group of workers created on day 2 with their own size, labels and taints
    (GPU nodes, storage nodes, big-memory nodes...). Members are NodeSpec entries
    with role=worker and pool=<name>."""
    name: str
    kind: PoolKind = "general"
    description: str = ""
    labels: Dict[str, str] = Field(default_factory=dict)
    taints: List[Taint] = Field(default_factory=list)
    extra_disks_gb: List[int] = Field(default_factory=list)   # default data disks for members
    serve_ingress: bool = False    # include members in the apps load balancer pool

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not re.fullmatch(_LABEL, v) or v in ("master", "worker", "infra", "bootstrap"):
            raise ValueError("pool name must be a lowercase DNS label other than master/worker/infra/bootstrap")
        return v


class ProxySpec(BaseModel):
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = ""            # comma separated; the cluster networks are always added by the installer


class MirrorSource(BaseModel):
    """One imageDigestSources entry: pulls for `source` are redirected to `mirrors`."""
    source: str = ""
    mirrors: List[str] = Field(default_factory=list)


class MirrorSpec(BaseModel):
    """Disconnected / restricted-network installs through a mirror registry."""
    enabled: bool = False
    registry: str = ""            # host[:port] of the mirror registry, e.g. mirror.example.com:8443
    ca_pem: str = ""              # registry CA (PEM), added to the trust bundle
    tls_verify: bool = True
    sources: List[MirrorSource] = Field(default_factory=list)
    channel: str = ""             # update channel to mirror from; default stable-4.N
    catalog: str = ""             # operator catalog for oc-mirror; default redhat-operator-index:v4.N
    operators: List[str] = Field(default_factory=list)          # package names to mirror
    additional_images: List[str] = Field(default_factory=list)
    mirrored_version: str = ""    # last version successfully mirrored by the app


class ClusterSpec(BaseModel):
    name: str
    base_domain: str = ""
    ocp_version: str = ""
    install_method: InstallMethod = "ipi"
    topology: Topology = "standard"
    vcenter: VCenterSpec = Field(default_factory=VCenterSpec)
    lb: LBSpec = Field(default_factory=LBSpec)
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    nodes: List[NodeSpec] = Field(default_factory=list)
    pools: List[NodePool] = Field(default_factory=list)
    proxy: ProxySpec = Field(default_factory=ProxySpec)
    mirror: MirrorSpec = Field(default_factory=MirrorSpec)
    additional_trust_bundle: str = ""   # extra CA certificates (PEM) to trust cluster-wide
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
        if lb.mode == "none":
            # single node: DNS points straight at the node
            m = self.nodes_by_role("master")
            return m[0].ip if m else ""
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

    def day1_workers(self) -> List[NodeSpec]:
        """Workers the installer creates on day 1 (members of a named pool are day-2)."""
        return [n for n in self.nodes if n.role == "worker" and not n.pool]

    def pooled_workers(self, pool: Optional[str] = None) -> List[NodeSpec]:
        return [n for n in self.nodes if n.role == "worker" and n.pool and (pool is None or n.pool == pool)]

    def pool(self, name: str) -> Optional[NodePool]:
        return next((p for p in self.pools if p.name == name), None)

    @property
    def is_sno(self) -> bool:
        return len(self.nodes_by_role("master")) == 1

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
