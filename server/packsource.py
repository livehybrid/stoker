"""Load packs from an object store or a git repo named by the environment.

Two env vars, for the same disposable-deployment problem
:mod:`server.configio` solves for targets and specs: an instance that is torn
down and rebuilt should come back with its packs.

``STOKER_PACK_REPOS``
    Comma-separated git URLs, registered as pack repos at boot. Deliberately a
    thin wrapper: it upserts a :class:`~server.models.Repo` row and lets the
    existing git-sync machinery do everything else, so these packs get the same
    lint, indexing, ref pinning, webhooks and custom-code gating as a repo added
    through the UI. Building a second, parallel GitHub path would have meant a
    second set of all of that.

``STOKER_PACK_SOURCE``
    An object-store prefix (``s3://bucket/prefix``) or a local directory, each
    holding one ``.tar.gz`` per pack. The archive format is the one Download and
    Upload pack already speak, so a pack exported from one instance can be
    dropped in a bucket and picked up by another with nothing in between.

``STOKER_PACK_SOURCE_WRITE=1``
    Also push packs back: a pack built or uploaded here is exported to the same
    prefix. Off by default and deliberately a separate switch, because the
    common case is a shared read-only mirror several instances pull from, and
    one of them quietly overwriting it would be hard to notice and worse to
    diagnose.

boto3 is imported lazily, so the dependency is only required by a deployment
that actually points at S3.
"""
from __future__ import annotations

import logging
import os
import posixpath
import shutil
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import bundles, gitsync, packexport, packupload
from .models import Pack, Repo

log = logging.getLogger("stoker.packsource")

REPOS_ENV = "STOKER_PACK_REPOS"
SOURCE_ENV = "STOKER_PACK_SOURCE"
WRITE_ENV = "STOKER_PACK_SOURCE_WRITE"

ARCHIVE_SUFFIX = ".tar.gz"


class PackSourceError(Exception):
    """A pack source that cannot be read or written."""


# --------------------------------------------------------------------------- #
# Git repos from the environment
# --------------------------------------------------------------------------- #

def register_repos_from_env(db, env=None):
    # type: (Session, Optional[Dict[str, str]]) -> List[str]
    """Upsert the repos named by ``STOKER_PACK_REPOS``; return the URLs added.

    Idempotent, and it only ever creates: an operator who later changes a repo's
    ref or credential in the UI should not have that overwritten on the next
    restart by a value baked into the deployment.
    """
    env = env if env is not None else os.environ
    raw = (env.get(REPOS_ENV) or "").strip()
    if not raw:
        return []
    added = []
    for url in [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]:
        if db.scalar(select(Repo).where(Repo.url == url)) is not None:
            continue
        db.add(Repo(url=url))
        added.append(url)
        log.info("registered pack repo from %s: %s", REPOS_ENV, url)
    if added:
        db.flush()
    return added


# --------------------------------------------------------------------------- #
# Object store / directory
# --------------------------------------------------------------------------- #

class Store:
    """The handful of operations a pack source needs, over S3 or a directory."""

    def list_archives(self):
        # type: () -> List[str]
        raise NotImplementedError

    def get(self, key):
        # type: (str) -> bytes
        raise NotImplementedError

    def put(self, key, data):
        # type: (str, bytes) -> None
        raise NotImplementedError


class DirectoryStore(Store):
    """A local directory. Useful for a mounted volume, and for the tests."""

    def __init__(self, path):
        # type: (str) -> None
        self.path = path

    def list_archives(self):
        # type: () -> List[str]
        if not os.path.isdir(self.path):
            return []
        return sorted(n for n in os.listdir(self.path)
                      if n.endswith(ARCHIVE_SUFFIX))

    def get(self, key):
        # type: (str) -> bytes
        with open(os.path.join(self.path, key), "rb") as fh:
            return fh.read()

    def put(self, key, data):
        # type: (str, bytes) -> None
        os.makedirs(self.path, exist_ok=True)
        tmp = os.path.join(self.path, key + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(self.path, key))


class S3Store(Store):
    """An ``s3://bucket/prefix``.

    Credentials come from the usual boto3 chain (instance role, env, profile),
    so nothing about them belongs in Stoker's own configuration.
    """

    def __init__(self, bucket, prefix, client=None):
        # type: (str, str, Optional[Any]) -> None
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client

    @property
    def client(self):
        # type: () -> Any
        if self._client is None:
            try:
                import boto3  # noqa: PLC0415 - optional dependency, by design
            except ImportError:
                raise PackSourceError(
                    "%s names an s3:// source but boto3 is not installed in this "
                    "image" % SOURCE_ENV)
            self._client = boto3.client("s3")
        return self._client

    def _key(self, name):
        # type: (str) -> str
        return posixpath.join(self.prefix, name) if self.prefix else name

    def list_archives(self):
        # type: () -> List[str]
        names = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket}
            if self.prefix:
                kwargs["Prefix"] = self.prefix + "/"
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                if not key.endswith(ARCHIVE_SUFFIX):
                    continue
                # Only this prefix's own level: a nested "archive/" of old
                # versions should not be imported as live packs.
                rel = key[len(self.prefix) + 1:] if self.prefix else key
                if "/" in rel:
                    continue
                names.append(rel)
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return sorted(names)

    def get(self, key):
        # type: (str) -> bytes
        return self.client.get_object(
            Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def put(self, key, data):
        # type: (str, bytes) -> None
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data,
                               ContentType="application/gzip")


