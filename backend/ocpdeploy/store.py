"""Per-cluster folder: clusters/<name>/cluster.json + state.sqlite + install/ + logs/."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import ClusterSpec
from .secrets import MASK, encrypt, decrypt, is_encrypted
from .settings import CLUSTERS_DIR

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  started TEXT NOT NULL,
  finished TEXT,
  exit_code INTEGER,
  meta TEXT
);
CREATE TABLE IF NOT EXISTS job_logs (
  job_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  ts TEXT NOT NULL,
  line TEXT NOT NULL,
  PRIMARY KEY (job_id, seq)
);
CREATE TABLE IF NOT EXISTS checks (
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  expected TEXT,
  actual TEXT,
  hint TEXT,
  ts TEXT NOT NULL,
  PRIMARY KEY (category, name)
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def _walk_secret(d: Dict[str, Any], fn):
    """Apply fn to every secret leaf in a raw cluster dict, in place."""
    if "pull_secret" in d:
        d["pull_secret"] = fn(d["pull_secret"], ("pull_secret",))
    vc = d.get("vcenter") or {}
    if "password" in vc:
        vc["password"] = fn(vc["password"], ("vcenter", "password"))
    for i, vm in enumerate((d.get("lb") or {}).get("vms") or []):
        if "ssh_password" in vm:
            vm["ssh_password"] = fn(vm["ssh_password"], ("lb", "vms", i, "ssh_password"))


class ClusterStore:
    def __init__(self, name: str):
        self.name = name
        self.dir: Path = CLUSTERS_DIR / name
        self.spec_file = self.dir / "cluster.json"
        self.db_file = self.dir / "state.sqlite"
        self.install_dir = self.dir / "install"
        self.logs_dir = self.dir / "logs"

    # ---- lifecycle -----------------------------------------------------
    def exists(self) -> bool:
        return self.spec_file.exists()

    def create(self, spec: ClusterSpec):
        with _lock:
            if self.exists():
                raise FileExistsError(self.name)
            for d in (self.dir, self.install_dir, self.logs_dir):
                d.mkdir(parents=True, exist_ok=True)
            self._init_db()
            self._write_raw(self._encrypt_all(spec.model_dump()))

    def _init_db(self):
        with self.db() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_file, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- spec ----------------------------------------------------------
    def _read_raw(self) -> Dict[str, Any]:
        return json.loads(self.spec_file.read_text())

    def _write_raw(self, raw: Dict[str, Any]):
        tmp = self.spec_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        tmp.replace(self.spec_file)

    @staticmethod
    def _encrypt_all(d: Dict[str, Any]) -> Dict[str, Any]:
        _walk_secret(d, lambda v, p: v if is_encrypted(v) else encrypt(v or ""))
        return d

    def load(self) -> ClusterSpec:
        """Spec with secrets decrypted; for internal use only."""
        raw = self._read_raw()
        _walk_secret(raw, lambda v, p: decrypt(v))
        return ClusterSpec.model_validate(raw)

    def public(self) -> Dict[str, Any]:
        """Spec safe to return over the API: secrets replaced by MASK or ''."""
        raw = self._read_raw()
        _walk_secret(raw, lambda v, p: MASK if (is_encrypted(v) and v.get("enc")) else "")
        return raw

    def update(self, incoming: Dict[str, Any]) -> ClusterSpec:
        """Merge an API payload. Secret fields equal to MASK keep their stored value."""
        with _lock:
            current = self._read_raw()
            existing_secrets: Dict[tuple, Any] = {}
            _walk_secret(current, lambda v, p: existing_secrets.__setitem__(p, v) or v)

            def keep_or_encrypt(v, p):
                if v == MASK:
                    return existing_secrets.get(p, encrypt(""))
                if is_encrypted(v):
                    return v
                return encrypt(v or "")

            incoming = json.loads(json.dumps(incoming))
            incoming["name"] = self.name
            # validate with decrypted view before persisting
            probe = json.loads(json.dumps(incoming))
            _walk_secret(probe, lambda v, p: "" if v == MASK else (decrypt(v) if is_encrypted(v) else v))
            ClusterSpec.model_validate(probe)
            _walk_secret(incoming, keep_or_encrypt)
            self._write_raw(incoming)
            return self.load()

    def set_status(self, status: str):
        with _lock:
            raw = self._read_raw()
            raw["status"] = status
            self._write_raw(raw)

    def patch(self, fn):
        """fn(raw_dict) -> mutate in place. Secrets untouched (still encrypted)."""
        with _lock:
            raw = self._read_raw()
            fn(raw)
            self._write_raw(raw)

    # ---- kv ------------------------------------------------------------
    def kv_get(self, key: str, default=None):
        with self.db() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value):
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value)))

    # ---- checks --------------------------------------------------------
    def save_checks(self, category: str, results: List[Dict[str, Any]]):
        from datetime import datetime
        ts = datetime.utcnow().isoformat()
        with self.db() as c:
            c.execute("DELETE FROM checks WHERE category=?", (category,))
            c.executemany(
                "INSERT INTO checks(category,name,status,expected,actual,hint,ts) VALUES(?,?,?,?,?,?,?)",
                [(category, r["name"], r["status"], r.get("expected", ""), r.get("actual", ""), r.get("hint", ""), ts) for r in results],
            )

    def get_checks(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.db() as c:
            if category:
                rows = c.execute("SELECT * FROM checks WHERE category=? ORDER BY rowid", (category,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM checks ORDER BY category, rowid").fetchall()
            return [dict(r) for r in rows]


def list_clusters() -> List[Dict[str, Any]]:
    out = []
    for p in sorted(CLUSTERS_DIR.iterdir()):
        if (p / "cluster.json").exists():
            s = ClusterStore(p.name)
            raw = s.public()
            out.append({
                "name": raw["name"],
                "base_domain": raw.get("base_domain", ""),
                "ocp_version": raw.get("ocp_version", ""),
                "install_method": raw.get("install_method", ""),
                "status": raw.get("status", "new"),
            })
    return out


def get_store(name: str) -> ClusterStore:
    s = ClusterStore(name)
    if not s.exists():
        raise KeyError(name)
    return s
