"""Export and restore a Stoker instance's configuration as JSON.

The point is a disposable deployment: configure an instance, export the JSON,
then tear the environment down including its PersistentVolumeClaims and bring it
back from the file. So the export has to describe configuration an operator
authored (targets, pack repos, job specs) and nothing a run produced (runs,
leases, metrics, packs themselves), and it has to restore onto an empty database
without a single id lining up with the old one.

**Nothing is addressed by id.** A restored instance allocates its own primary
keys, so every cross-reference travels by natural key instead: a spec names its
target and its pack rather than pointing at row 7. That also makes the file
diffable and hand-editable, which is what you want from something living in git
beside the chart that mounts it.

**Secrets travel as ciphertext.** A target's HEC token and a repo's credential
are Fernet ciphertext under ``STOKER_MASTER_KEY``; they are exported exactly as
stored. Restoring therefore needs the same master key, which in Kubernetes is a
Secret that survives precisely the PVC teardown this exists for. The alternative
would be writing live HEC tokens in clear into a file destined for a ConfigMap
or a git repo, which is not a trade worth making. The export records a
fingerprint of the key so a restore under the wrong one says so immediately
rather than leaving every target silently unable to authenticate. Pass
``include_secrets=False`` for a sanitised copy to share or commit; restoring it
recreates the configuration with the secrets blank, ready to be filled in.

Import is an idempotent upsert, so re-applying the same file changes nothing and
a half-finished restore can simply be run again.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Pack, Repo, Spec, Target

log = logging.getLogger("stoker.configio")

CONFIG_VERSION = 1

#: Env var naming a JSON file (or holding the JSON itself) to restore at boot.
IMPORT_ENV = "STOKER_CONFIG_IMPORT"

# Fields copied verbatim per entity. Explicit lists rather than reflection: a
# new column should not silently start travelling between instances, and a
# secret must never be added to an export by accident.
TARGET_FIELDS = ["name", "hec_url", "default_index", "verify_tls", "env_tag",
                 "max_concurrent_gb_day"]
TARGET_SECRETS = ["token_encrypted"]

REPO_FIELDS = ["url", "auth_kind", "default_ref", "trusted_code"]
REPO_SECRETS = ["secret_encrypted", "webhook_secret"]

SPEC_FIELDS = ["name", "ref", "engine", "overrides_json", "rate_mode", "rate_value",
               "interval_s", "workers", "duration_s", "fleet", "strict_release",
               "driver_opts_json", "eventgen_impl", "fast_envelope", "rate_shape",
               "extra_pack_ids_json"]


class ConfigError(Exception):
    """A config document that cannot be read or applied."""


def master_key_fingerprint(settings=None):
    # type: (Optional[Any]) -> Optional[str]
    """8 hex characters identifying the master key, never the key itself.

    Enough to tell an operator "this export was made under a different key", and
    useless to anyone who obtains the file.
    """
    key = os.environ.get("STOKER_MASTER_KEY") or ""
    if not key and settings is not None:
        key = getattr(settings, "master_key", "") or ""
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def export_config(db, include_secrets=True, settings=None):
    # type: (Session, bool, Optional[Any]) -> Dict[str, Any]
    """The instance's configuration as a JSON-serialisable document."""
    doc = {
        "stoker_config_version": CONFIG_VERSION,
        "exported_at": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0).isoformat(),
        "includes_secrets": bool(include_secrets),
        "master_key_fingerprint": master_key_fingerprint(settings),
        "targets": [],
        "repos": [],
        "specs": [],
    }  # type: Dict[str, Any]

    for target in db.scalars(select(Target).order_by(Target.name)).all():
        row = {f: getattr(target, f) for f in TARGET_FIELDS}
        if include_secrets:
            row.update({f: getattr(target, f) for f in TARGET_SECRETS})
        doc["targets"].append(row)

    for repo in db.scalars(select(Repo).order_by(Repo.url)).all():
        row = {f: getattr(repo, f) for f in REPO_FIELDS}
        if include_secrets:
            row.update({f: getattr(repo, f) for f in REPO_SECRETS})
        doc["repos"].append(row)

    for spec in db.scalars(select(Spec).order_by(Spec.name)).all():
        row = {f: getattr(spec, f) for f in SPEC_FIELDS}
        # By name, never by id: a restored instance allocates its own.
        row["target"] = spec.target.name if spec.target is not None else None
        row["pack"] = spec.pack.name if spec.pack is not None else None
        doc["specs"].append(row)

    return doc


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

def load_document(source):
    # type: (str) -> Dict[str, Any]
    """Parse a config document from a file path or from inline JSON.

    Both spellings are accepted because both are natural in a container: a
    mounted file for a ConfigMap or volume, inline JSON for a plain env var.
    """
    text = source
    # Anything opening with a JSON structural character is inline JSON, even if
    # it turns out to be the wrong shape: reporting an array as "not a readable
    # file" sends the operator looking for a mount that was never the problem.
    if source.lstrip()[:1] not in ("{", "["):
        if not os.path.isfile(source):
            raise ConfigError("config import source %r is neither a readable "
                              "file nor inline JSON" % source)
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ConfigError("config import is not valid JSON: %s" % exc)
    if not isinstance(doc, dict):
        raise ConfigError("config import must be a JSON object")
    version = doc.get("stoker_config_version")
    if version is not None and int(version) > CONFIG_VERSION:
        raise ConfigError(
            "config was exported by a newer Stoker (version %s, this instance "
            "understands %s)" % (version, CONFIG_VERSION))
    return doc


