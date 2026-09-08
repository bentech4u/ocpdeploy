"""HAProxy (and keepalived for the HA layout) rendering and push over SSH.

The app owns /etc/haproxy/conf.d/ocp-<cluster>.cfg on each VM. The package
config /etc/haproxy/haproxy.cfg keeps global/defaults and any unrelated
sections (for example a Ceph RGW frontend); sections that bind the ports the
cluster needs are stripped from it on first adoption so they cannot clash."""
import io
import re
import time
from typing import Dict, List, Tuple

import paramiko
from jinja2 import Environment, FileSystemLoader

from ..models import ClusterSpec, HAProxyVM
from ..settings import TEMPLATES_DIR
from .lbcheck import backends, API_PORTS, APPS_PORTS

_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), trim_blocks=True, lstrip_blocks=True)


def owned_ports(spec: ClusterSpec, role: str) -> List[int]:
    ports = [spec.lb.stats_port]
    if role in ("api", "both"):
        ports += list(API_PORTS)
    if role in ("apps", "both"):
        ports += list(APPS_PORTS)
    return ports


def render(spec: ClusterSpec, role: str) -> str:
    pools = backends(spec)
    return _env.get_template("haproxy-ocp.cfg.j2").render(spec=spec, role=role, pools=pools, api_ports=API_PORTS, apps_ports=APPS_PORTS)


def render_keepalived(spec: ClusterSpec, vm: HAProxyVM, priority: int) -> str:
    return _env.get_template("keepalived.conf.j2").render(spec=spec, vm=vm, priority=priority,
                                                        state="MASTER" if priority >= 100 else "BACKUP")


# ---------------------------------------------------------------- cfg surgery
_SECTION_RE = re.compile(r"^(global|defaults|frontend|backend|listen|userlist|peers|resolvers|cache|program|http-errors|ring|mailers)\b", re.M)


def split_sections(cfg: str) -> List[Tuple[str, str, str]]:
    """Return [(preamble, header_line, body)] in order. Comment/blank lines that
    immediately precede a section header are its preamble, so removing a section
    also removes its banner."""
    lines = cfg.splitlines()
    out: List[Tuple[str, str, List[str]]] = []
    pre: List[str] = []
    header = None
    body: List[str] = []
    for ln in lines:
        if _SECTION_RE.match(ln):
            if header is not None:
                # trailing comments/blank lines of the previous body become this section's preamble
                trail: List[str] = []
                while body and (not body[-1].strip() or body[-1].lstrip().startswith("#")):
                    trail.insert(0, body.pop())
                out.append(("\n".join(pre), header, body))
                pre = trail
            else:
                pre = pre + body
                body = []
            header, body = ln, []
        else:
            body.append(ln)
    if header is not None:
        out.append(("\n".join(pre), header, body))
    else:
        out.append(("\n".join(pre + body), "", []))
    return [(p, h, "\n".join(b)) for p, h, b in out]


def strip_conflicts(cfg: str, ports: List[int]) -> Tuple[str, List[str]]:
    """Remove frontend/listen sections binding any of `ports` and the backends they
    reference, plus the RHEL sample sections (frontend main :5000, backend static/app)."""
    sections = split_sections(cfg)
    removed: List[str] = []
    dead_backends = set()
    keep = []
    port_re = re.compile(r"^\s*bind\s+\S*?:(\d+)", re.M)
    for pre, h, body in sections:
        kind = h.split()[0] if h else ""
        if kind in ("frontend", "listen"):
            bound = {int(p) for p in port_re.findall(body)}
            sample = kind == "frontend" and h.split()[1:2] == ["main"]
            if bound & set(ports) or sample:
                removed.append(h)
                for m in re.finditer(r"(?:default_backend|use_backend)\s+(\S+)", body):
                    dead_backends.add(m.group(1))
                continue
        keep.append((pre, h, body))
    still_referenced = set()
    for _, h, body in keep:
        for m in re.finditer(r"(?:default_backend|use_backend)\s+(\S+)", body):
            still_referenced.add(m.group(1))
    out = []
    for pre, h, body in keep:
        if h.startswith("backend "):
            bname = h.split()[1]
            if (bname in dead_backends or bname in ("static", "app")) and bname not in still_referenced:
                removed.append(h)
                continue
        out.append((pre, h, body))
    parts = []
    for pre, h, body in out:
        if pre.strip():
            parts.append(pre)
        if h:
            parts.append(h)
        if body.strip():
            parts.append(body.rstrip())
        parts.append("")
    text = "\n".join(parts).rstrip() + "\n"
    return text, removed


