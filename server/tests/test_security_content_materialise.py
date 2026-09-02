"""Cross-check: a pack materialised from security_content lints as a valid
rawreplay pack under the REAL server linter.

The tools/ resolver (``security_content_packs``) builds pack.yaml as pure text;
this test builds one from a representative spec and runs the actual
``server.bundles.lint_pack`` over it, so the emitted format is validated by the
same linter the control plane uses at registration — not a stand-in. Importing
the tool needs no PyYAML (``build_pack_yaml`` is pure; only the checkout parse
lazy-imports yaml), so this runs in the server test environment.
"""
from __future__ import annotations

import os
import sys

from server import bundles

# Make tools/security_content_packs importable (server conftest only adds worker/).
_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import security_content_packs as scp  # noqa: E402


_SPEC = {
    "detection": "Detect Rare Executables",
    "detection_id": "44fddcb2-8d3b-454c-874e-7c6de5a4f7ac",
    "description": 'Rare process execution; has "quotes", a # hash, and commas.',
    "mitre": ["T1204", "T1204.002"],
    "dataset_url": ("https://media.githubusercontent.com/media/splunk/attack_data/"
                    "master/datasets/attack_techniques/T1204/rare/windows-sysmon.log"),
    "source": "XmlWinEventLog:Microsoft-Windows-Sysmon/Operational",
    "sourcetype": "XmlWinEventLog",
}


def test_materialised_pack_lints_ok(tmp_path):
    pack_dir = tmp_path / "sc-detect-rare-executables"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        scp.build_pack_yaml(_SPEC, "sc-detect-rare-executables", index="attack"))

    res = bundles.lint_pack(str(pack_dir))

    assert res.ok, res.errors
    assert res.engines == ["rawreplay"]
    replay = res.replay or {}
    # The URL survives the flat parser (colons in https:// intact) and will be
    # fetched at build time through the SSRF-safe path (fetch_url set).
    assert replay.get("dataset_url") == _SPEC["dataset_url"]
    assert replay.get("fetch_url") == _SPEC["dataset_url"]
    # source keeps its internal colon; sourcetype extracted; description survived
    # the quote/hash sanitising (no lint break).
    assert replay.get("source") == _SPEC["source"]
    assert replay.get("sourcetype") == _SPEC["sourcetype"]


def test_materialised_pack_is_detected_as_rawreplay(tmp_path):
    pack_dir = tmp_path / "sc-x"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(scp.build_pack_yaml(_SPEC, "sc-x"))
    assert bundles.is_rawreplay_pack(str(pack_dir))
