"""vCenter access through pyVmomi: certificate capture, inventory discovery,
privilege check, datastore upload and VM creation for the agent path."""
import hashlib
import ssl
import socket
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, vmodl

from ..models import VCenterSpec

REQUIRED_PRIVILEGES = [
    "Datastore.AllocateSpace", "Datastore.Browse", "Datastore.FileManagement",
    "Folder.Create", "Folder.Delete",
    "Network.Assign",
    "Resource.AssignVMToPool",
    "VirtualMachine.Config.AddNewDisk", "VirtualMachine.Config.AddExistingDisk",
    "VirtualMachine.Config.AdvancedConfig", "VirtualMachine.Config.CPUCount",
    "VirtualMachine.Config.Memory", "VirtualMachine.Config.Settings",
    "VirtualMachine.Interact.PowerOn", "VirtualMachine.Interact.PowerOff",
    "VirtualMachine.Inventory.Create", "VirtualMachine.Inventory.Delete",
    "VirtualMachine.Provisioning.Clone", "VirtualMachine.Provisioning.DeployTemplate",
    "VirtualMachine.Provisioning.MarkAsTemplate",
    "VApp.Import",
    "StorageProfile.View",
    "System.Read",
]


# ---------------------------------------------------------------- certificate
def fetch_cert(host: str, port: int = 443) -> Dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=8) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ss:
            der = ss.getpeercert(binary_form=True)
    pem = ssl.DER_cert_to_PEM_cert(der)
    from cryptography import x509
    cert = x509.load_der_x509_certificate(der)
    ca_pem, ca_subjects = fetch_ca_bundle(host, port)
    return {
        "pem": pem,
        "ca_pem": ca_pem,
        "ca_subjects": ca_subjects,
        "trust_pem": ca_pem or pem,
        "sha1": ":".join(f"{b:02X}" for b in hashlib.sha1(der).digest()),
        "sha256": ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest()),
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "not_after": cert.not_valid_after_utc.isoformat() if hasattr(cert, "not_valid_after_utc") else str(cert.not_valid_after),
        "self_signed": cert.subject == cert.issuer,
    }


def fetch_ca_bundle(host: str, port: int = 443):
    """vCenter publishes its trusted root CAs (VMCA or custom) at /certs/download.zip.
    Returns (concatenated PEM, [subjects]) or ("", []) when unavailable."""
    import io
    import zipfile
    from cryptography import x509
    try:
        r = httpx.get(f"https://{host}:{port}/certs/download.zip", verify=False, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            return "", []
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception:
        return "", []
    pems, subjects = [], []
    for name in z.namelist():
        if "/lin/" in name and (name.endswith(".0") or name.endswith(".pem") or name.endswith(".crt")):
            data = z.read(name)
            try:
                if data.strip().startswith(b"-----BEGIN"):
                    c = x509.load_pem_x509_certificate(data)
                    text = data.decode()
                else:
                    c = x509.load_der_x509_certificate(data)
                    text = ssl.DER_cert_to_PEM_cert(data)
            except Exception:
                continue
            if text.strip() not in [p.strip() for p in pems]:
                pems.append(text.strip() + "\n")
                subjects.append(c.subject.rfc4514_string())
    return "".join(pems), subjects


# ---------------------------------------------------------------- session
@contextmanager
def session(vc: VCenterSpec):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    si = SmartConnect(host=vc.host, user=vc.username, pwd=vc.password, sslContext=ctx, port=443)
    try:
        yield si
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass


def about(vc: VCenterSpec) -> Dict:
    with session(vc) as si:
        a = si.content.about
        return {"name": a.fullName, "version": a.version, "build": a.build, "api_type": a.apiType,
                "instance_uuid": a.instanceUuid, "os": a.osType}


def _path(obj) -> str:
    """Inventory path like /DC/host/Cluster or /DC/vm/folder."""
    parts = []
    while obj is not None and not isinstance(obj, vim.ServiceInstance):
        if isinstance(obj, vim.Folder) and obj.parent is None:
            break
        parts.append(obj.name)
        obj = getattr(obj, "parent", None)
    parts.reverse()
    # drop the root folder "Datacenters"
    if parts and parts[0] == "Datacenters":
        parts = parts[1:]
    return "/" + "/".join(parts)


def _rps(rp, out, depth=0):
    out.append({"name": rp.name, "path": _path(rp), "depth": depth})
    for c in rp.resourcePool:
        _rps(c, out, depth + 1)


def _folders(folder, out, depth=0):
    for c in folder.childEntity:
        if isinstance(c, vim.Folder):
            out.append({"name": c.name, "path": _path(c), "depth": depth})
            _folders(c, out, depth + 1)


def inventory(vc: VCenterSpec) -> Dict:
    with session(vc) as si:
        content = si.content
        dcs = []
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datacenter], True)
        try:
            for dc in view.view:
                clusters = []
                cv = content.viewManager.CreateContainerView(dc.hostFolder, [vim.ComputeResource], True)
                try:
                    for cr in cv.view:
                        summ = cr.summary
                        rps: List[Dict] = []
                        _rps(cr.resourcePool, rps)
                        clusters.append({
                            "name": cr.name, "path": _path(cr),
                            "kind": "cluster" if isinstance(cr, vim.ClusterComputeResource) else "host",
                            "hosts": summ.numHosts, "cpu_cores": summ.numCpuCores, "cpu_mhz": summ.totalCpu,
                            "memory_gb": round(summ.totalMemory / 2**30, 1),
                            "eff_cpu_mhz": summ.effectiveCpu, "eff_memory_gb": round(summ.effectiveMemory / 1024, 1),
                            "resource_pools": rps,
                            "datastores": [d.name for d in cr.datastore],
                            "networks": [n.name for n in cr.network],
                        })
                finally:
                    cv.Destroy()
                datastores = []
                for ds in dc.datastore:
                    s = ds.summary
                    datastores.append({"name": ds.name, "path": _path(ds), "type": s.type,
                                       "capacity_gb": round(s.capacity / 2**30, 1), "free_gb": round(s.freeSpace / 2**30, 1),
                                       "accessible": s.accessible, "multiple_host": s.multipleHostAccess})
                networks = []
                for n in dc.network:
                    kind = "dvportgroup" if isinstance(n, vim.dvs.DistributedVirtualPortgroup) else "standard"
                    if isinstance(n, vim.dvs.DistributedVirtualPortgroup) and getattr(n.config, "uplink", False):
                        continue
                    networks.append({"name": n.name, "path": _path(n), "type": kind})
                folders: List[Dict] = []
                _folders(dc.vmFolder, folders)
                dcs.append({"name": dc.name, "path": _path(dc), "clusters": clusters, "datastores": datastores,
                            "networks": networks, "folders": folders})
        finally:
            view.Destroy()
        a = content.about
        return {"about": {"name": a.fullName, "version": a.version, "build": a.build}, "datacenters": dcs}


