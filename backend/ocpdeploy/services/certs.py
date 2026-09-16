"""API and ingress serving certificates: bring your own, or Let's Encrypt via cert-manager DNS-01."""
import socket
import ssl
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, clusterops as ops

API_SECRET = "ocpdeploy-api-cert"
APPS_SECRET = "ocpdeploy-apps-cert"
ISSUER = "ocpdeploy-letsencrypt"
CM_NS = "cert-manager"


def _peek(host: str, port: int, sni: str) -> Dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=6) as s:
        with ctx.wrap_socket(s, server_hostname=sni) as ss:
            der = ss.getpeercert(binary_form=True)
    from cryptography import x509
    c = x509.load_der_x509_certificate(der)
    try:
        sans = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except Exception:
        sans = []
    na = c.not_valid_after_utc if hasattr(c, "not_valid_after_utc") else c.not_valid_after.replace(tzinfo=timezone.utc)
    return {"subject": c.subject.rfc4514_string(), "issuer": c.issuer.rfc4514_string(), "not_after": na.isoformat(),
            "days_left": (na - datetime.now(timezone.utc)).days, "sans": sans, "self_signed": c.issuer == c.subject,
            "internal_ca": "openshift" in c.issuer.rfc4514_string().lower() or "ingress-operator" in c.issuer.rfc4514_string().lower()}


def status(store: ClusterStore, spec: ClusterSpec) -> Dict:
    out: Dict = {}
    for key, host, port, sni in (("api", spec.api_ip, 6443, f"api.{spec.domain}"), ("apps", spec.apps_ip, 443, f"console-openshift-console.apps.{spec.domain}")):
        try:
            out[key] = _peek(host, port, sni) if host else {"error": "no address"}
        except Exception as ex:
            out[key] = {"error": str(ex)[-120:]}
    api = ops.get_opt(store, spec, "apiserver", "cluster") or {}
    out["api_named_certs"] = (api.get("spec") or {}).get("servingCerts", {}).get("namedCertificates", [])
    ic = ops.get_opt(store, spec, "ingresscontroller", "default", ns="openshift-ingress-operator") or {}
    out["ingress_default_cert"] = ((ic.get("spec") or {}).get("defaultCertificate") or {}).get("name", "")
    cm = ops.get_opt(store, spec, "crd", "certificates.cert-manager.io")
    out["cert_manager"] = cm is not None
    if cm:
        for key, ns in (("api", "openshift-config"), ("apps", "openshift-ingress")):
            c = ops.get_opt(store, spec, "certificate.cert-manager.io", f"{key}-cert", ns=ns)
            if c:
                cond = ops.conditions(c).get("Ready", {})
                out[f"{key}_certificate"] = {"ready": cond.get("status"), "message": cond.get("message", ""), "renewal": (c.get("status") or {}).get("renewalTime", "")}
    return out


def _wire(ctx, store, spec, api: bool, apps: bool):
    if api:
        ctx.log(f"Pointing the API server at secret {API_SECRET} for api.{spec.domain}")
        ops.patch(store, spec, "apiserver/cluster", {"spec": {"servingCerts": {"namedCertificates": [{"names": [f"api.{spec.domain}"], "servingCertificate": {"name": API_SECRET}}]}}})
    if apps:
        ctx.log(f"Pointing the default IngressController at secret {APPS_SECRET}")
        ops.patch(store, spec, "ingresscontroller/default", {"spec": {"defaultCertificate": {"name": APPS_SECRET}}}, ns="openshift-ingress-operator")


def _trust_ca(ctx, store, spec, ca_pem: str):
    """Append an issuing CA to the cluster-wide trust bundle (proxy/cluster trustedCA)."""
    existing = ops.get_opt(store, spec, "configmap", "user-ca-bundle", ns="openshift-config") or {}
    bundle = ((existing.get("data") or {}).get("ca-bundle.crt") or "").strip()
    if ca_pem.strip() in bundle:
        ctx.log("CA already in user-ca-bundle")
    else:
        bundle = (bundle + "\n" + ca_pem.strip() + "\n").strip() + "\n"
        ops.apply(store, spec, [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "user-ca-bundle", "namespace": "openshift-config"}, "data": {"ca-bundle.crt": bundle}}])
        ctx.log("CA appended to user-ca-bundle")
    ops.patch(store, spec, "proxy/cluster", {"spec": {"trustedCA": {"name": "user-ca-bundle"}}})


