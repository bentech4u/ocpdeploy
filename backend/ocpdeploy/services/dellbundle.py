"""Offline library of Dell CSM files (helm charts, helm, repctl) kept on the installer host.

Layout: bundles/dell/<component>/<version>/<file> plus meta.json. Every file is checked on
the way in: charts must contain the matching Chart.yaml, and when Dell (or helm.sh) publish a
digest for that version the sha256 has to match it. Nothing here needs internet; fetch() is only
a convenience for installers that do have it.
"""
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import yaml

from ..settings import ROOT, BIN_DIR

BUNDLE_DIR = ROOT / "bundles" / "dell"
INCOMING = BUNDLE_DIR / ".incoming"
MAX_UPLOAD = 512 * 1024 * 1024
DELL_INDEX = "https://dell.github.io/helm-charts/index.yaml"

COMPONENTS: Dict[str, Dict] = {
    "csi-isilon": {"title": "PowerScale CSI driver (helm chart)", "kind": "chart",
                   "url": "https://github.com/dell/helm-charts/releases/download/csi-isilon-{v}/csi-isilon-{v}.tgz",
                   "desc": "The driver itself. Needed for the Helm install method."},
    "csm-replication": {"title": "CSM Replication controller (helm chart)", "kind": "chart",
                        "url": "https://github.com/dell/helm-charts/releases/download/csm-replication-{v}/csm-replication-{v}.tgz",
                        "desc": "Replication controller and its CRDs. Needed for replication with the Helm method."},
    "helm": {"title": "helm (linux amd64)", "kind": "helm",
             "url": "https://get.helm.sh/helm-{v}-linux-amd64.tar.gz",
             "desc": "Runs the chart installs. Upload the .tar.gz from get.helm.sh or the bare binary."},
    "repctl": {"title": "repctl (linux amd64)", "kind": "binary",
               "url": "https://github.com/dell/csm/releases/download/v{v}/repctl",
               "desc": "Dell's replication CLI, offered for download to run DR commands by hand. The app itself does not need it."},
}

# Published digests, so an upload can be verified without internet (from Dell's helm-charts
# index.yaml and get.helm.sh .sha256sum files). The live index adds newer versions when reachable.
KNOWN: Dict[str, Dict[str, str]] = {
    "csi-isilon": {"2.17.2": "d1a406a5634aff2d753ee20d779afd9ce67fc524832abc3ebdde52af3c057333",
                   "2.17.1": "396881e1ee59ec71fde4be78bd69b5e3b99e87d6f20a3684915c40fd6a9730ac",
                   "2.17.0": "3a0fc22fb914b30d526c512222e466613f3de7a5226a32f8e3937499b357892c"},
    "csm-replication": {"1.15.0": "585d378cd51c0f47f5bcd2d46b8a7dd4c893940112f9e0c901585e6f43b0d218",
                        "1.14.0": "7eff270ae6ed21d38512ce5d76a95711c7791dfc3b816d6dd6f4537984860521",
                        "1.13.0": "b6a3fe67988812d52e134e39c385ea1ad4ae6d13370401a91098b3a7f6afe9ab"},
    "helm": {"v3.19.0": "a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496"},
    # GitHub asset digests of dell/csm releases (the csm-replication releases stopped shipping repctl after v1.13.0)
    "repctl": {"1.17.2": "93cb3f3662780680feba31a0b545dc197380ae430fbeaf5a3827c6d40c9ea2dd",
               "1.17.1": "93cb3f3662780680feba31a0b545dc197380ae430fbeaf5a3827c6d40c9ea2dd",
               "1.16.3": "f4e1cdbf54fa70940deb0fa87771b8aea18348899939269f9c16c419eea26655"},
}

# driver chart -> matching replication chart and the OpenShift / OneFS range Dell tested
DRIVER_MATRIX: Dict[str, Dict] = {
    "2.17": {"replication": "1.15.0", "csm": "1.17", "ocp": ("4.18", "4.21"), "onefs": ("9.5", "9.10")},
    "2.16": {"replication": "1.14.0", "csm": "1.16", "ocp": ("4.17", "4.20"), "onefs": ("9.5", "9.10")},
}

