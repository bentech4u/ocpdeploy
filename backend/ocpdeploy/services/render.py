"""Render install-config.yaml / agent-config.yaml / imageset-config.yaml and write the install dir."""
import ipaddress
import json
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..models import ClusterSpec, FailureDomain, VCenterSpec
from ..store import ClusterStore

RELEASE_IMAGE_REPO = "quay.io/openshift-release-dev/ocp-release"
RELEASE_ART_REPO = "quay.io/openshift-release-dev/ocp-v4.0-art-dev"


def _prefix(spec: ClusterSpec) -> int:
    return ipaddress.ip_network(spec.network.machine_cidr, strict=False).prefixlen


def _vs_platform_node(n) -> Dict:
    return {"cpus": n.cpus, "coresPerSocket": min(2, n.cpus), "memoryMB": n.memory_mb, "osDisk": {"diskSizeGB": n.disk_gb}}


def _topology(datacenter: str, cluster: str, datastore: str, network: str, resource_pool: str, folder: str) -> Dict:
    topo = {
        "datacenter": datacenter,
        "computeCluster": f"/{datacenter}/host/{cluster}",
        "datastore": f"/{datacenter}/datastore/{datastore}",
        "networks": [network],
    }
    if resource_pool:
        topo["resourcePool"] = resource_pool if resource_pool.startswith("/") else f"/{datacenter}/host/{cluster}/Resources/{resource_pool}"
    if folder:
        topo["folder"] = folder if folder.startswith("/") else f"/{datacenter}/vm/{folder}"
    return topo


def effective_failure_domains(vc: VCenterSpec) -> List[FailureDomain]:
    """The explicit failure domains, or one implicit domain built from the default placement."""
    if vc.failure_domains:
        return vc.failure_domains
    return [FailureDomain(name="fd-1", region="region-1", zone="zone-1", datacenter=vc.datacenter, cluster=vc.cluster,
                          datastore=vc.datastore, network=vc.network, resource_pool=vc.resource_pool, folder=vc.folder)]


def _zones(spec: ClusterSpec, nodes, fds: List[FailureDomain]) -> List[str]:
    """Zones a machine pool may use: the domains its nodes are pinned to, else all of them."""
    names = [fd.name for fd in fds]
    pinned = [n.failure_domain for n in nodes if n.failure_domain in names]
    return [nm for nm in names if nm in pinned] if pinned else names


def trust_bundle(spec: ClusterSpec) -> str:
    """Everything the cluster (and the installer host) must trust: vCenter CA, mirror
    registry CA and any user-supplied certificates. Unchanged single-CA output when
    only the vCenter certificate is present."""
    parts = []
    if spec.vcenter.cert_pem:
        parts.append(spec.vcenter.cert_pem)
    if spec.mirror.enabled and spec.mirror.ca_pem:
        parts.append(spec.mirror.ca_pem)
    if spec.additional_trust_bundle:
        parts.append(spec.additional_trust_bundle)
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return "\n".join(p.strip() for p in parts if p.strip()) + "\n"


def proxy_block(spec: ClusterSpec) -> Optional[Dict]:
    p = spec.proxy
    if not (p.http_proxy or p.https_proxy):
        return None
    out = {}
    if p.http_proxy:
        out["httpProxy"] = p.http_proxy
    if p.https_proxy:
        out["httpsProxy"] = p.https_proxy
    if p.no_proxy:
        out["noProxy"] = ",".join(x.strip() for x in p.no_proxy.replace(" ", ",").split(",") if x.strip())
    return out


def default_mirror_sources(registry: str) -> List[Dict]:
    """The two repositories oc-mirror v2 produces for a release."""
    return [
        {"source": RELEASE_ART_REPO, "mirrors": [f"{registry}/openshift/release"]},
        {"source": RELEASE_IMAGE_REPO, "mirrors": [f"{registry}/openshift/release-images"]},
    ]


def image_digest_sources(spec: ClusterSpec) -> List[Dict]:
    if not spec.mirror.enabled:
        return []
    srcs = [{"source": s.source, "mirrors": list(s.mirrors)} for s in spec.mirror.sources if s.source and s.mirrors]
    return srcs or (default_mirror_sources(spec.mirror.registry) if spec.mirror.registry else [])


