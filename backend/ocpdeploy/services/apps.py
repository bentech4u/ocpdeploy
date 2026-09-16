"""Application catalog: one-click lab workloads deployed as plain manifests, Helm
charts or operators. Credentials live in a Secret inside the app's namespace."""
import base64
import json
import secrets
import string
import subprocess
from typing import Dict, List, Optional

import httpx
import yaml

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, tools, clusterops as ops

CRED_SECRET = "ocpdeploy-credentials"


def _pw(n: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _route(name: str, ns: str, host: str, service: str, port, tls: bool = True) -> Dict:
    r = {"apiVersion": "route.openshift.io/v1", "kind": "Route", "metadata": {"name": name, "namespace": ns},
         "spec": {"host": host, "to": {"kind": "Service", "name": service}, "port": {"targetPort": port}}}
    if tls:
        r["spec"]["tls"] = {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"}
    return r


def _pvc(name: str, ns: str, size_gb: int, sc: str = "", rwx: bool = False) -> Dict:
    p = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": name, "namespace": ns},
         "spec": {"accessModes": ["ReadWriteMany" if rwx else "ReadWriteOnce"], "resources": {"requests": {"storage": f"{size_gb}Gi"}}}}
    if sc:
        p["spec"]["storageClassName"] = sc
    return p


def _ns(ns: str) -> Dict:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}


def _svc(name: str, ns: str, ports: List[Dict], selector: Dict) -> Dict:
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns}, "spec": {"selector": selector, "ports": ports}}


def _cred_secret(ns: str, data: Dict[str, str]) -> Dict:
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": CRED_SECRET, "namespace": ns}, "type": "Opaque", "stringData": data}


# ---------------------------------------------------------------- catalog
CATALOG: Dict[str, Dict] = {
    "gitea": {
        "title": "Gitea", "namespace": "gitea", "kind": "manifests", "deployment": "gitea",
        "desc": "Lightweight Git server with issues, PRs and Actions. SQLite-backed, single pod, rootless image.",
        "inputs": [{"key": "host", "label": "Hostname", "default": "gitea.apps.{domain}"}, {"key": "size_gb", "label": "Storage GB", "default": 20, "type": "number"},
                   {"key": "storage_class", "label": "Storage class", "default": "", "help": "empty = default"}, {"key": "admin_user", "label": "Admin user", "default": "gitea"}],
    },
    "minio": {
        "title": "MinIO", "namespace": "minio", "kind": "manifests", "deployment": "minio",
        "desc": "S3-compatible object storage. Single node; API and console exposed through Routes. Pairs with Quay, Velero, Loki.",
        "inputs": [{"key": "host", "label": "Console hostname", "default": "minio.apps.{domain}"}, {"key": "api_host", "label": "API hostname", "default": "s3.apps.{domain}"},
                   {"key": "size_gb", "label": "Storage GB", "default": 100, "type": "number"}, {"key": "storage_class", "label": "Storage class", "default": ""}],
    },
    "grafana": {
        "title": "Grafana dashboards", "namespace": "grafana", "kind": "manifests", "deployment": "grafana",
        "desc": "Grafana wired to the cluster's Prometheus (Thanos querier) with a cluster overview dashboard. Add your own dashboards on top.",
        "inputs": [{"key": "host", "label": "Hostname", "default": "grafana.apps.{domain}"}],
    },
    "keycloak": {
        "title": "Keycloak", "namespace": "keycloak", "kind": "operator", "deployment": "",
        "desc": "Red Hat build of Keycloak through its operator, with a PostgreSQL database. Use it as the OIDC provider on the Identity page.",
        "inputs": [{"key": "host", "label": "Hostname", "default": "keycloak.apps.{domain}"}, {"key": "size_gb", "label": "Database storage GB", "default": 10, "type": "number"},
                   {"key": "storage_class", "label": "Storage class", "default": ""}],
    },
    "harbor": {
        "title": "Harbor registry", "namespace": "harbor", "kind": "helm", "deployment": "harbor-core",
        "desc": "Container registry with projects, vulnerability scanning and replication (official Helm chart). Needs ~2 vCPU / 4 GB and a storage class.",
        "inputs": [{"key": "host", "label": "Hostname", "default": "harbor.apps.{domain}"}, {"key": "size_gb", "label": "Registry storage GB", "default": 100, "type": "number"},
                   {"key": "storage_class", "label": "Storage class", "default": ""}, {"key": "trivy", "label": "Vulnerability scanner (Trivy)", "default": "yes", "type": "select", "options": ["yes", "no"]}],
    },
}


