"""Day-2 operations: bootstrap removal, infra and pool nodes, ingress relocation."""
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..jobs import JobContext
from ..models import ClusterSpec, NodeSpec
from ..store import ClusterStore
from . import kube, render, vcenter, haproxy, tools
from .deploy import ensure_macs, create_node_vms, vm_name, job_push_haproxy, node_extra_disks

INFRA_TAINTS = [{"key": "node-role.kubernetes.io/infra", "value": "reserved", "effect": "NoSchedule"},
                {"key": "node-role.kubernetes.io/infra", "value": "reserved", "effect": "NoExecute"}]


def job_remove_bootstrap(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    store.patch(lambda raw: raw["lb"].__setitem__("bootstrap_removed", True))
    spec.lb.bootstrap_removed = True
    if spec.lb.mode == "haproxy":
        job_push_haproxy(ctx, store, spec)
    elif spec.lb.mode == "external":
        ctx.log("External LB: remove the bootstrap server from the API pools for 6443 and 22623 now.")
    else:
        ctx.log("No load balancer configured; nothing to update.")
    if spec.install_method == "ipi":
        b = [n for n in spec.nodes if n.role == "bootstrap"]
        if b and vcenter.find_vm(spec.vcenter, f"{store.kv_get('infra_id') or spec.name}-bootstrap"):
            ctx.log("Bootstrap VM still exists in vCenter; the installer normally deletes it after bootstrap-complete.")


# ---------------------------------------------------------------- node roles / labels / taints
def node_role(n: NodeSpec) -> str:
    """Role label used for MachineSet names and node-role labels: infra or the pool name."""
    return "infra" if n.role == "infra" else n.pool


def node_labels(spec: ClusterSpec, n: NodeSpec) -> Dict[str, str]:
    if n.role == "infra":
        return {"node-role.kubernetes.io/infra": ""}
    pool = spec.pool(n.pool)
    labels = {f"node-role.kubernetes.io/{n.pool}": ""}
    if pool:
        labels.update(pool.labels)
    return labels


def node_taints(spec: ClusterSpec, n: NodeSpec) -> List[Dict]:
    if n.role == "infra":
        return [dict(t) for t in INFRA_TAINTS]
    pool = spec.pool(n.pool)
    return [{"key": t.key, "value": t.value, "effect": t.effect} for t in (pool.taints if pool else []) if t.key]


def _wait_nodes_ready(ctx, store, spec, names, timeout=1800, approve=False):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if approve:
            try:
                kube.approve_pending_csrs(store, spec, ctx.log)
            except Exception as ex:
                ctx.log(f"csr check: {ex}")
        try:
            nodes = kube.oc_json(store, spec, ["get", "nodes"])
            ready = {n["metadata"]["name"].split(".")[0] for n in nodes["items"]
                     if any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])}
        except Exception as ex:
            ready = set()
            ctx.log(f"api not ready: {str(ex)[-120:]}")
        missing = [n for n in names if n.split(".")[0] not in ready and not any(r.startswith(n) for r in ready)]
        if not missing:
            ctx.log("All requested nodes are Ready")
            return
        ctx.log(f"waiting for nodes: {', '.join(missing)}")
        time.sleep(30)
    raise RuntimeError("timed out waiting for nodes to become Ready")


def _label_nodes(ctx, store, spec, targets: List[NodeSpec]):
    """Apply role labels and taints to nodes that joined without a MachineSet (agent method)."""
    nodes = kube.oc_json(store, spec, ["get", "nodes"])
    for t in targets:
        for n in nodes["items"]:
            name = n["metadata"]["name"]
            if not (name == t.name or name.startswith(t.name + ".") or name.startswith(t.name + "-")):
                continue
            for k, v in node_labels(spec, t).items():
                kube.oc(store, spec, ["label", "node", name, f"{k}={v}", "--overwrite"])
            for tn in node_taints(spec, t):
                spec_str = f"{tn['key']}={tn['value']}:{tn['effect']}" if tn.get("value") else f"{tn['key']}:{tn['effect']}"
                kube.oc(store, spec, ["adm", "taint", "node", name, spec_str, "--overwrite"])
            ctx.log(f"labelled {name} as {node_role(t)}" + (" and tainted" if node_taints(spec, t) else ""))