def privileges(vc: VCenterSpec) -> List[Dict]:
    """Best-effort check of the privileges the installer needs on the chosen objects."""
    with session(vc) as si:
        content = si.content
        am = content.authorizationManager
        sm = content.sessionManager
        user = sm.currentSession.userName
        entities = [content.rootFolder]
        want = {"datacenter": vc.datacenter, "cluster": vc.cluster, "datastore": vc.datastore, "network": vc.network}
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.ManagedEntity], True)
        try:
            for e in view.view:
                if isinstance(e, vim.Datacenter) and e.name == want["datacenter"]:
                    entities.append(e)
                elif isinstance(e, vim.ComputeResource) and e.name == want["cluster"]:
                    entities.append(e)
                elif isinstance(e, vim.Datastore) and e.name == want["datastore"]:
                    entities.append(e)
                elif isinstance(e, vim.Network) and e.name == want["network"]:
                    entities.append(e)
        finally:
            view.Destroy()
        results = []
        try:
            got = am.FetchUserPrivilegeOnEntities(entities, user)
            have = set()
            for g in got:
                have.update(getattr(g, "privileges", None) or getattr(g, "privId", []))
            for p in REQUIRED_PRIVILEGES:
                results.append({"name": p, "status": "pass" if p in have else "fail",
                                "expected": "granted", "actual": "granted" if p in have else "missing",
                                "hint": "" if p in have else "Grant this privilege to the vCenter user or use an administrator account"})
        except vmodl.MethodFault as ex:
            results.append({"name": "privilege query", "status": "warn", "expected": "", "actual": str(ex.msg),
                            "hint": "Could not enumerate privileges (needs vSphere 6.5+ and Authorization.ModifyPermissions); the deploy may still work"})
        return results


# ---------------------------------------------------------------- objects
def _find(content, vimtype, name, root=None):
    view = content.viewManager.CreateContainerView(root or content.rootFolder, [vimtype], True)
    try:
        for o in view.view:
            if o.name == name:
                return o
    finally:
        view.Destroy()
    return None


def _find_by_path(content, path: str):
    """Find inventory object by /DC/... path via the searchIndex."""
    return content.searchIndex.FindByInventoryPath(path)


def find_vm(vc: VCenterSpec, name: str):
    with session(vc) as si:
        vm = _find(si.content, vim.VirtualMachine, name)
        return vm_summary(vm) if vm else None