def catalog(spec: ClusterSpec) -> List[Dict]:
    out = []
    for key, a in CATALOG.items():
        inputs = []
        for i in a["inputs"]:
            d = dict(i)
            if isinstance(d.get("default"), str):
                d["default"] = d["default"].replace("{domain}", spec.domain)
            inputs.append(d)
        out.append({"key": key, "title": a["title"], "desc": a["desc"], "namespace": a["namespace"], "kind": a["kind"], "inputs": inputs})
    return out


def _inputs(spec: ClusterSpec, app: str, given: Optional[Dict]) -> Dict:
    given = given or {}
    out = {}
    for i in CATALOG[app]["inputs"]:
        v = given.get(i["key"], i["default"])
        if isinstance(v, str):
            v = v.replace("{domain}", spec.domain).strip()
        if i.get("type") == "number":
            v = int(v or i["default"])
        out[i["key"]] = v
    return out


def status(store: ClusterStore, spec: ClusterSpec) -> Dict:
    out = []
    for key, a in CATALOG.items():
        ns = a["namespace"]
        st = {"key": key, "installed": False, "ready": False, "url": "", "detail": ""}
        if ops.get_opt(store, spec, "namespace", ns):
            st["installed"] = True
            routes = ops.get_opt(store, spec, "routes", ns=ns) or {}
            hosts = [r["spec"]["host"] for r in routes.get("items", [])]
            if hosts:
                st["url"] = "https://" + sorted(hosts)[0]
            deps = ops.get_opt(store, spec, "deployments", ns=ns) or {}
            sts = ops.get_opt(store, spec, "statefulsets", ns=ns) or {}
            items = deps.get("items", []) + sts.get("items", [])
            ready = sum(1 for d in items if (d.get("status") or {}).get("readyReplicas", 0) >= (d["spec"].get("replicas") or 1))
            st["ready"] = bool(items) and ready == len(items)
            st["detail"] = f"{ready}/{len(items)} workloads ready"
            if not items:
                st["detail"] = "namespace exists, nothing running"
        out.append(st)
    return {"apps": out}


def credentials(store: ClusterStore, spec: ClusterSpec, app: str) -> Dict[str, str]:
    ns = CATALOG[app]["namespace"]
    sec = ops.get_opt(store, spec, "secret", CRED_SECRET, ns=ns)
    if app == "keycloak":
        sec = ops.get_opt(store, spec, "secret", "keycloak-initial-admin", ns=ns) or sec
    if not sec:
        return {}
    return {k: base64.b64decode(v).decode(errors="replace") for k, v in (sec.get("data") or {}).items()}


