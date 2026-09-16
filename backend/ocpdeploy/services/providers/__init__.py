from typing import Dict, Type

from ...models import ClusterSpec
from .base import Provider, vm_name, iso_url
from .vsphere import VSphereProvider
from .manual import ManualProvider
from .proxmox import ProxmoxProvider
from .libvirt import LibvirtProvider
from .redfish import RedfishProvider

PROVIDERS: Dict[str, Type[Provider]] = {
    "vsphere": VSphereProvider, "proxmox": ProxmoxProvider, "libvirt": LibvirtProvider, "redfish": RedfishProvider, "manual": ManualProvider,
}


def get_provider(spec: ClusterSpec, store) -> Provider:
    """IPI is always vSphere (the installer drives it); the agent path honours spec.provider."""
    name = spec.provider if spec.install_method == "agent" else "vsphere"
    return PROVIDERS.get(name, VSphereProvider)(spec, store)
