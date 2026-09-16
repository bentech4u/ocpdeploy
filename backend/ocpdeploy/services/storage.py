"""Storage classes, NFS provisioner, LVM Storage / ODF, and image registry storage."""
from typing import Dict, List

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, clusterops as ops

NFS_NS = "nfs-provisioner"
NFS_IMAGE = "registry.k8s.io/sig-storage/nfs-subdir-external-provisioner:v4.0.2"
NFS_PROVISIONER = "k8s-sigs.io/nfs-subdir-external-provisioner"
STORAGE_LABEL = "cluster.ocs.openshift.io/openshift-storage"


def status(store: ClusterStore, spec: ClusterSpec) -> Dict:
    scs = ops.get_opt(store, spec, "storageclasses") or {}
    classes = [{"name": s["metadata"]["name"], "provisioner": s.get("provisioner", ""),
                "default": (s["metadata"].get("annotations") or {}).get("storageclass.kubernetes.io/is-default-class") == "true",
                "binding": s.get("volumeBindingMode", "")} for s in scs.get("items", [])]
    reg = ops.get_opt(store, spec, "configs.imageregistry.operator.openshift.io", "cluster") or {}
    rs = reg.get("spec") or {}
    registry = {"management": rs.get("managementState", ""), "storage": rs.get("storage") or {}, "replicas": rs.get("replicas"),
                "ready": next((o for o in ops.co_states(store, spec) if o["name"] == "image-registry"), {})}
    pvs = ops.get_opt(store, spec, "pv") or {}
    subs = {s["package"]: s for s in ops.subscriptions(store, spec)}
    lvm = ops.get_opt(store, spec, "lvmcluster", ns=(subs.get("lvms-operator") or {}).get("namespace", "openshift-storage")) or {}
    lvm_state = [(l["metadata"]["name"], (l.get("status") or {}).get("state", "")) for l in lvm.get("items", [])]
    odf = ops.get_opt(store, spec, "storagecluster", ns="openshift-storage") or {}
    odf_state = [(o["metadata"]["name"], (o.get("status") or {}).get("phase", "")) for o in odf.get("items", [])]
    nfs = ops.get_opt(store, spec, "deployment", "nfs-client-provisioner", ns=NFS_NS)
    labelled = [n["name"] for n in ops.nodes_summary(store, spec)] if False else []
    nodes = ops.get_opt(store, spec, "nodes", extra=["-l", STORAGE_LABEL]) or {}
    return {"storage_classes": classes, "registry": registry, "pv_count": len(pvs.get("items", [])),
            "operators": {k: {"installed": k in subs, "csv": subs[k]["installed_csv"] if k in subs else ""} for k in ("lvms-operator", "odf-operator", "local-storage-operator")},
            "lvm_clusters": lvm_state, "odf_clusters": odf_state, "nfs_deployed": nfs is not None,
            "storage_nodes": [n["metadata"]["name"] for n in nodes.get("items", [])]}


def set_default_sc(store: ClusterStore, spec: ClusterSpec, name: str):
    scs = ops.get(store, spec, "storageclasses")
    names = [s["metadata"]["name"] for s in scs.get("items", [])]
    if name not in names:
        raise RuntimeError(f"storage class {name} not found")
    for n in names:
        ops.patch(store, spec, f"storageclass/{n}", {"metadata": {"annotations": {"storageclass.kubernetes.io/is-default-class": "true" if n == name else "false"}}})


