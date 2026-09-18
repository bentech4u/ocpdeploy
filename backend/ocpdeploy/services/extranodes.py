"""Add extra (bare-metal) nodes to a connected cluster with `oc adm node-image`.

Per the user's choice the node ISO is the one thing about a connected cluster that is
written to disk: it lands in ROOT/work/<build-id>/ (mode 700, ISO mode 600) and stays
there until "Delete ISO" is pressed, so it can still be downloaded after the connection
is gone. Everything else stays in the connection's RAM directory: the registry
credentials read from the cluster, the mirror CA and the monitor state."""
import base64
import ipaddress
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..jobs import JobContext
from ..models import ClusterSpec
from ..settings import ROOT
from ..store import ClusterStore
from . import kube, clusterops as ops

WORK_DIR = ROOT / "work"
MIN_MINOR = 17
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}-\d{8}-\d{6}$")
_MAC = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


# ---------------------------------------------------------------- work dir / builds
def _work() -> Path:
    WORK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(WORK_DIR, 0o700)
    return WORK_DIR


def build_dir(build_id: str) -> Path:
    if not _ID.match(build_id or ""):
        raise ValueError("bad build id")
    return _work() / build_id


def list_builds(cluster: Optional[str] = None) -> List[Dict]:
    out = []
    for d in sorted(_work().iterdir(), reverse=True) if WORK_DIR.exists() else []:
        if not (d.is_dir() and _ID.match(d.name)):
            continue
        try:
            meta = json.loads((d / "meta.json").read_text())
        except Exception:
            meta = {}
        if cluster and meta.get("cluster") != cluster:
            continue
        iso = next(d.glob("*.iso"), None)
        out.append({"id": d.name, "cluster": meta.get("cluster", ""), "server": meta.get("server", ""), "created": meta.get("created", ""),
                    "hosts": meta.get("hosts", []), "status": meta.get("status", "unknown"),
                    "iso": iso.name if iso else "", "size_mb": round(iso.stat().st_size / 2**20) if iso else 0})
    return out


def delete_build(build_id: str) -> bool:
    d = build_dir(build_id)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


def iso_path(build_id: str) -> Path:
    iso = next(build_dir(build_id).glob("*.iso"), None)
    if not iso:
        raise FileNotFoundError(build_id)
    return iso


# ---------------------------------------------------------------- host input
def host_from_form(h: Dict) -> Dict:
    """One nodes-config.yaml host from the static-IP form fields."""
    name = (h.get("hostname") or "").strip().lower()
    if not _LABEL.match(name):
        raise ValueError(f"hostname '{name}': lowercase letters, digits and dashes")
    mac = (h.get("mac") or "").strip().lower()
    if not _MAC.match(mac):
        raise ValueError(f"{name}: MAC address must look like 00:11:22:33:44:55")
    iface = (h.get("interface") or "eth0").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,15}", iface):
        raise ValueError(f"{name}: bad interface name")
    try:
        ip = ipaddress.ip_address((h.get("ip") or "").strip())
        prefix = int(h.get("prefix") or 24)
        net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
        gw = ipaddress.ip_address((h.get("gateway") or "").strip())
    except ValueError as ex:
        raise ValueError(f"{name}: {ex}")
    if gw not in net:
        raise ValueError(f"{name}: gateway {gw} is not inside {net}")
    dns = [x for x in re.split(r"[,\s]+", h.get("dns") or "") if x]
    for d in dns:
        ipaddress.ip_address(d)
    if not dns:
        raise ValueError(f"{name}: at least one DNS server is required")
    fam = "ipv6" if ip.version == 6 else "ipv4"
    other = "ipv4" if fam == "ipv6" else "ipv6"
    host: Dict = {"hostname": name, "interfaces": [{"name": iface, "macAddress": mac}],
                  "networkConfig": {
                      "interfaces": [{"name": iface, "type": "ethernet", "state": "up", "mac-address": mac,
                                      fam: {"enabled": True, "dhcp": False, "address": [{"ip": str(ip), "prefix-length": prefix}]},
                                      other: {"enabled": False}}],
                      "dns-resolver": {"config": {"server": dns}},
                      "routes": {"config": [{"destination": "::/0" if fam == "ipv6" else "0.0.0.0/0", "next-hop-address": str(gw),
                                             "next-hop-interface": iface, "table-id": 254}]}}}
    root = (h.get("root_device") or "").strip()
    if root:
        if not root.startswith("/dev/"):
            raise ValueError(f"{name}: root device must be a /dev/ path")
        host["rootDeviceHints"] = {"deviceName": root}
    nm = (h.get("nmstate") or "").strip()
    if nm:
        try:
            custom = yaml.safe_load(nm)
        except yaml.YAMLError as ex:
            raise ValueError(f"{name}: NMState YAML: {str(ex)[:120]}")
        if not isinstance(custom, dict) or "interfaces" not in custom:
            raise ValueError(f"{name}: NMState YAML must be a mapping with 'interfaces'")
        host["networkConfig"] = custom
    return host


