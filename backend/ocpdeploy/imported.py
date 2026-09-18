"""Connected ("imported") clusters: existing OpenShift clusters managed through a
kubeconfig, a username/password login or a token.

Nothing about them touches the disk. Each connection lives in a RAM-only tmpfs
directory (settings.IMPORTED_DIR) that is wiped at every start, belongs to the
console session that created it, and disappears on disconnect, logout, session
expiry, token expiry or app restart. Passwords are used once to obtain an OAuth
token and never kept; the token is revoked on disconnect."""
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import httpx
import yaml

from .settings import IMPORTED_DIR, CLUSTERS_DIR

_lock = threading.RLock()
_conns: Dict[str, Dict] = {}
RESERVED = {"import", "imported"}


# ---------------------------------------------------------------- registry
def _tmpfs_ok() -> bool:
    try:
        with open("/proc/mounts") as f:
            mounts = [l.split() for l in f]
        best = ""
        fstype = ""
        target = str(IMPORTED_DIR.parent)
        for m in mounts:
            mp = m[1]
            if (target == mp or target.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
                best, fstype = mp, m[2]
        return fstype in ("tmpfs", "ramfs")
    except OSError:
        return False


def reset():
    """Called at start: remove anything a previous process left in RAM."""
    with _lock:
        _conns.clear()
        if IMPORTED_DIR.exists():
            shutil.rmtree(IMPORTED_DIR, ignore_errors=True)


def exists(name: str) -> bool:
    with _lock:
        return name in _conns


def base_dir(name: str) -> Optional[Path]:
    with _lock:
        return IMPORTED_DIR if name in _conns else None


def info(name: str) -> Optional[Dict]:
    with _lock:
        c = _conns.get(name)
        return dict(c) if c else None


def list_for(session_id: str) -> List[Dict]:
    from .store import ClusterStore
    out = []
    with _lock:
        items = [(n, dict(c)) for n, c in _conns.items() if c["owner"] == session_id]
    for n, c in items:
        try:
            raw = ClusterStore(n, IMPORTED_DIR).public()
        except Exception:
            continue
        out.append({"name": n, "base_domain": raw.get("base_domain", ""), "ocp_version": raw.get("ocp_version", ""),
                    "install_method": raw.get("install_method", ""), "status": "installed", "imported": True,
                    "read_only": c["read_only"], "method": c["method"], "user": c["user"], "server": c["server"],
                    "expires": c.get("expires"), "platform": raw.get("platform", "")})
    return out


# ---------------------------------------------------------------- TLS
def _host_port(server: str):
    u = urlparse(server)
    if u.scheme != "https" or not u.hostname:
        raise ValueError("the API URL must look like https://api.<cluster>.<domain>:6443")
    return u.hostname, u.port or 443


def fetch_chain(host: str, port: int) -> List[str]:
    """PEM certificates the server presents (leaf first), via openssl s_client."""
    r = subprocess.run(["openssl", "s_client", "-connect", f"{host}:{port}", "-servername", host, "-showcerts"],
                       input="", capture_output=True, text=True, timeout=20)
    pems = re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", r.stdout, re.S)
    if not pems:
        # fall back to the leaf through Python's ssl module
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=10) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                pems = [ssl.DER_cert_to_PEM_cert(ss.getpeercert(binary_form=True)).strip()]
    return [p.strip() + "\n" for p in pems]


def describe(pem: str) -> Dict:
    from cryptography import x509
    c = x509.load_pem_x509_certificate(pem.encode())
    der = c.public_bytes(encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.DER)
    try:
        sans = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san_list = sans.get_values_for_type(x509.DNSName) + [str(i) for i in sans.get_values_for_type(x509.IPAddress)]
    except Exception:
        san_list = []
    nb = c.not_valid_before_utc if hasattr(c, "not_valid_before_utc") else c.not_valid_before
    na = c.not_valid_after_utc if hasattr(c, "not_valid_after_utc") else c.not_valid_after
    fp = lambda h: ":".join(f"{b:02X}" for b in h(der).digest())
    return {"subject": c.subject.rfc4514_string(), "issuer": c.issuer.rfc4514_string(), "serial": format(c.serial_number, "X"),
            "not_before": nb.isoformat(), "not_after": na.isoformat(), "sans": san_list, "self_signed": c.subject == c.issuer,
            "sha256": fp(hashlib.sha256), "sha1": fp(hashlib.sha1), "pem": pem}


def cert_bundle(host: str, port: int) -> Dict:
    chain = fetch_chain(host, port)
    leaf = describe(chain[0])
    return {"host": host, "port": port, "leaf": leaf, "chain": [describe(p) for p in chain], "sha256": leaf["sha256"]}


