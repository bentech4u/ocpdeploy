"""vSphere provider: thin wrapper over services.vcenter (behaviour unchanged)."""
from pathlib import Path
from typing import Dict, List, Optional

from ...models import NodeSpec
from .. import vcenter
from .base import Provider, vm_name


class VSphereProvider(Provider):
    name = "vsphere"
    title = "VMware vSphere"

    def upload_iso(self, iso_local: Path, log) -> str:
        return vcenter.upload_to_datastore(self.spec.vcenter, iso_local, f"ocpdeploy/{self.spec.name}/{iso_local.name}", log)

    def create_node(self, node: NodeSpec, iso_ref: str, log, extra_disks: Optional[List[int]] = None):
        vcenter.create_vm(self.spec.vcenter, vm_name(self.spec, node), node.cpus, node.memory_mb, node.disk_gb, node.mac, iso_ref, log,
                          extra_disks_gb=extra_disks or [])

    def exists(self, node: NodeSpec) -> Optional[Dict]:
        return vcenter.find_vm(self.spec.vcenter, vm_name(self.spec, node))

    def power_on(self, node: NodeSpec, log):
        vcenter.power(self.spec.vcenter, vm_name(self.spec, node), "on", log)

    def power_off(self, node: NodeSpec, log):
        vcenter.power(self.spec.vcenter, vm_name(self.spec, node), "off", log)

    def shutdown_guest(self, node: NodeSpec, log):
        vcenter.shutdown_guest(self.spec.vcenter, vm_name(self.spec, node), log)

    def power_state(self, node: NodeSpec) -> str:
        return vcenter.power_states(self.spec.vcenter, [vm_name(self.spec, node)]).get(vm_name(self.spec, node), "missing")

    def eject(self, node: NodeSpec, log):
        vcenter.eject_cdrom(self.spec.vcenter, vm_name(self.spec, node), log)

    def destroy(self, node: NodeSpec, log):
        vcenter.destroy_vm(self.spec.vcenter, vm_name(self.spec, node), log)
