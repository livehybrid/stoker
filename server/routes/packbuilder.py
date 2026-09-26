"""Pack builder API: sample events in, a registered eventgen pack out.

``/api/pack-builder``:

* ``GET  /wordlists``            shipped word lists (title, size, sample values)
* ``GET  /wordlists/{name}``     one list's distinct values
* ``POST /analyse``              split pasted/uploaded text into events and
                                 suggest the fields to abstract
* ``POST /preview``              render events from a builder config
* ``POST /packs``                write + register a pack (201, ``PackOut``)
* ``GET  /packs/{id}``           a builder pack's config, to reopen it
* ``PUT  /packs/{id}``           rebuild a builder pack in place

A builder pack is an ordinary local pack: its directory lives under
``PACK_UPLOAD_DIR`` (beside uploaded packs), it registers and lints through the
same path as ``POST /api/packs/upload``, and ``DELETE /api/packs/{id}`` removes
it. Bundles are content-addressed, so rebuilding never changes a run already
provisioned from the old contents. Every endpoint is POST/PUT or read-only, so
the auth middleware's role gate applies unchanged (writes need operator).
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import bundles, packbuilder, packupload
from ..config import get_settings
from ..db import get_db
from ..gitsync import local_pack_metadata
from ..models import Pack
from ..schemas import PackOut

log = logging.getLogger("stoker.routes.packbuilder")

router = APIRouter(prefix="/api/pack-builder", tags=["pack-builder"])


class AnalyseRequest(BaseModel):
    text: str = Field(..., description="Pasted or uploaded events: one per line, or a JSON array")


class PreviewRequest(BaseModel):
    config: Dict[str, Any]
    n: int = 20
    seed: Optional[int] = None


class BuildRequest(BaseModel):
    config: Dict[str, Any]


def _bad(exc):
    # type: (Exception) -> HTTPException
    return HTTPException(status_code=422, detail={"error": "pack_builder_invalid", "detail": str(exc)})


def _validated(config):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    try:
        return packbuilder.validate_config(config)
    except packbuilder.BuilderError as exc:
        raise _bad(exc)


@router.get("/wordlists")
def list_wordlists():
    # type: () -> Any
    return packbuilder.wordlist_index()


@router.get("/wordlists/{name}")
def get_wordlist(name: str):
    # type: (str) -> Any
    try:
        values = packbuilder.load_wordlist(name)
    except packbuilder.BuilderError:
        raise HTTPException(status_code=404, detail="unknown word list")
    distinct = list(dict.fromkeys(values))
    return {"name": name, "count": len(distinct), "values": distinct}


@router.post("/analyse")
def analyse(body: AnalyseRequest):
    # type: (AnalyseRequest) -> Any
    try:
        events = packbuilder.split_events(body.text)
    except packbuilder.BuilderError as exc:
        raise _bad(exc)
    result = packbuilder.analyse(events)
    result["event_list"] = events
    return result


@router.post("/preview")
def preview(body: PreviewRequest):
    # type: (PreviewRequest) -> Any
    cfg = _validated(body.config)
    result = packbuilder.render_preview(cfg, n=body.n, seed=body.seed)
    result["highlights"] = packbuilder.highlight(
        cfg["events"][:packbuilder.HIGHLIGHT_EVENTS],
        [t for t in cfg["tokens"] if t["enabled"]])
    return result


def _name_taken(db, name, except_id=None):
    # type: (Session, str, Optional[int]) -> bool
    q = select(Pack.id).where(Pack.name == name)
    ids = [i for i in db.execute(q).scalars().all() if i != except_id]
    return bool(ids)


def _upload_root():
    # type: () -> str
    root = get_settings().pack_upload_dir
    os.makedirs(root, exist_ok=True)
    return root


def _stage(cfg):
    # type: (Dict[str, Any]) -> str
    """Write the pack into a hidden staging directory under the upload root."""
    staging = os.path.join(_upload_root(), ".builder-%s" % uuid.uuid4().hex)
    try:
        packbuilder.write_pack(cfg, staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _apply_lint(pack, pack_dir, cfg):
    # type: (Pack, str, Dict[str, Any]) -> None
    meta = local_pack_metadata(pack_dir)
    lint = bundles.lint_pack(pack_dir)
    pack.name = cfg["name"]
    pack.source_path = pack_dir
    pack.description = cfg["description"] or meta["description"]
    pack.tags_json = meta["tags"] or [packbuilder.BUILDER_TAG]
    pack.engines_json = lint.engines
    pack.builder_config_json = None
    pack.sourcetypes_json = lint.sourcetypes
    pack.stanza_count = lint.stanza_count
    pack.est_bytes_per_event = lint.est_bytes_per_event
    pack.declared_per_day_gb = lint.declared_per_day_gb
    pack.verified = lint.ok
    pack.lint_status = "ok" if lint.ok else "error"
    pack.lint_errors_json = lint.errors


@router.post("/packs", response_model=PackOut, status_code=201)
def create_pack(body: BuildRequest, db: Session = Depends(get_db)):
    # type: (BuildRequest, Session) -> Any
    cfg = _validated(body.config)
    if _name_taken(db, cfg["name"]):
        raise HTTPException(status_code=409, detail={
            "error": "pack_name_taken", "detail": "a pack named %r already exists" % cfg["name"]})
    staging = _stage(cfg)
    final = packupload._unique_pack_dir(_upload_root(), cfg["name"])
    os.rename(staging, final)
    pack = Pack(name=cfg["name"], source_path=final)
    try:
        _apply_lint(pack, final, cfg)
    except bundles.BundleError as exc:
        shutil.rmtree(final, ignore_errors=True)
        raise _bad(exc)
    db.add(pack)
    db.commit()
    db.refresh(pack)
    log.info("pack builder created %s (id=%s) -> %s lint=%s", pack.name, pack.id, final, pack.lint_status)
    return pack


def _builder_pack(db, pack_id):
    # type: (Session, int) -> Pack
    pack = db.get(Pack, pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="unknown pack")
    root = os.path.realpath(get_settings().pack_upload_dir)
    real = os.path.realpath(pack.source_path or "")
    if pack.repo_id is not None or not real.startswith(root + os.sep) \
            or packbuilder.read_builder_config(real) is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_a_builder_pack", "detail": "pack %d was not made with the pack builder" % pack_id})
    return pack


@router.get("/packs/{pack_id}")
def get_pack_config(pack_id: int, db: Session = Depends(get_db)):
    # type: (int, Session) -> Any
    pack = _builder_pack(db, pack_id)
    return {"pack_id": pack.id, "config": packbuilder.read_builder_config(os.path.realpath(pack.source_path))}


@router.put("/packs/{pack_id}", response_model=PackOut)
def update_pack(pack_id: int, body: BuildRequest, db: Session = Depends(get_db)):
    # type: (int, BuildRequest, Session) -> Any
    pack = _builder_pack(db, pack_id)
    cfg = _validated(body.config)
    if _name_taken(db, cfg["name"], except_id=pack.id):
        raise HTTPException(status_code=409, detail={
            "error": "pack_name_taken", "detail": "a pack named %r already exists" % cfg["name"]})
    current = os.path.realpath(pack.source_path)
    staging = _stage(cfg)
    retired = current + ".old-%s" % uuid.uuid4().hex[:8]
    # Swap: the pack directory is replaced whole, never half-written.
    os.rename(current, retired)
    try:
        os.rename(staging, current)
    except OSError:
        os.rename(retired, current)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(retired, ignore_errors=True)
    try:
        _apply_lint(pack, current, cfg)
    except bundles.BundleError as exc:
        raise _bad(exc)
    db.commit()
    db.refresh(pack)
    log.info("pack builder rebuilt %s (id=%s) lint=%s", pack.name, pack.id, pack.lint_status)
    return pack
