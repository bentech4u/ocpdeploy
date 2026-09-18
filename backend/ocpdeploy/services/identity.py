"""Identity providers (htpasswd, LDAP, OpenID Connect), cluster admins, kubeadmin removal."""
import base64
import hashlib
from typing import Dict, List

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, clusterops as ops

CFG = "openshift-config"
HTPASS_SECRET = "ocpdeploy-htpasswd"
LDAP_SECRET = "ocpdeploy-ldap-bind"
LDAP_CA = "ocpdeploy-ldap-ca"
OIDC_SECRET = "ocpdeploy-oidc-client"
OIDC_CA = "ocpdeploy-oidc-ca"


def _htpasswd_bcrypt(h: str) -> str:
    """OpenShift's htpasswd provider only recognises the $2y$ bcrypt prefix (what htpasswd -B
    writes); Python's bcrypt emits $2b$. The algorithm is identical, only the tag differs."""
    return "$2y$" + h[4:] if h[:4] in ("$2a$", "$2b$") else h


def _hash(password: str) -> str:
    try:
        import bcrypt
        return _htpasswd_bcrypt(bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode())
    except ImportError:
        return "{SHA}" + base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()


def _existing_htpasswd(store, spec) -> Dict[str, str]:
    sec = ops.get_opt(store, spec, "secret", HTPASS_SECRET, ns=CFG)
    if not sec:
        return {}
    data = base64.b64decode((sec.get("data") or {}).get("htpasswd", "")).decode(errors="replace")
    out = {}
    for line in data.splitlines():
        if ":" in line:
            u, h = line.split(":", 1)
            out[u] = h
    return out


def htpasswd_file(store, spec) -> str:
    """Rebuild the htpasswd file: new passwords are hashed, users with an empty
    password keep their stored hash, users removed from the list disappear."""
    old = _existing_htpasswd(store, spec)
    lines = []
    for u in spec.day2.identity.users:
        if u.password:
            lines.append(f"{u.username}:{_hash(u.password)}")
        elif u.username in old:
            lines.append(f"{u.username}:{_htpasswd_bcrypt(old[u.username])}")
        else:
            raise RuntimeError(f"user {u.username} has no password and no stored hash")
    return "\n".join(lines) + "\n"


def status(store: ClusterStore, spec: ClusterSpec) -> Dict:
    oauth = ops.get_opt(store, spec, "oauth", "cluster") or {}
    idps = [{"name": i.get("name"), "type": i.get("type")} for i in (oauth.get("spec") or {}).get("identityProviders") or []]
    users = [u["metadata"]["name"] for u in (ops.get_opt(store, spec, "users") or {}).get("items", [])]
    crbs = ops.get_opt(store, spec, "clusterrolebindings") or {}
    admins = sorted({s["name"] for b in crbs.get("items", []) if b["roleRef"]["name"] == "cluster-admin" for s in b.get("subjects", []) if s["kind"] == "User"})
    groups_admin = sorted({s["name"] for b in crbs.get("items", []) if b["roleRef"]["name"] == "cluster-admin" for s in b.get("subjects", []) if s["kind"] == "Group"})
    kubeadmin = ops.get_opt(store, spec, "secret", "kubeadmin", ns="kube-system") is not None
    auth = next((o for o in ops.co_states(store, spec) if o["name"] == "authentication"), {})
    return {"identity_providers": idps, "users": users, "cluster_admins": admins, "cluster_admin_groups": groups_admin,
            "kubeadmin_present": kubeadmin, "stored_users": sorted(_existing_htpasswd(store, spec).keys()), "authentication": auth}


