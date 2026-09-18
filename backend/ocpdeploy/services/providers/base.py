"""Where agent-based nodes run. A provider knows how to put the agent ISO somewhere
the machines can boot from, create/power/eject/destroy them, and validate its settings.
IPI installs always use vSphere through the installer and never touch this layer."""
import socket
from pathlib import Path
from typing import Dict, List, Optional

from ...models import ClusterSpec, NodeSpec
from ...settings import ADVERTISE_URL, LISTEN_PORT


def vm_name(spec: ClusterSpec, node: NodeSpec) -> str:
    return f"{spec.name}-{node.name}"


def iso_url(store, filename: str, target_host: str = "") -> str:
    """URL under which the app serves an ISO from the cluster's install (or add-nodes) dir."""
    base = ADVERTISE_URL.rstrip("/")
    if not base:
        ip = "127.0.0.1"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target_host or "8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            try:
                ip = socket.gethostbyname(socket.getfqdn())
            except Exception:
                pass
        base = f"http://{ip}:{LISTEN_PORT}"
    return f"{base}/api/iso/{store.name}/{iso_token(store)}/{filename}"


def iso_token(store) -> str:
    """Per-cluster secret that authorises ISO downloads by BMCs (kept in state.sqlite)."""
    import secrets
    t = store.kv_get("iso_token")
    if not t:
        t = secrets.token_urlsafe(24)
        store.kv_set("iso_token", t)
    return t


class Provider:
    name = "base"
    title = "Provider"

    def __init__(self, spec: ClusterSpec, store):
        self.spec = spec
        self.store = store

    # --- validation -----------------------------------------------------
    def check(self) -> List[Dict]:
        return []

    # --- nodes -----------------------------------------------------------
    def prepare_nodes(self, log) -> ClusterSpec:
        """Make sure every node has what this provider needs (a MAC for VM providers)."""
        from ..deploy import ensure_macs
        return ensure_macs(self.store, self.spec, log)

    def upload_iso(self, iso_local: Path, log) -> str:
        raise NotImplementedError

    def create_node(self, node: NodeSpec, iso_ref: str, log, extra_disks: Optional[List[int]] = None):
        raise NotImplementedError

    def exists(self, node: NodeSpec) -> Optional[Dict]:
        return None

    def power_on(self, node: NodeSpec, log):
        raise NotImplementedError

    def power_off(self, node: NodeSpec, log):
        raise NotImplementedError

    def shutdown_guest(self, node: NodeSpec, log):
        self.power_off(node, log)

    def power_state(self, node: NodeSpec) -> str:
        """poweredOn | poweredOff | missing | unknown"""
        return "unknown"

    def eject(self, node: NodeSpec, log):
        pass

    def destroy(self, node: NodeSpec, log):
        raise NotImplementedError

    def instructions(self, nodes: List[NodeSpec], iso_ref: str) -> str:
        return ""
