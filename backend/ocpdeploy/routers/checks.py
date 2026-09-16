"""Per-cluster validation endpoints: DNS, load balancer, vCenter, mirror, preflight aggregate."""
import socket
from typing import Dict, List
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException

from ..jobs import start
from ..services import dnscheck, lbcheck, haproxy, vcenter, tools, versions, vctags, mirror
from .clusters import _store

router = APIRouter(prefix="/api/clusters/{name}", tags=["checks"])


@router.get("/dns/expected")
def dns_expected(name: str):
    return dnscheck.expected_records(_store(name).load())


@router.post("/dns/check")
def dns_check(name: str):
    s = _store(name)
    res = dnscheck.run(s.load())
    s.save_checks("dns", res)
    return res


@router.get("/lb/backends")
def lb_backends(name: str):
    return lbcheck.backends(_store(name).load())


@router.post("/lb/probe")
def lb_probe(name: str):
    s = _store(name)
    res = lbcheck.probe(s.load())
    s.save_checks("lb", res)
    return res


@router.get("/lb/render")
def lb_render(name: str):
    spec = _store(name).load()
    if spec.lb.mode != "haproxy":
        return {}
    out = {}
    for vm in spec.lb.vms:
        role = "both" if spec.lb.layout == "ha" else vm.role
        out[vm.host] = haproxy.render(spec, role)
    return out


@router.post("/lb/inspect")
def lb_inspect(name: str):
    spec = _store(name).load()
    out = []
    for vm in spec.lb.vms:
        try:
            out.append(haproxy.inspect(vm))
        except Exception as ex:
            out.append({"host": vm.host, "error": str(ex)})
    return out


@router.post("/lb/push")
def lb_push(name: str):
    s = _store(name)
    spec = s.load()
    from ..services.deploy import job_push_haproxy
    try:
        jid = start(s, "haproxy-push", lambda ctx: job_push_haproxy(ctx, s, spec))
    except RuntimeError as ex:
        raise HTTPException(409, str(ex))
    return {"job_id": jid}


# ---- vCenter
def _fd_rows(spec, inv) -> List[Dict]:
    """Objects referenced by every explicit failure domain must exist."""
    res: List[Dict] = []
    seen = set()
    for fd in spec.vcenter.failure_domains:
        if not fd.name:
            res.append({"name": "failure domain", "status": "fail", "expected": "name", "actual": "unnamed", "hint": "Every failure domain needs a name"})
            continue
        if fd.name in seen:
            res.append({"name": f"failure domain {fd.name}", "status": "fail", "expected": "unique name", "actual": "duplicate", "hint": ""})
        seen.add(fd.name)
        if not (fd.region and fd.zone):
            res.append({"name": f"failure domain {fd.name}", "status": "fail", "expected": "region and zone", "actual": f"region={fd.region or '-'} zone={fd.zone or '-'}", "hint": ""})
        dc = next((d for d in inv["datacenters"] if d["name"] == fd.datacenter), None)
        if not dc:
            res.append({"name": f"{fd.name}: datacenter {fd.datacenter}", "status": "fail", "expected": "exists", "actual": "missing", "hint": ""})
            continue
        cl = next((c for c in dc["clusters"] if c["name"] == fd.cluster), None)
        res.append({"name": f"{fd.name}: cluster {fd.cluster}", "status": "pass" if cl and cl["kind"] == "cluster" else "fail", "expected": "vSphere cluster object",
                    "actual": ("found" if cl["kind"] == "cluster" else "standalone host") if cl else "missing", "hint": "" if cl and cl["kind"] == "cluster" else "IPI needs a cluster object"})
        ds_ok = any(d["name"] == fd.datastore for d in dc["datastores"]) and (not cl or fd.datastore in cl["datastores"])
        res.append({"name": f"{fd.name}: datastore {fd.datastore}", "status": "pass" if ds_ok else "fail", "expected": "exists on the cluster", "actual": "found" if ds_ok else "missing", "hint": ""})
        net_ok = any(n["name"] == fd.network for n in dc["networks"]) and (not cl or fd.network in cl["networks"])
        res.append({"name": f"{fd.name}: network {fd.network}", "status": "pass" if net_ok else "fail", "expected": "exists on the cluster", "actual": "found" if net_ok else "missing", "hint": ""})
    names = {fd.name for fd in spec.vcenter.failure_domains}
    bad = [n.name for n in spec.nodes if n.failure_domain and n.failure_domain not in names]
    if bad:
        res.append({"name": "node failure domains", "status": "fail", "expected": "defined domains", "actual": ", ".join(bad), "hint": "These nodes point at a failure domain that does not exist"})
    return res


