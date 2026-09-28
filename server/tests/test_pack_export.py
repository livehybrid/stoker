"""Pack export (``GET /api/packs/{id}/export``) — the mirror of pack upload.

The contract under test is a **round trip**: whatever this endpoint hands back
must be exactly what ``POST /api/packs/upload`` accepts on another instance, and
the pack must arrive with everything it needs — including, for a rawreplay pack,
its dataset, so it replays on a control plane with no internet access.

The security suite here guards one specific escalation. ``POST /api/packs``
registers an **arbitrary operator-supplied** ``source_path``, so a pack row can
point at ``/`` or ``/app``. Export therefore ships a fixed allowlist of
pack-relative files, never a directory walk: a hostile row must not turn "may
register a path" into "may download the control plane's master key". The same
symlink / path-escape guards the bundle builder applies are asserted too, since
a pack synced from an untrusted git repo could otherwise smuggle a host file out
through the archive.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import tarfile

import pytest

from server import bundles
from server import config as config_mod
from server import packexport, packupload
from server.models import Pack


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _names(data):
    # type: (bytes) -> list
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        return sorted(tf.getnames())


def _member(data, arcname):
    # type: (bytes, str) -> bytes
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        fh = tf.extractfile(arcname)
        assert fh is not None, arcname
        return fh.read()


def _reimport(data, tmp_path, settings, sub="in"):
    # type: (bytes, object, object, str) -> str
    """Extract an export through the REAL upload path and return the pack dir."""
    dest = tmp_path / sub
    dest.mkdir(exist_ok=True)
    return packupload.store_uploaded_pack(
        data, str(dest), packupload.UploadLimits.from_settings(settings))


@pytest.fixture()
def replay_pack(tmp_path):
    # type: (object) -> object
    """Factory for a rawreplay pack, with a local dataset or a dataset_url."""
    def _make(name="replay-test", url=None):
        pack = tmp_path / name
        (pack / "default").mkdir(parents=True)
        (pack / "default" / "eventgen.conf").write_text(
            "[%s]\nmode = replay\nsampleFile = dataset/events.log\ntimeMultiple = 1.0\n" % name,
            encoding="utf-8")
        lines = ["name: %s" % name, "engine: rawreplay", "replay:"]
        if url:
            lines.append("  dataset_url: %s" % url)
        else:
            (pack / "dataset").mkdir()
            (pack / "dataset" / "events.log").write_text(
                "".join("2026-01-01T00:00:%02dZ line %d\n" % (i, i) for i in range(10)),
                encoding="utf-8")
            lines.append("  dataset: dataset/events.log")
        lines += ["  mode: rate", "  time_multiple: 1.0",
                  "estimates:", "  bytes_per_event: 30", ""]
        (pack / "pack.yaml").write_text("\n".join(lines), encoding="utf-8")
        return str(pack)

    return _make


METRICGEN = {
    "resolution_s": 30,
    "dimensions": [{"key": "service", "values": ["api", "web"]}],
    "metrics": [{"name": "requests", "kind": "count", "min": 1, "p95": 50, "max": 90,
                 "pattern": {"type": "business_double_hump"}}],
}


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #

def test_export_round_trips_through_the_upload_path(make_pack, tmp_path, settings):
    pack_dir = make_pack("round-trip")
    before = bundles.lint_pack(pack_dir)
    result = packexport.export_pack_bytes(pack_dir, "round-trip", settings=settings)

    assert result.filename == "round-trip.tar.gz"
    # One top-level directory named after the pack: an upload-accepted shape.
    assert _names(result.data) == [
        "round-trip/default/eventgen.conf",
        "round-trip/pack.yaml",
        "round-trip/samples/round-trip.sample",
    ]

    back = _reimport(result.data, tmp_path, settings)
    after = bundles.lint_pack(back)
    assert after.ok, after.errors
    assert (after.stanzas, after.engines, after.stanza_count) == (
        before.stanzas, before.engines, before.stanza_count)
    assert after.est_bytes_per_event == before.est_bytes_per_event
    # And it still builds a bundle on the far side.
    assert bundles.build_from_pack(back, bundle_dir=str(tmp_path / "bundles"),
                                   settings=settings).size_bytes > 0


def test_export_is_reproducible(make_pack, settings):
    pack_dir = make_pack("repro")
    a = packexport.export_pack_bytes(pack_dir, "repro", settings=settings).data
    b = packexport.export_pack_bytes(pack_dir, "repro", settings=settings).data
    assert a == b, "two exports of an unchanged pack must be byte-identical"


def test_export_carries_the_builder_config_so_the_pack_stays_editable(
        make_pack, tmp_path, settings):
    # A pack-builder pack keeps stoker-builder.json: the point is that the
    # RECEIVING instance can reopen and rebuild it in its own builder.
    pack_dir = make_pack("built-here")
    cfg = {"name": "built-here", "events": ["a b c"], "tokens": [], "count": 1,
           "interval": 1, "order": "sequential", "builder_version": 1}
    with open(os.path.join(pack_dir, "stoker-builder.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)

    data = packexport.export_pack_bytes(pack_dir, "built-here", settings=settings).data
    assert "built-here/stoker-builder.json" in _names(data)

    back = _reimport(data, tmp_path, settings)
    from server import packbuilder
    assert packbuilder.read_builder_config(back)["name"] == "built-here"


def test_pack_name_becomes_a_safe_archive_directory(make_pack, settings):
    # The pack name is free text; it must not escape or break the archive.
    pack_dir = make_pack("odd")
    data = packexport.export_pack_bytes(pack_dir, "../../etc/pass wd!", settings=settings).data
    top = {n.split("/")[0] for n in _names(data)}
    assert top == {"etc-pass-wd"}
    assert packexport.safe_basename("///") == "pack"


# --------------------------------------------------------------------------- #
# Security: only pack-shaped files, no links, no escapes
# --------------------------------------------------------------------------- #

def test_export_ships_only_pack_shaped_files(make_pack, settings):
    """A pack row may point anywhere, so export must never sweep a directory.

    Registering ``/`` and downloading it would be an arbitrary-file read; the
    allowlist means even a hostile row yields only these specific names.
    """
    pack_dir = make_pack("tight")
    for junk in (".env", "secret.txt", "id_rsa", "master.key"):
        with open(os.path.join(pack_dir, junk), "w", encoding="utf-8") as fh:
            fh.write("SUPER-SECRET")
    os.makedirs(os.path.join(pack_dir, "private"))
    with open(os.path.join(pack_dir, "private", "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("SUPER-SECRET")

    data = packexport.export_pack_bytes(pack_dir, "tight", settings=settings).data
    assert b"SUPER-SECRET" not in data
    for name in _names(data):
        rel = name.split("/", 1)[1]
        assert rel in packexport.EXPORT_RELPATHS or rel.startswith("samples/"), rel


def test_export_refuses_symlinks_and_paths_escaping_the_pack(make_pack, tmp_path, settings):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("HOST-FILE", encoding="utf-8")
    pack_dir = make_pack("linky")
    os.symlink(str(outside), os.path.join(pack_dir, "samples", "leak.sample"))
    os.symlink(str(outside), os.path.join(pack_dir, "pack.yaml.link"))
    os.symlink(str(tmp_path), os.path.join(pack_dir, "samples", "outdir"))

    data = packexport.export_pack_bytes(pack_dir, "linky", settings=settings).data
    assert b"HOST-FILE" not in data
    assert not any("leak" in n or "outdir" in n for n in _names(data))


def test_export_needs_something_pack_shaped(tmp_path, settings):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(packexport.PackExportError) as exc:
        packexport.export_pack_bytes(str(empty), "empty", settings=settings)
    assert "no exportable files" in str(exc.value)
    with pytest.raises(packexport.PackExportError):
        packexport.export_pack_bytes(str(tmp_path / "nope"), "nope", settings=settings)


# --------------------------------------------------------------------------- #
# rawreplay: the dataset has to travel or the pack is useless offline
# --------------------------------------------------------------------------- #

def test_local_dataset_travels_with_the_pack(replay_pack, tmp_path, settings):
    pack_dir = replay_pack("local-ds")
    result = packexport.export_pack_bytes(pack_dir, "local-ds", settings=settings)
    assert result.dataset_embedded
    assert "local-ds/dataset/events.log" in _names(result.data)

    back = _reimport(result.data, tmp_path, settings)
    assert bundles.lint_pack(back).ok
    assert bundles.parse_replay_config(back)[0]["dataset"] == "dataset/events.log"


def test_dataset_url_is_fetched_embedded_and_wired_into_pack_yaml(
        replay_pack, tmp_path, settings, monkeypatch):
    payload = b"".join(b"2026-01-01T00:00:00Z fetched %d\n" % i for i in range(50))
    monkeypatch.setattr(bundles, "_fetch_dataset_url",
                        lambda url, **kw: payload)
    pack_dir = replay_pack("url-ds", url="https://example.com/events.log.gz")

    result = packexport.export_pack_bytes(pack_dir, "url-ds", settings=settings)
    assert result.dataset_embedded
    assert "url-ds/dataset/replay.dat" in _names(result.data)
    assert _member(result.data, "url-ds/dataset/replay.dat") == payload
    # pack.yaml now names the local copy; the URL stays as provenance.
    yaml_text = _member(result.data, "url-ds/pack.yaml").decode()
    assert "dataset: dataset/replay.dat" in yaml_text
    assert "dataset_url: https://example.com/events.log.gz" in yaml_text

    # The re-imported pack builds its bundle with NO fetch (the air-gap case):
    # make any fetch attempt fail loudly, then build.
    back = _reimport(result.data, tmp_path, settings)
    replay = bundles.parse_replay_config(back)[0]
    assert replay["dataset"] == "dataset/replay.dat" and replay["fetch_url"] is None
    monkeypatch.setattr(bundles, "_fetch_dataset_url",
                        lambda *a, **kw: pytest.fail("must not fetch on the far side"))
    assert bundles.build_from_pack(back, bundle_dir=str(tmp_path / "b2"),
                                   settings=settings).size_bytes > 0


def test_dataset_can_be_skipped_for_a_quick_copy(replay_pack, settings, monkeypatch):
    monkeypatch.setattr(bundles, "_fetch_dataset_url",
                        lambda *a, **kw: pytest.fail("must not fetch when opted out"))
    pack_dir = replay_pack("url-skip", url="https://example.com/events.log")
    result = packexport.export_pack_bytes(pack_dir, "url-skip", include_dataset=False,
                                          settings=settings)
    assert not result.dataset_embedded
    assert not any("dataset" in n for n in _names(result.data))


def test_a_failed_dataset_fetch_is_an_operator_facing_refusal(
        replay_pack, settings, monkeypatch):
    def _boom(url, **kw):
        raise bundles.BundleError("dataset fetch %s returned HTTP 404" % url)

    monkeypatch.setattr(bundles, "_fetch_dataset_url", _boom)
    pack_dir = replay_pack("url-bad", url="https://example.com/gone.log")
    with pytest.raises(packexport.PackExportError) as exc:
        packexport.export_pack_bytes(pack_dir, "url-bad", settings=settings)
    assert "HTTP 404" in str(exc.value)


# --------------------------------------------------------------------------- #
# Caps: refuse rather than hand back something the far end rejects
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("field,msg", [
    ("pack_upload_max_member_bytes", "per-file upload limit"),
    ("pack_upload_max_total_bytes", "unpacks to"),
    ("pack_upload_max_members", "member upload limit"),
])
def test_export_refuses_what_the_receiving_instance_would_refuse(
        make_pack, settings, field, msg):
    pack_dir = make_pack("capped")
    tiny = dataclasses.replace(config_mod.get_settings(), **{field: 1})
    with pytest.raises(packexport.PackExportError) as exc:
        packexport.export_pack_bytes(pack_dir, "capped", settings=tiny)
    assert msg in str(exc.value)


# --------------------------------------------------------------------------- #
# Metric packs have no directory: the archive is synthesised
# --------------------------------------------------------------------------- #

def test_metric_pack_export_round_trips_its_metricgen(tmp_path, settings):
    result = packexport.export_metric_pack_bytes(
        "my-metrics", METRICGEN, description="counts per service", tags=["demo"])
    assert sorted(_names(result.data)) == ["my-metrics/pack.yaml", "my-metrics/stoker.json"]

    back = _reimport(result.data, tmp_path, settings)
    lint = bundles.lint_pack(back)
    assert lint.ok, lint.errors
    assert lint.engines == [bundles.METRICS_ENGINE]
    # The config is what makes it a metric pack; it must survive exactly, so the
    # far side stores it as the pack's builder config and can edit it.
    assert lint.metricgen == METRICGEN
    from server.gitsync import local_pack_metadata
    meta = local_pack_metadata(back)
    assert meta["name"] == "my-metrics" and meta["tags"] == ["demo"]
    assert meta["description"] == "counts per service"


def test_metric_pack_export_refuses_an_invalid_config():
    with pytest.raises(packexport.PackExportError) as exc:
        packexport.export_metric_pack_bytes("bad", {"resolution_s": 30, "metrics": []})
    assert "would not import" in str(exc.value)


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #

def test_api_export_endpoint(client, db_session, make_pack, settings):
    pack_dir = make_pack("api-export")
    lint = bundles.lint_pack(pack_dir)
    pack = Pack(name="api-export", source_path=pack_dir, engines_json=lint.engines,
                stanza_count=lint.stanza_count, verified=lint.ok, lint_status="ok")
    db_session.add(pack)
    db_session.commit()

    r = client.get("/api/packs/%d/export" % pack.id)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/gzip"
    assert r.headers["content-disposition"] == 'attachment; filename="api-export.tar.gz"'
    assert r.headers["x-stoker-dataset-embedded"] == "false"
    assert int(r.headers["x-stoker-export-members"]) == 3
    assert "api-export/pack.yaml" in _names(r.content)

    assert client.get("/api/packs/999999/export").status_code == 404


def test_api_export_reports_a_missing_directory_as_409(client, db_session, tmp_path):
    pack = Pack(name="ghost", source_path=str(tmp_path / "gone"), lint_status="ok")
    db_session.add(pack)
    db_session.commit()
    r = client.get("/api/packs/%d/export" % pack.id)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "pack_export_failed"


def test_api_export_of_a_repo_pack_uses_the_pinned_sha(
        client, db_session, make_pack, monkeypatch):
    """A repo pack exports the tree its runs use, not the branch head."""
    from server.models import Repo
    from server.routes import api as api_routes

    pack_dir = make_pack("repo-pack")
    repo = Repo(url="https://example.com/packs.git", auth_kind="none")
    db_session.add(repo)
    db_session.commit()
    pack = Pack(name="repo-pack", source_path="packs/repo-pack", repo_id=repo.id,
                indexed_sha="a" * 40, lint_status="ok")
    db_session.add(pack)
    db_session.commit()
    seen = {}

    def _fake_resolve(r, p, settings=None):
        # The pinned SHA is what the bundle builder would use for this pack.
        seen["sha"] = p.indexed_sha
        seen["repo"] = r.id
        return pack_dir

    monkeypatch.setattr(api_routes.gitsync, "resolve_pack_dir", _fake_resolve)
    r = client.get("/api/packs/%d/export" % pack.id)
    assert r.status_code == 200, r.text
    assert seen == {"sha": "a" * 40, "repo": repo.id}
    assert "repo-pack/pack.yaml" in _names(r.content)


def test_api_export_surfaces_a_git_failure_as_409(
        client, db_session, monkeypatch):
    from server import gitsync
    from server.models import Repo
    from server.routes import api as api_routes

    repo = Repo(url="https://example.com/packs.git", auth_kind="none")
    db_session.add(repo)
    db_session.commit()
    pack = Pack(name="stale", source_path="packs/stale", repo_id=repo.id,
                indexed_sha="b" * 40, lint_status="ok")
    db_session.add(pack)
    db_session.commit()

    def _boom(r, p, settings=None):
        raise gitsync.GitSyncError("repo %s clone is missing" % r.id)

    monkeypatch.setattr(api_routes.gitsync, "resolve_pack_dir", _boom)
    r = client.get("/api/packs/%d/export" % pack.id)
    assert r.status_code == 409
    assert "clone is missing" in r.json()["detail"]["detail"]