def install_config(spec: ClusterSpec) -> Dict:
    masters = spec.nodes_by_role("master")
    workers = spec.day1_workers()
    if not masters:
        raise ValueError("at least one master node is required")
    if len(masters) not in (1, 3):
        raise ValueError("control plane must have 1 (single node) or 3 masters")
    cfg: Dict = {
        "apiVersion": "v1",
        "baseDomain": spec.base_domain,
        "metadata": {"name": spec.name},
        "controlPlane": {"name": "master", "replicas": len(masters), "hyperthreading": "Enabled", "architecture": "amd64"},
        "compute": [{"name": "worker", "replicas": len(workers), "hyperthreading": "Enabled", "architecture": "amd64"}],
        "networking": {
            "networkType": "OVNKubernetes",
            "machineNetwork": [{"cidr": spec.network.machine_cidr}],
            "clusterNetwork": [{"cidr": spec.network.cluster_network, "hostPrefix": spec.network.host_prefix}],
            "serviceNetwork": [spec.network.service_network],
        },
        "fips": spec.fips,
        "pullSecret": spec.pull_secret.strip(),
        "sshKey": spec.ssh_public_key.strip(),
    }
    bundle = trust_bundle(spec)
    if spec.install_method == "ipi":
        vc = spec.vcenter
        fds = effective_failure_domains(vc)
        explicit_fds = bool(vc.failure_domains)
        m0, w0 = masters[0], (workers[0] if workers else masters[0])
        cfg["controlPlane"]["platform"] = {"vsphere": _vs_platform_node(m0)}
        cfg["compute"][0]["platform"] = {"vsphere": _vs_platform_node(w0)}
        if explicit_fds:
            cfg["controlPlane"]["platform"]["vsphere"]["zones"] = _zones(spec, masters, fds)
            cfg["compute"][0]["platform"]["vsphere"]["zones"] = _zones(spec, workers, fds)
        hosts: List[Dict] = []
        plen = _prefix(spec)
        for n in spec.nodes:
            role = {"bootstrap": "bootstrap", "master": "control-plane", "worker": "compute"}.get(n.role)
            if role is None or (n.role == "worker" and n.pool):
                continue  # infra nodes and pool members are added on day 2
            h = {"role": role, "networkDevice": {
                "ipAddrs": [f"{n.ip}/{plen}"], "gateway": spec.network.gateway,
                "nameservers": list(spec.network.dns_servers)}}
            if explicit_fds and n.failure_domain:
                h["failureDomain"] = n.failure_domain
            hosts.append(h)
        datacenters: List[str] = []
        for fd in fds:
            if fd.datacenter and fd.datacenter not in datacenters:
                datacenters.append(fd.datacenter)
        platform = {
            "apiVIPs": [spec.api_ip],
            "ingressVIPs": [spec.apps_ip],
            "loadBalancer": {"type": "UserManaged"},
            "failureDomains": [{"name": fd.name, "region": fd.region, "zone": fd.zone, "server": vc.host,
                                "topology": _topology(fd.datacenter, fd.cluster, fd.datastore, fd.network, fd.resource_pool, fd.folder)}
                               for fd in fds],
            "vcenters": [{"server": vc.host, "user": vc.username, "password": vc.password, "datacenters": datacenters}],
            "hosts": hosts,
        }
        if vc.cluster_os_image:
            platform["clusterOSImage"] = vc.cluster_os_image
        cfg["platform"] = {"vsphere": platform}
    else:
        cfg["platform"] = {"none": {}}
    if bundle:
        cfg["additionalTrustBundle"] = bundle
    proxy = proxy_block(spec)
    if proxy:
        cfg["proxy"] = proxy
    if bundle and (proxy or spec.mirror.enabled):
        cfg["additionalTrustBundlePolicy"] = "Always"
    ids = image_digest_sources(spec)
    if ids:
        cfg["imageDigestSources"] = ids
    return cfg


def _host_network(spec: ClusterSpec, n, plen: int, iface: str) -> Dict:
    return {
        "interfaces": [{"name": iface, "type": "ethernet", "state": "up", "mac-address": n.mac,
                        "ipv4": {"enabled": True, "dhcp": False, "address": [{"ip": n.ip, "prefix-length": plen}]},
                        "ipv6": {"enabled": False}}],
        "dns-resolver": {"config": {"server": spec.network.dns_servers, "search": [spec.domain]}},
        "routes": {"config": [{"destination": "0.0.0.0/0", "next-hop-address": spec.network.gateway,
                               "next-hop-interface": iface, "table-id": 254}]},
    }


