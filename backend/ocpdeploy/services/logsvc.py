"""Pod logs, warning events and must-gather."""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from . import kube, clusterops as ops
from .extranodes import build_dir, _joiner_namespaces, _cleanup_joiner

_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


def _check(v: str, what: str):
    if not _NAME.match(v or ""):
        raise ValueError(f"bad {what}")


def namespaces(store, spec) -> List[str]:
    out = kube.oc(store, spec, ["get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}"])
    return sorted(out.split())


def pods(store, spec, ns: str) -> List[Dict]:
    _check(ns, "namespace")
    items = ops.get(store, spec, "pods", ns=ns).get("items", [])
    out = []
    for p in items:
        st = p.get("status") or {}
        cs = {c["name"]: c for c in st.get("containerStatuses", []) + st.get("initContainerStatuses", [])}
        conts = [c["name"] for c in (p["spec"].get("initContainers") or [])] + [c["name"] for c in p["spec"].get("containers", [])]
        restarts = sum(c.get("restartCount", 0) for c in cs.values())
        waiting = next((c["state"]["waiting"].get("reason", "") for c in cs.values() if (c.get("state") or {}).get("waiting")), "")
        out.append({"name": p["metadata"]["name"], "phase": st.get("phase", ""), "node": p["spec"].get("nodeName", ""), "containers": conts,
                    "restarts": restarts, "reason": waiting, "age": ops.age(p["metadata"].get("creationTimestamp"))})
    out.sort(key=lambda x: x["name"])
    return out


def pod_log(store, spec, ns: str, pod: str, container: str = "", tail: int = 500, previous: bool = False) -> str:
    _check(ns, "namespace")
    _check(pod, "pod")
    tail = max(10, min(int(tail or 500), 10000))
    args = ["logs", "-n", ns, pod, f"--tail={tail}", "--timestamps"]
    if container:
        _check(container, "container")
        args += ["-c", container]
    if previous:
        args.append("--previous")
    out = kube.oc(store, spec, args, check=False, timeout=60)
    return out[-3_000_000:]


def events(store, spec, ns: str = "", only_warnings: bool = True, query: str = "", limit: int = 300) -> List[Dict]:
    extra = ["-A"] if not ns else []
    if ns:
        _check(ns, "namespace")
    if only_warnings:
        extra += ["--field-selector", "type=Warning"]
    items = ops.get(store, spec, "events", ns=ns or None, extra=extra).get("items", [])
    q = (query or "").lower()
    out = []
    for e in items:
        o = e.get("involvedObject", {})
        row = {"namespace": o.get("namespace", "") or e["metadata"].get("namespace", ""), "object": f"{o.get('kind', '')}/{o.get('name', '')}",
               "type": e.get("type", ""), "reason": e.get("reason", ""), "message": (e.get("message") or "")[:400], "count": e.get("count") or 1,
               "last": e.get("lastTimestamp") or e.get("eventTime") or e["metadata"].get("creationTimestamp", "")}
        if q and q not in (row["namespace"] + " " + row["object"] + " " + row["reason"] + " " + row["message"]).lower():
            continue
        out.append(row)
    out.sort(key=lambda r: r["last"] or "", reverse=True)
    return out[:max(1, min(limit, 2000))]


def job_mustgather(ctx: JobContext, store: ClusterStore, spec: ClusterSpec, since: str = "", images: Optional[List[str]] = None):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bid = f"{spec.name}-{stamp}"
    d = build_dir(bid)
    d.mkdir(mode=0o700)
    meta = {"kind": "mustgather", "cluster": spec.name, "server": spec.lb.external_api.fqdn if spec.imported else f"api.{spec.domain}",
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "hosts": [], "status": "running",
            "note": (f"since {since}" if since else "full") + (f", images: {', '.join(images)}" if images else "")}
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    raw = d / "must-gather"
    args = [kube.oc_bin(spec), "adm", "must-gather", f"--dest-dir={raw}"]
    if since:
        if not re.fullmatch(r"\d+[smh]", since):
            raise ValueError("since must look like 30m or 2h")
        args.append(f"--since={since}")
    for img in images or []:
        if not re.fullmatch(r"[A-Za-z0-9./:@_-]+", img):
            raise ValueError(f"bad image {img}")
        args.append(f"--image={img}")
    before = _joiner_namespaces(store, spec, "openshift-must-gather-")
    ctx.log("Collecting must-gather data. This takes several minutes and runs pods on the cluster.")
    try:
        ctx.run(args, env=kube.min_env(store))
        archive = d / "must-gather.tar.gz"
        ctx.log("Packing the result")
        r = subprocess.run(["tar", "czf", str(archive), "-C", str(d), "must-gather"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"tar failed: {r.stderr[-200:]}")
        os.chmod(archive, 0o600)
        meta["status"] = "ready"
        ctx.log(f"must-gather ready: {archive.stat().st_size // 2**20} MiB. Download it from the Logs page; it is kept until you delete it.")
    except Exception:
        meta["status"] = "failed"
        raise
    finally:
        _cleanup_joiner(store, spec, before, ctx.log, "openshift-must-gather-")
        shutil.rmtree(raw, ignore_errors=True)
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        if meta["status"] == "failed":
            for f in d.glob("*.tar.gz"):
                f.unlink()