_VER = re.compile(r"^v?[0-9]+(\.[0-9]+){1,3}([-+][0-9A-Za-z.-]+)?$")


def _ensure_dirs():
    for d in (BUNDLE_DIR, INCOMING):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def matrix(chart_version: str) -> Dict:
    mm = ".".join(chart_version.lstrip("v").split(".")[:2])
    return DRIVER_MATRIX.get(mm, {})


# ------------------------------------------------------------------ inventory
def inventory() -> Dict[str, List[Dict]]:
    """Files present, per component, newest version first."""
    out: Dict[str, List[Dict]] = {c: [] for c in COMPONENTS}
    if not BUNDLE_DIR.exists():
        return out
    for comp in COMPONENTS:
        d = BUNDLE_DIR / comp
        if not d.is_dir():
            continue
        for vd in d.iterdir():
            meta = vd / "meta.json"
            if not meta.exists():
                continue
            try:
                m = json.loads(meta.read_text())
            except Exception:
                continue
            f = vd / m.get("file", "")
            if f.is_file():
                m["size"] = f.stat().st_size
                out[comp].append(m)
        out[comp].sort(key=lambda m: _vkey(m["version"]), reverse=True)
    return out


def _vkey(v: str):
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:4])


def path_of(comp: str, version: str) -> Path:
    for m in inventory().get(comp, []):
        if m["version"] == version:
            return BUNDLE_DIR / comp / m["version"] / m["file"]
    raise RuntimeError(f"{comp} {version} is not in the offline bundle (Dell PowerScale page, Bundle tab)")


def latest(comp: str) -> Optional[Dict]:
    inv = inventory().get(comp, [])
    return inv[0] if inv else None


def helm_path() -> Path:
    """helm from the bundle (newest), else the one the app already downloaded."""
    m = latest("helm")
    if m:
        return BUNDLE_DIR / "helm" / m["version"] / m["file"]
    legacy = BIN_DIR / "helm" / "helm"
    if legacy.exists():
        return legacy
    raise RuntimeError("helm is not available: upload it on the Bundle tab (helm-vX.Y.Z-linux-amd64.tar.gz from get.helm.sh)")


def catalog(online: bool = True) -> Dict:
    """Versions known to exist, with download URL and digest (for downloading on another machine)."""
    known = {c: dict(v) for c, v in KNOWN.items()}
    live = False
    if online:
        try:
            idx = yaml.safe_load(httpx.get(DELL_INDEX, timeout=6, follow_redirects=True).text)
            for c in ("csi-isilon", "csm-replication"):
                for e in (idx.get("entries") or {}).get(c, [])[:6]:
                    if e.get("digest"):
                        known[c].setdefault(str(e["version"]), e["digest"])
            live = True
        except Exception:
            pass
    inv = inventory()
    rows = []
    for comp, meta in COMPONENTS.items():
        have = {m["version"] for m in inv[comp]}
        vers = sorted(known.get(comp, {}), key=_vkey, reverse=True)
        for v in vers:
            rows.append({"component": comp, "version": v, "url": meta["url"].format(v=v), "sha256": known[comp][v],
                         "present": v in have})
    return {"live": live, "rows": rows}


# ------------------------------------------------------------------ adding files
def _chart_meta(tgz: Path) -> Dict:
    with tarfile.open(tgz, "r:gz") as t:
        for m in t.getmembers():
            parts = m.name.split("/")
            if len(parts) == 2 and parts[1] == "Chart.yaml" and m.isfile():
                return yaml.safe_load(t.extractfile(m).read()) or {}
    raise ValueError("not a helm chart archive (no <chart>/Chart.yaml inside)")


def _is_elf(p: Path) -> bool:
    with open(p, "rb") as f:
        return f.read(4) == b"\x7fELF"


