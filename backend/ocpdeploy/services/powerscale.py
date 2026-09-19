"""Dell PowerScale (Isilon) CSI driver: detection, pre-checks, install/upgrade (Helm or CSM Operator),
StorageClasses and uninstall. Replication and DR actions live in powerscale_repl.py.

Safety rules:
* an existing install is detected and adopted (same method, namespace and release), never replaced;
* array passwords are never stored by the app: they come with each request and only go into the
  cluster's <release>-creds secret; a blank password keeps the one already in that secret;
* StorageClass parameters are immutable, so an existing class with different parameters is a
  pre-check failure instead of an apply error half way through.
"""
import base64
import copy
import difflib
import gzip
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from ..jobs import JobContext
from ..models import ClusterSpec, PowerScaleSpec, PowerScaleArray, PowerScaleClass
from ..store import ClusterStore
from . import kube, clusterops as ops, dellbundle, onefs

DRIVER = "csi-isilon.dellemc.com"
CSM_PACKAGE = "dell-csm-operator-certified"
CSM_NAMESPACE = "dell-csm-operator"
DRIVER_IMAGE = re.compile(r"(/csi-isilon[:@]|/dell-csm-powerscale[:@])")
INFRA_TOLERATION = {"key": "node-role.kubernetes.io/infra", "operator": "Exists", "effect": "NoSchedule"}
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


# ------------------------------------------------------------------ helpers
def _work(store: ClusterStore) -> Path:
    d = store.dir / "powerscale"
    d.mkdir(exist_ok=True)
    d.chmod(0o700)
    return d


def helm(store: ClusterStore, args: List[str], timeout: int = 900, check: bool = True) -> subprocess.CompletedProcess:
    exe = dellbundle.helm_path()
    env = dict(kube.env(store))
    h = _work(store) / "helm"
    for k in ("HELM_CACHE_HOME", "HELM_CONFIG_HOME", "HELM_DATA_HOME"):
        env[k] = str(h)
    r = subprocess.run([str(exe)] + args, env=env, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"helm {args[0]} failed: {(r.stderr or r.stdout).strip()[-600:]}")
    return r


def _decode_release(secret: Dict) -> Dict:
    raw = base64.b64decode(base64.b64decode(secret["data"]["release"]))
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def helm_release(store, spec, ns: str, release: str) -> Optional[Dict]:
    """The deployed helm release, read from its secret (no helm binary needed)."""
    secs = ops.get_opt(store, spec, "secrets", ns=ns, extra=["-l", f"owner=helm,name={release}"]) or {}
    items = sorted(secs.get("items", []), key=lambda s: int((s["metadata"].get("labels") or {}).get("version", "0")))
    for s in reversed(items):
        if (s["metadata"].get("labels") or {}).get("status") in ("deployed", "failed", "pending-upgrade", "pending-install"):
            try:
                r = _decode_release(s)
            except Exception:
                continue
            c = r["chart"]["metadata"]
            return {"name": r["name"], "revision": r.get("version"), "status": r["info"]["status"],
                    "chart": c["name"], "chart_version": c["version"], "app_version": c.get("appVersion", ""),
                    "deployed": r["info"].get("last_deployed", ""), "values": r.get("config") or {}}
    return None


def _image_tag(img: str) -> str:
    return img.rsplit(":", 1)[-1] if ":" in img.rsplit("/", 1)[-1] else img.rsplit("@", 1)[-1][:19]


def find_install(store: ClusterStore, spec: ClusterSpec) -> Dict:
    """Where the driver runs and how it was installed (helm release, CSM operator, or by hand)."""
    out = {"installed": False, "csidriver": False, "method": "", "namespace": "", "release": "", "controller": "", "node": "",
           "driver_image": "", "driver_version": ""}
    out["csidriver"] = ops.get_opt(store, spec, "csidriver", DRIVER) is not None
    deps = ops.get_opt(store, spec, "deployments", extra=["-A"]) or {}
    for d in deps.get("items", []):
        imgs = [c.get("image", "") for c in d["spec"]["template"]["spec"].get("containers", [])]
        drv = next((i for i in imgs if DRIVER_IMAGE.search(i)), None)
        if not drv:
            continue
        md = d["metadata"]
        ann, labels = md.get("annotations") or {}, md.get("labels") or {}
        owners = [o.get("kind") for o in md.get("ownerReferences") or []]
        out.update(installed=True, namespace=md["namespace"], controller=md["name"], driver_image=drv, driver_version=_image_tag(drv))
        if "ContainerStorageModule" in owners:
            out["method"] = "operator"
            out["release"] = next((o["name"] for o in md["ownerReferences"] if o.get("kind") == "ContainerStorageModule"), "")
        elif ann.get("meta.helm.sh/release-name") or labels.get("app.kubernetes.io/managed-by") == "Helm":
            out["method"] = "helm"
            out["release"] = ann.get("meta.helm.sh/release-name", "")
        else:
            out["method"] = "manual"
        break
    if out["installed"]:
        dss = ops.get_opt(store, spec, "daemonsets", ns=out["namespace"]) or {}
        for ds in dss.get("items", []):
            if any(DRIVER_IMAGE.search(c.get("image", "")) for c in ds["spec"]["template"]["spec"].get("containers", [])):
                out["node"] = ds["metadata"]["name"]
    return out


def _creds(store, spec, ns: str, release: str) -> List[Dict]:
    s = ops.get_opt(store, spec, "secret", f"{release}-creds", ns=ns)
    if not s or "config" not in (s.get("data") or {}):
        return []
    try:
        doc = yaml.safe_load(base64.b64decode(s["data"]["config"]).decode()) or {}
    except Exception:
        return []
    return doc.get("isilonClusters") or []


def _certs(store, spec, ns: str, release: str, count: int) -> List[str]:
    pems = []
    for i in range(max(count, 1)):
        s = ops.get_opt(store, spec, "secret", f"{release}-certs-{i}", ns=ns)
        raw = ((s or {}).get("data") or {}).get(f"cert-{i}", "")
        pems.append(base64.b64decode(raw).decode(errors="replace").strip() if raw else "")
    return pems


def _classes(store, spec) -> List[Dict]:
    scs = ops.get_opt(store, spec, "storageclasses") or {}
    return [s for s in scs.get("items", []) if s.get("provisioner") == DRIVER]


def _pods(store, spec, ns: str) -> List[Dict]:
    pods = ops.get_opt(store, spec, "pods", ns=ns) or {}
    out = []
    for p in pods.get("items", []):
        cs = (p.get("status") or {}).get("containerStatuses") or []
        out.append({"name": p["metadata"]["name"], "phase": (p.get("status") or {}).get("phase", ""),
                    "ready": f"{sum(1 for c in cs if c.get('ready'))}/{len(cs)}",
                    "restarts": sum(c.get("restartCount", 0) for c in cs), "node": p["spec"].get("nodeName", ""),
                    "waiting": next((c["state"]["waiting"].get("reason", "") for c in cs if (c.get("state") or {}).get("waiting")), ""),
                    "age": ops.age(p["metadata"].get("creationTimestamp"))})
    return out