def nfs_docs(spec: ClusterSpec) -> List[Dict]:
    n = spec.day2.storage.nfs
    if not (n.server and n.path):
        raise RuntimeError("NFS server and export path are required")
    sa = "nfs-client-provisioner"
    labels = {"app": sa}
    docs: List[Dict] = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NFS_NS}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": sa, "namespace": NFS_NS}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "nfs-client-provisioner-runner"},
         "rules": [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "watch"]},
                   {"apiGroups": [""], "resources": ["persistentvolumes"], "verbs": ["get", "list", "watch", "create", "delete"]},
                   {"apiGroups": [""], "resources": ["persistentvolumeclaims"], "verbs": ["get", "list", "watch", "update"]},
                   {"apiGroups": ["storage.k8s.io"], "resources": ["storageclasses"], "verbs": ["get", "list", "watch"]},
                   {"apiGroups": [""], "resources": ["events"], "verbs": ["create", "update", "patch"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": {"name": "run-nfs-client-provisioner"},
         "subjects": [{"kind": "ServiceAccount", "name": sa, "namespace": NFS_NS}], "roleRef": {"kind": "ClusterRole", "name": "nfs-client-provisioner-runner", "apiGroup": "rbac.authorization.k8s.io"}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "leader-locking-nfs-client-provisioner", "namespace": NFS_NS},
         "rules": [{"apiGroups": [""], "resources": ["endpoints"], "verbs": ["get", "list", "watch", "create", "update", "patch"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "leader-locking-nfs-client-provisioner", "namespace": NFS_NS},
         "subjects": [{"kind": "ServiceAccount", "name": sa, "namespace": NFS_NS}], "roleRef": {"kind": "Role", "name": "leader-locking-nfs-client-provisioner", "apiGroup": "rbac.authorization.k8s.io"}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": sa, "namespace": NFS_NS, "labels": labels},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {"serviceAccountName": sa, "containers": [{
                      "name": sa, "image": NFS_IMAGE, "volumeMounts": [{"name": "nfs-client-root", "mountPath": "/persistentvolumes"}],
                      "env": [{"name": "PROVISIONER_NAME", "value": NFS_PROVISIONER}, {"name": "NFS_SERVER", "value": n.server}, {"name": "NFS_PATH", "value": n.path}]}],
                      "volumes": [{"name": "nfs-client-root", "nfs": {"server": n.server, "path": n.path}}]}}}},
        {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
         "metadata": {"name": n.sc_name or "nfs-client", "annotations": {"storageclass.kubernetes.io/is-default-class": "true" if n.make_default else "false"}},
         "provisioner": NFS_PROVISIONER, "parameters": {"archiveOnDelete": "false"}, "reclaimPolicy": "Delete", "volumeBindingMode": "Immediate", "allowVolumeExpansion": True},
    ]
    return docs


