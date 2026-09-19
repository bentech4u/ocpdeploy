"""Operator Lifecycle Manager installs from a curated catalog."""
from typing import Dict, List, Optional

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, clusterops as ops

# package -> defaults; namespace/channel come from the packagemanifest at runtime when available
CATALOG: Dict[str, Dict] = {
    "kubevirt-hyperconverged": {"title": "OpenShift Virtualization", "namespace": "openshift-cnv", "desc": "Run VMs alongside containers (KubeVirt). Needs nested virtualisation on the ESXi hosts for vSphere.",
                                "post": [{"apiVersion": "hco.kubevirt.io/v1beta1", "kind": "HyperConverged", "metadata": {"name": "kubevirt-hyperconverged", "namespace": "openshift-cnv"}, "spec": {}}]},
    "cluster-logging": {"title": "Logging", "namespace": "openshift-logging", "desc": "Red Hat OpenShift Logging (Vector + Loki-based stack). Configure a ClusterLogForwarder afterwards."},
    "openshift-gitops-operator": {"title": "GitOps (Argo CD)", "namespace": "openshift-gitops-operator", "all_namespaces": True, "desc": "Argo CD managed by the GitOps operator; a default instance appears in openshift-gitops."},
    "openshift-pipelines-operator-rh": {"title": "Pipelines (Tekton)", "namespace": "openshift-operators", "all_namespaces": True, "desc": "Tekton pipelines, triggers and the tkn CLI."},
    "openshift-cert-manager-operator": {"title": "cert-manager", "namespace": "cert-manager-operator", "desc": "Certificate automation (ACME / Let's Encrypt, Vault, internal CAs). Used by the Certificates page."},
    "kubernetes-nmstate-operator": {"title": "NMState", "namespace": "openshift-nmstate", "desc": "Declarative host networking (bonds, VLANs, bridges) through NodeNetworkConfigurationPolicy.",
                                    "post": [{"apiVersion": "nmstate.io/v1", "kind": "NMState", "metadata": {"name": "nmstate"}}]},
    "local-storage-operator": {"title": "Local Storage", "namespace": "openshift-local-storage", "desc": "Expose local disks as PVs; prerequisite for ODF internal mode."},
    "odf-operator": {"title": "OpenShift Data Foundation", "namespace": "openshift-storage", "desc": "Ceph-based block, file and object storage. Needs 3 nodes with spare disks (storage pool)."},
    "lvms-operator": {"title": "LVM Storage", "namespace": "openshift-lvm-storage", "desc": "Lightweight local storage from spare disks; ideal for single node and compact clusters."},
    "nfd": {"title": "Node Feature Discovery", "namespace": "openshift-nfd", "desc": "Labels nodes with hardware features (needed by the NVIDIA GPU operator).",
            "post": [{"apiVersion": "nfd.openshift.io/v1", "kind": "NodeFeatureDiscovery", "metadata": {"name": "nfd-instance", "namespace": "openshift-nfd"}, "spec": {}}]},
    "gpu-operator-certified": {"title": "NVIDIA GPU Operator", "namespace": "nvidia-gpu-operator", "source": "certified-operators", "desc": "Drivers, device plugin and monitoring for GPU pools. Create a ClusterPolicy afterwards."},
    "rhbk-operator": {"title": "Red Hat build of Keycloak", "namespace": "keycloak", "desc": "Keycloak identity server operator; the Apps page deploys an instance with PostgreSQL."},
    "dell-csm-operator-certified": {"title": "Dell Container Storage Modules", "namespace": "dell-csm-operator", "source": "certified-operators",
                                    "desc": "Dell CSM Operator (PowerScale, PowerStore, PowerMax, ... CSI drivers). The Dell PowerScale page drives it."},
    "web-terminal": {"title": "Web Terminal", "namespace": "openshift-operators", "all_namespaces": True, "desc": "A terminal with oc/kubectl inside the web console."},
}


def status(store: ClusterStore, spec: ClusterSpec) -> Dict:
    subs = ops.subscriptions(store, spec)
    csvs = ops.csv_states(store, spec)
    out = []
    for pkg, meta in CATALOG.items():
        sub = next((s for s in subs if s["package"] == pkg), None)
        csv = None
        if sub and sub["installed_csv"]:
            csv = next((c for c in csvs if c["name"] == sub["installed_csv"] and c["namespace"] == sub["namespace"]), None)
        out.append({"package": pkg, "title": meta["title"], "desc": meta["desc"], "namespace": sub["namespace"] if sub else meta["namespace"],
                    "installed": bool(sub), "channel": sub["channel"] if sub else "", "csv": sub["installed_csv"] if sub else "",
                    "phase": csv["phase"] if csv else (sub["state"] if sub else ""), "version": csv["version"] if csv else ""})
    others = [c for c in csvs if not any(c["name"] == o["csv"] for o in out) and not c["name"].startswith("packageserver")]
    return {"catalog": out, "other_csvs": others}


def _manifest(store, spec, pkg: str) -> Dict:
    meta = CATALOG.get(pkg, {"namespace": "openshift-operators", "all_namespaces": True})
    pm = ops.get_opt(store, spec, "packagemanifest", pkg, ns="openshift-marketplace")
    if not pm:
        raise RuntimeError(f"package {pkg} is not in any catalog source (disconnected cluster: mirror it and apply the catalog resources)")
    st = pm.get("status", {})
    channel = st.get("defaultChannel", "")
    chan = next((c for c in st.get("channels", []) if c.get("name") == channel), st.get("channels", [{}])[0] if st.get("channels") else {})
    suggested = ((chan.get("currentCSVDesc") or {}).get("annotations") or {}).get("operatorframework.io/suggested-namespace", "")
    ns = meta.get("namespace") or suggested or "openshift-operators"
    return {"package": pkg, "channel": channel, "namespace": ns, "source": st.get("catalogSource", meta.get("source", "redhat-operators")),
            "source_ns": st.get("catalogSourceNamespace", "openshift-marketplace"), "all_namespaces": bool(meta.get("all_namespaces")),
            "post": meta.get("post", [])}


