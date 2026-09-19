"""Dell CSM Replication for PowerScale without repctl: wiring two clusters, replicated StorageClasses,
replication-group status and the DR actions.

What repctl does, done with plain API calls (csm-replication v1.15.0, repctl/pkg/cmd/cluster.go):
* on each cluster, namespace dell-replication-controller holds one Opaque Secret per peer, named after
  the peer's clusterId, key 'data' = a kubeconfig for the peer. The app builds that kubeconfig from the
  peer's own replication service account token (secret replication-secret), never from the admin login;
* ConfigMap dell-replication-controller-config, key config.yaml: clusterId + targets[{clusterId, address,
  secretRef}]. The Secret is written before the ConfigMap: the controller reloads on ConfigMap change and
  ignores a Secret it does not know yet;
* DR actions set spec.action on the right DellCSIReplicationGroup; the replicator sidecar performs them
  and records the result in the 'Action' annotation, status.state and status.lastAction.

PowerScale supports FAILOVER_REMOTE, UNPLANNED_FAILOVER_LOCAL, FAILBACK_LOCAL,
ACTION_FAILBACK_DISCARD_CHANGES_LOCAL, REPROTECT_LOCAL, SUSPEND, RESUME and SYNC (csi-powerscale
service/replication.go ExecuteAction). Every array must be listed in the driver secret on both clusters.
"""
import base64
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from ..jobs import JobContext
from ..models import ClusterSpec, PowerScaleSpec, PowerScaleReplication
from ..settings import IMPORTED_DIR
from ..store import ClusterStore
from . import kube, clusterops as ops, dellbundle, onefs, powerscale as ps

REPL_NS = "dell-replication-controller"
CM_NAME = "dell-replication-controller-config"
SA_SECRET = "replication-secret"
RG_KIND = "dellcsireplicationgroups.replication.storage.dell.com"
PREFIX = "replication.storage.dell.com"
RPOS = ["Five_Minutes", "Fifteen_Minutes", "Thirty_Minutes", "One_Hour", "Six_Hours", "Twelve_Hours", "One_Day"]
_ID = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# action -> (label, which RG it is set on, confirmation needed)
ACTIONS = {
    "FAILOVER_REMOTE": ("Planned failover to the other site", "source", True),
    "UNPLANNED_FAILOVER_LOCAL": ("Unplanned failover (source site is down)", "target", True),
    "REPROTECT_LOCAL": ("Reprotect (replicate from this site to the other)", "any", True),
    "FAILBACK_LOCAL": ("Failback, bringing back the data written at the DR site", "source", True),
    "ACTION_FAILBACK_DISCARD_CHANGES_LOCAL": ("Failback, discarding the data written at the DR site", "source", True),
    "SUSPEND": ("Suspend replication", "source", False),
    "RESUME": ("Resume replication", "source", False),
    "SYNC": ("Sync now", "source", False),
}


# ------------------------------------------------------------------ sides
class Side:
    def __init__(self, store: ClusterStore, spec: ClusterSpec, cluster_id: str, label: str):
        self.store, self.spec, self.cluster_id, self.label = store, spec, cluster_id, label

    @property
    def name(self):
        return self.store.name


def sides(store: ClusterStore, spec: ClusterSpec, r: PowerScaleReplication) -> Tuple[Side, Optional[Side]]:
    local = Side(store, spec, r.local_cluster_id, "local")
    if not r.peer or r.peer == store.name:
        return local, None
    from ..store import get_store
    ps_ = get_store(r.peer)
    if not (ps_.install_dir / "auth" / "kubeconfig").exists():
        raise RuntimeError(f"peer cluster {r.peer} has no kubeconfig here (install it with the app, or connect it)")
    return local, Side(ps_, ps_.load(), r.remote_cluster_id, "peer")


def api_endpoint(store: ClusterStore) -> Dict:
    """server + CA of a cluster, from the kubeconfig the app already uses for it."""
    kc = yaml.safe_load(Path(kube.kubeconfig(store)).read_text()) or {}
    ctx_name = kc.get("current-context")
    ctx = next((c["context"] for c in kc.get("contexts", []) if c.get("name") == ctx_name), None) or (kc.get("contexts") or [{}])[0].get("context", {})
    cl = next((c["cluster"] for c in kc.get("clusters", []) if c.get("name") == ctx.get("cluster")), None) or (kc.get("clusters") or [{}])[0].get("cluster", {})
    ca = cl.get("certificate-authority-data", "")
    if not ca and cl.get("certificate-authority"):
        try:
            ca = base64.b64encode(Path(cl["certificate-authority"]).read_bytes()).decode()
        except Exception:
            ca = ""
    return {"server": cl.get("server", ""), "ca": ca, "insecure": bool(cl.get("insecure-skip-tls-verify"))}


