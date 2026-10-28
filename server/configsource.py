"""A shared copy of the configuration in S3 (or a mounted directory).

:mod:`server.configio` made an instance's configuration a JSON document, and
``STOKER_CONFIG_IMPORT`` restores one at boot, but only from a mounted file or
inline JSON: getting the file there was still somebody's job. This puts the
document in the same kind of place packs already live, so a fresh or rebuilt
instance comes up configured and an engineer starting a test does not re-enter
targets and specs first.

``STOKER_CONFIG_SOURCE``
    ``s3://bucket/prefix`` or a directory. The document is one object,
    ``stoker-config.json``, under it. Pulled at boot (after the pack source, so
    specs can find their packs) and on demand from the Configuration page.

``STOKER_CONFIG_SOURCE_WRITE=1``
    Also push: whenever this instance's configuration changes, the document is
    written back. Off by default and a separate switch for the same reason as
    ``STOKER_PACK_SOURCE_WRITE``: several instances reading one copy is the
    common case, and one of them quietly overwriting it is hard to notice.

``STOKER_CONFIG_SOURCE_SSE`` / ``STOKER_CONFIG_SOURCE_SSE_KMS_KEY_ID``
    Server-side encryption headers for the upload, as for the pack source (see
    :func:`server.packsource._sse_args` for why a bucket policy can need them).
    Unset, the pack source's settings apply, since the two usually share a
    bucket.

**Pull is the existing import**: an idempotent upsert by name that never
deletes, so a pull never removes anything an operator made here.

**Push is the whole document, secrets included as ciphertext**, so a restore
needs the same ``STOKER_MASTER_KEY``. Changes are noticed by comparing the
document with the last one pulled or pushed, every few seconds, rather than by
hooking each route that edits a target, repo or spec: a new route that forgets
the hook would silently stop backing up, and this cannot.

**A row the pull could not apply is carried, not dropped.** The usual case is a
spec whose pack has not been indexed yet, because its git repo syncs after
boot. Pushing this instance's view verbatim would then delete that spec from
the shared copy on the first unrelated edit, which is data loss on a path
nobody would see. Rows a pull skipped are kept in every push until this
instance has them itself.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import threading
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import configio, packsource
from .models import Repo, Spec, Target

log = logging.getLogger("stoker.configsource")

SOURCE_ENV = "STOKER_CONFIG_SOURCE"
WRITE_ENV = "STOKER_CONFIG_SOURCE_WRITE"
SSE_ENV = "STOKER_CONFIG_SOURCE_SSE"
SSE_KEY_ENV = "STOKER_CONFIG_SOURCE_SSE_KMS_KEY_ID"

OBJECT_NAME = "stoker-config.json"

#: How often the background loop compares the configuration with the copy.
PUSH_INTERVAL_S = 15.0

# Each section of the document and the field that names a row in it.
_SECTIONS = (("targets", "name", Target.name),
             ("repos", "url", Repo.url),
             ("specs", "name", Spec.name))


class ConfigSourceError(Exception):
    """A config source that cannot be read or written."""


_lock = threading.Lock()
_state = {
    # sha256 of the document as last pulled or pushed; None until either.
    "synced": None,
    # Rows the last pull could not apply, by section then natural key.
    "carried": {},
    "last_pull": None,
    "last_push": None,
}  # type: Dict[str, Any]


def _now():
    # type: () -> str
    return (datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0).isoformat())


def _reset_for_tests():
    # type: () -> None
    with _lock:
        _state.update(synced=None, carried={}, last_pull=None, last_push=None)


# --------------------------------------------------------------------------- #
# Where it lives
# --------------------------------------------------------------------------- #

def store_from_env(env=None):
    # type: (Optional[Dict[str, str]]) -> Optional[packsource.Store]
    env = env if env is not None else os.environ
    source = (env.get(SOURCE_ENV) or "").strip()
    if not source:
        return None
    try:
        return packsource.open_store(source, label="config source", env_name=SOURCE_ENV)
    except packsource.PackSourceError as exc:
        raise ConfigSourceError(str(exc))


def writes_enabled(env=None):
    # type: (Optional[Dict[str, str]]) -> bool
    env = env if env is not None else os.environ
    return (env.get(WRITE_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _sse(env=None):
    # type: (Optional[Dict[str, str]]) -> Dict[str, str]
    env = env if env is not None else os.environ
    if (env.get(SSE_ENV) or "").strip():
        return packsource._sse_args(env, SSE_ENV, SSE_KEY_ENV)
    return packsource._sse_args(env)


def describe(env=None):
    # type: (Optional[Dict[str, str]]) -> Dict[str, Any]
    """What the config source is and what it last did, for the UI.

    ``location`` names a bucket and prefix and never a credential: boto3 takes
    those from its own chain.
    """
    env = env if env is not None else os.environ
    source = (env.get(SOURCE_ENV) or "").strip()
    with _lock:
        out = {
            "configured": bool(source), "writable": False, "kind": None,
            "location": source or None, "object": OBJECT_NAME, "error": None,
            "last_pull": _state["last_pull"], "last_push": _state["last_push"],
            "carried": sum(len(rows) for rows in _state["carried"].values()),
        }  # type: Dict[str, Any]
    if not source:
        return out
    try:
        store = store_from_env(env)
    except ConfigSourceError as exc:
        out["error"] = str(exc)
        return out
    out["kind"] = "s3" if isinstance(store, packsource.S3Store) else "directory"
    out["writable"] = writes_enabled(env)
    return out


def _fetch(store):
    # type: (packsource.Store) -> Optional[bytes]
    """The document's bytes, or None when nothing has been pushed yet."""
    try:
        return store.get(OBJECT_NAME)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - botocore's error types are optional
        code = str(((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code", ""))
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise ConfigSourceError("could not read %s: %s" % (OBJECT_NAME, exc))


def _digest(doc):
    # type: (Dict[str, Any]) -> str
    """A hash of what the document says, not of when or in what order.

    Rows are sorted by their natural key, so a copy that differs from this
    instance's export only in row order (carried rows are appended) does not
    count as a change and trigger a push.
    """
    body = {k: v for k, v in doc.items() if k != "exported_at"}
    for section, key, _column in _SECTIONS:
        if isinstance(body.get(section), list):
            body[section] = sorted(
                body[section],
                key=lambda row: str(row.get(key) if isinstance(row, dict) else row))
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Pull
# --------------------------------------------------------------------------- #

def pull(db, settings=None, store=None):
    # type: (Session, Optional[Any], Optional[packsource.Store]) -> Dict[str, Any]
    """Apply the shared copy to this instance. The caller commits.

    Returns the import report plus ``found``: False when the source holds no
    document yet, which is the normal state of a new bucket and not an error.
    """
    store = store if store is not None else store_from_env()
    if store is None:
        raise ConfigSourceError("%s is not set" % SOURCE_ENV)
    data = _fetch(store)
    if data is None:
        report = {"found": False, "targets": 0, "repos": 0, "specs": 0,
                  "skipped": [], "warnings": []}  # type: Dict[str, Any]
        with _lock:
            _state["last_pull"] = {"at": _now(), "ok": True, "found": False}
        return report
    try:
        doc = configio.load_document(data.decode("utf-8"))
    except (configio.ConfigError, UnicodeDecodeError) as exc:
        with _lock:
            _state["last_pull"] = {"at": _now(), "ok": False, "error": str(exc)}
        raise ConfigSourceError("%s is not a usable config: %s" % (OBJECT_NAME, exc))

    report = configio.import_config(db, doc, settings=settings)
    report["found"] = True

    carried = {}  # type: Dict[str, Dict[str, Any]]
    for section, key, column in _SECTIONS:
        for row in doc.get(section) or []:
            name = (row.get(key) or "").strip() if isinstance(row, dict) else ""
            if name and db.scalar(select(column).where(column == name)) is None:
                carried.setdefault(section, {})[name] = row
    with _lock:
        _state["carried"] = carried
        _state["synced"] = _digest(doc)
        _state["last_pull"] = {
            "at": _now(), "ok": True, "found": True,
            "created": {s: report[s] for s in ("targets", "repos", "specs")},
            "skipped": len(report["skipped"]),
        }
    return report


def load_from_env(db, settings=None, env=None):
    # type: (Session, Optional[Any], Optional[Dict[str, str]]) -> Optional[Dict[str, Any]]
    """Pull at boot when ``STOKER_CONFIG_SOURCE`` is set. Never raises.

    An unreachable bucket must degrade to "this instance starts unconfigured",
    not "this instance does not start".
    """
    env = env if env is not None else os.environ
    if not (env.get(SOURCE_ENV) or "").strip():
        return None
    try:
        report = pull(db, settings, store=store_from_env(env))
    except Exception as exc:  # noqa: BLE001 - startup must survive anything here
        log.error("config source pull failed: %s", exc)
        with _lock:
            _state["last_pull"] = {"at": _now(), "ok": False, "error": str(exc)}
        return {"error": str(exc)}
    if not report["found"]:
        log.info("config source: no %s yet, nothing to pull", OBJECT_NAME)
    else:
        log.info("config source: %d target(s), %d repo(s), %d spec(s) created%s",
                 report["targets"], report["repos"], report["specs"],
                 "; %d skipped" % len(report["skipped"]) if report["skipped"] else "")
        for line in report["warnings"] + report["skipped"]:
            log.warning("config source: %s", line)
    return report


# --------------------------------------------------------------------------- #
# Push
# --------------------------------------------------------------------------- #

def document(db, settings=None):
    # type: (Session, Optional[Any]) -> Dict[str, Any]
    """This instance's configuration plus any rows the last pull carried."""
    doc = configio.export_config(db, include_secrets=True, settings=settings)
    with _lock:
        carried = {s: dict(rows) for s, rows in _state["carried"].items()}
    for section, key, _column in _SECTIONS:
        rows = carried.get(section) or {}
        if not rows:
            continue
        present = {(row.get(key) or "") for row in doc[section]}
        kept = {}
        for name, row in rows.items():
            if name in present:
                continue  # this instance has it now; its own version wins.
            doc[section].append(row)
            kept[name] = row
        with _lock:
            if kept:
                _state["carried"][section] = kept
            else:
                _state["carried"].pop(section, None)
    return doc


def push(db, settings=None, store=None, env=None):
    # type: (Session, Optional[Any], Optional[packsource.Store], Optional[Dict[str, str]]) -> Dict[str, Any]
    """Write this instance's configuration to the source, now."""
    store = store if store is not None else store_from_env(env)
    if store is None:
        raise ConfigSourceError("%s is not set" % SOURCE_ENV)
    doc = document(db, settings)
    body = json.dumps(doc, indent=2, default=str) + "\n"
    try:
        store.put(OBJECT_NAME, body.encode("utf-8"),
                  content_type="application/json", sse=_sse(env))
    except Exception as exc:  # noqa: BLE001 - reported on the page, not raised as a 500
        with _lock:
            _state["last_push"] = {"at": _now(), "ok": False, "error": str(exc)}
        raise ConfigSourceError("could not write %s: %s" % (OBJECT_NAME, exc))
    counts = {s: len(doc[s]) for s in ("targets", "repos", "specs")}
    with _lock:
        _state["synced"] = _digest(doc)
        _state["last_push"] = {"at": _now(), "ok": True, "counts": counts}
    log.info("config source: pushed %d target(s), %d repo(s), %d spec(s)",
             counts["targets"], counts["repos"], counts["specs"])
    return {"pushed": True, **counts}


def maybe_push(db, settings=None, env=None):
    # type: (Session, Optional[Any], Optional[Dict[str, str]]) -> bool
    """Push if writes are on and the configuration changed since the last sync."""
    if not writes_enabled(env):
        return False
    store = store_from_env(env)
    if store is None:
        return False
    doc = document(db, settings)
    with _lock:
        unchanged = _state["synced"] == _digest(doc)
    if unchanged:
        return False
    push(db, settings, store=store, env=env)
    return True


__all__ = [
    "ConfigSourceError", "OBJECT_NAME", "PUSH_INTERVAL_S", "SOURCE_ENV", "WRITE_ENV",
    "describe", "document", "load_from_env", "maybe_push", "pull", "push",
    "store_from_env", "writes_enabled",
]
