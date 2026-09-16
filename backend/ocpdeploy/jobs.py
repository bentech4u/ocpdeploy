"""Background job runner with SQLite-persisted logs and live SSE fan-out.

A job is a Python callable job(ctx) where ctx.log(line) records output and
ctx.run(cmd, cwd, env) streams a subprocess. One running job per cluster.
"""
import json
import os
import re
import queue
import subprocess
import threading
import traceback
from datetime import datetime
from typing import Callable, Dict, List, Optional

from .store import ClusterStore

_subscribers: Dict[int, List[queue.Queue]] = {}
_running: Dict[str, int] = {}   # cluster -> job id
_units: Dict[int, str] = {}   # job id -> transient unit currently running
_lock = threading.Lock()


class JobCancelled(Exception):
    pass


_REDACT = [
    (re.compile(r'(password:\s*)"[^"]+"'), r'\1"<redacted; see Operate page>"'),
    (re.compile(r'(--password[= ]\S+)'), '--password <redacted>'),
]


def _redact(line: str) -> str:
    for rx, rep in _REDACT:
        line = rx.sub(rep, line)
    return line


class JobContext:
    def __init__(self, store: ClusterStore, job_id: int):
        self.store = store
        self.job_id = job_id
        self.seq = 0
        self.step = 0
        self.cancelled = False

    def log(self, line: str):
        line = _redact(line.rstrip("\n"))
        ts = datetime.utcnow().isoformat(timespec="seconds")
        with self.store.db() as c:
            self.seq += 1
            c.execute("INSERT INTO job_logs(job_id,seq,ts,line) VALUES(?,?,?,?)", (self.job_id, self.seq, ts, line))
        for q in list(_subscribers.get(self.job_id, [])):
            q.put({"seq": self.seq, "ts": ts, "line": line})

    def run(self, cmd: List[str], cwd: Optional[str] = None, env: Optional[dict] = None, check: bool = True) -> int:
        """Run cmd as a transient systemd unit and stream its output. The unit outlives
        the app process, so restarting ocpdeploy never kills a running install."""
        self.log(f"$ {' '.join(cmd)}")
        self.step += 1
        unit = f"ocpdeploy-{self.store.name}-job{self.job_id}-{self.step}"
        logfile = self.store.logs_dir / f"job{self.job_id}-{self.step}.out"
        logfile.touch()
        args = ["systemd-run", "--quiet", "--unit", unit, "-p", f"StandardOutput=append:{logfile}",
                "-p", f"StandardError=append:{logfile}", "-p", "KillMode=mixed"]
        if cwd:
            args += ["--working-directory", cwd]
        e = {"HOME": os.environ.get("HOME", "/root"), "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
             "LANG": "C.UTF-8"}
        if env:
            e.update(env)
        for k, v in e.items():
            args += ["--setenv", f"{k}={v}"]
        args += ["--"] + cmd
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"systemd-run failed: {r.stderr.strip()[-300:]}")
        _units[self.job_id] = unit
        try:
            code = self.follow_unit(unit, logfile)
        finally:
            _units.pop(self.job_id, None)
        if self.cancelled:
            raise JobCancelled()
        if check and code != 0:
            raise RuntimeError(f"command failed with exit code {code}: {cmd[0]}")
        return code

    def follow_unit(self, unit: str, logfile, pos: int = 0) -> int:
        """Tail logfile until the transient unit is gone; return its exit status."""
        import time
        buf = b""
        seen_active = False
        while True:
            with open(logfile, "rb") as f:
                f.seek(pos)
                chunk = f.read()
            if chunk:
                pos += len(chunk)
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for l in lines:
                    self.log(l.decode(errors="replace"))
            state, result, status = _unit_state(unit)
            if state in ("active", "activating", "deactivating"):
                seen_active = True
            elif state == "inactive" and result == "success":
                break
            elif state == "failed" or (state == "not-found" and seen_active):
                break
            elif state == "not-found" and not seen_active:
                # unit already finished and was garbage collected before we looked
                break
            time.sleep(1)
        if buf:
            self.log(buf.decode(errors="replace"))
        state, result, status = _unit_state(unit)
        code = 0 if (state == "not-found" or result == "success") else (status if status is not None else 1)
        if state == "failed":
            subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
        return code


def _unit_state(unit: str):
    r = subprocess.run(["systemctl", "show", unit, "-p", "LoadState,ActiveState,Result,ExecMainStatus"],
                       capture_output=True, text=True)
    props = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    if props.get("LoadState") == "not-found":
        return "not-found", None, None
    try:
        status = int(props.get("ExecMainStatus", "0"))
    except ValueError:
        status = None
    return props.get("ActiveState"), props.get("Result"), status


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