# ---------------------------------------------------------------- manifests per app
def gitea_docs(spec: ClusterSpec, inp: Dict, pw: str) -> List[Dict]:
    ns = "gitea"
    sel = {"app": "gitea"}
    return [
        _ns(ns), _cred_secret(ns, {"username": inp["admin_user"], "password": pw, "url": f"https://{inp['host']}"}),
        _pvc("gitea-data", ns, inp["size_gb"], inp["storage_class"]),
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "gitea", "namespace": ns, "labels": sel},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": sel},
                  "template": {"metadata": {"labels": sel}, "spec": {"containers": [{
                      "name": "gitea", "image": "docker.io/gitea/gitea:1.24-rootless", "ports": [{"containerPort": 3000}, {"containerPort": 2222}],
                      "env": [{"name": "GITEA__server__ROOT_URL", "value": f"https://{inp['host']}/"}, {"name": "GITEA__server__DOMAIN", "value": inp["host"]},
                              {"name": "GITEA__server__SSH_DOMAIN", "value": inp["host"]}, {"name": "GITEA__server__SSH_PORT", "value": "2222"},
                              {"name": "GITEA__security__INSTALL_LOCK", "value": "true"}, {"name": "GITEA__database__DB_TYPE", "value": "sqlite3"},
                              {"name": "GITEA__service__DISABLE_REGISTRATION", "value": "true"}, {"name": "GITEA_WORK_DIR", "value": "/var/lib/gitea"}],
                      "volumeMounts": [{"name": "data", "mountPath": "/var/lib/gitea"}, {"name": "data", "mountPath": "/etc/gitea", "subPath": "config"}],
                      "readinessProbe": {"httpGet": {"path": "/api/healthz", "port": 3000}, "initialDelaySeconds": 15, "periodSeconds": 10},
                      "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}}}],
                      "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "gitea-data"}}]}}}},
        _svc("gitea", ns, [{"name": "http", "port": 3000, "targetPort": 3000}, {"name": "ssh", "port": 2222, "targetPort": 2222}], sel),
        _route("gitea", ns, inp["host"], "gitea", "http"),
    ]


def minio_docs(spec: ClusterSpec, inp: Dict, pw: str) -> List[Dict]:
    ns = "minio"
    sel = {"app": "minio"}
    return [
        _ns(ns), _cred_secret(ns, {"username": "admin", "password": pw, "url": f"https://{inp['host']}", "s3_endpoint": f"https://{inp['api_host']}"}),
        _pvc("minio-data", ns, inp["size_gb"], inp["storage_class"]),
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "minio", "namespace": ns, "labels": sel},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": sel},
                  "template": {"metadata": {"labels": sel}, "spec": {"containers": [{
                      "name": "minio", "image": "quay.io/minio/minio:latest", "args": ["server", "/data", "--console-address", ":9001"],
                      "ports": [{"containerPort": 9000}, {"containerPort": 9001}],
                      "env": [{"name": "MINIO_ROOT_USER", "valueFrom": {"secretKeyRef": {"name": CRED_SECRET, "key": "username"}}},
                              {"name": "MINIO_ROOT_PASSWORD", "valueFrom": {"secretKeyRef": {"name": CRED_SECRET, "key": "password"}}},
                              {"name": "MINIO_BROWSER_REDIRECT_URL", "value": f"https://{inp['host']}"}, {"name": "MINIO_SERVER_URL", "value": f"https://{inp['api_host']}"}],
                      "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                      "readinessProbe": {"httpGet": {"path": "/minio/health/ready", "port": 9000}, "initialDelaySeconds": 10, "periodSeconds": 10},
                      "resources": {"requests": {"cpu": "100m", "memory": "512Mi"}}}],
                      "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "minio-data"}}]}}}},
        _svc("minio", ns, [{"name": "api", "port": 9000, "targetPort": 9000}, {"name": "console", "port": 9001, "targetPort": 9001}], sel),
        _route("minio-console", ns, inp["host"], "minio", "console"),
        _route("minio-api", ns, inp["api_host"], "minio", "api"),
    ]


GRAFANA_DASHBOARD = {
    "title": "Cluster overview", "uid": "ocpdeploy-cluster", "schemaVersion": 39, "time": {"from": "now-1h", "to": "now"}, "refresh": "30s",
    "panels": [
        {"type": "stat", "title": "Nodes ready", "gridPos": {"x": 0, "y": 0, "w": 6, "h": 4}, "targets": [{"expr": "sum(kube_node_status_condition{condition=\"Ready\",status=\"true\"})"}]},
        {"type": "stat", "title": "Running pods", "gridPos": {"x": 6, "y": 0, "w": 6, "h": 4}, "targets": [{"expr": "sum(kube_pod_status_phase{phase=\"Running\"})"}]},
        {"type": "stat", "title": "Degraded operators", "gridPos": {"x": 12, "y": 0, "w": 6, "h": 4}, "targets": [{"expr": "sum(cluster_operator_conditions{condition=\"Degraded\"} == 1) or vector(0)"}]},
        {"type": "stat", "title": "Firing alerts", "gridPos": {"x": 18, "y": 0, "w": 6, "h": 4}, "targets": [{"expr": "sum(ALERTS{alertstate=\"firing\",alertname!=\"Watchdog\"}) or vector(0)"}]},
        {"type": "timeseries", "title": "CPU usage by node", "gridPos": {"x": 0, "y": 4, "w": 12, "h": 8}, "fieldConfig": {"defaults": {"unit": "percentunit"}},
         "targets": [{"expr": "1 - avg by (instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[5m]))", "legendFormat": "{{instance}}"}]},
        {"type": "timeseries", "title": "Memory usage by node", "gridPos": {"x": 12, "y": 4, "w": 12, "h": 8}, "fieldConfig": {"defaults": {"unit": "percentunit"}},
         "targets": [{"expr": "1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes", "legendFormat": "{{instance}}"}]},
        {"type": "timeseries", "title": "Pod CPU by namespace (top 10)", "gridPos": {"x": 0, "y": 12, "w": 12, "h": 8},
         "targets": [{"expr": "topk(10, sum by (namespace) (rate(container_cpu_usage_seconds_total{container!=\"\"}[5m])))", "legendFormat": "{{namespace}}"}]},
        {"type": "timeseries", "title": "Persistent volume usage", "gridPos": {"x": 12, "y": 12, "w": 12, "h": 8}, "fieldConfig": {"defaults": {"unit": "percentunit"}},
         "targets": [{"expr": "kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes", "legendFormat": "{{namespace}}/{{persistentvolumeclaim}}"}]},
    ],
}


def grafana_docs(spec: ClusterSpec, inp: Dict, pw: str) -> List[Dict]:
    ns = "grafana"
    sel = {"app": "grafana"}
    ds = {"apiVersion": 1, "datasources": [{"name": "OpenShift Prometheus", "type": "prometheus", "access": "proxy", "isDefault": True, "editable": True,
                                            "url": "https://thanos-querier.openshift-monitoring.svc.cluster.local:9091",
                                            "jsonData": {"httpHeaderName1": "Authorization", "tlsSkipVerify": True, "timeInterval": "30s"},
                                            "secureJsonData": {"httpHeaderValue1": "Bearer $__file{/etc/grafana-token/token}"}}]}
    dash_prov = {"apiVersion": 1, "providers": [{"name": "ocpdeploy", "folder": "OpenShift", "type": "file", "options": {"path": "/var/lib/grafana/dashboards"}}]}
    return [
        _ns(ns), _cred_secret(ns, {"username": "admin", "password": pw, "url": f"https://{inp['host']}"}),
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "grafana", "namespace": ns}},
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "grafana-token", "namespace": ns, "annotations": {"kubernetes.io/service-account.name": "grafana"}}, "type": "kubernetes.io/service-account-token"},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": {"name": "ocpdeploy-grafana-monitoring-view"},
         "subjects": [{"kind": "ServiceAccount", "name": "grafana", "namespace": ns}], "roleRef": {"kind": "ClusterRole", "name": "cluster-monitoring-view", "apiGroup": "rbac.authorization.k8s.io"}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "grafana-provisioning", "namespace": ns},
         "data": {"datasource.yaml": yaml.safe_dump(ds, sort_keys=False), "dashboards.yaml": yaml.safe_dump(dash_prov, sort_keys=False)}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "grafana-dashboards", "namespace": ns}, "data": {"cluster-overview.json": json.dumps(GRAFANA_DASHBOARD)}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "grafana", "namespace": ns, "labels": sel},
         "spec": {"replicas": 1, "selector": {"matchLabels": sel},
                  "template": {"metadata": {"labels": sel}, "spec": {"serviceAccountName": "grafana", "containers": [{
                      "name": "grafana", "image": "docker.io/grafana/grafana:11.6.0", "ports": [{"containerPort": 3000}],
                      "env": [{"name": "GF_SECURITY_ADMIN_USER", "value": "admin"}, {"name": "GF_SECURITY_ADMIN_PASSWORD", "valueFrom": {"secretKeyRef": {"name": CRED_SECRET, "key": "password"}}},
                              {"name": "GF_SERVER_ROOT_URL", "value": f"https://{inp['host']}"}, {"name": "GF_PATHS_DATA", "value": "/var/lib/grafana"}],
                      "volumeMounts": [{"name": "data", "mountPath": "/var/lib/grafana"}, {"name": "token", "mountPath": "/etc/grafana-token", "readOnly": True},
                                       {"name": "provisioning", "mountPath": "/etc/grafana/provisioning/datasources/datasource.yaml", "subPath": "datasource.yaml"},
                                       {"name": "provisioning", "mountPath": "/etc/grafana/provisioning/dashboards/dashboards.yaml", "subPath": "dashboards.yaml"},
                                       {"name": "dashboards", "mountPath": "/var/lib/grafana/dashboards"}],
                      "readinessProbe": {"httpGet": {"path": "/api/health", "port": 3000}, "initialDelaySeconds": 10},
                      "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}}}],
                      "volumes": [{"name": "data", "emptyDir": {}}, {"name": "token", "secret": {"secretName": "grafana-token"}},
                                  {"name": "provisioning", "configMap": {"name": "grafana-provisioning"}}, {"name": "dashboards", "configMap": {"name": "grafana-dashboards"}}]}}}},
        _svc("grafana", ns, [{"name": "http", "port": 3000, "targetPort": 3000}], sel),
        _route("grafana", ns, inp["host"], "grafana", "http"),
    ]