def _ssl_ctx(pems: List[str]) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cadata="".join(pems))
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN   # trust the pinned certificate itself as an anchor
    return ctx


def _pinned(host: str, port: int, sha256: str) -> List[str]:
    """Fetch the chain again and make sure the leaf is the one the user accepted."""
    chain = fetch_chain(host, port)
    got = describe(chain[0])["sha256"]
    if not sha256 or got.replace(":", "").upper() != sha256.replace(":", "").upper():
        raise ValueError(f"certificate of {host}:{port} does not match the one you accepted (now {got[:23]}…); check it again")
    return chain


# ---------------------------------------------------------------- kubeconfig
FORBIDDEN_USER_KEYS = {"exec", "auth-provider", "tokenFile", "client-certificate", "client-key", "username", "password", "as", "as-groups", "as-user-extra"}


def parse_kubeconfig(text: str, context: Optional[str] = None) -> Dict:
    """Validate an uploaded kubeconfig and pick one context. Only inline credentials are
    accepted: exec plugins, auth providers and file references would run programs or
    read files on the installer host."""
    try:
        kc = yaml.safe_load(text)
    except yaml.YAMLError as ex:
        raise ValueError(f"not valid YAML: {str(ex)[:120]}")
    if not isinstance(kc, dict) or kc.get("kind") not in (None, "Config"):
        raise ValueError("not a kubeconfig")
    ctxs = {c.get("name"): c.get("context", {}) for c in kc.get("contexts") or []}
    if not ctxs:
        raise ValueError("kubeconfig has no contexts")
    name = context or kc.get("current-context") or next(iter(ctxs))
    if name not in ctxs:
        raise ValueError(f"context {name} not found")
    ctx = ctxs[name]
    cl = next((c.get("cluster", {}) for c in kc.get("clusters") or [] if c.get("name") == ctx.get("cluster")), None)
    us = next((u.get("user", {}) for u in kc.get("users") or [] if u.get("name") == ctx.get("user")), None)
    if cl is None or us is None:
        raise ValueError(f"context {name} references a missing cluster or user")
    bad = sorted(k for k in us if k in FORBIDDEN_USER_KEYS and us.get(k))
    if bad:
        raise ValueError(f"unsupported credentials in the kubeconfig ({', '.join(bad)}); use one with an inline token or client certificate, or log in with username and password")
    if cl.get("certificate-authority") and not cl.get("certificate-authority-data"):
        raise ValueError("the cluster CA is a file reference; use a kubeconfig with certificate-authority-data (oc config view --flatten)")
    if not (us.get("token") or (us.get("client-certificate-data") and us.get("client-key-data"))):
        raise ValueError("the selected user has no inline token or client certificate")
    server = cl.get("server", "")
    _host_port(server)
    return {"contexts": list(ctxs), "context": name, "server": server, "ca_data": cl.get("certificate-authority-data", ""),
            "tls_server_name": cl.get("tls-server-name", ""), "user": {k: v for k, v in us.items() if k in ("token", "client-certificate-data", "client-key-data")}}


def build_kubeconfig(server: str, ca_pems: List[str], user: Dict, tls_server_name: str = "") -> Dict:
    cluster = {"server": server, "certificate-authority-data": base64.b64encode("".join(ca_pems).encode()).decode()}
    if tls_server_name:
        cluster["tls-server-name"] = tls_server_name
    return {"apiVersion": "v1", "kind": "Config", "current-context": "ocpdeploy",
            "clusters": [{"name": "cluster", "cluster": cluster}], "users": [{"name": "user", "user": user}],
            "contexts": [{"name": "ocpdeploy", "context": {"cluster": "cluster", "user": "user", "namespace": "default"}}]}


# ---------------------------------------------------------------- OAuth (username / password)
def oauth_endpoint(server: str, api_pems: Optional[List[str]] = None) -> str:
    verify = _ssl_ctx(api_pems) if api_pems else False
    r = httpx.get(server.rstrip("/") + "/.well-known/oauth-authorization-server", verify=verify, timeout=15)
    r.raise_for_status()
    ep = r.json().get("authorization_endpoint", "")
    if not ep.startswith("https://"):
        raise ValueError("the cluster did not advertise an OAuth server")
    return ep


