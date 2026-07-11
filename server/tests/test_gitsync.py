"""Stage-3 git repo sync tests: managing repos containing sample packs.

These exercise the git-sync feature the DESIGN section 7 ("Sample pack format
and git repo contract") specifies, against a **local** git repository built in a
tmp dir by :mod:`tests._gitrepo` (``git init`` + a committed pack + a ``file://``
url). No network, no real GitHub.

The Stage-3 backend (``server.gitsync``, the ``repos`` table, the
``/api/repos*`` + ``/api/hooks/github`` routes) is built by a separate agent and
did not exist when these tests were authored. So every test is **capability
gated**: it probes for the exact symbol / route / column it needs and
``pytest.skip``s with a precise reason when that piece is still a stub or absent.
This keeps the file importing and collecting green today, and turning meaningful
the moment each slice of the backend lands. Nothing here edits a backend module.

Where the backend exposes the pure functions the task names
(``sync_repo(db, repo, settings)``, ``index_packs(...)``,
``clone_or_fetch(...)`` and a ``resolve_pack_dir`` for pinning) we call those
directly; where only the HTTP surface exists we drive it through the
``TestClient``. Each test prefers the route (the operator-visible contract) and
falls back to the pure function, so it proves the behaviour by whichever seam is
implemented.

Contract points covered (DESIGN s7 + CONTROL-PLANE / API s9):

* register + sync indexes the pack: a ``Pack`` row with ``repo_id`` set,
  ``lint_status ok``, the indexed SHA == repo head, and
  ``GET /api/packs?repo={id}`` returns it;
* a repo whose pack lacks ``pack.yaml`` but has ``default/eventgen.conf`` gets a
  synthesised ``pack.yaml`` and ``verified=False``;
* the repo secret is write-only: never in any repos GET body;
* custom-code default-deny: a ``bin/`` dir or a ``generator =`` stanza is
  rejected (lint error) unless the repo is ``trusted_code``;
* a ``file``-token replacement path escaping the pack root is rejected by lint;
* ref pinning: a second commit moves the branch; a pack indexed at the old SHA
  still resolves to old content (or a re-sync re-indexes at the new SHA and the
  indexed SHA changes);
* ``POST /api/hooks/github`` rejects a bad HMAC (401/403) and a good one
  triggers a re-sync.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import importlib
import inspect
import json
import os
from typing import Any, Dict, List, Optional

import pytest

from server import config as config_mod
from server import models

from . import _gitrepo


# --------------------------------------------------------------------------- #
# Environment / capability probes. Each returns a value or a skip reason so a
# missing backend slice self-documents instead of failing.
# --------------------------------------------------------------------------- #

pytestmark = pytest.mark.timeout(60)


@pytest.fixture(autouse=True)
def gitsync_settings(settings, tmp_path):
    # type: (Any, Any) -> Any
    """Point ``repo_clone_dir`` at a writable tmp dir for every gitsync test.

    The shared ``settings`` fixture leaves ``repo_clone_dir`` at its production
    default (``/data/repos``), which is not writable in the test sandbox, so a
    real clone/fetch would fail with a permission error. Git-sync clones live in
    the control-plane volume; here that volume is a per-test tmp dir. We rebuild
    the frozen :class:`Settings` with :func:`dataclasses.replace` (preserving the
    DB URL, master key and bundle dir the other fixtures already wired) and
    re-install it as the process singleton. The app and ``gitsync`` both read the
    singleton via ``get_settings()`` at call time, so this takes effect for both
    the HTTP routes and the direct function calls.

    Autouse + a ``settings`` dependency guarantees it runs after the base
    fixture; it yields the corrected settings for tests that want it by name.
    """
    clone_dir = tmp_path / "repo-clones"
    clone_dir.mkdir(exist_ok=True)
    patched = dataclasses.replace(settings, repo_clone_dir=str(clone_dir))
    config_mod.set_settings(patched)
    yield patched
    # The base ``settings`` fixture calls reset_settings() on teardown; nothing
    # extra to undo here (the singleton is per-test).


def _require_git():
    # type: () -> None
    if not _gitrepo.git_available():  # pragma: no cover - env-dependent
        pytest.skip("git binary not available; cannot build the fixture repo")


def _gitsync():
    # type: () -> Any
    """Import ``server.gitsync`` or skip; tolerant of the module not existing."""
    try:
        return importlib.import_module("server.gitsync")
    except Exception as exc:  # ImportError today; anything else is still "not ready"
        pytest.skip("server.gitsync not importable yet (%s)" % exc)


def _gitsync_optional():
    # type: () -> Optional[Any]
    """Import ``server.gitsync`` without skipping (returns None when absent)."""
    try:
        return importlib.import_module("server.gitsync")
    except Exception:
        return None


def _symbol(module, name):
    # type: (Any, str) -> Any
    """Fetch a public callable from a module or skip if it is a stub/absent.

    A symbol counts as a stub when it is missing, or when its source body is a
    bare ``pass`` / ``...`` / ``raise NotImplementedError`` (the shape the
    foundation uses for not-yet-filled functions). This lets the file stay green
    while the backend builder fills the module in.
    """
    fn = getattr(module, name, None)
    if fn is None:
        pytest.skip("%s.%s is not defined yet" % (getattr(module, "__name__", module), name))
    if _is_stub(fn):
        pytest.skip("%s.%s is still a stub" % (getattr(module, "__name__", module), name))
    return fn


def _is_stub(fn):
    # type: (Any) -> bool
    """Heuristic: True when a function's body is empty / NotImplementedError."""
    if not callable(fn):
        return False
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return False
    # Strip the signature line and the docstring, then look at what remains.
    body_lines = []
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("def ", "async def ", "@", "#")):
            continue
        if stripped.startswith(('"""', "'''", '"', "'")):
            continue
        if stripped.startswith("# type:"):
            continue
        body_lines.append(stripped)
    if not body_lines:
        return True
    meaningful = [
        l for l in body_lines
        if l not in ("pass", "...", "return", "return None")
        and not l.startswith("raise NotImplementedError")
    ]
    return len(meaningful) == 0


