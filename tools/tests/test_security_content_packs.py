"""Resolver for splunk/security_content attack_data -> rawreplay packs.

Drives ``tools/security_content_packs.py`` against a fixture security_content
checkout (a couple of detection YAMLs shaped exactly like the real repo:
top-level name/id/description/mitre_attack_id and tests[].attack_data[]).
"""
from __future__ import annotations

import os

import pytest

yaml = pytest.importorskip("yaml")  # the parse path needs PyYAML

import security_content_packs as scp  # noqa: E402  (conftest adds tools/ to path)


DET_A = {
    "name": "Detect Rare Executables",
    "id": "44fddcb2-8d3b-454c-874e-7c6de5a4f7ac",
    "description": "The following analytic detects rare processes.\n  Multi-line.",
    "mitre_attack_id": ["T1204", "T1204.002"],
    "data_source": ["Sysmon EventID 1"],
    "tests": [{
        "name": "t",
        "attack_data": [
            {"data": "https://media.githubusercontent.com/media/splunk/attack_data/master/datasets/x/windows-sysmon.log",
             "source": "XmlWinEventLog:Microsoft-Windows-Sysmon/Operational",
             "sourcetype": "XmlWinEventLog"},
            {"data": "https://media.githubusercontent.com/media/splunk/attack_data/master/datasets/x/second.log",
             "source": "aws:cloudtrail", "sourcetype": "aws:cloudtrail"},
        ],
    }],
}
DET_B = {
    "name": "LSASS Access",
    "id": "id-b",
    "description": "creds",
    "mitre_attack_id": "T1003.001",   # a bare string, not a list
    "tests": [{"attack_data": [
        {"data": "https://media.githubusercontent.com/media/splunk/attack_data/master/datasets/y/lsass.log",
         "sourcetype": "XmlWinEventLog"}]}],
}
DET_BAD_URL = {
    "name": "Internal Only",
    "tests": [{"attack_data": [{"data": "http://127.0.0.1/secret.log",
                                "sourcetype": "x"}]}],
}
NO_TESTS = {"name": "No Tests Here", "description": "nope"}


def _write_checkout(tmp_path, docs):
    root = tmp_path / "security_content" / "detections" / "endpoint"
    root.mkdir(parents=True)
    for i, doc in enumerate(docs):
        (root / ("d%d.yml" % i)).write_text(yaml.safe_dump(doc))
    return str(tmp_path / "security_content")


def test_parse_extracts_one_spec_per_attack_data(tmp_path):
    checkout = _write_checkout(tmp_path, [DET_A, DET_B, NO_TESTS])
    specs = scp.parse_security_content(checkout)
    # 2 datasets from A + 1 from B; NO_TESTS yields nothing.
    assert len(specs) == 3
    a = [s for s in specs if s["detection"] == "Detect Rare Executables"]
    assert len(a) == 2
    assert a[0]["mitre"] == ["T1204", "T1204.002"]
    assert a[0]["description"] == "The following analytic detects rare processes. Multi-line."
    b = [s for s in specs if s["detection"] == "LSASS Access"][0]
    assert b["mitre"] == ["T1003.001"]          # bare string normalised to a list
    assert b["source"] is None                   # absent -> None


def test_subset_filters_by_name_mitre_or_source(tmp_path):
    checkout = _write_checkout(tmp_path, [DET_A, DET_B])
    assert len(scp.parse_security_content(checkout, subset=["T1003"])) == 1
    assert len(scp.parse_security_content(checkout, subset=["lsass"])) == 1
    assert len(scp.parse_security_content(checkout, subset=["cloudtrail"])) == 1  # by source/sourcetype
    assert len(scp.parse_security_content(checkout, subset=["nothing-here"])) == 0


def test_materialise_writes_valid_packs(tmp_path):
    checkout = _write_checkout(tmp_path, [DET_A, DET_B])
    out = str(tmp_path / "out")
    specs = scp.parse_security_content(checkout)
    manifest = scp.materialise(specs, out, index="attack")

    assert len(manifest["written"]) == 3
    assert manifest["skipped"] == []
    names = {row["name"] for row in manifest["written"]}
    # unique names even though two datasets share a detection
    assert len(names) == 3
    assert all(n.startswith("sc-") for n in names)

    # Every pack.yaml has the rawreplay shape and a public https dataset_url.
    for name in names:
        text = (tmp_path / "out" / name / "pack.yaml").read_text()
        assert "engine: rawreplay" in text
        assert "dataset_url: https://media.githubusercontent.com/" in text
        assert "index: attack" in text
        assert 'description: "' in text


def test_invalid_dataset_url_is_skipped_not_written(tmp_path):
    checkout = _write_checkout(tmp_path, [DET_A, DET_BAD_URL])
    out = str(tmp_path / "out")
    specs = scp.parse_security_content(checkout)
    manifest = scp.materialise(specs, out, index="main")

    # A loopback http:// URL is refused (never fetched, never written).
    assert any(r["detection"] == "Internal Only" for r in manifest["skipped"])
    assert all(r["detection"] != "Internal Only" for r in manifest["written"])
    assert not os.path.isdir(os.path.join(out, "sc-internal-only"))


def test_description_sanitised_single_line_no_quote_or_hash(tmp_path):
    spec = {"detection": "X", "description": 'has "quotes" and # hash\nand newline',
            "mitre": [], "dataset_url": "https://media.githubusercontent.com/a.log",
            "sourcetype": "st", "source": None}
    y = scp.build_pack_yaml(spec, "sc-x")
    line = [l for l in y.splitlines() if l.startswith("description:")][0]
    assert '"' not in line[len("description: "):].strip()[1:-1]  # no inner quotes
    assert "#" not in line
    assert "\n" not in spec["description"] or line.count("\n") == 0