def oauth_login(authorize_url: str, oauth_pems: List[str], username: str, password: str) -> Dict:
    r = httpx.get(authorize_url, params={"response_type": "token", "client_id": "openshift-challenging-client"},
                  headers={"X-CSRF-Token": "1"}, auth=(username, password), verify=_ssl_ctx(oauth_pems),
                  follow_redirects=False, timeout=30)
    if r.status_code == 401:
        raise PermissionError("login failed: wrong username or password (or the identity provider does not accept password logins)")
    if r.status_code >= 400:
        # the OAuth server reports some rejections as plain text (e.g. kubeadmin password length)
        body = r.text.strip().splitlines()[0][:200] if r.text.strip() else f"HTTP {r.status_code}"
        raise PermissionError(f"login failed: {body.removeprefix('Error: ')}")
    loc = r.headers.get("location", "")
    frag = parse_qs(urlparse(loc).fragment)
    if r.status_code not in (302, 303) or "access_token" not in frag:
        err = parse_qs(urlparse(loc).query).get("error_description", [""])[0] or f"HTTP {r.status_code}"
        raise PermissionError(f"login failed: {err}")
    return {"token": frag["access_token"][0], "expires_in": int(frag.get("expires_in", ["86400"])[0])}


def _token_object_name(token: str) -> Optional[str]:
    if not token.startswith("sha256~"):
        return None
    digest = hashlib.sha256(token[len("sha256~"):].encode()).digest()
    return "sha256~" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


