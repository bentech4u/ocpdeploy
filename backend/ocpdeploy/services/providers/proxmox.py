"""Proxmox VE provider through the REST API with an API token."""
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from ...models import NodeSpec
from .base import Provider, vm_name


class ProxmoxProvider(Provider):
    name = "proxmox"
    title = "Proxmox VE"

    def __init__(self, spec, store):
        super().__init__(spec, store)
        p = spec.proxmox
        self.p = p
        self.c = httpx.Client(base_url=f"https://{p.host}:{p.port}/api2/json", verify=bool(p.verify_tls), timeout=60,
                              headers={"Authorization": f"PVEAPIToken={p.token_id}={p.token_secret}"})

    def _j(self, r: httpx.Response):
        if r.status_code >= 400:
            raise RuntimeError(f"Proxmox {r.request.method} {r.request.url.path}: HTTP {r.status_code} {r.text[:200]}")
        return (r.json() or {}).get("data")

    def _wait_task(self, upid: str, log, timeout: int = 1800):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self._j(self.c.get(f"/nodes/{self.p.node}/tasks/{upid}/status"))
            if st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise RuntimeError(f"Proxmox task failed: {st.get('exitstatus')}")
                return
            time.sleep(2)
        raise RuntimeError("Proxmox task timed out")

    def _find(self, name: str) -> Optional[Dict]:
        for r in self._j(self.c.get("/cluster/resources", params={"type": "vm"})) or []:
            if r.get("name") == name:
                return r
        return None

    # --- validation
    def check(self) -> List[Dict]:
        rows: List[Dict] = []
        p = self.p
        if not (p.host and p.token_id and p.token_secret and p.node):
            return [{"name": "Proxmox settings", "status": "fail", "expected": "host, token, node", "actual": "incomplete", "hint": "Fill the Infrastructure step"}]
        try:
            v = self._j(self.c.get("/version"))
            rows.append({"name": "Proxmox API", "status": "pass", "expected": "", "actual": f"PVE {v.get('version')}", "hint": ""})
        except Exception as ex:
            return [{"name": "Proxmox API", "status": "fail", "expected": "reachable with the token", "actual": str(ex)[-160:], "hint": "Check host, port, token id and secret (Datacenter > Permissions > API Tokens; privilege separation off or grant VM.* + Datastore.*)"}]
        try:
            nodes = [n["node"] for n in self._j(self.c.get("/nodes")) or []]
            ok = p.node in nodes
            rows.append({"name": f"node {p.node}", "status": "pass" if ok else "fail", "expected": "exists", "actual": "found" if ok else f"available: {', '.join(nodes)}", "hint": ""})
            if ok:
                stor = {s["storage"]: s for s in self._j(self.c.get(f"/nodes/{p.node}/storage")) or []}
                for label, name, ctype in (("disk storage", p.storage, "images"), ("ISO storage", p.iso_storage, "iso")):
                    s = stor.get(name)
                    good = bool(s) and ctype in (s.get("content") or "")
                    rows.append({"name": f"{label} {name}", "status": "pass" if good else "fail", "expected": f"content type {ctype}",
                                 "actual": (f"{round(s.get('avail', 0) / 2**30)} GB free, content {s.get('content')}" if s else "missing"), "hint": "" if good else f"Pick a storage that allows '{ctype}' content"})
                nets = [n["iface"] for n in self._j(self.c.get(f"/nodes/{p.node}/network")) or [] if n.get("type") == "bridge"]
                okb = p.bridge in nets
                rows.append({"name": f"bridge {p.bridge}", "status": "pass" if okb else "fail", "expected": "exists", "actual": "found" if okb else f"bridges: {', '.join(nets)}", "hint": ""})
        except Exception as ex:
            rows.append({"name": "inventory", "status": "fail", "expected": "", "actual": str(ex)[-160:], "hint": ""})
        existing = [n.name for n in self.spec.nodes if self._find(vm_name(self.spec, n))]
        rows.append({"name": "existing VMs with cluster names", "status": "pass" if not existing else "warn", "expected": "none", "actual": ", ".join(existing) or "none", "hint": "" if not existing else "Leftovers from a previous attempt; destroy first"})
        return rows

    # --- nodes
    def upload_iso(self, iso_local: Path, log) -> str:
        fname = f"{self.spec.name}-{iso_local.name}"
        content = self._j(self.c.get(f"/nodes/{self.p.node}/storage/{self.p.iso_storage}/content", params={"content": "iso"})) or []
        volid = f"{self.p.iso_storage}:iso/{fname}"
        if any(c.get("volid") == volid for c in content):
            log(f"ISO {fname} already on {self.p.iso_storage}; replacing")
            try:
                self._j(self.c.delete(f"/nodes/{self.p.node}/storage/{self.p.iso_storage}/content/{volid}"))
            except Exception:
                pass
        log(f"Uploading {iso_local.name} ({iso_local.stat().st_size // 2**20} MiB) to {self.p.iso_storage} on {self.p.node}")
        with open(iso_local, "rb") as f:
            r = self.c.post(f"/nodes/{self.p.node}/storage/{self.p.iso_storage}/upload", data={"content": "iso"}, files={"filename": (fname, f, "application/octet-stream")}, timeout=None)
        upid = self._j(r)
        if isinstance(upid, str) and upid.startswith("UPID"):
            self._wait_task(upid, log)
        log("Upload complete")
        return volid

    def create_node(self, node: NodeSpec, iso_ref: str, log, extra_disks: Optional[List[int]] = None):
        p = self.p
        vmid = self._j(self.c.get("/cluster/nextid"))
        cfg = {
            "vmid": vmid, "name": vm_name(self.spec, node), "cores": node.cpus, "sockets": 1, "cpu": "host", "memory": node.memory_mb,
            "machine": "q35", "bios": "ovmf", "ostype": "l26", "agent": "1", "scsihw": "virtio-scsi-single",
            "efidisk0": f"{p.storage}:1,efitype=4m,pre-enrolled-keys=0",
            "scsi0": f"{p.storage}:{node.disk_gb},discard=on,ssd=1",
            "net0": f"virtio={node.mac},bridge={p.bridge}",
            "ide2": f"{iso_ref},media=cdrom",
            "boot": "order=scsi0;ide2",
        }
        for i, gb in enumerate(extra_disks or []):
            cfg[f"scsi{i + 1}"] = f"{p.storage}:{gb},discard=on,ssd=1"
        log(f"Creating VM {vmid} {cfg['name']}: {node.cpus} vCPU, {node.memory_mb} MiB, {node.disk_gb} GiB" + (f" + {extra_disks}" if extra_disks else "") + f", MAC {node.mac}")
        upid = self._j(self.c.post(f"/nodes/{p.node}/qemu", data=cfg))
        self._wait_task(upid, log)

    def exists(self, node: NodeSpec) -> Optional[Dict]:
        r = self._find(vm_name(self.spec, node))
        return {"name": r["name"], "power": "poweredOn" if r.get("status") == "running" else "poweredOff", "vmid": r["vmid"], "node": r["node"]} if r else None

    def _status(self, node: NodeSpec, action: str, log):
        r = self._find(vm_name(self.spec, node))
        if not r:
            raise RuntimeError(f"VM {vm_name(self.spec, node)} not found")
        log(f"{r['name']}: {action}")
        upid = self._j(self.c.post(f"/nodes/{r['node']}/qemu/{r['vmid']}/status/{action}"))
        self._wait_task(upid, log, timeout=300)

    def power_on(self, node: NodeSpec, log):
        r = self._find(vm_name(self.spec, node))
        if r and r.get("status") != "running":
            self._status(node, "start", log)

    def power_off(self, node: NodeSpec, log):
        r = self._find(vm_name(self.spec, node))
        if r and r.get("status") == "running":
            self._status(node, "stop", log)

    def shutdown_guest(self, node: NodeSpec, log):
        r = self._find(vm_name(self.spec, node))
        if r and r.get("status") == "running":
            try:
                self._status(node, "shutdown", log)
            except Exception as ex:
                log(f"{r['name']}: shutdown failed ({str(ex)[-80:]}); stopping")
                self._status(node, "stop", log)

    def power_state(self, node: NodeSpec) -> str:
        r = self._find(vm_name(self.spec, node))
        return "missing" if not r else ("poweredOn" if r.get("status") == "running" else "poweredOff")

    def eject(self, node: NodeSpec, log):
        r = self._find(vm_name(self.spec, node))
        if r:
            log(f"{r['name']}: ejecting ISO")
            self._j(self.c.post(f"/nodes/{r['node']}/qemu/{r['vmid']}/config", data={"ide2": "none,media=cdrom"}))

    def destroy(self, node: NodeSpec, log):
        r = self._find(vm_name(self.spec, node))
        if not r:
            log(f"VM {vm_name(self.spec, node)} not present")
            return
        self.power_off(node, log)
        log(f"Destroying VM {r['vmid']} {r['name']}")
        upid = self._j(self.c.delete(f"/nodes/{r['node']}/qemu/{r['vmid']}", params={"purge": 1, "destroy-unreferenced-disks": 1}))
        self._wait_task(upid, log, timeout=300)