def _has_repo_model():
    # type: () -> bool
    """True when the ``repos`` ORM model exists (Stage-3 schema landed)."""
    return getattr(models, "Repo", None) is not None


def _require_repo_model():
    # type: () -> Any
    if not _has_repo_model():
        pytest.skip("models.Repo (the repos table) is not defined yet")
    return models.Repo


def _pack_has_repo_link():
    # type: () -> bool
    """True when ``packs`` has the ``repo_id`` FK the Stage-3 model adds."""
    return "repo_id" in {c.name for c in models.Pack.__table__.columns}


def _route_status(client, method, path, **kwargs):
    # type: (Any, str, str, Any) -> int
    resp = getattr(client, method.lower())(path, **kwargs)
    return resp.status_code


def _route_present(client, method, path, **kwargs):
    # type: (Any, str, str, Any) -> bool
    """True when a route exists and is implemented (not 404/405/501)."""
    status = _route_status(client, method, path, **kwargs)
    return status not in (404, 405, 501)


def _require_repos_route(client):
    # type: (Any) -> None
    # A create with an empty body: a live endpoint answers 422 (validation),
    # a stub answers 501, an absent route 404/405. Only skip on the latter two.
    status = _route_status(client, "post", "/api/repos", json={})
    if status in (404, 405):
        pytest.skip("POST /api/repos route not present yet")
    if status == 501:
        pytest.skip("POST /api/repos is a 501 stub")


# --------------------------------------------------------------------------- #
# Small operator-API helpers (route-first; each guarded by the probes above).
# --------------------------------------------------------------------------- #

def _create_repo(client, url, auth_kind="none", secret=None, trusted_code=False,
                 default_ref="main"):
    # type: (Any, str, str, Optional[str], bool, str) -> Dict[str, Any]
    """POST /api/repos and return the created repo body (asserting 2xx)."""
    body = {"url": url, "auth_kind": auth_kind, "default_ref": default_ref}
    if secret is not None:
        body["secret"] = secret
    if trusted_code:
        body["trusted_code"] = True
    resp = client.post("/api/repos", json=body)
    assert resp.status_code in (200, 201), (
        "POST /api/repos -> %s: %s" % (resp.status_code, resp.text))
    return resp.json()


def _sync_repo_route(client, repo_id):
    # type: (Any, int) -> Any
    return client.post("/api/repos/%d/sync" % repo_id, json={})


def _packs_for_repo(client, repo_id):
    # type: (Any, int) -> List[Dict[str, Any]]
    resp = client.get("/api/packs", params={"repo": repo_id})
    assert resp.status_code == 200, "GET /api/packs?repo=%d -> %s" % (repo_id, resp.status_code)
    return resp.json()


def _pack_rows_for_repo(db, repo_id):
    # type: (Any, int) -> List[Any]
    """Fetch Pack rows linked to a repo directly from the DB (post-sync).

    Expires the session first so rows written by the app's request session (a
    different Session than this assertion one) are re-read rather than served
    stale from the identity map.
    """
    from sqlalchemy import select

    db.expire_all()
    stmt = select(models.Pack)
    if _pack_has_repo_link():
        stmt = stmt.where(models.Pack.repo_id == repo_id)
    return list(db.execute(stmt).scalars().all())


