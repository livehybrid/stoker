"""PISTON (rawreplay engine) control-plane tests.

Covers the control-plane side of Piston, the raw-replay engine: a rawreplay pack
declares ``engine: rawreplay`` and a ``replay:`` section (a local ``dataset`` or
an https ``dataset_url``, ``mode: rate|cadence``, ``time_multiple``) with NO
``default/eventgen.conf``.

Scope (mirrors the task):

* lint: a rawreplay pack with a local dataset lints clean and measures
  bytes/event from the dataset; a ``dataset_url`` pack lints on the URL; the
  broken variants (missing dataset, both dataset+url, http url, escaping path,
  bad mode/time_multiple) each fail with a specific error.
* bundle: build embeds the local dataset + a ``replay`` block in ``stoker.json``;
  a ``dataset_url`` is fetched at build time (mocked), embedded at a fixed path,
  sha-verified when declared, size-capped; the build is reproducible/dedup.
* gitsync: ``index_packs`` recognises a rawreplay pack (no eventgen.conf) as a
  valid pack, sets ``engines_json == ["rawreplay"]`` and keeps the guards.
* lifecycle: a rawreplay run is forced to workers = 1 and its run snapshot
  projects ``STOKER_ENGINE=rawreplay``; the submit route rejects a multi-worker
  rawreplay spec and the estimate reflects the single worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile

import pytest

from server import bundles, lifecycle
from server.bundles import (
    BundleError,
    build_from_pack,
    build_stoker_manifest,
    is_rawreplay_pack,
    lint_pack,
    parse_replay_config,
)

from . import _helpers


# --------------------------------------------------------------------------- #
# Local rawreplay pack builders.
# --------------------------------------------------------------------------- #

_DATASET_LINES = (
    "2026-01-01T00:00:00Z evt A user=alice action=login src=10.0.0.1\n"
    "2026-01-01T00:00:03Z evt B user=alice action=list src=10.0.0.1\n"
    "2026-01-01T00:00:07Z evt C user=bob action=login src=10.0.0.2\n"
)


def _write_rawreplay_pack(root, name="attack-replay", mode="cadence",
                          time_multiple=1, dataset_rel="samples/capture.log",
                          sourcetype="aws:cloudtrail", source="capture.log",
                          bytes_per_event=None, write_dataset=True):
    # type: (...) -> str
    """Write a rawreplay pack with a local dataset; return its directory."""
    pack_dir = os.path.join(root, name)
    os.makedirs(os.path.join(pack_dir, os.path.dirname(dataset_rel)), exist_ok=True)
    if write_dataset:
        with open(os.path.join(pack_dir, dataset_rel), "w", encoding="utf-8") as fh:
            fh.write(_DATASET_LINES)
    lines = [
        "name: %s" % name,
        "engine: rawreplay",
        "description: \"replay an attack_data capture byte-for-byte\"",
        "replay:",
        "  dataset: %s" % dataset_rel,
        "  mode: %s" % mode,
        "  time_multiple: %s" % time_multiple,
        "defaults:",
    ]
    if sourcetype:
        lines.append("  sourcetype: %s" % sourcetype)
    if source:
        lines.append("  source: %s" % source)
    if bytes_per_event is not None:
        lines.append("estimates:")
        lines.append("  bytes_per_event: %d" % bytes_per_event)
    with open(os.path.join(pack_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return pack_dir


def _write_url_rawreplay_pack(root, name="attack-url",
                              url="https://raw.githubusercontent.com/splunk/attack_data/master/x/capture.log",
                              sha256=None, mode="rate"):
    # type: (...) -> str
    """Write a rawreplay pack whose dataset is an https ``dataset_url``."""
    pack_dir = os.path.join(root, name)
    os.makedirs(pack_dir, exist_ok=True)
    lines = [
        "name: %s" % name,
        "engine: rawreplay",
        "replay:",
        "  dataset_url: %s" % url,
        "  mode: %s" % mode,
    ]
    if sha256:
        lines.append("  dataset_sha256: %s" % sha256)
    lines += ["defaults:", "  sourcetype: attack:data"]
    with open(os.path.join(pack_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return pack_dir


# --------------------------------------------------------------------------- #
# Lint.
# --------------------------------------------------------------------------- #

def test_detects_rawreplay_pack(tmp_path):
    pack = _write_rawreplay_pack(str(tmp_path))
    assert is_rawreplay_pack(pack)


def test_lint_rawreplay_local_dataset_ok(tmp_path):
    pack = _write_rawreplay_pack(str(tmp_path), mode="cadence", time_multiple=2)
    result = lint_pack(pack)
    assert result.ok, result.errors
    assert result.engine == "rawreplay"
    assert result.engines == ["rawreplay"]
    assert result.stanzas == []
    assert result.stanza_count == 0
    assert result.sourcetypes == ["aws:cloudtrail"]
    # bytes/event measured from the dataset (no declared estimate).
    assert result.est_bytes_per_event is not None and result.est_bytes_per_event > 0
    assert result.replay is not None
    assert result.replay["mode"] == "cadence"
    assert result.replay["time_multiple"] == pytest.approx(2.0)
    assert result.replay["dataset"] == "samples/capture.log"
    assert result.replay["source"] == "capture.log"


def test_lint_rawreplay_declared_bytes_per_event(tmp_path):
    pack = _write_rawreplay_pack(str(tmp_path), bytes_per_event=200)
    result = lint_pack(pack)
    assert result.ok
    assert result.declared_bytes_per_event == pytest.approx(200.0)
    assert result.est_bytes_per_event == pytest.approx(200.0)


def test_lint_rawreplay_url_ok(tmp_path):
    pack = _write_url_rawreplay_pack(str(tmp_path))
    result = lint_pack(pack)
    assert result.ok, result.errors
    assert result.engine == "rawreplay"
    assert result.replay["dataset_url"].startswith("https://")
    assert result.replay["mode"] == "rate"


def test_lint_rawreplay_missing_dataset_file(tmp_path):
    pack = _write_rawreplay_pack(str(tmp_path), write_dataset=False)
    result = lint_pack(pack)
    assert not result.ok
    assert any("dataset file" in e for e in result.errors)


def test_lint_rawreplay_requires_a_dataset(tmp_path):
    pack_dir = os.path.join(str(tmp_path), "nodataset")
    os.makedirs(pack_dir)
    with open(os.path.join(pack_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
        fh.write("name: nodataset\nengine: rawreplay\nreplay:\n  mode: rate\n")
    result = lint_pack(pack_dir)
    assert not result.ok
    assert any("dataset" in e for e in result.errors)


def test_lint_rawreplay_local_dataset_with_provenance_url_ok(tmp_path):
    # A local `dataset` alongside a `dataset_url` is fine: the URL is provenance
    # only (where the capture came from) and is never fetched. The local dataset
    # wins, so an http (non-https) provenance URL is tolerated too.
    pack_dir = os.path.join(str(tmp_path), "both")
    os.makedirs(os.path.join(pack_dir, "samples"))
    with open(os.path.join(pack_dir, "samples", "c.log"), "w") as fh:
        fh.write("line\n")
    with open(os.path.join(pack_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
        fh.write(
            "name: both\nengine: rawreplay\nreplay:\n"
            "  dataset: samples/c.log\n"
            "  dataset_url: https://github.com/splunk/security_content/x\n  mode: rate\n")
    cfg, errors = parse_replay_config(pack_dir)
    assert errors == []
    assert cfg["dataset"] == "samples/c.log"
    assert cfg["dataset_url"].startswith("https://")  # kept for the audit trail
    assert cfg["fetch_url"] is None  # never fetched (local dataset wins)
    assert lint_pack(pack_dir).ok


def test_lint_rawreplay_http_url_rejected(tmp_path):
    pack = _write_url_rawreplay_pack(str(tmp_path), url="http://insecure/x")
    result = lint_pack(pack)
    assert not result.ok
    assert any("https" in e for e in result.errors)


def test_lint_rawreplay_escaping_dataset_rejected(tmp_path):
    pack_dir = os.path.join(str(tmp_path), "escape")
    os.makedirs(pack_dir)
    with open(os.path.join(pack_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
        fh.write(
            "name: escape\nengine: rawreplay\nreplay:\n"
            "  dataset: ../../etc/passwd\n  mode: rate\n")
    result = lint_pack(pack_dir)
    assert not result.ok
    assert any("escapes the pack root" in e for e in result.errors)


def test_lint_rawreplay_bad_mode_and_time_multiple(tmp_path):
    pack_dir = os.path.join(str(tmp_path), "badmode")
    os.makedirs(os.path.join(pack_dir, "samples"))
    with open(os.path.join(pack_dir, "samples", "c.log"), "w") as fh:
        fh.write("line\n")
    with open(os.path.join(pack_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
        fh.write(
            "name: badmode\nengine: rawreplay\nreplay:\n"
            "  dataset: samples/c.log\n  mode: sideways\n  time_multiple: -1\n")
    result = lint_pack(pack_dir)
    assert not result.ok
    assert any("mode must be one of" in e for e in result.errors)
    assert any("time_multiple must be > 0" in e for e in result.errors)


def test_parse_replay_config_defaults(tmp_path):
    # mode defaults to rate; time_multiple defaults to 1.0.
    pack_dir = os.path.join(str(tmp_path), "defaults")
    os.makedirs(os.path.join(pack_dir, "samples"))
    with open(os.path.join(pack_dir, "samples", "c.log"), "w") as fh:
        fh.write("line\n")
    with open(os.path.join(pack_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
        fh.write(
            "name: d\nengine: rawreplay\nreplay:\n  dataset: samples/c.log\n")
    cfg, errors = parse_replay_config(pack_dir)
    assert errors == []
    assert cfg["mode"] == "rate"
    assert cfg["time_multiple"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Bundle build.
# --------------------------------------------------------------------------- #

def _read_manifest(tar_path):
    # type: (str) -> dict
    with tarfile.open(tar_path, "r:*") as tar:
        name = next(n for n in tar.getnames() if n.endswith("stoker.json"))
        return json.loads(tar.extractfile(name).read())


def test_build_rawreplay_local_dataset(settings, tmp_path):
    pack = _write_rawreplay_pack(str(tmp_path), mode="cadence", time_multiple=3)
    built = build_from_pack(pack, bundle_dir=settings.bundle_dir, settings=settings)
    assert len(built.digest) == 64
    with open(built.path, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == built.digest
    # the dataset + a replay manifest block ship in the bundle.
    with tarfile.open(built.path, "r:*") as tar:
        names = tar.getnames()
    assert any(n.endswith("samples/capture.log") for n in names)
    manifest = _read_manifest(built.path)
    assert manifest["engine"] == "rawreplay"
    assert manifest["replay"]["dataset"] == "samples/capture.log"
    assert manifest["replay"]["mode"] == "cadence"
    assert manifest["replay"]["time_multiple"] == pytest.approx(3.0)
    assert manifest["replay"]["sourcetype"] == "aws:cloudtrail"
    assert manifest["replay"]["source"] == "capture.log"


def test_shipped_attack_replay_pack_lints_and_builds(settings):
    """The shipped ``packs/attack-replay`` rawreplay example is valid end-to-end.

    Locks the control-plane and worker sides to one pack.yaml shape: a local
    ``dataset`` with a provenance ``dataset_url`` beside it, an eventgen-fallback
    ``mode = replay`` conf, and declared estimates. The dataset ships in the
    bundle and the manifest carries the replay block.
    """
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pack_dir = os.path.join(here, "packs", "attack-replay")
    if not os.path.isdir(pack_dir):
        pytest.skip("packs/attack-replay not present in this checkout")
    assert is_rawreplay_pack(pack_dir)
    result = lint_pack(pack_dir)
    assert result.ok, result.errors
    assert result.engine == "rawreplay"
    assert result.replay["dataset"] == "dataset/events.log"
    assert result.replay["fetch_url"] is None  # provenance URL, not fetched
    built = build_from_pack(pack_dir, bundle_dir=settings.bundle_dir, settings=settings)
    with tarfile.open(built.path, "r:*") as tar:
        names = tar.getnames()
    assert any(n.endswith("dataset/events.log") for n in names)
    manifest = _read_manifest(built.path)
    assert manifest["engine"] == "rawreplay"
    assert manifest["replay"]["dataset"] == "dataset/events.log"


def test_server_bundle_read_by_worker_replay_loader(settings, tmp_path):
    """A server-built rawreplay bundle is read back by the worker's loader.

    The definitive cross-side contract: build a rawreplay bundle with the control
    plane, unpack it, and have the worker's ``resolve_replay_config`` read the
    ``stoker.json`` replay block the server wrote. Proves the two sides agree on
    the manifest shape (dataset path, mode, time_multiple). Skipped when the
    worker package is not importable in this checkout.
    """
    try:
        from stoker_agent.bundle import resolve_replay_config
    except Exception:
        pytest.skip("worker stoker_agent not importable")

    pack = _write_rawreplay_pack(str(tmp_path), mode="cadence", time_multiple=2)
    built = build_from_pack(pack, bundle_dir=settings.bundle_dir, settings=settings)

    dest = os.path.join(str(tmp_path), "unpacked")
    os.makedirs(dest)
    with tarfile.open(built.path, "r:*") as tar:
        tar.extractall(dest, filter="data")
    pack_root = next(
        os.path.join(dest, e) for e in os.listdir(dest)
        if os.path.isfile(os.path.join(dest, e, "stoker.json")))

    rc = resolve_replay_config(pack_root)
    assert rc is not None
    assert rc.mode == "cadence"
    assert rc.time_multiple == pytest.approx(2.0)
    # dataset resolved to an absolute path inside the unpacked pack.
    assert os.path.isfile(rc.dataset)
    assert rc.dataset.endswith(os.path.join("samples", "capture.log"))


def test_build_rawreplay_is_reproducible_and_dedups(settings, tmp_path):
    pack = _write_rawreplay_pack(str(tmp_path))
    a = build_from_pack(pack, bundle_dir=settings.bundle_dir, settings=settings)
    b = build_from_pack(pack, bundle_dir=settings.bundle_dir, settings=settings)
    assert a.digest == b.digest
    assert b.reused


def test_build_rawreplay_fetches_dataset_url(settings, tmp_path, monkeypatch):
    payload = b"".join(
        ("2026-01-01T00:00:%02dZ remote evt %d\n" % (i, i)).encode() for i in range(30))
    sha = hashlib.sha256(payload).hexdigest()
    pack = _write_url_rawreplay_pack(str(tmp_path), sha256=sha)

    calls = {"n": 0}

    def _fake_fetch(url, max_bytes, timeout_s, expected_sha256=None):
        calls["n"] += 1
        # honour the sha the builder passes through (proves it is wired).
        if expected_sha256 and hashlib.sha256(payload).hexdigest() != expected_sha256.lower():
            raise BundleError("sha mismatch")
        return payload

    monkeypatch.setattr(bundles, "_fetch_dataset_url", _fake_fetch)
    built = build_from_pack(pack, bundle_dir=settings.bundle_dir, settings=settings)
    assert calls["n"] == 1
    with tarfile.open(built.path, "r:*") as tar:
        names = tar.getnames()
        dataset_member = next(n for n in names if n.endswith("dataset/replay.dat"))
        assert tar.extractfile(dataset_member).read() == payload
    manifest = _read_manifest(built.path)
    # the manifest points the worker at the in-bundle fetched dataset.
    assert manifest["replay"]["dataset"] == "dataset/replay.dat"
    assert manifest["replay"]["mode"] == "rate"


def test_build_rawreplay_url_sha_mismatch_fails(settings, tmp_path, monkeypatch):
    pack = _write_url_rawreplay_pack(str(tmp_path), sha256="00" * 32)

    def _fake_fetch(url, max_bytes, timeout_s, expected_sha256=None):
        # the real fetcher raises on mismatch; emulate that contract.
        raise BundleError("rawreplay dataset %s sha256 mismatch" % url)

    monkeypatch.setattr(bundles, "_fetch_dataset_url", _fake_fetch)
    with pytest.raises(BundleError):
        build_from_pack(pack, bundle_dir=settings.bundle_dir, settings=settings)


def test_fetch_dataset_url_guards(monkeypatch):
    """The real fetcher enforces https-only, a PUBLIC host, the size cap, the
    sha, no embedded credentials, and no auto-redirect (allow_redirects=False)."""
    import types

    payload = b"x" * 200

    class _Resp(object):
        def __init__(self, status=200, headers=None, body=payload):
            self.status_code = status
            self.headers = headers or {}
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, n):
            for i in range(0, len(self._body), n):
                yield self._body[i:i + n]

    class _Exc(Exception):
        pass

    responses = {}  # url -> _Resp override

    def _get(url, stream, timeout, allow_redirects):
        assert allow_redirects is False, "fetcher must not auto-follow redirects"
        return responses.get(url, _Resp())

    fake_requests = types.SimpleNamespace(
        get=_get, exceptions=types.SimpleNamespace(RequestException=_Exc))
    monkeypatch.setitem(__import__("sys").modules, "requests", fake_requests)

    # A public IP literal passes the SSRF guard without touching DNS.
    pub = "https://8.8.8.8/y"
    good = hashlib.sha256(payload).hexdigest()
    assert bundles._fetch_dataset_url(pub, 1 << 20, 5, good) == payload
    with pytest.raises(BundleError):
        bundles._fetch_dataset_url(pub, 1 << 20, 5, "00" * 32)  # sha mismatch
    with pytest.raises(BundleError):
        bundles._fetch_dataset_url(pub, 10, 5)  # over the size cap

    # SSRF guard: non-https, internal/loopback/link-local literals, and embedded
    # credentials are all refused before any fetch happens.
    for bad in (
        "http://8.8.8.8/y",                       # non-https
        "https://127.0.0.1/y",                    # loopback
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://10.0.0.5/y",                     # private
        "https://192.168.1.1/y",                  # private
        "https://user:pw@8.8.8.8/y",              # embedded credentials
    ):
        with pytest.raises(BundleError):
            bundles._fetch_dataset_url(bad, 1 << 20, 5)

    # SSRF via redirect: a public URL that 302s to the metadata IP is refused
    # (every hop is re-validated, not just the first).
    responses["https://8.8.8.8/redir"] = _Resp(
        status=302, headers={"Location": "https://169.254.169.254/"})
    with pytest.raises(BundleError):
        bundles._fetch_dataset_url("https://8.8.8.8/redir", 1 << 20, 5)


def test_scale_run_clamps_rawreplay(db_session, settings, fake_driver, tmp_path):
    """scale_run must never grow a replay run: a rawreplay run stays at one
    worker even if a caller asks for more (else the dataset stream duplicates)."""
    pack_dir = _write_rawreplay_pack(str(tmp_path))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, pack_dir, name="attack-replay")
    pack.engines_json = ["rawreplay"]
    db_session.flush()
    spec = _helpers.make_spec(
        db_session, pack, target, engine="rawreplay", rate_mode="eps",
        rate_value=500.0, workers=1, fleet="fake-local")
    db_session.commit()
    run = lifecycle.provision_run(db_session, spec, fake_driver, started_by="test")
    db_session.commit()

    lifecycle.scale_run(db_session, run, fake_driver, 4)
    db_session.commit()

    leases = lifecycle.get_run_leases(db_session, run)
    assert len(leases) == 1, "scale must not grow a rawreplay run beyond one worker"
    assert run.spec_snapshot_json["workers"] == 1


def test_scale_endpoint_rejects_rawreplay(client, db_session, settings, fake_driver, tmp_path):
    """POST /runs/{id}/scale on a rawreplay run returns 409 replay_single_worker."""
    pack_dir = _write_rawreplay_pack(str(tmp_path))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, pack_dir, name="attack-replay")
    pack.engines_json = ["rawreplay"]
    db_session.flush()
    spec = _helpers.make_spec(
        db_session, pack, target, engine="rawreplay", rate_mode="eps",
        rate_value=500.0, workers=1, fleet="fake-local")
    db_session.commit()
    run = lifecycle.provision_run(db_session, spec, fake_driver, started_by="test")
    db_session.commit()

    resp = client.post("/api/runs/%d/scale" % run.id, json={"workers": 4})
    assert resp.status_code == 409, resp.text
    assert "replay_single_worker" in resp.text


# --------------------------------------------------------------------------- #
# gitsync indexing of a rawreplay pack (no eventgen.conf).
# --------------------------------------------------------------------------- #

def _git_available():
    # type: () -> bool
    from . import _gitrepo

    return _gitrepo.git_available()


@pytest.fixture()
def gitsync_settings(settings, tmp_path):
    # type: (...) -> object
    """Point repo_clone_dir at a writable temp dir (dataclass is frozen)."""
    import dataclasses

    clone_dir = tmp_path / "clones"
    clone_dir.mkdir()
    patched = dataclasses.replace(settings, repo_clone_dir=str(clone_dir))
    from server import config as config_mod

    config_mod.set_settings(patched)
    return patched


def _init_rawreplay_repo(repo_dir, name="attack-replay"):
    # type: (str, str) -> dict
    """git init a repo with one rawreplay pack under ``packs/<name>/``.

    ``_write_rawreplay_pack(root, name=name)`` writes the pack under
    ``<root>/<name>``, so rooting it at ``<repo>/packs`` lands it at exactly
    ``packs/<name>`` (the monorepo layout gitsync discovers).
    """
    from . import _gitrepo

    os.makedirs(os.path.join(repo_dir, "packs"), exist_ok=True)
    _gitrepo._git(repo_dir, "init", "-q")
    _gitrepo._git(repo_dir, "symbolic-ref", "HEAD", "refs/heads/main")
    _write_rawreplay_pack(os.path.join(repo_dir, "packs"), name=name)
    _gitrepo._git(repo_dir, "add", "-A")
    _gitrepo._git(repo_dir, "commit", "-q", "-m", "add rawreplay pack")
    head = _gitrepo.head_sha(repo_dir)
    return {"url": "file://%s" % os.path.abspath(repo_dir), "head_sha": head}


@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_gitsync_indexes_rawreplay_pack(db_session, gitsync_settings, tmp_path):
    from server import gitsync
    from server.models import Repo

    meta = _init_rawreplay_repo(str(tmp_path / "attack-packs"), name="attack-replay")

    repo = Repo(url=meta["url"], default_ref="main")
    db_session.add(repo)
    db_session.commit()

    result = gitsync.sync_repo(db_session, repo, gitsync_settings)
    db_session.commit()
    assert result["packs_indexed"] >= 1
    assert result["lint_failures"] == 0

    from sqlalchemy import select
    from server.models import Pack

    rows = list(db_session.execute(
        select(Pack).where(Pack.repo_id == repo.id)).scalars().all())
    assert rows, "rawreplay pack was not indexed"
    row = next(r for r in rows if r.name == "attack-replay")
    assert row.lint_status == "ok", row.lint_errors_json
    assert row.engines_json == ["rawreplay"]
    assert row.indexed_sha == meta["head_sha"]
    # verified: an author-supplied pack.yaml on a clean pack.
    assert row.verified is True

    # And it builds into a bundle from the pinned SHA (dataset ships inside).
    pack_dir = gitsync.resolve_pack_dir(repo, row, gitsync_settings)
    assert bundles.is_rawreplay_pack(pack_dir)
    built = build_from_pack(pack_dir, bundle_dir=gitsync_settings.bundle_dir,
                            settings=gitsync_settings)
    with tarfile.open(built.path, "r:*") as tar:
        assert any(n.endswith("samples/capture.log") for n in tar.getnames())


# --------------------------------------------------------------------------- #
# Lifecycle: workers forced to 1 + STOKER_ENGINE projection + route rejections.
# --------------------------------------------------------------------------- #

def test_effective_workers_clamps_rawreplay():
    assert lifecycle.effective_workers("rawreplay", 4) == 1
    assert lifecycle.effective_workers("rawreplay", 1) == 1
    assert lifecycle.effective_workers("eventgen", 4) == 4
    assert lifecycle.effective_workers(None, 3) == 3


def test_provision_rawreplay_forces_single_worker(db_session, settings, fake_driver, tmp_path):
    pack_dir = _write_rawreplay_pack(str(tmp_path))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, pack_dir, name="attack-replay")
    pack.engines_json = ["rawreplay"]
    db_session.flush()
    # A spec that (wrongly) asks for 4 workers: provisioning must clamp to 1.
    spec = _helpers.make_spec(
        db_session, pack, target, name="replay-spec", engine="rawreplay",
        rate_mode="eps", rate_value=500.0, workers=4, fleet="fake-local")
    db_session.commit()

    run = lifecycle.provision_run(db_session, spec, fake_driver, started_by="test")
    db_session.commit()

    leases = lifecycle.get_run_leases(db_session, run)
    assert len(leases) == 1, "rawreplay run must have exactly one lease"
    assert run.spec_snapshot_json["workers"] == 1


def test_run_snapshot_projects_stoker_engine(db_session, settings, tmp_path):
    pack_dir = _write_rawreplay_pack(str(tmp_path))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, pack_dir, name="attack-replay")
    spec = _helpers.make_spec(
        db_session, pack, target, engine="rawreplay", rate_mode="eps",
        rate_value=500.0, workers=1, fleet="fake-local")
    from server.models import Run
    from server import crypto

    run = Run(spec_id=spec.id, jwt_kid=crypto.new_kid(),
              spec_snapshot_json=lifecycle.build_spec_snapshot(spec, target))
    db_session.add(run)
    db_session.flush()

    snap = lifecycle.build_run_snapshot(run, spec, target, "tok", settings=settings, workers=1)
    assert snap.env["STOKER_ENGINE"] == "rawreplay"
    assert snap.env["STOKER_TOTAL_WORKERS"] == "1"
    # HEC token is projected as an env var, never in the slice.
    assert snap.env["STOKER_HEC_TOKEN"] == "tok"


def test_run_snapshot_omits_engine_for_eventgen(db_session, settings, make_pack):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, engine="eventgen",
                              rate_mode="eps", rate_value=500.0, workers=2)
    from server.models import Run
    from server import crypto

    run = Run(spec_id=spec.id, jwt_kid=crypto.new_kid(),
              spec_snapshot_json=lifecycle.build_spec_snapshot(spec, target))
    db_session.add(run)
    db_session.flush()

    snap = lifecycle.build_run_snapshot(run, spec, target, None, settings=settings)
    # eventgen is the worker default: the env stays byte-for-byte unchanged.
    assert "STOKER_ENGINE" not in snap.env


def test_run_spec_rejects_multi_worker_rawreplay(client, db_session, settings, fake_driver, tmp_path):
    pack_dir = _write_rawreplay_pack(str(tmp_path))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, pack_dir, name="attack-replay")
    pack.engines_json = ["rawreplay"]
    db_session.flush()
    spec = _helpers.make_spec(
        db_session, pack, target, engine="rawreplay", rate_mode="eps",
        rate_value=500.0, workers=3, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 409, resp.text
    assert "replay" in resp.text.lower()


def test_run_spec_rawreplay_single_worker_happy_path(client, db_session, settings, fake_driver, tmp_path):
    pack_dir = _write_rawreplay_pack(str(tmp_path))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, pack_dir, name="attack-replay")
    pack.engines_json = ["rawreplay"]
    db_session.flush()
    spec = _helpers.make_spec(
        db_session, pack, target, engine="rawreplay", rate_mode="eps",
        rate_value=500.0, workers=1, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["run_id"]


def test_estimate_rawreplay_reflects_single_worker(client, db_session, settings, tmp_path):
    pack_dir = _write_rawreplay_pack(str(tmp_path))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, pack_dir, name="attack-replay",
                              est_bytes_per_event=120.0)
    spec = _helpers.make_spec(
        db_session, pack, target, engine="rawreplay", rate_mode="eps",
        rate_value=1000.0, workers=4, fleet="fake-local")
    db_session.commit()

    resp = client.get("/api/specs/%d/estimate" % spec.id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Forced to a single worker: the whole rate lands on one worker's share.
    assert body["workers"] == 1
    assert body["per_worker_eps"] == pytest.approx(1000.0)


def test_run_spec_rejects_rawreplay_engine_on_eventgen_pack(client, db_session, settings, fake_driver, make_pack):
    # engine=rawreplay against a plain eventgen pack has no replay config to run.
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack(), name="plain-eventgen")
    spec = _helpers.make_spec(
        db_session, pack, target, engine="rawreplay", rate_mode="eps",
        rate_value=100.0, workers=1, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 422, resp.text
    assert "engine_pack_mismatch" in resp.text


def test_spec_create_accepts_rawreplay_engine(client, db_session, settings, tmp_path):
    pack_dir = _write_rawreplay_pack(str(tmp_path))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, pack_dir, name="attack-replay")
    db_session.commit()

    resp = client.post("/api/specs", json={
        "name": "replay-via-api", "pack_id": pack.id, "target_id": target.id,
        "engine": "rawreplay", "rate_mode": "count_interval", "workers": 1,
        "fleet": "fake-local",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["engine"] == "rawreplay"


def test_spec_create_rejects_unknown_engine(client, db_session, settings, make_pack):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    db_session.commit()

    resp = client.post("/api/specs", json={
        "name": "bad-engine", "pack_id": pack.id, "target_id": target.id,
        "engine": "nonsense", "rate_mode": "eps", "rate_value": 100.0, "workers": 1,
    })
    assert resp.status_code == 422, resp.text
