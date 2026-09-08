"""Background job runner with SQLite-persisted logs and live SSE fan-out.

A job is a Python callable job(ctx) where ctx.log(line) records output and
ctx.run(cmd, cwd, env) streams a subprocess. One running job per cluster.
"""
import json
import os
import queue
import subprocess
import threading
import traceback
from datetime import datetime
from typing import Callable, Dict, List, Optional

from .store import ClusterStore

_subscribers: Dict[int, List[queue.Queue]] = {}
_running: Dict[str, int] = {}   # cluster -> job id
_procs: Dict[int, subprocess.Popen] = {}
_lock = threading.Lock()


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, store: ClusterStore, job_id: int):
        self.store = store
        self.job_id = job_id
        self.seq = 0
        self.cancelled = False

    def log(self, line: str):
        line = line.rstrip("\n")
        ts = datetime.utcnow().isoformat(timespec="seconds")
        with self.store.db() as c:
            self.seq += 1
            c.execute("INSERT INTO job_logs(job_id,seq,ts,line) VALUES(?,?,?,?)", (self.job_id, self.seq, ts, line))
        for q in list(_subscribers.get(self.job_id, [])):
            q.put({"seq": self.seq, "ts": ts, "line": line})

    def run(self, cmd: List[str], cwd: Optional[str] = None, env: Optional[dict] = None, check: bool = True) -> int:
        self.log(f"$ {' '.join(cmd)}")
        e = dict(os.environ)
        if env:
            e.update(env)
        p = subprocess.Popen(cmd, cwd=cwd, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        _procs[self.job_id] = p
        try:
            for line in p.stdout:
                self.log(line)
            p.wait()
        finally:
            _procs.pop(self.job_id, None)
        if self.cancelled:
            raise JobCancelled()
        if check and p.returncode != 0:
            raise RuntimeError(f"command failed with exit code {p.returncode}: {cmd[0]}")
        return p.returncode


def start(store: ClusterStore, kind: str, fn: Callable[[JobContext], None], meta: Optional[dict] = None) -> int:
    with _lock:
        if store.name in _running:
            raise RuntimeError(f"job {_running[store.name]} already running for {store.name}")
        with store.db() as c:
            cur = c.execute("INSERT INTO jobs(kind,status,started,meta) VALUES(?,?,?,?)",
                            (kind, "running", datetime.utcnow().isoformat(timespec="seconds"), json.dumps(meta or {})))
            job_id = cur.lastrowid
        _running[store.name] = job_id
        _subscribers[job_id] = []

    ctx = JobContext(store, job_id)

    def _thread():
        status, code = "succeeded", 0
        try:
            ctx.log(f"== job {job_id} [{kind}] started ==")
            fn(ctx)
            ctx.log(f"== job {job_id} finished OK ==")
        except JobCancelled:
            status, code = "cancelled", 130
            ctx.log("== job cancelled ==")
        except Exception as ex:  # noqa
            status, code = "failed", 1
            ctx.log("ERROR: " + str(ex))
            for l in traceback.format_exc().splitlines()[-8:]:
                ctx.log("  " + l)
        finally:
            with store.db() as c:
                c.execute("UPDATE jobs SET status=?, finished=?, exit_code=? WHERE id=?",
                          (status, datetime.utcnow().isoformat(timespec="seconds"), code, job_id))
            with _lock:
                _running.pop(store.name, None)
            for q in list(_subscribers.get(job_id, [])):
                q.put({"done": True, "status": status})
            _subscribers.pop(job_id, None)

    threading.Thread(target=_thread, name=f"job-{job_id}", daemon=True).start()
    return job_id


def cancel(store: ClusterStore, job_id: int) -> bool:
    p = _procs.get(job_id)
    if store.name in _running and _running[store.name] == job_id:
        if p:
            p.terminate()
        return True
    return False


def running_job(store: ClusterStore) -> Optional[int]:
    return _running.get(store.name)


def list_jobs(store: ClusterStore, limit: int = 50):
    with store.db() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_job(store: ClusterStore, job_id: int):
    with store.db() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None


def get_logs(store: ClusterStore, job_id: int, after: int = 0):
    with store.db() as c:
        rows = c.execute("SELECT seq,ts,line FROM job_logs WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after)).fetchall()
        return [dict(r) for r in rows]


def subscribe(job_id: int) -> Optional[queue.Queue]:
    q: queue.Queue = queue.Queue()
    subs = _subscribers.get(job_id)
    if subs is None:
        return None
    subs.append(q)
    return q


def unsubscribe(job_id: int, q: queue.Queue):
    subs = _subscribers.get(job_id)
    if subs and q in subs:
        subs.remove(q)
