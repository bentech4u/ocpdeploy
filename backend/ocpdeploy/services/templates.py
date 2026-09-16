"""Cluster templates: export a spec as YAML, keep a library on the installer host, and
create new clusters from a template or from an existing cluster (clone)."""
import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..models import ClusterSpec
from ..secrets import MASK
from ..settings import ROOT
from ..store import ClusterStore, _walk_secret

TEMPLATES_DIR = ROOT / "templates"
# runtime state that never belongs in a template
RUNTIME = {"status": "new"}


def sanitize(raw: Dict[str, Any], keep_hardware: bool = False) -> Dict[str, Any]:
    """Strip per-cluster runtime state and (unless asked) hardware identities."""
    d = json.loads(json.dumps(raw))
    d["status"] = "new"
    d.get("lb", {}).update({"bootstrap_removed": False, "ingress_on": "all"})
    d.get("mirror", {}).pop("mirrored_version", None)
    d.get("day2", {}).get("identity", {}).pop("kubeadmin_disabled", None)
    d.get("day2", {}).get("backup", {}).update({"schedule": ""})
    for n in d.get("nodes", []):
        if not keep_hardware:
            n["mac"] = None
            for k in ("bmc_address", "bmc_username", "bmc_password", "bmc_system_id"):
                n.pop(k, None)
    return d


def export_yaml(store: ClusterStore, include_secrets: bool = False, keep_hardware: bool = False) -> str:
    if include_secrets:
        raw = store.load().model_dump()
    else:
        raw = store.public()
        _walk_secret(raw, lambda v, p: "" if v == MASK else v)   # masked -> empty, so the file has no placeholders
    d = sanitize(raw, keep_hardware)
    header = (f"# ocpdeploy cluster template exported from {store.name}\n"
              + ("# WARNING: contains decrypted secrets (pull secret, passwords). Keep it private.\n" if include_secrets else "# Secrets are omitted; fill them in after import.\n"))
    return header + yaml.safe_dump(d, sort_keys=False, width=1000)


def renumber(d: Dict[str, Any], first_ip: str):
    """Assign sequential IPs to the nodes in table order starting at first_ip."""
    ip = ipaddress.ip_address(first_ip)
    for n in d.get("nodes", []):
        n["ip"] = str(ip)
        ip += 1


def build(raw: Dict[str, Any], name: str, base_domain: Optional[str] = None, first_ip: Optional[str] = None,
          machine_cidr: Optional[str] = None, gateway: Optional[str] = None) -> Dict[str, Any]:
    d = sanitize(raw)
    d["name"] = name
    if base_domain:
        d["base_domain"] = base_domain
    if machine_cidr:
        d.setdefault("network", {})["machine_cidr"] = machine_cidr
    if gateway:
        d.setdefault("network", {})["gateway"] = gateway
    if first_ip:
        renumber(d, first_ip)
    # names inside the template that embed the old cluster name are left alone on purpose
    # (nodes are generic); the VM folder is per-cluster on IPI, so drop a cluster-specific one
    vc = d.get("vcenter", {})
    if vc.get("folder", "").rstrip("/").endswith("/" + raw.get("name", "")):
        vc["folder"] = ""
    return d


def create_from(raw: Dict[str, Any], name: str, **overrides) -> ClusterStore:
    d = build(raw, name, **overrides)
    probe = json.loads(json.dumps(d))
    _walk_secret(probe, lambda v, p: "" if v == MASK else v)
    spec = ClusterSpec.model_validate(probe)          # validates before anything is written
    store = ClusterStore(name)
    store.create(spec)
    return store


# ---------------------------------------------------------------- library
def _path(tname: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}", tname):
        raise ValueError("template name may contain letters, digits, dot, dash and underscore")
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    return TEMPLATES_DIR / f"{tname}.yaml"


def list_templates() -> List[Dict[str, Any]]:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(TEMPLATES_DIR.glob("*.yaml")):
        try:
            d = yaml.safe_load(f.read_text()) or {}
        except Exception:
            d = {}
        nodes = d.get("nodes", [])
        out.append({"name": f.stem, "source": d.get("name", ""), "install_method": d.get("install_method", ""), "provider": d.get("provider", ""),
                    "topology": d.get("topology", ""), "ocp_version": d.get("ocp_version", ""), "nodes": len(nodes),
                    "has_secrets": bool(d.get("pull_secret")), "modified": f.stat().st_mtime})
    return out


def save_template(tname: str, text: str) -> Dict[str, Any]:
    d = yaml.safe_load(text)
    if not isinstance(d, dict) or "nodes" not in d:
        raise ValueError("not a cluster template (expected a mapping with nodes)")
    _path(tname).write_text(text)
    return {"name": tname}


def read_template(tname: str) -> str:
    p = _path(tname)
    if not p.exists():
        raise FileNotFoundError(tname)
    return p.read_text()


def delete_template(tname: str):
    p = _path(tname)
    if p.exists():
        p.unlink()