# ------------------------------------------------------------------ status
def status(store: ClusterStore, spec: ClusterSpec) -> Dict:
    inst = find_install(store, spec)
    out: Dict = {"install": inst, "pods": [], "arrays": [], "classes": [], "snapshot_classes": [], "pv_count": 0,
                 "helm": None, "csm": None, "operator": _operator_state(store, spec)}
    for s in _classes(store, spec):
        ann = s["metadata"].get("annotations") or {}
        out["classes"].append({"name": s["metadata"]["name"], "parameters": s.get("parameters") or {},
                               "reclaim_policy": s.get("reclaimPolicy", ""), "binding_mode": s.get("volumeBindingMode", ""),
                               "expansion": bool(s.get("allowVolumeExpansion")),
                               "default": ann.get("storageclass.kubernetes.io/is-default-class") == "true",
                               "replication": (s.get("parameters") or {}).get("replication.storage.dell.com/isReplicationEnabled") == "true"})
    vsc = ops.get_opt(store, spec, "volumesnapshotclasses") or {}
    out["snapshot_classes"] = [{"name": v["metadata"]["name"], "deletion_policy": v.get("deletionPolicy", "")}
                               for v in vsc.get("items", []) if v.get("driver") == DRIVER]
    pvs = ops.get_opt(store, spec, "pv") or {}
    mine = [p for p in pvs.get("items", []) if ((p.get("spec") or {}).get("csi") or {}).get("driver") == DRIVER]
    out["pv_count"] = len(mine)
    out["pv_bound"] = sum(1 for p in mine if (p.get("status") or {}).get("phase") == "Bound")
    if inst["installed"]:
        ns = inst["namespace"]
        out["pods"] = _pods(store, spec, ns)
        release = inst["release"] if inst["method"] == "helm" else (inst["release"] or "isilon")
        for c in _creds(store, spec, ns, release if inst["method"] == "helm" else "isilon"):
            out["arrays"].append({k: c.get(k) for k in ("clusterName", "endpoint", "endpointPort", "username", "isDefault",
                                                       "skipCertificateValidation", "isiPath")})
        if inst["method"] == "helm" and inst["release"]:
            rel = helm_release(store, spec, ns, inst["release"])
            if rel:
                out["helm"] = {k: v for k, v in rel.items() if k != "values"}
        if inst["method"] == "operator":
            out["csm"] = _csm_state(store, spec, ns, inst["release"])
    return out


def _operator_state(store, spec) -> Dict:
    subs = [s for s in (_safe(lambda: ops.subscriptions(store, spec), []) or []) if s["package"] == CSM_PACKAGE]
    if not subs:
        pm = ops.get_opt(store, spec, "packagemanifest", CSM_PACKAGE, ns="openshift-marketplace")
        st = (pm or {}).get("status") or {}
        ch = next((c for c in st.get("channels", []) if c.get("name") == st.get("defaultChannel")), None)
        return {"installed": False, "available": bool(pm), "catalog": st.get("catalogSource", ""),
                "channel": st.get("defaultChannel", ""), "csv": (ch or {}).get("currentCSV", "")}
    s = subs[0]
    csv = ops.get_opt(store, spec, "csv", s["installed_csv"], ns=s["namespace"]) if s["installed_csv"] else None
    return {"installed": True, "available": True, "namespace": s["namespace"], "channel": s["channel"], "csv": s["installed_csv"],
            "phase": ((csv or {}).get("status") or {}).get("phase", s["state"])}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _csm_state(store, spec, ns: str, name: str) -> Optional[Dict]:
    cr = ops.get_opt(store, spec, "containerstoragemodule", name, ns=ns) if name else None
    if not cr:
        return None
    st = cr.get("status") or {}
    return {"name": name, "state": st.get("state", ""), "version": (cr.get("spec") or {}).get("version", ""),
            "controller": st.get("controllerStatus") or {}, "node": st.get("nodeStatus") or {},
            "modules": [{"name": m.get("name"), "enabled": m.get("enabled")} for m in (cr.get("spec") or {}).get("modules", [])]}


# ------------------------------------------------------------------ prefill (adopt an existing install)
def prefill(store: ClusterStore, spec: ClusterSpec) -> Dict:
    """Form values: from the live install when there is one, else what was saved, else defaults."""
    saved = spec.day2.powerscale
    inst = find_install(store, spec)
    cfg = PowerScaleSpec(**saved.model_dump())
    src = "saved" if saved.arrays else "defaults"
    if inst["installed"] and inst["method"] in ("helm", "operator"):
        src = "cluster"
        cfg.method = inst["method"]
        cfg.namespace = inst["namespace"]
        if inst["method"] == "helm":
            cfg.release = inst["release"] or "isilon"
            rel = helm_release(store, spec, inst["namespace"], cfg.release)
            v = (rel or {}).get("values") or {}
            if rel:
                cfg.chart_version = rel["chart_version"]
            zone = _values_to_cfg(v, cfg)
        else:
            cfg.release = "isilon"
            zone = None
            cr = ops.get_opt(store, spec, "containerstoragemodule", inst["release"], ns=inst["namespace"]) or {}
            _csm_to_cfg(cr, cfg)
        creds = _creds(store, spec, cfg.namespace, cfg.release)
        certs = _certs(store, spec, cfg.namespace, cfg.release, len(creds))
        if creds:
            ca = next((c for c in certs if c), "")
            cfg.arrays = [PowerScaleArray(name=c.get("clusterName", ""), endpoint=str(c.get("endpoint", "")),
                                          port=int(c.get("endpointPort") or 8080), username=c.get("username", ""),
                                          is_default=str(c.get("isDefault", "")).lower() == "true",
                                          skip_cert_validation=str(c.get("skipCertificateValidation", "true")).lower() != "false",
                                          ca_pem=ca if str(c.get("skipCertificateValidation", "true")).lower() == "false" else "",
                                          isi_path=c.get("isiPath") or "/ifs/data/csi",
                                          access_zone=zone or (cfg.arrays[0].access_zone if cfg.arrays else "System"),
                                          replication_certificate_id=str(c.get("replicationCertificateID") or ""))
                          for c in creds]
        classes = []
        for s in _classes(store, spec):
            p = s.get("parameters") or {}
            if p.get("replication.storage.dell.com/isReplicationEnabled") == "true":
                continue
            ann = s["metadata"].get("annotations") or {}
            classes.append(PowerScaleClass(name=s["metadata"]["name"], array=p.get("ClusterName", ""), access_zone=p.get("AccessZone", "System"),
                                           isi_path=p.get("IsiPath", ""), az_service_ip=p.get("AzServiceIP", ""),
                                           root_client_enabled=str(p.get("RootClientEnabled", "false")).lower() == "true",
                                           reclaim_policy=s.get("reclaimPolicy", "Delete"), binding_mode=s.get("volumeBindingMode", "Immediate"),
                                           default=ann.get("storageclass.kubernetes.io/is-default-class") == "true"))
        cfg.classes = classes
        vsc = [v["metadata"]["name"] for v in (ops.get_opt(store, spec, "volumesnapshotclasses") or {}).get("items", []) if v.get("driver") == DRIVER]
        if vsc:
            cfg.snapshot_class = vsc[0]
    if not cfg.arrays:
        cfg.arrays = [PowerScaleArray(is_default=True)]
    if not cfg.classes:
        cfg.classes = [PowerScaleClass(name="isilon", array=cfg.arrays[0].name)]
    if not cfg.chart_version:
        latest = dellbundle.latest("csi-isilon")
        cfg.chart_version = latest["version"] if latest else ""
    return {"config": cfg.model_dump(), "source": src, "install": inst}


