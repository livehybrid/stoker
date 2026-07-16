"""FastAPI application factory and lifespan.

``create_app()`` wires the whole control plane:

* initialises the DB (engine + ``create_all``; Alembic baseline arrives later),
* seeds the ``fake-local`` and ``swarm-local`` fleets on first boot,
* registers the agent and operator routers by importing their ``router``
  objects (feature builders never edit this file),
* exposes ``/healthz``,
* runs a background **supervisor loop** in the lifespan that calls
  :func:`server.lifecycle.supervisor_tick` every ~2 s and, on boot, calls
  :func:`server.lifecycle.reconcile_on_boot`,
* serves ``ui/dist`` as static files when that directory is present.

The module-level ``app = create_app()`` is the uvicorn entry point.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
from typing import Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import lifecycle
from .config import get_settings
from .db import SessionLocal, init_db
from .drivers.base import ExecutionDriver

log = logging.getLogger("stoker.app")

# How often the supervisor loop wakes (contract: every ~2 s).
SUPERVISOR_INTERVAL_S = 2.0
# Where the built UI lands relative to this package's parent (server/../ui/dist).
_UI_DIST = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ui", "dist")

# Only ``/api/*`` is guarded by the auth middleware; the SPA shell, hashed
# ``/assets`` and ``/healthz`` are public (the HTML is public — the API it calls
# is what is protected, so the UI redirects to login on a 401).
_PROTECTED_PREFIX = "/api/"

# ``/api`` paths that are NOT session-guarded:
# * ``/api/agent`` — workers authenticate with a per-run JWT (see routes/agent).
# * ``/api/hooks`` — GitHub webhooks authenticate with a per-repo HMAC.
# * the unauthenticated auth entry points the login page needs before a session
#   exists (login/logout/status/setup). ``/api/auth/me`` and ``/api/users`` are
#   deliberately absent, so they require a session (and admin, in the route).
_AUTH_EXEMPT_PREFIXES = (
    "/api/agent",
    "/api/hooks",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/status",
    "/api/auth/setup",
)


def _build_drivers_map():
    # type: () -> Dict[str, ExecutionDriver]
    """Resolve a fleet-name -> driver map for the supervisor from seeded fleets.

    ``fake-local`` is always available (in-process). ``swarm-local`` is included
    only when Portainer is configured; otherwise it is skipped so the supervisor
    never tries to reach a Portainer that is not set up (a swarm run would fail
    loudly at provision time instead).
    """
    from sqlalchemy import select

    from .drivers import get_driver
    from .models import Fleet

    settings = get_settings()
    drivers = {}  # type: Dict[str, ExecutionDriver]
    with SessionLocal() as db:
        fleets = list(db.execute(select(Fleet)).scalars().all())
    for fleet in fleets:
        if fleet.driver == "swarm" and not settings.portainer_host:
            log.info("fleet %s skipped in supervisor map (Portainer not configured)",
                     fleet.name)
            continue
        try:
            drivers[fleet.name] = get_driver(fleet)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("could not build driver for fleet %s: %s", fleet.name, exc)
    return drivers


async def _supervisor_loop(app):
    # type: (FastAPI) -> None
    """Background task: tick the supervisor every ~2 s until cancelled.

    Each tick runs in a worker thread (the lifecycle is synchronous SQLAlchemy).
    ``NotImplementedError`` from the stubbed lifecycle is swallowed with a single
    debug line so the skeleton boots green before the Core builder fills it in;
    any other error is logged and the loop continues (a bad tick must not kill
    the control plane).
    """
    boot_time = app.state.boot_time
    drivers = app.state.drivers

    # One-shot boot reconciliation (best-effort; tolerate the stub).
    try:
        await asyncio.to_thread(_run_reconcile, drivers)
    except NotImplementedError:
        log.debug("reconcile_on_boot not implemented yet; skipping")
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("boot reconciliation failed: %s", exc)

    warned = False
    while True:
        try:
            await asyncio.to_thread(_run_tick, drivers, boot_time)
        except NotImplementedError:
            if not warned:
                log.debug("supervisor_tick not implemented yet; loop idling")
                warned = True
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("supervisor tick error: %s", exc)
        try:
            await asyncio.sleep(SUPERVISOR_INTERVAL_S)
        except asyncio.CancelledError:
            break


def _run_tick(drivers, boot_time):
    # type: (Dict[str, ExecutionDriver], datetime.datetime) -> None
    with SessionLocal() as db:
        lifecycle.supervisor_tick(db, drivers, boot_time)
        db.commit()


async def _maintenance_loop(app):
    # type: (FastAPI) -> None
    """Slow background loop: roll up + prune metric_samples ~hourly.

    Runs alongside the fast supervisor but on its own cadence
    (``metric_maintenance_interval_s``, ~1 h). The roll-up/prune runs in a worker
    thread (synchronous SQLAlchemy) and chunks + commits its own deletes, so a
    huge prune never blocks the supervisor. Any error is logged and the loop
    continues; the loop exits cleanly on cancellation.
    """
    settings = get_settings()
    interval = max(1.0, float(settings.metric_maintenance_interval_s))
    while True:
        # Sleep first: give the app a moment to settle before the initial pass,
        # and avoid a burst of work at every boot.
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        try:
            await asyncio.to_thread(_run_maintenance)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("metric maintenance error: %s", exc)


def _run_maintenance():
    # type: () -> None
    from . import metrics_lifecycle

    with SessionLocal() as db:
        metrics_lifecycle.roll_up_and_prune(db, get_settings())


async def _dogfood_metrics_loop(app):
    # type: (FastAPI) -> None
    """Periodic dogfood metrics loop: a stoker:metrics aggregate per active run.

    Only meaningful when ``dogfood_enabled``; when disabled the emit is a no-op
    so the loop simply idles at its cadence (``dogfood_metrics_interval_s``,
    ~30 s). Best-effort and failure-isolated (the emitter swallows HEC errors and
    never logs the token). Exits cleanly on cancellation.
    """
    settings = get_settings()
    if not settings.dogfood_enabled:
        # Nothing to ship: don't spin a loop that can only no-op.
        log.debug("dogfood disabled; metrics loop not started")
        return
    interval = max(1.0, float(settings.dogfood_metrics_interval_s))
    log.info("dogfood telemetry enabled; metrics loop running (%.0fs cadence)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        try:
            await asyncio.to_thread(_run_dogfood_metrics)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("dogfood metrics tick error: %s", exc)


def _run_dogfood_metrics():
    # type: () -> None
    from . import metrics_lifecycle

    with SessionLocal() as db:
        metrics_lifecycle.emit_active_run_metrics(db, get_settings())


def _run_reconcile(drivers):
    # type: (Dict[str, ExecutionDriver]) -> None
    with SessionLocal() as db:
        lifecycle.reconcile_on_boot(db, drivers)
        db.commit()


@contextlib.asynccontextmanager
async def _lifespan(app):
    # type: (FastAPI) -> Any
    """Start the supervisor on startup, cancel it cleanly on shutdown."""
    app.state.boot_time = datetime.datetime.now(datetime.timezone.utc)
    # Create the env-configured default admin before serving traffic (idempotent;
    # a no-op when unset or already present). Never logs the password.
    from . import auth as auth_mod

    with SessionLocal() as db:
        auth_mod.bootstrap_admin(db, get_settings())
        _warn_if_bootstrap_open(db, get_settings())
    app.state.drivers = _build_drivers_map()
    task = asyncio.create_task(_supervisor_loop(app), name="stoker-supervisor")
    app.state.supervisor_task = task
    # Slow background loops running alongside the supervisor: metric_samples
    # roll-up/prune (~hourly) and, when dogfood is on, the per-run metrics
    # aggregate (~30 s). Each is self-contained and failure-isolated.
    maintenance_task = asyncio.create_task(
        _maintenance_loop(app), name="stoker-metric-maintenance")
    app.state.maintenance_task = maintenance_task
    dogfood_task = asyncio.create_task(
        _dogfood_metrics_loop(app), name="stoker-dogfood-metrics")
    app.state.dogfood_task = dogfood_task
    log.info("control plane started; supervisor loop running (%.0fs cadence)",
             SUPERVISOR_INTERVAL_S)
    try:
        yield
    finally:
        for bg in (task, maintenance_task, dogfood_task):
            bg.cancel()
        for bg in (task, maintenance_task, dogfood_task):
            with contextlib.suppress(asyncio.CancelledError):
                await bg
        log.info("control plane stopped; background loops cancelled")


def _warn_if_bootstrap_open(db, settings):
    # type: (Any, Any) -> None
    """Log a loud warning when the instance boots in the open first-run window.

    With zero users, no trusted proxy and no env admin, the auth guard stands
    down so the first admin can be created via ``/api/auth/setup`` — which means
    the operator API is reachable UNAUTHENTICATED by anyone who can reach it,
    until setup runs. That is fine on a trusted LAN but dangerous if the instance
    is network-exposed. Warn loudly and point at the one-line fix. Runs after
    ``bootstrap_admin``, so an env admin has already closed the window (no warn).
    """
    from . import auth as auth_mod

    try:
        if not settings.auth_disabled and auth_mod.setup_needed(db, settings):
            log.warning(
                "SECURITY: no users, no trusted proxy and no STOKER_ADMIN_USER — "
                "the operator API is UNAUTHENTICATED until the first admin is "
                "created at /api/auth/setup. Create it immediately, or set "
                "STOKER_ADMIN_USER / STOKER_ADMIN_PASSWORD to close this window "
                "at boot — especially if this instance is network-exposed.")
    except Exception:  # pragma: no cover - defensive; a warning must never break boot
        pass


def _is_auth_exempt(path):
    # type: (str) -> bool
    """True when ``path`` is not subject to the session guard.

    Non-``/api/`` paths (the SPA shell, ``/assets``, ``/healthz``, favicon, the
    OpenAPI docs) are public. Within ``/api/`` the agent + webhook namespaces and
    the unauthenticated auth endpoints are exempt; everything else is guarded.
    """
    if not path.startswith(_PROTECTED_PREFIX):
        return True
    # Match only on a path-segment boundary: an unanchored startswith(p) would
    # silently exempt a future sibling like /api/agents or /api/setup-wizard.
    return any(path == p or path.startswith(p + "/")
               for p in _AUTH_EXEMPT_PREFIXES)


def _install_openapi(app):
    # type: (FastAPI) -> None
    """Add the ``bearerAuth`` security scheme to the generated OpenAPI spec.

    Swagger/ReDoc and any client-codegen tool then know the API accepts an
    ``Authorization: Bearer <token>`` credential: Swagger UI renders an
    **Authorize** box, and the scheme is applied as a **global** security
    requirement so the spec documents every operation as token-authenticable.
    The schema is generated once and cached on ``app.openapi_schema`` (FastAPI's
    own pattern), and ``/openapi.json`` / ``/docs`` / ``/redoc`` stay reachable
    (they are non-``/api`` and thus exempt from the auth guard).
    """
    from fastapi.openapi.utils import get_openapi

    def custom_openapi():
        # type: () -> dict
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        schemes["bearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "description": (
                "Stoker API token (stk_...): "
                "Authorization: Bearer <token>"
            ),
        }
        # Global requirement: every operation may be called with a bearer token.
        # Optional in effect (the unauthenticated entry points still work) but
        # this makes the Authorize box appear and drives usable client codegen.
        schema["security"] = [{"bearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def _install_auth_middleware(app):
    # type: (FastAPI) -> None
    """Require an authenticated session for guarded ``/api/*`` requests.

    Skipped entirely when ``STOKER_AUTH_DISABLED`` is set. Otherwise every
    guarded ``/api/*`` request must resolve to a user (trusted-proxy header or
    session cookie) once auth is active; an anonymous request gets a 401 JSON
    body and the UI redirects to login. Before any admin exists and with no proxy
    trust configured, the instance is in first-run mode: the guard stands down so
    the first admin can be created via ``/api/auth/setup`` (the API is otherwise
    open only in that bootstrap window — the lifespan logs a loud warning while
    it is open, and setting the env admin closes it at boot). ``/api/users``
    additionally requires the admin role, enforced in the route via
    ``require_admin``.
    """
    from . import auth as auth_mod

    @app.middleware("http")
    async def _auth_guard(request, call_next):
        # type: (Any, Any) -> Any
        settings = get_settings()
        if settings.auth_disabled or _is_auth_exempt(request.url.path):
            return await call_next(request)

        # A guarded /api path: resolve identity against a short-lived session.
        # Runs in a worker thread — SQLAlchemy here is synchronous.
        def _resolve():
            with SessionLocal() as db:
                if not auth_mod.auth_active(db, settings):
                    return "bootstrap"  # first-run: no admin yet
                user = auth_mod.resolve_user(request, db, settings)
                if user is None or not user.active:
                    return None
                return (user.role, user.username)

        outcome = await asyncio.to_thread(_resolve)
        if outcome == "bootstrap":
            # First-run window (zero users, no proxy trust): the guard stands
            # down so the first admin can be created via ``/api/auth/setup``. This
            # window is open by design; the lifespan logs a loud warning while it
            # is, and setting STOKER_ADMIN_USER/PASSWORD closes it entirely at
            # boot (see _warn_if_bootstrap_open + SECURITY.md).
            return await call_next(request)
        if outcome is None:
            return JSONResponse(
                {"detail": "authentication required"}, status_code=401)
        # Stash the resolved caller so mutating handlers can attribute the audit
        # trail (run started_by + event actor) to a specific admin/user or a
        # token principal (username "token:<name>"). Handlers fall back to
        # "operator" when this is absent (bootstrap / auth-disabled paths).
        role, username = outcome
        request.state.actor = username
        # Role gate: /api/users and /api/tokens are admin-only (managing users
        # or API tokens is a strictly higher privilege than holding one); any
        # other mutating request needs operator+ (so a read-only `viewer` cannot
        # delete targets, launch runs or register repos); safe methods need only
        # an authenticated viewer.
        path, method = request.url.path, request.method
        if (path == "/api/users" or path.startswith("/api/users/")
                or path == "/api/tokens" or path.startswith("/api/tokens/")):
            if role != "admin":
                return JSONResponse({"detail": "admin role required"},
                                    status_code=403)
        elif method not in ("GET", "HEAD", "OPTIONS"):
            if role not in ("operator", "admin"):
                return JSONResponse({"detail": "operator role required"},
                                    status_code=403)
        return await call_next(request)


def create_app():
    # type: () -> FastAPI
    """Build and return the configured FastAPI application."""
    settings = get_settings()

    # Schema + fleet seeding before the app serves traffic.
    init_db()
    with SessionLocal() as db:
        lifecycle.seed_fleets(db, settings=settings)

    app = FastAPI(
        title="Stoker Control Plane",
        version="0.1.0",
        summary="Server-owned lifecycle for the Stoker load-generation fleet.",
        description=(
            "HTTP API for the Stoker Splunk HEC load-generation control plane: "
            "targets, packs, specs and runs.\n\n"
            "Machine callers (CI/CD) authenticate with an **API token** minted at "
            "`POST /api/tokens` (admin only), presented as "
            "`Authorization: Bearer stk_...`. Interactive users use a session "
            "cookie from `POST /api/auth/login`. Use the **Authorize** button to "
            "supply a token when trying requests here."
        ),
        lifespan=_lifespan,
    )
    _install_openapi(app)

    # Routers registered by importing the stable ``router`` objects.
    from .routes.agent import router as agent_router
    from .routes.api import router as api_router
    from .routes.auth import router as auth_router
    from .routes.auth import users_router
    from .routes.metrics import router as metric_packs_router
    from .routes.tokens import router as tokens_router

    app.include_router(agent_router)
    app.include_router(api_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(tokens_router)
    app.include_router(metric_packs_router)

    # Session guard for /api/* (agent + webhook + unauthenticated auth endpoints
    # are exempt; the SPA shell and /healthz are public). Installed after the
    # routers so their paths are resolvable.
    _install_auth_middleware(app)

    @app.get("/healthz", tags=["ops"])
    def healthz():
        # type: () -> JSONResponse
        """Liveness probe: 200 with basic build/runtime info (no secrets)."""
        return JSONResponse(
            {
                "status": "ok",
                "service": "stoker-control-plane",
                "version": app.version,
                "database": "sqlite" if settings.is_sqlite else "postgres",
            }
        )

    _mount_ui(app)
    return app


def _mount_ui(app):
    # type: (FastAPI) -> None
    """Serve the built single-page UI from ``ui/dist`` when present.

    The UI is a client-routed SPA (TanStack Router). Hashed build assets under
    ``ui/dist/assets`` are served as real static files (a missing asset returns a
    genuine 404, never HTML, so cache-busting stays honest). Every other GET that
    is not an API/agent/ops route falls back to ``index.html`` so a hard load,
    refresh or bookmark of a client route (e.g. ``/runs/5``, ``/targets``) is
    handled by the in-browser router instead of 404ing. The API, the agent API
    and ``/healthz`` are registered before this and resolve first. Absence of
    ``ui/dist`` is normal (e.g. an API-only deployment) and logged at debug.
    """
    if not os.path.isdir(_UI_DIST):
        log.debug("ui/dist not present (%s); UI not mounted this stage", _UI_DIST)
        return

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    assets_dir = os.path.join(_UI_DIST, "assets")
    index_html = os.path.join(_UI_DIST, "index.html")

    # Hashed JS/CSS: served verbatim; a missing file is a real 404 (not the SPA
    # fallback) so a stale/incorrect asset reference never masquerades as HTML.
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="ui-assets")

    # Prefixes that must NOT be swallowed by the SPA fallback. These already have
    # registered handlers (so they resolve first), but we still refuse to serve
    # HTML for them: an unknown /api path should 404 as JSON-ish, not as the app.
    _reserved = ("api/", "agent/", "healthz", "assets/", "docs", "openapi.json", "redoc")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        # type: (str) -> Any
        """Return a concrete file under ui/dist if it exists, else index.html.

        This is the SPA history-fallback: client routes have no file on disk, so
        we hand back index.html and let the browser router render them.
        """
        if any(full_path == p.rstrip("/") or full_path.startswith(p) for p in _reserved):
            # Let the reserved namespaces 404 on their own terms (not as the app).
            return JSONResponse({"detail": "not found"}, status_code=404)
        # Serve a real static file at the root of dist (e.g. favicon) when present.
        if full_path:
            candidate = os.path.normpath(os.path.join(_UI_DIST, full_path))
            # Guard against path traversal escaping the dist directory.
            if candidate.startswith(_UI_DIST + os.sep) and os.path.isfile(candidate):
                return FileResponse(candidate)
        return FileResponse(index_html)

    log.info("serving SPA from %s (client-route fallback -> index.html)", _UI_DIST)


# uvicorn entry point: `uvicorn server.app:app`.
app = create_app()
