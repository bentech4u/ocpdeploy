"""Console authentication: local users (bcrypt) in users.json, in-memory sessions.

users.json  {"users": {"<name>": {"hash": "<bcrypt>", "gen": <int>, "created": "...", "changed": "..."}}}
The file is shared with the CLI (ocpdeployctl user ...). Every password change bumps
"gen", which invalidates that user's existing sessions, even from another process."""
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import bcrypt

from .settings import USERS_FILE

SESSION_COOKIE = "ocpdeploy_session"
IDLE_TIMEOUT = 4 * 3600          # seconds without a request
ABSOLUTE_TIMEOUT = 24 * 3600     # seconds since login
MAX_FAILURES = 5                 # per client address within FAIL_WINDOW
FAIL_WINDOW = 600
LOCKOUT = 300

_lock = threading.RLock()
_cache: Dict[str, object] = {"mtime": None, "data": None}
_sessions: Dict[str, Dict] = {}
_failures: Dict[str, List[float]] = {}
_logout_hooks = []


# ---------------------------------------------------------------- password policy
def password_problems(username: str, password: str) -> List[str]:
    probs = []
    if len(password) < 12:
        probs.append("at least 12 characters")
    classes = sum(bool(re.search(rx, password)) for rx in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    if classes < 3:
        probs.append("at least three of: lowercase, uppercase, digit, symbol")
    if username and username.lower() in password.lower():
        probs.append("must not contain the username")
    if len(set(password)) < 6:
        probs.append("too few different characters")
    return probs


def valid_username(username: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,31}", username or ""))


# ---------------------------------------------------------------- users file
def _read() -> Dict:
    with _lock:
        try:
            m = USERS_FILE.stat().st_mtime_ns
        except FileNotFoundError:
            _cache.update(mtime=None, data={"users": {}})
            return _cache["data"]
        if _cache["mtime"] != m:
            try:
                data = json.loads(USERS_FILE.read_text() or "{}")
            except (ValueError, OSError):
                data = {}
            data.setdefault("users", {})
            _cache.update(mtime=m, data=data)
        return _cache["data"]


def _write(data: Dict):
    with _lock:
        USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USERS_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, USERS_FILE)
        os.chmod(USERS_FILE, 0o600)
        _cache.update(mtime=None, data=None)


def list_users() -> List[str]:
    return sorted(_read()["users"].keys())


def has_users() -> bool:
    return bool(_read()["users"])


def set_password(username: str, password: str, must_exist: Optional[bool] = None) -> None:
    """Create or update a user. must_exist=True: only update; False: only create."""
    if not valid_username(username):
        raise ValueError("username: 2-32 characters, letters, digits, dot, dash, underscore")
    probs = password_problems(username, password)
    if probs:
        raise ValueError("password too weak: " + "; ".join(probs))
    with _lock:
        data = json.loads(json.dumps(_read()))
        users = data["users"]
        exists = username in users
        if must_exist is True and not exists:
            raise KeyError(username)
        if must_exist is False and exists:
            raise FileExistsError(username)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        u = users.get(username, {"created": now, "gen": 0})
        u["hash"] = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
        u["gen"] = int(u.get("gen", 0)) + 1
        u["changed"] = now
        users[username] = u
        _write(data)


def delete_user(username: str) -> None:
    with _lock:
        data = json.loads(json.dumps(_read()))
        if username not in data["users"]:
            raise KeyError(username)
        del data["users"][username]
        _write(data)


def verify(username: str, password: str) -> Optional[int]:
    """Return the user's generation when the password is right, else None (constant-ish time)."""
    u = _read()["users"].get(username)
    h = (u or {}).get("hash") or "$2b$12$" + "x" * 53
    try:
        ok = bcrypt.checkpw(password.encode(), h.encode())
    except ValueError:
        ok = False
    return int(u.get("gen", 0)) if (u and ok) else None


# ---------------------------------------------------------------- brute force
def locked_out(client: str) -> bool:
    now = time.time()
    with _lock:
        f = [t for t in _failures.get(client, []) if now - t < FAIL_WINDOW]
        _failures[client] = f
        return len(f) >= MAX_FAILURES and now - f[-1] < LOCKOUT


def record_failure(client: str):
    with _lock:
        _failures.setdefault(client, []).append(time.time())


def clear_failures(client: str):
    with _lock:
        _failures.pop(client, None)


# ---------------------------------------------------------------- sessions
def create_session(username: str, gen: int, client: str) -> str:
    sid = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _sessions[sid] = {"user": username, "gen": gen, "created": now, "seen": now, "client": client}
    return sid


def session(sid: Optional[str]) -> Optional[Dict]:
    """Valid session or None. Touches last-seen."""
    if not sid:
        return None
    now = time.time()
    with _lock:
        s = _sessions.get(sid)
        if not s:
            return None
        u = _read()["users"].get(s["user"])
        if (not u or int(u.get("gen", 0)) != s["gen"] or now - s["seen"] > IDLE_TIMEOUT or now - s["created"] > ABSOLUTE_TIMEOUT):
            _drop(sid)
            return None
        s["seen"] = now
        return dict(s, id=sid)


def end_session(sid: Optional[str]):
    with _lock:
        if sid in _sessions:
            _drop(sid)


def _drop(sid: str):
    _sessions.pop(sid, None)
    for hook in list(_logout_hooks):
        try:
            hook(sid)
        except Exception:
            pass


def on_session_end(hook):
    """Register fn(session_id) called when a session ends (logout, expiry, password change)."""
    _logout_hooks.append(hook)


def live_session_ids() -> List[str]:
    """Session ids that are still valid (expired ones are dropped as a side effect)."""
    with _lock:
        ids = list(_sessions)
    return [i for i in ids if session_peek(i)]


def session_peek(sid: str) -> bool:
    now = time.time()
    with _lock:
        s = _sessions.get(sid)
        if not s:
            return False
        u = _read()["users"].get(s["user"])
        if not u or int(u.get("gen", 0)) != s["gen"] or now - s["seen"] > IDLE_TIMEOUT or now - s["created"] > ABSOLUTE_TIMEOUT:
            _drop(sid)
            return False
        return True
