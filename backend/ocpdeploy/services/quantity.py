"""Kubernetes resource quantities."""
import re

_BIN = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DEC = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}


def parse(q) -> float:
    if q is None or q == "":
        return 0.0
    if isinstance(q, (int, float)):
        return float(q)
    m = re.fullmatch(r"([0-9.eE+-]+)([a-zA-Z]*)", str(q).strip())
    if not m:
        return 0.0
    num, suf = float(m.group(1)), m.group(2)
    if suf in _BIN:
        return num * _BIN[suf]
    return num * _DEC.get(suf, 1)