def job_nfs(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    docs = nfs_docs(spec)
    n = spec.day2.storage.nfs
    ctx.log(f"Deploying nfs-subdir-external-provisioner for {n.server}:{n.path} as StorageClass {n.sc_name}")
    ctx.log(ops.apply(store, spec, docs).strip())
    kube.oc(store, spec, ["adm", "policy", "add-scc-to-user", "hostmount-anyuid", "-z", "nfs-client-provisioner", "-n", NFS_NS])
    if n.make_default:
        set_default_sc(store, spec, n.sc_name)
    ops.wait_for("provisioner rollout", lambda: (ops.get(store, spec, "deployment", "nfs-client-provisioner", ns=NFS_NS).get("status") or {}).get("availableReplicas", 0) >= 1, timeout=600, interval=10, log=ctx.log)
    if spec.mirror.enabled:
        ctx.log(f"Disconnected cluster: make sure {NFS_IMAGE} is in the mirror (add it to 'additional images').")
    ctx.log("NFS storage class ready.")


def lvm_docs(ns: str) -> List[Dict]:
    return [{"apiVersion": "lvm.topolvm.io/v1alpha1", "kind": "LVMCluster", "metadata": {"name": "lvmcluster", "namespace": ns},
             "spec": {"storage": {"deviceClasses": [{"name": "vg1", "default": True, "fstype": "xfs",
                                                     "thinPoolConfig": {"name": "thin-pool-1", "sizePercent": 90, "overprovisionRatio": 10}}]}}}]


def job_lvms(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    from . import operators
    m = operators.ensure_installed(ctx, store, spec, "lvms-operator")
    ns = m["namespace"]
    ops.wait_for("LVMCluster CRD", lambda: ops.get_opt(store, spec, "crd", "lvmclusters.lvm.topolvm.io") is not None, timeout=600, interval=10, log=ctx.log)
    ctx.log(f"Creating LVMCluster in {ns} using every unused disk on the nodes (device class vg1, thin pool)")
    ctx.log(ops.apply(store, spec, lvm_docs(ns)).strip())
    ops.wait_for("LVMCluster Ready", lambda: (ops.get(store, spec, "lvmcluster", "lvmcluster", ns=ns).get("status") or {}).get("state") == "Ready", timeout=1200, interval=15, log=ctx.log,
                 on_tick=lambda: "state: " + str((ops.get(store, spec, "lvmcluster", "lvmcluster", ns=ns).get("status") or {}).get("state", "")))
    ctx.log("LVM Storage ready: StorageClass lvms-vg1 is the default.")


def odf_docs(spec: ClusterSpec, storage_nodes: List[str], disk_count: int) -> List[Dict]:
    st = spec.day2.storage
    return [
        {"apiVersion": "local.storage.openshift.io/v1alpha1", "kind": "LocalVolumeSet", "metadata": {"name": "localblock", "namespace": "openshift-local-storage"},
         "spec": {"nodeSelector": {"nodeSelectorTerms": [{"matchExpressions": [{"key": STORAGE_LABEL, "operator": "In", "values": [""]}]}]},
                  "storageClassName": "localblock", "volumeMode": "Block", "fsType": "ext4", "maxDeviceCount": 10,
                  "deviceInclusionSpec": {"deviceTypes": ["disk", "part"], "deviceMechanicalProperties": ["NonRotational", "Rotational"],
                                          "minSize": f"{st.odf_min_disk_gb}Gi", "maxSize": f"{st.odf_max_disk_gb}Gi"}}},
        {"apiVersion": "ocs.openshift.io/v1", "kind": "StorageCluster", "metadata": {"name": "ocs-storagecluster", "namespace": "openshift-storage"},
         "spec": {"manageNodes": False, "resources": {}, "monDataDirHostPath": "/var/lib/rook", "flexibleScaling": True,
                  "storageDeviceSets": [{"name": "ocs-deviceset", "count": max(1, disk_count), "replica": 1, "portable": False, "resources": {},
                                         "dataPVCTemplate": {"spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1"}},
                                                                      "storageClassName": "localblock", "volumeMode": "Block"}}}]}},
    ]


