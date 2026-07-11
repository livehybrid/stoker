"""Local git-repo fixture builder for the Stage-3 gitsync tests.

Stage 3 syncs sample packs out of git repos. To test that end to end without a
network or a real GitHub, these helpers build a throwaway git repository in a
tmp dir: ``git init`` a bare-ish working repo, drop a pack into it (either at the
root or under ``packs/<name>/``), commit, and hand back a ``file://`` URL plus
the head SHA. A second commit helper moves the branch so the pinning test can
prove an old-SHA pack still resolves to old content.

Everything shells out to the system ``git`` (2.47 here) via :mod:`subprocess`;
no new Python dependency, matching the Stage-3 stack rule. Commits are made with
an explicit identity and isolated config so the tests never depend on (or
mutate) the developer's global git config. ``core.hooksPath=/dev/null`` and
``commit.gpgsign=false`` keep a hostile local environment (sample-pack repos
carrying hooks, a globally-enabled signing key) from breaking the fixture.

The pack payload deliberately mirrors ``conftest.make_pack``: one sample-mode
stanza, a matching sample file under ``samples/`` and a timestamp token, so a
pack written here lints identically to the flat pack the rest of the suite uses.
Optional switches let a test omit ``pack.yaml`` (to exercise synthesis), add a
``bin/`` directory or a ``generator =`` stanza (custom-code default-deny) or
inject a bundle-escaping ``file`` token path (path-traversal rejection).
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional


class GitError(RuntimeError):
    """A git subprocess exited non-zero while building the fixture repo."""


def git_available():
    # type: () -> bool
    """True when a usable ``git`` binary is on PATH (else the tests skip)."""
    try:
        subprocess.run(
            ["git", "--version"],
            check=True,
            capture_output=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - env-dependent
        return False


# Isolated, deterministic commit identity + config. Passed as ``-c`` overrides on
# every git call so nothing leaks from (or into) the developer's global config.
_GIT_ID = [
    "-c", "user.name=Stoker Test",
    "-c", "user.email=stoker-test@example.invalid",
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgsign=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "init.defaultBranch=main",
    "-c", "protocol.file.allow=always",
]


def _git(repo_dir, *args, env=None):
    # type: (str, str, Optional[Dict[str, str]]) -> str
    """Run ``git -C <repo_dir> <args>`` with the isolated identity; return stdout.

    Raises :class:`GitError` on a non-zero exit so a broken fixture fails loudly
    rather than silently producing an empty/again-committed repo.
    """
    cmd = ["git", "-C", repo_dir] + _GIT_ID + list(args)
    run_env = dict(os.environ)
    # Belt and braces: also blank the env-driven identity/config paths so a CI
    # box with GIT_AUTHOR_* set cannot override the -c identity above.
    run_env.update({
        "GIT_AUTHOR_NAME": "Stoker Test",
        "GIT_AUTHOR_EMAIL": "stoker-test@example.invalid",
        "GIT_COMMITTER_NAME": "Stoker Test",
        "GIT_COMMITTER_EMAIL": "stoker-test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    if env:
        run_env.update(env)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=run_env,
    )
    if proc.returncode != 0:
        raise GitError(
            "git %s failed (%d): %s"
            % (" ".join(args), proc.returncode, (proc.stderr or proc.stdout).strip())
        )
    return proc.stdout


def _eventgen_conf(name, count=100, extra_stanza=None, file_token_path=None):
    # type: (str, int, Optional[str], Optional[str]) -> str
    """Render an ``eventgen.conf`` for a flat sample-mode pack.

    ``extra_stanza`` is appended verbatim (used to inject a ``generator =``
    stanza for the custom-code test). ``file_token_path``, when given, adds a
    ``token.1`` of ``replacementType = file`` pointing at that path (used to
    inject a bundle-escaping traversal path for the path-safety lint test).
    """
    conf = (
        "[%s.sample]\n"
        "mode = sample\n"
        "interval = 1\n"
        "count = %d\n"
        "earliest = -1s\n"
        "latest = now\n"
        "\n"
        "token.0.token = \\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\n"
        "token.0.replacementType = timestamp\n"
        "token.0.replacement = %%Y-%%m-%%dT%%H:%%M:%%S\n"
    ) % (name, count)
    if file_token_path is not None:
        conf += (
            "\n"
            "token.1.token = @@FIELD@@\n"
            "token.1.replacementType = file\n"
            "token.1.replacement = %s\n"
        ) % file_token_path
    if extra_stanza:
        conf += "\n" + extra_stanza.rstrip() + "\n"
    return conf


def _pack_yaml(name, bytes_per_event=120):
    # type: (str, int) -> str
    return (
        "name: %s\n"
        "engine: eventgen\n"
        "description: \"tiny flat test pack (git fixture)\"\n"
        "tags: [test, git]\n"
        "estimates:\n"
        "  bytes_per_event: %d\n"
        "defaults:\n"
        "  index: main\n"
        "  sourcetype: stoker:%s\n"
    ) % (name, bytes_per_event, name)


def write_pack(
    dest_dir,
    name="flatline-git",
    count=100,
    bytes_per_event=120,
    with_pack_yaml=True,
    with_bin=False,
    with_generator_stanza=False,
    file_token_path=None,
):
    # type: (str, str, int, int, bool, bool, bool, Optional[str]) -> None
    """Write a pack payload into ``dest_dir`` (created if absent).

    Produces ``default/eventgen.conf`` + ``samples/<name>.sample`` and, unless
    ``with_pack_yaml=False``, a ``pack.yaml``. The optional switches build the
    hostile variants the lint tests need:

    * ``with_bin`` -- add ``bin/generator.py`` (arbitrary Python; default-deny).
    * ``with_generator_stanza`` -- add a ``generator = mygen`` stanza to the conf
      (also default-deny).
    * ``file_token_path`` -- add a ``replacementType = file`` token whose
      replacement path is ``file_token_path`` (pass a ``../`` path to exercise
      the bundle-root escape rejection).
    """
    default_dir = os.path.join(dest_dir, "default")
    samples_dir = os.path.join(dest_dir, "samples")
    os.makedirs(default_dir, exist_ok=True)
    os.makedirs(samples_dir, exist_ok=True)

    extra_stanza = None
    if with_generator_stanza:
        # A custom-generator stanza. It points ``sampleFile`` at the pack's
        # existing sample so the stanza lints clean *except* for the policy-
        # relevant ``generator =`` key: that way the custom-code test isolates
        # the default-deny behaviour (and the trusted_code case lints ``ok``).
        extra_stanza = (
            "[%s.custom]\n"
            "mode = sample\n"
            "sampleFile = %s.sample\n"
            "generator = mygenerator\n"
            "count = 10\n"
        ) % (name, name)

    conf = _eventgen_conf(
        name,
        count=count,
        extra_stanza=extra_stanza,
        file_token_path=file_token_path,
    )
    with open(os.path.join(default_dir, "eventgen.conf"), "w", encoding="utf-8") as fh:
        fh.write(conf)

    sample_lines = "\n".join(
        "2026-01-01T00:00:%02d event line %d payload=abcdefghij" % (i % 60, i)
        for i in range(20)
    )
    with open(os.path.join(samples_dir, "%s.sample" % name), "w", encoding="utf-8") as fh:
        fh.write(sample_lines + "\n")

    if with_pack_yaml:
        with open(os.path.join(dest_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
            fh.write(_pack_yaml(name, bytes_per_event=bytes_per_event))

    if with_bin:
        bin_dir = os.path.join(dest_dir, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        with open(os.path.join(bin_dir, "generator.py"), "w", encoding="utf-8") as fh:
            fh.write(
                "# Arbitrary custom generator code (default-deny per DESIGN s7).\n"
                "def generate(*args, **kwargs):\n"
                "    return []\n"
            )


def init_repo(
    repo_dir,
    name="flatline-git",
    layout="packs",
    with_pack_yaml=True,
    with_bin=False,
    with_generator_stanza=False,
    file_token_path=None,
    count=100,
):
    # type: (str, str, str, bool, bool, bool, Optional[str], int) -> Dict[str, str]
    """Create a git repo at ``repo_dir`` with one committed pack; return metadata.

    ``layout`` is either ``"packs"`` (pack under ``packs/<name>/``, the monorepo
    shape) or ``"root"`` (``default/eventgen.conf`` at the repo root, the bare
    community-TA shape the synthesis tolerance targets).

    Returns ``{"url": "file://<abs>", "path": <abs>, "head_sha": <sha>,
    "pack_rel": <relative pack dir>}``. ``url`` is a ``file://`` URL suitable for
    ``git clone``; ``path`` is the same absolute path for callers that prefer a
    local-path URL. ``head_sha`` is the full 40-char commit SHA of the initial
    commit.
    """
    os.makedirs(repo_dir, exist_ok=True)
    _git(repo_dir, "init", "-q")
    # Force the branch name deterministically (older git may honour a different
    # global default despite the -c above on very old versions).
    _git(repo_dir, "symbolic-ref", "HEAD", "refs/heads/main")

    if layout == "root":
        pack_root = repo_dir
        pack_rel = "."
    else:
        pack_rel = os.path.join("packs", name)
        pack_root = os.path.join(repo_dir, pack_rel)

    write_pack(
        pack_root,
        name=name,
        count=count,
        with_pack_yaml=with_pack_yaml,
        with_bin=with_bin,
        with_generator_stanza=with_generator_stanza,
        file_token_path=file_token_path,
    )

    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "Add %s pack" % name)
    head = head_sha(repo_dir)
    return {
        "url": "file://%s" % os.path.abspath(repo_dir),
        "path": os.path.abspath(repo_dir),
        "head_sha": head,
        "pack_rel": pack_rel,
    }


def add_commit(repo_dir, name="flatline-git", layout="packs", count=250, message="bump count"):
    # type: (str, str, str, int, str) -> str
    """Mutate the pack and add a second commit; return the new head SHA.

    Rewrites the pack's ``eventgen.conf`` with a different ``count`` so the tree
    (and therefore the commit SHA and any bundle content digest) changes. Used
    by the pinning test: after this, the branch head has moved, but a pack
    indexed at the previous SHA must still resolve to the previous content.
    """
    if layout == "root":
        pack_root = repo_dir
    else:
        pack_root = os.path.join(repo_dir, "packs", name)
    # Rewrite only the conf (keep pack.yaml/samples) so it is unambiguously the
    # same pack at a new revision.
    conf = _eventgen_conf(name, count=count)
    with open(os.path.join(pack_root, "default", "eventgen.conf"), "w", encoding="utf-8") as fh:
        fh.write(conf)
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", message)
    return head_sha(repo_dir)


def head_sha(repo_dir, ref="HEAD"):
    # type: (str, str) -> str
    """Return the full commit SHA that ``ref`` resolves to in ``repo_dir``."""
    return _git(repo_dir, "rev-parse", ref).strip()


def read_conf_count(repo_dir, name="flatline-git", layout="packs"):
    # type: (str, str, str) -> Optional[int]
    """Read back the ``count`` from a pack's conf on disk (content assertion)."""
    if layout == "root":
        conf_path = os.path.join(repo_dir, "default", "eventgen.conf")
    else:
        conf_path = os.path.join(repo_dir, "packs", name, "default", "eventgen.conf")
    try:
        with open(conf_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("count ="):
                    return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None


__all__ = [
    "GitError",
    "git_available",
    "init_repo",
    "add_commit",
    "head_sha",
    "write_pack",
    "read_conf_count",
]