def agent_config(spec: ClusterSpec) -> Dict:
    masters = spec.nodes_by_role("master")
    if not masters:
        raise ValueError("at least one master node is required")
    plen = _prefix(spec)
    iface = spec.network.interface_name
    hosts = []
    for n in masters + spec.day1_workers():
        if not n.mac:
            raise ValueError(f"node {n.name} has no MAC address; generate MACs in the Nodes step")
        hosts.append({
            "hostname": n.name,
            "role": n.role,
            "interfaces": [{"name": iface, "macAddress": n.mac}],
            "networkConfig": _host_network(spec, n, plen, iface),
        })
    cfg = {
        "apiVersion": "v1beta1",
        "kind": "AgentConfig",
        "metadata": {"name": spec.name},
        "rendezvousIP": masters[0].ip,
        "hosts": hosts,
    }
    if spec.network.ntp_servers:
        cfg["additionalNTPSources"] = spec.network.ntp_servers
    return cfg


def nodes_config(spec: ClusterSpec, nodes) -> Dict:
    """nodes-config.yaml for `oc adm node-image create` (agent day-2 add nodes)."""
    plen = _prefix(spec)
    iface = spec.network.interface_name
    hosts = []
    for n in nodes:
        hosts.append({
            "hostname": n.name,
            "interfaces": [{"name": iface, "macAddress": n.mac}],
            "networkConfig": _host_network(spec, n, plen, iface),
        })
    return {"hosts": hosts}


def imageset_config(spec: ClusterSpec) -> Dict:
    """oc-mirror v2 ImageSetConfiguration for the selected release (+ operators / extra images)."""
    if not spec.ocp_version:
        raise ValueError("select an OpenShift version first")
    minor = f"4.{spec.minor}"
    channel = spec.mirror.channel or f"stable-{minor}"
    mirror: Dict = {"platform": {"channels": [{"name": channel, "minVersion": spec.ocp_version, "maxVersion": spec.ocp_version}], "graph": True}}
    if spec.mirror.operators:
        catalog = spec.mirror.catalog or f"registry.redhat.io/redhat/redhat-operator-index:v{minor}"
        mirror["operators"] = [{"catalog": catalog, "packages": [{"name": p} for p in spec.mirror.operators]}]
    if spec.mirror.additional_images:
        mirror["additionalImages"] = [{"name": i} for i in spec.mirror.additional_images]
    return {"kind": "ImageSetConfiguration", "apiVersion": "mirror.openshift.io/v2alpha1", "mirror": mirror}


def masked(cfg: Dict) -> Dict:
    c = json.loads(json.dumps(cfg))
    if "pullSecret" in c:
        c["pullSecret"] = "<pull secret redacted>"
    try:
        for v in c["platform"]["vsphere"]["vcenters"]:
            v["password"] = "<redacted>"
    except (KeyError, TypeError):
        pass
    return c


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True   # repeated lists (nameservers) are written out in full instead of as &id anchors


def _str_presenter(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_presenter)


def to_yaml(d: Dict) -> str:
    return yaml.dump(d, Dumper=_Dumper, sort_keys=False, default_flow_style=False, width=1000)


def write_install_dir(store: ClusterStore, spec: ClusterSpec, log=print, fresh: bool = True) -> Path:
    d = store.install_dir
    if fresh and d.exists() and any(d.iterdir()):
        bak = store.dir / f"install.{time.strftime('%Y%m%d-%H%M%S')}.bak"
        log(f"Existing install dir moved to {bak.name}")
        shutil.move(str(d), str(bak))
    d.mkdir(parents=True, exist_ok=True)
    ic = install_config(spec)
    (d / "install-config.yaml").write_text(to_yaml(ic))
    (store.dir / "install-config.yaml").write_text(to_yaml(ic))   # installer consumes the copy inside install/
    log("Wrote install-config.yaml")
    if spec.install_method == "agent":
        ac = agent_config(spec)
        (d / "agent-config.yaml").write_text(to_yaml(ac))
        (store.dir / "agent-config.yaml").write_text(to_yaml(ac))
        log("Wrote agent-config.yaml")
    return d
