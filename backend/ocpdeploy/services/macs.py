"""Deterministic locally-administered MAC addresses for agent-installed nodes.
VMware accepts manual MACs in 00:50:56:00:00:00 - 00:50:56:3F:FF:FF."""
import hashlib


def mac_for(cluster: str, node: str) -> str:
    h = hashlib.sha256(f"{cluster}/{node}".encode()).digest()
    b1 = h[0] & 0x3F
    return f"00:50:56:{b1:02x}:{h[1]:02x}:{h[2]:02x}"
