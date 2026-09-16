"""etcd backups: run cluster-backup.sh on a master through `oc debug node`, copy the
tarball to the installer host, prune old copies, and schedule with a systemd timer."""
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..jobs import JobContext
from ..models import ClusterSpec
from ..store import ClusterStore
from ..settings import ROOT
from . import kube, clusterops as ops

REMOTE_DIR = "/home/core/assets/ocpdeploy-backup"


def backups_dir(store: ClusterStore) -> Path:
    d = store.dir / "backups"
    d.mkdir(exist_ok=True)
    return d


def list_backups(store: ClusterStore) -> List[Dict]:
    out = []
    for f in sorted(backups_dir(store).glob("etcd-*.tar.gz"), reverse=True):
        st = f.stat()
        out.append({"file": f.name, "size_mb": round(st.st_size / 2**20, 1), "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(st.st_mtime)) + "Z"})
    return out


def _master_node(store, spec) -> str:
    for n in ops.nodes_summary(store, spec):
        if "master" in n["roles"] and n["ready"] == "True":
            return n["name"]
    raise RuntimeError("no Ready master node found")


def _debug(store, spec, node: str, script: str, timeout: int = 900, stdout=None) -> subprocess.CompletedProcess:
    """Run a shell snippet on a node through `oc debug node/<node>` (no SSH key needed)."""
    cmd = [kube.oc_bin(spec), "debug", f"node/{node}", "--quiet", "--", "chroot", "/host", "bash", "-c", script]
    return subprocess.run(cmd, env=kube.env(store), stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, text=stdout is None)


def run_backup(store: ClusterStore, spec: ClusterSpec, log=print, keep: Optional[int] = None) -> Path:
    node = _master_node(store, spec)
    log(f"Running cluster-backup.sh on {node} (via oc debug)")
    r = _debug(store, spec, node, f"rm -rf {REMOTE_DIR} && mkdir -p {REMOTE_DIR} && /usr/local/bin/cluster-backup.sh {REMOTE_DIR} 2>&1")
    for line in (r.stdout or "").splitlines()[-12:]:
        log("  " + line)
    if r.returncode != 0:
        raise RuntimeError(f"cluster-backup.sh failed with exit {r.returncode}: {(r.stderr or '')[-200:]}")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest = backups_dir(store) / f"etcd-{stamp}.tar.gz"
    log(f"Copying the snapshot to {dest.relative_to(ROOT)}")
    with open(dest, "wb") as f:
        p = _debug(store, spec, node, f"tar czf - -C {REMOTE_DIR} . && rm -rf {REMOTE_DIR}", timeout=1800, stdout=f)
    if p.returncode != 0 or dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"copy failed: {p.stderr.decode(errors='replace')[-200:] if isinstance(p.stderr, bytes) else p.stderr}")
    log(f"Backup complete: {dest.name} ({dest.stat().st_size // 2**20} MiB)")
    keep = keep if keep is not None else spec.day2.backup.keep
    if keep and keep > 0:
        olds = sorted(backups_dir(store).glob("etcd-*.tar.gz"))
        for f in olds[:-keep]:
            f.unlink()
            log(f"pruned {f.name}")
    return dest


def job_backup(ctx: JobContext, store: ClusterStore, spec: ClusterSpec):
    run_backup(store, spec, ctx.log)


# ---------------------------------------------------------------- systemd timer
def _unit(store: ClusterStore) -> str:
    return f"ocpdeploy-backup-{store.name}"


def timer_status(store: ClusterStore) -> Dict:
    unit = _unit(store)
    r = subprocess.run(["systemctl", "show", f"{unit}.timer", "-p", "ActiveState,NextElapseUSecRealtime,LastTriggerUSec,TimersCalendar"], capture_output=True, text=True)
    props = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    return {"unit": unit, "active": props.get("ActiveState") == "active", "next": props.get("NextElapseUSecRealtime", ""),
            "last": props.get("LastTriggerUSec", ""), "calendar": props.get("TimersCalendar", "")}


def set_schedule(store: ClusterStore, schedule: str, keep: int) -> Dict:
    unit = _unit(store)
    sd = Path("/etc/systemd/system")
    if not schedule:
        subprocess.run(["systemctl", "disable", "--now", f"{unit}.timer"], capture_output=True)
        for f in (sd / f"{unit}.timer", sd / f"{unit}.service"):
            f.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        store.patch(lambda raw: raw["day2"]["backup"].update({"schedule": "", "keep": keep}))
        return timer_status(store)
    chk = subprocess.run(["systemd-analyze", "calendar", schedule], capture_output=True, text=True)
    if chk.returncode != 0:
        raise RuntimeError(f"invalid schedule: {chk.stderr.strip() or chk.stdout.strip()}")
    venv = ROOT / ".venv" / "bin" / "python"
    (sd / f"{unit}.service").write_text(f"""[Unit]
Description=ocpdeploy etcd backup for cluster {store.name}
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={ROOT}
Environment=PYTHONPATH={ROOT}/backend
Environment=OCPDEPLOY_ROOT={ROOT}
ExecStart={venv} -m ocpdeploy.cli backup {store.name}
""")
    (sd / f"{unit}.timer").write_text(f"""[Unit]
Description=ocpdeploy etcd backup timer for cluster {store.name}

[Timer]
OnCalendar={schedule}
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
""")
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    r = subprocess.run(["systemctl", "enable", "--now", f"{unit}.timer"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"could not enable timer: {r.stderr.strip()[-200:]}")
    store.patch(lambda raw: raw["day2"]["backup"].update({"schedule": schedule, "keep": keep}))
    return timer_status(store)
