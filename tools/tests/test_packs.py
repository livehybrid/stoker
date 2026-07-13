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


# --------------------------------------------------------------------------- #
# Shared helpers for the larger, more-varied packs. Each of these packs has one
# sample-mode stanza with three tokens; the token regexes must match every
# sample line (a worker rewrites group 1 in place) and the sourcetype must be the
# Splunk-native one so the events land under the right props/transforms.
# --------------------------------------------------------------------------- #

def _single_stanza(pack, section):
    conf = load_conf(pack)
    assert conf.sections() == [section]
    return dict(conf.items(section))


def _assert_tokens_match_every_line(stanza, pack, sample_name, ntokens):
    patterns = [re.compile(stanza["token.%d.token" % i]) for i in range(ntokens)]
    for line in read_sample(pack, sample_name):
        for pattern in patterns:
            assert pattern.search(line), "token /%s/ missed: %s" % (pattern.pattern, line)


def _assert_mean_bytes(pack, sample_name, target, tol=0.10, min_lines=100):
    lines = read_sample(pack, sample_name)
    assert len(lines) >= min_lines
    sizes = [len(line.encode("utf-8")) for line in lines]
    mean = sum(sizes) / len(sizes)
    lo, hi = target * (1 - tol), target * (1 + tol)
    assert lo <= mean <= hi, "mean %.1f outside %d +/- %d%%" % (mean, target, int(tol * 100))
    assert all(size > 0 for size in sizes)


# ---- web-access (NCSA combined, access_combined) ----

def test_web_access_stanza_and_diurnal_curve():
    stanza = _single_stanza("web-access", "web_access.sample")
    assert stanza["mode"] == "sample"
    assert stanza["randomizeCount"] == "0.2"
    rates = json.loads(stanza["hourOfDayRate"])
    assert set(rates.keys()) == {str(h) for h in range(24)}
    assert max(rates.values()) == 1.0
    assert all(v > 0 for v in rates.values())


def test_web_access_tokens_and_status_mix():
    stanza = _single_stanza("web-access", "web_access.sample")
    assert stanza["token.0.replacementType"] == "timestamp"
    assert stanza["token.1.replacementType"] == "random"
    assert stanza["token.1.replacement"] == "ipv4"
    assert stanza["token.2.replacementType"] == "file"
    assert stanza["token.2.replacement"].endswith("samples/status_codes.sample")
    _assert_tokens_match_every_line(stanza, "web-access", "web_access.sample", 3)
    # Leading client IP is a valid dotted-quad on every line.
    ip_re = re.compile(stanza["token.1.token"])
    for line in read_sample("web-access", "web_access.sample"):
        octets = ip_re.search(line).group(1).split(".")
        assert len(octets) == 4 and all(0 <= int(o) <= 255 for o in octets)
    # Weighted status pool: 200 dominant, 4xx/5xx present but a minority.
    codes = read_sample("web-access", "status_codes.sample")
    assert all(re.fullmatch(r"[1-5]\d{2}", c) for c in codes)
    counts = {c: codes.count(c) for c in set(codes)}
    assert counts["200"] / len(codes) > 0.4
    error_share = sum(v for k, v in counts.items() if k >= "400") / len(codes)
    assert 0 < error_share < 0.4
    assert any(k.startswith("5") for k in counts)


def test_web_access_sample_size_and_meta():
    _assert_mean_bytes("web-access", "web_access.sample", 190)
    y = (PACKS / "web-access" / "pack.yaml").read_text()
    assert "name: web-access" in y
    assert "sourcetype: access_combined" in y
    assert "bytes_per_event: 190" in y


# ---- aws-cloudtrail (JSON, aws:cloudtrail) ----

def test_cloudtrail_stanza_and_tokens():
    stanza = _single_stanza("aws-cloudtrail", "cloudtrail.sample")
    assert stanza["mode"] == "sample"
    assert stanza["token.0.replacementType"] == "timestamp"
    assert stanza["token.1.replacementType"] == "random"
    assert stanza["token.1.replacement"] == "ipv4"
    assert stanza["token.2.replacementType"] == "random"
    assert stanza["token.2.replacement"] == "guid"
    _assert_tokens_match_every_line(stanza, "aws-cloudtrail", "cloudtrail.sample", 3)


