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
ProviderName = Literal["vsphere", "proxmox", "libvirt", "redfish", "manual"]   # where agent-based nodes run
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
    bmc_address: str = ""             # Redfish (bare metal): https://idrac.example.com or an IP
    bmc_username: str = ""            # empty = RedfishSpec defaults
    bmc_password: str = ""            # secret
    bmc_system_id: str = ""           # Redfish System member id when the BMC has several (e.g. System.Embedded.1)

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


class ProxmoxSpec(BaseModel):
    host: str = ""
    port: int = 8006
    token_id: str = ""            # user@realm!tokenname
    token_secret: str = ""        # secret
    node: str = ""                # Proxmox node that runs the VMs
    storage: str = "local-lvm"    # disks
    iso_storage: str = "local"    # ISO images (content type iso)
    bridge: str = "vmbr0"
    verify_tls: bool = False


class LibvirtSpec(BaseModel):
    host: str = ""                # KVM host reachable over SSH
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_password: str = ""        # secret; empty = installer host's SSH key
    pool: str = "default"
    bridge: str = "br0"
    images_dir: str = "/var/lib/libvirt/images"
    os_variant: str = "rhel9.0"


class RedfishSpec(BaseModel):
    username: str = ""            # default BMC credentials for nodes without their own
    password: str = ""            # secret
    verify_tls: bool = False
    iso_url_base: str = ""        # http://<installer-host>:8080 as seen from the BMCs; auto-detected when empty


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


# ---------------------------------------------------------------- day 2
class HtpasswdUser(BaseModel):
    username: str
    password: str = ""            # secret; empty keeps the stored hash
    cluster_admin: bool = False


class LDAPSpec(BaseModel):
    enabled: bool = False
    name: str = "ldap"
    url: str = ""                 # ldaps://ldap.example.com/ou=users,dc=example,dc=com?uid
    bind_dn: str = ""
    bind_password: str = ""       # secret
    ca_pem: str = ""
    insecure: bool = False
    attr_id: str = "dn"
    attr_email: str = "mail"
    attr_name: str = "cn"
    attr_preferred_username: str = "uid"


class OIDCSpec(BaseModel):
    enabled: bool = False
    name: str = "oidc"
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""       # secret
    ca_pem: str = ""
    claim_preferred_username: str = "preferred_username"
    claim_name: str = "name"
    claim_email: str = "email"
    claim_groups: str = ""
    extra_scopes: List[str] = Field(default_factory=list)


class IdentitySpec(BaseModel):
    htpasswd_enabled: bool = False
    htpasswd_name: str = "htpasswd"
    users: List[HtpasswdUser] = Field(default_factory=list)
    ldap: LDAPSpec = Field(default_factory=LDAPSpec)
    oidc: OIDCSpec = Field(default_factory=OIDCSpec)
    cluster_admins: List[str] = Field(default_factory=list)   # extra user names (LDAP/OIDC) to bind cluster-admin
    kubeadmin_disabled: bool = False


class CustomCertSpec(BaseModel):
    api_cert_pem: str = ""
    api_key_pem: str = ""         # secret
    apps_cert_pem: str = ""
    apps_key_pem: str = ""        # secret
    ca_pem: str = ""              # issuing CA chain to add to the cluster-wide trust


class ACMESpec(BaseModel):
    email: str = ""
    staging: bool = False
    provider: Literal["cloudflare", "route53", "rfc2136"] = "cloudflare"
    cloudflare_token: str = ""    # secret
    aws_access_key: str = ""
    aws_secret_key: str = ""      # secret
    aws_region: str = "us-east-1"
    aws_hosted_zone_id: str = ""
    rfc2136_nameserver: str = ""  # ip:53
    rfc2136_tsig_key_name: str = ""
    rfc2136_tsig_secret: str = "" # secret
    rfc2136_tsig_algorithm: str = "HMACSHA256"


class CertsSpec(BaseModel):
    mode: Literal["none", "custom", "acme"] = "none"
    custom: CustomCertSpec = Field(default_factory=CustomCertSpec)
    acme: ACMESpec = Field(default_factory=ACMESpec)


class NFSSpec(BaseModel):
    server: str = ""
    path: str = ""
    sc_name: str = "nfs-client"
    make_default: bool = False


class RegistrySpec(BaseModel):
    mode: Literal["pvc", "emptydir", "removed"] = "pvc"
    storage_class: str = ""       # empty = cluster default
    size_gb: int = 100
    replicas: int = 1


class StorageSpec(BaseModel):
    nfs: NFSSpec = Field(default_factory=NFSSpec)
    registry: RegistrySpec = Field(default_factory=RegistrySpec)
    odf_min_disk_gb: int = 100
    odf_max_disk_gb: int = 20000


class BackupSpec(BaseModel):
    schedule: str = ""            # systemd OnCalendar, e.g. daily or *-*-* 02:00:00; empty = no timer
    keep: int = 7


class PowerScaleArray(BaseModel):
    """One OneFS cluster in the driver's isilon-creds. The password is never stored by the app:
    it is sent with each install/check request and only lands in the cluster's secret."""
    name: str = ""                        # clusterName in isilon-creds (logical name, used by StorageClasses)
    endpoint: str = ""                    # OneFS API host/IP
    port: int = 8080
    username: str = ""
    is_default: bool = False
    skip_cert_validation: bool = True
    ca_pem: str = ""                      # CA to trust when skip_cert_validation is off (goes to <release>-certs-N)
    access_zone: str = "System"
    isi_path: str = "/ifs/data/csi"
    az_service_ip: str = ""               # SmartConnect name/IP for NFS traffic; empty = endpoint
    replication_certificate_id: str = ""  # SyncIQ encryption: this array's certificate ID (replicationCertificateID)