def _values_to_cfg(v: Dict, cfg: PowerScaleSpec):
    ctl = v.get("controller") or {}
    cfg.auth_type = int(v.get("isiAuthType", cfg.auth_type))
    cfg.enable_quota = bool(v.get("enableQuota", cfg.enable_quota))
    cfg.snapshots = bool((ctl.get("snapshot") or {}).get("enabled", cfg.snapshots))
    cfg.resizer = bool((ctl.get("resizer") or {}).get("enabled", cfg.resizer))
    cfg.controller_count = int(ctl.get("controllerCount", cfg.controller_count))
    cfg.volume_name_prefix = ctl.get("volumeNamePrefix", cfg.volume_name_prefix)
    cfg.log_level = v.get("logLevel", cfg.log_level) if v.get("logLevel") in ("error", "warn", "info", "debug") else cfg.log_level
    cfg.replication.enabled = bool((ctl.get("replication") or {}).get("enabled", False)) or cfg.replication.enabled
    tol = ((v.get("node") or {}).get("tolerations") or [])
    cfg.infra_nodes = any(t.get("key") == INFRA_TOLERATION["key"] for t in tol)
    zone, path = v.get("isiAccessZone"), v.get("isiPath")
    img = ((v.get("images") or {}).get("driver") or {}).get("image", "")
    m = re.match(r"^([^/]+(?:/[^/]+)*)/container-storage-modules/csi-isilon", img)
    if m and not img.startswith("quay.io/dell/"):
        cfg.image_registry = m.group(1)
    return zone


def _csm_to_cfg(cr: Dict, cfg: PowerScaleSpec):
    d = (cr.get("spec") or {}).get("driver") or {}
    envs = {e["name"]: e.get("value", "") for sec in ("common", "controller", "node") for e in ((d.get(sec) or {}).get("envs") or [])}
    cfg.auth_type = int(envs.get("X_CSI_ISI_AUTH_TYPE", cfg.auth_type) or 1)
    cfg.enable_quota = envs.get("X_CSI_ISI_QUOTA_ENABLED", "true") == "true"
    cfg.controller_count = int(d.get("replicas", cfg.controller_count))
    tol = ((d.get("node") or {}).get("tolerations") or [])
    cfg.infra_nodes = any(t.get("key") == INFRA_TOLERATION["key"] for t in tol)
    mods = {m.get("name"): m for m in (cr.get("spec") or {}).get("modules", [])}
    cfg.replication.enabled = bool((mods.get("replication") or {}).get("enabled"))


# ------------------------------------------------------------------ validation + helm values
def validate(cfg: PowerScaleSpec) -> List[str]:
    errs = []
    if not cfg.arrays:
        errs.append("add at least one array")
    names = [a.name for a in cfg.arrays]
    if len(set(names)) != len(names):
        errs.append("array names must be unique")
    for a in cfg.arrays:
        if not a.name or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", a.name):
            errs.append(f"array name '{a.name}': letters, digits, - _ . only")
        if not a.endpoint or not a.username:
            errs.append(f"array {a.name}: endpoint and API user are required")
        if not a.isi_path.startswith("/ifs"):
            errs.append(f"array {a.name}: isiPath must start with /ifs")
        if a.replication_certificate_id and not re.match(r"^[A-Za-z0-9]{8,128}$", a.replication_certificate_id):
            errs.append(f"array {a.name}: replicationCertificateID is the certificate ID from 'isi sync certificates server list'")
        if not a.skip_cert_validation and "BEGIN CERTIFICATE" not in a.ca_pem:
            errs.append(f"array {a.name}: certificate validation is on, so its CA certificate (PEM) is needed")
    if len(cfg.arrays) > 1 and sum(1 for a in cfg.arrays if a.is_default) != 1:
        errs.append("mark exactly one array as default")
    for n in (cfg.namespace, cfg.release):
        if not _NAME.match(n or ""):
            errs.append(f"'{n}' is not a valid Kubernetes name")
    cn = [c.name for c in cfg.classes]
    if len(set(cn)) != len(cn):
        errs.append("StorageClass names must be unique")
    for c in cfg.classes:
        if not _NAME.match(c.name):
            errs.append(f"StorageClass '{c.name}' is not a valid name")
        if c.array and c.array not in names:
            errs.append(f"StorageClass {c.name} uses array '{c.array}', which is not in the list")
    if sum(1 for c in cfg.classes if c.default) > 1:
        errs.append("only one StorageClass can be the default")
    if not 1 <= cfg.controller_count <= 5:
        errs.append("controller replicas: 1 to 5")
    if cfg.method == "helm" and not cfg.chart_version:
        errs.append("pick the csi-isilon chart version (upload it on the Bundle tab first)")
    if cfg.image_registry and not re.match(r"^[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._/-]+)?$", cfg.image_registry):
        errs.append("image registry: host[:port][/path]")
    return errs


def _default_array(cfg: PowerScaleSpec) -> PowerScaleArray:
    return next((a for a in cfg.arrays if a.is_default), cfg.arrays[0])


def _registry_rewrite(img: str, reg: str) -> str:
    """quay.io/dell/container-storage-modules/x:tag -> <reg>/dell/container-storage-modules/x:tag (same for registry.k8s.io)."""
    if not reg:
        return img
    host, _, rest = img.partition("/")
    return f"{reg.rstrip('/')}/{rest}" if "." in host or ":" in host else img


def helm_values(cfg: PowerScaleSpec, chart_defaults: Dict, current: Optional[Dict] = None) -> Dict:
    """Values for helm: the chart's defaults, the current release's own settings on top (adopt), then the form.
    Images and 'version' always follow the target chart (so an upgrade really upgrades)."""
    v = copy.deepcopy(chart_defaults)
    if current:
        keep = copy.deepcopy(current)
        keep.pop("images", None)
        keep.pop("version", None)
        _deep_merge(v, keep)
    da = _default_array(cfg)
    ca_count = len({a.ca_pem.strip() for a in cfg.arrays if not a.skip_cert_validation and a.ca_pem.strip()})
    ctl = v.setdefault("controller", {})
    node = v.setdefault("node", {})
    v.update({
        "openshift": True,
        "logLevel": cfg.log_level,
        "isiAuthType": cfg.auth_type,
        "isiAccessZone": da.access_zone,
        "isiPath": da.isi_path,
        "endpointPort": da.port,
        "skipCertificateValidation": da.skip_cert_validation,
        "enableQuota": cfg.enable_quota,
        "certSecretCount": max(1, ca_count),
    })
    ctl["controllerCount"] = cfg.controller_count
    ctl["volumeNamePrefix"] = cfg.volume_name_prefix
    ctl.setdefault("snapshot", {})["enabled"] = cfg.snapshots
    ctl.setdefault("resizer", {})["enabled"] = cfg.resizer
    ctl.setdefault("replication", {})["enabled"] = cfg.replication.enabled
    tol = [t for t in (node.get("tolerations") or []) if t.get("key") != INFRA_TOLERATION["key"]]
    if cfg.infra_nodes:
        tol.append(dict(INFRA_TOLERATION))
    node["tolerations"] = tol or None
    if cfg.image_registry:
        for comp in (v.get("images") or {}).values():
            if isinstance(comp, dict) and comp.get("image"):
                comp["image"] = _registry_rewrite(comp["image"], cfg.image_registry)
    return v