@router.post("/vcenter/check")
def vcenter_check(name: str):
    """Infrastructure checks: vCenter for IPI and vSphere agent clusters, otherwise the
    chosen provider (Proxmox, KVM, Redfish, manual). Saved under the 'vcenter' category."""
    s = _store(name)
    spec = s.load()
    if spec.install_method == "agent" and spec.provider != "vsphere":
        from ..services.providers import get_provider
        try:
            res = get_provider(spec, s).check()
        except Exception as ex:
            res = [{"name": "provider", "status": "fail", "expected": "", "actual": str(ex)[-200:], "hint": "Check the Infrastructure step"}]
        s.save_checks("vcenter", res)
        return res
    vc = spec.vcenter
    res: List[Dict] = []
    if not vc.host:
        res.append({"name": "vCenter", "status": "fail", "expected": "configured", "actual": "", "hint": "Fill the vCenter step"})
        s.save_checks("vcenter", res)
        return res
    try:
        cert = vcenter.fetch_cert(vc.host)
        ok = cert["sha1"] == vc.cert_thumbprint
        res.append({"name": "certificate thumbprint", "status": "pass" if ok else "fail", "expected": vc.cert_thumbprint,
                    "actual": cert["sha1"], "hint": "" if ok else "Certificate changed since you accepted it; re-accept in the vCenter step"})
    except Exception as ex:
        res.append({"name": "certificate", "status": "fail", "expected": "", "actual": str(ex), "hint": "vCenter unreachable on 443"})
    try:
        a = vcenter.about(vc)
        res.append({"name": "login", "status": "pass", "expected": "", "actual": f"{a['name']} build {a['build']}", "hint": ""})
    except Exception as ex:
        res.append({"name": "login", "status": "fail", "expected": "", "actual": str(ex)[-200:], "hint": "Check credentials"})
        s.save_checks("vcenter", res)
        return res
    try:
        inv = vcenter.inventory(vc)
        dc = next((d for d in inv["datacenters"] if d["name"] == vc.datacenter), None)
        res.append({"name": f"datacenter {vc.datacenter}", "status": "pass" if dc else "fail", "expected": "exists", "actual": "found" if dc else "missing", "hint": ""})
        if dc:
            cl = next((c for c in dc["clusters"] if c["name"] == vc.cluster), None)
            res.append({"name": f"cluster {vc.cluster}", "status": "pass" if cl else "fail", "expected": "exists", "actual": "found" if cl else "missing", "hint": ""})
            if cl and cl["kind"] == "host":
                ipi = spec.install_method == "ipi"
                res.append({"name": "compute resource type", "status": "fail" if ipi else "warn", "expected": "vSphere cluster object",
                            "actual": "standalone ESXi host",
                            "hint": ("IPI requires a cluster object: in vSphere Client right-click the datacenter > New Cluster (DRS/HA off is fine), "
                                     "move the host into it, then reload the inventory and select the cluster.") if ipi else
                                    "Agent-based installs tolerate a standalone host."})
            ds = next((d for d in dc["datastores"] if d["name"] == vc.datastore), None)
            need = sum(n.disk_gb + sum(n.extra_disks_gb) for n in spec.nodes)
            if ds:
                ok = ds["free_gb"] >= need
                res.append({"name": f"datastore {vc.datastore} free space", "status": "pass" if ok else "warn",
                            "expected": f">= {need} GB (thin)", "actual": f"{ds['free_gb']} GB free", "hint": "" if ok else "Thin provisioning may still work but watch capacity"})
            else:
                res.append({"name": f"datastore {vc.datastore}", "status": "fail", "expected": "exists", "actual": "missing", "hint": ""})
            net = next((n for n in dc["networks"] if n["name"] == vc.network), None)
            res.append({"name": f"network {vc.network}", "status": "pass" if net else "fail", "expected": "exists", "actual": "found" if net else "missing", "hint": ""})
            if cl:
                need_cpu = sum(n.cpus for n in spec.nodes)
                need_mem = sum(n.memory_mb for n in spec.nodes) / 1024
                res.append({"name": "cluster capacity (nominal)", "status": "pass" if cl["cpu_cores"] >= need_cpu / 2 and cl["memory_gb"] >= need_mem * 0.8 else "warn",
                            "expected": f"{need_cpu} vCPU / {need_mem:.0f} GB", "actual": f"{cl['cpu_cores']} cores / {cl['memory_gb']} GB",
                            "hint": "Over-commit is normal in labs; a warning here is not fatal"})
        if spec.install_method == "ipi" and vc.failure_domains:
            res += _fd_rows(spec, inv)
    except Exception as ex:
        res.append({"name": "inventory", "status": "fail", "expected": "", "actual": str(ex)[-200:], "hint": ""})
    if spec.install_method == "ipi" and vc.failure_domains:
        try:
            res += vctags.check(vc, vc.failure_domains)
        except Exception as ex:
            res.append({"name": "region/zone tags", "status": "warn", "expected": "", "actual": str(ex)[-160:], "hint": "Could not query the vSphere tagging API"})
    try:
        for hc in vcenter.host_clocks(vc, vc.cluster):
            off = hc["offset_s"]
            status = "pass" if abs(off) <= 5 else ("warn" if abs(off) <= 60 else "fail")
            res.append({"name": f"ESXi clock {hc['host']}", "status": status, "expected": "within 5 s of installer",
                        "actual": f"{off:+.0f} s", "hint": "" if status == "pass" else
                        "Nodes inherit the host clock at boot and later step it, which can invalidate freshly issued certificates. "
                        "Fix: host > Configure > System > Time Configuration > NTP (your DNS/AD server if it serves NTP, or pool.ntp.org), start the service."})
            res.append({"name": f"ESXi NTP {hc['host']}", "status": "pass" if hc["ntp_servers"] and hc["ntpd_running"] else "warn",
                        "expected": "NTP configured and running",
                        "actual": (", ".join(hc["ntp_servers"]) or "no servers") + (" / running" if hc["ntpd_running"] else " / stopped"),
                        "hint": "" if hc["ntp_servers"] and hc["ntpd_running"] else "Configure NTP on the host so every VM boots with the right time"})
    except Exception as ex:
        res.append({"name": "ESXi clock", "status": "warn", "expected": "", "actual": str(ex)[-160:], "hint": "Could not read host time"})
    try:
        res += vcenter.privileges(vc)
    except Exception as ex:
        res.append({"name": "privileges", "status": "warn", "expected": "", "actual": str(ex)[-200:], "hint": "Privilege enumeration failed; the install may still work with an admin account"})
    # existing VMs with the cluster prefix
    try:
        vms = vcenter.list_vms(vc, prefix=spec.name + "-")
        res.append({"name": "existing VMs with cluster prefix", "status": "pass" if not vms else "warn", "expected": "none",
                    "actual": ", ".join(v["name"] for v in vms) or "none", "hint": "" if not vms else "Leftovers from a previous attempt; destroy first"})
    except Exception:
        pass
    s.save_checks("vcenter", res)
    return res