def install_docs(m: Dict) -> List[Dict]:
    docs: List[Dict] = []
    ns = m["namespace"]
    if ns != "openshift-operators":
        docs.append({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {"openshift.io/cluster-monitoring": "true"}}})
        og = {"apiVersion": "operators.coreos.com/v1", "kind": "OperatorGroup", "metadata": {"name": f"{ns}-og", "namespace": ns}, "spec": {}}
        if not m["all_namespaces"]:
            og["spec"]["targetNamespaces"] = [ns]
        docs.append(og)
    docs.append({"apiVersion": "operators.coreos.com/v1alpha1", "kind": "Subscription", "metadata": {"name": m["package"], "namespace": ns},
                 "spec": {"channel": m["channel"], "name": m["package"], "source": m["source"], "sourceNamespace": m["source_ns"], "installPlanApproval": "Automatic"}})
    return docs


def ensure_installed(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, pkg: str, timeout: int = 1200) -> Dict:
    m = _manifest(store, spec, pkg)
    existing = next((s for s in ops.subscriptions(store, spec) if s["package"] == pkg), None)
    if existing:
        m["namespace"] = existing["namespace"]
        ctx.log(f"{pkg}: subscription already present in {existing['namespace']} (channel {existing['channel']})")
    else:
        # an existing OperatorGroup in the namespace must not be duplicated
        ogs = ops.get_opt(store, spec, "operatorgroups", ns=m["namespace"]) or {}
        docs = install_docs(m)
        if ogs.get("items"):
            docs = [d for d in docs if d["kind"] != "OperatorGroup"]
        ctx.log(f"{pkg}: subscribing in {m['namespace']} (channel {m['channel']}, source {m['source']})")
        ctx.log(ops.apply(store, spec, docs).strip())

    def ready():
        sub = next((s for s in ops.subscriptions(store, spec) if s["package"] == pkg), None)
        if not sub or not sub["installed_csv"]:
            return False
        csv = ops.get_opt(store, spec, "csv", sub["installed_csv"], ns=sub["namespace"])
        return bool(csv) and (csv.get("status") or {}).get("phase") == "Succeeded"

    def tick():
        sub = next((s for s in ops.subscriptions(store, spec) if s["package"] == pkg), None)
        if not sub:
            return f"{pkg}: waiting for subscription"
        if not sub["installed_csv"]:
            return f"{pkg}: {sub['state'] or 'resolving'} (current {sub['current_csv'] or '-'})"
        csv = ops.get_opt(store, spec, "csv", sub["installed_csv"], ns=sub["namespace"]) or {}
        return f"{pkg}: {sub['installed_csv']} {(csv.get('status') or {}).get('phase', '')} {((csv.get('status') or {}).get('message') or '')[:100]}"
    ops.wait_for(f"{pkg} operator", ready, timeout=timeout, interval=15, log=ctx.log, on_tick=tick)
    return m


def job_install(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, packages: List[str]):
    for pkg in packages:
        m = ensure_installed(ctx, store, spec, pkg)
        for doc in m.get("post", []):
            kind = doc["kind"]
            # the CRD arrives with the operator; retry briefly
            ops.wait_for(f"{kind} API", lambda: bool(kube.oc(store, spec, ["api-resources", "--api-group", doc["apiVersion"].split("/")[0], "-o", "name"], check=False).strip()),
                         timeout=300, interval=10, log=ctx.log)
            ctx.log(f"{pkg}: creating {kind}/{doc['metadata']['name']}")
            ctx.log(ops.apply(store, spec, [doc]).strip())
    ctx.log("Operators installed: " + ", ".join(packages))


def job_remove(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, pkg: str):
    sub = next((s for s in ops.subscriptions(store, spec) if s["package"] == pkg), None)
    if not sub:
        raise RuntimeError(f"{pkg} is not installed")
    ctx.log(f"Removing subscription {sub['name']} in {sub['namespace']}")
    kube.oc(store, spec, ["delete", f"subscription.operators.coreos.com/{sub['name']}", "-n", sub["namespace"]])
    if sub["installed_csv"]:
        ctx.log(f"Removing CSV {sub['installed_csv']}")
        kube.oc(store, spec, ["delete", f"csv/{sub['installed_csv']}", "-n", sub["namespace"]], check=False)
    ctx.log("Operator removed. Custom resources and the namespace were left in place.")


def job_apply_mirror_resources(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, disable_default_sources: bool = True):
    """Apply the CatalogSource / IDMS / ITMS files oc-mirror produced so operators install from the mirror."""
    res = store.dir / "mirror" / "workspace" / "working-dir" / "cluster-resources"
    files = sorted(res.glob("*.yaml")) if res.exists() else []
    if not files:
        raise RuntimeError("no cluster resources from oc-mirror found; run the mirror job first")
    for f in files:
        ctx.log(f"applying {f.name}")
        ctx.log(kube.apply(store, spec, f.read_text()).strip())
    if disable_default_sources:
        ctx.log("Disabling the default (internet) catalog sources")
        ops.patch(store, spec, "operatorhub/cluster", {"spec": {"disableAllDefaultSources": True}})
    ctx.log("Mirror catalog resources applied; packages appear in a few minutes")
