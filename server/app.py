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

    app.include_router(agent_router)
    app.include_router(api_router)

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