class PowerScaleClass(BaseModel):
    name: str = "isilon"
    array: str = ""                       # clusterName; empty = the default array
    access_zone: str = "System"
    isi_path: str = "/ifs/data/csi"
    az_service_ip: str = ""
    root_client_enabled: bool = False
    reclaim_policy: Literal["Delete", "Retain"] = "Delete"
    binding_mode: Literal["Immediate", "WaitForFirstConsumer"] = "Immediate"
    default: bool = False


class PowerScaleReplication(BaseModel):
    enabled: bool = False
    peer: str = ""                        # the other cluster (installed or connected in this app); empty = same cluster
    local_cluster_id: str = ""            # clusterId of this cluster in the replication config
    remote_cluster_id: str = ""           # clusterId of the peer ("self" for single-cluster replication)
    source_array: str = ""                # clusterName of the local array
    target_array: str = ""                # clusterName of the remote array (as known to the peer's driver)
    rpo: str = "Five_Minutes"
    class_name: str = "isilon-replication"
    remote_class_name: str = "isilon-replication"
    source_zone: str = "System"
    target_zone: str = "System"
    source_path: str = "/ifs/data/csi"
    target_path: str = "/ifs/data/csi"
    source_az_service_ip: str = ""
    target_az_service_ip: str = ""
    volume_group_prefix: str = "csi"
    ignore_namespaces: bool = False
    chart_version: str = ""               # csm-replication chart (helm method)


class PowerScaleSpec(BaseModel):
    method: Literal["helm", "operator"] = "helm"
    chart_version: str = ""               # csi-isilon chart from the offline bundle (helm method)
    namespace: str = "isilon"
    release: str = "isilon"               # helm release (also the prefix of <release>-creds / -certs-N)
    arrays: List[PowerScaleArray] = Field(default_factory=list)
    auth_type: int = 1                    # 0 basic, 1 session (OneFS 9.15+ needs 1)
    enable_quota: bool = True
    snapshots: bool = True
    snapshot_class: str = "isilon-snapclass"
    resizer: bool = True
    controller_count: int = 2
    volume_name_prefix: str = "csivol"
    infra_nodes: bool = False             # also run the node plugin on nodes tainted node-role.kubernetes.io/infra
    image_registry: str = ""              # disconnected: pull the driver images from this registry
    log_level: Literal["error", "warn", "info", "debug"] = "info"
    classes: List[PowerScaleClass] = Field(default_factory=list)
    replication: PowerScaleReplication = Field(default_factory=PowerScaleReplication)


class Day2Spec(BaseModel):
    identity: IdentitySpec = Field(default_factory=IdentitySpec)
    certs: CertsSpec = Field(default_factory=CertsSpec)
    storage: StorageSpec = Field(default_factory=StorageSpec)
    backup: BackupSpec = Field(default_factory=BackupSpec)
    powerscale: PowerScaleSpec = Field(default_factory=PowerScaleSpec)


class ClusterSpec(BaseModel):
    name: str
    base_domain: str = ""
    ocp_version: str = ""
    install_method: InstallMethod = "ipi"
    topology: Topology = "standard"
    provider: ProviderName = "vsphere"   # agent method only; IPI is always vSphere
    vcenter: VCenterSpec = Field(default_factory=VCenterSpec)
    proxmox: ProxmoxSpec = Field(default_factory=ProxmoxSpec)
    libvirt: LibvirtSpec = Field(default_factory=LibvirtSpec)
    redfish: RedfishSpec = Field(default_factory=RedfishSpec)
    lb: LBSpec = Field(default_factory=LBSpec)
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    nodes: List[NodeSpec] = Field(default_factory=list)
    pools: List[NodePool] = Field(default_factory=list)
    proxy: ProxySpec = Field(default_factory=ProxySpec)
    mirror: MirrorSpec = Field(default_factory=MirrorSpec)
    additional_trust_bundle: str = ""   # extra CA certificates (PEM) to trust cluster-wide
    day2: Day2Spec = Field(default_factory=Day2Spec)
    pull_secret: str = ""         # secret
    ssh_public_key: str = ""
    fips: bool = False
    status: str = "new"           # new | configured | deploying | installed | failed | destroyed
    imported: bool = False        # connected cluster (RAM-only session, not installed by this app)
    read_only: bool = False       # imported with read-only mode (every change is refused)
    platform: str = ""            # infrastructure platform reported by an imported cluster
    cluster_domain: str = ""      # imported: the cluster's real <name>.<base> when its console name differs

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?", v):
            raise ValueError("cluster name must be a lowercase DNS label")
        return v

    # ---- derived helpers -------------------------------------------------
    @property
    def domain(self) -> str:
        return self.cluster_domain or f"{self.name}.{self.base_domain}"

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


# Paths of secret leaves inside the raw cluster dict; [] marks "every element of this list".
SECRET_PATHS = [
    ("pull_secret",),
    ("vcenter", "password"),
    ("lb", "vms", [], "ssh_password"),
    ("day2", "identity", "users", [], "password"),
    ("day2", "identity", "ldap", "bind_password"),
    ("day2", "identity", "oidc", "client_secret"),
    ("day2", "certs", "custom", "api_key_pem"),
    ("day2", "certs", "custom", "apps_key_pem"),
    ("day2", "certs", "acme", "cloudflare_token"),
    ("day2", "certs", "acme", "aws_secret_key"),
    ("day2", "certs", "acme", "rfc2136_tsig_secret"),
    ("proxmox", "token_secret"),
    ("libvirt", "ssh_password"),
    ("redfish", "password"),
    ("nodes", [], "bmc_password"),
]
