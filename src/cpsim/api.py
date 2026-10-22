import asyncio
import contextlib
import sys
from datetime import date
from pathlib import Path

import psycopg
from apgkit.auth.rp import safe_return_to
from apgkit.config import env_bool
from apgkit.guard import SecurityGuard
from apgkit.web import is_https, mount_kit_static, public_url
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, auth, citygen, core, credentials, metrics, reporter
from .catalog import DISTRICT_KINDS, DISTRICTS, GROUPS, TYPES
from .core import COOKIE, CSRF_HEADER, fed, local
from .hr_catalog import DEPARTMENTS
from .hr_catalog import TYPES as HR_TYPES
from .hr_gen import build_plan
from .hr_model import MAX_EMPLOYEES, MAX_PER_TYPE, MAX_TOTAL, parse_company
from .kinds import KINDS, MANAGED_ELSEWHERE
from .model import (
    DEVICE_LIMITS,
    MAX_CITY_SENSORS,
    MAX_GROUP_COUNT,
    PROTOCOL_CHOICES,
    SpecError,
    check_target,
    parse_city,
)
from .protocols.image import MAX_CHANNELS
from .runtime import Runtime
from .schedule import MAX_BATCH, MAX_INTERVAL, MIN_INTERVAL, max_rate
from .store import open_store
from .wl_model import parse_workload

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 512 * 1024
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
        with core.db.connect(retries=1) as conn:
            if auth.needs_setup(conn) and not fed.enabled:
                _announce_setup_code()
            credentials.load(conn, core.vault)
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


# --- authentication ---------------------------------------------------------------------------------------
# Fail closed: with no credential configured every protected route is denied (and the setup flow is offered), unless the
# operator explicitly sets CPSIM_ALLOW_ANONYMOUS=true. The mechanics (local admin, API tokens, federated JWTs, sessions, rate
# limiting) are agentpg-kit's `Authenticator`; here we only name the permission each route needs.

def authenticate(request: Request):
    return core.auth.authenticate(request)


def require(permission: str, **kw):
    return core.auth.require(permission, **kw)


def _guard_attempts(request: Request) -> str:
    client = core.auth.client(request)
    if core.auth.limiter.limited(client):
        raise HTTPException(429, "too many failed attempts; wait a minute")
    return client


def _db_unavailable() -> HTTPException:
    return core.auth.db_unavailable()


app = FastAPI(title="CityPulse Simulator", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityGuard, csrf_header=CSRF_HEADER, max_body=MAX_BODY, observe=core.metrics.observe)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
mount_kit_static(app)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__, "cities_running": len(runtime.runners)}


@app.get("/readyz")
def readyz():
    return {"status": "ready"} if store.healthy() else JSONResponse({"status": "waking-database"}, status_code=503)


@app.get("/metrics", dependencies=[Depends(require("metrics:scrape", allow_cookie=False))])
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


class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    scope: str = Field(pattern="^(read|metrics|full)$")


def _start_session(conn, request: Request, response: Response, **who) -> None:
    raw = local.create_session(conn, **who)
    response.set_cookie(COOKIE, raw, httponly=True, samesite="strict" if not who else "lax", secure=is_https(request),
                        max_age=auth.SESSION_SECONDS, path="/")


@app.get("/api/session")
def session_state(request: Request):
    """Public: tells the UI which screen to show. Reveals no secrets."""
    principal = None
    try:
        principal = authenticate(request)
        with core.db.connect(retries=1) as conn:
            setup = local.needs_setup(conn) and not env_bool("CPSIM_ALLOW_ANONYMOUS") and not fed.enabled
            has_password = local.admin_configured(conn)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    if setup:
        _announce_setup_code()
    state = "setup_required" if setup else "logged_in" if principal else "logged_out"
    local_ok = fed.local_login or not fed.enabled
    out = {"state": state, "has_admin_password": has_password, "password_login": has_password and local_ok,
           "key_login": local.static_key_configured() and local_ok, "version": __version__,
           "sso": {"issuer": fed.issuer, "login_url": "/auth/login"} if fed.enabled else None}
    if principal:
        out["user"] = {"name": principal.name, "email": principal.email, "source": principal.source, "roles": sorted(principal.roles),
                       "permissions": ["*"] if "*" in principal.permissions else sorted(principal.permissions)}
    return out