def vm_summary(vm) -> Dict:
    return {"name": vm.name, "power": str(vm.runtime.powerState), "ip": vm.guest.ipAddress,
            "uuid": vm.config.uuid if vm.config else None, "moid": vm._moId,
            "cpus": vm.config.hardware.numCPU if vm.config else None,
            "memory_mb": vm.config.hardware.memoryMB if vm.config else None,
            "tools": vm.guest.toolsRunningStatus, "path": _path(vm)}


def list_vms(vc: VCenterSpec, prefix: str = "") -> List[Dict]:
    with session(vc) as si:
        view = si.content.viewManager.CreateContainerView(si.content.rootFolder, [vim.VirtualMachine], True)
        try:
            return [vm_summary(v) for v in view.view if v.name.startswith(prefix)]
        finally:
            view.Destroy()


def _wait(task, log=None):
    while task.info.state in (vim.TaskInfo.State.running, vim.TaskInfo.State.queued):
        import time
        time.sleep(1)
    if task.info.state == vim.TaskInfo.State.error:
        raise RuntimeError(f"vCenter task failed: {task.info.error.msg}")
    return task.info.result


def upload_to_datastore(vc: VCenterSpec, local: Path, remote_path: str, log=print):
    """PUT a file into the datastore at remote_path (e.g. ocpdeploy/homeshift/agent.iso)."""
    with session(vc) as si:
        cookie = si._stub.cookie
        content = si.content
        ds = None
        dc = _find(content, vim.Datacenter, vc.datacenter)
        for d in dc.datastore:
            if d.name == vc.datastore:
                ds = d
        if ds is None:
            raise RuntimeError(f"datastore {vc.datastore} not found in {vc.datacenter}")
        # make directory
        folder = remote_path.rsplit("/", 1)[0] if "/" in remote_path else ""
        if folder:
            try:
                content.fileManager.MakeDirectory(name=f"[{vc.datastore}] {folder}", datacenter=dc, createParentDirectories=True)
            except vim.fault.FileAlreadyExists:
                pass
        url = f"https://{vc.host}:443/folder/{remote_path}"
        params = {"dsName": vc.datastore, "dcPath": vc.datacenter}
        size = local.stat().st_size
        log(f"Uploading {local.name} ({size // 2**20} MiB) to [{vc.datastore}] {remote_path}")
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size),
                   "Cookie": cookie.split(";")[0]}
        with open(local, "rb") as f:
            with httpx.Client(verify=False, timeout=None) as client:
                r = client.put(url, params=params, headers=headers, content=f)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"datastore upload failed: HTTP {r.status_code} {r.text[:200]}")
        log("Upload complete")
        return f"[{vc.datastore}] {remote_path}"


