"""The git-sync engine: clone/fetch, pack indexing, pinning.

Stack: subprocess ``git`` (present in the image; no new Python dependency).
Every subprocess call is built so credentials never appear on ``argv`` and never
reach a log:

* **pat** — the token is delivered through a throwaday 0600 ``GIT_ASKPASS``
  helper script and ``GIT_TERMINAL_PROMPT=0``; the URL on ``argv`` is the plain
  HTTPS URL with no userinfo.
* **deploy_key** — the private key is written to a 0600 temp file and passed via
  ``GIT_SSH_COMMAND="ssh -i <file> -o StrictHostKeyChecking=accept-new"``.
* **none** — plain HTTPS / ``file://`` with no credential material.

The temp credential material is created per invocation and deleted in a
``finally`` (the key file is also best-effort overwritten before unlink). Command
output is captured; on failure a :class:`GitSyncError` carrying a scrubbed,
secret-free message is raised (the caller stores it in ``repo.sync_error``).

Indexing walks the clone for **pack roots** (a directory holding
``default/eventgen.conf`` at the repo root or under ``packs/*/``), synthesises a
``pack.yaml`` when the pack lacks one (flagging the pack ``verified=False``),
lints via :func:`server.bundles.lint_pack`, enforces the custom-code default-deny
(``bin/`` and ``generator =`` stanzas are rejected unless ``repo.trusted_code``)
and rejects ``file`` / ``mvfile`` token replacement paths that escape the pack
root. Each pack upserts a :class:`~server.models.Pack` row keyed on
``(repo_id, name)`` with ``indexed_sha`` = the repo head SHA.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import bundles, crypto
from ..bundles import CONF_RELPATH
from ..config import Settings, get_settings
from ..models import Pack, Repo, utcnow

log = logging.getLogger("stoker.gitsync")

# Timeouts (seconds) for the git subprocesses. Clone can be slow on a big repo;
# resolve/rev-parse are near-instant.
_CLONE_TIMEOUT_S = 300
_GIT_TIMEOUT_S = 120

# A ref reaches `git fetch/checkout <ref>` and `clone --branch <ref>` as an
# operand that git would parse as an option if it began with '-' (git accepts no
# `--` guard before a ref). Only these characters are ever valid in a branch/tag
# name or SHA, so anything else is rejected before it reaches git.
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _require_safe_ref(ref):
    # type: (str) -> None
    if not ref or ref.startswith("-") or not _SAFE_REF_RE.match(ref):
        raise GitSyncError(
            "unsafe git ref %r: only letters, digits, '.', '_', '/', '-' are "
            "allowed and it must not start with '-'" % ref)

# eventgen token stanza keys we security-check for path escape. A token line is
# ``token.<n>.replacement = <path>`` guarded by ``token.<n>.replacementType =
# file`` (or ``mvfile``). We reject a replacement path that escapes the pack root.
_FILE_TOKEN_TYPES = ("file", "mvfile")
_TOKEN_TYPE_RE = re.compile(r"^token\.(\d+)\.replacementType$")
_TOKEN_REPL_RE = re.compile(r"^token\.(\d+)\.replacement$")
# 'generator =' custom-code stanza key (default-deny unless trusted_code).
_GENERATOR_KEY = "generator"

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class GitSyncError(Exception):
    """A repo could not be cloned/fetched or a pack failed the code policy.

    The message is always secret-free (git output is scrubbed of any embedded
    credential before it is attached), so it is safe to store in
    ``repo.sync_error`` and return to an operator.
    """


# --------------------------------------------------------------------------- #
# Clone / fetch
# --------------------------------------------------------------------------- #

def clone_or_fetch(repo, dest_root, settings=None):
    # type: (Repo, str, Optional[Settings]) -> Tuple[str, str]
    """Shallow-clone (or fetch if the clone exists) ``repo`` at its default ref.

    The clone lives at ``{dest_root}/{repo.id}``. On first sync it is created
    with ``git clone --depth 1``; on subsequent syncs the existing clone is
    fetched. ``repo.default_ref`` is resolved to a concrete commit SHA which is
    returned alongside the clone directory.

    Returns ``(clone_dir, head_sha)``. Raises :class:`GitSyncError` on any git
    failure (message scrubbed of credentials).
    """
    if settings is None:
        settings = get_settings()
    if not repo.id:
        raise GitSyncError("repo must be persisted (have an id) before sync")

    clone_dir = os.path.join(dest_root, str(repo.id))
    os.makedirs(dest_root, exist_ok=True)

    ref = (repo.default_ref or "main").strip() or "main"
    _require_safe_ref(ref)
    secret = _decrypt_secret(repo)

    with _git_auth_env(repo, secret) as env:
        if _is_git_clone(clone_dir):
            _fetch(clone_dir, ref, env)
        else:
            # A stale non-clone directory (partial previous run) is cleared first.
            if os.path.exists(clone_dir):
                shutil.rmtree(clone_dir, ignore_errors=True)
            _clone(repo.url, clone_dir, ref, env)
        head_sha = _resolve_ref_sha(clone_dir, ref, env)

    if not _SHA_RE.match(head_sha):
        raise GitSyncError(
            "could not resolve ref %r to a commit SHA in %s" % (ref, repo.url))
    return clone_dir, head_sha


def _clone(url, clone_dir, ref, env):
    # type: (str, str, str, Dict[str, str]) -> None
    """``git clone --depth 1`` the URL at ``ref`` into ``clone_dir``.

    A branch/tag ref is passed via ``--branch`` for a shallow single-ref clone.
    When ``ref`` is a raw SHA (``--branch`` rejects those) we clone the default
    head shallowly, then fetch+checkout the SHA.
    """
    # `--` ends option parsing before the url positional so a url beginning with
    # '-' can never be read as a git option. `git fetch`/`checkout` do not accept
    # `--` before a ref (it would be treated as a pathspec), so the ref is guarded
    # by charset validation instead (_require_safe_ref below + the boundary
    # validator); together these close both option-injection sinks.
    if _looks_like_sha(ref):
        _run_git(["clone", "--depth", "1", "--no-tags", "--", url, clone_dir], env=env,
                 timeout=_CLONE_TIMEOUT_S, redact=(url,))
        # Deepen just enough to reach the pinned SHA, then check it out.
        _run_git(["-C", clone_dir, "fetch", "--depth", "1", "origin", ref], env=env,
                 timeout=_CLONE_TIMEOUT_S, redact=(url,))
        _run_git(["-C", clone_dir, "checkout", "--detach", ref], env=env,
                 timeout=_GIT_TIMEOUT_S, redact=(url,))
    else:
        _run_git(
            ["clone", "--depth", "1", "--no-tags", "--branch", ref, "--", url, clone_dir],
            env=env, timeout=_CLONE_TIMEOUT_S, redact=(url,))


def _fetch(clone_dir, ref, env):
    # type: (str, str, Dict[str, str]) -> None
    """Fetch ``ref`` into an existing clone and move the working tree onto it.

    Works for a branch, tag or SHA. After the fetch we hard-reset the detached
    HEAD to ``FETCH_HEAD`` so the tree reflects the current tip of ``ref``
    (indexing reads the working tree).
    """
    _run_git(["-C", clone_dir, "fetch", "--depth", "1", "--no-tags", "origin", ref],
             env=env, timeout=_CLONE_TIMEOUT_S)
    _run_git(["-C", clone_dir, "checkout", "--detach", "FETCH_HEAD"],
             env=env, timeout=_GIT_TIMEOUT_S)
    _run_git(["-C", clone_dir, "reset", "--hard", "FETCH_HEAD"],
             env=env, timeout=_GIT_TIMEOUT_S)


def _resolve_ref_sha(clone_dir, ref, env):
    # type: (str, str, Dict[str, str]) -> str
    """Resolve the checked-out HEAD to a concrete 40-char commit SHA."""
    out = _run_git(["-C", clone_dir, "rev-parse", "HEAD"], env=env, timeout=_GIT_TIMEOUT_S)
    return out.strip()


# --------------------------------------------------------------------------- #
# Pinned checkout for the bundle builder
# --------------------------------------------------------------------------- #

def resolve_pack_dir(repo, pack, settings=None):
    # type: (Repo, Pack, Optional[Settings]) -> str
    """Return the local directory of ``pack`` at ``pack.indexed_sha``.

    Reuses the repo clone: the pinned SHA is materialised into a per-SHA worktree
    under ``{REPO_CLONE_DIR}/{repo.id}-sha/{sha}`` (created once, reused after),
    and the pack's sub-path within the repo is returned. This gives the bundle
    builder a byte-identical tree even after the branch has moved (pinning).

    Raises :class:`GitSyncError` if the pack is not indexed, its clone is missing
    or the SHA cannot be checked out.
    """
    if settings is None:
        settings = get_settings()
    if not pack.indexed_sha:
        raise GitSyncError("pack %s has no indexed SHA (not synced from a repo)" % pack.id)
    if pack.repo_id != repo.id:
        raise GitSyncError("pack %s does not belong to repo %s" % (pack.id, repo.id))

    clone_dir = os.path.join(settings.repo_clone_dir, str(repo.id))
    if not _is_git_clone(clone_dir):
        raise GitSyncError(
            "repo %s clone is missing; run a sync before building bundles" % repo.id)

    sha = pack.indexed_sha
    checkout_root = os.path.join(settings.repo_clone_dir, "%s-sha" % repo.id, sha)
    rel = _pack_relpath(pack)
    pack_dir = os.path.join(checkout_root, rel) if rel else checkout_root

    if os.path.isfile(os.path.join(pack_dir, CONF_RELPATH)):
        return pack_dir  # already materialised

    secret = _decrypt_secret(repo)
    os.makedirs(os.path.dirname(checkout_root), exist_ok=True)
    with _git_auth_env(repo, secret) as env:
        # Ensure the object is present (a shallow clone may not have it), then
        # extract the exact tree for the SHA with `git archive` (no worktree
        # bookkeeping, immutable snapshot).
        _ensure_sha_present(clone_dir, sha, repo.url, env)
        _extract_tree(clone_dir, sha, checkout_root, env)

    if not os.path.isfile(os.path.join(pack_dir, CONF_RELPATH)):
        raise GitSyncError(
            "pack path %r not found at SHA %s in repo %s" % (rel, sha[:12], repo.id))
    return pack_dir


def _ensure_sha_present(clone_dir, sha, url, env):
    # type: (str, str, str, Dict[str, str]) -> None
    """Make sure ``sha`` is a reachable object in the clone (fetch it if not)."""
    try:
        _run_git(["-C", clone_dir, "cat-file", "-e", "%s^{commit}" % sha],
                 env=env, timeout=_GIT_TIMEOUT_S)
        return
    except GitSyncError:
        pass
    _run_git(["-C", clone_dir, "fetch", "--depth", "1", "origin", sha],
             env=env, timeout=_CLONE_TIMEOUT_S, redact=(url,))


def _extract_tree(clone_dir, sha, dest, env):
    # type: (str, str, str, Dict[str, str]) -> None
    """Extract the tree at ``sha`` into ``dest`` via ``git archive`` + tar.

    The archive is buffered fully (a pack tree is small) and opened seekably so
    the traversal-safety filter can pre-scan members without a stream-seek error.
    Atomic: extract into a temp sibling then rename, so a concurrent reader never
    sees a half-populated snapshot.
    """
    import io
    import tarfile

    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)

    # `git archive` to a captured buffer (deterministic tree snapshot for the SHA).
    out = _run_git_bytes(
        ["-C", clone_dir, "archive", "--format=tar", sha], env=env,
        timeout=_GIT_TIMEOUT_S)

    tmp = tempfile.mkdtemp(prefix=".sha-", dir=parent)
    try:
        with tarfile.open(fileobj=io.BytesIO(out), mode="r:") as tar:
            # filter="data" (py3.12+) rejects traversal/links/devices in the
            # stdlib; our _safe_tar_members is a second, explicit guard.
            tar.extractall(tmp, members=_safe_tar_members(tar, tmp), filter="data")
        if os.path.exists(dest):
            shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
        tmp = None  # renamed; do not clean
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _safe_tar_members(tar, dest):
    # type: (Any, str) -> List[Any]
    """Yield only tar members that stay inside ``dest`` (no traversal/symlink)."""
    dest_abs = os.path.abspath(dest)
    safe = []
    for member in tar.getmembers():
        target = os.path.abspath(os.path.join(dest, member.name))
        if target != dest_abs and not target.startswith(dest_abs + os.sep):
            log.warning("git archive member %r escapes dest; skipped", member.name)
            continue
        if member.issym() or member.islnk():
            log.warning("git archive member %r is a link; skipped", member.name)
            continue
        safe.append(member)
    return safe


# --------------------------------------------------------------------------- #
# Pack indexing
# --------------------------------------------------------------------------- #

def index_packs(db, repo, clone_dir, head_sha, settings=None):
    # type: (Session, Repo, str, str, Optional[Settings]) -> Dict[str, int]
    """Walk ``clone_dir`` for pack roots and upsert a Pack row for each.

    For every pack root (``default/eventgen.conf`` at the repo root or under
    ``packs/*/``):

    * synthesise a ``pack.yaml`` (measuring ``bytes_per_event`` from the samples)
      when one is absent, and flag the pack ``verified=False``;
    * lint via :func:`server.bundles.lint_pack`;
    * enforce the custom-code default-deny: a pack carrying ``bin/`` or a
      ``generator =`` stanza fails lint unless ``repo.trusted_code`` (its errors
      record why); the ``bin/`` tree is not part of the bundle regardless;
    * reject ``file`` / ``mvfile`` token replacement paths that escape the pack
      root;
    * upsert the Pack row keyed on ``(repo_id, name)`` with ``indexed_sha`` =
      ``head_sha``, ``lint_status`` / ``lint_errors_json`` and ``verified``.

    Returns ``{"packs_indexed": n, "lint_failures": m}``.
    """
    if settings is None:
        settings = get_settings()

    roots = _find_pack_roots(clone_dir)
    packs_indexed = 0
    lint_failures = 0

    for pack_dir in roots:
        rel = os.path.relpath(pack_dir, clone_dir)
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        name = _pack_name(pack_dir, clone_dir, repo)

        had_yaml = os.path.isfile(os.path.join(pack_dir, "pack.yaml"))
        synthesised = False
        if not had_yaml:
            _synthesise_pack_yaml(pack_dir)
            synthesised = True

        lint = bundles.lint_pack(pack_dir)
        errors = list(lint.errors)

        # Custom-code default-deny (unless the repo is flagged trusted_code).
        code_errors = _custom_code_errors(pack_dir)
        trusted = bool(repo.trusted_code)
        if code_errors and not trusted:
            errors.extend(code_errors)

        # file/mvfile token paths that escape the pack root are always rejected.
        errors.extend(_token_path_escape_errors(pack_dir))

        ok = len(errors) == 0
        # A synthesised pack.yaml means the pack is unverified (no author-declared
        # metadata); an author-supplied pack.yaml on a clean pack is verified.
        verified = ok and had_yaml and not synthesised
        lint_status = "ok" if ok else "error"
        if not ok:
            lint_failures += 1

        _upsert_pack(
            db, repo=repo, name=name, rel=rel, pack_dir=pack_dir, head_sha=head_sha,
            lint=lint, errors=errors, lint_status=lint_status, verified=verified,
            trusted=trusted)
        packs_indexed += 1
        log.info("indexed pack %r (repo=%s sha=%s) lint=%s verified=%s%s",
                 name, repo.id, head_sha[:12], lint_status, verified,
                 " [synthesised pack.yaml]" if synthesised else "")

    db.flush()
    return {"packs_indexed": packs_indexed, "lint_failures": lint_failures}


def _upsert_pack(db, repo, name, rel, pack_dir, head_sha, lint, errors,
                 lint_status, verified, trusted):
    # type: (Session, Repo, str, str, str, str, Any, List[str], str, bool, bool) -> Pack
    """Insert or update the Pack row for ``(repo_id, name)``."""
    existing = db.execute(
        select(Pack).where(Pack.repo_id == repo.id, Pack.name == name)
    ).scalars().first()

    pack = existing or Pack(name=name)
    pack.name = name
    pack.repo_id = repo.id
    pack.source_path = pack_dir
    pack.description = _pack_description(pack_dir)
    pack.tags_json = _pack_tags(pack_dir)
    pack.engines_json = lint.engines
    pack.sourcetypes_json = lint.sourcetypes
    pack.stanza_count = lint.stanza_count
    pack.est_bytes_per_event = lint.est_bytes_per_event
    pack.declared_per_day_gb = lint.declared_per_day_gb
    pack.verified = verified
    pack.lint_status = lint_status
    pack.lint_errors_json = errors
    pack.indexed_sha = head_sha
    if existing is None:
        db.add(pack)
    return pack


# --------------------------------------------------------------------------- #
# Full sync
# --------------------------------------------------------------------------- #

def sync_repo(db, repo, settings=None):
    # type: (Session, Repo, Optional[Settings]) -> Dict[str, Any]
    """Clone/fetch ``repo`` then index its packs; update the repo row.

    On success: ``repo.head_sha`` / ``repo.last_synced_at`` are stamped and
    ``repo.sync_error`` cleared. On failure ``repo.sync_error`` records a
    secret-free message and the error is re-raised. Returns
    ``{"head_sha", "packs_indexed", "lint_failures"}``.
    """
    if settings is None:
        settings = get_settings()

    try:
        clone_dir, head_sha = clone_or_fetch(repo, settings.repo_clone_dir, settings)
        counts = index_packs(db, repo, clone_dir, head_sha, settings)
    except GitSyncError as exc:
        repo.sync_error = _scrub(str(exc), ())
        repo.last_synced_at = utcnow()
        db.flush()
        log.warning("sync failed for repo %s: %s", repo.id, repo.sync_error)
        raise
    except Exception as exc:  # unexpected: still record, never leak a secret
        repo.sync_error = "sync failed: %s" % type(exc).__name__
        repo.last_synced_at = utcnow()
        db.flush()
        log.exception("unexpected sync error for repo %s", repo.id)
        raise GitSyncError(repo.sync_error)

    repo.head_sha = head_sha
    repo.last_synced_at = utcnow()
    repo.sync_error = None
    db.flush()
    log.info("synced repo %s at %s: %d packs (%d lint failures)",
             repo.id, head_sha[:12], counts["packs_indexed"], counts["lint_failures"])
    return {
        "head_sha": head_sha,
        "packs_indexed": counts["packs_indexed"],
        "lint_failures": counts["lint_failures"],
    }


# --------------------------------------------------------------------------- #
# Pack discovery + metadata
# --------------------------------------------------------------------------- #

def _find_pack_roots(clone_dir):
    # type: (str) -> List[str]
    """Pack roots in a clone: the repo root, then each ``packs/*/`` directory.

    A pack root is a directory containing ``default/eventgen.conf``. The design
    accepts the pack at the repo root or one level under ``packs/``. Sorted for
    deterministic indexing.
    """
    roots = []  # type: List[str]
    if os.path.isfile(os.path.join(clone_dir, CONF_RELPATH)):
        roots.append(clone_dir)
    packs_parent = os.path.join(clone_dir, "packs")
    if os.path.isdir(packs_parent):
        for entry in sorted(os.listdir(packs_parent)):
            cand = os.path.join(packs_parent, entry)
            if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, CONF_RELPATH)):
                roots.append(cand)
    return roots


def _pack_name(pack_dir, clone_dir, repo=None):
    # type: (str, str, Optional[Repo]) -> str
    """A stable pack name: the pack.yaml name, else the directory basename.

    A repo-root pack (no ``packs/<name>``) has no meaningful directory name (the
    clone dir is the numeric repo id), so it falls back to the repo URL slug.
    Uniqueness within a repo is enforced by the ``(repo_id, name)`` upsert key.
    """
    yaml_name = _yaml_scalar(pack_dir, "name")
    if yaml_name:
        return str(yaml_name)
    if os.path.abspath(pack_dir) == os.path.abspath(clone_dir):
        # Repo-root pack: derive a name from the repo URL rather than the numeric
        # clone-dir id.
        if repo is not None and repo.url:
            return _repo_slug(repo.url)
        return "root"
    return os.path.basename(os.path.normpath(pack_dir))


def _repo_slug(url):
    # type: (str) -> str
    """A short pack name from a repo URL: the trailing path segment sans ``.git``.

    ``https://github.com/livehybrid/splunk-sample-packs.git`` -> ``splunk-sample-packs``;
    ``git@host:org/repo.git`` -> ``repo``; a ``file:///.../src`` path -> ``src``.
    """
    text = url.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    # split on both / and : (scp-style) and take the last non-empty segment.
    for sep in ("/", ":"):
        if sep in text:
            text = text.rsplit(sep, 1)[-1]
    return text or "root"


def _pack_relpath(pack):
    # type: (Pack) -> str
    """The pack's path within its repo, recovered from its indexed source_path.

    ``resolve_pack_dir`` needs the sub-path (``""`` for a repo-root pack, else
    ``packs/<name>``). We reconstruct it from the stored ``source_path`` relative
    to the repo clone dir; if that is unavailable we fall back to ``packs/<name>``
    when the name does not look like a bare repo root.
    """
    # source_path was ``{clone}/{rel}``; derive rel from the trailing components.
    src = pack.source_path or ""
    marker = os.sep + "packs" + os.sep
    idx = src.rfind(marker)
    if idx != -1:
        return src[idx + 1:].replace(os.sep, "/")  # "packs/<name>[/...]"
    return ""


def _pack_description(pack_dir):
    # type: (str) -> Optional[str]
    val = _yaml_scalar(pack_dir, "description")
    return str(val) if val is not None else None


def _pack_tags(pack_dir):
    # type: (str) -> Optional[Any]
    """Tags from pack.yaml if present (the subset parser drops list-shaped values,
    so this is usually ``None``); returns a list or ``None``.
    """
    doc = _read_pack_yaml(pack_dir)
    tags = doc.get("tags") if isinstance(doc, dict) else None
    if isinstance(tags, list):
        return tags
    if isinstance(tags, str) and tags:
        return [t.strip() for t in tags.split(",") if t.strip()]
    return None


# --------------------------------------------------------------------------- #
# pack.yaml synthesis
# --------------------------------------------------------------------------- #

def _synthesise_pack_yaml(pack_dir):
    # type: (str) -> None
    """Write a minimal ``pack.yaml`` for a repo that ships only eventgen.conf.

    Measures ``bytes_per_event`` from the sample files (a synthesised pack always
    gets a measured estimate, per the design) and records the discovered engines.
    The pack is flagged unverified by the caller; this file exists only so the
    bundle carries an estimate and the pack has consistent metadata.
    """
    lint = bundles.lint_pack(pack_dir)
    name = os.path.basename(os.path.normpath(pack_dir))
    lines = [
        "# synthesised by stoker git-sync (unverified: no author pack.yaml)",
        "name: %s" % name,
        "description: \"synthesised pack (no author pack.yaml)\"",
        "engine: %s" % (lint.engines[0] if lint.engines else "eventgen"),
    ]
    bpe = lint.est_bytes_per_event
    if bpe is not None:
        lines.append("estimates:")
        lines.append("  bytes_per_event: %s" % _fmt_num(bpe))
        if lint.declared_per_day_gb is not None:
            lines.append("  per_day_gb: %s" % _fmt_num(lint.declared_per_day_gb))
    body = "\n".join(lines) + "\n"
    try:
        with open(os.path.join(pack_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        # Non-fatal: indexing can proceed without the file (estimate re-measured
        # by the linter), but a synthesised pack normally has one.
        log.warning("could not write synthesised pack.yaml in %s: %s", pack_dir, exc)


def _fmt_num(value):
    # type: (float) -> str
    if float(value).is_integer():
        return str(int(value))
    return repr(round(float(value), 2))


# --------------------------------------------------------------------------- #
# Custom-code default-deny + token path escape
# --------------------------------------------------------------------------- #

def _custom_code_errors(pack_dir):
    # type: (str) -> List[str]
    """Reasons this pack is custom code (blocked unless the repo is trusted_code).

    Two triggers per the design: a ``bin/`` directory (arbitrary Python eventgen
    would import and run inside a worker), and any ``generator =`` stanza key
    (a custom generator plugin). Returns a list of human-readable reasons; empty
    means the pack carries no custom code.
    """
    reasons = []  # type: List[str]
    bin_dir = os.path.join(pack_dir, "bin")
    if os.path.isdir(bin_dir) and _dir_has_files(bin_dir):
        reasons.append(
            "custom code: pack contains a bin/ directory (default-deny; flag the "
            "repo trusted_code to allow, or remove bin/)")
    gen_stanzas = _generator_stanzas(pack_dir)
    if gen_stanzas:
        reasons.append(
            "custom code: 'generator =' stanza(s) %s (default-deny; flag the repo "
            "trusted_code to allow)" % ", ".join("[%s]" % s for s in gen_stanzas))
    return reasons


def _generator_stanzas(pack_dir):
    # type: (str) -> List[str]
    """Stanzas whose keys include a non-default ``generator`` (custom plugin).

    eventgen's default generator is ``default``; a stanza declaring any other
    ``generator = <name>`` is a custom-code plugin reference and is treated as
    custom code.
    """
    parser = _read_conf(pack_dir)
    if parser is None:
        return []
    hits = []  # type: List[str]
    for section in parser.sections():
        if section.lower() in ("global", "default"):
            continue
        val = parser.get(section, _GENERATOR_KEY, fallback=None)
        if val is not None and val.strip() and val.strip().lower() != "default":
            hits.append(section)
    return hits


def _token_path_escape_errors(pack_dir):
    # type: (str) -> List[str]
    """Reject ``file`` / ``mvfile`` token replacement paths that escape the pack.

    A token pair ``token.<n>.replacementType = file|mvfile`` +
    ``token.<n>.replacement = <path>`` reads ``<path>`` at generation time. A path
    that resolves outside the pack root (``..`` traversal or an absolute path
    landing elsewhere) is rejected: a malicious pack must not read arbitrary host
    files. Paths inside the pack are allowed.
    """
    parser = _read_conf(pack_dir)
    if parser is None:
        return []
    pack_abs = os.path.abspath(pack_dir)
    errors = []  # type: List[str]
    for section in parser.sections():
        if section.lower() in ("global", "default"):
            continue
        types = {}  # type: Dict[str, str]
        repls = {}  # type: Dict[str, str]
        for key in parser.options(section):
            mt = _TOKEN_TYPE_RE.match(key)
            if mt:
                types[mt.group(1)] = (parser.get(section, key) or "").strip().lower()
                continue
            mr = _TOKEN_REPL_RE.match(key)
            if mr:
                repls[mr.group(1)] = parser.get(section, key) or ""
        for idx, ttype in types.items():
            if ttype not in _FILE_TOKEN_TYPES:
                continue
            path = repls.get(idx)
            if not path:
                continue
            if _path_escapes(pack_abs, path.strip()):
                errors.append(
                    "stanza [%s] token.%s: %s path %r escapes the pack root "
                    "(rejected)" % (section, idx, ttype, path.strip()))
    return errors


def _path_escapes(pack_abs, path):
    # type: (str, str) -> bool
    """True when ``path`` (as eventgen would read it) resolves outside the pack.

    Relative paths are resolved against the pack root; absolute paths are checked
    as-is. A path equal to or under the pack root is safe.
    """
    if not path:
        return False
    # realpath (not abspath) so a symlinked segment inside the pack that points
    # outside is resolved before the containment test.
    pack_real = os.path.realpath(pack_abs)
    if os.path.isabs(path):
        resolved = os.path.realpath(path)
    else:
        resolved = os.path.realpath(os.path.join(pack_real, path))
    return resolved != pack_real and not resolved.startswith(pack_real + os.sep)


# --------------------------------------------------------------------------- #
# Small conf / yaml helpers (reuse the bundles parser semantics)
# --------------------------------------------------------------------------- #

def _read_conf(pack_dir):
    # type: (str) -> Optional[Any]
    """Parse ``default/eventgen.conf`` with the bundles-compatible parser."""
    import configparser

    conf_path = os.path.join(pack_dir, CONF_RELPATH)
    if not os.path.isfile(conf_path):
        return None
    parser = configparser.RawConfigParser(
        delimiters=("=",), strict=False, allow_no_value=True, interpolation=None)
    parser.optionxform = str
    try:
        parser.read(conf_path, encoding="utf-8")
    except configparser.Error:
        return None
    return parser


def _read_pack_yaml(pack_dir):
    # type: (str) -> Dict[str, Any]
    path = os.path.join(pack_dir, "pack.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        from stoker_agent.bundle import parse_pack_yaml
    except Exception:  # pragma: no cover - worker not importable
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return parse_pack_yaml(fh.read())
    except OSError:
        return {}


def _yaml_scalar(pack_dir, key):
    # type: (str, str) -> Optional[Any]
    doc = _read_pack_yaml(pack_dir)
    if isinstance(doc, dict):
        val = doc.get(key)
        if not isinstance(val, (dict, list)):
            return val
    return None


def _dir_has_files(path):
    # type: (str) -> bool
    for _root, _dirs, files in os.walk(path):
        if files:
            return True
    return False


# --------------------------------------------------------------------------- #
# git subprocess plumbing + credential handling (no secret on argv or in logs)
# --------------------------------------------------------------------------- #

class _GitAuthEnv:
    """Context manager building the git subprocess env for a repo's auth kind.

    Yields the env dict. Creates and (in ``__exit__``) deletes any temp
    credential material: a 0600 ``GIT_ASKPASS`` helper for a PAT, or a 0600 key
    file for a deploy key. The secret is never placed on a command line.
    """

    def __init__(self, repo, secret):
        # type: (Repo, Optional[str]) -> None
        self._repo = repo
        self._secret = secret
        self._tmpdir = None  # type: Optional[str]

    def __enter__(self):
        # type: () -> Dict[str, str]
        env = dict(os.environ)
        # Never let git block on an interactive credential/host prompt.
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Restrict transports to the real remote schemes: this drops git's
        # ext:: transport (which runs an arbitrary command) so a crafted url can
        # never reach it, even if validation upstream were bypassed.
        env["GIT_ALLOW_PROTOCOL"] = "https:ssh:git:file"
        env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
        # Deterministic identity so any implicit commit never touches host config.
        env.setdefault("GIT_AUTHOR_NAME", "stoker")
        env.setdefault("GIT_AUTHOR_EMAIL", "stoker@localhost")
        env.setdefault("GIT_COMMITTER_NAME", "stoker")
        env.setdefault("GIT_COMMITTER_EMAIL", "stoker@localhost")

        kind = (self._repo.auth_kind or "none").strip()
        if kind == "pat" and self._secret:
            self._tmpdir = tempfile.mkdtemp(prefix="stoker-git-")
            askpass = os.path.join(self._tmpdir, "askpass.sh")
            secret_file = os.path.join(self._tmpdir, "pat")
            # The token lives in a 0600 file; the helper echoes username then
            # password. GitHub PATs authenticate as the token in the password
            # slot with any username; we use a fixed placeholder username.
            _write_private(secret_file, self._secret)
            # Built by concatenation (no %-format): git calls this helper with a
            # prompt like "Username for ...". We answer a fixed username and, for
            # anything else (the password prompt), cat the 0600 token file. The
            # token never appears in the script body or on any argv.
            script = (
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "*[Uu]sername*) printf 'x-access-token' ;;\n"
                "*) cat " + _sh_quote(secret_file) + " ;;\n"
                "esac\n"
            )
            _write_private(askpass, script, executable=True)
            env["GIT_ASKPASS"] = askpass
            # Belt and braces: also stop core.askpass/SSH_ASKPASS interfering.
            env.pop("SSH_ASKPASS", None)
        elif kind == "deploy_key" and self._secret:
            self._tmpdir = tempfile.mkdtemp(prefix="stoker-git-")
            key_file = os.path.join(self._tmpdir, "id")
            key_material = self._secret
            if not key_material.endswith("\n"):
                key_material += "\n"
            _write_private(key_file, key_material)
            env["GIT_SSH_COMMAND"] = (
                "ssh -i " + _sh_quote(key_file) + " -o IdentitiesOnly=yes"
                " -o StrictHostKeyChecking=accept-new"
                " -o PasswordAuthentication=no"
            )
        return env

    def __exit__(self, *exc):
        # type: (Any) -> None
        if self._tmpdir and os.path.isdir(self._tmpdir):
            _shred_dir(self._tmpdir)
        self._tmpdir = None


def _git_auth_env(repo, secret):
    # type: (Repo, Optional[str]) -> _GitAuthEnv
    return _GitAuthEnv(repo, secret)


def _decrypt_secret(repo):
    # type: (Repo) -> Optional[str]
    """Decrypt the repo credential, or ``None`` for a public/none-auth repo.

    Never logs the secret; a decryption failure raises a secret-free
    :class:`GitSyncError`.
    """
    kind = (repo.auth_kind or "none").strip()
    if kind == "none" or not repo.secret_encrypted:
        return None
    try:
        return crypto.decrypt(repo.secret_encrypted)
    except crypto.CryptoError as exc:
        raise GitSyncError(
            "cannot decrypt repo credential (auth_kind=%s): %s" % (kind, exc))


def _run_git(args, env, timeout, redact=()):
    # type: (List[str], Dict[str, str], int, Tuple[str, ...]) -> str
    """Run ``git <args>`` capturing output; raise a scrubbed error on non-zero.

    ``redact`` lists strings (e.g. the repo URL) to strip from any error text
    defensively. The secret is never on ``argv`` so it cannot appear here, but we
    still scrub to be safe. Returns stdout as text.
    """
    cmd = ["git"] + list(args)
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise GitSyncError("git executable not found on PATH")
    except subprocess.TimeoutExpired:
        raise GitSyncError("git %s timed out after %ds" % (args[0], timeout))
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        stdout = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
        msg = (stderr or stdout or "git exited %d" % proc.returncode).strip()
        raise GitSyncError("git %s failed: %s" % (args[0], _scrub(msg, redact)))
    return proc.stdout.decode("utf-8", "replace")


def _run_git_bytes(args, env, timeout, redact=()):
    # type: (List[str], Dict[str, str], int, Tuple[str, ...]) -> bytes
    """Run ``git <args>`` capturing raw stdout bytes (e.g. for ``git archive``)."""
    cmd = ["git"] + list(args)
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise GitSyncError("git executable not found on PATH")
    except subprocess.TimeoutExpired:
        raise GitSyncError("git %s timed out after %ds" % (args[0], timeout))
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        raise GitSyncError("git %s failed: %s" % (args[0], _scrub(stderr, redact)))
    return proc.stdout


def _is_git_clone(path):
    # type: (str) -> bool
    """True when ``path`` is an existing git working clone."""
    return os.path.isdir(os.path.join(path, ".git")) or (
        os.path.isdir(path) and os.path.isfile(os.path.join(path, "HEAD"))
    )


def _looks_like_sha(ref):
    # type: (str) -> bool
    return bool(_SHA_RE.match(ref.strip())) and len(ref.strip()) >= 7


def _scrub(text, redact):
    # type: (str, Tuple[str, ...]) -> str
    """Strip embedded credentials and any ``redact`` strings from a message.

    Removes ``userinfo@`` from URLs (``https://user:tok@host`` -> ``https://host``)
    and blanks any explicit redact strings. Keeps the message short.
    """
    out = text
    out = re.sub(r"(https?://)[^/@\s]+@", r"\1", out)
    for token in redact:
        if token:
            out = out.replace(token, "<repo-url>")
    out = out.strip()
    if len(out) > 500:
        out = out[:500] + " ..."
    return out


def _write_private(path, content, executable=False):
    # type: (str, str, bool) -> None
    """Write ``content`` to ``path`` with 0600 (0700 when executable) perms."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    mode = 0o700 if executable else 0o600
    fd = os.open(path, flags, mode)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    # Ensure perms even if a prior file existed with a wider mode.
    os.chmod(path, mode)


def _shred_dir(path):
    # type: (str) -> None
    """Best-effort overwrite of any credential files, then remove the dir."""
    try:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    size = os.path.getsize(fp)
                    with open(fp, "wb") as fh:
                        fh.write(b"\0" * size)
                except OSError:
                    pass
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _sh_quote(value):
    # type: (str) -> str
    """Single-quote a path for embedding in an sh command string."""
    return "'" + value.replace("'", "'\\''") + "'"


__all__ = [
    "GitSyncError",
    "clone_or_fetch",
    "index_packs",
    "resolve_pack_dir",
    "sync_repo",
]