# ---- federated sign-in (only when CPSIM_OIDC_ISSUER is set) ---------------------------------------------------
@app.get("/auth/login")
def sso_login(request: Request, next: str | None = None):
    if not fed.enabled:
        raise HTTPException(404, "single sign-on is not configured")
    try:
        with core.db.connect(retries=1) as conn:
            url = fed.rp.begin(conn, public_url(request, core.public_url_env) + "/auth/callback", next)
    except psycopg.Error as exc:
        raise _db_unavailable() from exc
    except Exception:  # noqa: BLE001 - the identity provider may be unreachable
        return RedirectResponse("/?sso_error=the identity provider is unavailable", status_code=303)
    return RedirectResponse(url, status_code=303)


@app.get("/auth/callback")
def sso_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not fed.enabled:
        raise HTTPException(404, "single sign-on is not configured")
    if error or not code or not state:
        return RedirectResponse("/?sso_error=the sign-in was cancelled or failed", status_code=303)
    from apgkit.auth import TokenError
    try:
        with core.db.connect(retries=1) as conn:
            claims, back, id_token = fed.rp.finish(conn, public_url(request, core.public_url_env) + "/auth/callback", code, state)
            roles = [r for r in claims.get("roles", []) if isinstance(r, str)]
            resp = RedirectResponse(safe_return_to(back), status_code=303)
            if not core.APP_AUTH.permissions_for(roles=roles):
                return RedirectResponse("/?sso_error=your account has no role in CityPulse Simulator; ask an administrator", status_code=303)
            _start_session(conn, request, resp, subject=str(claims["sub"]), name=str(claims.get("name") or ""), email=str(claims.get("email") or ""),
                           roles=roles, source="oidc", id_token_hint=id_token)
            return resp
    except TokenError as exc:
        return RedirectResponse("/?sso_error=" + str(exc)[:120], status_code=303)


