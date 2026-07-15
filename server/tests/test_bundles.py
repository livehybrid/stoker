"""Bundle builder + pack linter tests.

The bundle is content-addressed: an identical pack must produce a byte-identical
reproducible tarball, hence an identical sha256 digest, hence a dedup (the row
is reused, not rebuilt). The worker fetches ``<digest>.tgz`` and verifies the
sha256, and its ``_find_pack_root`` accepts the pack one level down, so the
archive must unpack to ``<pack>/default/eventgen.conf``. The linter must pass a
well-formed pack and flag the specific breakages the contract lists (missing
conf, missing sample file, bad token regex, no stanzas).
"""

from __future__ import annotations

import hashlib
import os
import tarfile

import pytest

from server.bundles import (
    BundleError,
    build_from_pack,
    build_stoker_manifest,
    build_tarball_bytes,
    lint_pack,
)


# --------------------------------------------------------------------------- #
# Local pack builders (broken variants for the linter).
# --------------------------------------------------------------------------- #

def _write_good_pack(root, name="goodpack", count=100, bytes_per_event=120):
    # type: (str, str, int, int) -> str
    pack_dir = os.path.join(root, name)
    os.makedirs(os.path.join(pack_dir, "default"))
    os.makedirs(os.path.join(pack_dir, "samples"))
    conf = (
        "[%s.sample]\n"
        "mode = sample\n"
        "interval = 1\n"
        "count = %d\n"
        "token.0.token = \\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\n"
        "token.0.replacementType = timestamp\n"
        "token.0.replacement = %%Y-%%m-%%dT%%H:%%M:%%S\n"
    ) % (name, count)
    with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w") as fh:
        fh.write(conf)
    with open(os.path.join(pack_dir, "samples", "%s.sample" % name), "w") as fh:
        fh.write("\n".join("2026-01-01T00:00:%02d line %d" % (i % 60, i)
                           for i in range(20)) + "\n")
    with open(os.path.join(pack_dir, "pack.yaml"), "w") as fh:
        fh.write("name: %s\nengine: eventgen\nestimates:\n  bytes_per_event: %d\n"
                 % (name, bytes_per_event))
    return pack_dir


# --------------------------------------------------------------------------- #
# Lint: good pack.
# --------------------------------------------------------------------------- #

def test_lint_good_pack(tmp_path):
    pack = _write_good_pack(str(tmp_path))
    result = lint_pack(pack)
    assert result.ok
    assert result.errors == []
    assert result.stanza_count == 1
    assert result.stanzas == ["goodpack.sample"]
    assert result.engines == ["eventgen"]
    assert result.declared_bytes_per_event == pytest.approx(120.0)
    assert result.est_bytes_per_event == pytest.approx(120.0)


def test_lint_measures_bytes_per_event_when_undeclared(tmp_path):
    # No pack.yaml estimate -> the linter measures from the sample lines.
    pack_dir = os.path.join(str(tmp_path), "nometa")
    os.makedirs(os.path.join(pack_dir, "default"))
    os.makedirs(os.path.join(pack_dir, "samples"))
    with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w") as fh:
        fh.write("[nometa.sample]\nmode = sample\ninterval = 1\ncount = 10\n")
    with open(os.path.join(pack_dir, "samples", "nometa.sample"), "w") as fh:
        fh.write("abcdefghij\nabcdefghij\n")  # 10 bytes/line
    result = lint_pack(pack_dir)
    assert result.ok
    assert result.declared_bytes_per_event is None
    assert result.est_bytes_per_event == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# Lint: directory metric packs (metricgen in stoker.json).
# --------------------------------------------------------------------------- #

