import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .routers import clusters, discovery, checks, ops, day2
from .settings import STATIC_DIR, LISTEN_HOST, LISTEN_PORT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = FastAPI(title="ocpdeploy", version=__version__)


@app.on_event("startup")
def _recover_jobs():
    from . import jobs
    from .services.deploy import finalize_after_restart
    try:
        jobs.recover({"deploy": finalize_after_restart})
    except Exception:
        logging.getLogger("ocpdeploy").exception("job recovery failed")
app.include_router(clusters.router)
app.include_router(discovery.router)
app.include_router(checks.router)
app.include_router(ops.router)
app.include_router(day2.router)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    logging.getLogger("ocpdeploy").exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        f = STATIC_DIR / path
        if path and f.is_file():
            return FileResponse(f)
        return FileResponse(STATIC_DIR / "index.html")


def main():
    import uvicorn
    uvicorn.run("ocpdeploy.main:app", host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")


if __name__ == "__main__":
    main()