def _deep_merge(dst: Dict, src: Dict):
    for k, val in src.items():
        if isinstance(val, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], val)
        else:
            dst[k] = val


def creds_doc(cfg: PowerScaleSpec, passwords: Dict[str, str], existing: List[Dict]) -> Tuple[Dict, List[str]]:
    """isilon-creds 'config' content. Blank password = keep the one already in the cluster secret."""
    old = {c.get("clusterName"): c for c in existing}
    missing = []
    out = []
    for a in cfg.arrays:
        prev = old.get(a.name) or {}
        pw = passwords.get(a.name) or prev.get("password") or ""
        if not pw:
            missing.append(a.name)
        entry = dict(prev)                 # keep keys the form does not know about
        entry.update({"clusterName": a.name, "username": a.username, "password": pw, "endpoint": a.endpoint,
                      "endpointPort": a.port, "isDefault": a.is_default or len(cfg.arrays) == 1,
                      "skipCertificateValidation": a.skip_cert_validation, "isiPath": a.isi_path,
                      "isiVolumePathPermissions": prev.get("isiVolumePathPermissions", "0777")})
        if a.replication_certificate_id:
            entry["replicationCertificateID"] = a.replication_certificate_id
        out.append(entry)
    return {"isilonClusters": out}, missing


def secret_docs(cfg: PowerScaleSpec, creds: Dict) -> List[Dict]:
    docs = [{"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
             "metadata": {"name": f"{cfg.release}-creds", "namespace": cfg.namespace, "labels": {"app.kubernetes.io/managed-by-ocpdeploy": "true"}},
             "data": {"config": base64.b64encode(yaml.safe_dump(creds, sort_keys=False).encode()).decode()}}]
    cas = []
    for a in cfg.arrays:
        pem = a.ca_pem.strip()
        if not a.skip_cert_validation and pem and pem not in cas:
            cas.append(pem)
    for i, pem in enumerate(cas or [""]):
        docs.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                     "metadata": {"name": f"{cfg.release}-certs-{i}", "namespace": cfg.namespace},
                     "data": {f"cert-{i}": base64.b64encode((pem + "\n").encode()).decode() if pem else ""}})
    return docs


def class_docs(cfg: PowerScaleSpec) -> List[Dict]:
    docs = []
    arrays = {a.name: a for a in cfg.arrays}
    for c in cfg.classes:
        a = arrays.get(c.array) or _default_array(cfg)
        params = {"AccessZone": c.access_zone or a.access_zone, "IsiPath": c.isi_path or a.isi_path,
                  "RootClientEnabled": "true" if c.root_client_enabled else "false", "csi.storage.k8s.io/fstype": "nfs"}
        if len(cfg.arrays) > 1 or c.array:
            params["ClusterName"] = a.name
        if c.az_service_ip or a.az_service_ip:
            params["AzServiceIP"] = c.az_service_ip or a.az_service_ip
        docs.append({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
                     "metadata": {"name": c.name, "annotations": {"storageclass.kubernetes.io/is-default-class": "true"} if c.default else {}},
                     "provisioner": DRIVER, "reclaimPolicy": c.reclaim_policy, "allowVolumeExpansion": True,
                     "volumeBindingMode": c.binding_mode, "parameters": params})
    if cfg.snapshots and cfg.snapshot_class:
        docs.append({"apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshotClass",
                     "metadata": {"name": cfg.snapshot_class}, "driver": DRIVER, "deletionPolicy": "Delete",
                     "parameters": {"IsiPath": _default_array(cfg).isi_path}})
    return docs


def _same_class(live: Dict, want: Dict) -> Tuple[bool, str]:
    diffs = []
    for k in ("provisioner", "reclaimPolicy", "volumeBindingMode"):
        if live.get(k) != want.get(k):
            diffs.append(f"{k}: {live.get(k)} -> {want.get(k)}")
    lp, wp = live.get("parameters") or {}, want.get("parameters") or {}
    for k in sorted(set(lp) | set(wp)):
        if str(lp.get(k, "")) != str(wp.get(k, "")):
            diffs.append(f"{k}: {lp.get(k, '-')} -> {wp.get(k, '-')}")
    return not diffs, "; ".join(diffs)