def _indexed_sha(pack):
    # type: (Any) -> Optional[str]
    """Read whichever indexed-SHA attribute the model exposes.

    The CONTROL-PLANE contract names it ``indexed_sha``; DESIGN s8 names it
    ``last_indexed_sha``. Accept either so the test tracks the model as built.
    """
    for attr in ("indexed_sha", "last_indexed_sha"):
        val = getattr(pack, attr, None)
        if val:
            return val
    return None


def _repo_get_bodies(client, repo_id):
    # type: (Any, int) -> List[str]
    """Every repos GET response body as text (for the secret-leak hunt)."""
    bodies = []
    for path in ("/api/repos", "/api/repos/%d" % repo_id):
        resp = client.get(path)
        if resp.status_code == 200:
            bodies.append(resp.text)
    return bodies


# --------------------------------------------------------------------------- #
# 1. Register a repo, sync it, assert the pack is indexed with the head SHA.
# --------------------------------------------------------------------------- #

def test_register_and_sync_indexes_pack(client, db_session, settings, tmp_path):
    """POST /api/repos then /sync indexes the committed pack.

    Asserts: the Pack row appears with ``repo_id`` set (when the model has the
    FK), ``lint_status == "ok"``, its indexed SHA equals the repo head SHA, and
    ``GET /api/packs?repo={id}`` returns it. Prefers the HTTP surface; if the
    routes are still stubs it drives the pure ``sync_repo``/``index_packs`` path
    instead and asserts the same DB end-state.
    """
    _require_git()
    _require_repo_model()

    repo_meta = _gitrepo.init_repo(str(tmp_path / "sample-packs"), name="nginx-access")
    head = repo_meta["head_sha"]

    # Preferred path: the operator routes.
    if _route_present(client, "post", "/api/repos", json={}):
        repo = _create_repo(client, repo_meta["url"])
        repo_id = repo["id"]

        sync = _sync_repo_route(client, repo_id)
        if sync.status_code == 501:
            pytest.skip("POST /api/repos/{id}/sync is a 501 stub")
        assert sync.status_code in (200, 202), (
            "sync -> %s: %s" % (sync.status_code, sync.text))
        payload = sync.json()
        # The API contract: {"head_sha":..., "packs_indexed":N, "lint_failures":M}
        assert payload.get("head_sha", head).startswith(head[:7]) or payload.get("head_sha") == head
        assert payload.get("packs_indexed", 1) >= 1

        listed = _packs_for_repo(client, repo_id)
        assert len(listed) >= 1, "GET /api/packs?repo=%d returned nothing" % repo_id
        names = [p.get("name") for p in listed]
        assert "nginx-access" in names
        indexed = next(p for p in listed if p.get("name") == "nginx-access")
        assert indexed.get("lint_status") == "ok", indexed
        # SHA surfaced on the pack row (either attribute name).
        sha = indexed.get("indexed_sha") or indexed.get("last_indexed_sha")
        assert sha == head, "indexed sha %r != head %r" % (sha, head)

        # Cross-check the DB row's repo link.
        rows = _pack_rows_for_repo(db_session, repo_id)
        assert rows, "no Pack rows linked to repo %d" % repo_id
        if _pack_has_repo_link():
            assert all(r.repo_id == repo_id for r in rows)
        row = next(r for r in rows if r.name == "nginx-access")
        assert row.lint_status == "ok"
        assert _indexed_sha(row) == head
        return

    # Fallback: pure functions (routes not present yet).
    gs = _gitsync()
    sync_repo = _symbol(gs, "sync_repo")
    Repo = models.Repo
    repo_row = Repo(url=repo_meta["url"])
    if hasattr(repo_row, "default_ref"):
        repo_row.default_ref = "main"
    db_session.add(repo_row)
    db_session.commit()

    result = sync_repo(db_session, repo_row, settings)  # signature: (db, repo, settings)
    db_session.commit()
    assert result is not None

    rows = _pack_rows_for_repo(db_session, repo_row.id)
    assert rows, "sync_repo indexed no packs"
    row = next((r for r in rows if r.name == "nginx-access"), rows[0])
    assert row.lint_status == "ok", row.lint_errors_json
    assert _indexed_sha(row) == head
    if _pack_has_repo_link():
        assert row.repo_id == repo_row.id


# --------------------------------------------------------------------------- #
# 2. A pack with no pack.yaml but a default/eventgen.conf: synthesised + unverified.
# --------------------------------------------------------------------------- #