def job_odf(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    from . import operators
    nodes = ops.get_opt(store, spec, "nodes", extra=["-l", STORAGE_LABEL]) or {}
    storage_nodes = [n["metadata"]["name"] for n in nodes.get("items", [])]
    if len(storage_nodes) < 3:
        workers = [n["name"] for n in ops.nodes_summary(store, spec) if "worker" in n["roles"] and "infra" not in n["roles"]]
        if len(workers) < 3:
            raise RuntimeError("ODF internal mode needs at least 3 storage nodes; label nodes or create a storage pool first")
        ctx.log(f"No nodes carry {STORAGE_LABEL}; labelling the workers: {', '.join(workers)}")
        for w in workers:
            ops.label_node(store, spec, w, {STORAGE_LABEL: ""})
        storage_nodes = workers
    operators.ensure_installed(ctx, store, spec, "local-storage-operator")
    operators.ensure_installed(ctx, store, spec, "odf-operator")
    ops.wait_for("LocalVolumeSet CRD", lambda: ops.get_opt(store, spec, "crd", "localvolumesets.local.storage.openshift.io") is not None, timeout=600, interval=10, log=ctx.log)
    ops.wait_for("StorageCluster CRD", lambda: ops.get_opt(store, spec, "crd", "storageclusters.ocs.openshift.io") is not None, timeout=600, interval=10, log=ctx.log)
    docs = odf_docs(spec, storage_nodes, 3)
    ctx.log(f"Creating LocalVolumeSet localblock on {', '.join(storage_nodes)} (disks {spec.day2.storage.odf_min_disk_gb}-{spec.day2.storage.odf_max_disk_gb} GiB)")
    ctx.log(ops.apply(store, spec, docs[:1]).strip())

    def pvs():
        p = ops.get(store, spec, "pv")
        return len([i for i in p.get("items", []) if (i["spec"].get("storageClassName") == "localblock")])
    ops.wait_for("local block PVs (>= 3)", lambda: pvs() >= 3, timeout=900, interval=15, log=ctx.log, on_tick=lambda: f"localblock PVs: {pvs()}")
    count = pvs()
    docs = odf_docs(spec, storage_nodes, count)
    ctx.log(f"Creating StorageCluster with {count} OSD device(s)")
    ctx.log(ops.apply(store, spec, docs[1:]).strip())
    ops.wait_for("StorageCluster Ready", lambda: (ops.get(store, spec, "storagecluster", "ocs-storagecluster", ns="openshift-storage").get("status") or {}).get("phase") == "Ready",
                 timeout=3600, interval=30, log=ctx.log,
                 on_tick=lambda: "phase: " + str((ops.get(store, spec, "storagecluster", "ocs-storagecluster", ns="openshift-storage").get("status") or {}).get("phase", "")))
    ctx.log("ODF ready: storage classes ocs-storagecluster-ceph-rbd, ocs-storagecluster-cephfs and the NooBaa object bucket class are available.")


def registry_docs(spec: ClusterSpec) -> (List[Dict], Dict):
    r = spec.day2.storage.registry
    docs: List[Dict] = []
    if r.mode == "removed":
        return docs, {"spec": {"managementState": "Removed"}}
    if r.mode == "emptydir":
        return docs, {"spec": {"managementState": "Managed", "storage": {"emptyDir": {}}, "replicas": 1, "rolloutStrategy": "Recreate"}}
    pvc: Dict = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "image-registry-storage", "namespace": "openshift-image-registry"},
                 "spec": {"accessModes": ["ReadWriteMany" if r.replicas > 1 else "ReadWriteOnce"], "resources": {"requests": {"storage": f"{r.size_gb}Gi"}}}}
    if r.storage_class:
        pvc["spec"]["storageClassName"] = r.storage_class
    docs.append(pvc)
    patch = {"spec": {"managementState": "Managed", "storage": {"pvc": {"claim": "image-registry-storage"}}, "replicas": r.replicas,
                      "rolloutStrategy": "RollingUpdate" if r.replicas > 1 else "Recreate"}}
    return docs, patch


def job_registry(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    docs, patch = registry_docs(spec)
    r = spec.day2.storage.registry
    if docs:
        existing = ops.get_opt(store, spec, "pvc", "image-registry-storage", ns="openshift-image-registry")
        if existing:
            ctx.log("PVC image-registry-storage already exists; keeping it")
        else:
            ctx.log(f"Creating PVC image-registry-storage ({r.size_gb} GiB, {docs[0]['spec']['accessModes'][0]}, class {r.storage_class or 'default'})")
            ctx.log(ops.apply(store, spec, docs).strip())
    # a previous storage backend must be cleared before a new one is set
    cur = (ops.get_opt(store, spec, "configs.imageregistry.operator.openshift.io", "cluster") or {}).get("spec", {}).get("storage") or {}
    if cur and r.mode != "removed" and set(cur) - set(patch["spec"]["storage"]):
        ctx.log(f"Clearing previous registry storage config {list(cur)}")
        ops.patch(store, spec, "configs.imageregistry.operator.openshift.io/cluster", {"spec": {"storage": None}})
    ctx.log(f"Registry: {r.mode} (management {patch['spec']['managementState']})")
    ops.patch(store, spec, "configs.imageregistry.operator.openshift.io/cluster", patch)
    if r.mode != "removed":
        ops.wait_for("image-registry operator available", lambda: next((o for o in ops.co_states(store, spec) if o["name"] == "image-registry"), {}).get("available") == "True"
                     and next((o for o in ops.co_states(store, spec) if o["name"] == "image-registry"), {}).get("progressing") == "False", timeout=900, interval=15, log=ctx.log,
                     on_tick=lambda: "image-registry: " + (next((o["message"] for o in ops.co_states(store, spec) if o["name"] == "image-registry"), "") or "rolling")[:140])
    ctx.log("Registry storage configured.")