# ------------------------------------------------------------------ pre-checks
def cluster_checks(store: ClusterStore, spec: ClusterSpec, cfg: PowerScaleSpec) -> List[Dict]:
    rows: List[Dict] = []

    def add(name, st, expected, actual, hint=""):
        rows.append({"host": "cluster", "name": name, "status": st, "expected": expected, "actual": actual, "hint": hint})

    for e in validate(cfg):
        add("input", "fail", "valid", e)
    inst = find_install(store, spec)
    # version against Dell's tested range
    ver = spec.ocp_version
    try:
        cv = ops.get(store, spec, "clusterversion", "version")
        ver = (cv.get("status") or {}).get("desired", {}).get("version", ver)
    except Exception:
        pass
    target = cfg.chart_version if cfg.method == "helm" else ""
    mx = dellbundle.matrix(target) if target else {}
    if mx and ver:
        mm = tuple(int(x) for x in ver.split(".")[:2])
        lo, hi = (tuple(int(x) for x in s.split(".")) for s in mx["ocp"])
        ok = lo <= mm <= hi
        add("OpenShift version", "pass" if ok else "warn", f"{mx['ocp'][0]} - {mx['ocp'][1]} (tested by Dell for {target})", ver,
            "" if ok else "Newer than Dell tested; it works on homelab 4.22, but Dell suggests running cert-csi")
    else:
        add("OpenShift version", "pass", "4.x", ver or "?")
    # existing install
    if inst["installed"]:
        if inst["method"] != cfg.method:
            add("existing driver", "fail", f"none, or installed with {cfg.method}",
                f"{inst['method']} in {inst['namespace']} ({inst['driver_version']})",
                "The app will not switch install methods on a running driver; uninstall it first, or pick the same method")
        elif inst["namespace"] != cfg.namespace or (cfg.method == "helm" and inst["release"] and inst["release"] != cfg.release):
            add("existing driver", "fail", f"{cfg.namespace}/{cfg.release}", f"{inst['namespace']}/{inst['release']}",
                "Use the namespace and release of the running driver (the form is prefilled with them)")
        else:
            add("existing driver", "pass", "adopt or upgrade", f"{inst['method']} {inst['namespace']}/{inst['release']} {inst['driver_version']}",
                "The install will upgrade this release in place")
    elif inst["csidriver"]:
        add("existing driver", "warn", "none", f"CSIDriver {DRIVER} exists without a running controller",
            "A previous install left the CSIDriver object; helm may refuse to take it over")
    else:
        add("existing driver", "pass", "none", "not installed")
    # snapshot API
    crds = {c["metadata"]["name"] for c in (ops.get_opt(store, spec, "crd") or {}).get("items", [])}
    need = {"volumesnapshotclasses.snapshot.storage.k8s.io", "volumesnapshotcontents.snapshot.storage.k8s.io", "volumesnapshots.snapshot.storage.k8s.io"}
    add("snapshot CRDs", "pass" if need <= crds else ("fail" if cfg.snapshots else "warn"), "present", "present" if need <= crds else "missing")
    if cfg.replication.enabled:
        rep = {"dellcsireplicationgroups.replication.storage.dell.com", "dellcsimigrationgroups.replication.storage.dell.com"}
        add("replication CRDs", "pass" if rep <= crds else "info", "present", "present" if rep <= crds else "missing: installed from the csm-replication chart first")
    # tooling / bundle
    if cfg.method == "helm":
        try:
            h = dellbundle.helm_path()
            add("helm", "pass", "available", str(h).replace(str(dellbundle.ROOT) + "/", ""))
        except Exception as ex:
            add("helm", "fail", "available", "missing", str(ex))
        try:
            dellbundle.path_of("csi-isilon", cfg.chart_version)
            add(f"chart csi-isilon {cfg.chart_version}", "pass", "in the offline bundle", "present")
        except Exception as ex:
            add(f"chart csi-isilon {cfg.chart_version or '?'}", "fail", "in the offline bundle", "missing", str(ex))
        if cfg.replication.enabled and not (rep <= crds):
            rv = cfg.replication.chart_version or (mx.get("replication") if mx else "")
            try:
                dellbundle.path_of("csm-replication", rv)
                add(f"chart csm-replication {rv}", "pass", "in the offline bundle (for the CRDs)", "present")
            except Exception as ex:
                add(f"chart csm-replication {rv or '?'}", "fail", "in the offline bundle (for the CRDs)", "missing", str(ex))
    else:
        op = _operator_state(store, spec)
        if op.get("installed"):
            add("CSM Operator", "pass" if op.get("phase") == "Succeeded" else "warn", "Succeeded", f"{op.get('csv')} {op.get('phase')}")
        elif op.get("available"):
            add("CSM Operator", "pass", "in a catalog", f"{op.get('csv')} from {op.get('catalog')} (will be installed)")
        else:
            add("CSM Operator", "fail", "in a catalog", "package dell-csm-operator-certified not found",
                "Disconnected cluster: mirror the certified-operators catalog (dell-csm-operator-certified) or use the Helm method")
    # images
    if spec.mirror.enabled and not cfg.image_registry:
        idms = ops.get_opt(store, spec, "imagedigestmirrorsets") or {}
        itms = ops.get_opt(store, spec, "imagetagmirrorsets") or {}
        srcs = {m.get("source", "") for x in idms.get("items", []) + itms.get("items", [])
                for m in (x.get("spec") or {}).get("imageDigestMirrors", []) + (x.get("spec") or {}).get("imageTagMirrors", [])}
        need_src = ["quay.io/dell", "registry.k8s.io/sig-storage"] if cfg.method == "helm" else ["registry.connect.redhat.com/dell-emc", "registry.k8s.io/sig-storage"]
        miss = [s for s in need_src if not any(s.startswith(x) or x.startswith(s) for x in srcs)]
        add("image mirrors", "warn" if miss else "pass", "mirror for " + ", ".join(need_src), "missing " + ", ".join(miss) if miss else "configured",
            "Mirror the images listed on the Bundle tab, or set 'Image registry'" if miss else "")
    elif cfg.image_registry:
        add("image registry", "pass", "set", cfg.image_registry, "All driver images will be pulled from here; mirror the list on the Bundle tab to it")
    # storage classes that already exist
    live = {s["metadata"]["name"]: s for s in (ops.get_opt(store, spec, "storageclasses") or {}).get("items", [])}
    for d in class_docs(cfg):
        if d["kind"] != "StorageClass":
            continue
        cur = live.get(d["metadata"]["name"])
        if not cur:
            add(f"StorageClass {d['metadata']['name']}", "pass", "new or unchanged", "will be created")
            continue
        same, why = _same_class(cur, d)
        add(f"StorageClass {d['metadata']['name']}", "pass" if same else "fail", "new or unchanged",
            "exists, unchanged" if same else f"exists with other settings: {why}",
            "" if same else "StorageClass parameters cannot be changed; use another name, or delete the old class (volumes keep working)")
    return rows


def node_reachability(store: ClusterStore, spec: ClusterSpec, cfg: PowerScaleSpec) -> List[Dict]:
    """TCP tests from one schedulable worker (oc debug): API port and NFS ports of every array."""
    rows: List[Dict] = []
    nodes = ops.get(store, spec, "nodes")
    pick = None
    for n in nodes.get("items", []):
        labels = n["metadata"].get("labels") or {}
        ready = ops.conditions(n).get("Ready", {}).get("status") == "True"
        if ready and "node-role.kubernetes.io/worker" in labels and not n["spec"].get("taints") and not n["spec"].get("unschedulable"):
            pick = n["metadata"]["name"]
            break
    if not pick:
        rows.append({"host": "nodes", "name": "reachability from a node", "status": "warn", "expected": "tested", "actual": "no untainted worker",
                     "hint": "Test TCP 8080/2049/111 from a node by hand"})
        return rows
    targets = []
    for a in cfg.arrays:
        h = onefs.host_of(a.endpoint)
        targets.append((h, a.port))
        nfs = a.az_service_ip or h
        targets += [(nfs, 2049), (nfs, 111)]
    for c in cfg.classes:
        if c.az_service_ip:
            targets += [(c.az_service_ip, 2049)]
    seen, uniq = set(), []
    for t in targets:
        if t not in seen and re.match(r"^[A-Za-z0-9.-]+$", t[0]):
            seen.add(t)
            uniq.append(t)
    script = "; ".join(f"if timeout 5 bash -c '</dev/tcp/{h}/{p}' 2>/dev/null; then echo 'R {h}:{p} open'; else echo 'R {h}:{p} closed'; fi" for h, p in uniq)
    try:
        out = kube.oc(store, spec, ["debug", f"node/{pick}", "--quiet", "--", "chroot", "/host", "bash", "-c", script], timeout=240)
    except Exception as ex:
        rows.append({"host": "nodes", "name": f"oc debug node/{pick}", "status": "warn", "expected": "runs", "actual": str(ex)[-150:],
                     "hint": "Could not start a debug pod; test the ports from a node by hand"})
        return rows
    for line in out.splitlines():
        m = re.match(r"^R (\S+):(\d+) (open|closed)", line.strip())
        if not m:
            continue
        port = int(m.group(2))
        ok = m.group(3) == "open"
        what = {2049: "NFS", 111: "rpcbind"}.get(port, "OneFS API (controller)")
        rows.append({"host": "nodes", "name": f"{pick} -> {m.group(1)}:{port}", "status": "pass" if ok else ("warn" if port == 111 else "fail"),
                     "expected": f"open ({what})", "actual": m.group(3),
                     "hint": "" if ok else "Nodes must reach the array (routing, firewall, SmartConnect DNS)"})
    return rows


