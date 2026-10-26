"""Pack builder API: sample events in, a registered eventgen pack out.

``/api/pack-builder``:

* ``GET  /wordlists``            shipped and custom word lists and tables
                                 (title, size, sample values, columns)
* ``GET  /wordlists/{name}``     one list's distinct values (a table's rows)
* ``POST /wordlists``            create or replace a custom list or table
* ``DELETE /wordlists/{name}``   delete a custom list
* ``POST /analyse``              split pasted/uploaded text into events
                                 (lines, JSON, CSV or multi-line with a
                                 breaker) and suggest the fields to abstract
* ``POST /preview``              render events from a builder config
* ``POST /packs``                write + register a pack (201, ``PackOut``)
* ``GET  /packs/{id}``           a builder pack's config, to reopen it
* ``PUT  /packs/{id}``           rebuild a builder pack in place
* ``GET  /drafts``                saved-but-unbuilt sessions (no contents)
* ``POST /drafts``                save a session that need not build yet
* ``GET  /drafts/{id}``           a draft's builder state, to reopen it
* ``PUT  /drafts/{id}``           save over a draft
* ``DELETE /drafts/{id}``         discard a draft

A draft holds the ORIGINAL sample (pseudonymising happens on build), so it is
stored encrypted, is never part of a pack, and is deleted by the build that
names it in ``draft_id``. Reading one needs operator: a viewer may see built
packs, whose identifiers are already stand-ins, but not the originals.

A builder pack is an ordinary local pack: its directory lives under
``PACK_UPLOAD_DIR`` (beside uploaded packs), it registers and lints through the
same path as ``POST /api/packs/upload``, and ``DELETE /api/packs/{id}`` removes
it. Bundles are content-addressed, so rebuilding never changes a run already
provisioned from the old contents. Every endpoint is POST/PUT or read-only, so
the auth middleware's role gate applies unchanged (writes need operator).

Custom word lists live in ``PACK_UPLOAD_DIR/.wordlists`` (the same persistent
volume). A pack copies the values it uses into its own ``samples/lists``, so
deleting a list never breaks a built pack; only rebuilding it in the builder
needs the list again.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import bundles, crypto, packbuilder, packsource, packupload, pseudonymkeys
from ..packbuilder import pseudonym
from ..config import get_settings
from ..db import get_db
from ..gitsync import local_pack_metadata
from ..models import Pack, PackDraft, utcnow
from ..schemas import PackOut

log = logging.getLogger("stoker.routes.packbuilder")

def _use_custom_lists():
    # type: () -> None
    settings = get_settings()
    packbuilder.set_custom_dir(os.path.join(settings.pack_upload_dir, ".wordlists"))
    packbuilder.set_max_fields(settings.pack_builder_max_fields)


router = APIRouter(prefix="/api/pack-builder", tags=["pack-builder"],
                   dependencies=[Depends(_use_custom_lists)])


class AnalyseRequest(BaseModel):
    text: str = Field(..., description="Pasted or uploaded events: one per line, a JSON array, CSV, "
                                       "or multi-line events")
    mode: str = Field("auto", description="auto | line | csv | regex")
    breaker: Optional[str] = Field(None, description="event breaker regex (mode=regex)")


class WordlistRequest(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    values: List[str] = Field(default_factory=list)
    columns: Optional[List[str]] = None


class PreviewRequest(BaseModel):
    config: Dict[str, Any]
    n: int = 20
    seed: Optional[int] = None
    # Events already pseudonymised by a previous build, echoed back by the UI
    # when it is previewing a reopened pack. Advisory only: the authoritative
    # list for a SAVE is read from the pack on disk, never from the client.
    already: Optional[List[str]] = None


class BuildRequest(BaseModel):
    config: Dict[str, Any]
    # The draft this build finishes; deleted once the pack is saved.
    draft_id: Optional[int] = None


# A draft is the builder's state as the editor holds it, so it is bounded by
# what the editor can produce (a 2 MB sample plus its fields), with headroom.
DRAFT_MAX_BYTES = 32 * 1024 * 1024


class DraftRequest(BaseModel):
    name: str = Field("", max_length=255)
    pack_id: Optional[int] = None
    # Deliberately NOT validated: a draft exists to hold a config that does
    # not build yet.
    config: Dict[str, Any]
    # Advisory, as in PreviewRequest: the events a previous build already
    # pseudonymised, so the reopened preview does not hash them again.
    already: Optional[List[str]] = None


class DraftOut(BaseModel):
    id: int
    name: str
    pack_id: Optional[int]
    created_by: Optional[str]
    created_at: Any
    updated_at: Any


def _bad(exc):
    # type: (Exception) -> HTTPException
    return HTTPException(status_code=422, detail={"error": "pack_builder_invalid", "detail": str(exc)})


def _pseudonym_key(db, cfg, actor=None, create=True):
    # type: (Session, Dict[str, Any], Optional[str], bool) -> Tuple[Optional[bytes], Optional[str]]
    """``(subkey, fingerprint)`` for a config that needs one, else ``(None, None)``.

    Resolved per request rather than cached, so a key created or restored
    between requests takes effect at once. A config with no pseudonym field
    never touches the key store, which is what keeps ``GET`` previews of
    ordinary packs free of any key side effect.
    """
    if not packbuilder.pseudonym_fields(cfg):
        return None, None
    try:
        sub, row = pseudonymkeys.subkey(db, actor=actor, create=create)
    except pseudonymkeys.PseudonymKeyError as exc:
        raise HTTPException(status_code=409, detail={
            "error": "pseudonym_key_unavailable", "detail": str(exc)})
    return sub, row.fingerprint


def _actor(request):
    # type: (Any) -> Optional[str]
    user = getattr(getattr(request, "state", None), "user", None)
    return getattr(user, "username", None) if user is not None else None


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
    out = {"name": name, "count": len(distinct), "values": distinct}  # type: Dict[str, Any]
    columns = packbuilder.table_columns().get(name)
    if columns:
        out["columns"] = columns
    return out


@router.post("/wordlists", status_code=201)
def save_wordlist(body: WordlistRequest):
    # type: (WordlistRequest) -> Any
    try:
        return packbuilder.save_custom_wordlist(body.name, body.values, body.title, body.description,
                                                body.columns)
    except packbuilder.BuilderError as exc:
        raise _bad(exc)


@router.delete("/wordlists/{name}", status_code=204)
def delete_wordlist(name: str):
    # type: (str) -> None
    try:
        packbuilder.delete_custom_wordlist(name)
    except packbuilder.BuilderError as exc:
        raise HTTPException(status_code=404 if "unknown" in str(exc) else 422, detail=str(exc))


@router.get("/pseudonym-key")
def pseudonym_key(db: Session = Depends(get_db)):
    # type: (Session) -> Any
    """Whether this instance has a pseudonym key, and its public fingerprint.

    Read-only in the strongest sense: it never creates one, so opening the
    builder cannot mint a key as a side effect of someone looking at a page.
    """
    return pseudonymkeys.describe(pseudonymkeys.peek(db))


class LookupRequest(BaseModel):
    values: List[str] = Field(..., description="real identifiers to look up", max_length=200)
    widen: Optional[Dict[str, Any]] = Field(
        default=None, description="the field's widen policy, or null for 'keep the format'")


@router.post("/pseudonym-lookup")
def pseudonym_lookup(body: LookupRequest, request: Request, db: Session = Depends(get_db)):
    # type: (LookupRequest, Request, Session) -> Any
    """What a real identifier became, so an operator can search for it.

    Without this an operator cannot act on their own test data: they know
    client 123 exists but not which stand-in to put in a Splunk search. It is a
    deliberate oracle, and no new privilege - any operator can already learn the
    same mapping by saving a pack - so the actor and the number of values are
    logged, and the key is never created here: a lookup before any pack has been
    built has nothing to look up.

    ``widen`` must match the field's own policy, because the policy is part of
    the identity: the same value at "keep the format" and at ``digits(15)`` has
    two different stand-ins.
    """
    actor = _actor(request)
    try:
        sub, row = pseudonymkeys.subkey(db, create=False)
    except pseudonymkeys.PseudonymKeyError as exc:
        raise HTTPException(status_code=409, detail={
            "error": "pseudonym_key_unavailable", "detail": str(exc)})
    widen = None
    if body.widen:
        try:
            widen = pseudonym.widen_format(body.widen.get("shape"), body.widen.get("length"))
        except pseudonym.PseudonymError as exc:
            raise _bad(exc)
    log.info("pseudonym lookup of %d value(s) by %s (key %s)",
             len(body.values), actor or "an operator", row.fingerprint)
    out = []
    for value in body.values:
        stand_in = pseudonym.pseudonym(value, sub, widen=widen)
        out.append({"value": value, "stand_in": stand_in,
                    "format": str(pseudonym.infer(value)) if stand_in else None})
    return {"key_fingerprint": row.fingerprint, "results": out}


@router.post("/analyse")
def analyse(body: AnalyseRequest):
    # type: (AnalyseRequest) -> Any
    try:
        split = packbuilder.split_input(body.text, body.mode, body.breaker)
    except packbuilder.BuilderError as exc:
        raise _bad(exc)
    result = packbuilder.analyse(split["events"], split["header"])
    result["event_list"] = split["events"]
    result["format"] = split["format"]
    result["breaker"] = split["breaker"]
    result["header"] = split["header"]
    return result


@router.post("/preview")
def preview(body: PreviewRequest, request: Request, db: Session = Depends(get_db)):
    # type: (PreviewRequest, Request, Session) -> Any
    cfg = _validated(body.config)
    # A preview of a pseudonym field shows the REAL stand-ins, because the
    # operator uploaded the originals and will see the same values in the saved
    # pack; a preview-only key would show values the pack does not contain and
    # would hide the cross-pack consistency the feature exists for.
    sub, _fp = _pseudonym_key(db, cfg, actor=_actor(request))
    result = packbuilder.render_preview(cfg, n=body.n, seed=body.seed, subkey=sub,
                                       already=body.already or None)
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


def _stage(cfg, subkey=None, fingerprint=None, already=None):
    # type: (Dict[str, Any], Optional[bytes], Optional[str], Optional[List[str]]) -> str
    """Write the pack into a hidden staging directory under the upload root."""
    staging = os.path.join(_upload_root(), ".builder-%s" % uuid.uuid4().hex)
    try:
        packbuilder.write_pack(cfg, staging, subkey=subkey, fingerprint=fingerprint,
                               already=already)
    except packbuilder.BuilderError as exc:
        # A refused build (an identifier left in the pack, say) is the
        # operator's to fix, so it goes back as a 422 the editor shows.
        shutil.rmtree(staging, ignore_errors=True)
        raise _bad(exc)
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
def create_pack(body: BuildRequest, request: Request, db: Session = Depends(get_db)):
    # type: (BuildRequest, Request, Session) -> Any
    cfg = _validated(body.config)
    if _name_taken(db, cfg["name"]):
        raise HTTPException(status_code=409, detail={
            "error": "pack_name_taken", "detail": "a pack named %r already exists" % cfg["name"]})
    sub, fp = _pseudonym_key(db, cfg, actor=_actor(request))
    staging = _stage(cfg, subkey=sub, fingerprint=fp)
    final = packupload._unique_pack_dir(_upload_root(), cfg["name"])
    os.rename(staging, final)
    pack = Pack(name=cfg["name"], source_path=final)
    try:
        _apply_lint(pack, final, cfg)
    except bundles.BundleError as exc:
        shutil.rmtree(final, ignore_errors=True)
        raise _bad(exc)
    db.add(pack)
    _drop_draft(db, body.draft_id)
    db.commit()
    db.refresh(pack)
    log.info("pack builder created %s (id=%s) -> %s lint=%s", pack.name, pack.id, final, pack.lint_status)
    # Mirror to the pack source when one is configured for writing. Best
    # effort by design: a bucket that refuses the write must not fail the save
    # and lose the operator's work.
    packsource.publish_pack(pack, get_settings())
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
def update_pack(pack_id: int, body: BuildRequest, request: Request,
                db: Session = Depends(get_db)):
    # type: (int, BuildRequest, Request, Session) -> Any
    pack = _builder_pack(db, pack_id)
    cfg = _validated(body.config)
    if _name_taken(db, cfg["name"], except_id=pack.id):
        raise HTTPException(status_code=409, detail={
            "error": "pack_name_taken", "detail": "a pack named %r already exists" % cfg["name"]})
    current = os.path.realpath(pack.source_path)
    sub, fp = _pseudonym_key(db, cfg, actor=_actor(request))
    # The events ON DISK are the ones a previous build pseudonymised, so they
    # must not be pseudonymised again (p(p(x)) would silently stop this pack
    # correlating with every other one). Read from disk, never from the client.
    previous = packbuilder.read_builder_config(current) or {}
    staging = _stage(cfg, subkey=sub, fingerprint=fp,
                     already=list(previous.get("events") or ()))
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
    _drop_draft(db, body.draft_id)
    db.commit()
    db.refresh(pack)
    log.info("pack builder rebuilt %s (id=%s) lint=%s", pack.name, pack.id, pack.lint_status)
    packsource.publish_pack(pack, get_settings())
    return pack


# ---- drafts ----

def _drop_draft(db, draft_id):
    # type: (Session, Optional[int]) -> None
    """Delete the draft a build came from, in the build's own transaction.

    The draft is the only place the originals were kept; once the pack holds
    the stand-ins there is no reason to keep them.
    """
    if draft_id is None:
        return
    draft = db.get(PackDraft, draft_id)
    if draft is not None:
        db.delete(draft)


def _may_read_originals(request):
    # type: (Request) -> None
    # Absent when auth is off or in the first-run window, as for every route.
    if getattr(request.state, "role", None) == "viewer":
        raise HTTPException(status_code=403, detail="operator role required")


def _draft(db, draft_id):
    # type: (Session, int) -> PackDraft
    draft = db.get(PackDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="unknown draft")
    return draft


def _draft_pack_id(db, pack_id):
    # type: (Session, Optional[int]) -> Optional[int]
    if pack_id is None:
        return None
    _builder_pack(db, pack_id)  # 404 unless it is a builder pack
    return pack_id


def _sealed(body):
    # type: (DraftRequest) -> str
    plain = json.dumps({"config": body.config, "already": body.already or []},
                       separators=(",", ":"))
    if len(plain) > DRAFT_MAX_BYTES:
        raise HTTPException(status_code=413, detail={
            "error": "draft_too_large",
            "detail": "this draft is %d MB, over the %d MB limit; trim the sample"
                      % (len(plain) // (1024 * 1024), DRAFT_MAX_BYTES // (1024 * 1024))})
    if getattr(get_settings(), "master_key_generated", False):
        # Saved under a key that dies with the process, the draft would be
        # unreadable after the next restart: refuse now, while the work is
        # still on screen, rather than lose it later.
        raise HTTPException(status_code=409, detail={
            "error": "draft_unencryptable",
            "detail": "drafts need a persistent master key; set STOKER_MASTER_KEY "
                      "(or STOKER_MASTER_KEY_FILE) so a saved draft survives a restart"})
    try:
        return crypto.encrypt(plain)
    except crypto.CryptoError as exc:
        raise HTTPException(status_code=409, detail={
            "error": "draft_unencryptable", "detail": str(exc)})


def _draft_out(draft):
    # type: (PackDraft) -> Dict[str, Any]
    return {"id": draft.id, "name": draft.name, "pack_id": draft.pack_id,
            "created_by": draft.created_by, "created_at": draft.created_at,
            "updated_at": draft.updated_at}


@router.get("/drafts", response_model=List[DraftOut])
def list_drafts(db: Session = Depends(get_db)):
    # type: (Session) -> Any
    rows = db.execute(select(PackDraft).order_by(PackDraft.updated_at.desc())).scalars().all()
    return [_draft_out(d) for d in rows]


@router.post("/drafts", response_model=DraftOut, status_code=201)
def create_draft(body: DraftRequest, request: Request, db: Session = Depends(get_db)):
    # type: (DraftRequest, Request, Session) -> Any
    draft = PackDraft(name=body.name.strip(), pack_id=_draft_pack_id(db, body.pack_id),
                      state_encrypted=_sealed(body),
                      created_by=getattr(request.state, "actor", None))
    db.add(draft)
    db.commit()
    db.refresh(draft)
    log.info("pack builder draft %s saved", draft.id)
    return _draft_out(draft)


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: int, request: Request, db: Session = Depends(get_db)):
    # type: (int, Request, Session) -> Any
    _may_read_originals(request)
    draft = _draft(db, draft_id)
    try:
        state = json.loads(crypto.decrypt(draft.state_encrypted))
    except crypto.CryptoError:
        # The master key changed since the draft was saved. Nothing can recover
        # it, so say so rather than failing with a 500.
        raise HTTPException(status_code=409, detail={
            "error": "draft_unreadable",
            "detail": "this draft was saved under a different master key and cannot be opened"})
    out = _draft_out(draft)
    out["config"] = state.get("config") or {}
    out["already"] = state.get("already") or []
    return out


@router.put("/drafts/{draft_id}", response_model=DraftOut)
def update_draft(draft_id: int, body: DraftRequest, db: Session = Depends(get_db)):
    # type: (int, DraftRequest, Session) -> Any
    draft = _draft(db, draft_id)
    draft.name = body.name.strip()
    draft.pack_id = _draft_pack_id(db, body.pack_id)
    draft.state_encrypted = _sealed(body)
    draft.updated_at = utcnow()
    db.commit()
    db.refresh(draft)
    return _draft_out(draft)


@router.delete("/drafts/{draft_id}", status_code=204)
def delete_draft(draft_id: int, db: Session = Depends(get_db)):
    # type: (int, Session) -> Any
    db.delete(_draft(db, draft_id))
    db.commit()
    return Response(status_code=204)