def _label_infra(ctx, store, spec, node_match):
    _label_nodes(ctx, store, spec, [n for n in spec.nodes_by_role("infra") if n.name in node_match])


# ---------------------------------------------------------------- add nodes (infra + pools)
def add_targets(spec: ClusterSpec, node_names: Optional[List[str]] = None) -> List[NodeSpec]:
    """Infra nodes and pool members (optionally filtered by name) that day-2 add-nodes would create."""
    targets = spec.nodes_by_role("infra") + spec.pooled_workers()
    if node_names:
        targets = [n for n in targets if n.name in node_names]
    return targets


def job_add_infra(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, node_names=None):
    names = [n.name for n in spec.nodes_by_role("infra") if not node_names or n.name in node_names]
    if not names:
        raise RuntimeError("no infra nodes defined in the Nodes step")
    job_add_nodes(ctx, store, spec, names)


def job_add_nodes(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, node_names=None):
    """Create infra nodes and node-pool members (GPU, storage, ...) on a running cluster."""
    targets = add_targets(spec, node_names)
    if not targets:
        raise RuntimeError("no infra or pool nodes to add; define them in the Nodes step")
    undefined = sorted({n.pool for n in targets if n.role == "worker" and not spec.pool(n.pool)})
    if undefined:
        raise RuntimeError(f"nodes reference pools that are not defined: {', '.join(undefined)}")
    ctx.log("Adding nodes: " + ", ".join(f"{n.name} ({node_role(n)})" for n in targets))
    if spec.install_method == "ipi":
        _add_nodes_ipi(ctx, store, spec, targets)
    else:
        _add_nodes_agent(ctx, store, spec, targets)
    # make sure the apps LB knows about the new nodes
    if spec.lb.mode == "haproxy":
        job_push_haproxy(ctx, store, spec)
    elif spec.lb.mode == "external":
        ctx.log("External LB: add the new nodes that serve ingress to the apps pools for 80 and 443.")


def resolve_template(spec: ClusterSpec, value: Dict, log) -> Dict:
    """The Machine API looks a bare template name up in the datacenter's root VM folder
    only. The installer puts the RHCOS template inside the cluster's VM folder, so
    replace the name with the template's full inventory path."""
    tmpl = value.get("template", "")
    if tmpl and not tmpl.startswith("/"):
        try:
            found = vcenter.find_vm(spec.vcenter, tmpl)
        except Exception as ex:
            log(f"warning: could not look up template {tmpl} in vCenter: {ex}")
            found = None
        if found and found.get("path") and found["path"] != f"/{spec.vcenter.datacenter}/vm/{tmpl}":
            log(f"template {tmpl} lives at {found['path']}; using the full path")
            value["template"] = found["path"]
        elif not found:
            log(f"warning: RHCOS template {tmpl} not found in vCenter; machines will fail to clone")
    return value


MAPI_IPAM_RBAC = [
    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "ocpdeploy-machine-api-ipam"},
     "rules": [{"apiGroups": ["ipam.cluster.x-k8s.io"], "resources": ["ipaddresses", "ipaddressclaims", "ipaddressclaims/status"],
                "verbs": ["get", "list", "watch", "create", "update", "patch"]}]},
    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": {"name": "ocpdeploy-machine-api-ipam"},
     "subjects": [{"kind": "ServiceAccount", "name": "machine-api-controllers", "namespace": "openshift-machine-api"}],
     "roleRef": {"kind": "ClusterRole", "name": "ocpdeploy-machine-api-ipam", "apiGroup": "rbac.authorization.k8s.io"}},
]


def _worker_machineset_for(ctx, spec: ClusterSpec, workers: List[Dict], n: NodeSpec):
    """The worker MachineSet whose placement matches the node's failure domain (its
    providerSpec carries the right template, datastore and resource pool)."""
    fd = next((f for f in spec.vcenter.failure_domains if f.name == n.failure_domain), None) if n.failure_domain else None
    if fd:
        for m in workers:
            ws = m["spec"]["template"]["spec"]["providerSpec"]["value"].get("workspace", {}) or {}
            if ws.get("datacenter") == fd.datacenter and f"/host/{fd.cluster}/" in (ws.get("resourcePool") or ""):
                return m, fd
        ctx.log(f"warning: no worker MachineSet found in failure domain {fd.name}; cloning {workers[0]['metadata']['name']} instead")
    return workers[0], fd