# ---------------------------------------------------------------- ssh
class SSH:
    def __init__(self, vm: HAProxyVM, log):
        self.vm = vm
        self.log = log
        self.c = paramiko.SSHClient()
        self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=vm.host, port=vm.ssh_port, username=vm.ssh_user, timeout=10,
                  allow_agent=False, look_for_keys=not vm.ssh_password)
        if vm.ssh_password:
            kw["password"] = vm.ssh_password
        self.c.connect(**kw)
        self.sudo = vm.ssh_user != "root"

    def close(self):
        self.c.close()

    def run(self, cmd: str, check: bool = True, quiet: bool = False) -> Tuple[int, str]:
        if self.sudo:
            pw = self.vm.ssh_password.replace("'", "'\\''")
            full = f"echo '{pw}' | sudo -S -p '' bash -c {_q(cmd)}"
        else:
            full = f"bash -c {_q(cmd)}"
        if not quiet:
            self.log(f"[{self.vm.host}] $ {cmd}")
        _, out, err = self.c.exec_command(full, timeout=600)
        o = out.read().decode(errors="replace") + err.read().decode(errors="replace")
        rc = out.channel.recv_exit_status()
        for l in o.rstrip().splitlines()[-40:]:
            if not quiet:
                self.log(f"[{self.vm.host}]   {l}")
        if check and rc != 0:
            raise RuntimeError(f"{cmd!r} on {self.vm.host} failed rc={rc}: {o.strip()[-300:]}")
        return rc, o

    def read(self, path: str) -> str:
        rc, o = self.run(f"cat {path} 2>/dev/null || true", check=False, quiet=True)
        return o

    def write(self, path: str, content: str, mode: str = "0644"):
        b64 = __import__("base64").b64encode(content.encode()).decode()
        self.run(f"echo {b64} | base64 -d > {path} && chmod {mode} {path}", quiet=True)
        self.log(f"[{self.vm.host}] wrote {path} ({len(content)} bytes)")


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- push
def push(spec: ClusterSpec, vm: HAProxyVM, log, role: str = None, dry_run: bool = False) -> Dict:
    role = role or vm.role
    if spec.lb.layout == "ha":
        role = "both"
    ports = owned_ports(spec, role)
    ours = render(spec, role)
    ours_path = f"/etc/haproxy/conf.d/ocp-{spec.name}.cfg"
    result = {"host": vm.host, "role": role, "changed": [], "rendered": ours}
    if dry_run:
        return result
    s = SSH(vm, log)
    try:
        rc, osrel = s.run("cat /etc/os-release", check=False, quiet=True)
        if "ID_LIKE=\"rhel" not in osrel and "ID=\"rhel\"" not in osrel and "rhel" not in osrel and "fedora" not in osrel:
            raise RuntimeError("only RHEL-family hosts are supported for HAProxy VMs")
        log(f"[{vm.host}] OS: {[l for l in osrel.splitlines() if l.startswith('PRETTY_NAME')][0]}")
        # install
        rc, _ = s.run("rpm -q haproxy", check=False, quiet=True)
        if rc != 0:
            s.run("dnf -y -q install haproxy")
            result["changed"].append("installed haproxy")
        if spec.lb.layout == "ha":
            rc, _ = s.run("rpm -q keepalived", check=False, quiet=True)
            if rc != 0:
                s.run("dnf -y -q install keepalived")
                result["changed"].append("installed keepalived")
        s.run("mkdir -p /etc/haproxy/conf.d", quiet=True)
        # make sure the unit loads conf.d (EL9 does via CFGDIR; add a drop-in otherwise)
        rc, unit = s.run("systemctl cat haproxy 2>/dev/null", check=False, quiet=True)
        if "conf.d" not in unit:
            s.run("mkdir -p /etc/systemd/system/haproxy.service.d", quiet=True)
            s.write("/etc/systemd/system/haproxy.service.d/confd.conf",
                    "[Service]\nExecStart=\nExecStart=/usr/sbin/haproxy -Ws -f /etc/haproxy/haproxy.cfg -f /etc/haproxy/conf.d -p /run/haproxy.pid\n")
            s.run("systemctl daemon-reload", quiet=True)
            result["changed"].append("added systemd drop-in to load conf.d")
        # adopt haproxy.cfg
        cfg = s.read("/etc/haproxy/haproxy.cfg")
        if cfg.strip():
            new_cfg, removed = strip_conflicts(cfg, ports)
            if removed:
                ts = time.strftime("%Y%m%d-%H%M%S")
                s.run(f"cp -a /etc/haproxy/haproxy.cfg /etc/haproxy/haproxy.cfg.{ts}.bak", quiet=True)
                s.write("/etc/haproxy/haproxy.cfg", new_cfg)
                result["changed"].append(f"removed conflicting sections from haproxy.cfg: {', '.join(removed)} (backup haproxy.cfg.{ts}.bak)")
                log(f"[{vm.host}] stripped: {', '.join(removed)}")
        else:
            s.write("/etc/haproxy/haproxy.cfg", _env.get_template("haproxy-base.cfg.j2").render())
            result["changed"].append("wrote base haproxy.cfg")
        # other ocp-*.cfg files from other clusters that use the same ports -> leave, but warn
        rc, others = s.run(f"ls /etc/haproxy/conf.d/ 2>/dev/null | grep -v '^ocp-{spec.name}.cfg$' || true", check=False, quiet=True)
        if others.strip():
            log(f"[{vm.host}] note: other conf.d files present: {others.split()}")
        old = s.read(ours_path)
        if old != ours:
            s.write(ours_path, ours)
            result["changed"].append(f"updated {ours_path}")
        # validate
        s.run("haproxy -c -f /etc/haproxy/haproxy.cfg -f /etc/haproxy/conf.d")
        # firewall / selinux
        rc, fw = s.run("systemctl is-active firewalld", check=False, quiet=True)
        if fw.strip() == "active":
            for p in ports:
                s.run(f"firewall-cmd -q --permanent --add-port={p}/tcp", check=False, quiet=True)
            if spec.lb.layout == "ha":
                s.run("firewall-cmd -q --permanent --add-protocol=vrrp", check=False, quiet=True)
            s.run("firewall-cmd -q --reload", check=False, quiet=True)
            result["changed"].append("opened firewall ports")
        rc, se = s.run("getenforce", check=False, quiet=True)
        if se.strip() == "Enforcing":
            s.run("setsebool -P haproxy_connect_any 1", check=False, quiet=True)
        if spec.lb.layout == "ha":
            s.run("sysctl -w net.ipv4.ip_nonlocal_bind=1 >/dev/null && echo 'net.ipv4.ip_nonlocal_bind=1' > /etc/sysctl.d/90-haproxy.conf", quiet=True)
            prio = 101 if spec.lb.vms and spec.lb.vms[0].host == vm.host else 100 - spec.lb.vms.index(vm)
            iface = spec.lb.ha.interface
            if not iface:
                rc, iface = s.run("ip -o -4 route show to default | awk '{print $5}' | head -1", check=False, quiet=True)
                iface = iface.strip()
            kc = _env.get_template("keepalived.conf.j2").render(spec=spec, vm=vm, priority=prio, iface=iface,
                                                              state="MASTER" if prio > 100 else "BACKUP")
            if s.read("/etc/keepalived/keepalived.conf") != kc:
                s.write("/etc/keepalived/keepalived.conf", kc)
                result["changed"].append("updated keepalived.conf")
            s.run("systemctl enable --now keepalived && systemctl reload keepalived || systemctl restart keepalived")
        # enable + reload
        s.run("systemctl enable haproxy >/dev/null 2>&1; systemctl is-active haproxy >/dev/null && systemctl reload haproxy || systemctl restart haproxy")
        rc, st = s.run("systemctl is-active haproxy", check=False, quiet=True)
        result["haproxy"] = st.strip()
        if st.strip() != "active":
            rc, j = s.run("journalctl -u haproxy -n 20 --no-pager", check=False)
            raise RuntimeError("haproxy is not active after reload")
        log(f"[{vm.host}] haproxy active; stats on http://{vm.ip or vm.host}:{spec.lb.stats_port}/")
    finally:
        s.close()
    return result