def keycloak_docs(spec: ClusterSpec, inp: Dict, pw: str) -> List[Dict]:
    ns = "keycloak"
    sel = {"app": "postgres"}
    return [
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "keycloak-db", "namespace": ns}, "type": "Opaque", "stringData": {"username": "keycloak", "password": pw}},
        _pvc("postgres-data", ns, inp["size_gb"], inp["storage_class"]),
        {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "postgres", "namespace": ns, "labels": sel},
         "spec": {"serviceName": "postgres", "replicas": 1, "selector": {"matchLabels": sel},
                  "template": {"metadata": {"labels": sel}, "spec": {"containers": [{
                      "name": "postgres", "image": "registry.redhat.io/rhel9/postgresql-16:latest", "ports": [{"containerPort": 5432}],
                      "env": [{"name": "POSTGRESQL_USER", "valueFrom": {"secretKeyRef": {"name": "keycloak-db", "key": "username"}}},
                              {"name": "POSTGRESQL_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "keycloak-db", "key": "password"}}},
                              {"name": "POSTGRESQL_DATABASE", "value": "keycloak"}],
                      "volumeMounts": [{"name": "data", "mountPath": "/var/lib/pgsql/data"}],
                      "readinessProbe": {"exec": {"command": ["pg_isready", "-U", "keycloak"]}, "initialDelaySeconds": 10},
                      "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}}}],
                      "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "postgres-data"}}]}}}},
        _svc("postgres", ns, [{"name": "pg", "port": 5432, "targetPort": 5432}], sel),
        {"apiVersion": "k8s.keycloak.org/v2alpha1", "kind": "Keycloak", "metadata": {"name": "keycloak", "namespace": ns},
         "spec": {"instances": 1,
                  "db": {"vendor": "postgres", "host": "postgres", "port": 5432, "database": "keycloak",
                         "usernameSecret": {"name": "keycloak-db", "key": "username"}, "passwordSecret": {"name": "keycloak-db", "key": "password"}},
                  "hostname": {"hostname": inp["host"]}, "http": {"httpEnabled": True}, "proxy": {"headers": "xforwarded"}, "ingress": {"enabled": False}}},
        _route("keycloak", ns, inp["host"], "keycloak-service", 8080),
    ]


def harbor_values(spec: ClusterSpec, inp: Dict, pw: str) -> Dict:
    sc = inp["storage_class"]
    pvc = lambda size: {"storageClass": sc, "size": f"{size}Gi"}
    return {
        "expose": {"type": "clusterIP", "tls": {"enabled": False}, "clusterIP": {"name": "harbor"}},
        "externalURL": f"https://{inp['host']}",
        "harborAdminPassword": pw,
        "trivy": {"enabled": inp["trivy"] == "yes"},
        "persistence": {"enabled": True, "persistentVolumeClaim": {"registry": pvc(inp["size_gb"]), "jobservice": {"jobLog": pvc(2)}, "database": pvc(5), "redis": pvc(2), "trivy": pvc(5)}},
        "metrics": {"enabled": False},
    }


# ---------------------------------------------------------------- jobs
def job_install(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, app: str, inputs: Optional[Dict] = None):
    if app not in CATALOG:
        raise RuntimeError(f"unknown app {app}")
    a = CATALOG[app]
    inp = _inputs(spec, app, inputs)
    ns = a["namespace"]
    existing = credentials(store, spec, app)
    pw = existing.get("password") or _pw()
    ctx.log(f"Installing {a['title']} in namespace {ns}: " + ", ".join(f"{k}={v}" for k, v in inp.items() if k != "storage_class" or v))
    if app == "gitea":
        ctx.log(ops.apply(store, spec, gitea_docs(spec, inp, pw)).strip())
        _wait_deploy(ctx, store, spec, ns, "gitea")
        ctx.log("Creating the admin user")
        r = kube.oc(store, spec, ["exec", "-n", ns, "deploy/gitea", "--", "gitea", "admin", "user", "create", "--admin", "--username", inp["admin_user"],
                                  "--password", pw, "--email", f"{inp['admin_user']}@{spec.domain}", "--must-change-password=false"], check=False, timeout=120)
        ctx.log(r.strip()[-200:] or "admin user ready")
        ctx.log(f"Gitea: https://{inp['host']}  user {inp['admin_user']} (password on the Apps page)")
    elif app == "minio":
        ctx.log(ops.apply(store, spec, minio_docs(spec, inp, pw)).strip())
        _wait_deploy(ctx, store, spec, ns, "minio")
        ctx.log(f"MinIO console: https://{inp['host']}  S3 endpoint: https://{inp['api_host']}  user admin (password on the Apps page)")
    elif app == "grafana":
        ctx.log(ops.apply(store, spec, grafana_docs(spec, inp, pw)).strip())
        _wait_deploy(ctx, store, spec, ns, "grafana")
        ctx.log(f"Grafana: https://{inp['host']}  user admin (password on the Apps page). Datasource: cluster Prometheus via Thanos.")
    elif app == "keycloak":
        from . import operators
        ctx.log(ops.apply(store, spec, [_ns(ns)]).strip())
        operators.ensure_installed(ctx, store, spec, "rhbk-operator")
        ops.wait_for("Keycloak CRD", lambda: ops.get_opt(store, spec, "crd", "keycloaks.k8s.keycloak.org") is not None, timeout=600, interval=10, log=ctx.log)
        ctx.log(ops.apply(store, spec, keycloak_docs(spec, inp, pw)).strip())
        ops.wait_for("PostgreSQL", lambda: (ops.get(store, spec, "statefulset", "postgres", ns=ns).get("status") or {}).get("readyReplicas", 0) >= 1, timeout=900, interval=15, log=ctx.log)

        def kc_ready():
            k = ops.get(store, spec, "keycloak", "keycloak", ns=ns)
            return ops.cond_true(k, "Ready")

        def kc_tick():
            k = ops.get(store, spec, "keycloak", "keycloak", ns=ns)
            c = ops.conditions(k)
            return "keycloak: " + (c.get("Ready", {}).get("message") or c.get("HasErrors", {}).get("message") or "starting")[:160]
        ops.wait_for("Keycloak Ready", kc_ready, timeout=1200, interval=15, log=ctx.log, on_tick=kc_tick)
        ctx.log(f"Keycloak: https://{inp['host']}  initial admin credentials on the Apps page (secret keycloak-initial-admin)")
    elif app == "harbor":
        helm = tools.ensure_helm(ctx.log)
        idx = yaml.safe_load(httpx.get("https://helm.goharbor.io/index.yaml", timeout=30, follow_redirects=True).text)
        version = idx["entries"]["harbor"][0]["version"]
        ctx.log(ops.apply(store, spec, [_ns(ns), _cred_secret(ns, {"username": "admin", "password": pw, "url": f"https://{inp['host']}"})]).strip())
        kube.oc(store, spec, ["adm", "policy", "add-scc-to-user", "anyuid", "-z", "default", "-n", ns])
        values = store.dir / "apps"
        values.mkdir(exist_ok=True)
        vf = values / "harbor-values.yaml"
        vf.write_text(yaml.safe_dump(harbor_values(spec, inp, pw), sort_keys=False))
        ctx.log(f"helm upgrade --install harbor (chart {version}) with values from apps/harbor-values.yaml")
        env = dict(kube.env(store))
        env["HELM_CACHE_HOME"] = str(store.dir / "apps" / "helm")
        env["HELM_CONFIG_HOME"] = str(store.dir / "apps" / "helm")
        env["HELM_DATA_HOME"] = str(store.dir / "apps" / "helm")
        r = subprocess.run([str(helm), "upgrade", "--install", "harbor", "harbor", "--repo", "https://helm.goharbor.io", "--version", version, "-n", ns, "-f", str(vf), "--wait", "--timeout", "20m"],
                           env=env, capture_output=True, text=True, timeout=1500)
        for line in (r.stdout + r.stderr).splitlines()[-15:]:
            ctx.log("  " + line)
        if r.returncode != 0:
            raise RuntimeError(f"helm failed with exit {r.returncode}")
        ctx.log(ops.apply(store, spec, [_route("harbor", ns, inp["host"], "harbor", 80)]).strip())
        ctx.log(f"Harbor: https://{inp['host']}  user admin (password on the Apps page)")
    if spec.mirror.enabled:
        ctx.log("Disconnected cluster: the container images used here must be in the mirror (add them to 'additional images').")


def _wait_deploy(ctx, store, spec, ns: str, name: str, timeout: int = 900):
    ops.wait_for(f"{name} rollout", lambda: (ops.get(store, spec, "deployment", name, ns=ns).get("status") or {}).get("readyReplicas", 0) >= 1, timeout=timeout, interval=10, log=ctx.log,
                 on_tick=lambda: f"{name}: " + ", ".join(f"{p['metadata']['name']} {p['status'].get('phase')}" for p in (ops.get(store, spec, "pods", ns=ns).get("items") or [])[:3]))


def job_remove(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, app: str):
    a = CATALOG[app]
    ns = a["namespace"]
    if a["kind"] == "helm":
        helm = tools.ensure_helm(ctx.log)
        env = dict(kube.env(store))
        for k in ("HELM_CACHE_HOME", "HELM_CONFIG_HOME", "HELM_DATA_HOME"):
            env[k] = str(store.dir / "apps" / "helm")
        r = subprocess.run([str(helm), "uninstall", "harbor", "-n", ns], env=env, capture_output=True, text=True, timeout=600)
        ctx.log((r.stdout + r.stderr).strip()[-200:])
    if app == "grafana":
        kube.oc(store, spec, ["delete", "clusterrolebinding", "ocpdeploy-grafana-monitoring-view", "--ignore-not-found"])
    if app == "keycloak":
        kube.oc(store, spec, ["delete", "keycloak", "--all", "-n", ns, "--ignore-not-found"], check=False)
    ctx.log(f"Deleting namespace {ns} (all data of {a['title']} is removed)")
    kube.oc(store, spec, ["delete", "namespace", ns, "--ignore-not-found", "--wait=false"])
    ops.wait_for("namespace removal", lambda: ops.get_opt(store, spec, "namespace", ns) is None, timeout=900, interval=10, log=ctx.log)
    ctx.log(f"{a['title']} removed.")