# ---------------------------------------------------------------- discovery
def _oc(oc: str, kubeconfig: Path, args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    env = {"KUBECONFIG": str(kubeconfig), "HOME": str(kubeconfig.parent), "PATH": os.environ.get("PATH", "/usr/bin")}
    return subprocess.run([oc] + args, env=env, capture_output=True, text=True, timeout=timeout)


def _ocj(oc, kc, args):
    r = _oc(oc, kc, args + ["-o", "json"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return json.loads(r.stdout or "{}")


def discover(oc: str, kc: Path) -> Dict:
    d: Dict = {}
    who = _oc(oc, kc, ["whoami"])
    if who.returncode != 0:
        raise PermissionError("the cluster rejected the credentials: " + (who.stderr.strip()[-200:] or "unauthorized"))
    d["user"] = who.stdout.strip()
    can = _oc(oc, kc, ["auth", "can-i", "*", "*", "--all-namespaces"])
    d["cluster_admin"] = can.stdout.strip() == "yes"
    try:
        cv = _ocj(oc, kc, ["get", "clusterversion", "version"])
        d["version"] = cv["status"]["desired"]["version"]
    except Exception as ex:
        raise RuntimeError(f"not an OpenShift 4 cluster, or no read access to clusterversion: {str(ex)[-160:]}")
    try:
        infra = _ocj(oc, kc, ["get", "infrastructure", "cluster"])
        d["platform"] = (infra.get("status", {}).get("platformStatus") or {}).get("type") or infra.get("status", {}).get("platform", "")
        d["infra_id"] = infra.get("status", {}).get("infrastructureName", "")
    except Exception:
        d["platform"], d["infra_id"] = "", ""
    try:
        dns = _ocj(oc, kc, ["get", "dns.config", "cluster"])
        d["domain"] = dns["spec"]["baseDomain"]
    except Exception:
        d["domain"] = ""
    try:
        net = _ocj(oc, kc, ["get", "network.config", "cluster"])
        d["cluster_network"] = net["spec"]["clusterNetwork"][0]["cidr"]
        d["host_prefix"] = net["spec"]["clusterNetwork"][0].get("hostPrefix", 23)
        d["service_network"] = net["spec"]["serviceNetwork"][0]
    except Exception:
        pass
    try:
        cm = _ocj(oc, kc, ["get", "configmap", "cluster-config-v1", "-n", "kube-system"])
        ic = yaml.safe_load(cm["data"]["install-config"])
        d["machine_cidr"] = ic["networking"]["machineNetwork"][0]["cidr"]
    except Exception:
        pass
    try:
        ips = _ocj(oc, kc, ["get", "ipaddresses.ipam.cluster.x-k8s.io", "-n", "openshift-machine-api"])
        items = ips.get("items", [])
        if items:
            d["gateway"] = items[0]["spec"].get("gateway", "")
            if not d.get("machine_cidr") and items[0]["spec"].get("prefix"):
                d["machine_cidr"] = str(ipaddress.ip_network(f"{items[0]['spec']['address']}/{items[0]['spec']['prefix']}", strict=False))
    except Exception:
        pass
    try:
        ms = _ocj(oc, kc, ["get", "machines", "-n", "openshift-machine-api"])
        d["machine_api"] = bool(ms.get("items"))
    except Exception:
        d["machine_api"] = False
    nodes = []
    try:
        for n in _ocj(oc, kc, ["get", "nodes"]).get("items", []):
            labels = n["metadata"].get("labels", {})
            role = "master" if ("node-role.kubernetes.io/master" in labels or "node-role.kubernetes.io/control-plane" in labels) else \
                   "infra" if "node-role.kubernetes.io/infra" in labels else "worker"
            ip = next((a["address"] for a in n["status"].get("addresses", []) if a["type"] == "InternalIP"), "")
            nodes.append({"name": n["metadata"]["name"].split(".")[0], "role": role, "ip": ip})
    except Exception:
        pass
    d["nodes"] = nodes
    return d


# ---------------------------------------------------------------- connect / disconnect
def _free_name(wanted: str) -> str:
    base = (wanted or "cluster").lower()
    base = re.sub(r"[^a-z0-9-]", "-", base).strip("-")[:28] or "cluster"
    name, i = base, 2
    while name in RESERVED or (CLUSTERS_DIR / name / "cluster.json").exists() or exists(name):
        name, i = f"{base}-{i}", i + 1
    return name


def connect(owner: str, method: str, server: str = "", api_sha256: str = "", oauth_sha256: str = "", kubeconfig: str = "",
            context: str = "", username: str = "", password: str = "", token: str = "", name: str = "", read_only: bool = False,
            log=lambda *_: None) -> Dict:
    from .models import ClusterSpec
    from .store import ClusterStore
    from .services import tools
    if not _tmpfs_ok():
        raise RuntimeError(f"{IMPORTED_DIR.parent} is not a RAM filesystem; refusing to hold cluster credentials there")
    if name and (not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?", name) or name in RESERVED):
        raise ValueError("name must be a lowercase DNS label")
    user_entry: Dict
    ca_extra: List[str] = []
    tls_server_name = ""
    expires = None
    oauth_token = None
    if method == "kubeconfig":
        k = parse_kubeconfig(kubeconfig, context or None)
        server = k["server"]
        user_entry = k["user"]
        tls_server_name = k["tls_server_name"]
        if k["ca_data"]:
            ca_extra = [base64.b64decode(k["ca_data"]).decode(errors="replace")]
    elif method == "password":
        if not (username and password):
            raise ValueError("username and password are required")
    elif method == "token":
        if not token.strip():
            raise ValueError("token is required")
        user_entry = {"token": token.strip()}
    else:
        raise ValueError("unknown method")
    host, port = _host_port(server)
    api_chain = _pinned(host, port, api_sha256)
    if method == "password":
        ep = oauth_endpoint(server, api_chain)
        oh, op = _host_port(ep)
        oauth_chain = _pinned(oh, op, oauth_sha256)
        tok = oauth_login(ep, oauth_chain, username, password)
        password = ""   # not kept anywhere
        user_entry = {"token": tok["token"]}
        oauth_token = tok["token"]
        expires = time.time() + tok["expires_in"]
    # stage in RAM under a temporary name, discover, then rename to the final name
    IMPORTED_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(IMPORTED_DIR, 0o700)
    stage = IMPORTED_DIR / f".stage-{os.urandom(6).hex()}"
    stage.mkdir(mode=0o700)
    try:
        kc_path = stage / "kubeconfig"
        fd = os.open(kc_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(build_kubeconfig(server, api_chain + ca_extra, user_entry, tls_server_name), f)
        oc = tools.any_oc()
        if not oc:
            raise RuntimeError("no oc client available on this host yet; download tools for any version on a cluster profile first")
        d = discover(str(oc), kc_path)
        try:
            tools.ensure_oc(d["version"], log)
        except Exception as ex:
            log(f"could not download oc {d['version']} ({ex}); using {oc}")
        domain = d.get("domain", "")
        cname, _, base = domain.partition(".")
        with _lock:
            final = name or _free_name(cname or (host.split(".")[1] if host.count(".") > 1 else "cluster"))
            if final in RESERVED or exists(final) or (CLUSTERS_DIR / final / "cluster.json").exists():
                raise FileExistsError(f"a cluster named {final} already exists; choose another name")
            nodes = []
            seen = set()
            for n in d.get("nodes", []):
                try:
                    ipaddress.ip_address(n["ip"])
                except ValueError:
                    continue
                nm = n["name"] if re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", n["name"]) else re.sub(r"[^a-z0-9-]", "-", n["name"].lower()).strip("-")
                if nm and nm not in seen:
                    seen.add(nm)
                    nodes.append({"name": nm, "role": n["role"], "ip": n["ip"]})
            masters = sum(1 for n in nodes if n["role"] == "master")
            workers = sum(1 for n in nodes if n["role"] != "master")
            topology = "sno" if masters == 1 else ("compact" if workers == 0 else "standard")
            apps_host = f"console-openshift-console.apps.{domain}" if domain else host
            spec = ClusterSpec.model_validate({
                "name": final, "base_domain": base or domain, "ocp_version": d["version"], "cluster_domain": domain,
                "install_method": "ipi" if d.get("machine_api") else "agent", "topology": topology, "status": "installed",
                "imported": True, "read_only": bool(read_only or not d.get("cluster_admin")), "platform": d.get("platform", ""),
                "lb": {"mode": "external", "external_api": {"fqdn": host, "ip": host}, "external_apps": {"fqdn": apps_host, "ip": apps_host}},
                "network": {k: v for k, v in {"machine_cidr": d.get("machine_cidr", ""), "gateway": d.get("gateway", ""),
                                              "cluster_network": d.get("cluster_network"), "host_prefix": d.get("host_prefix"),
                                              "service_network": d.get("service_network")}.items() if v},
                "nodes": nodes,
            })
            target = IMPORTED_DIR / final
            store = ClusterStore(final, IMPORTED_DIR)
            store.dir = stage   # create() writes the profile into the staging dir first
            store.spec_file, store.db_file = stage / "cluster.json", stage / "state.sqlite"
            store.install_dir, store.logs_dir = stage / "install", stage / "logs"
            store.create(spec)
            (stage / "install" / "auth").mkdir(parents=True, exist_ok=True)
            os.replace(kc_path, stage / "install" / "auth" / "kubeconfig")
            os.rename(stage, target)
            store = ClusterStore(final, IMPORTED_DIR)
            if d.get("infra_id"):
                store.kv_set("infra_id", d["infra_id"])
            _conns[final] = {"owner": owner, "method": method, "user": d["user"], "server": server, "read_only": spec.read_only,
                             "cluster_admin": d.get("cluster_admin", False), "created": time.time(), "expires": expires,
                             "oauth_token": oauth_token, "orphaned": False}
        return {"name": final, "user": d["user"], "version": d["version"], "platform": d.get("platform", ""),
                "read_only": spec.read_only, "cluster_admin": d.get("cluster_admin", False),
                "read_only_reason": ("requested" if read_only else "") or ("" if d.get("cluster_admin") else f"{d['user']} is not cluster-admin")}
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def disconnect(name: str, force: bool = False) -> bool:
    """Revoke a password-login token, delete the RAM directory, forget the connection."""
    from . import jobs
    from .store import ClusterStore
    with _lock:
        c = _conns.get(name)
        if not c:
            return False
        store = ClusterStore(name, IMPORTED_DIR)
        if not force and jobs.running_job(store):
            raise RuntimeError("a job is running on this cluster; wait for it or cancel it first")
        tok = c.get("oauth_token")
        if tok:
            obj = _token_object_name(tok)
            kc = store.install_dir / "auth" / "kubeconfig"
            from .services import tools
            oc = tools.any_oc()
            if obj and oc and kc.exists():
                try:
                    r = _oc(str(oc), kc, ["delete", "useroauthaccesstoken", obj], timeout=30)
                    if r.returncode != 0:
                        _oc(str(oc), kc, ["delete", "oauthaccesstoken", obj], timeout=30)
                except Exception:
                    pass
        _conns.pop(name, None)
        shutil.rmtree(IMPORTED_DIR / name, ignore_errors=True)
        return True


def session_ended(session_id: str):
    """Logout / expiry: drop that session's connections (those with a running job are
    marked and dropped by the sweeper when the job ends)."""
    with _lock:
        names = [n for n, c in _conns.items() if c["owner"] == session_id]
    for n in names:
        try:
            disconnect(n)
        except RuntimeError:
            with _lock:
                if n in _conns:
                    _conns[n]["orphaned"] = True


def sweep(live_sessions: List[str]):
    now = time.time()
    with _lock:
        names = [n for n, c in _conns.items() if c["orphaned"] or c["owner"] not in live_sessions or (c.get("expires") and now > c["expires"])]
    for n in names:
        try:
            disconnect(n)
        except RuntimeError:
            with _lock:
                if n in _conns:
                    _conns[n]["orphaned"] = True


def start_sweeper():
    from . import auth

    def loop():
        while True:
            time.sleep(60)
            try:
                sweep(auth.live_session_ids())
            except Exception:
                pass
    threading.Thread(target=loop, name="imported-sweeper", daemon=True).start()
