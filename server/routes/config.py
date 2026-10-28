"""Configuration backup and restore.

:data:`router` (prefix ``/api/config``) is **admin only**. It exists so an
instance can be configured once and then rebuilt from a file: export the JSON,
tear the environment down including its PersistentVolumeClaims, and bring it
back by mounting the file and setting ``STOKER_CONFIG_IMPORT``.

The export carries configuration an operator authored (targets, pack repos, job
specs) and nothing a run produced. Secrets travel as the Fernet ciphertext they
are stored as, so a restore needs the same ``STOKER_MASTER_KEY``; see
:mod:`server.configio` for why that beats writing live HEC tokens in clear into
a file bound for a ConfigMap.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from .. import auth, configio, configsource
from ..config import get_settings
from ..db import get_db
from ..models import User

log = logging.getLogger("stoker.routes.config")

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/export")
def export_config(
    response: Response,
    secrets: str = Query("include", pattern="^(include|exclude)$"),
    download: bool = Query(True),
    db: Session = Depends(get_db),
    admin: User = Depends(auth.require_admin),
):
    # type: (...) -> object
    """The instance's configuration as JSON (admin only).

    ``secrets=exclude`` omits the encrypted HEC tokens and repo credentials, for
    a copy safe to commit or share; restoring it recreates the configuration
    with those fields blank, ready to be filled in.
    """
    doc = configio.export_config(
        db, include_secrets=(secrets == "include"), settings=get_settings())
    body = json.dumps(doc, indent=2, sort_keys=False, default=str) + "\n"
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="stoker-config.json"'
    log.info("config exported by %s (%d target(s), %d repo(s), %d spec(s), secrets=%s)",
             admin.username, len(doc["targets"]), len(doc["repos"]),
             len(doc["specs"]), secrets)
    return Response(content=body, media_type="application/json", headers=headers)


@router.post("/import")
def import_config(
    doc: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(auth.require_admin),
):
    # type: (...) -> object
    """Apply a configuration document (admin only); idempotent.

    Returns a report rather than failing the whole restore on a partial apply: a
    spec naming a pack that has not been indexed yet is normal when its repo was
    created by this very import, and re-running after the sync completes it.
    """
    try:
        report = configio.import_config(db, doc, settings=get_settings())
    except configio.ConfigError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "bad_config", "detail": str(exc)})
    db.commit()
    log.info("config imported by %s: %d target(s), %d repo(s), %d spec(s)%s",
             admin.username, report["targets"], report["repos"], report["specs"],
             "; %d skipped" % len(report["skipped"]) if report["skipped"] else "")
    return report


# --------------------------------------------------------------------------- #
# The shared copy (STOKER_CONFIG_SOURCE)
# --------------------------------------------------------------------------- #

def _require_source():
    # type: () -> object
    try:
        store = configsource.store_from_env()
    except configsource.ConfigSourceError as exc:
        raise HTTPException(status_code=409, detail={
            "error": "bad_config_source", "detail": str(exc)})
    if store is None:
        raise HTTPException(status_code=409, detail={
            "error": "no_config_source",
            "detail": "%s is not set" % configsource.SOURCE_ENV})
    return store


@router.get("/source")
def config_source(admin: User = Depends(auth.require_admin)):
    # type: (...) -> object
    """Where the shared copy lives, whether this instance writes it, and what
    the last pull and push did (admin only)."""
    return configsource.describe()


@router.post("/source/pull")
def pull_config_source(
    db: Session = Depends(get_db),
    admin: User = Depends(auth.require_admin),
):
    # type: (...) -> object
    """Apply the shared copy now (admin only). Same upsert as an import."""
    store = _require_source()
    try:
        report = configsource.pull(db, settings=get_settings(), store=store)
    except configsource.ConfigSourceError as exc:
        raise HTTPException(status_code=502, detail={
            "error": "config_source_failed", "detail": str(exc)})
    db.commit()
    log.info("config pulled from the source by %s: %d target(s), %d repo(s), "
             "%d spec(s) created", admin.username, report["targets"],
             report["repos"], report["specs"])
    return report


@router.post("/source/push")
def push_config_source(
    db: Session = Depends(get_db),
    admin: User = Depends(auth.require_admin),
):
    # type: (...) -> object
    """Write this instance's configuration to the shared copy now (admin only).

    409 unless ``STOKER_CONFIG_SOURCE_WRITE`` is on: the switch exists so an
    instance cannot overwrite a shared copy it was only meant to read, and a
    button that bypassed it would make the switch meaningless.
    """
    store = _require_source()
    if not configsource.writes_enabled():
        raise HTTPException(status_code=409, detail={
            "error": "config_source_read_only",
            "detail": "set %s=1 to let this instance write the shared copy"
            % configsource.WRITE_ENV})
    try:
        result = configsource.push(db, settings=get_settings(), store=store)
    except configsource.ConfigSourceError as exc:
        raise HTTPException(status_code=502, detail={
            "error": "config_source_failed", "detail": str(exc)})
    log.info("config pushed to the source by %s", admin.username)
    return result


__all__ = ["router"]