def _write_metric_pack(root, name="mpack", metricgen=None):
    # type: (str, str, dict) -> str
    import json as _json

    if metricgen is None:
        metricgen = {
            "resolution_s": 10,
            "dimensions": [{"key": "svc", "values": ["a", "b"]}],
            "metrics": [{"name": "req.count", "kind": "count",
                         "min": 1, "p95": 50, "max": 100,
                         "pattern": {"type": "sine"}}],
        }
    pack_dir = os.path.join(root, name)
    os.makedirs(os.path.join(pack_dir, "default"))
    with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w") as fh:
        fh.write("[stub]\nmode = sample\n")
    with open(os.path.join(pack_dir, "stoker.json"), "w") as fh:
        _json.dump({"name": name, "engine": "metrics", "metricgen": metricgen}, fh)
    with open(os.path.join(pack_dir, "pack.yaml"), "w") as fh:
        fh.write("name: %s\nengine: metrics\n" % name)
    return pack_dir


def test_lint_directory_metrics_pack(tmp_path):
    from server.bundles import is_metrics_pack

    pack = _write_metric_pack(str(tmp_path))
    assert is_metrics_pack(pack)
    result = lint_pack(pack)
    assert result.ok, result.errors
    assert result.engines == ["metrics"]
    assert result.stanza_count == 0
    # the validated metricgen rides back so the indexer can store it as the
    # pack's builder config (first-class, like a UI-authored metric pack).
    assert result.metricgen is not None
    assert len(result.metricgen["metrics"]) == 1


def test_lint_directory_metrics_pack_bad_config(tmp_path):
    # min > max must fail the metricgen lint (surfaced through lint_pack).
    bad = {
        "resolution_s": 10,
        "dimensions": [{"key": "svc", "values": ["a"]}],
        "metrics": [{"name": "x", "kind": "gauge", "min": 100, "p95": 5, "max": 1}],
    }
    result = lint_pack(_write_metric_pack(str(tmp_path), "badm", bad))
    assert not result.ok
    assert result.metricgen is None  # not returned when invalid


def test_lint_metrics_engine_without_metricgen_fails(tmp_path):
    # pack.yaml says engine: metrics but there is no metricgen block.
    pack_dir = os.path.join(str(tmp_path), "nogen")
    os.makedirs(os.path.join(pack_dir, "default"))
    with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w") as fh:
        fh.write("[stub]\nmode = sample\n")
    with open(os.path.join(pack_dir, "pack.yaml"), "w") as fh:
        fh.write("name: nogen\nengine: metrics\n")
    result = lint_pack(pack_dir)
    assert not result.ok
    assert any("metricgen" in e for e in result.errors)


# --------------------------------------------------------------------------- #
# Lint: broken packs (each specific failure).
# --------------------------------------------------------------------------- #

def test_lint_missing_directory(tmp_path):
    result = lint_pack(os.path.join(str(tmp_path), "does-not-exist"))
    assert not result.ok
    assert any("not found" in e for e in result.errors)


def test_lint_missing_conf(tmp_path):
    pack_dir = os.path.join(str(tmp_path), "noconf")
    os.makedirs(pack_dir)
    result = lint_pack(pack_dir)
    assert not result.ok
    assert any("eventgen.conf" in e for e in result.errors)


def test_lint_missing_sample_file(tmp_path):
    pack_dir = os.path.join(str(tmp_path), "nosample")
    os.makedirs(os.path.join(pack_dir, "default"))
    with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w") as fh:
        fh.write("[orphan.sample]\nmode = sample\ninterval = 1\ncount = 10\n")
    result = lint_pack(pack_dir)
    assert not result.ok
    assert any("sample file" in e and "orphan.sample" in e for e in result.errors)


def test_lint_bad_token_regex(tmp_path):
    pack_dir = os.path.join(str(tmp_path), "badregex")
    os.makedirs(os.path.join(pack_dir, "default"))
    os.makedirs(os.path.join(pack_dir, "samples"))
    with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w") as fh:
        # An unbalanced group is an invalid regex.
        fh.write("[badregex.sample]\nmode = sample\ninterval = 1\ncount = 1\n"
                 "token.0.token = (unterminated\n")
    with open(os.path.join(pack_dir, "samples", "badregex.sample"), "w") as fh:
        fh.write("line\n")
    result = lint_pack(pack_dir)
    assert not result.ok
    assert any("bad regex" in e for e in result.errors)