def inspect(vm: HAProxyVM, log=lambda s: None) -> Dict:
    """Read-only view of a VM's haproxy state for the UI."""
    s = SSH(vm, log)
    try:
        rc, osrel = s.run("grep PRETTY_NAME /etc/os-release | cut -d= -f2 | tr -d '\"'", check=False, quiet=True)
        rc, pkg = s.run("rpm -q haproxy 2>/dev/null || echo not-installed", check=False, quiet=True)
        rc, act = s.run("systemctl is-active haproxy 2>/dev/null || true", check=False, quiet=True)
        rc, kpkg = s.run("rpm -q keepalived 2>/dev/null || echo not-installed", check=False, quiet=True)
        rc, listen = s.run("ss -tlnH | awk '{print $4}' | sed 's/.*://' | sort -un | tr '\\n' ' '", check=False, quiet=True)
        rc, confd = s.run("ls /etc/haproxy/conf.d 2>/dev/null | tr '\\n' ' '", check=False, quiet=True)
        cfg = s.read("/etc/haproxy/haproxy.cfg")
        return {"host": vm.host, "os": osrel.strip(), "haproxy": pkg.strip(), "active": act.strip(),
                "keepalived": kpkg.strip(), "listening": listen.split(), "confd": confd.split(),
                "haproxy_cfg": cfg}
    finally:
        s.close()