def full_check(store: ClusterStore, spec: ClusterSpec, cfg: PowerScaleSpec, passwords: Dict[str, str], with_nodes: bool = True) -> Dict:
    rows = cluster_checks(store, spec, cfg)
    facts = {}
    existing = {}
    inst = find_install(store, spec)
    if inst["installed"]:
        existing = {c.get("clusterName"): c for c in _creds(store, spec, inst["namespace"], cfg.release)}
    for a in cfg.arrays:
        pw = passwords.get(a.name) or (existing.get(a.name) or {}).get("password", "")
        r, f = onefs.check_array({"name": a.name, "endpoint": a.endpoint, "port": a.port, "username": a.username, "password": pw,
                                  "zone": a.access_zone, "isi_path": a.isi_path, "az_service_ip": a.az_service_ip},
                                 need_quota=cfg.enable_quota, need_snapshots=cfg.snapshots, need_replication=False)  # SyncIQ: replication checks
        rows += r
        f.pop("privileges", None)
        facts[a.name] = f
        if f.get("auth_type") is not None and f["auth_type"] != cfg.auth_type:
            rows.append({"host": f"array {a.name}", "name": "isiAuthType", "status": "fail", "expected": str(f["auth_type"]),
                         "actual": str(cfg.auth_type), "hint": f"This array needs isiAuthType {f['auth_type']} ({'session' if f['auth_type'] else 'basic'} auth)"})
        if not a.skip_cert_validation and f.get("cert") and a.ca_pem and "BEGIN" in a.ca_pem:
            if f["cert"].get("chain_pem", "").split("-----END CERTIFICATE-----")[-2:-1] and not _ca_matches(a.ca_pem, f["cert"].get("chain_pem", "")):
                rows.append({"host": f"array {a.name}", "name": "CA certificate", "status": "warn", "expected": "signs the array certificate",
                             "actual": "no match found in the presented chain", "hint": "Check the CA you pasted"})
    if with_nodes:
        try:
            rows += node_reachability(store, spec, cfg)
        except Exception as ex:
            rows.append({"host": "nodes", "name": "reachability from a node", "status": "warn", "expected": "tested", "actual": str(ex)[-150:], "hint": ""})
    return {"rows": rows, "facts": facts, "install": inst}