def _wait_rollout(ctx, store, spec, api: bool, apps: bool):
    import time
    time.sleep(15)
    if apps:
        ops.wait_for("ingress operator", lambda: next((o for o in ops.co_states(store, spec) if o["name"] == "ingress"), {}).get("progressing") == "False", timeout=600, interval=15, log=ctx.log)
    if api:
        ctx.log("The kube-apiserver rolls out the new certificate over the next ~10 minutes (one master at a time).")
        ops.wait_for("kube-apiserver rollout", lambda: next((o for o in ops.co_states(store, spec) if o["name"] == "kube-apiserver"), {}).get("progressing") == "False", timeout=1800, interval=30, log=ctx.log,
                     on_tick=lambda: "kube-apiserver: " + (next((o["message"] for o in ops.co_states(store, spec) if o["name"] == "kube-apiserver"), "") or "rolling")[:140])


def job_apply_custom(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    c = spec.day2.certs.custom
    api = bool(c.api_cert_pem and c.api_key_pem)
    apps = bool(c.apps_cert_pem and c.apps_key_pem)
    if not (api or apps):
        raise RuntimeError("paste a certificate and key for the API and/or the ingress wildcard")
    docs: List[Dict] = []
    if api:
        docs.append({"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls", "metadata": {"name": API_SECRET, "namespace": "openshift-config"},
                     "stringData": {"tls.crt": c.api_cert_pem.strip() + "\n", "tls.key": c.api_key_pem.strip() + "\n"}})
    if apps:
        docs.append({"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls", "metadata": {"name": APPS_SECRET, "namespace": "openshift-ingress"},
                     "stringData": {"tls.crt": c.apps_cert_pem.strip() + "\n", "tls.key": c.apps_key_pem.strip() + "\n"}})
    ctx.log(f"Creating TLS secret(s): {', '.join(d['metadata']['name'] for d in docs)}")
    ctx.log(ops.apply(store, spec, docs).strip())
    if c.ca_pem:
        _trust_ca(ctx, store, spec, c.ca_pem)
    _wire(ctx, store, spec, api, apps)
    store.patch(lambda raw: [raw["day2"]["certs"]["custom"].__setitem__(k, {"enc": ""}) for k in ("api_key_pem", "apps_key_pem")])
    _wait_rollout(ctx, store, spec, api, apps)
    ctx.log("Certificates installed.")


def _solver(spec: ClusterSpec) -> (Dict, List[Dict]):
    a = spec.day2.certs.acme
    docs: List[Dict] = []
    if a.provider == "cloudflare":
        if not a.cloudflare_token:
            raise RuntimeError("Cloudflare API token missing")
        docs.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "ocpdeploy-cloudflare", "namespace": CM_NS}, "stringData": {"api-token": a.cloudflare_token}})
        solver = {"dns01": {"cloudflare": {"apiTokenSecretRef": {"name": "ocpdeploy-cloudflare", "key": "api-token"}}}}
    elif a.provider == "route53":
        if not (a.aws_access_key and a.aws_secret_key):
            raise RuntimeError("AWS access key and secret missing")
        docs.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "ocpdeploy-route53", "namespace": CM_NS}, "stringData": {"secret-access-key": a.aws_secret_key}})
        r53 = {"region": a.aws_region or "us-east-1", "accessKeyID": a.aws_access_key, "secretAccessKeySecretRef": {"name": "ocpdeploy-route53", "key": "secret-access-key"}}
        if a.aws_hosted_zone_id:
            r53["hostedZoneID"] = a.aws_hosted_zone_id
        solver = {"dns01": {"route53": r53}}
    else:
        if not (a.rfc2136_nameserver and a.rfc2136_tsig_key_name and a.rfc2136_tsig_secret):
            raise RuntimeError("RFC2136 nameserver, TSIG key name and secret are required")
        docs.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "ocpdeploy-tsig", "namespace": CM_NS}, "stringData": {"tsig-secret": a.rfc2136_tsig_secret}})
        solver = {"dns01": {"rfc2136": {"nameserver": a.rfc2136_nameserver, "tsigKeyName": a.rfc2136_tsig_key_name, "tsigAlgorithm": a.rfc2136_tsig_algorithm or "HMACSHA256",
                                         "tsigSecretSecretRef": {"name": "ocpdeploy-tsig", "key": "tsig-secret"}}}}
    return solver, docs


