"""Download and verify openshift-install / oc (and oc-mirror on demand) for a given
version into bin/<version>/."""
import hashlib
import tarfile
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from ..settings import BIN_DIR, MIRROR

FILES = {
    "openshift-install": "openshift-install-linux.tar.gz",
    "oc": "openshift-client-linux.tar.gz",
}
# oc-mirror ships as a RHEL 9 build and an older RHEL 8 build; prefer the first that exists
OC_MIRROR_FILES = ["oc-mirror.rhel9.tar.gz", "oc-mirror.tar.gz"]


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
        "oc-mirror": (d / "oc-mirror").exists(),
    }


def _vkey(v: str):
    try:
        return tuple(int(x) for x in v.split("-")[0].split("."))
    except ValueError:
        return (0,)


def any_oc() -> Optional[Path]:
    """The newest oc binary available in bin/, or None."""
    found = [p for p in BIN_DIR.iterdir() if (p / "oc").exists()] if BIN_DIR.exists() else []
    found.sort(key=lambda p: _vkey(p.name))
    return (found[-1] / "oc") if found else None


def ensure_oc(version: str, log=print) -> Path:
    """Only the client tarball for a version (connected clusters need oc, not the installer)."""
    d = tool_dir(version)
    d.mkdir(parents=True, exist_ok=True)
    if (d / "oc").exists():
        return d / "oc"
    sums = _checksums(version, log)
    _fetch_verified(version, FILES["oc"], d, sums, log, ["oc", "kubectl"])
    return d / "oc"


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


def _checksums(version: str, log) -> Dict[str, str]:
    base = f"{MIRROR}/{version}"
    log(f"Fetching checksums from {base}/sha256sum.txt")
    sums = {}
    r = httpx.get(f"{base}/sha256sum.txt", timeout=30, follow_redirects=True)
    r.raise_for_status()
    for line in r.text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1]] = parts[0]
    return sums


def _fetch_verified(version: str, fname: str, d: Path, sums: Dict[str, str], log, executables: List[str]):
    base = f"{MIRROR}/{version}"
    tgz = d / fname
    log(f"Downloading {base}/{fname}")
    _download(f"{base}/{fname}", tgz, log)
    digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
    if fname not in sums:
        tgz.unlink()
        raise RuntimeError(f"{fname} missing from sha256sum.txt")
    if digest != sums[fname]:
        tgz.unlink()
        raise RuntimeError(f"checksum mismatch for {fname}")
    log(f"  sha256 OK {digest[:16]}…")
    with tarfile.open(tgz) as t:
        t.extractall(d, filter="data")
    tgz.unlink()
    (d / "README.md").unlink(missing_ok=True)
    for exe in executables:
        p = d / exe
        if p.exists():
            p.chmod(0o755)


def ensure(version: str, log=print) -> Dict:
    d = tool_dir(version)
    d.mkdir(parents=True, exist_ok=True)
    sums = _checksums(version, log)
    for tool, fname in FILES.items():
        if (d / tool).exists():
            log(f"{tool} {version} already present")
            continue
        _fetch_verified(version, fname, d, sums, log, ["openshift-install", "oc", "kubectl"])
    log(f"Tools ready in {d}")
    return status(version)


def ensure_oc_mirror(version: str, log=print) -> Path:
    """Fetch oc-mirror for the version (used for disconnected installs)."""
    d = tool_dir(version)
    d.mkdir(parents=True, exist_ok=True)
    if (d / "oc-mirror").exists():
        log(f"oc-mirror {version} already present")
        return d / "oc-mirror"
    sums = _checksums(version, log)
    for fname in OC_MIRROR_FILES:
        if fname in sums:
            _fetch_verified(version, fname, d, sums, log, ["oc-mirror"])
            break
    else:
        raise RuntimeError(f"no oc-mirror archive published for {version}")
    if not (d / "oc-mirror").exists():
        raise RuntimeError("archive did not contain an oc-mirror binary")
    return d / "oc-mirror"


HELM_VERSION = "v3.18.6"


def ensure_helm(log=print) -> Path:
    """Fetch the helm binary (checksum-verified) into bin/helm/."""
    d = BIN_DIR / "helm"
    exe = d / "helm"
    if exe.exists():
        return exe
    d.mkdir(parents=True, exist_ok=True)
    fname = f"helm-{HELM_VERSION}-linux-amd64.tar.gz"
    url = f"https://get.helm.sh/{fname}"
    log(f"Downloading {url}")
    tgz = d / fname
    _download(url, tgz, log)
    want = httpx.get(url + ".sha256sum", timeout=30, follow_redirects=True).text.split()[0]
    got = hashlib.sha256(tgz.read_bytes()).hexdigest()
    if got != want:
        tgz.unlink()
        raise RuntimeError("helm checksum mismatch")
    with tarfile.open(tgz) as t:
        t.extractall(d, filter="data")
    tgz.unlink()
    (d / "linux-amd64" / "helm").rename(exe)
    exe.chmod(0o755)
    log(f"helm {HELM_VERSION} ready")
    return exe
