"""Disconnected installs: mirror registry checks and the oc-mirror (v2) job."""
import json
import os
import ssl
import socket
from pathlib import Path
from typing import Dict, List

import httpx
import yaml

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import render, tools


def host_port(registry: str):
    host, _, port = registry.partition(":")
    return host, int(port) if port else 443


def fetch_cert(registry: str) -> Dict:
    """Server certificate of the mirror registry (same shape as the vCenter certificate capture)."""
    from . import vcenter
    host, port = host_port(registry)
    return vcenter.fetch_cert(host, port)


def _ca_file(store: ClusterStore, spec: ClusterSpec) -> str:
    p = store.dir / "mirror" / "registry-ca.pem"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(spec.mirror.ca_pem.strip() + "\n")
    return str(p)


def check(store: ClusterStore, spec: ClusterSpec) -> List[Dict]:
    m = spec.mirror
    rows: List[Dict] = []
    if not m.enabled:
        return rows
    if not m.registry:
        rows.append({"name": "mirror registry", "status": "fail", "expected": "host[:port]", "actual": "", "hint": "Set the registry in the Proxy & mirror step"})
        return rows
    verify = _ca_file(store, spec) if (m.tls_verify and m.ca_pem) else (True if m.tls_verify else False)
    try:
        r = httpx.get(f"https://{m.registry}/v2/", verify=verify, timeout=10)
        ok = r.status_code in (200, 401)
        rows.append({"name": f"registry {m.registry}", "status": "pass" if ok else "warn", "expected": "HTTP 200/401 on /v2/",
                     "actual": f"HTTP {r.status_code}", "hint": "" if ok else "Unexpected answer; is this a container registry?"})
    except Exception as ex:
        msg = str(ex)[-160:]
        tls = "certificate" in msg.lower() or "ssl" in msg.lower()
        rows.append({"name": f"registry {m.registry}", "status": "fail", "expected": "reachable over TLS", "actual": msg,
                     "hint": "Fetch and accept the registry certificate, or disable TLS verification" if tls else "Registry unreachable from the installer host"})
    try:
        auths = json.loads(spec.pull_secret or "{}").get("auths", {})
        host = m.registry.split(":")[0]
        has = any(k == m.registry or k == host or k.startswith(m.registry + "/") for k in auths)
        rows.append({"name": "pull secret has mirror credentials", "status": "pass" if has else "fail", "expected": m.registry,
                     "actual": ", ".join(auths) or "empty", "hint": "" if has else "Merge the mirror registry login into the pull secret (Secrets step)"})
    except Exception:
        rows.append({"name": "pull secret", "status": "fail", "expected": "valid JSON", "actual": "invalid", "hint": ""})
    srcs = render.image_digest_sources(spec)
    rows.append({"name": "image digest sources", "status": "pass" if srcs else "fail", "expected": ">= 1 mapping",
                 "actual": f"{len(srcs)} mapping(s)" + ("" if m.sources else " (oc-mirror defaults)"),
                 "hint": "" if srcs else "Run oc-mirror from the app or add the mappings by hand"})
    if spec.ocp_version:
        done = m.mirrored_version == spec.ocp_version
        rows.append({"name": f"release {spec.ocp_version} mirrored", "status": "pass" if done else "warn", "expected": "mirrored",
                     "actual": f"last mirrored: {m.mirrored_version or 'never'}",
                     "hint": "" if done else "Run 'Mirror release' or make sure the release is already in the registry"})
    if spec.install_method == "ipi":
        img = spec.vcenter.cluster_os_image
        if img:
            try:
                r = httpx.head(img, timeout=10, follow_redirects=True, verify=False)
                ok = r.status_code < 400
                rows.append({"name": "RHCOS OVA URL", "status": "pass" if ok else "fail", "expected": "reachable", "actual": f"HTTP {r.status_code}", "hint": ""})
            except Exception as ex:
                rows.append({"name": "RHCOS OVA URL", "status": "fail", "expected": "reachable", "actual": str(ex)[-100:], "hint": "Host the OVA on an internal web server"})
        else:
            rows.append({"name": "RHCOS OVA URL", "status": "warn", "expected": "internal URL",
                         "actual": "not set", "hint": "Without it the installer downloads the OVA from the internet (fine behind a proxy, fails fully offline)"})
    return rows


def write_imageset(store: ClusterStore, spec: ClusterSpec) -> Path:
    d = store.dir / "mirror"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "imageset-config.yaml"
    p.write_text(render.to_yaml(render.imageset_config(spec)))
    return p


def _parse_cluster_resources(workspace: Path) -> List[Dict]:
    """Collect source->mirrors mappings from the IDMS/ITMS files oc-mirror v2 writes."""
    out: Dict[str, List[str]] = {}
    res = workspace / "working-dir" / "cluster-resources"
    for f in sorted(res.glob("*.yaml")) if res.exists() else []:
        try:
            for doc in yaml.safe_load_all(f.read_text()):
                if not isinstance(doc, dict):
                    continue
                entries = (doc.get("spec") or {}).get("imageDigestMirrors") or (doc.get("spec") or {}).get("imageTagMirrors") or []
                for e in entries:
                    src = e.get("source")
                    if src:
                        out.setdefault(src, [])
                        for mr in e.get("mirrors", []):
                            if mr not in out[src]:
                                out[src].append(mr)
        except Exception:
            continue
    return [{"source": s, "mirrors": ms} for s, ms in out.items()]


def job_mirror(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    from .deploy import trust_env
    m = spec.mirror
    if not m.enabled or not m.registry:
        raise RuntimeError("enable the mirror and set the registry in the Proxy & mirror step")
    if not spec.ocp_version:
        raise RuntimeError("select an OpenShift version first")
    bin_path = tools.ensure_oc_mirror(spec.ocp_version, ctx.log)
    imageset = write_imageset(store, spec)
    ctx.log(f"ImageSetConfiguration written to mirror/{imageset.name}")
    d = store.dir / "mirror"
    auth = d / "auth.json"
    fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(spec.pull_secret.strip() or "{}")
    workspace = d / "workspace"
    workspace.mkdir(exist_ok=True)
    cmd = [str(bin_path), "--v2", "-c", str(imageset), "--workspace", f"file://{workspace}", f"docker://{m.registry}",
           "--authfile", str(auth), "--cache-dir", str(d / "cache")]
    if not m.tls_verify:
        cmd.append("--dest-tls-verify=false")
    env = trust_env(store, spec, ctx.log)
    ctx.log(f"Mirroring release {spec.ocp_version}" + (f" + {len(m.operators)} operator(s)" if m.operators else "") + f" to {m.registry}. This can take a long time.")
    ctx.run(cmd, cwd=str(d), env=env)
    srcs = _parse_cluster_resources(workspace)
    if srcs:
        def _p(raw):
            raw.setdefault("mirror", {})["sources"] = srcs
            raw["mirror"]["mirrored_version"] = spec.ocp_version
        store.patch(_p)
        ctx.log(f"Recorded {len(srcs)} image source mapping(s) from oc-mirror output into the cluster spec")
    else:
        store.patch(lambda raw: raw.setdefault("mirror", {}).__setitem__("mirrored_version", spec.ocp_version))
        ctx.log("oc-mirror produced no IDMS/ITMS files; keeping the existing image source mappings")