def acme_docs(spec: ClusterSpec) -> List[Dict]:
    a = spec.day2.certs.acme
    if not a.email:
        raise RuntimeError("ACME account e-mail is required")
    solver, docs = _solver(spec)
    server = "https://acme-staging-v02.api.letsencrypt.org/directory" if a.staging else "https://acme-v02.api.letsencrypt.org/directory"
    docs.append({"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": ISSUER},
                 "spec": {"acme": {"server": server, "email": a.email, "privateKeySecretRef": {"name": f"{ISSUER}-account"}, "solvers": [solver]}}})
    docs.append({"apiVersion": "cert-manager.io/v1", "kind": "Certificate", "metadata": {"name": "api-cert", "namespace": "openshift-config"},
                 "spec": {"secretName": API_SECRET, "dnsNames": [f"api.{spec.domain}"], "issuerRef": {"name": ISSUER, "kind": "ClusterIssuer"}, "duration": "2160h", "renewBefore": "720h"}})
    docs.append({"apiVersion": "cert-manager.io/v1", "kind": "Certificate", "metadata": {"name": "apps-cert", "namespace": "openshift-ingress"},
                 "spec": {"secretName": APPS_SECRET, "dnsNames": [f"*.apps.{spec.domain}"], "issuerRef": {"name": ISSUER, "kind": "ClusterIssuer"}, "duration": "2160h", "renewBefore": "720h"}})
    return docs


def job_acme(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    from . import operators
    docs = acme_docs(spec)   # validate inputs before touching the cluster
    operators.ensure_installed(ctx, store, spec, "openshift-cert-manager-operator")
    ops.wait_for("cert-manager CRDs", lambda: ops.get_opt(store, spec, "crd", "clusterissuers.cert-manager.io") is not None, timeout=600, interval=10, log=ctx.log)
    ops.wait_for("cert-manager namespace", lambda: ops.get_opt(store, spec, "namespace", CM_NS) is not None, timeout=300, interval=10, log=ctx.log)
    ctx.log(f"Creating ClusterIssuer {ISSUER} ({'staging' if spec.day2.certs.acme.staging else 'production'}) and Certificates for api.{spec.domain} and *.apps.{spec.domain}")
    ctx.log(ops.apply(store, spec, docs).strip())

    def ready(ns, name):
        c = ops.get_opt(store, spec, "certificate.cert-manager.io", name, ns=ns) or {}
        return ops.conditions(c).get("Ready", {}).get("status") == "True"

    def msg(ns, name):
        c = ops.get_opt(store, spec, "certificate.cert-manager.io", name, ns=ns) or {}
        cond = ops.conditions(c)
        m = cond.get("Ready", {}).get("message") or cond.get("Issuing", {}).get("message") or "waiting"
        return f"{name}: {m[:160]}"
    for ns, name in (("openshift-config", "api-cert"), ("openshift-ingress", "apps-cert")):
        ops.wait_for(f"{name} issued", lambda: ready(ns, name), timeout=1200, interval=20, log=ctx.log, on_tick=lambda: msg(ns, name))
    _wire(ctx, store, spec, True, True)
    _wait_rollout(ctx, store, spec, True, True)
    if spec.day2.certs.acme.staging:
        ctx.log("Staging certificates are not trusted by browsers; switch to production once DNS-01 works.")
    ctx.log("Let's Encrypt certificates installed; cert-manager renews them automatically.")
