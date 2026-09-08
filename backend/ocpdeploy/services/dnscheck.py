"""DNS validation for a cluster spec. Produces a checklist the UI renders as
required/recommended records with pass/fail and hints."""
import random
import string
from typing import Dict, List

import dns.resolver
import dns.reversename
import dns.exception

from ..models import ClusterSpec


def _resolver(servers: List[str]) -> dns.resolver.Resolver:
    r = dns.resolver.Resolver(configure=not servers)
    if servers:
        r.nameservers = servers
    r.lifetime = 4
    r.timeout = 2
    return r


def _a(res, name: str) -> List[str]:
    try:
        return sorted(str(x) for x in res.resolve(name, "A"))
    except dns.exception.DNSException:
        return []


def _ptr(res, ip: str) -> List[str]:
    try:
        return sorted(str(x).rstrip(".") for x in res.resolve(dns.reversename.from_address(ip), "PTR"))
    except dns.exception.DNSException:
        return []


def expected_records(spec: ClusterSpec) -> List[Dict]:
    """The hint list: what must exist in DNS for this cluster."""
    dom = spec.domain
    recs = [
        {"name": f"api.{dom}", "type": "A", "value": spec.api_ip, "level": "required",
         "why": "Kubernetes API endpoint used by clients and the installer"},
        {"name": f"api-int.{dom}", "type": "A", "value": spec.api_ip, "level": "required",
         "why": "Internal API endpoint used by nodes (machine config server on 22623)"},
        {"name": f"*.apps.{dom}", "type": "A", "value": spec.apps_ip, "level": "required",
         "why": "Wildcard for the ingress routers (console, oauth, every Route)"},
    ]
    node_level = "required" if spec.install_method == "agent" else "optional"
    node_why = ("Agent-based installs use DNS names as node hostnames" if spec.install_method == "agent"
                else "IPI names nodes itself; A records are only for your convenience")
    for n in spec.nodes:
        recs.append({"name": f"{n.name}.{dom}", "type": "A", "value": n.ip, "level": node_level, "why": node_why})
    for n in spec.nodes:
        recs.append({"name": n.ip, "type": "PTR", "value": f"{n.name}.{dom}", "level": "recommended",
                     "why": "RHCOS derives its hostname from reverse DNS when nothing else sets it; stale PTRs from other domains cause wrong hostnames"})
    if spec.lb.mode == "haproxy" and spec.lb.layout == "ha":
        recs.append({"name": f"{spec.lb.ha.api_vip}", "type": "note", "value": "keepalived VIP",
                     "level": "info", "why": "api/api-int must point at the API VIP, *.apps at the apps VIP"})
    return recs


def run(spec: ClusterSpec) -> List[Dict]:
    res = _resolver(spec.network.dns_servers)
    dom = spec.domain
    out: List[Dict] = []

    def add(name, status, expected, actual, hint="", level="required"):
        out.append({"name": name, "status": status, "expected": expected, "actual": actual, "hint": hint, "level": level})

    # API records
    for host, ip in ((f"api.{dom}", spec.api_ip), (f"api-int.{dom}", spec.api_ip)):
        got = _a(res, host)
        if not ip:
            add(host, "warn", "", ", ".join(got), "Load balancer IP not set yet")
        elif got == [ip]:
            add(host, "pass", ip, ip)
        elif ip in got:
            add(host, "warn", ip, ", ".join(got), "Extra A records present; keep exactly one")
        else:
            add(host, "fail", ip, ", ".join(got) or "NXDOMAIN", f"Create A record {host} -> {ip}")

    # wildcard: random label + the two names the installer really needs
    rnd = "".join(random.choices(string.ascii_lowercase, k=8))
    for host in (f"{rnd}.apps.{dom}", f"console-openshift-console.apps.{dom}", f"oauth-openshift.apps.{dom}"):
        got = _a(res, host)
        ip = spec.apps_ip
        if not ip:
            add(host, "warn", "", ", ".join(got), "Apps load balancer IP not set yet")
        elif got == [ip]:
            add(host, "pass", ip, ip, "" if rnd not in host else "wildcard *.apps works")
        else:
            add(host, "fail", ip, ", ".join(got) or "NXDOMAIN", f"Create wildcard A record *.apps.{dom} -> {ip}")

    # nodes
    node_level = "required" if spec.install_method == "agent" else "optional"
    for n in spec.nodes:
        host = f"{n.name}.{dom}"
        got = _a(res, host)
        if got == [n.ip]:
            add(host, "pass", n.ip, n.ip, level=node_level)
        elif not got:
            add(host, "fail" if node_level == "required" else "warn", n.ip, "NXDOMAIN", f"Create A record {host} -> {n.ip}", node_level)
        else:
            add(host, "fail", n.ip, ", ".join(got), f"A record points elsewhere; fix to {n.ip}", node_level)

    # reverse
    for n in spec.nodes:
        host = f"{n.name}.{dom}"
        ptrs = _ptr(res, n.ip)
        foreign = [p for p in ptrs if p != host]
        if ptrs == [host]:
            add(f"PTR {n.ip}", "pass", host, host, level="recommended")
        elif host in ptrs and foreign:
            add(f"PTR {n.ip}", "warn", host, ", ".join(ptrs),
                f"Stale PTR(s) also present: {', '.join(foreign)}. Delete them so the node cannot pick a foreign hostname.", "recommended")
        elif not ptrs:
            add(f"PTR {n.ip}", "warn", host, "none", f"Create PTR {n.ip} -> {host} (recommended)", "recommended")
        else:
            add(f"PTR {n.ip}", "fail", host, ", ".join(ptrs), f"PTR points to a different name; change to {host}", "recommended")

    # LB reverse sanity (only informational)
    for label, ip in (("api LB", spec.api_ip), ("apps LB", spec.apps_ip)):
        if ip:
            ptrs = _ptr(res, ip)
            add(f"PTR {label} {ip}", "pass" if ptrs else "warn", "", ", ".join(ptrs) or "none",
                "" if ptrs else "No reverse record; harmless", "info")

    # resolver itself
    add("DNS servers", "pass" if spec.network.dns_servers else "warn",
        ", ".join(spec.network.dns_servers), ", ".join(res.nameservers),
        "" if spec.network.dns_servers else "No DNS servers set in the network step; used installer's resolver", "info")
    return out
