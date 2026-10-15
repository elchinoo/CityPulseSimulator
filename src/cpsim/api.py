import asyncio
import contextlib
import sys
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__, auth, metrics, reporter
from .catalog import DISTRICTS, GROUPS, TYPES
from .config import get_settings
from .model import (
    DEVICE_LIMITS,
    MAX_CITY_SENSORS,
    MAX_GROUP_COUNT,
    PROTOCOL_CHOICES,
    SpecError,
    parse_city,
)
from .protocols.image import MAX_CHANNELS
from .runtime import Runtime
from .store import open_store

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 512 * 1024
COOKIE = "cs_session"
CSRF_HEADER = "x-cpsim-csrf"
store = open_store()
runtime = Runtime(store)
metrics.bind(runtime, store)
_announced = False


def _announce_setup_code() -> None:
    global _announced
    if not _announced:
        _announced = True
        print("\n=== CityPulse Simulator first-run setup ===\n"
              "Open the app in your browser and enter this one-time code to create the admin password:\n"
              f"    {auth.setup_code()}\n"
              "(It changes every time the server restarts until setup is complete.)\n", file=sys.stderr, flush=True)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = []
    try:
        if auth.needs_setup(store):
            _announce_setup_code()
        await runtime.autostart()
    except Exception as exc:  # noqa: BLE001 - e.g. the database is asleep; the UI still comes up
        print(f"cpsim: startup: {str(exc).splitlines()[0][:160]}", file=sys.stderr, flush=True)
    rep = reporter.maybe_start(runtime)
    if rep:
        tasks.append(asyncio.create_task(rep.run()))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await runtime.stop_all()


_FAIL_WINDOW, _FAIL_LIMIT = 60.0, 10
_failures: dict[str, list[float]] = {}


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def _rate_limited(client: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failures.get(client, []) if now - t < _FAIL_WINDOW]
    _failures[client] = recent
    return len(recent) >= _FAIL_LIMIT


def _record_failure(client: str) -> None:
    _failures.setdefault(client, []).append(time.monotonic())
    if len(_failures) > 10_000:
        _failures.clear()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header[:7].lower() == "bearer " else None


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def authenticate(request: Request, scope: str) -> bool:
    """True if the caller may use `scope` ('full' or 'metrics'). Raises 401/429 for bad credentials."""
    if get_settings().allow_anonymous:
        return True
    client = _client(request)
    if _rate_limited(client):
        raise HTTPException(429, "too many failed attempts; wait a minute")
    raw = _bearer(request)
    if raw is not None:
        if auth.static_key_matches(raw) or (scope == "metrics" and auth.metrics_token_matches(raw)):
            return True
        _record_failure(client)
        raise HTTPException(401, "invalid credentials")
    return auth.session_valid(request.cookies.get(COOKIE)) and scope == "full"


def require(scope: str = "full"):
    def dep(request: Request) -> None:
        if not authenticate(request, scope):
            raise HTTPException(401 if not _bearer(request) else 403, "authentication required")
    return dep