def run_sync(store: ClusterStore, kind: str, fn: Callable[[JobContext], None], log_stdout: bool = True) -> int:
    """Run a job in the calling thread (used by the CLI / systemd timers) and record it
    like any other job so it shows up in the Jobs list."""
    with store.db() as c:
        cur = c.execute("INSERT INTO jobs(kind,status,started,meta) VALUES(?,?,?,?)",
                        (kind, "running", datetime.utcnow().isoformat(timespec="seconds"), "{}"))
        job_id = cur.lastrowid
    ctx = JobContext(store, job_id)
    if log_stdout:
        orig = ctx.log

        def _log(line: str):
            orig(line)
            print(line, flush=True)
        ctx.log = _log
    status, code = "succeeded", 0
    try:
        ctx.log(f"== job {job_id} [{kind}] started ==")
        fn(ctx)
        ctx.log(f"== job {job_id} finished OK ==")
    except Exception as ex:  # noqa
        status, code = "failed", 1
        ctx.log("ERROR: " + str(ex))
    finally:
        with store.db() as c:
            c.execute("UPDATE jobs SET status=?, finished=?, exit_code=? WHERE id=?",
                      (status, datetime.utcnow().isoformat(timespec="seconds"), code, job_id))
    return code


def cancel(store: ClusterStore, job_id: int) -> bool:
    if store.name in _running and _running[store.name] == job_id:
        unit = _units.get(job_id)
        if unit:
            subprocess.run(["systemctl", "stop", unit], capture_output=True)
        return True
    return False


def recover(finalizers: Dict[str, Callable] = None):
    """Called at startup: re-attach to jobs that were running when the app stopped.
    If their transient unit is still alive, follow it to the end; otherwise mark failed."""
    from .store import list_clusters
    finalizers = finalizers or {}
    for c in list_clusters():
        store = ClusterStore(c["name"])
        with store.db() as db:
            rows = db.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
        for j in rows:
            job_id = j["id"]
            r = subprocess.run(["systemctl", "list-units", "--plain", "--no-legend", "--all",
                                f"ocpdeploy-{store.name}-job{job_id}-*"], capture_output=True, text=True)
            units = [l.split()[0] for l in r.stdout.splitlines() if l.strip()]
            live = [u for u in units if _unit_state(u)[0] in ("active", "activating")]
            with store.db() as db:
                seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM job_logs WHERE job_id=?", (job_id,)).fetchone()[0]
            ctx = JobContext(store, job_id)
            ctx.seq = seq
            if not live:
                ctx.log("== app restarted; job process is gone -> marked failed ==")
                with store.db() as db:
                    db.execute("UPDATE jobs SET status='failed', finished=?, exit_code=1 WHERE id=?",
                               (datetime.utcnow().isoformat(timespec="seconds"), job_id))
                continue
            unit = live[-1]
            step = int(unit.rsplit("-", 1)[1])
            logfile = store.logs_dir / f"job{job_id}-{step}.out"
            # resume tailing from the last line we stored (approximate: byte offset of stored lines)
            with store.db() as db:
                stored = db.execute("SELECT line FROM job_logs WHERE job_id=? ORDER BY seq", (job_id,)).fetchall()
            pos = 0
            if logfile.exists():
                data = logfile.read_bytes()
                # find how much of the file is already in the DB by matching the last stored line
                if stored:
                    last_line = stored[-1]["line"].encode()
                    idx = data.rfind(last_line)
                    pos = idx + len(last_line) + 1 if idx >= 0 else 0
            _running[store.name] = job_id
            _subscribers[job_id] = []

            def _thread(ctx=ctx, unit=unit, logfile=logfile, pos=pos, kind=j["kind"], store=store, job_id=job_id):
                ctx.log(f"== app restarted; re-attached to {unit} ==")
                status, code = "succeeded", 0
                try:
                    code = ctx.follow_unit(unit, logfile, pos)
                    if code != 0:
                        status = "failed"
                    fin = finalizers.get(kind)
                    if fin:
                        fin(ctx, store, code)
                except Exception as ex:  # noqa
                    status, code = "failed", 1
                    ctx.log("ERROR: " + str(ex))
                finally:
                    with store.db() as db:
                        db.execute("UPDATE jobs SET status=?, finished=?, exit_code=? WHERE id=?",
                                   (status, datetime.utcnow().isoformat(timespec="seconds"), code, job_id))
                    with _lock:
                        _running.pop(store.name, None)
                    for q in list(_subscribers.get(job_id, [])):
                        q.put({"done": True, "status": status})
                    _subscribers.pop(job_id, None)
            threading.Thread(target=_thread, name=f"job-{job_id}-recover", daemon=True).start()


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
