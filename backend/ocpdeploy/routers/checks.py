"""Per-cluster validation endpoints: DNS, load balancer, vCenter, preflight aggregate."""
import socket
from typing import Dict, List

import httpx
from fastapi import APIRouter, HTTPException

from ..jobs import start
from ..services import dnscheck, lbcheck, haproxy, vcenter, tools, versions
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


@router.post("/vcenter/check")
def vcenter_check(name: str):
    s = _store(name)
    spec = s.load()
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
            need = sum(n.disk_gb for n in spec.nodes)
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
    except Exception as ex:
        res.append({"name": "inventory", "status": "fail", "expected": "", "actual": str(ex)[-200:], "hint": ""})
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
    # internet
    for host in ("quay.io", "registry.redhat.io", "mirror.openshift.com", "api.openshift.com"):
        try:
            r = httpx.head(f"https://{host}/", timeout=8, follow_redirects=False)
            general.append({"name": f"reach {host}", "status": "pass", "expected": "reachable", "actual": f"HTTP {r.status_code}", "hint": ""})
        except Exception as ex:
            general.append({"name": f"reach {host}", "status": "fail", "expected": "reachable", "actual": str(ex)[-80:], "hint": "Installer host needs outbound HTTPS"})
    # ntp
    import subprocess
    try:
        out = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=5).stdout
        synced = "Leap status     : Normal" in out
        general.append({"name": "installer time sync", "status": "pass" if synced else "warn", "expected": "synchronised", "actual": "ok" if synced else "not synchronised", "hint": ""})
    except Exception:
        general.append({"name": "installer time sync", "status": "warn", "expected": "chrony", "actual": "unknown", "hint": ""})
    # nodes sanity
    roles = {r: len(spec.nodes_by_role(r)) for r in ("bootstrap", "master", "worker", "infra")}
    ok = roles["master"] in (1, 3) and (spec.install_method == "agent" or roles["bootstrap"] == 1)
    general.append({"name": "node layout", "status": "pass" if ok else "fail",
                    "expected": "3 masters (+1 bootstrap for IPI)", "actual": ", ".join(f"{k}={v}" for k, v in roles.items()),
                    "hint": "" if ok else "Fix the Nodes step"})
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
    dns = dns_check(name)
    lb = lb_probe(name)
    vc = vcenter_check(name)
    return {"general": general, "dns": dns, "lb": lb, "vcenter": vc}


@router.get("/checks")
def get_checks(name: str):
    s = _store(name)
    out: Dict[str, List] = {}
    for r in s.get_checks():
        out.setdefault(r["category"], []).append(r)
    return out