def create_vm(vc: VCenterSpec, name: str, cpus: int, memory_mb: int, disk_gb: int, mac: str,
              iso_ds_path: Optional[str], log=print, firmware: str = "efi", cores_per_socket: int = 2,
              extra_config: Optional[Dict[str, str]] = None) -> Dict:
    with session(vc) as si:
        content = si.content
        dc = _find(content, vim.Datacenter, vc.datacenter)
        if dc is None:
            raise RuntimeError(f"datacenter {vc.datacenter} not found")
        if _find(content, vim.VirtualMachine, name, dc.vmFolder):
            raise RuntimeError(f"VM {name} already exists")
        cr = _find(content, vim.ComputeResource, vc.cluster, dc.hostFolder)
        if cr is None:
            raise RuntimeError(f"cluster {vc.cluster} not found")
        pool = cr.resourcePool
        if vc.resource_pool:
            p = _find_by_path(content, vc.resource_pool) or _find(content, vim.ResourcePool, vc.resource_pool.rsplit("/", 1)[-1], cr)
            if p:
                pool = p
        folder = dc.vmFolder
        if vc.folder:
            f = _find_by_path(content, vc.folder) if vc.folder.startswith("/") else _find(content, vim.Folder, vc.folder, dc.vmFolder)
            if f is None:
                log(f"Creating VM folder {vc.folder}")
                f = dc.vmFolder.CreateFolder(vc.folder.rsplit("/", 1)[-1])
            folder = f
        net = None
        for n in dc.network:
            if n.name == vc.network:
                net = n
        if net is None:
            raise RuntimeError(f"network {vc.network} not found")

        devices = []
        # SCSI controller
        scsi = vim.vm.device.VirtualDeviceSpec()
        scsi.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        scsi.device = vim.vm.device.ParaVirtualSCSIController()
        scsi.device.key = -100
        scsi.device.busNumber = 0
        scsi.device.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing
        devices.append(scsi)
        # disk
        disk = vim.vm.device.VirtualDeviceSpec()
        disk.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        disk.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create
        disk.device = vim.vm.device.VirtualDisk()
        disk.device.key = -101
        disk.device.controllerKey = -100
        disk.device.unitNumber = 0
        disk.device.capacityInKB = disk_gb * 1024 * 1024
        backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        backing.diskMode = "persistent"
        backing.thinProvisioned = True
        backing.fileName = f"[{vc.datastore}]"
        disk.device.backing = backing
        devices.append(disk)
        # NIC
        nic = vim.vm.device.VirtualDeviceSpec()
        nic.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        nic.device = vim.vm.device.VirtualVmxnet3()
        nic.device.key = -102
        if isinstance(net, vim.dvs.DistributedVirtualPortgroup):
            b = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
            b.port = vim.dvs.PortConnection(portgroupKey=net.key, switchUuid=net.config.distributedVirtualSwitch.uuid)
        else:
            b = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=net.name, network=net)
        nic.device.backing = b
        nic.device.addressType = "manual"
        nic.device.macAddress = mac
        nic.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, allowGuestControl=True, connected=True)
        devices.append(nic)
        # CD-ROM with ISO
        if iso_ds_path:
            ide = vim.vm.device.VirtualDeviceSpec()
            ide.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            ide.device = vim.vm.device.VirtualIDEController()
            ide.device.key = -200
            ide.device.busNumber = 0
            devices.append(ide)
            cd = vim.vm.device.VirtualDeviceSpec()
            cd.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            cd.device = vim.vm.device.VirtualCdrom()
            cd.device.key = -201
            cd.device.controllerKey = -200
            cd.device.unitNumber = 0
            cd.device.backing = vim.vm.device.VirtualCdrom.IsoBackingInfo(fileName=iso_ds_path)
            cd.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, allowGuestControl=True, connected=True)
            devices.append(cd)

        spec = vim.vm.ConfigSpec()
        spec.name = name
        spec.guestId = vc.guest_id or "rhel9_64Guest"
        spec.numCPUs = cpus
        spec.numCoresPerSocket = min(cores_per_socket, cpus)
        spec.memoryMB = memory_mb
        spec.firmware = firmware
        spec.files = vim.vm.FileInfo(vmPathName=f"[{vc.datastore}]")
        spec.deviceChange = devices
        ec = {"disk.EnableUUID": "TRUE"}
        if extra_config:
            ec.update(extra_config)
        spec.extraConfig = [vim.option.OptionValue(key=k, value=v) for k, v in ec.items()]
        if firmware == "efi":
            spec.bootOptions = vim.vm.BootOptions(efiSecureBootEnabled=False)
        log(f"Creating VM {name}: {cpus} vCPU, {memory_mb} MiB, {disk_gb} GiB, MAC {mac}")
        task = folder.CreateVM_Task(config=spec, pool=pool)
        vm = _wait(task, log)
        return vm_summary(vm)


def power(vc: VCenterSpec, name: str, state: str, log=print) -> Dict:
    with session(vc) as si:
        vm = _find(si.content, vim.VirtualMachine, name)
        if vm is None:
            raise RuntimeError(f"VM {name} not found")
        if state == "on" and vm.runtime.powerState != "poweredOn":
            log(f"Powering on {name}")
            _wait(vm.PowerOnVM_Task())
        elif state == "off" and vm.runtime.powerState != "poweredOff":
            log(f"Powering off {name}")
            _wait(vm.PowerOffVM_Task())
        return vm_summary(vm)


def destroy_vm(vc: VCenterSpec, name: str, log=print) -> bool:
    with session(vc) as si:
        vm = _find(si.content, vim.VirtualMachine, name)
        if vm is None:
            log(f"VM {name} not present")
            return False
        if vm.runtime.powerState != "poweredOff":
            _wait(vm.PowerOffVM_Task())
        log(f"Destroying VM {name}")
        _wait(vm.Destroy_Task())
        return True


def eject_cdrom(vc: VCenterSpec, name: str, log=print):
    """Disconnect the ISO so the node boots from disk on the next reboot."""
    with session(vc) as si:
        vm = _find(si.content, vim.VirtualMachine, name)
        if vm is None:
            return
        changes = []
        for d in vm.config.hardware.device:
            if isinstance(d, vim.vm.device.VirtualCdrom):
                d.backing = vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo(deviceName="", exclusive=False)
                d.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=False, allowGuestControl=True, connected=False)
                ch = vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.edit, device=d)
                changes.append(ch)
        if changes:
            log(f"Ejecting ISO from {name}")
            _wait(vm.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=changes)))
