"""Download and verify openshift-install / oc for a given version into bin/<version>/."""
import hashlib
import tarfile
from pathlib import Path
from typing import Dict

import httpx

from ..settings import BIN_DIR, MIRROR

FILES = {
    "openshift-install": "openshift-install-linux.tar.gz",
    "oc": "openshift-client-linux.tar.gz",
}


def tool_dir(version: str) -> Path:
    return BIN_DIR / version


def tool_path(version: str, tool: str) -> Path:
    return tool_dir(version) / tool


def status(version: str) -> Dict:
    d = tool_dir(version)
    return {
        "version": version,
        "dir": str(d),
        "openshift-install": (d / "openshift-install").exists(),
        "oc": (d / "oc").exists(),
        "kubectl": (d / "kubectl").exists(),
    }


def installed_versions():
    return sorted([p.name for p in BIN_DIR.iterdir() if (p / "openshift-install").exists()])


def _download(url: str, dest: Path, log):
    with httpx.stream("GET", url, timeout=60, follow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        last = -1
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    if pct // 10 != last // 10:
                        log(f"  {dest.name}: {pct}%")
                        last = pct


def ensure(version: str, log=print) -> Dict:
    d = tool_dir(version)
    d.mkdir(parents=True, exist_ok=True)
    base = f"{MIRROR}/{version}"
    log(f"Fetching checksums from {base}/sha256sum.txt")
    sums = {}
    r = httpx.get(f"{base}/sha256sum.txt", timeout=30, follow_redirects=True)
    r.raise_for_status()
    for line in r.text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1]] = parts[0]
    for tool, fname in FILES.items():
        if (d / tool).exists():
            log(f"{tool} {version} already present")
            continue
        tgz = d / fname
        log(f"Downloading {base}/{fname}")
        _download(f"{base}/{fname}", tgz, log)
        digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
        if fname not in sums:
            raise RuntimeError(f"{fname} missing from sha256sum.txt")
        if digest != sums[fname]:
            tgz.unlink()
            raise RuntimeError(f"checksum mismatch for {fname}")
        log(f"  sha256 OK {digest[:16]}…")
        with tarfile.open(tgz) as t:
            t.extractall(d, filter="data")
        tgz.unlink()
        (d / "README.md").unlink(missing_ok=True)
        for exe in ("openshift-install", "oc", "kubectl"):
            p = d / exe
            if p.exists():
                p.chmod(0o755)
    log(f"Tools ready in {d}")
    return status(version)