class Guard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        started, code = time.perf_counter(), 500
        try:
            resp = await self._dispatch(request, call_next)
            code = resp.status_code
            return resp
        finally:
            label = getattr(request.scope.get("route"), "path", None) or "other"
            if label != "/metrics":
                metrics.observe(request.method, label, code, started)

    async def _dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith("/api/") and int(request.headers.get("content-length", "0") or 0) > MAX_BODY:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        if (path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}
                and _bearer(request) is None and not request.headers.get(CSRF_HEADER)):
            return JSONResponse({"detail": "missing CSRF header"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = resp.headers.get("Cache-Control", "no-store")
        resp.headers["Content-Security-Policy"] = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                                   "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        return resp


app = FastAPI(title="CityPulse Simulator", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(Guard)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__, "cities_running": len(runtime.runners)}


@app.get("/readyz")
def readyz():
    return {"status": "ready"} if store.healthy() else JSONResponse({"status": "waking-database"}, status_code=503)


@app.get("/metrics", dependencies=[Depends(require("metrics"))])
def prometheus_metrics():
    body, ctype = metrics.render()
    return Response(body, media_type=ctype)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# --- session ------------------------------------------------------------------------------------

class SetupBody(BaseModel):
    code: str = Field(max_length=64)
    password: str = Field(max_length=256)


class LoginBody(BaseModel):
    password: str | None = Field(default=None, max_length=256)
    api_key: str | None = Field(default=None, max_length=512)


class PasswordBody(BaseModel):
    current: str | None = Field(default=None, max_length=256)
    new: str = Field(max_length=256)


def _start_session(request: Request, response: Response) -> None:
    response.set_cookie(COOKIE, auth.create_session(), httponly=True, samesite="strict", secure=_is_https(request),
                        max_age=auth.SESSION_SECONDS, path="/")


def _guard_attempts(request: Request) -> str:
    client = _client(request)
    if _rate_limited(client):
        raise HTTPException(429, "too many failed attempts; wait a minute")
    return client


@app.get("/api/session")
def session_state(request: Request):
    try:
        ok = authenticate(request, "full")
        setup = auth.needs_setup(store) and not get_settings().allow_anonymous
        has_password = auth.admin_configured(store)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - store unavailable (database asleep)
        raise HTTPException(503, "the state store is unavailable; it may be waking up, retry in a few seconds") from exc
    if setup:
        _announce_setup_code()
    state = "setup_required" if setup else "logged_in" if ok else "logged_out"
    return {"state": state, "has_admin_password": has_password, "password_login": has_password,
            "key_login": bool(get_settings().api_key), "version": __version__, "store": store.kind}


@app.post("/api/setup")
def setup(body: SetupBody, request: Request, response: Response):
    client = _guard_attempts(request)
    try:
        auth.complete_setup(store, body.code, body.password)
    except PermissionError as exc:
        _record_failure(client)
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _start_session(request, response)
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    client = _guard_attempts(request)
    ok = (body.password is not None and auth.login(store, body.password)) or \
         (body.api_key is not None and auth.static_key_matches(body.api_key))
    if not ok:
        _record_failure(client)
        raise HTTPException(401, "invalid credentials")
    _start_session(request, response)
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    auth.delete_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.post("/api/admin-password", dependencies=[Depends(require())])
def admin_password(body: PasswordBody, request: Request):
    client = _guard_attempts(request)
    try:
        if auth.admin_configured(store):
            if body.current is None:
                raise HTTPException(422, "current password is required")
            auth.change_password(store, body.current, body.new)
        else:
            auth.set_password(store, body.new)
    except PermissionError as exc:
        _record_failure(client)
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "signed_out_everywhere": True}


# --- cities --------------------------------------------------------------------------------------

@app.get("/api/catalog", dependencies=[Depends(require())])
def catalog():
    return {"types": [{"key": t.key, "label": t.label, "unit": t.unit, "group": t.group, "lo": t.lo, "hi": t.hi,
                       "interval": t.interval} for t in TYPES.values()],
            "groups": GROUPS, "districts": [{"id": k, "name": v} for k, v in DISTRICTS.items()],
            "protocols": list(PROTOCOL_CHOICES),
            "limits": {"group_count": MAX_GROUP_COUNT, "city_sensors": MAX_CITY_SENSORS, "channels_per_device": MAX_CHANNELS,
                       "devices": DEVICE_LIMITS}}


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(422, str(exc))


def _summary(name: str, city) -> dict:
    runner = runtime.runners.get(name)
    return {"name": name, "target_url": city.target_url, "sensors": city.sensor_count(), "groups": len(city.groups),
            "running": runner is not None, "autostart": city.autostart,
            "protocols": sorted({g.protocol for g in city.groups}),
            "stats": runner.snapshot() if runner else None}


@app.get("/api/cities", dependencies=[Depends(require())])
def list_cities():
    try:
        return [_summary(n, c) for n, c in runtime.specs().items()]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, "the state store is unavailable") from exc


@app.get("/api/cities/{name}", dependencies=[Depends(require())])
def get_city(name: str):
    city = runtime.get(name)
    if city is None:
        raise HTTPException(404, "no such city")
    return {**city.to_dict(), "running": name in runtime.runners}


@app.put("/api/cities/{name}", dependencies=[Depends(require())])
def put_city(name: str, spec: dict):
    """Create or replace a city. A running city must be stopped first."""
    if name in runtime.runners:
        raise HTTPException(409, "stop the city before changing it")
    try:
        city = parse_city({**spec, "name": name})
    except SpecError as exc:
        raise _bad(exc) from exc
    store.save_city(name, city.to_dict())
    return {"ok": True, "sensors": city.sensor_count()}


@app.delete("/api/cities/{name}", dependencies=[Depends(require())])
async def delete_city(name: str):
    await runtime.stop(name)
    store.delete_city(name)
    return {"ok": True}


@app.post("/api/cities/{name}/start", dependencies=[Depends(require())])
async def start_city(name: str):
    try:
        runner = await runtime.start(name)
    except KeyError as exc:
        raise HTTPException(404, "no such city") from exc
    except SpecError as exc:
        raise _bad(exc) from exc
    except OSError as exc:
        raise HTTPException(500, f"could not open the protocol endpoints: {exc.strerror}") from exc
    return runner.snapshot()


@app.post("/api/cities/{name}/stop", dependencies=[Depends(require())])
async def stop_city(name: str):
    return {"stopped": await runtime.stop(name)}


class BoostBody(BaseModel):
    seconds: int = Field(default=120, ge=5, le=1800)


@app.post("/api/cities/{name}/burst", dependencies=[Depends(require())])
def burst_city(name: str, body: BoostBody):
    """Temporarily make every sensor's burst episodes far more likely (a storm over the city)."""
    runner = runtime.runners.get(name)
    if runner is None:
        raise HTTPException(409, "the city is not running")
    runner.boost(body.seconds)
    return runner.snapshot()


@app.get("/api/cities/{name}/stats", dependencies=[Depends(require())])
def city_stats(name: str):
    runner = runtime.runners.get(name)
    if runner is None:
        raise HTTPException(404, "the city is not running")
    return runner.snapshot()
