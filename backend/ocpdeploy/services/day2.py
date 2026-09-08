"""Day-2 operations: bootstrap removal, infra nodes, ingress relocation."""
import json
import time
from pathlib import Path

import yaml

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, render, vcenter, haproxy, tools
from .deploy import ensure_macs, create_node_vms, vm_name, job_push_haproxy


def job_remove_bootstrap(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    store.patch(lambda raw: raw["lb"].__setitem__("bootstrap_removed", True))
    spec.lb.bootstrap_removed = True
    if spec.lb.mode == "haproxy":
        job_push_haproxy(ctx, store, spec)
    else:
        ctx.log("External LB: remove the bootstrap server from the API pools for 6443 and 22623 now.")
    if spec.install_method == "ipi":
        b = [n for n in spec.nodes if n.role == "bootstrap"]
        if b and vcenter.find_vm(spec.vcenter, f"{store.kv_get('infra_id') or spec.name}-bootstrap"):
            ctx.log("Bootstrap VM still exists in vCenter; the installer normally deletes it after bootstrap-complete.")


# ---------------------------------------------------------------- infra nodes
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


def _label_infra(ctx, store, spec, node_match):
    nodes = kube.oc_json(store, spec, ["get", "nodes"])
    for n in nodes["items"]:
        name = n["metadata"]["name"]
        if any(name == m or name.startswith(m + ".") or name.startswith(m + "-") for m in node_match):
            kube.oc(store, spec, ["label", "node", name, "node-role.kubernetes.io/infra=", "--overwrite"])
            kube.oc(store, spec, ["adm", "taint", "node", name, "node-role.kubernetes.io/infra=reserved:NoSchedule", "--overwrite"])
            kube.oc(store, spec, ["adm", "taint", "node", name, "node-role.kubernetes.io/infra=reserved:NoExecute", "--overwrite"])
            ctx.log(f"labelled and tainted {name} as infra")


def job_add_infra(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, node_names=None):
    infra = [n for n in spec.nodes_by_role("infra") if not node_names or n.name in node_names]
    if not infra:
        raise RuntimeError("no infra nodes defined in the Nodes step")
    if spec.install_method == "ipi":
        _add_infra_ipi(ctx, store, spec, infra)
    else:
        _add_infra_agent(ctx, store, spec, infra)
    # make sure the apps LB knows about the new nodes
    if spec.lb.mode == "haproxy":
        job_push_haproxy(ctx, store, spec)
    else:
        ctx.log("External LB: add the infra nodes to the apps pools for 80 and 443.")


def _add_infra_ipi(ctx, store, spec, infra):
    infra_id = store.kv_get("infra_id")
    if not infra_id:
        md = store.install_dir / "metadata.json"
        infra_id = json.loads(md.read_text())["infraID"]
        store.kv_set("infra_id", infra_id)
    mss = kube.oc_json(store, spec, ["get", "machineset", "-n", "openshift-machine-api"])
    worker_ms = next((m for m in mss["items"] if m["spec"]["template"]["metadata"]["labels"].get("machine.openshift.io/cluster-api-machine-role") == "worker"), None)
    if not worker_ms:
        raise RuntimeError("no worker MachineSet found to clone provider settings from")
    pv = worker_ms["spec"]["template"]["spec"]["providerSpec"]["value"]
    plen = render._prefix(spec)
    docs = []
    for n in infra:
        ms_name = f"{infra_id}-infra-{n.name}"
        labels = {"machine.openshift.io/cluster-api-cluster": infra_id,
                  "machine.openshift.io/cluster-api-machine-role": "infra",
                  "machine.openshift.io/cluster-api-machine-type": "infra",
                  "machine.openshift.io/cluster-api-machineset": ms_name}
        value = json.loads(json.dumps(pv))
        value["numCPUs"] = n.cpus
        value["numCoresPerSocket"] = min(2, n.cpus)
        value["memoryMiB"] = n.memory_mb
        value["diskGiB"] = n.disk_gb
        value["network"] = {"devices": [{"networkName": spec.vcenter.network, "gateway": spec.network.gateway,
                                         "ipAddrs": [f"{n.ip}/{plen}"], "nameservers": spec.network.dns_servers}]}
        docs.append({
            "apiVersion": "machine.openshift.io/v1beta1", "kind": "MachineSet",
            "metadata": {"name": ms_name, "namespace": "openshift-machine-api",
                         "labels": {"machine.openshift.io/cluster-api-cluster": infra_id}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"machine.openshift.io/cluster-api-cluster": infra_id,
                                             "machine.openshift.io/cluster-api-machineset": ms_name}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "metadata": {"labels": {"node-role.kubernetes.io/infra": ""}},
                        "taints": [{"key": "node-role.kubernetes.io/infra", "value": "reserved", "effect": "NoSchedule"},
                                   {"key": "node-role.kubernetes.io/infra", "value": "reserved", "effect": "NoExecute"}],
                        "providerSpec": {"value": value},
                    },
                },
            },
        })
    y = "---\n".join(yaml.safe_dump(d, sort_keys=False) for d in docs)
    (store.dir / "infra-machinesets.yaml").write_text(y)
    ctx.log(f"Applying {len(docs)} infra MachineSet(s) (saved to infra-machinesets.yaml)")
    ctx.log(kube.apply(store, spec, y).strip())
    _wait_nodes_ready(ctx, store, spec, [f"{infra_id}-infra-{n.name}" for n in infra])


def _add_infra_agent(ctx, store, spec, infra):
    spec = ensure_macs(store, spec, ctx.log)
    infra = [n for n in spec.nodes if n.role == "infra" and n.name in {i.name for i in infra}]
    d = store.dir / "add-nodes"
    d.mkdir(exist_ok=True)
    for f in d.glob("*.iso"):
        f.unlink()
    (d / "nodes-config.yaml").write_text(render.to_yaml(render.nodes_config(spec, infra)))
    ctx.log("Creating node ISO with oc adm node-image create")
    ctx.run([kube.oc_bin(spec), "adm", "node-image", "create", "--dir", str(d)], env={"KUBECONFIG": kube.kubeconfig(store)})
    iso = next(d.glob("node.*.iso"))
    create_node_vms(ctx, spec, infra, iso)
    ctx.log("Monitoring node join; CSRs are approved automatically")
    _wait_nodes_ready(ctx, store, spec, [n.name for n in infra], approve=True)
    _label_infra(ctx, store, spec, [n.name for n in infra])
    for n in infra:
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
    else:
        ctx.log("External LB: point the apps pools (80/443) at the infra nodes only.")