def sa_kubeconfig(side: Side) -> str:
    """kubeconfig for the peer controller, using this cluster's replication service-account token."""
    sec = ops.get(side.store, side.spec, "secret", SA_SECRET, ns=REPL_NS)
    tok = ((sec.get("data") or {}).get("token") or "")
    if not tok:
        raise RuntimeError(f"{side.name}: secret {REPL_NS}/{SA_SECRET} has no token yet")
    ep = api_endpoint(side.store)
    cluster = {"server": ep["server"]}
    if ep["ca"]:
        cluster["certificate-authority-data"] = ep["ca"]
    elif ep["insecure"]:
        cluster["insecure-skip-tls-verify"] = True
    doc = {"apiVersion": "v1", "kind": "Config", "current-context": "replication",
           "clusters": [{"name": side.cluster_id, "cluster": cluster}],
           "users": [{"name": "dell-replication-controller-sa", "user": {"token": base64.b64decode(tok).decode()}}],
           "contexts": [{"name": "replication", "context": {"cluster": side.cluster_id, "user": "dell-replication-controller-sa"}}]}
    return yaml.safe_dump(doc, sort_keys=False)


def _can_list_rgs(kubeconfig_text: str, spec: ClusterSpec) -> Tuple[bool, str]:
    """Use the generated kubeconfig from the installer, the way the peer controller will."""
    d = Path(tempfile.mkdtemp(dir=IMPORTED_DIR if IMPORTED_DIR.exists() else ("/dev/shm" if os.path.isdir("/dev/shm") else None), prefix="repl-"))
    try:
        os.chmod(d, 0o700)
        f = d / "kubeconfig"
        f.write_text(kubeconfig_text)
        f.chmod(0o600)
        import subprocess
        env = {"KUBECONFIG": str(f), "HOME": str(d), "PATH": os.environ.get("PATH", "")}
        r = subprocess.run([kube.oc_bin(spec), "auth", "can-i", "list", RG_KIND], env=env, capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip()
        return out.startswith("yes"), out[-160:]
    finally:
        for p in d.iterdir():
            p.unlink()
        d.rmdir()


def controller_state(side: Side) -> Dict:
    dep = ops.get_opt(side.store, side.spec, "deployment", "dell-replication-controller-manager", ns=REPL_NS)
    cm = ops.get_opt(side.store, side.spec, "configmap", CM_NAME, ns=REPL_NS)
    conf = {}
    try:
        conf = yaml.safe_load(((cm or {}).get("data") or {}).get("config.yaml", "")) or {}
    except Exception:
        pass
    st = (dep or {}).get("status") or {}
    managed = ""
    if dep:
        ann = dep["metadata"].get("annotations") or {}
        managed = "helm" if ann.get("meta.helm.sh/release-name") else ("operator" if dep["metadata"].get("ownerReferences") else "other")
    return {"installed": bool(dep), "ready": st.get("readyReplicas", 0), "replicas": (dep or {}).get("spec", {}).get("replicas", 0),
            "managed_by": managed, "cluster_id": conf.get("clusterId", ""), "targets": conf.get("targets") or [],
            "image": ((dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers") or [{}])[0].get("image", "") if dep else ""}


def _sidecar(side: Side) -> Tuple[bool, Dict]:
    inst = ps.find_install(side.store, side.spec)
    if not inst["installed"]:
        return False, inst
    dep = ops.get(side.store, side.spec, "deployment", inst["controller"], ns=inst["namespace"])
    names = [c.get("name") for c in dep["spec"]["template"]["spec"].get("containers", [])]
    return "dell-csi-replicator" in names, inst


# ------------------------------------------------------------------ StorageClasses
def class_pair(cfg: PowerScaleSpec) -> Tuple[Dict, Dict]:
    """(local SC, remote SC). For single-cluster replication both go to this cluster (remote id 'self')."""
    r = cfg.replication
    single = not r.peer
    remote_id = "self" if single else r.remote_cluster_id
    local_id = "self" if single else r.local_cluster_id
    remote_name = r.remote_class_name if not single else (r.remote_class_name if r.remote_class_name != r.class_name else f"{r.class_name}-tgt")

    def sc(name, other, other_id, arr, other_arr, zone, other_zone, path, az, other_az):
        p = {f"{PREFIX}/isReplicationEnabled": "true", f"{PREFIX}/remoteStorageClassName": other,
             f"{PREFIX}/remoteClusterID": other_id, f"{PREFIX}/remoteSystem": other_arr, f"{PREFIX}/rpo": r.rpo,
             f"{PREFIX}/ignoreNamespaces": "true" if r.ignore_namespaces else "false",
             f"{PREFIX}/volumeGroupPrefix": r.volume_group_prefix, f"{PREFIX}/remoteAccessZone": other_zone,
             "ClusterName": arr, "AccessZone": zone, "IsiPath": path, "RootClientEnabled": "false", "csi.storage.k8s.io/fstype": "nfs"}
        if az:
            p["AzServiceIP"] = az
        if other_az:
            p[f"{PREFIX}/remoteAzServiceIP"] = other_az
        return {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": name}, "provisioner": ps.DRIVER,
                "reclaimPolicy": "Delete", "allowVolumeExpansion": True, "volumeBindingMode": "Immediate", "parameters": p}
    a = sc(r.class_name, remote_name, remote_id, r.source_array, r.target_array, r.source_zone, r.target_zone, r.source_path,
           r.source_az_service_ip, r.target_az_service_ip)
    b = sc(remote_name, r.class_name, local_id, r.target_array, r.source_array, r.target_zone, r.source_zone, r.target_path,
           r.target_az_service_ip, r.source_az_service_ip)
    return a, b


# ------------------------------------------------------------------ checks
def validate(cfg: PowerScaleSpec, local_name: str) -> List[str]:
    r = cfg.replication
    errs = []
    if r.peer and r.peer == local_name:
        errs.append("the peer must be another cluster (leave it empty for single-cluster replication)")
    if r.peer:
        for n, v in (("this cluster's ID", r.local_cluster_id), ("peer cluster ID", r.remote_cluster_id)):
            if not _ID.match(v or "") or v == "self":
                errs.append(f"{n}: lower-case letters, digits and '-' (not 'self')")
        if r.local_cluster_id and r.local_cluster_id == r.remote_cluster_id:
            errs.append("the two cluster IDs must differ")
    elif not _ID.match(r.local_cluster_id or ""):
        errs.append("this cluster's ID: lower-case letters, digits and '-'")
    if not r.source_array or not r.target_array:
        errs.append("pick the source and target arrays")
    if r.rpo not in RPOS:
        errs.append("RPO: " + ", ".join(RPOS))
    for n in (r.class_name, r.remote_class_name):
        if not ps._NAME.match(n or ""):
            errs.append(f"StorageClass name '{n}' is not valid")
    if not r.peer and r.class_name == r.remote_class_name:
        pass  # the target class becomes <name>-tgt
    if not re.match(r"^[A-Za-z0-9-]{1,20}$", r.volume_group_prefix or ""):
        errs.append("volume group prefix: letters, digits and '-', up to 20")
    for pth in (r.source_path, r.target_path):
        if not (pth or "").startswith("/ifs"):
            errs.append("isiPath must start with /ifs")
    return errs


def checks(store: ClusterStore, spec: ClusterSpec, cfg: PowerScaleSpec) -> Dict:
    rows: List[Dict] = []
    r = cfg.replication

    def add(scope, name, st, expected, actual, hint=""):
        rows.append({"host": scope, "name": name, "status": st, "expected": expected, "actual": actual, "hint": hint})

    for e in validate(cfg, store.name):
        add("input", "input", "fail", "valid", e)
    try:
        local, peer = sides(store, spec, r)
    except Exception as ex:
        add("peer", "peer cluster", "fail", "reachable", str(ex))
        return {"rows": rows}
    all_sides = [local] + ([peer] if peer else [])
    creds_by_side: Dict[str, Dict[str, Dict]] = {}
    for s in all_sides:
        try:
            has, inst = _sidecar(s)
        except Exception as ex:
            add(s.name, "cluster reachable", "fail", "yes", str(ex)[-150:])
            continue
        if not inst["installed"]:
            add(s.name, "PowerScale driver", "fail", "installed", "not installed", "Install the driver on this cluster first (Install tab)")
            continue
        add(s.name, "PowerScale driver", "pass", "installed", f"{inst['method']} {inst['namespace']} {inst['driver_version']}")
        add(s.name, "replicator sidecar", "pass" if has else "fail", "enabled", "enabled" if has else "missing",
            "" if has else "Tick 'Replication' on the Install tab of this cluster and upgrade the driver")
        release = inst["release"] if inst["method"] == "helm" else (inst["release"] or "isilon")
        creds_by_side[s.name] = {c.get("clusterName"): c for c in ps._creds(s.store, s.spec, inst["namespace"], release)}
        cs = controller_state(s)
        if cs["installed"]:
            add(s.name, "replication controller", "pass" if cs["ready"] else "warn", "running", f"{cs['ready']}/{cs['replicas']} ready ({cs['managed_by']})")
            if cs["cluster_id"] and cs["cluster_id"] != s.cluster_id:
                add(s.name, "controller clusterId", "fail", s.cluster_id, cs["cluster_id"],
                    "This cluster is already wired with another ID; use that ID here")
        else:
            if inst["method"] == "operator":
                add(s.name, "replication controller", "pass", "running", "will be enabled in the ContainerStorageModule")
            else:
                rv = r.chart_version or dellbundle.matrix(inst["driver_version"].lstrip("v")).get("replication", "")
                try:
                    dellbundle.path_of("csm-replication", rv)
                    add(s.name, "replication controller", "pass", "running", f"will be installed from csm-replication {rv}")
                except Exception as ex:
                    add(s.name, "replication controller", "fail", "chart in the bundle", f"csm-replication {rv or '?'} missing", str(ex))
        # classes
        for d in class_pair(cfg)[0 if s is local else 1:(1 if s is local else 2)] if peer else class_pair(cfg):
            cur = ops.get_opt(s.store, s.spec, "storageclass", d["metadata"]["name"])
            if cur:
                same, why = ps._same_class(cur, d)
                add(s.name, f"StorageClass {d['metadata']['name']}", "pass" if same else "fail", "new or unchanged",
                    "exists, unchanged" if same else f"exists with other settings: {why}", "" if same else "Pick another class name")
            else:
                add(s.name, f"StorageClass {d['metadata']['name']}", "pass", "new or unchanged", "will be created")
    # arrays in the driver secret on every side
    for s in all_sides:
        have = creds_by_side.get(s.name)
        if have is None:
            continue
        for arr in (r.source_array, r.target_array):
            if arr in have:
                add(s.name, f"array {arr} in driver secret", "pass", "listed", "listed")
            elif any(arr in (creds_by_side.get(o.name) or {}) for o in all_sides if o is not s):
                add(s.name, f"array {arr} in driver secret", "pass", "listed", "will be copied from the other cluster's secret")
            else:
                add(s.name, f"array {arr} in driver secret", "fail", "listed", "missing on every cluster",
                    "Add this array on the Install tab (the driver needs both arrays on both sides)")
    # the arrays themselves: SyncIQ
    seen = {}
    for s in all_sides:
        for n, c in (creds_by_side.get(s.name) or {}).items():
            seen.setdefault(n, c)
    for arr, zone, path in dict.fromkeys([(r.source_array, r.source_zone, r.source_path), (r.target_array, r.target_zone, r.target_path)]):
        c = seen.get(arr)
        if not c:
            continue
        arows, facts = onefs.check_array({"name": arr, "endpoint": str(c.get("endpoint", "")), "port": c.get("endpointPort") or 8080,
                                          "username": c.get("username", ""), "password": c.get("password", ""), "zone": zone,
                                          "isi_path": path, "az_service_ip": ""}, need_replication=True)
        rows += [x for x in arows if x["name"].split(" ")[0] in ("TCP", "authentication", "license", "API", "SyncIQ", "access", "isiPath", "DNS")]
    if r.source_array == r.target_array:
        add("arrays", "source and target array", "warn", "different arrays", r.source_array,
            "Replicating to the same array only makes sense for tests (different access zones or paths)")
    return {"rows": rows}


# ------------------------------------------------------------------ setup
def _ensure_controller(ctx: JobContext, side: Side, cfg: PowerScaleSpec):
    cs = controller_state(side)
    inst = ps.find_install(side.store, side.spec)
    if cs["installed"]:
        ctx.log(f"{side.name}: replication controller present ({cs['managed_by']})")
    elif inst["method"] == "operator":
        cr = ops.get(side.store, side.spec, "containerstoragemodule", inst["release"], ns=inst["namespace"])
        mods = (cr.get("spec") or {}).get("modules") or []
        for m in mods:
            if m.get("name") == "replication":
                m["enabled"] = True
        ctx.log(f"{side.name}: enabling the replication module in ContainerStorageModule {inst['release']}")
        ops.patch(side.store, side.spec, f"containerstoragemodule/{inst['release']}", {"spec": {"modules": mods}}, ns=inst["namespace"])
    else:
        rv = cfg.replication.chart_version or dellbundle.matrix(inst["driver_version"].lstrip("v")).get("replication", "")
        tgz = dellbundle.path_of("csm-replication", rv)
        values = dellbundle.chart_values("csm-replication", rv)
        if cfg.image_registry:
            values["image"] = ps._registry_rewrite(values.get("image", ""), cfg.image_registry)
        vf = ps._work(side.store) / "replication-values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False))
        vf.chmod(0o600)
        ctx.log(f"{side.name}: helm upgrade --install replication csm-replication-{rv}.tgz -n {REPL_NS}")
        r = ps.helm(side.store, ["upgrade", "--install", "replication", str(tgz), "-n", REPL_NS, "--create-namespace", "-f", str(vf),
                                 "--timeout", "10m"], timeout=900, check=False)
        for line in (r.stdout + r.stderr).splitlines()[-8:]:
            ctx.log("  " + line)
        if r.returncode != 0:
            raise RuntimeError(f"{side.name}: helm install of csm-replication failed")
    ops.wait_for(f"{side.name}: replication controller ready", lambda: controller_state(side)["ready"] >= 1, timeout=900, interval=10, log=ctx.log)
    ops.wait_for(f"{side.name}: replication service-account token",
                 lambda: bool(((ops.get(side.store, side.spec, "secret", SA_SECRET, ns=REPL_NS).get("data") or {}).get("token"))),
                 timeout=300, interval=5, log=ctx.log)


def _merge_creds(ctx: JobContext, sides_: List[Side], arrays: List[str]):
    """Every array must be in the driver secret on every cluster; copy missing entries across."""
    info = {}
    for s in sides_:
        inst = ps.find_install(s.store, s.spec)
        release = inst["release"] if inst["method"] == "helm" else (inst["release"] or "isilon")
        info[s.name] = (inst, release, ps._creds(s.store, s.spec, inst["namespace"], release))
    pool = {}
    for _, (_, _, creds) in info.items():
        for c in creds:
            pool.setdefault(c.get("clusterName"), c)
    for s in sides_:
        inst, release, creds = info[s.name]
        names = {c.get("clusterName") for c in creds}
        add = [dict(pool[a], isDefault=False) for a in arrays if a not in names and a in pool]
        if not add:
            continue
        doc = {"isilonClusters": creds + add}
        ctx.log(f"{s.name}: adding array(s) {', '.join(a['clusterName'] for a in add)} to secret {release}-creds")
        ops.patch(s.store, s.spec, f"secret/{release}-creds",
                  {"data": {"config": base64.b64encode(yaml.safe_dump(doc, sort_keys=False).encode()).decode()}}, ns=inst["namespace"])


def _write_config(ctx: JobContext, side: Side, target: Optional[Side]):
    cs = controller_state(side)
    targets = [t for t in cs["targets"] if not target or t.get("clusterId") != target.cluster_id]
    if target:
        kc = sa_kubeconfig(target)
        ok, why = _can_list_rgs(kc, side.spec)
        if not ok:
            raise RuntimeError(f"the replication identity of {target.name} cannot list replication groups there: {why}")
        ctx.log(f"{side.name}: secret {REPL_NS}/{target.cluster_id} -> {target.name} (service account dell-replication-controller-sa; verified)")
        ops.apply(side.store, side.spec, [{"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                                           "metadata": {"name": target.cluster_id, "namespace": REPL_NS},
                                           "data": {"data": base64.b64encode(kc.encode()).decode()}}])
        targets.append({"clusterId": target.cluster_id, "address": api_endpoint(target.store)["server"], "secretRef": target.cluster_id})
    old = ops.get_opt(side.store, side.spec, "configmap", CM_NAME, ns=REPL_NS) or {}
    try:
        conf = yaml.safe_load(((old.get("data") or {}).get("config.yaml")) or "") or {}
    except Exception:
        conf = {}
    conf["clusterId"] = side.cluster_id
    conf["targets"] = targets
    conf.setdefault("CSI_LOG_LEVEL", "INFO")
    ctx.log(f"{side.name}: {CM_NAME}: clusterId={side.cluster_id} targets=" + (", ".join(t["clusterId"] for t in targets) or "none"))
    ops.patch(side.store, side.spec, f"configmap/{CM_NAME}", {"data": {"config.yaml": yaml.safe_dump(conf, sort_keys=False)}}, ns=REPL_NS)


def job_setup(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, cfg: PowerScaleSpec):
    errs = validate(cfg, store.name)
    if errs:
        raise RuntimeError("; ".join(errs))
    r = cfg.replication
    local, peer = sides(store, spec, r)
    all_sides = [local] + ([peer] if peer else [])
    ctx.log(f"Replication {store.name} ({r.local_cluster_id}) -> " + (f"{peer.name} ({r.remote_cluster_id})" if peer else "same cluster")
            + f", arrays {r.source_array} -> {r.target_array}, RPO {r.rpo}")
    for s in all_sides:
        has, inst = _sidecar(s)
        if not has:
            raise RuntimeError(f"{s.name}: the driver's replicator sidecar is not enabled (Install tab: tick Replication and upgrade)")
    _merge_creds(ctx, all_sides, [r.source_array, r.target_array])
    for s in all_sides:
        _ensure_controller(ctx, s, cfg)
    if peer:
        _write_config(ctx, local, peer)
        _write_config(ctx, peer, local)
    else:
        _write_config(ctx, local, None)
    a, b = class_pair(cfg)
    for s, docs in ((local, [a]), (peer, [b])) if peer else ((local, [a, b]),):
        for d in docs:
            cur = ops.get_opt(s.store, s.spec, "storageclass", d["metadata"]["name"])
            if cur:
                same, why = ps._same_class(cur, d)
                ctx.log(f"{s.name}: StorageClass {d['metadata']['name']} " + ("unchanged" if same else f"exists with other settings, left alone ({why})"))
                continue
            ctx.log(f"{s.name}: " + ops.apply(s.store, s.spec, [d]).strip())
    store.patch(lambda raw: raw.setdefault("day2", {}).setdefault("powerscale", PowerScaleSpec().model_dump()).__setitem__("replication", r.model_dump()))
    ctx.log(f"Replication ready. PVCs created with StorageClass {r.class_name} are replicated to "
            + (f"{peer.name} (class {b['metadata']['name']})" if peer else f"class {b['metadata']['name']} on this cluster")
            + "; each namespace gets a replication group (see the Replication tab).")


# ------------------------------------------------------------------ groups + actions
def _rg_row(rg: Dict, side: Side) -> Dict:
    st = rg.get("status") or {}
    link = st.get("replicationLinkState") or {}
    ann = rg["metadata"].get("annotations") or {}
    act = {}
    try:
        act = json.loads(ann.get("Action", "") or "{}")
    except Exception:
        pass
    last = st.get("lastAction") or {}
    attrs = (rg.get("spec") or {}).get("protectionGroupAttributes") or {}
    return {"cluster": side.name, "side": side.label, "name": rg["metadata"]["name"],
            "is_source": bool(link.get("isSource")), "link_state": link.get("state", ""), "last_sync": link.get("lastSuccessfulUpdate", ""),
            "link_error": link.get("errorMessage", ""), "state": st.get("state", ""), "action": (rg.get("spec") or {}).get("action", ""),
            "remote_cluster": (rg.get("spec") or {}).get("remoteClusterId", ""), "last_action": last.get("condition", ""),
            "last_action_time": last.get("time", ""), "last_error": last.get("errorMessage", ""),
            "action_annotation": act, "system": attrs.get("powerscale/systemName", ""), "remote_system": attrs.get("powerscale/remoteSystemName", ""),
            "volume_group": attrs.get("powerscale/VolumeGroupName", ""), "age": ops.age(rg["metadata"].get("creationTimestamp"))}


def groups(store: ClusterStore, spec: ClusterSpec) -> Dict:
    cfg = spec.day2.powerscale
    out = {"groups": [], "controllers": {}, "config": cfg.replication.model_dump(), "errors": [],
           "actions": {k: {"label": v[0], "on": v[1], "confirm": v[2]} for k, v in ACTIONS.items()}}
    try:
        local, peer = sides(store, spec, cfg.replication)
    except Exception as ex:
        local, peer = Side(store, spec, cfg.replication.local_cluster_id, "local"), None
        out["errors"].append(str(ex))
    for s in [local] + ([peer] if peer else []):
        try:
            out["controllers"][s.name] = controller_state(s)
            rgs = ops.get_opt(s.store, s.spec, RG_KIND) or {}
            pvcount = {}
            pvs = ops.get_opt(s.store, s.spec, "pv") or {}
            for p in pvs.get("items", []):
                g = (p["metadata"].get("annotations") or {}).get(f"{PREFIX}/replicationGroupName") or (p["metadata"].get("labels") or {}).get(f"{PREFIX}/replicationGroupName")
                if g:
                    pvcount[g] = pvcount.get(g, 0) + 1
            for rg in rgs.get("items", []):
                row = _rg_row(rg, s)
                row["pvs"] = pvcount.get(row["name"], 0)
                out["groups"].append(row)
        except Exception as ex:
            out["errors"].append(f"{s.name}: {str(ex)[-200:]}")
    return out


def _rg_side(store, spec, cluster: str) -> Side:
    cfg = spec.day2.powerscale.replication
    local, peer = sides(store, spec, cfg)
    if cluster == local.name:
        return local
    if peer and cluster == peer.name:
        return peer
    raise RuntimeError(f"{cluster} is not part of this replication setup")


def check_action(store: ClusterStore, spec: ClusterSpec, cluster: str, rg_name: str, action: str) -> Tuple[Side, Dict]:
    if action not in ACTIONS:
        raise ValueError(f"unsupported action {action}")
    side = _rg_side(store, spec, cluster)
    rg = ops.get(side.store, side.spec, RG_KIND, rg_name)
    row = _rg_row(rg, side)
    if row["action"] or row["state"].endswith("_IN_PROGRESS"):
        raise RuntimeError(f"{rg_name} is busy ({row['action'] or row['state']}); wait for it to finish")
    need = ACTIONS[action][1]
    if need == "source" and not row["is_source"]:
        raise RuntimeError(f"{ACTIONS[action][0]} runs on the source group; {rg_name} on {side.name} is the target")
    if need == "target" and row["is_source"]:
        raise RuntimeError(f"{ACTIONS[action][0]} runs on the target group; {rg_name} on {side.name} is the source")
    return side, row


def job_action(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, cluster: str, rg_name: str, action: str, timeout: int = 3600):
    side, row = check_action(store, spec, cluster, rg_name, action)
    ctx.log(f"{ACTIONS[action][0]}: {action} on replication group {rg_name} in {side.name} "
            f"(link {row['link_state']}, source={row['is_source']}, last sync {row['last_sync'] or '-'})")
    before = row["action_annotation"].get("finishTime") or row["last_action_time"]
    ops.patch(side.store, side.spec, f"{RG_KIND}/{rg_name}", {"spec": {"action": action}})
    started = time.time()
    last = ""
    while time.time() - started < timeout:
        time.sleep(5)
        cur = _rg_row(ops.get(side.store, side.spec, RG_KIND, rg_name), side)
        msg = f"state {cur['state'] or '-'}, link {cur['link_state'] or '-'}" + (f", {cur['last_action']}" if cur["last_action"] else "")
        if msg != last:
            ctx.log(msg)
            last = msg
        a = cur["action_annotation"]
        done = not cur["action"] and (a.get("name", "").upper() == action and a.get("completed")) and (a.get("finishTime") or cur["last_action_time"]) != before
        if done or (not cur["action"] and cur["state"] in ("Ready", "Error") and cur["last_action_time"] != before and action.lower() in cur["last_action"].lower()):
            if a.get("finalError") or cur["state"] == "Error" or "failed" in cur["last_action"].lower():
                raise RuntimeError(f"{action} failed: {a.get('finalError') or cur['last_error'] or cur['last_action']}")
            ctx.log(f"{action} completed: {cur['last_action'] or 'ok'}")
            return
        if not cur["action"] and not cur["state"].endswith("_IN_PROGRESS") and time.time() - started > 120 and not a:
            raise RuntimeError(f"the action was cleared without a result (unsupported by the driver?): check events on {rg_name}")
    raise RuntimeError(f"timed out after {timeout}s; the action may still be running on the array (retries last up to 1 h)")
