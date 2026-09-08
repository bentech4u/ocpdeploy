"""Discover installable OpenShift versions from Red Hat's update graph.

Channels stable-4.N / fast-4.N / candidate-4.N are queried for each minor from
MIN_MINOR upwards until two consecutive minors come back empty. Results are
cached for an hour."""
import re
import threading
import time
from typing import Dict, List

import httpx

from ..settings import GRAPH_URL, MIRROR, MIN_MINOR

_cache: Dict[str, object] = {"ts": 0, "data": None}
_lock = threading.Lock()
CHANNELS = ("stable", "fast", "candidate")


def _vkey(v: str):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(.*)", v)
    if not m:
        return (0, 0, 0, v)
    return (int(m[1]), int(m[2]), int(m[3]), m[4])


def _graph_versions(channel: str, minor: int, client: httpx.Client) -> List[str]:
    r = client.get(GRAPH_URL, params={"channel": f"{channel}-4.{minor}", "arch": "amd64"},
                   headers={"Accept": "application/json"})
    if r.status_code != 200:
        return []
    nodes = r.json().get("nodes", [])
    return sorted({n["version"] for n in nodes if n["version"].startswith(f"4.{minor}.")}, key=_vkey)


def discover(force: bool = False) -> Dict:
    with _lock:
        if not force and _cache["data"] and time.time() - _cache["ts"] < 3600:
            return _cache["data"]
        minors = []
        empty = 0
        minor = MIN_MINOR
        with httpx.Client(timeout=15) as client:
            while empty < 2 and minor < MIN_MINOR + 20:
                entry = {"minor": f"4.{minor}", "channels": {}}
                any_found = False
                for ch in CHANNELS:
                    try:
                        vs = _graph_versions(ch, minor, client)
                    except Exception:
                        vs = []
                    if vs:
                        any_found = True
                        entry["channels"][ch] = vs
                if any_found:
                    entry["latest"] = entry["channels"].get("stable", entry["channels"].get("fast", entry["channels"].get("candidate")))[-1]
                    minors.append(entry)
                    empty = 0
                else:
                    empty += 1
                minor += 1
        data = {"minors": minors, "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "source": GRAPH_URL}
        _cache.update(ts=time.time(), data=data)
        return data


def mirror_has(version: str) -> bool:
    try:
        r = httpx.head(f"{MIRROR}/{version}/sha256sum.txt", timeout=10, follow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False