def parse_nodes_config(text: str) -> List[Dict]:
    try:
        d = yaml.safe_load(text)
    except yaml.YAMLError as ex:
        raise ValueError(f"nodes-config.yaml: {str(ex)[:160]}")
    hosts = (d or {}).get("hosts") if isinstance(d, dict) else None
    if not isinstance(hosts, list) or not hosts:
        raise ValueError("nodes-config.yaml needs a non-empty 'hosts' list")
    for h in hosts:
        macs = [i.get("macAddress") for i in (h.get("interfaces") or []) if isinstance(i, dict)]
        if not macs or not all(_MAC.match(str(m or "")) for m in macs):
            raise ValueError(f"host {h.get('hostname', '?')}: every interface needs a macAddress")
    return hosts


def host_summary(h: Dict) -> Dict:
    ips = []
    for i in ((h.get("networkConfig") or {}).get("interfaces") or []):
        for fam in ("ipv4", "ipv6"):
            for a in ((i.get(fam) or {}).get("address") or []):
                if a.get("ip"):
                    ips.append(a["ip"])
    return {"hostname": h.get("hostname", ""), "macs": [i.get("macAddress") for i in h.get("interfaces") or []], "ips": ips}


# ---------------------------------------------------------------- cluster checks
def support(store: ClusterStore, spec: ClusterSpec) -> Dict:
    ok = spec.minor >= MIN_MINOR
    return {"supported": ok and not spec.read_only, "version": spec.ocp_version, "platform": spec.platform,
            "reason": "" if ok else f"oc adm node-image needs OpenShift 4.{MIN_MINOR} or newer (cluster is {spec.ocp_version})",
            "read_only": spec.read_only}


def _registry_files(store: ClusterStore, spec: ClusterSpec, log) -> List[str]:
    """Pull secret (and mirror CA if the cluster has one) from the cluster into its RAM dir."""
    ram = store.dir / "extranodes"
    ram.mkdir(mode=0o700, exist_ok=True)
    auth = ram / "auth.json"
    data = kube.oc(store, spec, ["get", "secret", "pull-secret", "-n", "openshift-config", "-o", "jsonpath={.data.\\.dockerconfigjson}"])
    fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64decode(data))
    args = ["-a", str(auth)]
    log("Using the cluster's own pull secret for the release payload (kept in memory)")
    img = ops.get_opt(store, spec, "image.config.openshift.io", "cluster") or {}
    ca_cm = ((img.get("spec") or {}).get("additionalTrustedCA") or {}).get("name")
    if ca_cm:
        cm = ops.get_opt(store, spec, "configmap", ca_cm, ns="openshift-config") or {}
        pems = "\n".join(v for v in (cm.get("data") or {}).values() if "BEGIN CERTIFICATE" in v)
        if pems:
            (ram / "registry-ca.pem").write_text(pems)
            args += ["--certificate-authority", str(ram / "registry-ca.pem")]
            log(f"Using the registry CAs from configmap {ca_cm} (mirror registry)")
    return args


def _joiner_namespaces(store: ClusterStore, spec: ClusterSpec) -> set:
    try:
        out = kube.oc(store, spec, ["get", "namespaces", "-o", "name"], timeout=60)
    except Exception:
        return set()
    return {l.split("/", 1)[1] for l in out.split() if l.startswith("namespace/openshift-node-joiner-")}


def _cleanup_joiner(store: ClusterStore, spec: ClusterSpec, before: set, log):
    """oc removes its temporary namespace on success, but not when it is stopped or times
    out. Delete only the namespaces that appeared while this job ran."""
    for ns in sorted(_joiner_namespaces(store, spec) - before):
        try:
            kube.oc(store, spec, ["delete", "namespace", ns, "--wait=false"], timeout=60)
            log(f"removed temporary namespace {ns}")
        except Exception as ex:
            log(f"could not remove temporary namespace {ns}: {str(ex)[-120:]}")


def _env(store: ClusterStore) -> Dict[str, str]:
    return {"KUBECONFIG": kube.kubeconfig(store), "HOME": str(store.dir), "KUBECACHEDIR": str(store.dir / ".kube" / "cache")}


