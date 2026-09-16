"""Manual provider: the app builds the ISO and serves it; you boot the machines yourself
(any hypervisor, USB stick, PXE-less bare metal)."""
from pathlib import Path
from typing import Dict, List, Optional

from ...models import NodeSpec
from .base import Provider, iso_url


class ManualProvider(Provider):
    name = "manual"
    title = "Manual (boot the ISO yourself)"

    def check(self) -> List[Dict]:
        missing = [n.name for n in self.spec.nodes if n.role != "bootstrap" and not n.mac]
        return [{"name": "node MAC addresses", "status": "pass" if not missing else "warn", "expected": "one per node",
                 "actual": "all set" if not missing else "missing: " + ", ".join(missing),
                 "hint": "" if not missing else "MACs are generated on deploy; set the same MAC on each VM you create, or fill in the real NIC MAC for physical machines"}]

    def upload_iso(self, iso_local: Path, log) -> str:
        url = iso_url(self.store, iso_local.name)
        log(f"ISO ready: {iso_local} — download it from {url}")
        return url

    def create_node(self, node: NodeSpec, iso_ref: str, log, extra_disks: Optional[List[int]] = None):
        log(f"  boot {node.name} from the ISO: {node.cpus} vCPU, {node.memory_mb} MB, {node.disk_gb} GB disk, NIC MAC {node.mac}, IP {node.ip}")

    def power_on(self, node: NodeSpec, log):
        pass

    def power_off(self, node: NodeSpec, log):
        log(f"{node.name}: power it off yourself (manual provider)")

    def destroy(self, node: NodeSpec, log):
        log(f"{node.name}: delete the machine yourself (manual provider)")

    def instructions(self, nodes: List[NodeSpec], iso_ref: str) -> str:
        lines = [f"Boot every machine from {iso_ref} (all of them, in any order). Each must use the MAC below so it picks up its host entry:"]
        for n in nodes:
            lines.append(f"  {n.name:14} MAC {n.mac}  IP {n.ip}  {n.cpus} vCPU / {n.memory_mb} MB / {n.disk_gb} GB")
        lines.append("The install starts on its own once all hosts have booted the agent; leave the ISO attached until install-complete.")
        return "\n".join(lines)