def _ca_matches(ca_pem: str, chain_pem: str) -> bool:
    norm = lambda s: re.sub(r"\s+", "", s)
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", ca_pem, re.S)
    chain = norm(chain_pem)
    if any(norm(b) in chain for b in blocks):
        return True
    try:
        from cryptography import x509
        leaf = x509.load_pem_x509_certificate(re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", chain_pem, re.S)[0].encode())
        return any(x509.load_pem_x509_certificate(b.encode()).subject == leaf.issuer for b in blocks)
    except Exception:
        return False


# ------------------------------------------------------------------ preview
def preview(store: ClusterStore, spec: ClusterSpec, cfg: PowerScaleSpec) -> Dict:
    """What an install/upgrade would change: helm values and rendered manifests vs the running release,
    plus the StorageClass plan. Nothing is applied."""
    if cfg.method != "helm":
        return {"method": cfg.method, "cr": csm_cr(store, spec, cfg), "classes": class_docs(cfg)}
    tgz = dellbundle.path_of("csi-isilon", cfg.chart_version)
    rel = helm_release(store, spec, cfg.namespace, cfg.release)
    values = helm_values(cfg, dellbundle.chart_values("csi-isilon", cfg.chart_version), (rel or {}).get("values"))
    vf = _work(store) / "preview-values.yaml"
    vf.write_text(yaml.safe_dump(values, sort_keys=False))
    vf.chmod(0o600)
    new = helm(store, ["template", cfg.release, str(tgz), "-n", cfg.namespace, "-f", str(vf)], timeout=120).stdout
    out = {"method": "helm", "chart_version": cfg.chart_version, "values": values, "classes": class_docs(cfg), "upgrade": bool(rel)}
    if rel:
        cur = helm(store, ["get", "manifest", cfg.release, "-n", cfg.namespace], timeout=120).stdout
        cur_vals = helm(store, ["get", "values", cfg.release, "-n", cfg.namespace, "-o", "yaml"], timeout=60).stdout
        out["from_version"] = rel["chart_version"]
        out["manifest_diff"] = "".join(difflib.unified_diff(_norm_manifest(cur).splitlines(True), _norm_manifest(new).splitlines(True),
                                                            "deployed", "new", n=2))
        out["values_diff"] = "".join(difflib.unified_diff(yaml.safe_dump(yaml.safe_load(cur_vals) or {}, sort_keys=True).splitlines(True),
                                                          yaml.safe_dump(values, sort_keys=True).splitlines(True), "deployed", "new", n=2))
    else:
        out["objects"] = [f"{d.get('kind')}/{(d.get('metadata') or {}).get('name')}" for d in yaml.safe_load_all(new) if d]
    return out


def _norm_manifest(text: str) -> str:
    docs = [d for d in yaml.safe_load_all(text) if d]
    docs.sort(key=lambda d: (d.get("kind", ""), (d.get("metadata") or {}).get("name", "")))
    return "".join(yaml.safe_dump(d, sort_keys=True) + "---\n" for d in docs)


# ------------------------------------------------------------------ install / upgrade
def _apply_secrets(ctx: JobContext, store, spec, cfg: PowerScaleSpec, passwords: Dict[str, str]):
    ctx.log(ops.apply(store, spec, [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": cfg.namespace}}]).strip())
    existing = _creds(store, spec, cfg.namespace, cfg.release)
    creds, missing = creds_doc(cfg, passwords, existing)
    if missing:
        raise RuntimeError("password needed for array(s): " + ", ".join(missing))
    kept = [a.name for a in cfg.arrays if not passwords.get(a.name)]
    ctx.log(f"Secret {cfg.release}-creds: {len(cfg.arrays)} array(s)" + (f"; kept the existing password for {', '.join(kept)}" if kept else ""))
    out = ops.apply(store, spec, secret_docs(cfg, creds))
    ctx.log(out.strip())


def _apply_classes(ctx: JobContext, store, spec, cfg: PowerScaleSpec):
    live = {s["metadata"]["name"]: s for s in (ops.get_opt(store, spec, "storageclasses") or {}).get("items", [])}
    for d in class_docs(cfg):
        name = d["metadata"]["name"]
        if d["kind"] == "StorageClass" and name in live:
            same, why = _same_class(live[name], d)
            if not same:
                ctx.log(f"StorageClass {name} exists with other parameters ({why}); left unchanged")
                continue
            ann = (live[name]["metadata"].get("annotations") or {})
            want_default = d["metadata"]["annotations"].get("storageclass.kubernetes.io/is-default-class") == "true"
            if want_default != (ann.get("storageclass.kubernetes.io/is-default-class") == "true") and want_default:
                pass  # handled below
            ctx.log(f"StorageClass {name}: unchanged")
            continue
        if d["kind"] == "VolumeSnapshotClass" and ops.get_opt(store, spec, "volumesnapshotclass", name):
            ctx.log(f"VolumeSnapshotClass {name}: exists")
            continue
        d2 = copy.deepcopy(d)
        if d2["kind"] == "StorageClass":
            d2["metadata"]["annotations"] = {}
        ctx.log(ops.apply(store, spec, [d2]).strip())
    want = next((c.name for c in cfg.classes if c.default), None)
    if want:
        from . import storage
        storage.set_default_sc(store, spec, want)
        ctx.log(f"Default StorageClass: {want}")


def _wait_driver(ctx: JobContext, store, spec, ns: str, timeout: int = 900):
    def ready():
        inst = find_install(store, spec)
        if not inst["installed"] or not inst["node"]:
            return False
        dep = ops.get(store, spec, "deployment", inst["controller"], ns=ns)
        ds = ops.get(store, spec, "daemonset", inst["node"], ns=ns)
        dst, sst = dep.get("status") or {}, ds.get("status") or {}
        return (dst.get("readyReplicas", 0) >= (dep["spec"].get("replicas") or 1) and dst.get("updatedReplicas", 0) >= (dep["spec"].get("replicas") or 1)
                and sst.get("numberReady", 0) >= sst.get("desiredNumberScheduled", 1) and sst.get("updatedNumberScheduled", 0) >= sst.get("desiredNumberScheduled", 1))

    def tick():
        ps = _pods(store, spec, ns)
        return "pods: " + ", ".join(f"{p['name'].split('-')[-2] if p['name'].count('-') > 1 else p['name']}:{p['ready']}{' ' + p['waiting'] if p['waiting'] else ''}" for p in ps[:8])
    ops.wait_for("driver pods ready", ready, timeout=timeout, interval=10, log=ctx.log, on_tick=tick)


def ensure_replication_crds(ctx: JobContext, store, spec, chart_version: str):
    text = dellbundle.chart_file("csm-replication", chart_version, "crds/replicationcrds.all.yaml")
    if not text:
        raise RuntimeError(f"csm-replication {chart_version} has no crds/replicationcrds.all.yaml")
    ctx.log(f"Applying replication CRDs from csm-replication {chart_version}")
    ctx.log(kube.oc(store, spec, ["apply", "--server-side", "--force-conflicts", "-f", "-"], input_text=text, timeout=180).strip())


def job_install(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, cfg: PowerScaleSpec, passwords: Dict[str, str]):
    errs = validate(cfg)
    if errs:
        raise RuntimeError("; ".join(errs))
    inst = find_install(store, spec)
    if inst["installed"] and (inst["method"] != cfg.method or inst["namespace"] != cfg.namespace):
        raise RuntimeError(f"driver already installed with {inst['method']} in {inst['namespace']}; refusing to change method or namespace")
    ctx.log(f"PowerScale CSI via {cfg.method}: namespace {cfg.namespace}, arrays " + ", ".join(f"{a.name} ({a.endpoint})" for a in cfg.arrays))
    if cfg.method == "helm":
        _install_helm(ctx, store, spec, cfg, passwords)
    else:
        _install_operator(ctx, store, spec, cfg, passwords)
    _apply_classes(ctx, store, spec, cfg)
    # patch only this section (the spec may have been edited while the job ran; secrets stay encrypted)
    store.patch(lambda raw: raw.setdefault("day2", {}).__setitem__("powerscale", cfg.model_dump()))
    st = status(store, spec)
    ctx.log(f"Done: driver {st['install']['driver_version']} in {cfg.namespace}; StorageClasses: " + ", ".join(c["name"] for c in st["classes"]))


def _install_helm(ctx, store, spec, cfg: PowerScaleSpec, passwords):
    tgz = dellbundle.path_of("csi-isilon", cfg.chart_version)
    rel = helm_release(store, spec, cfg.namespace, cfg.release)
    if cfg.replication.enabled:
        crds = {c["metadata"]["name"] for c in (ops.get_opt(store, spec, "crd") or {}).get("items", [])}
        if "dellcsireplicationgroups.replication.storage.dell.com" not in crds:
            rv = cfg.replication.chart_version or dellbundle.matrix(cfg.chart_version).get("replication", "")
            ensure_replication_crds(ctx, store, spec, rv)
    _apply_secrets(ctx, store, spec, cfg, passwords)
    values = helm_values(cfg, dellbundle.chart_values("csi-isilon", cfg.chart_version), (rel or {}).get("values"))
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    vf = _work(store) / f"values-{stamp}.yaml"
    vf.write_text(yaml.safe_dump(values, sort_keys=False))
    vf.chmod(0o600)
    verb = f"upgrade from {rel['chart_version']} (revision {rel['revision']})" if rel else "install"
    ctx.log(f"helm upgrade --install {cfg.release} csi-isilon-{cfg.chart_version}.tgz -n {cfg.namespace} ({verb}); values in powerscale/{vf.name}")
    r = helm(store, ["upgrade", "--install", cfg.release, str(tgz), "-n", cfg.namespace, "-f", str(vf), "--timeout", "15m"], timeout=1200, check=False)
    for line in (r.stdout + r.stderr).splitlines()[-20:]:
        ctx.log("  " + line)
    if r.returncode != 0:
        raise RuntimeError(f"helm failed (exit {r.returncode}); the previous release (if any) is still in place: helm rollback is possible")
    _wait_driver(ctx, store, spec, cfg.namespace)


# ------------------------------------------------------------------ CSM Operator
def _csm_example(store, spec) -> Dict:
    """The operator's own PowerScale example CR (from the installed CSV, else the catalog), so spec.version
    and the env names always match the operator version that will reconcile it."""
    ann = {}
    op = _operator_state(store, spec)
    if op.get("installed") and op.get("csv"):
        csv = ops.get_opt(store, spec, "csv", op["csv"], ns=op["namespace"]) or {}
        ann = (csv.get("metadata") or {}).get("annotations") or {}
    if not ann.get("alm-examples"):
        pm = ops.get_opt(store, spec, "packagemanifest", CSM_PACKAGE, ns="openshift-marketplace") or {}
        st = pm.get("status") or {}
        ch = next((c for c in st.get("channels", []) if c.get("name") == st.get("defaultChannel")), None) or {}
        ann = (ch.get("currentCSVDesc") or {}).get("annotations") or {}
    for e in json.loads(ann.get("alm-examples") or "[]"):
        if e.get("kind") == "ContainerStorageModule" and ((e.get("spec") or {}).get("driver") or {}).get("csiDriverType") == "isilon":
            return e
    raise RuntimeError("the CSM Operator catalog entry has no PowerScale example (is dell-csm-operator-certified in a catalog?)")


def _set_env(envs: List[Dict], name: str, value: str):
    for e in envs:
        if e.get("name") == name:
            e["value"] = value
            return
    envs.append({"name": name, "value": value})


def csm_cr(store, spec, cfg: PowerScaleSpec) -> Dict:
    cr = copy.deepcopy(_csm_example(store, spec))
    da = _default_array(cfg)
    ca_count = len({a.ca_pem.strip() for a in cfg.arrays if not a.skip_cert_validation and a.ca_pem.strip()})
    cr["metadata"] = {"name": cfg.release, "namespace": cfg.namespace}
    sp = cr["spec"]
    d = sp["driver"]
    d.pop("configVersion", None)            # spec.version is the supported field; both together are rejected
    d["authSecret"] = f"{cfg.release}-creds"
    d["replicas"] = cfg.controller_count
    d["forceRemoveDriver"] = True
    common = d.setdefault("common", {}).setdefault("envs", [])
    for k, v in (("X_CSI_ISI_PATH", da.isi_path), ("X_CSI_ISI_PORT", str(da.port)), ("X_CSI_ISI_AUTH_TYPE", str(cfg.auth_type)),
                 ("X_CSI_ISI_SKIP_CERTIFICATE_VALIDATION", "true" if da.skip_cert_validation else "false"),
                 ("CERT_SECRET_COUNT", str(max(1, ca_count))), ("CSI_LOG_LEVEL", cfg.log_level)):
        _set_env(common, k, v)
    ctl = d.setdefault("controller", {})
    for k, v in (("X_CSI_ISI_ACCESS_ZONE", da.access_zone), ("X_CSI_ISI_QUOTA_ENABLED", "true" if cfg.enable_quota else "false")):
        _set_env(ctl.setdefault("envs", []), k, v)
    node = d.setdefault("node", {})
    tol = [t for t in (node.get("tolerations") or []) if t.get("key") != INFRA_TOLERATION["key"]]
    if cfg.infra_nodes:
        tol.append(dict(INFRA_TOLERATION))
    node["tolerations"] = tol or None
    sc = [x for x in (d.get("sideCars") or []) if x.get("name") not in ("provisioner", "snapshotter", "resizer")]
    sc.append({"name": "provisioner", "args": [f"--volume-name-prefix={cfg.volume_name_prefix}"]})
    if not cfg.snapshots:
        sc.append({"name": "snapshotter", "enabled": False})
    if not cfg.resizer:
        sc.append({"name": "resizer", "enabled": False})
    d["sideCars"] = sc
    for m in sp.get("modules") or []:
        if m.get("name") == "replication":
            m["enabled"] = bool(cfg.replication.enabled)
    if cfg.image_registry:
        sp["customRegistry"] = cfg.image_registry
        sp["retainImageRegistryPath"] = True
    else:
        sp.pop("customRegistry", None)
        sp.pop("retainImageRegistryPath", None)
    return cr


def _install_operator(ctx, store, spec, cfg: PowerScaleSpec, passwords):
    from . import operators
    operators.ensure_installed(ctx, store, spec, CSM_PACKAGE)
    ops.wait_for("ContainerStorageModule API", lambda: bool(kube.oc(store, spec, ["api-resources", "--api-group", "storage.dell.com", "-o", "name"], check=False).strip()),
                 timeout=300, interval=10, log=ctx.log)
    _apply_secrets(ctx, store, spec, cfg, passwords)
    cr = csm_cr(store, spec, cfg)
    cur = ops.get_opt(store, spec, "containerstoragemodule", cfg.release, ns=cfg.namespace)
    if cur and any((m.get("name") == "replication" and m.get("enabled")) for m in (cur.get("spec") or {}).get("modules", [])) and not cfg.replication.enabled:
        raise RuntimeError("turning replication off on an operator install makes the operator delete the replication CRDs "
                           "(and every replication group in the cluster); the app will not do that")
    ctx.log(f"ContainerStorageModule {cfg.release} (CSM {cr['spec'].get('version')}): " + ("updating" if cur else "creating"))
    ctx.log(ops.apply(store, spec, [cr]).strip())

    def ready():
        c = ops.get(store, spec, "containerstoragemodule", cfg.release, ns=cfg.namespace)
        return (c.get("status") or {}).get("state") == "Succeeded"

    def tick():
        c = ops.get(store, spec, "containerstoragemodule", cfg.release, ns=cfg.namespace)
        st = c.get("status") or {}
        ev = kube.oc(store, spec, ["get", "events", "-n", cfg.namespace, "--field-selector", f"involvedObject.name={cfg.release},type=Warning",
                                   "-o", "jsonpath={.items[-1:].message}"], check=False).strip()
        return f"CR state {st.get('state', '-')} controller {(st.get('controllerStatus') or {}).get('available', '?')}/{(st.get('controllerStatus') or {}).get('desired', '?')} node {(st.get('nodeStatus') or {}).get('available', '?')}/{(st.get('nodeStatus') or {}).get('desired', '?')}" + (f" - {ev[:160]}" if ev else "")
    ops.wait_for("ContainerStorageModule Succeeded", ready, timeout=1200, interval=15, log=ctx.log, on_tick=tick)
    _wait_driver(ctx, store, spec, cfg.namespace)


# ------------------------------------------------------------------ uninstall
def job_uninstall(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, remove_classes: bool):
    inst = find_install(store, spec)
    if not inst["installed"]:
        raise RuntimeError("the driver is not installed")
    st = status(store, spec)
    if st["pv_count"]:
        raise RuntimeError(f"{st['pv_count']} PersistentVolume(s) still use {DRIVER}; delete those volumes first (their data stays on the array only if reclaimPolicy is Retain)")
    ns = inst["namespace"]
    if inst["method"] == "helm":
        ctx.log(f"helm uninstall {inst['release']} -n {ns}")
        r = helm(store, ["uninstall", inst["release"], "-n", ns, "--wait", "--timeout", "10m"], timeout=900, check=False)
        ctx.log((r.stdout + r.stderr).strip()[-400:])
        if r.returncode != 0:
            raise RuntimeError("helm uninstall failed")
    elif inst["method"] == "operator":
        ctx.log(f"Deleting ContainerStorageModule {inst['release']} in {ns}")
        kube.oc(store, spec, ["delete", "containerstoragemodule", inst["release"], "-n", ns, "--wait=true", "--timeout=600s"], timeout=700)
    else:
        raise RuntimeError("this driver was not installed with helm or the CSM Operator; remove it the way it was installed")
    if remove_classes:
        for c in st["classes"]:
            ctx.log(kube.oc(store, spec, ["delete", "storageclass", c["name"], "--ignore-not-found"]).strip())
        for v in st["snapshot_classes"]:
            ctx.log(kube.oc(store, spec, ["delete", "volumesnapshotclass", v["name"], "--ignore-not-found"]).strip())
    ctx.log(f"Driver removed. Namespace {ns} and its secrets were left in place (delete them by hand if no longer needed).")
