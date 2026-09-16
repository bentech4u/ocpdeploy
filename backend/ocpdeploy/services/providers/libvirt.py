"""KVM / libvirt provider: drives virsh and virt-install on the hypervisor over SSH."""
import shlex
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import paramiko

from ...models import NodeSpec
from ...settings import DEFAULT_SSH_KEY
from .base import Provider, vm_name


class LibvirtProvider(Provider):
    name = "libvirt"
    title = "KVM / libvirt over SSH"

    def _client(self) -> paramiko.SSHClient:
        l = self.spec.libvirt
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = {"hostname": l.host, "port": l.ssh_port, "username": l.ssh_user, "timeout": 15, "allow_agent": False}
        if l.ssh_password:
            kw["password"] = l.ssh_password
            kw["look_for_keys"] = False
        else:
            kw["key_filename"] = str(DEFAULT_SSH_KEY)
        c.connect(**kw)
        return c

    def _run(self, cmd: str, check: bool = True, timeout: int = 600) -> Tuple[int, str, str]:
        c = self._client()
        try:
            _, out, err = c.exec_command(cmd, timeout=timeout)
            rc = out.channel.recv_exit_status()
            o, e = out.read().decode(errors="replace"), err.read().decode(errors="replace")
        finally:
            c.close()
        if check and rc != 0:
            raise RuntimeError(f"{cmd.split()[0]} failed ({rc}): {(e or o).strip()[-200:]}")
        return rc, o, e

    def check(self) -> List[Dict]:
        l = self.spec.libvirt
        if not l.host:
            return [{"name": "KVM host", "status": "fail", "expected": "configured", "actual": "", "hint": "Fill the Infrastructure step"}]
        rows: List[Dict] = []
        try:
            _, o, _ = self._run("virsh version --daemon && virt-install --version")
            rows.append({"name": f"libvirt on {l.host}", "status": "pass", "expected": "virsh + virt-install", "actual": " ".join(o.split()[-6:]), "hint": ""})
        except Exception as ex:
            return [{"name": f"libvirt on {l.host}", "status": "fail", "expected": "SSH + virsh + virt-install", "actual": str(ex)[-160:], "hint": "Install libvirt and virt-install; allow SSH for the user (key from the installer host or password)"}]
        rc, o, _ = self._run(f"virsh pool-info {shlex.quote(l.pool)}", check=False)
        rows.append({"name": f"storage pool {l.pool}", "status": "pass" if rc == 0 else "fail", "expected": "exists", "actual": " ".join(x for x in o.split("\n") if x.startswith("Available")) or ("found" if rc == 0 else "missing"), "hint": "" if rc == 0 else "virsh pool-define-as default dir --target /var/lib/libvirt/images && virsh pool-start default"})
        rc, _, _ = self._run(f"ip link show {shlex.quote(l.bridge)}", check=False)
        rows.append({"name": f"bridge {l.bridge}", "status": "pass" if rc == 0 else "fail", "expected": "exists on the host", "actual": "found" if rc == 0 else "missing", "hint": "" if rc == 0 else "Create a Linux bridge on the node network (nmcli con add type bridge ...)"})
        rc, _, _ = self._run(f"test -d {shlex.quote(l.images_dir)} && test -w {shlex.quote(l.images_dir)}", check=False)
        rows.append({"name": f"images dir {l.images_dir}", "status": "pass" if rc == 0 else "fail", "expected": "writable", "actual": "ok" if rc == 0 else "missing or read-only", "hint": ""})
        _, o, _ = self._run("virsh list --all --name", check=False)
        existing = [n.name for n in self.spec.nodes if vm_name(self.spec, n) in o.split()]
        rows.append({"name": "existing VMs with cluster names", "status": "pass" if not existing else "warn", "expected": "none", "actual": ", ".join(existing) or "none", "hint": "" if not existing else "Leftovers from a previous attempt; destroy first"})
        return rows

    def upload_iso(self, iso_local: Path, log) -> str:
        l = self.spec.libvirt
        remote = f"{l.images_dir.rstrip('/')}/{self.spec.name}-{iso_local.name}"
        log(f"Copying {iso_local.name} ({iso_local.stat().st_size // 2**20} MiB) to {l.host}:{remote}")
        c = self._client()
        try:
            sftp = c.open_sftp()
            sftp.put(str(iso_local), remote)
            sftp.close()
        finally:
            c.close()
        log("Copy complete")
        return remote

    def create_node(self, node: NodeSpec, iso_ref: str, log, extra_disks: Optional[List[int]] = None):
        l = self.spec.libvirt
        name = vm_name(self.spec, node)
        disks = f"--disk pool={shlex.quote(l.pool)},size={node.disk_gb},bus=virtio,format=qcow2"
        for gb in extra_disks or []:
            disks += f" --disk pool={shlex.quote(l.pool)},size={gb},bus=virtio,format=qcow2"
        cmd = (f"virt-install --connect qemu:///system --name {shlex.quote(name)} --memory {node.memory_mb} --vcpus {node.cpus} --cpu host-passthrough "
               f"{disks} --network bridge={shlex.quote(l.bridge)},model=virtio,mac={node.mac} --cdrom {shlex.quote(iso_ref)} "
               f"--os-variant {shlex.quote(l.os_variant)} --boot uefi --graphics vnc,listen=127.0.0.1 --noautoconsole --wait 0")
        log(f"Creating VM {name}: {node.cpus} vCPU, {node.memory_mb} MiB, {node.disk_gb} GiB" + (f" + {extra_disks}" if extra_disks else "") + f", MAC {node.mac}")
        rc, o, e = self._run(cmd, check=False, timeout=900)
        if rc != 0 and "already exists" not in e:
            raise RuntimeError(f"virt-install failed: {(e or o).strip()[-300:]}")

    def _state(self, name: str) -> str:
        rc, o, _ = self._run(f"virsh domstate {shlex.quote(name)}", check=False, timeout=60)
        if rc != 0:
            return "missing"
        return "poweredOn" if "running" in o else "poweredOff"

    def exists(self, node: NodeSpec) -> Optional[Dict]:
        st = self._state(vm_name(self.spec, node))
        return None if st == "missing" else {"name": vm_name(self.spec, node), "power": st}

    def power_on(self, node: NodeSpec, log):
        name = vm_name(self.spec, node)
        if self._state(name) == "poweredOff":
            log(f"Powering on {name}")
            self._run(f"virsh start {shlex.quote(name)}", timeout=60)

    def power_off(self, node: NodeSpec, log):
        name = vm_name(self.spec, node)
        if self._state(name) == "poweredOn":
            log(f"Powering off {name}")
            self._run(f"virsh destroy {shlex.quote(name)}", check=False, timeout=60)

    def shutdown_guest(self, node: NodeSpec, log):
        name = vm_name(self.spec, node)
        if self._state(name) == "poweredOn":
            log(f"{name}: guest shutdown requested")
            self._run(f"virsh shutdown {shlex.quote(name)}", check=False, timeout=60)

    def power_state(self, node: NodeSpec) -> str:
        return self._state(vm_name(self.spec, node))

    def eject(self, node: NodeSpec, log):
        name = vm_name(self.spec, node)
        _, o, _ = self._run(f"virsh domblklist {shlex.quote(name)}", check=False, timeout=60)
        for line in o.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].endswith(".iso"):
                log(f"{name}: ejecting ISO from {parts[0]}")
                self._run(f"virsh change-media {shlex.quote(name)} {parts[0]} --eject --force --config --live", check=False, timeout=60)

    def destroy(self, node: NodeSpec, log):
        name = vm_name(self.spec, node)
        if self._state(name) == "missing":
            log(f"VM {name} not present")
            return
        self.power_off(node, log)
        log(f"Destroying VM {name}")
        self._run(f"virsh undefine {shlex.quote(name)} --nvram --remove-all-storage", check=False, timeout=120)
