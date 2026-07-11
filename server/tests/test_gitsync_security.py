"""Regression tests for the git-sync security review findings.

Two option-injection sinks (repo url + ref) are closed at the API boundary
(RepoCreate validators) and defence-in-depth in sync.py (a `--` guard before the
url positional, a ref charset guard, and GIT_ALLOW_PROTOCOL dropping the ext::
transport). Path-escape checks resolve symlinks (realpath).
"""

from __future__ import annotations

import os

import pytest

from server.gitsync import sync as gitsync
from server.schemas import RepoCreate


# --------------------------------------------------------------------------- #
# URL option-injection is rejected at the API boundary.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_url", [
    "--upload-pack=/bin/sh",          # leading dash -> git option
    "-oProxyCommand=x",               # ssh option smuggling
    "ext::sh -c id",                  # arbitrary-command transport
    "ftp://example.com/x",            # non-allowlisted scheme
    "javascript:alert(1)",
    "",
])
def test_create_repo_rejects_dangerous_url(client, bad_url):
    resp = client.post("/api/repos", json={"url": bad_url, "auth_kind": "none"})
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("good_url", [
    "https://github.com/livehybrid/x.git",
    "ssh://git@github.com/livehybrid/x.git",
    "git@github.com:livehybrid/x.git",
    "file:///tmp/some/repo",
])
def test_create_repo_accepts_valid_url(client, good_url):
    # Accepted at validation (sync itself may fail later on a fake host; we only
    # assert the url passed the boundary, i.e. not a 422).
    resp = client.post("/api/repos", json={"url": good_url, "auth_kind": "none"})
    assert resp.status_code == 201, resp.text


def test_create_repo_rejects_ref_injection(client):
    resp = client.post("/api/repos", json={
        "url": "https://github.com/x/y.git",
        "default_ref": "--upload-pack=/bin/sh"})
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# sync-layer defence in depth.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_ref", ["-x", "--upload-pack=x", "a b", "a;b", "a$b", ""])
def test_require_safe_ref_rejects(bad_ref):
    with pytest.raises(gitsync.GitSyncError):
        gitsync._require_safe_ref(bad_ref)


@pytest.mark.parametrize("good_ref", ["main", "v1.2.3", "release/2026", "a" * 40])
def test_require_safe_ref_accepts(good_ref):
    gitsync._require_safe_ref(good_ref)  # no raise


def test_git_env_restricts_protocol():
    # The git subprocess env drops the ext:: transport.
    from server.models import Repo

    repo = Repo(url="https://x/y.git", auth_kind="none")
    with gitsync._GitAuthEnv(repo, None) as env:
        assert "ext" not in env["GIT_ALLOW_PROTOCOL"].split(":")
        assert "https" in env["GIT_ALLOW_PROTOCOL"].split(":")
        assert env["GIT_TERMINAL_PROMPT"] == "0"


# --------------------------------------------------------------------------- #
# Path escape resolves symlinks (realpath, not abspath).
# --------------------------------------------------------------------------- #

def test_path_escape_follows_symlink(tmp_path):
    pack = tmp_path / "pack"
    (pack / "samples").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    # A symlink that stays textually inside the pack but points outside it.
    link = pack / "samples" / "leak"
    os.symlink(str(outside), str(link))
    # Textually "samples/leak" is under the pack; realpath resolves it outside.
    assert gitsync._path_escapes(str(pack), "samples/leak") is True
    # A genuine in-pack path is not flagged.
    (pack / "samples" / "real.sample").write_text("x\n")
    assert gitsync._path_escapes(str(pack), "samples/real.sample") is False