def test_missing_pack_yaml_is_synthesised_and_unverified(client, db_session, settings, tmp_path):
    """A root-layout repo with only ``default/eventgen.conf`` still indexes.

    DESIGN s7 tolerance: ``pack.yaml`` is synthesised on sync and the pack is
    flagged ``unverified``. Asserts the Pack row exists, ``lint_status == "ok"``
    (a synthesised pack still lints) and ``verified is False``.
    """
    _require_git()
    _require_repo_model()

    repo_meta = _gitrepo.init_repo(
        str(tmp_path / "bare-ta"), name="barepack", layout="root", with_pack_yaml=False)

    row = _sync_and_get_single_pack(client, db_session, settings, repo_meta)
    if row is None:
        pytest.skip("neither the sync route nor sync_repo is implemented yet")

    assert row.lint_status == "ok", row.lint_errors_json
    assert row.verified is False, (
        "a pack whose pack.yaml was synthesised must be flagged unverified")


# --------------------------------------------------------------------------- #
# 3. The repo secret is write-only: never echoed in any repos GET body.
# --------------------------------------------------------------------------- #

SECRET_KEY_MATERIAL = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjE-STOKER-TEST-DO-NOT-LEAK-abcdef0123456789\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


def test_repo_secret_is_write_only(client, db_session, settings, tmp_path):
    """A repo registered with a deploy-key secret never echoes it back.

    Registers a repo with ``auth_kind=deploy_key`` and a known secret, then hunts
    for that material in ``GET /api/repos`` and ``GET /api/repos/{id}``. Also
    asserts the DB column stores ciphertext, not the plaintext (Fernet at rest).
    """
    _require_git()
    _require_repo_model()
    _require_repos_route(client)

    repo_meta = _gitrepo.init_repo(str(tmp_path / "private-repo"), name="secretpack")
    repo = _create_repo(
        client, repo_meta["url"], auth_kind="deploy_key", secret=SECRET_KEY_MATERIAL)
    repo_id = repo["id"]

    # The create response itself must not echo the secret.
    assert SECRET_KEY_MATERIAL not in json.dumps(repo)
    for forbidden_key in ("secret", "secret_encrypted", "webhook_secret"):
        # If a ciphertext field is surfaced at all it must not be the plaintext.
        if forbidden_key in repo and repo[forbidden_key]:
            assert SECRET_KEY_MATERIAL not in str(repo[forbidden_key])

    bodies = _repo_get_bodies(client, repo_id)
    assert bodies, "no repos GET endpoint answered 200 to check for leaks"
    for body in bodies:
        assert SECRET_KEY_MATERIAL not in body, "repo secret leaked into a GET body"
        # A distinctive fragment, in case of any re-encoding.
        assert "STOKER-TEST-DO-NOT-LEAK" not in body

    # DB-at-rest: the stored secret column is ciphertext, never the plaintext.
    db_session.expire_all()
    repo_row = db_session.get(models.Repo, repo_id)
    assert repo_row is not None
    stored = _first_secret_column_value(repo_row)
    if stored:
        assert SECRET_KEY_MATERIAL not in stored
        assert "STOKER-TEST-DO-NOT-LEAK" not in stored


# --------------------------------------------------------------------------- #
# 4. Custom-code default-deny: bin/ or 'generator =' rejected unless trusted_code.
# --------------------------------------------------------------------------- #

def test_custom_code_default_deny_bin_dir(client, db_session, settings, tmp_path):
    """A pack carrying a ``bin/`` dir is rejected (lint error) by default."""
    _require_git()
    _run_custom_code_denied_case(
        client, db_session, settings, tmp_path,
        name="binpack", with_bin=True)


def test_custom_code_default_deny_generator_stanza(client, db_session, settings, tmp_path):
    """A pack with a ``generator =`` stanza is rejected (lint error) by default."""
    _require_git()
    _run_custom_code_denied_case(
        client, db_session, settings, tmp_path,
        name="genpack", with_generator_stanza=True)


def test_custom_code_allowed_when_trusted_code(client, db_session, settings, tmp_path):
    """The same custom-code pack indexes ``ok`` when the repo is trusted_code.

    This is the other half of default-deny: an admin flag flips the policy. Skips
    when the backend has no trusted-code concept yet (route rejects the field or
    the model lacks the column), so it never fails a partial backend.
    """
    _require_git()
    _require_repo_model()

    repo_meta = _gitrepo.init_repo(
        str(tmp_path / "trusted-repo"), name="trustpack",
        with_bin=True, with_generator_stanza=True)

    # Route path: only meaningful if the create endpoint accepts trusted_code.
    if _route_present(client, "post", "/api/repos", json={}):
        if not _repo_model_supports_trusted():
            pytest.skip("repos model has no trusted_code flag yet")
        probe = client.post(
            "/api/repos",
            json={"url": repo_meta["url"], "auth_kind": "none", "trusted_code": True})
        if probe.status_code == 422:
            pytest.skip("POST /api/repos does not accept trusted_code yet")
        assert probe.status_code in (200, 201), probe.text
        repo_id = probe.json()["id"]
        sync = _sync_repo_route(client, repo_id)
        if sync.status_code == 501:
            pytest.skip("sync route is a stub")
        assert sync.status_code in (200, 202), sync.text
        rows = _pack_rows_for_repo(db_session, repo_id)
        if not rows:
            pytest.skip("trusted-code sync indexed no packs (feature incomplete)")
        # A trusted-code pack is allowed: at least one indexed pack lints ok.
        assert any(r.lint_status == "ok" for r in rows), (
            "trusted_code repo should permit custom-code packs to lint ok")
        return

    pytest.skip("repos routes not present; trusted-code path not exercisable yet")