def _extract_member(tgz: Path, want: str, dest: Path) -> bool:
    with tarfile.open(tgz, "r:gz") as t:
        for m in t.getmembers():
            if m.isfile() and m.name.split("/")[-1] == want:
                with t.extractfile(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                return True
    return False


def _run_version(exe: Path, args: List[str]) -> str:
    try:
        r = subprocess.run([str(exe)] + args, capture_output=True, text=True, timeout=20)
        return (r.stdout + r.stderr).strip()
    except Exception:
        return ""


def add_file(comp: str, src: Path, filename: str = "", version_hint: str = "", expected_sha: str = "", packed: bool = False) -> Dict:
    """Validate src and store it under bundles/dell/<comp>/<version>/. src is consumed (moved or removed)."""
    if comp not in COMPONENTS:
        raise ValueError(f"unknown component {comp}")
    _ensure_dirs()
    kind = COMPONENTS[comp]["kind"]
    work = Path(tempfile.mkdtemp(dir=INCOMING))
    try:
        sha = _sha256(src)
        if expected_sha and sha != expected_sha.strip().lower():
            raise ValueError(f"sha256 mismatch: file is {sha}, expected {expected_sha}")
        verified = ""
        if kind == "chart":
            try:
                meta = _chart_meta(src)
            except tarfile.TarError:
                raise ValueError("not a .tgz helm chart")
            if meta.get("name") != comp:
                raise ValueError(f"this is chart '{meta.get('name')}', expected '{comp}'")
            version = str(meta.get("version", ""))
            fname = f"{comp}-{version}.tgz"
            final_src = src
        elif kind == "helm":
            exe = work / "helm"
            if _is_elf(src):
                shutil.copyfile(src, exe)
            else:
                try:
                    if not _extract_member(src, "helm", exe):
                        raise ValueError("archive has no 'helm' binary")
                except tarfile.TarError:
                    raise ValueError("upload the helm .tar.gz from get.helm.sh or the bare linux-amd64 binary")
            exe.chmod(0o755)
            if not _is_elf(exe):
                raise ValueError("helm is not a Linux binary")
            out = _run_version(exe, ["version", "--short"])
            m = re.match(r"(v\d+\.\d+\.\d+)", out)
            if not m:
                raise ValueError(f"could not run it as helm: {out[:120]}")
            version = m.group(1)
            if not version.startswith("v3."):
                raise ValueError(f"helm {version}: Dell's charts need helm 3")
            fname = "helm"
            final_src = exe
        else:  # repctl
            exe = work / "repctl"
            if _is_elf(src):
                shutil.copyfile(src, exe)
            else:
                try:
                    if not _extract_member(src, "repctl", exe):
                        raise ValueError("archive has no 'repctl' binary")
                except tarfile.TarError:
                    raise ValueError("upload the repctl linux-amd64 binary (or a .tar.gz containing it)")
            exe.chmod(0o755)
            if not _is_elf(exe):
                raise ValueError("repctl is not a Linux binary")
            out = _run_version(exe, ["--version"])
            m = re.search(r"v?(\d+\.\d+\.\d+)", out)
            version = version_hint.lstrip("v") or (m.group(1) if m else "custom")
            fname = "repctl"
            final_src = exe
        if not _VER.match(version) and version != "custom":
            raise ValueError(f"unexpected version '{version}'")
        want = KNOWN.get(comp, {}).get(version)
        if packed:
            verified = ""               # packed here from an unpacked directory: nothing published to compare with
        elif want and kind == "chart":
            if sha != want:
                raise ValueError(f"{comp} {version}: sha256 {sha[:16]}... does not match Dell's published digest {want[:16]}...")
            verified = "Dell index digest"
        elif want and kind == "binary":
            if sha != want:
                raise ValueError(f"repctl {version}: sha256 does not match the digest GitHub publishes for dell/csm v{version}")
            verified = "GitHub release digest"
        elif want and kind == "helm" and not _is_elf(src):
            if sha != want:
                raise ValueError(f"helm {version}: sha256 does not match get.helm.sh's published checksum")
            verified = "get.helm.sh checksum"
        elif expected_sha:
            verified = "checksum you supplied"
        dest = BUNDLE_DIR / comp / version
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        shutil.move(str(final_src), dest / fname)
        os.chmod(dest / fname, 0o755 if kind != "chart" else 0o600)
        meta = {"component": comp, "version": version, "file": fname, "sha256": sha, "verified": verified,
                "source": filename or src.name, "added": datetime.utcnow().isoformat(timespec="seconds")}
        (dest / "meta.json").write_text(json.dumps(meta, indent=1))
        return meta
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            if src.exists() and INCOMING in src.parents:
                src.unlink()
        except Exception:
            pass


def incoming_file() -> Path:
    _ensure_dirs()
    fd, p = tempfile.mkstemp(dir=INCOMING, prefix="up-")
    os.close(fd)
    return Path(p)


def import_path(comp: str, path: str) -> Dict:
    """Add a file (or an unpacked chart directory) that already sits on this host."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError("give an absolute path on the installer host")
    try:
        st = p.lstat()
    except FileNotFoundError:
        raise ValueError(f"{p} does not exist")
    if stat.S_ISLNK(st.st_mode):
        p = p.resolve()
    tmp = incoming_file()
    if p.is_dir():
        if COMPONENTS.get(comp, {}).get("kind") != "chart":
            raise ValueError("a directory can only be imported as a helm chart")
        if not (p / "Chart.yaml").exists():
            raise ValueError(f"{p} has no Chart.yaml")
        name = (yaml.safe_load((p / "Chart.yaml").read_text()) or {}).get("name", p.name)
        with tarfile.open(tmp, "w:gz") as t:
            t.add(str(p), arcname=name, filter=lambda ti: None if ti.name.split("/")[-1] in (".git", ".helmignore") else ti)
    elif p.is_file():
        shutil.copyfile(p, tmp)
    else:
        raise ValueError(f"{p} is not a regular file or directory")
    return add_file(comp, tmp, filename=str(p), packed=p.is_dir())


def fetch(comp: str, version: str, log=print) -> Dict:
    """Download from the published URL (installer with internet only) and verify."""
    if comp not in COMPONENTS:
        raise ValueError(f"unknown component {comp}")
    url = COMPONENTS[comp]["url"].format(v=version)
    tmp = incoming_file()
    log(f"Downloading {url}")
    with httpx.stream("GET", url, timeout=60, follow_redirects=True) as r:
        if r.status_code != 200:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"HTTP {r.status_code} for {url}")
        n = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                n += len(chunk)
                if n > MAX_UPLOAD:
                    raise RuntimeError("download too large")
                f.write(chunk)
    expected = ""
    if comp == "helm" and version not in KNOWN["helm"]:
        try:
            expected = httpx.get(url + ".sha256sum", timeout=20, follow_redirects=True).text.split()[0]
        except Exception:
            expected = ""
    if comp in ("csi-isilon", "csm-replication") and version not in KNOWN[comp]:
        for row in catalog(True)["rows"]:
            if row["component"] == comp and row["version"] == version:
                expected = row["sha256"]
    m = add_file(comp, tmp, filename=url, expected_sha=expected, version_hint=version if comp == "repctl" else "")
    log(f"{comp} {m['version']} stored ({m['verified'] or 'no published checksum to compare'})")
    return m


def delete(comp: str, version: str):
    if comp not in COMPONENTS or not version or "/" in version or version.startswith("."):
        raise ValueError("bad component or version")
    d = BUNDLE_DIR / comp / version
    if not (d / "meta.json").exists():
        raise ValueError(f"{comp} {version} is not in the bundle")
    shutil.rmtree(d)


# ------------------------------------------------------------------ chart contents
def chart_file(comp: str, version: str, member: str) -> Optional[str]:
    tgz = path_of(comp, version)
    with tarfile.open(tgz, "r:gz") as t:
        for m in t.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) == 2 and parts[1] == member and m.isfile():
                return t.extractfile(m).read().decode()
    return None


def chart_values(comp: str, version: str) -> Dict:
    return yaml.safe_load(chart_file(comp, version, "values.yaml") or "") or {}


def chart_images(comp: str, version: str) -> List[str]:
    """Every image reference in the chart's default values (for mirroring)."""
    found: List[str] = []

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "image" and isinstance(x, str) and "/" in x:
                    found.append(x)
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(chart_values(comp, version))
    return sorted(set(found))


def summary() -> Dict:
    inv = inventory()
    return {"dir": str(BUNDLE_DIR), "components": [
        {"component": c, **{k: v for k, v in meta.items() if k != "url"}, "url_template": meta["url"], "files": inv[c]}
        for c, meta in COMPONENTS.items()]}
