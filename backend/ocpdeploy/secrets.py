"""Fernet-based at-rest encryption for passwords and the pull secret.

Secrets live inside cluster.json as {"enc": "<token>"}. The key never leaves
the installer host; API responses only ever expose the MASK placeholder.
"""
import os
from cryptography.fernet import Fernet
from .settings import SECRET_KEY_FILE

MASK = "********"


def _key() -> bytes:
    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def encrypt(value: str) -> dict:
    if value is None or value == "":
        return {"enc": ""}
    return {"enc": Fernet(_key()).encrypt(value.encode()).decode()}


def decrypt(blob) -> str:
    if not blob:
        return ""
    if isinstance(blob, str):
        return blob
    tok = blob.get("enc", "")
    if not tok:
        return ""
    return Fernet(_key()).decrypt(tok.encode()).decode()


def is_encrypted(v) -> bool:
    return isinstance(v, dict) and "enc" in v