@app.post("/api/setup")
def setup(body: SetupBody, request: Request, response: Response):
    client = _guard_attempts(request)
    try:
        with core.db.connect(retries=1) as conn:
            try:
                local.complete_setup(conn, body.code, body.password)
            except PermissionError as exc:
                core.auth.limiter.record(client)
                raise HTTPException(403, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            _start_session(conn, request, response)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    client = _guard_attempts(request)
    try:
        with core.db.connect(retries=1) as conn:
            ok = False
            if body.password is not None:
                ok = local.login_password(conn, body.password)
            elif body.api_key is not None:
                ok = local.static_key_matches(body.api_key)
            if not ok:
                core.auth.limiter.record(client)
                raise HTTPException(401, "invalid credentials")
            _start_session(conn, request, response)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    out: dict = {"ok": True}
    try:
        with core.db.connect(retries=1) as conn:
            row = local.delete_session(conn, request.cookies.get(COOKIE))
        if row and row["source"] == "oidc" and fed.enabled:
            with contextlib.suppress(Exception):
                out["logout_url"] = fed.rp.logout_url(row["id_token_hint"], public_url(request, core.public_url_env) + "/")
    except (psycopg.Error, RuntimeError):
        pass  # the cookie is cleared regardless; the session row expires on its own
    response.delete_cookie(COOKIE, path="/")
    return out


@app.post("/api/admin-password", dependencies=[Depends(require("settings:manage"))])
def admin_password(body: PasswordBody, request: Request):
    """Set the admin password (first time, when signed in with a static key) or change it."""
    client = _guard_attempts(request)
    try:
        with core.db.connect() as conn:
            try:
                if local.admin_configured(conn):
                    if body.current is None:
                        raise HTTPException(422, "current password is required")
                    local.change_password(conn, body.current, body.new)
                else:
                    local.set_admin_password(conn, body.new)
            except PermissionError as exc:
                core.auth.limiter.record(client)
                raise HTTPException(403, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            conn.execute("DELETE FROM web_session")  # sign every browser out after a password change
            conn.commit()
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return {"ok": True, "signed_out_everywhere": True}


@app.get("/api/tokens", dependencies=[Depends(require("settings:manage"))])
def list_tokens():
    with core.db.connect() as conn:
        return local.list_tokens(conn)


@app.post("/api/tokens", dependencies=[Depends(require("settings:manage"))])
def create_token(body: TokenBody):
    """The token value is returned exactly once; only its hash is stored."""
    with core.db.connect() as conn:
        token_id, raw = local.create_token(conn, body.name.strip(), body.scope)
    return {"id": token_id, "token": raw, "scope": body.scope, "name": body.name.strip()}


@app.delete("/api/tokens/{token_id}", dependencies=[Depends(require("settings:manage"))])
def revoke_token(token_id: int):
    with core.db.connect() as conn:
        if not local.revoke_token(conn, token_id):
            raise HTTPException(404, "token not found or already revoked")
    return {"revoked": token_id}


# --- credentials the simulator presents to the apps it drives ----------------------------------------------

class CredentialBody(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


def _credential_users() -> dict[str, list[str]]:
    """Which variable names the saved cities, companies and workloads refer to, and by what."""
    users: dict[str, list[str]] = {}
    for kind, specs in (("city", store.cities()), ("company", runtime.store.companies()), ("workload", store.workloads())):
        for name, spec in specs.items():
            for key in ("token_env", "publishable_env"):
                if spec.get(key):
                    users.setdefault(spec[key], []).append(f"{kind} {name}")
    return users


@app.get("/api/credentials", dependencies=[Depends(require("settings:manage"))])
def list_credentials():
    """Names only, with where each value comes from. A value is never returned."""
    users = _credential_users()
    names = {k.token_env for k in KINDS.values() if k.token_env} | {"CITYPULSE_INGEST_TOKEN", "TRUSTDESK_CLIENT_TOKEN"} | set(users)
    with core.db.connect() as conn:
        saved = credentials.saved_names(conn)
    return {"unlocked": core.vault.is_unlocked(),
            "items": [{"name": n, "source": credentials.source(n), "saved": n in saved, "used_by": sorted(users.get(n, []))}
                      for n in sorted(names)]}


@app.put("/api/credentials/{name}", dependencies=[Depends(require("settings:manage"))])
def save_credential(name: str, body: CredentialBody):
    try:
        with core.db.connect() as conn:
            credentials.put(conn, core.vault, name, body.value)
    except SpecError as exc:
        raise HTTPException(409 if "locked" in str(exc) else 422, str(exc)) from exc
    return {"name": name, "source": credentials.source(name)}


@app.delete("/api/credentials/{name}", dependencies=[Depends(require("settings:manage"))])
def delete_credential(name: str):
    with core.db.connect() as conn:
        if not credentials.delete(conn, name):
            raise HTTPException(404, "no saved credential with that name")
    return {"deleted": name}


# --- cities --------------------------------------------------------------------------------------

@app.get("/api/catalog", dependencies=[Depends(require("sim:read"))])
def catalog():
    return {"types": [{"key": t.key, "label": t.label, "unit": t.unit, "group": t.group, "lo": t.lo, "hi": t.hi,
                       "interval": t.interval} for t in TYPES.values()],
            "groups": GROUPS, "districts": [{"id": k, "name": v, "kind": DISTRICT_KINDS[k]} for k, v in DISTRICTS.items()],
            "generator": {"sizes": citygen.SIZES, "profiles": list(citygen.PROFILES), "max_count": citygen.MAX_COUNT},
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


@app.get("/api/cities", dependencies=[Depends(require("sim:read"))])
def list_cities():
    try:
        return [_summary(n, c) for n, c in runtime.specs().items()]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, "the state store is unavailable") from exc


class GenerateBody(BaseModel):
    name: str | None = Field(None, max_length=40)
    seed: int | None = Field(None, ge=0, lt=2**32)
    size: str | int = "medium"
    profile: str = "mixed"
    count: int = Field(1, ge=1, le=citygen.MAX_COUNT)
    target_url: str = Field("", max_length=300)
    token_env: str = Field("CITYPULSE_INGEST_TOKEN", max_length=64)
    autostart: bool = False


@app.post("/api/cities/generate", dependencies=[Depends(require("sim:edit"))])
def generate_cities(body: GenerateBody):
    """Create 1 to 50 varied demo cities (never replacing an existing one) and report what each contains."""
    try:
        taken = set(store.cities())
        made = citygen.generate_cities(body.count, body.size, body.profile, body.seed, body.name, taken,
                                       target_url=check_target(body.target_url), token_env=body.token_env, autostart=body.autostart)
        for g in made:
            parse_city(g.city)
    except SpecError as exc:
        if "already exists" in str(exc):
            raise HTTPException(409, str(exc)) from exc
        raise _bad(exc) from exc
    for g in made:
        store.save_city(g.name, g.city)
    cities = [g.report() for g in made]
    return {"created": len(cities), "sensors": sum(c["sensors"] for c in cities), "cities": cities}


@app.get("/api/cities/{name}", dependencies=[Depends(require("sim:read"))])
def get_city(name: str):
    city = runtime.get(name)
    if city is None:
        raise HTTPException(404, "no such city")
    return {**city.to_dict(), "running": name in runtime.runners}


@app.put("/api/cities/{name}", dependencies=[Depends(require("sim:edit"))])
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


@app.delete("/api/cities/{name}", dependencies=[Depends(require("sim:edit"))])
async def delete_city(name: str):
    await runtime.stop(name)
    store.delete_city(name)
    return {"ok": True}


@app.post("/api/cities/{name}/start", dependencies=[Depends(require("sim:run"))])
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


@app.post("/api/cities/{name}/stop", dependencies=[Depends(require("sim:run"))])
async def stop_city(name: str):
    return {"stopped": await runtime.stop(name)}


class BoostBody(BaseModel):
    seconds: int = Field(default=120, ge=5, le=1800)


@app.post("/api/cities/{name}/burst", dependencies=[Depends(require("sim:run"))])
def burst_city(name: str, body: BoostBody):
    """Temporarily make every sensor's burst episodes far more likely (a storm over the city)."""
    runner = runtime.runners.get(name)
    if runner is None:
        raise HTTPException(409, "the city is not running")
    runner.boost(body.seconds)
    return runner.snapshot()


@app.get("/api/cities/{name}/stats", dependencies=[Depends(require("sim:read"))])
def city_stats(name: str):
    runner = runtime.runners.get(name)
    if runner is None:
        raise HTTPException(404, "the city is not running")
    return runner.snapshot()


# --- companies: simulated employers that send employees and requests to TrustDesk ----------------------------

def _mix(**counts: int) -> dict:
    return {k: v for k, v in counts.items() if v}


PRESETS = [
    {"id": "quick", "label": "Quick check (about 60 requests)", "employees": {"new": 4, "tenured": 12},
     "requests": _mix(vacation=10, sick_leave=8, business_trip=4, expense_travel=6, expense_customer=6, expense_meals=6,
                      expense_other=4, remote_work=3, overtime=3, other=4, parental_leave=2, equipment_request=2, bereavement_leave=2)},
    {"id": "hundreds", "label": "Hundreds of mixed requests (about 500)", "employees": {"new": 30, "tenured": 120},
     "requests": _mix(vacation=90, sick_leave=60, parental_leave=8, bereavement_leave=6, unpaid_leave=8, business_trip=25,
                      expense_travel=50, expense_customer=40, expense_meals=40, expense_medical=6, expense_training=10,
                      expense_equipment=12, expense_home_office=8, expense_other=30, remote_work=20, overtime=15,
                      training_request=12, equipment_request=10, other=50)},
    {"id": "leave", "label": "Leave stress: vacation and sick leave for old and new hires", "employees": {"new": 25, "tenured": 60},
     "requests": _mix(vacation=250, sick_leave=150, parental_leave=15, bereavement_leave=15, unpaid_leave=10)},
    {"id": "expenses", "label": "Expense audit: every kind of reimbursement", "employees": {"new": 15, "tenured": 60},
     "requests": _mix(expense_travel=90, expense_customer=70, expense_meals=70, expense_medical=25, expense_training=25,
                      expense_equipment=30, expense_home_office=25, expense_other=40)},
]


@app.get("/api/hr/catalog", dependencies=[Depends(require("sim:read"))])
def hr_catalog():
    return {"types": [{"key": t.key, "label": t.label, "kind": t.kind} for t in HR_TYPES.values()],
            "departments": [d for d, _ in DEPARTMENTS], "presets": PRESETS,
            "limits": {"per_type": MAX_PER_TYPE, "total": MAX_TOTAL, "employees": MAX_EMPLOYEES}}


def _company_summary(name: str, c) -> dict:
    run = runtime.hr_runs.get(name)
    return {"name": name, "target_url": c.target_url, "employees": c.new_employees + c.tenured_employees,
            "requests": c.total_requests(), "types": len(c.requests), "problem_pct": c.problem_pct,
            "state": run.state if run else "idle", "dry_run": not c.target_url,
            "scheduled": bool(runtime.company_schedules.get(name) and runtime.company_schedules[name].running),
            "every": c.schedule["interval"] if c.schedule else None}


@app.get("/api/companies", dependencies=[Depends(require("sim:read"))])
def list_companies():
    try:
        return [_company_summary(n, c) for n, c in runtime.companies().items()]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, "the state store is unavailable") from exc


@app.get("/api/companies/{name}", dependencies=[Depends(require("sim:read"))])
def get_company(name: str):
    c = runtime.company(name)
    if c is None:
        raise HTTPException(404, "no such company")
    run = runtime.hr_runs.get(name)
    sched = runtime.company_schedules.get(name)
    return {**c.to_dict(), "state": run.state if run else "idle", "schedule_run": sched.snapshot() if sched else None}


@app.put("/api/companies/{name}", dependencies=[Depends(require("sim:edit"))])
def put_company(name: str, spec: dict):
    run = runtime.hr_runs.get(name)
    sched = runtime.company_schedules.get(name)
    if (run and run.running) or (sched and sched.running):
        raise HTTPException(409, "stop the company (and its schedule) before changing it")
    try:
        c = parse_company({**spec, "name": name})
    except SpecError as exc:
        raise _bad(exc) from exc
    runtime.store.save_company(name, c.to_dict())
    return {"ok": True, "employees": c.new_employees + c.tenured_employees, "requests": c.total_requests()}


@app.delete("/api/companies/{name}", dependencies=[Depends(require("sim:edit"))])
async def delete_company(name: str):
    await runtime.stop_company_schedule(name)
    await runtime.stop_hr(name)
    runtime.hr_runs.pop(name, None)
    runtime.company_schedules.pop(name, None)
    runtime.store.delete_company(name)
    return {"ok": True}


@app.post("/api/companies/{name}/preview", dependencies=[Depends(require("sim:read"))])
def preview_company(name: str):
    """What a run would send, decided from the seed, without sending anything."""
    c = runtime.company(name)
    if c is None:
        raise HTTPException(404, "no such company")
    plan = build_plan(c)
    samples: dict[str, list] = {}
    per: dict[str, dict] = {}
    for it in plan.items:
        p = per.setdefault(it.type, {"planned": 0, "problem": 0})
        p["planned"] += 1
        p["problem"] += it.problem
        if len(samples.setdefault(it.type, [])) < 2:
            samples[it.type].append({"problem": it.problem, **it.payload})
    depts: dict[str, int] = {}
    for e in plan.employees:
        depts[e.department] = depts.get(e.department, 0) + 1
    return {"seed": plan.seed, "employees": len(plan.employees), "departments": depts,
            "new_hires": sum(1 for e in plan.employees if e.tenure_days(date.today()) < 90),
            "requests": len(plan.items), "problem": plan.problems(), "types": per, "samples": samples,
            "use_existing_note": "Employees already in TrustDesk are added to the pool at run time." if c.use_existing else ""}


@app.post("/api/companies/{name}/run", dependencies=[Depends(require("sim:run"))])
async def run_company(name: str):
    try:
        run = runtime.start_hr(name)
    except KeyError as exc:
        raise HTTPException(404, "no such company") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except SpecError as exc:
        raise _bad(exc) from exc
    return run.snapshot()


@app.post("/api/companies/{name}/stop", dependencies=[Depends(require("sim:run"))])
async def stop_company(name: str):
    return {"stopped": await runtime.stop_hr(name)}


@app.get("/api/companies/{name}/status", dependencies=[Depends(require("sim:read"))])
def company_status(name: str):
    run = runtime.hr_runs.get(name)
    if run is None:
        raise HTTPException(404, "this company has not run yet")
    return run.snapshot()


@app.post("/api/companies/{name}/refresh", dependencies=[Depends(require("sim:run"))])
async def refresh_company(name: str):
    """Ask TrustDesk where the requests of the latest run stand (approved, rejected, waiting...)."""
    run = runtime.hr_runs.get(name)
    if run is None:
        raise HTTPException(404, "this company has not run yet")
    try:
        await run.refresh_statuses()
    except Exception as exc:  # noqa: BLE001 - e.g. the token was revoked meanwhile
        raise HTTPException(502, f"could not read the statuses: {exc}") from exc
    return run.snapshot()


@app.post("/api/companies/{name}/schedule/start", dependencies=[Depends(require("sim:run"))])
async def start_company_schedule(name: str):
    """Repeat the company's run on its schedule (every `interval` seconds)."""
    try:
        runner = runtime.start_company_schedule(name)
    except KeyError as exc:
        raise HTTPException(404, "no such company") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except SpecError as exc:
        raise _bad(exc) from exc
    return runner.snapshot()


@app.post("/api/companies/{name}/schedule/stop", dependencies=[Depends(require("sim:run"))])
async def stop_company_schedule(name: str):
    return {"stopped": await runtime.stop_company_schedule(name)}


# --- workloads: scheduled pushes to the other apps of the ecosystem ---------------------------------------------------------

@app.get("/api/workloads/catalog", dependencies=[Depends(require("sim:read"))])
def workload_catalog():
    return {"kinds": [{"key": k.name, "label": k.label, "description": k.description, "token_env": k.token_env, "scope": k.scope,
                       "min_interval": max(k.min_interval, MIN_INTERVAL), "max_batch": min(k.max_batch, MAX_BATCH),
                       "schedule": k.schedule, "params": k.defaults(), "fields": k.fields, "presets": k.presets,
                       "url_hint": k.url_hint, "env_hint": k.env_hint} for k in KINDS.values()],
            "managed_elsewhere": MANAGED_ELSEWHERE,
            "limits": {"interval": [MIN_INTERVAL, MAX_INTERVAL], "batch": MAX_BATCH, "max_rate": max_rate()}}


def _workload_summary(name: str, wl) -> dict:
    runner = runtime.wl_runners.get(name)
    snap = runner.snapshot() if runner else None
    return {"name": name, "kind": wl.kind, "target_url": wl.target_url, "interval": wl.schedule.interval, "batch": wl.schedule.batch,
            "start_on_boot": wl.schedule.start_on_boot, "state": snap["state"] if snap else "idle", "stats": snap}


@app.get("/api/workloads", dependencies=[Depends(require("sim:read"))])
def list_workloads():
    try:
        return [_workload_summary(n, w) for n, w in runtime.workloads().items()]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, "the state store is unavailable") from exc


@app.get("/api/workloads/{name}", dependencies=[Depends(require("sim:read"))])
def get_workload(name: str):
    wl = runtime.workload(name)
    if wl is None:
        raise HTTPException(404, "no such workload")
    runner = runtime.wl_runners.get(name)
    return {**wl.to_dict(), "state": runner.state if runner else "idle"}


@app.put("/api/workloads/{name}", dependencies=[Depends(require("sim:edit"))])
def put_workload(name: str, spec: dict):
    """Create or replace a workload. A running one must be stopped first."""
    runner = runtime.wl_runners.get(name)
    if runner and runner.running:
        raise HTTPException(409, "stop the workload before changing it")
    try:
        wl = parse_workload({**spec, "name": name})
    except SpecError as exc:
        raise _bad(exc) from exc
    store.save_workload(name, wl.to_dict())
    return {"ok": True, "rate": round(wl.schedule.rate(), 3)}


@app.delete("/api/workloads/{name}", dependencies=[Depends(require("sim:edit"))])
async def delete_workload(name: str):
    await runtime.stop_workload(name)
    runtime.wl_runners.pop(name, None)
    store.delete_workload(name)
    return {"ok": True}


@app.get("/api/workloads/{name}/status", dependencies=[Depends(require("sim:read"))])
def workload_status(name: str):
    runner = runtime.wl_runners.get(name)
    if runner is None:
        raise HTTPException(404, "this workload has not run yet")
    return runner.snapshot()


@app.post("/api/workloads/{name}/start", dependencies=[Depends(require("sim:run"))])
async def start_workload(name: str):
    try:
        runner = runtime.start_workload(name)
    except KeyError as exc:
        raise HTTPException(404, "no such workload") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except SpecError as exc:
        raise _bad(exc) from exc
    return runner.snapshot()


@app.post("/api/workloads/{name}/stop", dependencies=[Depends(require("sim:run"))])
async def stop_workload(name: str):
    return {"stopped": await runtime.stop_workload(name)}


@app.post("/api/workloads/{name}/run-once", dependencies=[Depends(require("sim:run"))])
async def run_workload_once(name: str):
    """One batch now, outside the schedule (it still counts in the workload's numbers)."""
    try:
        return await runtime.run_workload_once(name)
    except KeyError as exc:
        raise HTTPException(404, "no such workload") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except SpecError as exc:
        raise _bad(exc) from exc


class ScenarioBody(BaseModel):
    scenario: str = Field(max_length=20)


@app.post("/api/workloads/{name}/scenario", dependencies=[Depends(require("sim:run"))])
def set_workload_scenario(name: str, body: ScenarioBody):
    """PagerLens workloads: switch the failure scenario of a RUNNING workload (healthy, bad_deploy, error_spike, latency, silence)."""
    runner = runtime.wl_runners.get(name)
    if runner is None or not runner.running:
        raise HTTPException(409, "the workload is not running")
    if not hasattr(runner.sender, "set_scenario"):
        raise HTTPException(409, "this kind of workload has no scenarios")
    try:
        runner.sender.set_scenario(body.scenario)
    except SpecError as exc:
        raise _bad(exc) from exc
    return runner.snapshot()
