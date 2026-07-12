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
    app.state.drivers = _build_drivers_map()
    task = asyncio.create_task(_supervisor_loop(app), name="stoker-supervisor")
    app.state.supervisor_task = task
    log.info("control plane started; supervisor loop running (%.0fs cadence)",
             SUPERVISOR_INTERVAL_S)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        log.info("control plane stopped; supervisor loop cancelled")


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


def _install_auth_middleware(app):
    # type: (FastAPI) -> None
    """Require an authenticated session for guarded ``/api/*`` requests.

    Skipped entirely when ``STOKER_AUTH_DISABLED`` is set. Otherwise every
    guarded ``/api/*`` request must resolve to a user (trusted-proxy header or
    session cookie) once auth is active; an anonymous request gets a 401 JSON
    body and the UI redirects to login. Before any admin exists and with no proxy
    trust configured, the instance is in first-run mode: the guard stands down so
    the first admin can be created via ``/api/auth/setup`` (the API is otherwise
    open only in that bootstrap window). ``/api/users`` additionally requires the
    admin role, enforced in the route via ``require_admin``.
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
                    return "bootstrap"  # first-run: no admin yet, guard stands down
                user = auth_mod.resolve_user(request, db, settings)
                if user is None or not user.active:
                    return None
                return user.role

        outcome = await asyncio.to_thread(_resolve)
        if outcome == "bootstrap":
            return await call_next(request)
        if outcome is None:
            return JSONResponse(
                {"detail": "authentication required"}, status_code=401)
        # Role gate: /api/users is admin-only; any other mutating request needs
        # operator+ (so a read-only `viewer` cannot delete targets, launch runs
        # or register repos); safe methods need only an authenticated viewer.
        role, path, method = outcome, request.url.path, request.method
        if path == "/api/users" or path.startswith("/api/users/"):
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
        title="Stoker control plane",
        version="0.1.0",
        summary="Server-owned lifecycle for the Stoker load-generation fleet.",
        lifespan=_lifespan,
    )

    # Routers registered by importing the stable ``router`` objects.
    from .routes.agent import router as agent_router
    from .routes.api import router as api_router
    from .routes.auth import router as auth_router
    from .routes.auth import users_router

    app.include_router(agent_router)
    app.include_router(api_router)
    app.include_router(auth_router)
    app.include_router(users_router)

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
