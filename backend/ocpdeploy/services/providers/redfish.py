"""Bare metal through Redfish: virtual media boot of the agent ISO served by this app."""
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from ...models import NodeSpec
from .base import Provider, iso_url


class _BMC:
    def __init__(self, base: str, user: str, password: str, verify: bool, system_id: str = ""):
        if not base.startswith("http"):
            base = "https://" + base
        self.c = httpx.Client(base_url=base.rstrip("/"), auth=(user, password), verify=verify, timeout=30, headers={"Accept": "application/json"})
        self.system_id = system_id

    def _j(self, r: httpx.Response):
        if r.status_code >= 400:
            raise RuntimeError(f"Redfish {r.request.method} {r.request.url.path}: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {}

    def get(self, path: str):
        return self._j(self.c.get(path))

    def system(self) -> Dict:
        members = self.get("/redfish/v1/Systems").get("Members", [])
        if not members:
            raise RuntimeError("BMC reports no Systems")
        if self.system_id:
            path = next((m["@odata.id"] for m in members if m["@odata.id"].rstrip("/").endswith("/" + self.system_id)), None)
            if not path:
                raise RuntimeError(f"system {self.system_id} not found; available: {', '.join(m['@odata.id'].split('/')[-1] for m in members)}")
        else:
            path = members[0]["@odata.id"]
        return self.get(path)

    def virtual_media(self) -> Dict:
        """The first CD/DVD virtual media slot of the first manager (Dell, HPE, Supermicro, Lenovo)."""
        sysd = self.system()
        mgr_paths = [m["@odata.id"] for m in (sysd.get("Links") or {}).get("ManagedBy", [])] or [m["@odata.id"] for m in self.get("/redfish/v1/Managers").get("Members", [])]
        for mp in mgr_paths:
            mgr = self.get(mp)
            vm_path = (mgr.get("VirtualMedia") or {}).get("@odata.id")
            if not vm_path:
                continue
            for member in self.get(vm_path).get("Members", []):
                vm = self.get(member["@odata.id"])
                types = [t.upper() for t in vm.get("MediaTypes", [])]
                if any(t in ("CD", "DVD") for t in types):
                    return vm
        raise RuntimeError("no CD/DVD virtual media slot found on the BMC")

    def insert(self, image_url: str):
        vm = self.virtual_media()
        actions = vm.get("Actions", {})
        if vm.get("Inserted"):
            self.eject()
        target = (actions.get("#VirtualMedia.InsertMedia") or {}).get("target")
        if not target:
            raise RuntimeError("virtual media slot has no InsertMedia action")
        self._j(self.c.post(target, json={"Image": image_url, "Inserted": True, "WriteProtected": True}))

    def eject(self):
        vm = self.virtual_media()
        target = (vm.get("Actions", {}).get("#VirtualMedia.EjectMedia") or {}).get("target")
        if target and vm.get("Inserted", True):
            self._j(self.c.post(target, json={}))

    def boot_once_cd(self):
        sysd = self.system()
        boot = {"BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Once"}
        if "BootSourceOverrideMode" in (sysd.get("Boot") or {}):
            boot["BootSourceOverrideMode"] = "UEFI"
        self._j(self.c.patch(sysd["@odata.id"], json={"Boot": boot}))

    def power(self, reset_type: str):
        sysd = self.system()
        target = (sysd.get("Actions", {}).get("#ComputerSystem.Reset") or {}).get("target")
        if not target:
            raise RuntimeError("system has no Reset action")
        self._j(self.c.post(target, json={"ResetType": reset_type}))

    def power_state(self) -> str:
        return self.system().get("PowerState", "Unknown")

    def first_mac(self) -> str:
        sysd = self.system()
        eth = (sysd.get("EthernetInterfaces") or {}).get("@odata.id")
        if not eth:
            return ""
        for m in self.get(eth).get("Members", []):
            i = self.get(m["@odata.id"])
            mac = i.get("MACAddress") or i.get("PermanentMACAddress")
            if mac and (i.get("LinkStatus") in (None, "LinkUp") or i.get("Status", {}).get("State") == "Enabled"):
                return mac.lower()
        return ""


class RedfishProvider(Provider):
    name = "redfish"
    title = "Bare metal (Redfish virtual media)"

    def _bmc(self, node: NodeSpec) -> _BMC:
        r = self.spec.redfish
        if not node.bmc_address:
            raise RuntimeError(f"node {node.name} has no BMC address")
        return _BMC(node.bmc_address, node.bmc_username or r.username, node.bmc_password or r.password, bool(r.verify_tls), node.bmc_system_id)

    def _targets(self) -> List[NodeSpec]:
        return [n for n in self.spec.nodes if n.role != "bootstrap"]

    def check(self) -> List[Dict]:
        rows: List[Dict] = []
        for n in self._targets():
            if not n.bmc_address:
                rows.append({"name": f"{n.name} BMC", "status": "fail", "expected": "address", "actual": "missing", "hint": "Set the BMC address in the Nodes step"})
                continue
            try:
                b = self._bmc(n)
                sysd = b.system()
                b.virtual_media()
                rows.append({"name": f"{n.name} BMC {n.bmc_address}", "status": "pass", "expected": "Redfish + virtual media",
                             "actual": f"{sysd.get('Manufacturer', '')} {sysd.get('Model', '')} power {sysd.get('PowerState', '?')}", "hint": ""})
                if not n.mac:
                    mac = b.first_mac()
                    rows.append({"name": f"{n.name} MAC", "status": "pass" if mac else "warn", "expected": "from the BMC", "actual": mac or "not reported",
                                 "hint": "" if mac else "Fill the MAC of the NIC on the machine network in the Nodes step"})
            except Exception as ex:
                rows.append({"name": f"{n.name} BMC {n.bmc_address}", "status": "fail", "expected": "Redfish + virtual media", "actual": str(ex)[-160:], "hint": "Check address and credentials; virtual media needs an iDRAC Enterprise / iLO Advanced style licence on some vendors"})
        base = self.spec.redfish.iso_url_base or iso_url(self.store, "agent.x86_64.iso", (self._targets()[0].bmc_address if self._targets() else "")).rsplit("/api/", 1)[0]
        rows.append({"name": "ISO URL for the BMCs", "status": "pass", "expected": "reachable from the BMC network", "actual": base, "hint": "Set OCPDEPLOY_ADVERTISE_URL or the ISO URL base if the BMCs must use another address"})
        return rows

    def prepare_nodes(self, log):
        """Fill missing MACs from the BMC (the first enabled NIC) instead of inventing them."""
        changed = {}
        for n in self._targets():
            if not n.mac:
                mac = self._bmc(n).first_mac()
                if not mac:
                    raise RuntimeError(f"node {n.name}: no MAC known; set the NIC MAC in the Nodes step")
                n.mac = mac
                changed[n.name] = mac
                log(f"{n.name}: MAC {mac} from the BMC")
        if changed:
            def _p(raw):
                for x in raw["nodes"]:
                    if x["name"] in changed:
                        x["mac"] = changed[x["name"]]
            self.store.patch(_p)
        return self.spec

    def upload_iso(self, iso_local: Path, log) -> str:
        r = self.spec.redfish
        url = (r.iso_url_base.rstrip("/") + f"/api/clusters/{self.store.name}/iso/{iso_local.name}") if r.iso_url_base else iso_url(self.store, iso_local.name, self._targets()[0].bmc_address if self._targets() else "")
        log(f"ISO served at {url} (the BMCs fetch it from this app)")
        return url

    def create_node(self, node: NodeSpec, iso_ref: str, log, extra_disks: Optional[List[int]] = None):
        b = self._bmc(node)
        log(f"{node.name}: inserting virtual media {iso_ref}")
        b.insert(iso_ref)
        time.sleep(3)
        b.boot_once_cd()
        log(f"{node.name}: boot-once from CD set")

    def exists(self, node: NodeSpec) -> Optional[Dict]:
        try:
            return {"name": node.name, "power": "poweredOn" if self._bmc(node).power_state() == "On" else "poweredOff"}
        except Exception:
            return None

    def power_on(self, node: NodeSpec, log):
        b = self._bmc(node)
        st = b.power_state()
        log(f"{node.name}: power {st} -> {'ForceRestart' if st == 'On' else 'On'}")
        b.power("ForceRestart" if st == "On" else "On")

    def power_off(self, node: NodeSpec, log):
        b = self._bmc(node)
        if b.power_state() == "On":
            log(f"{node.name}: forcing power off")
            b.power("ForceOff")

    def shutdown_guest(self, node: NodeSpec, log):
        b = self._bmc(node)
        if b.power_state() == "On":
            log(f"{node.name}: graceful shutdown")
            b.power("GracefulShutdown")

    def power_state(self, node: NodeSpec) -> str:
        try:
            return "poweredOn" if self._bmc(node).power_state() == "On" else "poweredOff"
        except Exception:
            return "unknown"

    def eject(self, node: NodeSpec, log):
        log(f"{node.name}: ejecting virtual media")
        self._bmc(node).eject()

    def destroy(self, node: NodeSpec, log):
        """Bare metal cannot be deleted: power off and eject so the machine is free."""
        self.power_off(node, log)
        try:
            self.eject(node, log)
        except Exception as ex:
            log(f"{node.name}: eject failed: {str(ex)[-80:]}")