def open_store(source):
    # type: (str) -> Store
    """A :class:`Store` for ``s3://bucket/prefix`` or a directory path."""
    source = source.strip()
    if not source:
        raise PackSourceError("empty pack source")
    parsed = urlparse(source)
    if parsed.scheme == "s3":
        if not parsed.netloc:
            raise PackSourceError("pack source %r has no bucket" % source)
        return S3Store(parsed.netloc, parsed.path)
    if parsed.scheme in ("", "file"):
        return DirectoryStore(parsed.path if parsed.scheme == "file" else source)
    raise PackSourceError(
        "pack source %r: only s3:// and local directories are supported" % source)


def store_from_env(env=None):
    # type: (Optional[Dict[str, str]]) -> Optional[Store]
    env = env if env is not None else os.environ
    source = (env.get(SOURCE_ENV) or "").strip()
    return open_store(source) if source else None


def writes_enabled(env=None):
    # type: (Optional[Dict[str, str]]) -> bool
    env = env if env is not None else os.environ
    return (env.get(WRITE_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Pull
# --------------------------------------------------------------------------- #

def archive_name(pack_name):
    # type: (str) -> str
    return "%s%s" % (packexport.safe_basename(pack_name), ARCHIVE_SUFFIX)


def sync_from_store(db, store, settings, limits=None):
    # type: (Session, Store, Any, Optional[Any]) -> Dict[str, Any]
    """Import every archive in ``store`` that this instance does not have.

    Matched by pack name, so re-running imports only what is new. An archive
    that fails to extract or lint is reported and skipped rather than aborting
    the sync, because one bad object in a shared bucket must not stop an
    instance from starting with the rest of its packs.
    """
    limits = limits or packupload.UploadLimits.from_settings(settings)
    report = {"imported": [], "skipped": [], "errors": []}  # type: Dict[str, Any]
    try:
        names = store.list_archives()
    except PackSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - any client error, reported not raised
        report["errors"].append("cannot list the pack source: %s" % exc)
        return report

    existing = {p.name for p in db.scalars(select(Pack)).all()}
    for name in names:
        pack_name = name[: -len(ARCHIVE_SUFFIX)]
        if pack_name in existing:
            report["skipped"].append(pack_name)
            continue
        try:
            data = store.get(name)
            pack_dir = packupload.store_uploaded_pack(
                data, settings.pack_upload_dir, limits, name_hint=pack_name)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append("%s: %s" % (name, exc))
            continue
        try:
            meta = gitsync.local_pack_metadata(pack_dir)
            lint = bundles.lint_pack(pack_dir)
        except bundles.BundleError as exc:
            shutil.rmtree(pack_dir, ignore_errors=True)
            report["errors"].append("%s: metadata unreadable: %s" % (name, exc))
            continue
        pack = Pack(
            name=meta["name"] or pack_name,
            source_path=pack_dir,
            description=meta["description"],
            tags_json=meta["tags"] or [],
            engines_json=lint.engines,
            builder_config_json=lint.metricgen,
            sourcetypes_json=lint.sourcetypes,
            stanza_count=lint.stanza_count,
            est_bytes_per_event=lint.est_bytes_per_event,
            declared_per_day_gb=lint.declared_per_day_gb,
            verified=lint.ok,
            lint_status="ok" if lint.ok else "error",
            lint_errors_json=lint.errors,
        )
        db.add(pack)
        report["imported"].append(pack.name)
        log.info("imported pack %r from the pack source (lint=%s)",
                 pack.name, pack.lint_status)
    if report["imported"]:
        db.flush()
    return report


# --------------------------------------------------------------------------- #
# Push
# --------------------------------------------------------------------------- #

def publish_pack(pack, settings, store=None, env=None):
    # type: (Pack, Any, Optional[Store], Optional[Dict[str, str]]) -> Optional[str]
    """Push a pack to the configured source. Returns the key, or None.

    A no-op unless both a source and ``STOKER_PACK_SOURCE_WRITE`` are set.
    Never raises: failing to mirror a pack must not fail the save that produced
    it, or an operator loses work because a bucket policy changed.
    """
    if store is None:
        if not writes_enabled(env):
            return None
        store = store_from_env(env)
    if store is None:
        return None
    try:
        result = packexport.export_pack_bytes(
            pack.source_path, pack.name, settings=settings)
        # The same filename Download produces, so a key in the bucket and a
        # file on an operator's disk are the same artefact under the same name.
        key = result.filename
        store.put(key, result.data)
    except Exception as exc:  # noqa: BLE001 - mirroring is best-effort
        log.warning("could not publish pack %r to the pack source: %s",
                    pack.name, exc)
        return None
    log.info("published pack %r to the pack source as %s", pack.name, key)
    return key


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #

def load_from_env(db, settings, env=None):
    # type: (Session, Any, Optional[Dict[str, str]]) -> Optional[Dict[str, Any]]
    """Apply both env vars at boot. Never raises, for the ConfigMap-typo reason."""
    env = env if env is not None else os.environ
    out = {}  # type: Dict[str, Any]
    try:
        repos = register_repos_from_env(db, env)
        if repos:
            out["repos"] = repos
    except Exception as exc:  # noqa: BLE001
        log.exception("could not register pack repos from %s: %s", REPOS_ENV, exc)
        out["repos_error"] = str(exc)
    try:
        store = store_from_env(env)
        if store is not None:
            out["packs"] = sync_from_store(db, store, settings)
    except PackSourceError as exc:
        log.error("pack source unusable: %s", exc)
        out["packs_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        log.exception("pack source sync failed: %s", exc)
        out["packs_error"] = str(exc)
    return out or None