def test_cloudtrail_every_line_is_valid_json_with_core_fields():
    for line in read_sample("aws-cloudtrail", "cloudtrail.sample"):
        rec = json.loads(line)
        for field in ("eventVersion", "userIdentity", "eventTime", "eventSource",
                      "eventName", "awsRegion", "sourceIPAddress", "eventID",
                      "recipientAccountId", "eventCategory"):
            assert field in rec, "missing %s in %s" % (field, line[:80])


def test_cloudtrail_weighted_event_mix_includes_s3_and_management():
    names, sources, cats = set(), set(), set()
    for line in read_sample("aws-cloudtrail", "cloudtrail.sample"):
        rec = json.loads(line)
        names.add(rec["eventName"])
        sources.add(rec["eventSource"])
        cats.add(rec["eventCategory"])
    # S3 read/write data events are present, plus representative management events.
    assert {"GetObject", "PutObject", "DeleteObject"} <= names
    assert {"ConsoleLogin", "AssumeRole", "RunInstances"} <= names
    assert "s3.amazonaws.com" in sources
    assert {"Data", "Management"} <= cats


def test_cloudtrail_sample_size_and_meta():
    _assert_mean_bytes("aws-cloudtrail", "cloudtrail.sample", 1280)
    y = (PACKS / "aws-cloudtrail" / "pack.yaml").read_text()
    assert "name: aws-cloudtrail" in y
    assert "sourcetype: aws:cloudtrail" in y


# ---- aws-s3-access (S3 server access logs, aws:s3:accesslogs) ----

def test_s3_access_stanza_and_tokens():
    stanza = _single_stanza("aws-s3-access", "s3_access.sample")
    assert stanza["mode"] == "sample"
    assert stanza["token.0.replacementType"] == "timestamp"
    assert stanza["token.1.replacementType"] == "random"
    assert stanza["token.1.replacement"] == "ipv4"
    assert stanza["token.2.replacementType"] == "file"
    _assert_tokens_match_every_line(stanza, "aws-s3-access", "s3_access.sample", 3)


def test_s3_access_operations_and_format():
    ops = set()
    for line in read_sample("aws-s3-access", "s3_access.sample"):
        fields = line.split(" ")
        # Bucket owner is a 64-char canonical id; operation is REST.<METHOD>.<TYPE>.
        assert len(fields[0]) == 64
        m = re.search(r"REST\.[A-Z]+\.[A-Z_]+", line)
        assert m, line
        ops.add(m.group(0))
    assert "REST.GET.OBJECT" in ops
    assert any(o.startswith("REST.PUT") for o in ops)


def test_s3_access_sample_size_and_meta():
    _assert_mean_bytes("aws-s3-access", "s3_access.sample", 500)
    y = (PACKS / "aws-s3-access" / "pack.yaml").read_text()
    assert "name: aws-s3-access" in y
    assert "sourcetype: aws:s3:accesslogs" in y


# ---- aws-elb-alb (ALB access logs, aws:elb:accesslogs) ----

def test_alb_stanza_and_tokens():
    stanza = _single_stanza("aws-elb-alb", "alb_access.sample")
    assert stanza["mode"] == "sample"
    assert stanza["token.0.replacementType"] == "timestamp"
    assert stanza["token.1.replacementType"] == "random"
    assert stanza["token.1.replacement"] == "ipv4"
    assert stanza["token.2.replacementType"] == "file"
    _assert_tokens_match_every_line(stanza, "aws-elb-alb", "alb_access.sample", 3)


def test_alb_field_count_and_types():
    types = set()
    for line in read_sample("aws-elb-alb", "alb_access.sample"):
        # ALB logs are space-delimited with quoted fields; the leading connection
        # type is the first token and there are 30 fields through conn_trace_id.
        first = line.split(" ", 1)[0]
        types.add(first)
        assert first in ("http", "https", "h2", "ws", "wss", "grpcs"), line[:40]
        assert line.startswith(first + " ")
        assert "app/prod-alb/" in line
    assert "https" in types


def test_alb_sample_size_and_meta():
    _assert_mean_bytes("aws-elb-alb", "alb_access.sample", 640)
    y = (PACKS / "aws-elb-alb" / "pack.yaml").read_text()
    assert "name: aws-elb-alb" in y
    assert "sourcetype: aws:elb:accesslogs" in y
