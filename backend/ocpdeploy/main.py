import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .routers import clusters, discovery, checks, ops, day2, apps, templates
from .routers import auth as auth_router, imported as imported_router, extranodes as extranodes_router, manage as manage_router
from .settings import STATIC_DIR, LISTEN_HOST, LISTEN_PORT
from . import auth, imported

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # request URLs of cluster logins stay out of the journal
app = FastAPI(title="ocpdeploy", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

# ---------------------------------------------------------------- access control
PUBLIC_API = ("/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/logout")
PUBLIC_API_PREFIX = ("/api/iso/",)   # tokenised ISO URLs fetched by BMCs, which cannot log in
_CLUSTER_PATH = re.compile(r"^/api/clusters/([^/]+)(/.*)?$")
# what a connected (imported) cluster may use; everything else is install-time only
_IMPORTED_ALLOWED = re.compile(r"^(|/health(/.*)?|/upgrade(/.*)?|/scale(/.*)?|/identity(/.*)?|/certs(/.*)?|/storage(/.*)?|"
                               r"/operators(/.*)?|/backup|/backup/run|/backup/download/[^/]+|/apps(/.*)?|/jobs(/.*)?|/status|/extra-nodes(/.*)?|"
                               r"/maintenance(/.*)?|/logs(/.*)?|/mustgather(/.*)?|/projects(/.*)?|/capacity)$")


def _deny(status: int, detail: str):
    return JSONResponse(status_code=status, content={"detail": detail})


@app.middleware("http")
async def _access(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in PUBLIC_API or path.startswith(PUBLIC_API_PREFIX):
        return await call_next(request)
    s = auth.session(request.cookies.get(auth.SESSION_COOKIE))
    if not s:
        return _deny(401, "login required")
    request.state.session_id = s["id"]
    request.state.user = s["user"]
    m = _CLUSTER_PATH.match(path)
    if m and imported.exists(m.group(1)):
        c = imported.info(m.group(1)) or {}
        if c.get("owner") != s["id"]:
            return _deny(404, "cluster not found")
        rest = m.group(2) or ""
        if not _IMPORTED_ALLOWED.match(rest) or (rest == "" and request.method not in ("GET", "PUT")):
            return _deny(409, "not available for a connected cluster")
        if c.get("read_only") and request.method != "GET" and rest not in ("/extra-nodes/csrs", "/extra-nodes/check", "/maintenance/drain-check"):
            return _deny(403, "read-only connection: changes are disabled")
    return await call_next(request)


@app.on_event("startup")
def _imported_reset():
    imported.reset()                       # connected clusters never survive a restart
    auth.on_session_end(imported.session_ended)
    imported.start_sweeper()
    if not auth.has_users():
        logging.getLogger("ocpdeploy").warning("no console account yet: open the web UI to create one, or run: ocpdeployctl user set <name>")


@app.on_event("startup")
def _recover_jobs():
    from . import jobs
    from .services.deploy import finalize_after_restart
    try:
        jobs.recover({"deploy": finalize_after_restart, "resume": finalize_after_restart})
    except Exception:
        logging.getLogger("ocpdeploy").exception("job recovery failed")
app.include_router(auth_router.router)
app.include_router(imported_router.router)
app.include_router(clusters.router)
app.include_router(ops.iso_router)
app.include_router(extranodes_router.router)
app.include_router(extranodes_router.isos)
app.include_router(manage_router.router)
app.include_router(discovery.router)
app.include_router(checks.router)
app.include_router(ops.router)
app.include_router(day2.router)
app.include_router(apps.router)
app.include_router(templates.router)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    logging.getLogger("ocpdeploy").exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    _static_root = STATIC_DIR.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        f = (STATIC_DIR / path).resolve()
        if path and f.is_file() and f.is_relative_to(_static_root):
            return FileResponse(f)
        return FileResponse(STATIC_DIR / "index.html")


def main():
    import uvicorn
    uvicorn.run("ocpdeploy.main:app", host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")


if __name__ == "__main__":
    main()
