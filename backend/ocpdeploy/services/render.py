"""Render install-config.yaml / agent-config.yaml and write the install dir."""
import ipaddress
import json
import shutil
import time
from pathlib import Path
from typing import Dict, List

import yaml

from ..models import ClusterSpec
from ..store import ClusterStore


def _prefix(spec: ClusterSpec) -> int:
    return ipaddress.ip_network(spec.network.machine_cidr, strict=False).prefixlen


def _vs_platform_node(n) -> Dict:
    return {"cpus": n.cpus, "coresPerSocket": min(2, n.cpus), "memoryMB": n.memory_mb, "osDisk": {"diskSizeGB": n.disk_gb}}


def install_config(spec: ClusterSpec) -> Dict:
    masters = spec.nodes_by_role("master")
    workers = spec.nodes_by_role("worker")
    if not masters:
        raise ValueError("at least one master node is required")
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
    if spec.install_method == "ipi":
        vc = spec.vcenter
        m0, w0 = masters[0], (workers[0] if workers else masters[0])
        cfg["controlPlane"]["platform"] = {"vsphere": _vs_platform_node(m0)}
        cfg["compute"][0]["platform"] = {"vsphere": _vs_platform_node(w0)}
        topo = {
            "datacenter": vc.datacenter,
            "computeCluster": f"/{vc.datacenter}/host/{vc.cluster}",
            "datastore": f"/{vc.datacenter}/datastore/{vc.datastore}",
            "networks": [vc.network],
        }
        if vc.resource_pool:
            topo["resourcePool"] = vc.resource_pool if vc.resource_pool.startswith("/") else f"/{vc.datacenter}/host/{vc.cluster}/Resources/{vc.resource_pool}"
        if vc.folder:
            topo["folder"] = vc.folder if vc.folder.startswith("/") else f"/{vc.datacenter}/vm/{vc.folder}"
        hosts: List[Dict] = []
        plen = _prefix(spec)
        for n in spec.nodes:
            role = {"bootstrap": "bootstrap", "master": "control-plane", "worker": "compute"}.get(n.role)
            if role is None:
                continue  # infra nodes are added on day 2
            hosts.append({"role": role, "networkDevice": {
                "ipAddrs": [f"{n.ip}/{plen}"], "gateway": spec.network.gateway,
                "nameservers": spec.network.dns_servers}})
        cfg["platform"] = {"vsphere": {
            "apiVIPs": [spec.api_ip],
            "ingressVIPs": [spec.apps_ip],
            "loadBalancer": {"type": "UserManaged"},
            "failureDomains": [{"name": "fd-1", "region": "region-1", "zone": "zone-1", "server": vc.host, "topology": topo}],
            "vcenters": [{"server": vc.host, "user": vc.username, "password": vc.password, "datacenters": [vc.datacenter]}],
            "hosts": hosts,
        }}
        if vc.cert_pem:
            cfg["additionalTrustBundle"] = vc.cert_pem
    else:
        cfg["platform"] = {"none": {}}
        if spec.vcenter.cert_pem:
            cfg["additionalTrustBundle"] = spec.vcenter.cert_pem
    return cfg


def agent_config(spec: ClusterSpec) -> Dict:
    masters = spec.nodes_by_role("master")
    plen = _prefix(spec)
    iface = spec.network.interface_name
    hosts = []
    for n in spec.nodes_by_role("master", "worker"):
        if not n.mac:
            raise ValueError(f"node {n.name} has no MAC address; generate MACs in the Nodes step")
        hosts.append({
            "hostname": n.name,
            "role": n.role,
            "interfaces": [{"name": iface, "macAddress": n.mac}],
            "networkConfig": {
                "interfaces": [{"name": iface, "type": "ethernet", "state": "up", "mac-address": n.mac,
                                "ipv4": {"enabled": True, "dhcp": False, "address": [{"ip": n.ip, "prefix-length": plen}]},
                                "ipv6": {"enabled": False}}],
                "dns-resolver": {"config": {"server": spec.network.dns_servers, "search": [spec.domain]}},
                "routes": {"config": [{"destination": "0.0.0.0/0", "next-hop-address": spec.network.gateway,
                                       "next-hop-interface": iface, "table-id": 254}]},
            },
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
            "networkConfig": {
                "interfaces": [{"name": iface, "type": "ethernet", "state": "up", "mac-address": n.mac,
                                "ipv4": {"enabled": True, "dhcp": False, "address": [{"ip": n.ip, "prefix-length": plen}]},
                                "ipv6": {"enabled": False}}],
                "dns-resolver": {"config": {"server": spec.network.dns_servers, "search": [spec.domain]}},
                "routes": {"config": [{"destination": "0.0.0.0/0", "next-hop-address": spec.network.gateway,
                                       "next-hop-interface": iface, "table-id": 254}]},
            },
        })
    return {"hosts": hosts}


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
    pass


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