def _idp_docs(store, spec) -> (List[Dict], List[Dict]):
    ident = spec.day2.identity
    docs: List[Dict] = []
    idps: List[Dict] = []
    if ident.htpasswd_enabled and ident.users:
        docs.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": HTPASS_SECRET, "namespace": CFG}, "type": "Opaque",
                     "stringData": {"htpasswd": htpasswd_file(store, spec)}})
        idps.append({"name": ident.htpasswd_name or "htpasswd", "mappingMethod": "claim", "type": "HTPasswd", "htpasswd": {"fileData": {"name": HTPASS_SECRET}}})
    if ident.ldap.enabled:
        l = ident.ldap
        if not l.url:
            raise RuntimeError("LDAP URL is empty")
        ld: Dict = {"url": l.url, "insecure": bool(l.insecure),
                    "attributes": {"id": [l.attr_id], "email": [l.attr_email], "name": [l.attr_name], "preferredUsername": [l.attr_preferred_username]}}
        if l.bind_dn:
            ld["bindDN"] = l.bind_dn
            docs.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": LDAP_SECRET, "namespace": CFG}, "type": "Opaque", "stringData": {"bindPassword": l.bind_password}})
            ld["bindPassword"] = {"name": LDAP_SECRET}
        if l.ca_pem and not l.insecure:
            docs.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": LDAP_CA, "namespace": CFG}, "data": {"ca.crt": l.ca_pem}})
            ld["ca"] = {"name": LDAP_CA}
        idps.append({"name": l.name or "ldap", "mappingMethod": "claim", "type": "LDAP", "ldap": ld})
    if ident.oidc.enabled:
        o = ident.oidc
        if not (o.issuer and o.client_id):
            raise RuntimeError("OIDC issuer and client ID are required")
        docs.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": OIDC_SECRET, "namespace": CFG}, "type": "Opaque", "stringData": {"clientSecret": o.client_secret}})
        oid: Dict = {"issuer": o.issuer, "clientID": o.client_id, "clientSecret": {"name": OIDC_SECRET},
                     "claims": {"preferredUsername": [o.claim_preferred_username], "name": [o.claim_name], "email": [o.claim_email]}}
        if o.claim_groups:
            oid["claims"]["groups"] = [o.claim_groups]
        if o.extra_scopes:
            oid["extraScopes"] = list(o.extra_scopes)
        if o.ca_pem:
            docs.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": OIDC_CA, "namespace": CFG}, "data": {"ca.crt": o.ca_pem}})
            oid["ca"] = {"name": OIDC_CA}
        idps.append({"name": o.name or "oidc", "mappingMethod": "claim", "type": "OpenID", "openID": oid})
    return docs, idps


def preview(store, spec) -> Dict:
    docs, idps = _idp_docs(store, spec)
    safe = []
    for d in docs:
        if d["kind"] == "Secret":
            d = dict(d); d["stringData"] = {k: "<redacted>" for k in d["stringData"]}
        safe.append(d)
    return {"resources": safe, "identityProviders": idps}


def job_apply(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    docs, idps = _idp_docs(store, spec)
    if docs:
        ctx.log(f"Applying {len(docs)} secret/configmap resource(s) in {CFG}")
        ctx.log(ops.apply(store, spec, docs).strip())
    ctx.log(f"Setting identity providers: {', '.join(i['name'] + ' (' + i['type'] + ')' for i in idps) or 'none'}")
    ops.patch(store, spec, "oauth/cluster", {"spec": {"identityProviders": idps}})
    admins = [u.username for u in spec.day2.identity.users if u.cluster_admin] + list(spec.day2.identity.cluster_admins)
    for u in admins:
        kube.oc(store, spec, ["adm", "policy", "add-cluster-role-to-user", "cluster-admin", u])
        ctx.log(f"cluster-admin granted to {u}")
    # passwords are consumed: keep only hashes on the cluster
    store.patch(lambda raw: [u.__setitem__("password", {"enc": ""}) for u in raw["day2"]["identity"]["users"]])

    def settled():
        auth = next((o for o in ops.co_states(store, spec) if o["name"] == "authentication"), None)
        return bool(auth) and auth["available"] == "True" and auth["progressing"] != "True" and auth["degraded"] != "True"
    import time
    time.sleep(20)
    ops.wait_for("authentication operator to roll out", settled, timeout=900, interval=15, log=ctx.log,
                 on_tick=lambda: "authentication: " + (next((o["message"] for o in ops.co_states(store, spec) if o["name"] == "authentication"), "") or "rolling out"))
    ctx.log("Identity providers active. Log in at the console with the new users.")


def job_disable_kubeadmin(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    st = status(store, spec)
    real_admins = [a for a in st["cluster_admins"] if a not in ("kube:admin", "system:admin")]
    if not st["identity_providers"]:
        raise RuntimeError("no identity provider configured; you would lock yourself out")
    if not real_admins and not st["cluster_admin_groups"]:
        raise RuntimeError("no user other than kubeadmin has cluster-admin; grant it first")
    if not st["kubeadmin_present"]:
        ctx.log("kubeadmin secret already removed")
    else:
        ctx.log(f"Removing the kubeadmin secret (cluster-admins remaining: {', '.join(real_admins) or 'via groups ' + ', '.join(st['cluster_admin_groups'])})")
        kube.oc(store, spec, ["delete", "secret", "kubeadmin", "-n", "kube-system"])
    store.patch(lambda raw: raw["day2"]["identity"].__setitem__("kubeadmin_disabled", True))
    ctx.log("Done. The kubeconfig in install/auth (system:admin, certificate based) keeps working for this app.")