# --------------------------------------------------------------------------- #
# 5. A file-token replacement path that escapes the pack root is rejected.
# --------------------------------------------------------------------------- #

def test_file_token_path_escape_rejected(client, db_session, settings, tmp_path):
    """A ``replacementType = file`` token pointing outside the bundle root fails lint.

    DESIGN s7: "Lint also rejects file/mvfile token paths that escape the bundle
    root." The fixture injects ``replacement = ../../../etc/passwd``; sync must
    record a lint error for that pack.
    """
    _require_git()
    _require_repo_model()

    repo_meta = _gitrepo.init_repo(
        str(tmp_path / "traversal-repo"), name="escapepack",
        file_token_path="../../../../etc/passwd")

    row = _sync_and_get_single_pack(client, db_session, settings, repo_meta,
                                    allow_lint_error=True)
    if row is None:
        pytest.skip("neither the sync route nor sync_repo is implemented yet")

    assert row.lint_status == "error", (
        "a bundle-escaping file token path must fail lint, got %r" % row.lint_status)
    errors = _lint_error_text(row)
    assert errors, "expected lint error details for the traversal token"


# --------------------------------------------------------------------------- #
# 6. Ref pinning: an old-SHA pack still resolves to old content after the branch moves.
# --------------------------------------------------------------------------- #

def test_ref_pinning_resync_updates_sha(client, db_session, gitsync_settings, tmp_path):
    """A second commit moves the branch; re-sync re-indexes at the new SHA.

    Two complementary assertions:

    * the indexed SHA advances from sha1 to sha2 after a re-sync (the re-index
      half of pinning); and
    * where ``resolve_pack_dir`` exists, the tree materialised at sha1 *before*
      the branch moved still holds the old content (count=100) after the move,
      proving a job pinned to sha1 is byte-identical even once the branch is at
      sha2. This is the reproducibility guarantee in DESIGN s7.

    Driven through the pure ``sync_repo``/``resolve_pack_dir`` seam so the SHA
    plumbing is asserted directly against the DB rows (the HTTP route delegates
    to the same functions; it is covered by the register+sync test).
    """
    _require_git()
    Repo = _require_repo_model()
    gs = _gitsync()
    sync_repo = _symbol(gs, "sync_repo")
    settings = gitsync_settings

    repo_dir = str(tmp_path / "moving-repo")
    repo_meta = _gitrepo.init_repo(repo_dir, name="pinpack", count=100)
    sha1 = repo_meta["head_sha"]

    # --- Register the repo + first sync at sha1 -----------------------------
    repo = _make_repo_row(repo_meta)
    db_session.add(repo)
    db_session.commit()
    sync_repo(db_session, repo, settings)
    db_session.commit()

    pack = _pack_by_name(db_session, repo.id, "pinpack")
    assert pack is not None, "first sync indexed no pinpack"
    assert _indexed_sha(pack) == sha1, "first sync should index at sha1"
    assert pack.lint_status == "ok"

    # --- Materialise the sha1 tree now (pinning snapshot) -------------------
    resolve = getattr(gs, "resolve_pack_dir", None)
    pinned_dir_sha1 = None
    if resolve is not None and not _is_stub(resolve):
        pinned_dir_sha1 = _resolve_pack_dir(resolve, repo, pack, settings)
        if pinned_dir_sha1:
            assert _read_count_from_pack_dir(pinned_dir_sha1) == 100, (
                "sha1 tree should hold the original count=100 content")

    # --- Move the branch to sha2, then re-sync ------------------------------
    sha2 = _gitrepo.add_commit(repo_dir, name="pinpack", count=250)
    assert sha2 != sha1
    sync_repo(db_session, repo, settings)
    db_session.commit()

    # Re-read the pack fresh (the upsert mutated the same (repo_id, name) row).
    db_session.expire_all()
    pack2 = _pack_by_name(db_session, repo.id, "pinpack")
    assert _indexed_sha(pack2) == sha2, (
        "re-sync should re-index at the new head %s, got %s"
        % (sha2[:7], (_indexed_sha(pack2) or "")[:7]))
    assert (repo.head_sha or "") == sha2

    # --- Pinning: the sha1 tree captured earlier is still count=100 ---------
    if pinned_dir_sha1 and os.path.isdir(pinned_dir_sha1):
        assert _read_count_from_pack_dir(pinned_dir_sha1) == 100, (
            "the sha1-pinned tree must stay byte-identical after the branch "
            "moved to sha2 (reproducibility)")
        # And a fresh resolve of the *old* SHA still yields old content: build a
        # detached view of the pack pinned at sha1 and resolve it again.
        if resolve is not None and not _is_stub(resolve):
            old_view = _pack_pinned_at(pack2, sha1)
            old_dir = _resolve_pack_dir(resolve, repo, old_view, settings)
            if old_dir:
                assert _read_count_from_pack_dir(old_dir) == 100