@router.get("/vcenter/tags")
def vcenter_tags(name: str):
    spec = _store(name).load()
    if not spec.vcenter.failure_domains:
        return []
    try:
        return vctags.check(spec.vcenter, spec.vcenter.failure_domains)
    except Exception as ex:
        raise HTTPException(502, f"tag check failed: {ex}")


@router.post("/vcenter/tags")
def vcenter_tags_ensure(name: str):
    """Create the openshift-region / openshift-zone categories and tags and attach them."""
    spec = _store(name).load()
    if not spec.vcenter.failure_domains:
        raise HTTPException(422, "no failure domains defined")
    try:
        return vctags.ensure(spec.vcenter, spec.vcenter.failure_domains)
    except Exception as ex:
        raise HTTPException(502, f"creating tags failed: {ex}")


# ---- mirror
@router.post("/mirror/check")
def mirror_check(name: str):
    s = _store(name)
    res = mirror.check(s, s.load())
    s.save_checks("mirror", res)
    return res


# ---- preflight
def _proxy_url(spec) -> str:
    return spec.proxy.https_proxy or spec.proxy.http_proxy


@router.post("/preflight")
def preflight(name: str):
    s = _store(name)
    spec = s.load()
    general: List[Dict] = []
    # version / tools
    if spec.ocp_version:
        st = tools.status(spec.ocp_version)
        ok = st["openshift-install"] and st["oc"]
        general.append({"name": f"tools {spec.ocp_version}", "status": "pass" if ok else "warn", "expected": "downloaded",
                        "actual": "present" if ok else "missing", "hint": "" if ok else "Downloaded automatically when you deploy, or press Download now"})
    else:
        general.append({"name": "OpenShift version", "status": "fail", "expected": "selected", "actual": "", "hint": "Pick a version in the Cluster step"})
    # pull secret
    import json
    try:
        ps = json.loads(spec.pull_secret or "{}")
        auths = ps.get("auths", {})
        need = ["quay.io", "registry.redhat.io"]
        missing = [n for n in need if n not in auths]
        general.append({"name": "pull secret", "status": "pass" if not missing else "fail", "expected": ", ".join(need),
                        "actual": ", ".join(auths.keys()) or "empty", "hint": "" if not missing else "Paste the pull secret from console.redhat.com/openshift/install/pull-secret"})
    except Exception:
        general.append({"name": "pull secret", "status": "fail", "expected": "valid JSON", "actual": "invalid", "hint": "Paste the whole JSON document"})
    general.append({"name": "SSH public key", "status": "pass" if spec.ssh_public_key.startswith("ssh-") else "fail",
                    "expected": "ssh-... key", "actual": spec.ssh_public_key[:40] + ("…" if len(spec.ssh_public_key) > 40 else ""), "hint": ""})
    # proxy
    proxy = _proxy_url(spec)
    if proxy:
        u = urlparse(proxy)
        try:
            with socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=5):
                general.append({"name": f"proxy {u.hostname}:{u.port}", "status": "pass", "expected": "reachable", "actual": "open", "hint": ""})
        except Exception as ex:
            general.append({"name": f"proxy {u.hostname}:{u.port}", "status": "fail", "expected": "reachable", "actual": str(ex)[-80:], "hint": "Check the proxy URL in the Proxy & mirror step"})
    # internet (through the proxy when one is set; only a warning in disconnected mode)
    offline = spec.mirror.enabled
    for host in ("quay.io", "registry.redhat.io", "mirror.openshift.com", "api.openshift.com"):
        try:
            r = httpx.head(f"https://{host}/", timeout=8, follow_redirects=False, proxy=proxy or None)
            general.append({"name": f"reach {host}", "status": "pass", "expected": "reachable", "actual": f"HTTP {r.status_code}", "hint": ""})
        except Exception as ex:
            general.append({"name": f"reach {host}", "status": "warn" if offline else "fail", "expected": "reachable", "actual": str(ex)[-80:],
                            "hint": "Expected in a disconnected environment; everything must come from the mirror registry" if offline else "Installer host needs outbound HTTPS" + (" (via the proxy)" if proxy else "")})
    # ntp
    import subprocess
    try:
        out = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=5).stdout
        synced = "Leap status     : Normal" in out
        general.append({"name": "installer time sync", "status": "pass" if synced else "warn", "expected": "synchronised", "actual": "ok" if synced else "not synchronised", "hint": ""})
    except Exception:
        general.append({"name": "installer time sync", "status": "warn", "expected": "chrony", "actual": "unknown", "hint": ""})
    # nodes sanity: topology
    masters = len(spec.nodes_by_role("master"))
    d1w = len(spec.day1_workers())
    pooled = spec.pooled_workers()
    boot = len(spec.nodes_by_role("bootstrap"))
    infra = len(spec.nodes_by_role("infra"))
    if spec.topology == "sno":
        ok, expected = masters == 1 and d1w == 0, "1 master, 0 day-1 workers"
    elif spec.topology == "compact":
        ok, expected = masters == 3 and d1w == 0, "3 masters, 0 day-1 workers"
    else:
        ok, expected = masters == 3 and d1w >= 1, "3 masters + at least 1 worker"
    if spec.install_method == "ipi":
        expected += ", 1 bootstrap"
        ok = ok and boot == 1
    hint = "" if ok else ("Fix the Nodes step" + (" (3 masters without workers is the compact topology)" if spec.topology == "standard" and masters == 3 and d1w == 0 else ""))
    general.append({"name": f"node layout ({spec.topology})", "status": "pass" if ok else "fail", "expected": expected,
                    "actual": f"bootstrap={boot}, master={masters}, worker={d1w}, infra={infra}, pool nodes={len(pooled)}", "hint": hint})
    if pooled:
        undefined = sorted({n.pool for n in pooled if not spec.pool(n.pool)})
        general.append({"name": "node pools", "status": "pass" if not undefined else "fail", "expected": "every member references a defined pool",
                        "actual": ", ".join(f"{p.name}={len(spec.pooled_workers(p.name))}" for p in spec.pools) or "no pools", "hint": "" if not undefined else f"Undefined pools: {', '.join(undefined)}"})
    if spec.lb.mode == "none":
        general.append({"name": "load balancer mode", "status": "pass" if masters == 1 else "fail", "expected": "'none' only for single node",
                        "actual": f"{masters} master(s)", "hint": "" if masters == 1 else "Choose HAProxy or an external load balancer"})
    if spec.install_method == "ipi" and spec.vcenter.failure_domains:
        names = {fd.name for fd in spec.vcenter.failure_domains}
        bad = [n.name for n in spec.nodes if n.failure_domain and n.failure_domain not in names]
        general.append({"name": "failure domains", "status": "pass" if not bad else "fail", "expected": f"{len(names)} domain(s): {', '.join(sorted(names))}",
                        "actual": "ok" if not bad else ", ".join(bad), "hint": "" if not bad else "Nodes reference undefined failure domains"})
    ips = [n.ip for n in spec.nodes]
    dup = {i for i in ips if ips.count(i) > 1}
    general.append({"name": "unique node IPs", "status": "pass" if not dup else "fail", "expected": "unique", "actual": ", ".join(dup) or "ok", "hint": ""})
    import ipaddress
    if spec.network.machine_cidr:
        net = ipaddress.ip_network(spec.network.machine_cidr, strict=False)
        outside = [n.name for n in spec.nodes if ipaddress.ip_address(n.ip) not in net]
        general.append({"name": "node IPs inside machine CIDR", "status": "pass" if not outside else "fail", "expected": spec.network.machine_cidr, "actual": ", ".join(outside) or "ok", "hint": ""})
        gw_ok = bool(spec.network.gateway) and ipaddress.ip_address(spec.network.gateway) in net
        general.append({"name": "gateway", "status": "pass" if gw_ok else "fail", "expected": f"inside {net}", "actual": spec.network.gateway or "unset", "hint": ""})
    else:
        general.append({"name": "machine CIDR", "status": "fail", "expected": "set", "actual": "", "hint": "Fill the Network step"})
    # node IPs must not answer already
    alive = []
    for n in spec.nodes:
        try:
            r = subprocess.run(["ping", "-c1", "-W1", n.ip], capture_output=True, timeout=3)
            if r.returncode == 0:
                alive.append(f"{n.name}({n.ip})")
        except Exception:
            pass
    general.append({"name": "node IPs currently unused", "status": "pass" if not alive else "warn", "expected": "no reply",
                    "actual": ", ".join(alive) or "all silent", "hint": "" if not alive else "Something already answers on these IPs"})
    s.save_checks("general", general)
    out = {"general": general, "dns": dns_check(name), "lb": lb_probe(name), "vcenter": vcenter_check(name)}
    if spec.mirror.enabled:
        out["mirror"] = mirror_check(name)
    else:
        s.save_checks("mirror", [])
    return out


@router.get("/checks")
def get_checks(name: str):
    s = _store(name)
    out: Dict[str, List] = {}
    for r in s.get_checks():
        out.setdefault(r["category"], []).append(r)
    return out