def _add_nodes_ipi(ctx, store, spec, targets: List[NodeSpec]):
    infra_id = store.kv_get("infra_id")
    if not infra_id:
        md = store.install_dir / "metadata.json"
        infra_id = json.loads(md.read_text())["infraID"]
        store.kv_set("infra_id", infra_id)
    mss = kube.oc_json(store, spec, ["get", "machineset", "-n", "openshift-machine-api"])
    workers = [m for m in mss["items"] if m["spec"]["template"]["metadata"]["labels"].get("machine.openshift.io/cluster-api-machine-role") == "worker"]
    if not workers:
        raise RuntimeError("no worker MachineSet found to clone provider settings from")
    plen = render._prefix(spec)
    docs = []
    names = []
    for n in targets:
        role = node_role(n)
        ms_name = f"{infra_id}-{role}-{n.name}"
        base, fd = _worker_machineset_for(ctx, spec, workers, n)
        pv = base["spec"]["template"]["spec"]["providerSpec"]["value"]
        labels = {"machine.openshift.io/cluster-api-cluster": infra_id,
                  "machine.openshift.io/cluster-api-machine-role": role,
                  "machine.openshift.io/cluster-api-machine-type": role,
                  "machine.openshift.io/cluster-api-machineset": ms_name}
        value = json.loads(json.dumps(pv))
        value["numCPUs"] = n.cpus
        value["numCoresPerSocket"] = min(2, n.cpus)
        value["memoryMiB"] = n.memory_mb
        value["diskGiB"] = n.disk_gb
        value = resolve_template(spec, value, ctx.log)
        value["network"] = {"devices": [{"networkName": fd.network if fd else spec.vcenter.network, "gateway": spec.network.gateway,
                                         "ipAddrs": [f"{n.ip}/{plen}"], "nameservers": spec.network.dns_servers}]}
        disks = node_extra_disks(spec, n)
        if disks:
            if spec.minor and spec.minor < 18:
                ctx.log(f"warning: data disks on MachineSets need OpenShift 4.18+; {n.name} may come up without them")
            value["dataDisks"] = [{"name": f"data{i}", "sizeGiB": int(gb), "provisioningMode": "Thin"} for i, gb in enumerate(disks)]
        tmpl_spec: Dict = {"metadata": {"labels": node_labels(spec, n)}, "providerSpec": {"value": value}}
        taints = node_taints(spec, n)
        if taints:
            tmpl_spec["taints"] = taints
        docs.append({
            "apiVersion": "machine.openshift.io/v1beta1", "kind": "MachineSet",
            "metadata": {"name": ms_name, "namespace": "openshift-machine-api",
                         "labels": {"machine.openshift.io/cluster-api-cluster": infra_id}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"machine.openshift.io/cluster-api-cluster": infra_id,
                                             "machine.openshift.io/cluster-api-machineset": ms_name}},
                "template": {"metadata": {"labels": labels}, "spec": tmpl_spec},
            },
        })
        names.append(ms_name)
    y = "---\n".join(yaml.safe_dump(d, sort_keys=False) for d in docs)
    (store.dir / "day2-machinesets.yaml").write_text(y)
    ctx.log(f"Applying {len(docs)} MachineSet(s) (saved to day2-machinesets.yaml)")
    ctx.log(kube.apply(store, spec, y).strip())
    _wait_nodes_ready(ctx, store, spec, names)


def _add_nodes_agent(ctx, store, spec, targets: List[NodeSpec]):
    spec = ensure_macs(store, spec, ctx.log)
    wanted = {t.name for t in targets}
    targets = [n for n in spec.nodes if n.name in wanted]
    d = store.dir / "add-nodes"
    d.mkdir(exist_ok=True)
    for f in d.glob("*.iso"):
        f.unlink()
    (d / "nodes-config.yaml").write_text(render.to_yaml(render.nodes_config(spec, targets)))
    ctx.log("Creating node ISO with oc adm node-image create")
    ctx.run([kube.oc_bin(spec), "adm", "node-image", "create", "--dir", str(d)], env={"KUBECONFIG": kube.kubeconfig(store)})
    iso = next(d.glob("node.*.iso"))
    create_node_vms(ctx, spec, targets, iso)
    ctx.log("Monitoring node join; CSRs are approved automatically")
    _wait_nodes_ready(ctx, store, spec, [n.name for n in targets], approve=True)
    _label_nodes(ctx, store, spec, targets)
    for n in targets:
        try:
            vcenter.eject_cdrom(spec.vcenter, vm_name(spec, n), ctx.log)
        except Exception as ex:
            ctx.log(f"eject {vm_name(spec, n)}: {ex}")