def import_config(db, doc, settings=None):
    # type: (Session, Dict[str, Any], Optional[Any]) -> Dict[str, Any]
    """Apply a config document. Idempotent: re-running changes nothing.

    Returns a report rather than raising on a partial apply, because the common
    partial case is benign and self-correcting: a spec names a pack that has not
    been indexed yet (its repo was created by this very import and syncs
    afterwards), and the next import picks it up. Failing the whole restore over
    that would leave the operator with nothing.
    """
    report = {"targets": 0, "repos": 0, "specs": 0, "skipped": [], "warnings": []}  # type: Dict[str, Any]

    want = doc.get("master_key_fingerprint")
    have = master_key_fingerprint(settings)
    if want and have and want != have:
        # Not fatal: the configuration still restores and is still useful, but
        # every encrypted secret in it is unreadable, so say so loudly once
        # rather than let it surface as a pile of auth failures later.
        report["warnings"].append(
            "this config was exported under master key %s but this instance "
            "uses %s: HEC tokens and repo credentials in it cannot be "
            "decrypted and must be re-entered" % (want, have))

    for row in doc.get("targets") or []:
        name = (row.get("name") or "").strip()
        if not name:
            report["skipped"].append("a target with no name")
            continue
        target = db.scalar(select(Target).where(Target.name == name))
        if target is None:
            target = Target(name=name, hec_url=row.get("hec_url") or "")
            db.add(target)
            report["targets"] += 1
        for field in TARGET_FIELDS + TARGET_SECRETS:
            if field in row and field != "name":
                setattr(target, field, row[field])

    for row in doc.get("repos") or []:
        url = (row.get("url") or "").strip()
        if not url:
            report["skipped"].append("a repo with no url")
            continue
        repo = db.scalar(select(Repo).where(Repo.url == url))
        if repo is None:
            repo = Repo(url=url)
            db.add(repo)
            report["repos"] += 1
        for field in REPO_FIELDS + REPO_SECRETS:
            if field in row and field != "url":
                setattr(repo, field, row[field])

    db.flush()

    for row in doc.get("specs") or []:
        name = (row.get("name") or "").strip()
        if not name:
            report["skipped"].append("a spec with no name")
            continue
        target_name, pack_name = row.get("target"), row.get("pack")
        target = (db.scalar(select(Target).where(Target.name == target_name))
                  if target_name else None)
        pack = (db.scalar(select(Pack).where(Pack.name == pack_name))
                if pack_name else None)
        if target_name and target is None:
            report["skipped"].append(
                "spec %r: target %r is not registered" % (name, target_name))
            continue
        if pack_name and pack is None:
            # The usual cause is ordering: the pack's repo was created by this
            # same import and has not synced yet. Re-running after the sync
            # picks it up, which is why import is idempotent.
            report["skipped"].append(
                "spec %r: pack %r is not indexed yet (re-run the import after "
                "the repo has synced)" % (name, pack_name))
            continue
        spec = db.scalar(select(Spec).where(Spec.name == name))
        if spec is None:
            spec = Spec(name=name, pack_id=pack.id if pack else None,
                        target_id=target.id if target else None)
            db.add(spec)
            report["specs"] += 1
        spec.pack_id = pack.id if pack else None
        spec.target_id = target.id if target else None
        for field in SPEC_FIELDS:
            if field in row and field != "name":
                setattr(spec, field, row[field])

    db.flush()
    return report


def import_from_env(db, settings=None, env=None):
    # type: (Session, Optional[Any], Optional[Dict[str, str]]) -> Optional[Dict[str, Any]]
    """Apply ``STOKER_CONFIG_IMPORT`` if it is set. Returns the report, or None.

    Never raises: a malformed config file must not stop the control plane from
    starting, or a typo in a ConfigMap takes the whole deployment down with it
    and leaves no way in to fix the typo.
    """
    env = env if env is not None else os.environ
    source = (env.get(IMPORT_ENV) or "").strip()
    if not source:
        return None
    try:
        doc = load_document(source)
        report = import_config(db, doc, settings=settings)
    except ConfigError as exc:
        log.error("config import failed: %s", exc)
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - startup must survive anything here
        log.exception("config import failed unexpectedly: %s", exc)
        return {"error": str(exc)}
    log.info("config import: %d target(s), %d repo(s), %d spec(s) created%s",
             report["targets"], report["repos"], report["specs"],
             "; %d skipped" % len(report["skipped"]) if report["skipped"] else "")
    for line in report["warnings"] + report["skipped"]:
        log.warning("config import: %s", line)
    return report
