"""Validate the shipped packs against the worker contract.

The conf files are parsed exactly as the worker's rewriter does
(RawConfigParser with case-preserving optionxform).
"""
from __future__ import annotations

import configparser
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKS = REPO_ROOT / "packs"


def load_conf(pack):
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    path = PACKS / pack / "default" / "eventgen.conf"
    assert parser.read(path), "missing %s" % path
    return parser


def read_sample(pack, name):
    lines = (PACKS / pack / "samples" / name).read_text().splitlines()
    assert lines, "empty sample %s/%s" % (pack, name)
    return lines


# ---- flatline ----

def test_flatline_single_sample_mode_stanza():
    conf = load_conf("flatline")
    assert conf.sections() == ["flatline.sample"]
    stanza = dict(conf.items("flatline.sample"))
    assert stanza["mode"] == "sample"
    assert stanza["interval"] == "1"
    assert stanza["count"] == "100"
    assert stanza["earliest"] == "-1s"
    assert stanza["latest"] == "now"


def test_flatline_has_no_randomisation_or_rate_maps():
    stanza = dict(load_conf("flatline").items("flatline.sample"))
    for banned in ("randomizeCount", "hourOfDayRate", "dayOfWeekRate"):
        assert banned not in stanza


def test_flatline_has_no_output_side_keys():
    stanza = dict(load_conf("flatline").items("flatline.sample"))
    for banned in ("outputMode", "index", "sourcetype", "source", "host",
                   "splunkHost", "splunkPort", "splunkMethod"):
        assert banned not in stanza, "%s must be stamped by the agent" % banned


def test_flatline_timestamp_token_matches_every_line():
    stanza = dict(load_conf("flatline").items("flatline.sample"))
    assert stanza["token.0.replacementType"] == "timestamp"
    pattern = re.compile(stanza["token.0.token"])
    for line in read_sample("flatline", "flatline.sample"):
        assert pattern.search(line), "timestamp token missed: %s" % line


def test_flatline_sample_lines_near_120_bytes():
    lines = read_sample("flatline", "flatline.sample")
    assert len(lines) >= 15
    sizes = [len(line.encode("utf-8")) for line in lines]
    mean = sum(sizes) / len(sizes)
    assert 108 <= mean <= 132, "mean %.1f outside 120 +/- 10%%" % mean
    assert all(size > 0 for size in sizes)


# ---- apigw ----

def test_apigw_stanza_shape():
    conf = load_conf("apigw")
    assert conf.sections() == ["apigw.sample"]
    stanza = dict(conf.items("apigw.sample"))
    assert stanza["mode"] == "sample"
    assert stanza["randomizeCount"] == "0.2"


def test_apigw_hour_of_day_rate_full_24_keys():
    stanza = dict(load_conf("apigw").items("apigw.sample"))
    rates = json.loads(stanza["hourOfDayRate"])
    assert set(rates.keys()) == {str(h) for h in range(24)}
    assert all(isinstance(v, (int, float)) and v > 0 for v in rates.values())
    assert max(rates.values()) == 1.0


def test_apigw_three_tokens():
    stanza = dict(load_conf("apigw").items("apigw.sample"))
    assert stanza["token.0.replacementType"] == "timestamp"
    assert stanza["token.1.replacementType"] == "random"
    assert stanza["token.1.replacement"] == "ipv4"
    assert stanza["token.2.replacementType"] == "file"
    assert stanza["token.2.replacement"].endswith("samples/status_codes.sample")


def test_apigw_tokens_match_every_sample_line():
    stanza = dict(load_conf("apigw").items("apigw.sample"))
    patterns = [re.compile(stanza["token.%d.token" % i]) for i in range(3)]
    for line in read_sample("apigw", "apigw.sample"):
        for pattern in patterns:
            assert pattern.search(line), "token /%s/ missed: %s" % (pattern.pattern, line)


def test_apigw_srcip_values_are_valid_ipv4():
    stanza = dict(load_conf("apigw").items("apigw.sample"))
    pattern = re.compile(stanza["token.1.token"])
    for line in read_sample("apigw", "apigw.sample"):
        octets = pattern.search(line).group(1).split(".")
        assert len(octets) == 4
        assert all(0 <= int(o) <= 255 for o in octets)


def test_apigw_sample_lines_near_380_bytes():
    lines = read_sample("apigw", "apigw.sample")
    assert len(lines) >= 15
    sizes = [len(line.encode("utf-8")) for line in lines]
    mean = sum(sizes) / len(sizes)
    assert 342 <= mean <= 418, "mean %.1f outside 380 +/- 10%%" % mean


def test_apigw_status_codes_weighted_mix():
    lines = read_sample("apigw", "status_codes.sample")
    assert all(re.fullmatch(r"[1-5]\d{2}", line) for line in lines)
    counts = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    # 200 dominates; errors present but rare, as in real gateway traffic
    assert counts["200"] / len(lines) > 0.4
    error_share = sum(v for k, v in counts.items() if k >= "400") / len(lines)
    assert 0 < error_share < 0.4
    assert any(k.startswith("5") for k in counts)


def test_pack_yaml_estimates_present():
    # Minimal structural check without a YAML dependency: these keys are
    # what the worker's per_day_gb conversion reads.
    flat = (PACKS / "flatline" / "pack.yaml").read_text()
    assert "name: flatline" in flat
    assert "bytes_per_event: 120" in flat
    assert "index: main" in flat
    assert "sourcetype: stoker:flatline" in flat
    apigw = (PACKS / "apigw" / "pack.yaml").read_text()
    assert "name: api-gateway" in apigw
    assert "bytes_per_event: 380" in apigw