# ---------------------------------------------------------------- ingress / monitoring to infra
INGRESS_PATCH = {"spec": {"nodePlacement": {
    "nodeSelector": {"matchLabels": {"node-role.kubernetes.io/infra": ""}},
    "tolerations": [{"key": "node-role.kubernetes.io/infra", "value": "reserved", "effect": "NoSchedule"},
                    {"key": "node-role.kubernetes.io/infra", "value": "reserved", "effect": "NoExecute"}]}}}

MONITORING_CM = """apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-monitoring-config
  namespace: openshift-monitoring
data:
  config.yaml: |
%s
"""


def job_move_ingress(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, monitoring: bool = True, registry: bool = True):
    infra = spec.nodes_by_role("infra")
    if not infra:
        raise RuntimeError("no infra nodes defined")
    n_infra = len(infra)
    ctx.log(f"Patching default IngressController: replicas={n_infra}, nodePlacement=infra")
    patch = json.loads(json.dumps(INGRESS_PATCH))
    patch["spec"]["replicas"] = n_infra
    kube.oc(store, spec, ["patch", "ingresscontroller/default", "-n", "openshift-ingress-operator", "--type=merge", "-p", json.dumps(patch)])
    if monitoring:
        sel = {"nodeSelector": {"node-role.kubernetes.io/infra": ""},
               "tolerations": [{"key": "node-role.kubernetes.io/infra", "value": "reserved", "effect": "NoSchedule"},
                               {"key": "node-role.kubernetes.io/infra", "value": "reserved", "effect": "NoExecute"}]}
        cfg = {k: sel for k in ("alertmanagerMain", "prometheusK8s", "prometheusOperator", "metricsServer",
                                "kubeStateMetrics", "telemeterClient", "openshiftStateMetrics", "thanosQuerier", "monitoringPlugin")}
        body = "\n".join("    " + l for l in yaml.safe_dump(cfg, sort_keys=False).splitlines())
        ctx.log("Applying cluster-monitoring-config to move monitoring to infra")
        kube.apply(store, spec, MONITORING_CM % body)
    if registry:
        ctx.log("Moving image registry to infra")
        kube.oc(store, spec, ["patch", "configs.imageregistry.operator.openshift.io/cluster", "--type=merge", "-p", json.dumps({
            "spec": {"nodeSelector": {"node-role.kubernetes.io/infra": ""},
                     "tolerations": [{"key": "node-role.kubernetes.io/infra", "value": "reserved", "effect": "NoSchedule"},
                                     {"key": "node-role.kubernetes.io/infra", "value": "reserved", "effect": "NoExecute"}]}})], check=False)
    # wait for router pods to land on infra
    deadline = time.time() + 900
    infra_ips = {n.ip for n in infra}
    while time.time() < deadline:
        pods = kube.oc_json(store, spec, ["get", "pods", "-n", "openshift-ingress", "-l", "ingresscontroller.operator.openshift.io/deployment-ingresscontroller=default"])
        on_infra = [p for p in pods["items"] if p["status"].get("hostIP") in infra_ips and p["status"].get("phase") == "Running"]
        ctx.log(f"router pods on infra: {len(on_infra)}/{n_infra}")
        if len(on_infra) >= n_infra:
            break
        time.sleep(20)
    store.patch(lambda raw: raw["lb"].__setitem__("ingress_on", "infra"))
    spec.lb.ingress_on = "infra"
    if spec.lb.mode == "haproxy":
        job_push_haproxy(ctx, store, spec)
    elif spec.lb.mode == "external":
        ctx.log("External LB: point the apps pools (80/443) at the infra nodes only.")
