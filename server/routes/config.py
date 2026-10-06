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

from .. import auth, configio
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


__all__ = ["router"]
