"""OneFS Platform API checks for the Dell PowerScale CSI driver.

Port of check-powerscale.py (github.com/bentech4u/dell-powerscale-csi) to the app's pre-check
rows. Every request is read-only except create_path(), which the user triggers explicitly.
OneFS 9.15+ refuses basic auth on the Platform API, so session auth (isiAuthType 1) is tried too.
"""
import base64
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..imported import fetch_chain, describe

# Dell CSM docs, PowerScale helm installation: privileges the API user must hold
REQUIRED_PRIVS = {
    "ISI_PRIV_LOGIN_PAPI": "r",
    "ISI_PRIV_NFS": "rw",
    "ISI_PRIV_QUOTA": "rw",
    "ISI_PRIV_SNAPSHOT": "rw",
    "ISI_PRIV_IFS_RESTORE": "r",
    "ISI_PRIV_NS_IFS_ACCESS": "r",
    "ISI_PRIV_IFS_BACKUP": "r",
    "ISI_PRIV_AUTH_ZONES": "r",
    "ISI_PRIV_STATISTICS": "r",
}
REPLICATION_PRIVS = {"ISI_PRIV_SYNCIQ": "rw"}
GOOD_LICENSE = ("Licensed", "Evaluation", "Activated")


class OneFS:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: int = 15):
        self.base = f"https://{host}:{port}"
        self.user, self.password, self.timeout = user, password, timeout
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.cookie: Optional[str] = None
        self.csrf: Optional[str] = None
        self.default_auth = "basic"

    def _auth(self, req, auth):
        auth = auth or self.default_auth
        if auth == "basic":
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        elif auth == "session" and self.cookie:
            req.add_header("Cookie", self.cookie)
            req.add_header("X-CSRF-Token", self.csrf or "")
            req.add_header("Referer", self.base)

    def req(self, method: str, path: str, body=None, auth: Optional[str] = None, headers: Optional[Dict] = None, noauth: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Accept", "application/json")
        if data is not None:
            r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        if not noauth:
            self._auth(r, auth)
        try:
            with urllib.request.urlopen(r, timeout=self.timeout, context=self.ctx) as resp:
                return resp.status, resp.headers, _json(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, e.headers, _json(e.read())

    def get(self, path: str, auth: Optional[str] = None):
        st, _, d = self.req("GET", path, auth=auth)
        return st, d

    def login_session(self):
        body = {"username": self.user, "password": self.password, "services": ["platform", "namespace"]}
        st, headers, d = self.req("POST", "/session/1/session", body, noauth=True)
        if st in (200, 201):
            cookies = "; ".join(headers.get_all("Set-Cookie") or [])
            sess = re.search(r"isisessid=([^;]+)", cookies)
            csrf = re.search(r"isicsrf=([^;]+)", cookies)
            if sess:
                self.cookie = f"isisessid={sess.group(1)}" + (f"; isicsrf={csrf.group(1)}" if csrf else "")
                self.csrf = csrf.group(1) if csrf else None
        return st, d

    def logout(self):
        if self.cookie:
            try:
                self.req("DELETE", "/session/1/session", auth="session")
            except Exception:
                pass


def _json(payload: bytes):
    try:
        return json.loads(payload.decode() or "null")
    except Exception:
        return payload.decode(errors="replace")


def err_text(d) -> str:
    if isinstance(d, dict) and "errors" in d:
        return "; ".join(e.get("message", str(e)) for e in d["errors"])
    text = str(d)
    if "<html" in text.lower():
        text = re.sub(r"<title>.*?</title>", "", text, flags=re.S | re.I)
        text = " ".join(re.sub(r"<[^>]+>", " ", text).split())
    return text[:200]


def _tcp(host: str, port: int, timeout: int = 5) -> Tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except Exception as e:
        return False, str(e)


def _is_ip(s: str) -> bool:
    return bool(re.match(r"^\d+\.\d+\.\d+\.\d+$", s or ""))


def host_of(endpoint: str) -> str:
    return (endpoint or "").replace("https://", "").replace("http://", "").strip().strip("/")


def connect(a: Dict) -> Tuple[OneFS, int]:
    """Log in the way the driver will; returns the client and the isiAuthType that works."""
    api = OneFS(host_of(a["endpoint"]), int(a.get("port") or 8080), a["username"], a["password"])
    st, d = api.get("/platform/1/cluster/config", auth="basic")
    if st == 200:
        return api, 0
    st, d = api.login_session()
    if st in (200, 201) and api.cookie:
        api.default_auth = "session"
        return api, 1
    raise RuntimeError(f"login to {api.base} failed: HTTP {st} {err_text(d)}")


def create_path(a: Dict) -> str:
    """Create isiPath (mode 0777) on the array. Explicit user action only."""
    api, _ = connect(a)
    try:
        path = a["isi_path"].rstrip("/")
        if not path.startswith("/ifs/"):
            raise ValueError("isiPath must be under /ifs/")
        st, _, d = api.req("PUT", "/namespace" + urllib.parse.quote(path) + "?recursive=true",
                           headers={"x-isi-ifs-target-type": "container", "x-isi-ifs-access-control": "0777"})
        if st not in (200, 201):
            raise RuntimeError(f"PUT {path}: HTTP {st} {err_text(d)}")
        return f"created {path} on {a['endpoint']}"
    finally:
        api.logout()


def check_array(a: Dict, need_quota: bool = True, need_snapshots: bool = True, need_replication: bool = False) -> Tuple[List[Dict], Dict]:
    """a: name, endpoint, port, username, password, zone, isi_path, az_service_ip.
    Returns (rows, facts). 'fail' rows block an install."""
    rows: List[Dict] = []
    facts: Dict = {}
    scope = f"array {a.get('name') or a.get('endpoint')}"

    def add(name, status, expected, actual, hint=""):
        rows.append({"host": scope, "name": name, "status": status, "expected": expected, "actual": actual, "hint": hint})

    host = host_of(a.get("endpoint", ""))
    port = int(a.get("port") or 8080)
    user, password = a.get("username", ""), a.get("password", "")
    zone = a.get("zone") or "System"
    ipath = (a.get("isi_path") or "/ifs/data/csi").rstrip("/")
    if not host or not user:
        add("endpoint and user", "fail", "set", "missing", "Fill in the array endpoint and API user")
        return rows, facts
    if not password:
        add("password", "fail", "set", "missing", "The password is needed to check the array (it is not stored by the app)")
        return rows, facts

    # network
    ip = host
    if not _is_ip(host):
        try:
            ip = socket.gethostbyname(host)
            add(f"DNS {host}", "pass", "resolves", ip)
        except Exception as e:
            add(f"DNS {host}", "fail", "resolves", str(e)[:100], "The installer host must resolve the endpoint")
            return rows, facts
    ok, msg = _tcp(ip, port)
    add(f"TCP {host}:{port}", "pass" if ok else "fail", "OneFS API reachable", "open" if ok else msg[:100],
        "" if ok else "Check the endpoint/port and firewalls between the installer and the array")
    if not ok:
        return rows, facts
    nfs = a.get("az_service_ip") or host
    nfs_ip = nfs
    if not _is_ip(nfs):
        try:
            nfs_ip = socket.gethostbyname(nfs)
        except Exception as e:
            add(f"DNS {nfs} (NFS)", "warn", "resolves", str(e)[:100], "Cluster nodes must resolve the SmartConnect name (DNS delegation)")
            nfs_ip = ""
    if nfs_ip:
        for p, what, lvl in ((2049, "NFS", "fail"), (111, "rpcbind", "warn")):
            ok, msg = _tcp(nfs_ip, p)
            add(f"TCP {nfs}:{p} ({what})", "pass" if ok else lvl, "open", "open" if ok else msg[:100],
                "" if ok else "NFS data path: nodes mount from this address")

    # TLS
    try:
        chain = fetch_chain(ip, port)
        leaf = describe(chain[0])
        facts["cert"] = {"subject": leaf.get("subject"), "issuer": leaf.get("issuer"), "not_after": leaf.get("not_after"),
                         "sha256": leaf.get("sha256"), "self_signed": leaf.get("subject") == leaf.get("issuer"),
                         "chain_pem": "\n".join(chain)}
        verified = True
        try:
            vctx = ssl.create_default_context()
            with socket.create_connection((ip, port), timeout=8) as s:
                with vctx.wrap_socket(s, server_hostname=host):
                    pass
        except Exception:
            verified = False
        facts["cert_trusted"] = verified
        add("TLS certificate", "pass" if verified else "warn", "trusted",
            "trusted by the installer" if verified else f"not trusted ({leaf.get('issuer', '?')[:60]})",
            "" if verified else "Either skip certificate validation, or trust the array's CA (put it in isilon-certs-0)")
        exp = leaf.get("not_after")
        if exp:
            try:
                days = (datetime.fromisoformat(exp.replace("Z", "+00:00")) - datetime.now(timezone.utc)).days
                add("certificate expiry", "pass" if days > 30 else "warn", "> 30 days", f"{exp[:10]} ({days} days)")
            except Exception:
                pass
    except Exception as e:
        add("TLS handshake", "warn", "ok", str(e)[:120])

    # auth
    api = OneFS(ip, port, user, password)
    st, d = api.get("/platform/1/cluster/identity", auth="none")
    if st == 200 and isinstance(d, dict):
        facts["cluster_name"] = d.get("name", "")
    st, d = api.get("/platform/1/cluster/config", auth="basic")
    basic_ok = st == 200
    st2, d2 = api.login_session()
    session_ok = False
    if st2 in (200, 201) and api.cookie:
        st3, d3 = api.get("/platform/1/cluster/config", auth="session")
        session_ok = st3 == 200
        if session_ok:
            d = d3
    if session_ok:
        facts["auth_type"] = 1
        api.default_auth = "session"
        add("authentication", "pass", "basic or session", "session auth works" + ("" if basic_ok else " (basic refused)") + " -> isiAuthType 1")
    elif basic_ok:
        facts["auth_type"] = 0
        add("authentication", "pass", "basic or session", "basic auth works, sessions do not -> isiAuthType 0")
    else:
        add("authentication", "fail", "basic or session", f"HTTP {st2}: {err_text(d2)}",
            "Wrong password, or the user lacks ISI_PRIV_LOGIN_PAPI")
        return rows, facts
    try:
        if isinstance(d, dict):
            rel = (d.get("onefs_version") or {}).get("release", "")
            facts["onefs"] = rel
            facts.setdefault("cluster_name", d.get("name", ""))
            facts["guid"] = d.get("guid", "")
            add("OneFS version", "pass", ">= 9.5", rel or "?")
            try:
                major, minor = (int(x) for x in rel.lstrip("v").split(".")[:2])
                if (major, minor) < (9, 5):
                    rows[-1].update(status="warn", hint="Older than 9.5; check Dell's support matrix")
            except Exception:
                pass

        # zones
        st, d = api.get("/platform/1/zones")
        if st == 200:
            zones = {z["name"]: z.get("path", "") for z in d.get("zones", [])}
            facts["zones"] = zones
            if zone in zones:
                inside = zones[zone] == "/ifs" or (ipath + "/").startswith(zones[zone].rstrip("/") + "/")
                add(f"access zone {zone}", "pass" if inside else "fail", f"exists, contains {ipath}", f"root {zones[zone]}",
                    "" if inside else f"isiPath must be inside the zone root {zones[zone]}")
            else:
                add(f"access zone {zone}", "fail", "exists", "not found", "Zones: " + ", ".join(zones))
        else:
            add("list access zones", "warn", "HTTP 200", f"HTTP {st}", "Needs ISI_PRIV_AUTH_ZONES")

        # isiPath
        st, _, d = api.req("GET", "/namespace" + urllib.parse.quote(ipath) + "?metadata")
        if st == 200:
            attrs = {x.get("name"): x.get("value") for x in (d.get("attrs", []) if isinstance(d, dict) else [])}
            add(f"isiPath {ipath}", "pass", "exists", f"mode {attrs.get('mode', '?')} owner {attrs.get('owner', '?')}")
            facts["path_exists"] = True
        elif st == 404:
            add(f"isiPath {ipath}", "fail", "exists", "missing", "Create it on the array, or press 'Create path'")
            facts["path_exists"] = False
        else:
            add(f"isiPath {ipath}", "warn", "exists", f"HTTP {st} {err_text(d)}")

        # NFS
        st, d = api.get("/platform/3/protocols/nfs/settings/global")
        if st == 200:
            s = d.get("settings", d)
            add("NFS service", "pass" if s.get("service") else "fail", "enabled", "enabled" if s.get("service") else "disabled",
                "" if s.get("service") else "isi services nfs enable")
            facts["nfsv3"], facts["nfsv4"] = bool(s.get("nfsv3_enabled")), bool(s.get("nfsv4_enabled"))
        else:
            add("NFS service", "warn", "readable", f"HTTP {st}", "Needs ISI_PRIV_NFS")

        # licenses
        st, d = api.get("/platform/5/license/licenses")
        if st == 404:
            st, d = api.get("/platform/1/license/licenses")
        if st == 200:
            lic = {str(l.get("name") or l.get("id")).upper(): l.get("status") for l in d.get("licenses", [])}
            facts["licenses"] = lic
            for name, needed, why in (("SmartQuotas", need_quota, "volume size limits (enableQuota)"),
                                      ("SnapshotIQ", need_snapshots, "volume snapshots"),
                                      ("SyncIQ", need_replication, "replication")):
                status = lic.get(name.upper(), "not listed")
                good = status in GOOD_LICENSE
                lvl = "pass" if good else ("fail" if needed and name in ("SmartQuotas", "SyncIQ") else "warn" if needed else "info")
                add(f"license {name}", lvl, "licensed" if needed else "optional", status, "" if good else f"Needed for {why}")
        else:
            add("licenses", "warn", "readable", f"HTTP {st} {err_text(d)}")

        # privileges (the caller's own token needs no extra rights)
        have: Dict[str, bool] = {}
        st, d = api.get("/platform/1/auth/id")
        if st == 200 and isinstance(d, dict):
            for p in (d.get("ntoken") or {}).get("privilege", []):
                have[p.get("id")] = have.get(p.get("id"), False) or not p.get("read_only", True)
            need = dict(REQUIRED_PRIVS)
            if need_replication:
                need.update(REPLICATION_PRIVS)
            missing = []
            for pid, lvl in need.items():
                if pid not in have:
                    missing.append((pid, lvl))
                elif lvl == "rw" and not have[pid]:
                    missing.append((pid, lvl))
            if missing:
                rd = " ".join(f"--add-priv-read {p}" for p, l in missing if l == "r")
                wr = " ".join(f"--add-priv-write {p}" for p, l in missing if l == "rw")
                add("API user privileges", "fail", "Dell's required list", "missing " + ", ".join(f"{p} ({l})" for p, l in missing),
                    f"On the array: isi auth roles modify <role> --zone System {rd} {wr}".strip())
            else:
                add("API user privileges", "pass", "Dell's required list", f"all {len(need)} present")
            facts["privileges"] = have
        else:
            add("API user privileges", "warn", "readable", f"HTTP {st}", "Verify the role privileges by hand")

        # SyncIQ service (replication)
        if need_replication:
            st, d = api.get("/platform/3/sync/settings")
            if st == 200:
                svc = (d.get("settings") or {}).get("service", "?")
                add("SyncIQ service", "pass" if svc == "on" else "fail", "on", svc, "" if svc == "on" else "isi sync settings modify --service on")
            else:
                add("SyncIQ service", "warn", "readable", f"HTTP {st} {err_text(d)}")

        # SmartConnect pools of the zone
        st, d = api.get("/platform/3/network/pools")
        if st == 200:
            pools = [p for p in d.get("pools", []) if p.get("access_zone") == zone]
            facts["pools"] = [{"name": f"{p.get('groupnet')}.{p.get('subnet')}.{p.get('name')}", "smartconnect": p.get("sc_dns_zone") or "",
                               "ranges": [f"{r.get('low')}-{r.get('high')}" for r in p.get("ranges", [])]} for p in pools]
            if pools:
                p = pools[0]
                facts["az_service_ip"] = p.get("sc_dns_zone") or ((p.get("ranges") or [{}])[0].get("low") or "")
                add(f"network pool for zone {zone}", "pass", "present",
                    f"{facts['pools'][0]['name']} smartconnect={p.get('sc_dns_zone') or '-'}")
            else:
                add(f"network pool for zone {zone}", "warn", "present", "none", "NFS clients would use the API address")
        else:
            add("SmartConnect pools", "info", "readable (optional)", f"HTTP {st}",
                "The API user cannot read network pools (optional privilege ISI_PRIV_NETWORK_GROUPNET_SUBNET_POOL); enter the NFS address (AzServiceIP) by hand")
    finally:
        api.logout()
    return rows, facts