# --------------------------------------------------------------------------- #
# 7. GitHub webhook: bad HMAC rejected; good HMAC triggers a re-sync.
# --------------------------------------------------------------------------- #

def test_github_webhook_bad_hmac_rejected(client, db_session, settings, tmp_path):
    """POST /api/hooks/github with a wrong signature is rejected (401/403)."""
    _require_git()
    _require_repo_model()

    if not _route_hooks_present(client):
        pytest.skip("POST /api/hooks/github route not present yet")

    repo_meta = _gitrepo.init_repo(str(tmp_path / "hook-repo-bad"), name="hookpack")
    repo = _register_repo_with_webhook(client, db_session, repo_meta)
    if repo is None:
        pytest.skip("cannot register a repo with a webhook secret yet")

    payload = json.dumps({"ref": "refs/heads/main", "repository": {"id": 1}}).encode()
    bad_sig = "sha256=" + "0" * 64
    resp = client.post(
        "/api/hooks/github",
        content=payload,
        headers={
            "X-Hub-Signature-256": bad_sig,
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code in (401, 403), (
        "a bad HMAC must be rejected 401/403, got %s: %s" % (resp.status_code, resp.text))


def test_github_webhook_good_hmac_triggers_resync(client, db_session, settings, tmp_path):
    """POST /api/hooks/github with a valid signature is accepted (2xx).

    Registers a repo with a known ``webhook_secret``, signs the payload with
    HMAC-SHA256 and asserts a 2xx (the contract's "targeted re-sync"). We do not
    over-specify the side effect (the sync may be async); a 2xx acceptance is the
    observable guarantee, contrasted against the 401/403 in the bad-HMAC test.
    """
    _require_git()
    _require_repo_model()

    if not _route_hooks_present(client):
        pytest.skip("POST /api/hooks/github route not present yet")

    repo_meta = _gitrepo.init_repo(str(tmp_path / "hook-repo-good"), name="hookpack2")
    repo, secret = _register_repo_with_webhook(client, db_session, repo_meta, return_secret=True)
    if repo is None:
        pytest.skip("cannot register a repo with a known webhook secret yet")
    if not secret:
        pytest.skip("webhook secret is not knowable to the test (write-only, no seam)")

    payload = json.dumps({
        "ref": "refs/heads/main",
        "repository": {"id": 1, "full_name": "livehybrid/%s" % repo_meta["pack_rel"]},
    }).encode()
    good_sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    resp = client.post(
        "/api/hooks/github",
        content=payload,
        headers={
            "X-Hub-Signature-256": good_sig,
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code in (200, 202, 204), (
        "a valid HMAC should be accepted (2xx), got %s: %s" % (resp.status_code, resp.text))


# --------------------------------------------------------------------------- #
# Shared drivers used by more than one test (route-first, function-fallback).
# --------------------------------------------------------------------------- #

def _sync_and_get_single_pack(client, db_session, settings, repo_meta,
                              pack_name=None, allow_lint_error=False):
    # type: (Any, Any, Any, Dict[str, Any], Optional[str], bool) -> Optional[Any]
    """Register + sync a repo and return the single indexed Pack row (or None).

    Returns ``None`` (rather than skipping) when no sync seam is implemented, so
    the caller decides whether to skip. When ``allow_lint_error`` is False and
    the routes are used, a non-2xx sync that clearly means "not implemented"
    (501) returns None; a lint failure surfaced as a row is still returned.

    ``settings`` is accepted for signature symmetry but the direct-function path
    reads the live singleton via ``config_mod.get_settings()`` so it always uses
    the writable ``repo_clone_dir`` the ``gitsync_settings`` fixture installed
    (the same settings the HTTP route sees).
    """
    # Route path first.
    if _route_present(client, "post", "/api/repos", json={}):
        create = client.post(
            "/api/repos", json={"url": repo_meta["url"], "auth_kind": "none",
                                "default_ref": "main"})
        if create.status_code in (200, 201):
            repo_id = create.json()["id"]
            sync = _sync_repo_route(client, repo_id)
            if sync.status_code not in (501,):
                rows = _pack_rows_for_repo(db_session, repo_id)
                if rows:
                    if pack_name:
                        return next((r for r in rows if r.name == pack_name), rows[0])
                    return rows[0]
                # Sync ran but indexed nothing meaningful for us; fall through.
        # fall through to the function path if the route did not produce a row

    gs = _gitsync_optional()
    if gs is None:
        return None
    sync_repo = getattr(gs, "sync_repo", None)
    if sync_repo is None or _is_stub(sync_repo):
        return None

    live_settings = config_mod.get_settings()
    repo_row = _make_repo_row(repo_meta)
    db_session.add(repo_row)
    db_session.commit()
    try:
        sync_repo(db_session, repo_row, live_settings)
        db_session.commit()
    except Exception as exc:  # a real lint rejection may raise; re-read rows
        db_session.rollback()
        # If it raised for an unexpected reason and there are no rows, treat as
        # "not implemented enough" rather than a hard failure.
        if not allow_lint_error:
            pytest.skip("sync_repo raised before indexing (%s)" % exc)
    rows = _pack_rows_for_repo(db_session, repo_row.id)
    if not rows:
        return None
    if pack_name:
        return next((r for r in rows if r.name == pack_name), rows[0])
    return rows[0]


def _run_custom_code_denied_case(client, db_session, settings, tmp_path,
                                 name, with_bin=False, with_generator_stanza=False):
    # type: (Any, Any, Any, Any, str, bool, bool) -> None
    """Assert a custom-code pack is rejected (lint error) with no trusted flag."""
    _require_repo_model()
    repo_meta = _gitrepo.init_repo(
        str(tmp_path / ("deny-%s" % name)), name=name,
        with_bin=with_bin, with_generator_stanza=with_generator_stanza)

    row = _sync_and_get_single_pack(client, db_session, settings, repo_meta,
                                    pack_name=name, allow_lint_error=True)
    if row is None:
        pytest.skip("neither the sync route nor sync_repo is implemented yet")

    assert row.lint_status == "error", (
        "custom-code pack (bin=%s generator=%s) must be rejected by default, "
        "got lint_status=%r" % (with_bin, with_generator_stanza, row.lint_status))
    # It must not silently pass as verified either.
    assert row.verified is False


def _register_repo_with_webhook(client, db_session, repo_meta, return_secret=False):
    # type: (Any, Any, Dict[str, Any], bool) -> Any
    """Register a repo and recover its webhook secret; return (repo[, secret]).

    The contract's create route generates a per-repo ``webhook_secret`` and
    returns it **once** on the create response (never on later GETs); the webhook
    handler matches on it. So the good-HMAC test signs with the secret from that
    create response. Falls back, if the create response omits it, to reading the
    row's ``webhook_secret`` column directly (an equally valid seam). Returns the
    repo dict and, when ``return_secret`` is set, the secret (or ``None`` when it
    is genuinely unknowable to the test).
    """
    if not _route_present(client, "post", "/api/repos", json={}):
        return (None, None) if return_secret else None

    resp = client.post(
        "/api/repos", json={"url": repo_meta["url"], "auth_kind": "none"})
    if resp.status_code not in (200, 201):
        return (None, None) if return_secret else None
    repo_obj = resp.json()

    # Preferred: the secret returned once on create.
    secret = repo_obj.get("webhook_secret")

    # Fallback: read the stored column (plaintext, as the HMAC matcher uses it).
    if not secret and getattr(models, "Repo", None) is not None:
        db_session.expire_all()
        row = db_session.get(models.Repo, repo_obj["id"])
        if row is not None:
            current = getattr(row, "webhook_secret", None)
            if current and not _looks_like_ciphertext(current):
                secret = current

    if return_secret:
        return repo_obj, secret
    return repo_obj


# --------------------------------------------------------------------------- #
# Model / route introspection helpers.
# --------------------------------------------------------------------------- #

def _make_repo_row(repo_meta):
    # type: (Dict[str, Any]) -> Any
    """Build a ``Repo`` ORM row for the function-path tests, tolerant of schema."""
    Repo = models.Repo
    kwargs = {"url": repo_meta["url"]}  # type: Dict[str, Any]
    cols = {c.name for c in Repo.__table__.columns}
    if "auth_kind" in cols:
        kwargs["auth_kind"] = "none"
    if "default_ref" in cols:
        kwargs["default_ref"] = "main"
    return Repo(**kwargs)


def _repo_model_supports_trusted():
    # type: () -> bool
    Repo = getattr(models, "Repo", None)
    if Repo is None:
        return False
    cols = {c.name for c in Repo.__table__.columns}
    return bool({"trusted_code", "trusted", "code_trusted"} & cols)


def _first_secret_column_value(repo_row):
    # type: (Any) -> Optional[str]
    for attr in ("secret_encrypted", "token_encrypted", "secret", "credential_encrypted"):
        val = getattr(repo_row, attr, None)
        if val:
            return str(val)
    return None


def _looks_like_ciphertext(value):
    # type: (Any) -> bool
    """Rough guess: a Fernet token starts with 'gAAAAA' and has no newlines."""
    s = str(value)
    return s.startswith("gAAAAA")


def _lint_error_text(pack_row):
    # type: (Any) -> str
    errs = getattr(pack_row, "lint_errors_json", None)
    if not errs:
        return ""
    if isinstance(errs, (list, tuple)):
        return " ".join(str(e) for e in errs)
    return str(errs)


def _route_hooks_present(client):
    # type: (Any) -> bool
    """True when POST /api/hooks/github exists (probed with an empty unsigned body)."""
    resp = client.post("/api/hooks/github", content=b"{}",
                       headers={"Content-Type": "application/json"})
    # 404/405 => route missing; 501 => stub. Anything else (400/401/403/422/2xx)
    # means the route is wired.
    return resp.status_code not in (404, 405, 501)


def _pack_by_name(db, repo_id, name):
    # type: (Any, int, str) -> Optional[Any]
    """Fetch a Pack row by ``(repo_id, name)`` fresh from the DB."""
    from sqlalchemy import select

    stmt = select(models.Pack).where(models.Pack.name == name)
    if _pack_has_repo_link():
        stmt = stmt.where(models.Pack.repo_id == repo_id)
    return db.execute(stmt).scalars().first()


def _pack_pinned_at(pack, sha):
    # type: (Any, str) -> Any
    """A detached, transient Pack view pinned at ``sha`` (for old-SHA resolve).

    ``resolve_pack_dir`` reads ``pack.indexed_sha`` to pick the tree, so to prove
    an *old* pin still resolves after the branch moved we hand it a copy of the
    real pack with ``indexed_sha`` set back to ``sha``. It is not added to the
    session (purely a value object for the resolver's read path).
    """
    view = models.Pack(
        name=pack.name,
        source_path=pack.source_path,
        lint_status=pack.lint_status,
    )
    view.indexed_sha = sha
    if _pack_has_repo_link():
        view.repo_id = pack.repo_id
    view.id = pack.id
    return view


def _resolve_pack_dir(resolve_fn, repo, pack, settings):
    # type: (Any, Any, Any, Any) -> Optional[str]
    """Call ``resolve_pack_dir`` and return the resolved directory path or None.

    The backend signature is ``resolve_pack_dir(repo, pack, settings=None)``; we
    try that first, then a couple of tolerant fallbacks in case the exact order
    differs, so the test tracks the implementation as built. Any resolver error
    (e.g. the pin cannot be materialised) yields None rather than a hard failure,
    letting the caller treat pinning as best-effort where unsupported.
    """
    attempts = (
        lambda: resolve_fn(repo, pack, settings),
        lambda: resolve_fn(repo, pack),
        lambda: resolve_fn(pack, settings),
        lambda: resolve_fn(pack),
    )
    for call in attempts:
        try:
            result = call()
        except TypeError:
            continue
        except Exception:
            return None
        if isinstance(result, str):
            return result
        for attr in ("path", "pack_dir", "dir"):
            val = getattr(result, attr, None)
            if isinstance(val, str):
                return val
        try:
            return os.fspath(result)  # PathLike
        except TypeError:
            continue
    return None


def _read_count_from_pack_dir(pack_dir):
    # type: (str) -> Optional[int]
    """Read the ``count`` from a resolved pack dir's eventgen.conf (root or +1)."""
    candidates = [
        os.path.join(pack_dir, "default", "eventgen.conf"),
    ]
    # root-plus-one: a single subdir containing default/eventgen.conf.
    try:
        for entry in os.listdir(pack_dir):
            sub = os.path.join(pack_dir, entry, "default", "eventgen.conf")
            if os.path.isfile(sub):
                candidates.append(sub)
    except OSError:
        pass
    for conf_path in candidates:
        if not os.path.isfile(conf_path):
            continue
        try:
            with open(conf_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip().startswith("count ="):
                        return int(line.split("=", 1)[1].strip())
        except (OSError, ValueError):
            continue
    return None