# ---------------------------------------------------------------- jobs
def job_build(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, hosts: List[Dict]):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bid = f"{spec.name}-{stamp}"
    d = build_dir(bid)
    d.mkdir(mode=0o700)
    meta = {"cluster": spec.name, "server": spec.lb.external_api.fqdn, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hosts": [host_summary(h) for h in hosts], "status": "building"}
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    (d / "nodes-config.yaml").write_text(yaml.safe_dump({"hosts": hosts}, sort_keys=False))
    ctx.log(f"Building node ISO {bid} for: " + ", ".join(f"{h['hostname']} ({', '.join(h['ips']) or 'custom network'})" for h in meta["hosts"]))
    ctx.log("This takes several minutes: the base image is extracted from the cluster's release payload.")
    before = _joiner_namespaces(store, spec)
    try:
        reg = _registry_files(store, spec, ctx.log)
        ctx.run([kube.oc_bin(spec), "adm", "node-image", "create", "--dir", str(d), "--report"] + reg, env=_env(store))
        iso = next(d.glob("*.iso"), None)
        if not iso:
            raise RuntimeError("the command finished but produced no ISO")
        os.chmod(iso, 0o600)
        meta["status"] = "ready"
        ctx.log(f"ISO ready: {iso.name} ({iso.stat().st_size // 2**20} MiB). Download it from the Add Extra nodes page and boot every listed host from it.")
    except Exception:
        meta["status"] = "failed"
        rep = d / "report.json"
        if rep.exists():
            try:
                r = json.loads(rep.read_text())
                for st in r.get("stages", []):
                    if st.get("result") and "error" in str(st.get("result")).lower():
                        ctx.log(f"report: {st.get('description')}: {str(st.get('result'))[:400]}")
                if r.get("troubleshooting"):
                    ctx.log("report troubleshooting: " + str(r["troubleshooting"])[:600])
            except Exception:
                pass
        raise
    finally:
        _cleanup_joiner(store, spec, before, ctx.log)
        # only the ISO and a small index stay on disk
        for f in ("nodes-config.yaml", "report.json"):
            (d / f).unlink(missing_ok=True)
        for sub in d.iterdir():
            if sub.is_dir():
                shutil.rmtree(sub, ignore_errors=True)
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        if meta["status"] == "failed":
            for iso in d.glob("*.iso"):
                iso.unlink()


def job_monitor(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, ips: List[str]):
    for ip in ips:
        ipaddress.ip_address(ip)
    ctx.log(f"Monitoring {', '.join(ips)}. Boot the hosts from the ISO now; approve their CSRs on the page when they appear.")
    reg = _registry_files(store, spec, ctx.log)
    before = _joiner_namespaces(store, spec)
    try:
        ctx.run([kube.oc_bin(spec), "adm", "node-image", "monitor", "--ip-addresses", ",".join(ips)] + reg, env=_env(store))
        ctx.log("All monitored nodes have joined the cluster.")
    finally:
        _cleanup_joiner(store, spec, before, ctx.log)


# ---------------------------------------------------------------- CSRs / labels
def _csr_cn(req_b64: str) -> str:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    try:
        csr = x509.load_pem_x509_csr(base64.b64decode(req_b64))
        cn = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return cn[0].value if cn else ""
    except Exception:
        return ""


def pending_csrs(store: ClusterStore, spec: ClusterSpec, hostnames: List[str]) -> Dict:
    """Pending CSRs whose node name matches one of the hosts being added."""
    wanted = {h.lower() for h in hostnames if h}
    csrs = ops.get(store, spec, "csr")
    mine, others = [], 0
    for c in csrs.get("items", []):
        if (c.get("status") or {}).get("conditions"):
            continue
        cn = _csr_cn(c["spec"].get("request", ""))
        node = cn.removeprefix("system:node:")
        short = node.split(".")[0].lower()
        if cn.startswith("system:node:") and (node.lower() in wanted or short in wanted):
            kind = "client (node bootstrap)" if "node-bootstrapper" in c["spec"].get("username", "") else "serving (kubelet)"
            mine.append({"name": c["metadata"]["name"], "node": node, "kind": kind, "requestor": c["spec"].get("username", ""),
                         "signer": c["spec"].get("signerName", ""), "age": ops.age(c["metadata"].get("creationTimestamp"))})
        else:
            others += 1
    return {"csrs": mine, "other_pending": others}


def approve_csr(store: ClusterStore, spec: ClusterSpec, name: str, hostnames: List[str]) -> str:
    """Approve one CSR, but only if it belongs to a host being added."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name or ""):
        raise ValueError("bad CSR name")
    if name not in {c["name"] for c in pending_csrs(store, spec, hostnames)["csrs"]}:
        raise PermissionError("this CSR is not pending for one of the hosts being added")
    return kube.oc(store, spec, ["adm", "certificate", "approve", name]).strip()


def node_status(store: ClusterStore, spec: ClusterSpec, hostnames: List[str]) -> List[Dict]:
    wanted = {h.lower() for h in hostnames if h}
    out = []
    for n in ops.nodes_summary(store, spec):
        if n["name"].lower() in wanted or n["name"].split(".")[0].lower() in wanted:
            out.append(n)
    return out


def label_node(store: ClusterStore, spec: ClusterSpec, node: str, role: str, hostnames: List[str]) -> str:
    if not _LABEL.match(role or ""):
        raise ValueError("role must be a lowercase DNS label, e.g. infra or storage")
    names = {n["name"] for n in node_status(store, spec, hostnames)}
    if node not in names:
        raise PermissionError("only nodes added from this page can be labelled here")
    return kube.oc(store, spec, ["label", "node", node, f"node-role.kubernetes.io/{role}=", "--overwrite"]).strip()