def test_lint_no_stanzas(tmp_path):
    pack_dir = os.path.join(str(tmp_path), "emptyconf")
    os.makedirs(os.path.join(pack_dir, "default"))
    with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w") as fh:
        fh.write("# a comment, no stanzas\n")
    result = lint_pack(pack_dir)
    assert not result.ok
    assert any("no sample stanzas" in e for e in result.errors)


# --------------------------------------------------------------------------- #
# Build: determinism, dedup, content-addressing, unpack shape.
# --------------------------------------------------------------------------- #

def test_build_digest_is_content_addressed(settings, tmp_path):
    pack = _write_good_pack(str(tmp_path))
    built = build_from_pack(pack, bundle_dir=settings.bundle_dir)
    assert len(built.digest) == 64
    assert not built.reused
    with open(built.path, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == built.digest


def test_build_is_reproducible_byte_identical(tmp_path):
    # Two builds of the same pack produce byte-identical tarballs (fixed mtime /
    # uid / gid / mode + zeroed gzip header), so the digest is stable.
    pack = _write_good_pack(str(tmp_path))
    lint = lint_pack(pack)
    manifest = build_stoker_manifest(pack, lint)
    a = build_tarball_bytes(pack, manifest)
    b = build_tarball_bytes(pack, manifest)
    assert a == b
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_build_dedup_reuses_on_second_build(settings, tmp_path):
    pack = _write_good_pack(str(tmp_path))
    first = build_from_pack(pack, bundle_dir=settings.bundle_dir)
    second = build_from_pack(pack, bundle_dir=settings.bundle_dir)
    assert second.digest == first.digest
    assert second.reused
    assert second.path == first.path


def test_build_different_content_different_digest(settings, tmp_path):
    a_dir = _write_good_pack(str(tmp_path), name="packa", count=100)
    b_dir = _write_good_pack(str(tmp_path), name="packb", count=999)
    a = build_from_pack(a_dir, bundle_dir=settings.bundle_dir)
    b = build_from_pack(b_dir, bundle_dir=settings.bundle_dir)
    assert a.digest != b.digest


def test_build_rejects_broken_pack(settings, tmp_path):
    pack_dir = os.path.join(str(tmp_path), "broken")
    os.makedirs(pack_dir)  # no conf -> lint fails -> build refuses
    with pytest.raises(BundleError):
        build_from_pack(pack_dir, bundle_dir=settings.bundle_dir)


def test_bundle_unpacks_to_worker_pack_root(settings, tmp_path):
    pack = _write_good_pack(str(tmp_path))
    built = build_from_pack(pack, bundle_dir=settings.bundle_dir)
    dest = os.path.join(str(tmp_path), "unpacked")
    os.makedirs(dest)
    with tarfile.open(built.path, "r:*") as tar:
        tar.extractall(dest, filter="data")
    # <pack>/default/eventgen.conf exists exactly one level down.
    found = [
        entry for entry in os.listdir(dest)
        if os.path.isfile(os.path.join(dest, entry, "default", "eventgen.conf"))
    ]
    assert found, "bundle did not unpack to <pack>/default/eventgen.conf"
    # The manifest is present too.
    assert os.path.isfile(os.path.join(dest, found[0], "stoker.json"))


def test_manifest_carries_name_engine_estimates(tmp_path):
    pack = _write_good_pack(str(tmp_path), name="mp", bytes_per_event=140)
    manifest = build_stoker_manifest(pack, lint_pack(pack))
    assert manifest["name"] == "mp"
    assert manifest["engine"] == "eventgen"
    assert manifest["estimates"]["bytes_per_event"] == pytest.approx(140.0)
    assert manifest["stanzas"] == ["mp.sample"]
