"""Console login: first-run setup, login, logout, password change, status."""
import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Creds(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current: str
    new: str


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_cookie(request: Request, response: Response, sid: str):
    response.set_cookie(auth.SESSION_COOKIE, sid, httponly=True, samesite="strict", path="/",
                        secure=request.url.scheme == "https", max_age=auth.ABSOLUTE_TIMEOUT)


@router.get("/status")
def status(request: Request):
    s = auth.session(request.cookies.get(auth.SESSION_COOKIE))
    return {"setup_required": not auth.has_users(), "authenticated": bool(s), "user": s["user"] if s else None,
            "policy": "at least 12 characters, three of: lowercase, uppercase, digit, symbol; not containing the username"}


@router.post("/setup")
def setup(body: Creds, request: Request, response: Response):
    """Create the first account. Only possible while no account exists."""
    with auth._lock:
        if auth.has_users():
            raise HTTPException(409, "an account already exists; log in instead")
        try:
            auth.set_password(body.username, body.password, must_exist=False)
        except ValueError as ex:
            raise HTTPException(422, str(ex))
    gen = auth.verify(body.username, body.password)
    _set_cookie(request, response, auth.create_session(body.username, gen, _client(request)))
    return {"user": body.username}


@router.post("/login")
async def login(body: Creds, request: Request, response: Response):
    client = _client(request)
    if auth.locked_out(client):
        raise HTTPException(429, "too many failed logins; wait five minutes")
    gen = await asyncio.get_running_loop().run_in_executor(None, auth.verify, body.username, body.password)
    if gen is None:
        auth.record_failure(client)
        await asyncio.sleep(0.8)
        raise HTTPException(401, "wrong username or password")
    auth.clear_failures(client)
    _set_cookie(request, response, auth.create_session(body.username, gen, client))
    return {"user": body.username}


@router.post("/logout")
def logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/password")
def change_password(body: PasswordChange, request: Request, response: Response):
    s = auth.session(request.cookies.get(auth.SESSION_COOKIE))
    if not s:
        raise HTTPException(401, "not logged in")
    if auth.verify(s["user"], body.current) is None:
        raise HTTPException(403, "current password is wrong")
    try:
        auth.set_password(s["user"], body.new, must_exist=True)   # ends every session of this user
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    gen = auth.verify(s["user"], body.new)
    _set_cookie(request, response, auth.create_session(s["user"], gen, _client(request)))
    return {"ok": True}
