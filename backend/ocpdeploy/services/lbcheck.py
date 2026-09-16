"""Reachability probes for the API / apps load balancers, whatever runs them."""
import socket
from typing import Dict, List

from ..models import ClusterSpec

API_PORTS = (6443, 22623)
APPS_PORTS = (80, 443)


def _open(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def backends(spec: ClusterSpec) -> Dict[str, List[Dict]]:
    """What each LB pool must contain, used both for rendering and as an
    operator hint in external mode."""
    masters = spec.nodes_by_role("master")
    boot = spec.nodes_by_role("bootstrap")
    api_pool = ([] if spec.lb.bootstrap_removed else boot) + masters
    # ingress: until infra nodes carry the routers, workers serve apps
    infra = spec.nodes_by_role("infra")
    ingress_pools = {p.name for p in spec.pools if p.serve_ingress}
    workers = spec.day1_workers() + [n for n in spec.pooled_workers() if n.pool in ingress_pools]
    if spec.lb.ingress_on == "infra" and infra:
        apps_pool = infra
    elif spec.lb.ingress_on == "workers":
        apps_pool = workers
    else:
        apps_pool = workers + infra
    if not apps_pool:
        apps_pool = masters   # compact / single-node: the routers run on the control plane
    return {
        "api": [{"name": n.name, "ip": n.ip, "ports": list(API_PORTS)} for n in api_pool],
        "apps": [{"name": n.name, "ip": n.ip, "ports": list(APPS_PORTS)} for n in apps_pool],
    }


def probe(spec: ClusterSpec) -> List[Dict]:
    out = []
    if spec.lb.mode == "none":
        m = spec.nodes_by_role("master")
        out.append({"name": "load balancer", "status": "pass" if len(m) == 1 else "fail",
                    "expected": "single node, DNS points at the node", "actual": f"{len(m)} master(s)",
                    "hint": "" if len(m) == 1 else "'No load balancer' is only valid for a single-node cluster"})
        return out
    for label, ip, ports in (("API LB", spec.api_ip, API_PORTS), ("Apps LB", spec.apps_ip, APPS_PORTS)):
        if not ip:
            out.append({"name": label, "status": "fail", "expected": "IP set", "actual": "", "hint": "Configure the load balancer step"})
            continue
        for p in ports:
            ok = _open(ip, p)
            out.append({"name": f"{label} {ip}:{p}", "status": "pass" if ok else "fail",
                        "expected": "listening", "actual": "open" if ok else "closed/filtered",
                        "hint": "" if ok else ("Push the HAProxy configuration from the Load balancer step" if spec.lb.mode == "haproxy"
                                               else "Ask the LB owner to open this frontend")})
    if spec.lb.mode == "haproxy":
        for vm in spec.lb.vms:
            ok = _open(vm.host, vm.ssh_port)
            out.append({"name": f"SSH {vm.host}:{vm.ssh_port}", "status": "pass" if ok else "fail",
                        "expected": "open", "actual": "open" if ok else "closed", "hint": "" if ok else "HAProxy VM unreachable"})
            st = _open(vm.ip or vm.host, spec.lb.stats_port)
            out.append({"name": f"HAProxy stats {vm.host}:{spec.lb.stats_port}", "status": "pass" if st else "warn",
                        "expected": "open", "actual": "open" if st else "closed", "hint": "" if st else "Stats page appears after the first push"})
    return out
