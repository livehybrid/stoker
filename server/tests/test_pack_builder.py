"""The pack builder: sample events in, a linted eventgen pack out.

Covers field detection on the three common shapes (NCSA access log, JSON,
syslog), config validation, the written pack (lint-clean, bundles, tokens that
compile and match their sample), the preview, the API round trip (create,
reopen, rebuild in place, delete) and the upgraded pack preview that now
renders file/list tokens.
"""
from __future__ import annotations

import configparser
import dataclasses
import json
import os
import re

import pytest

from server import bundles
from server import config as config_mod
from server import packbuilder as pb
from server.preview import preview_pack

ACCESS = (
    '10.1.2.3 - - [25/Sep/2026:14:03:09 +0000] "GET /products/1001 HTTP/1.1" 200 5123 "-" '
    '"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"\n'
    '203.0.113.9 - - [25/Sep/2026:14:03:11 +0000] "POST /cart HTTP/1.1" 302 0 "https://shop.example.com/" "curl/8.9.1"\n'
)
JSON_EVENTS = json.dumps([
    {"eventTime": "2026-09-25T14:03:09Z", "sourceIPAddress": "198.51.100.7",
     "userIdentity": {"userName": "ada.smith"}, "awsRegion": "eu-west-2",
     "eventID": "3f2b1c9e-8a7d-4e21-9b3c-1d2e3f4a5b6c", "city": "London", "bytes": 512},
    {"eventTime": "2026-09-25T14:04:10Z", "sourceIPAddress": "198.51.100.8",
     "userIdentity": {"userName": "bob.jones"}, "awsRegion": "us-east-1",
     "eventID": "4a2b1c9e-8a7d-4e21-9b3c-1d2e3f4a5b6d", "city": "Paris", "bytes": 2048},
])
SYSLOG = (
    "Sep 25 14:03:09 web-prd-03 sshd[4121]: Accepted password for alice from 10.0.0.5 port 51234 ssh2\n"
    "Sep  5 14:03:12 web-prd-04 sshd[877]: Failed password for invalid user admin from 203.0.113.44 "
    "port 40022 ssh2 action=blocked dst=10.0.0.9\n"
)


def _by_field(result):
    return {s["field"]: s for s in result["suggestions"]}


def _config(text, name="demo", **extra):
    events = pb.split_events(text)
    cfg = {"name": name, "events": events, "tokens": pb.analyse(events)["suggestions"]}
    cfg.update(extra)
    return pb.validate_config(cfg)


# ---- word lists ----

def test_wordlists_ship_and_load():
    index = pb.wordlist_index()
    names = {e["name"] for e in index}
    assert {"cities", "first_names", "usernames", "internal_ips", "user_agents", "http_status"} <= names
    for e in index:
        assert e["count"] > 0 and e["sample"], e["name"]
    assert "London" in pb.load_wordlist("cities")
    with pytest.raises(pb.BuilderError):
        pb.load_wordlist("../../etc/passwd")
    with pytest.raises(pb.BuilderError):
        pb.load_wordlist("nope")


# ---- input splitting ----

def test_split_lines_and_json_arrays():
    assert pb.split_events("a\n\n b \r\nc") == ["a", " b ", "c"]
    evs = pb.split_events(JSON_EVENTS)
    assert len(evs) == 2 and evs[0].startswith('{"eventTime":"2026-09-25T14:03:09Z"')
    with pytest.raises(pb.BuilderError):
        pb.split_events("   \n  ")
    with pytest.raises(pb.BuilderError):
        pb.split_events("x" * (pb.MAX_EVENT_BYTES + 1))


# ---- detection ----

def test_detects_access_log_fields():
    res = pb.analyse(pb.split_events(ACCESS))
    f = _by_field(res)
    assert f["access-log timestamp"]["replacement"] == {"kind": "timestamp", "format": "%d/%b/%Y:%H:%M:%S"}
    assert f["HTTP method"]["replacement"] == {"kind": "list", "list": "http_methods"}
    assert f["HTTP status"]["replacement"] == {"kind": "list", "list": "http_status"}
    assert f["user agent"]["replacement"] == {"kind": "list", "list": "user_agents"}
    assert f["URI path"]["replacement"]["kind"] == "values"
    assert f["response bytes"]["replacement"]["kind"] == "integer"
    ip = f["IPv4 address at line start"]
    assert ip["pattern"].startswith("^(") and ip["matches"] == 2
    # the version number inside the user agent is not mistaken for an address
    assert not any("Chrome" in s["pattern"] for s in res["suggestions"])


def test_detects_json_fields_and_word_lists():
    f = _by_field(pb.analyse(pb.split_events(JSON_EVENTS)))
    assert f["ISO 8601 timestamp"]["replacement"]["format"] == "%Y-%m-%dT%H:%M:%S"
    assert f["sourceIPAddress"]["replacement"] == {"kind": "ipv4"}  # documentation ranges are not RFC 1918
    assert f["userName"]["replacement"] == {"kind": "list", "list": "usernames"}
    assert f["awsRegion"]["replacement"] == {"kind": "list", "list": "aws_regions"}
    assert f["eventID"]["replacement"] == {"kind": "guid"}
    assert f["city"]["replacement"] == {"kind": "list", "list": "cities"}
    assert f["bytes"]["replacement"]["kind"] == "integer"
    assert f["city"]["pattern"] == '"city"\\s*:\\s*"([^"]*)"'


def test_detects_syslog_phrases_and_kv():
    f = _by_field(pb.analyse(pb.split_events(SYSLOG)))
    assert f["syslog timestamp"]["replacement"]["format"] == "%b %e %H:%M:%S"
    assert f["syslog host"]["replacement"] == {"kind": "list", "list": "hostnames"}
    assert f["user (for ... from)"]["examples"] == ["alice", "admin"]
    assert f["port"]["replacement"] == {"kind": "integer", "min": 1024, "max": 65535}
    assert f["process id"]["replacement"]["kind"] == "integer"
    assert f["action"]["replacement"] == {"kind": "list", "list": "actions"}
    assert f["dst"]["replacement"] == {"kind": "list", "list": "internal_ips"}
    assert "IPv4 address after 'from'" in f


def test_distinct_anchors_make_distinct_tokens():
    # one token writes one value into all its matches, so "from" and "to"
    # addresses must be separate fields
    res = pb.analyse(["conn from 1.2.3.4 to 5.6.7.8 ok", "conn from 9.9.9.9 to 8.8.8.8 ok"])
    fields = [s["field"] for s in res["suggestions"] if s["kind"] == "bare"]
    assert "IPv4 address after 'from'" in fields and "IPv4 address after 'to'" in fields


def test_free_text_cities_and_constants():
    res = pb.analyse(["order shipped to London by courier", "order shipped to New York by courier"])
    f = _by_field(res)
    city = f["city names"]
    assert city["replacement"] == {"kind": "list", "list": "cities"}
    assert re.search(city["pattern"], "to New York by")
    # a constant key=value is suggested but off by default
    res = pb.analyse(["app=shop level=INFO msg=a", "app=shop level=WARN msg=b"])
    f = _by_field(res)
    assert f["app"]["enabled"] is False
    assert f["level"]["replacement"] == {"kind": "list", "list": "log_levels"}


def test_highlights_mark_the_rewritten_spans():
    events = pb.split_events(SYSLOG)
    res = pb.analyse(events)
    spans = res["highlights"][0]
    text = events[0]
    marked = [text[s:e] for s, e, _ in spans]
    assert "Sep 25 14:03:09" in marked and "10.0.0.5" in marked and "4121" in marked
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))  # sorted, non-overlapping


# ---- validation ----

@pytest.mark.parametrize("patch,msg", [
    ({"name": "../evil"}, "name"),
    ({"events": []}, "event"),
    ({"events": ["a\nb"]}, "one event per line"),
    ({"tokens": [{"pattern": "(", "replacement": {"kind": "static", "value": "x"}}]}, "invalid regular expression"),
    ({"tokens": [{"pattern": "a", "replacement": {"kind": "list", "list": "nope"}}]}, "unknown word list"),
    ({"tokens": [{"pattern": "a", "replacement": {"kind": "integer", "min": 5, "max": 1}}]}, "below the minimum"),
    ({"tokens": [{"pattern": "a", "replacement": {"kind": "timestamp", "format": "none"}}]}, "strftime"),
    ({"tokens": [{"pattern": "a", "replacement": {"kind": "values", "values": []}}]}, "at least one value"),
    ({"tokens": [{"pattern": "a", "replacement": {"kind": "rot13"}}]}, "unknown replacement"),
    ({"count": 0}, "count"),
])
def test_validation_rejects(patch, msg):
    cfg = {"name": "ok", "events": ["a b"], "tokens": []}
    cfg.update(patch)
    with pytest.raises(pb.BuilderError) as exc:
        pb.validate_config(cfg)
    assert msg in str(exc.value)


# ---- writing the pack ----

def _conf_tokens(pack_dir):
    parser = configparser.RawConfigParser(delimiters=("=",), strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(os.path.join(pack_dir, "default", "eventgen.conf"))
    (section,) = parser.sections()
    out = []
    i = 0
    while parser.has_option(section, "token.%d.token" % i):
        out.append((parser.get(section, "token.%d.token" % i),
                    parser.get(section, "token.%d.replacementType" % i),
                    parser.get(section, "token.%d.replacement" % i)))
        i += 1
    return section, parser, out


@pytest.mark.parametrize("text", [ACCESS, JSON_EVENTS, SYSLOG])
def test_written_pack_lints_bundles_and_matches(tmp_path, text):
    cfg = _config(text, name="Demo Pack", sourcetype="demo:st", tags=["web"], count=7, interval=2)
    dest = str(tmp_path / "pack")
    pb.write_pack(cfg, dest)
    lint = bundles.lint_pack(dest)
    assert lint.ok, lint.errors
    section, parser, tokens = _conf_tokens(dest)
    assert section == "demo-pack.sample"
    assert parser.get(section, "count") == "7" and parser.get(section, "interval") == "2"
    sample = open(os.path.join(dest, "samples", "demo-pack.sample")).read().splitlines()
    assert sample == cfg["events"]
    for pattern, rtype, replacement in tokens:
        assert any(re.search(pattern, ev) for ev in sample), pattern
        if rtype == "file":
            assert os.path.isfile(os.path.join(dest, replacement)), replacement
    # timestamps first, then generated values, then list/value text
    group = {"timestamp": 0, "random": 1, "integerid": 1, "file": 2, "static": 2}
    order = [group[rtype] for _, rtype, _ in tokens]
    assert order == sorted(order) and order[0] == 0
    yaml = open(os.path.join(dest, "pack.yaml")).read()
    assert "tags: pack-builder, web" in yaml and "sourcetype: demo:st" in yaml
    assert pb.read_builder_config(dest)["name"] == "Demo Pack"
    # the stanza (a regex over sample file names) matches only the event sample
    assert not any(re.fullmatch(section, f) for f in os.listdir(os.path.join(dest, "samples", "lists")))


def test_patterns_with_edge_whitespace_survive_the_conf(tmp_path):
    cfg = pb.validate_config({"name": "ws", "events": ['"GET /x HTTP/1.1"'], "tokens": [
        {"field": "method", "pattern": '"(GET) ', "replacement": {"kind": "static", "value": "PUT"}}]})
    dest = str(tmp_path / "p")
    pb.write_pack(cfg, dest)
    _, _, tokens = _conf_tokens(dest)
    assert tokens[0][0] == '"(GET)[ ]'
    assert re.search(tokens[0][0], '"GET /x')


def test_render_preview_rewrites_and_warns():
    cfg = _config(SYSLOG)
    out = pb.render_preview(cfg, n=10, seed=3)
    assert len(out["events"]) == 10 and out["warnings"] == []
    hosts = pb.load_wordlist("hostnames")
    for ev in out["events"]:
        host = ev.split()[3]
        assert host in hosts
        assert "4121" not in ev or "877" not in ev
    cfg["tokens"].append({"id": "x", "field": "never", "pattern": "NOPE", "enabled": True,
                          "replacement": {"kind": "static", "value": "y"}})
    assert pb.render_preview(cfg, n=2)["warnings"] == ["never: the pattern matches none of the sample events"]


def test_pack_preview_renders_list_tokens(tmp_path):
    cfg = _config(ACCESS)
    dest = str(tmp_path / "pack")
    pb.write_pack(cfg, dest)
    agents = set(pb.load_wordlist("user_agents"))
    events = preview_pack(dest, n=20)
    assert len(events) == 20
    assert all(re.search(r'"([^"]*)"$', e).group(1) in agents for e in events)


# ---- API ----

@pytest.fixture()
def upload_dir(settings, tmp_path):
    target = tmp_path / "uploads"
    config_mod.set_settings(dataclasses.replace(config_mod.get_settings(), pack_upload_dir=str(target)))
    return str(target)


def test_api_end_to_end(client, upload_dir):
    r = client.get("/api/pack-builder/wordlists")
    assert r.status_code == 200 and any(w["name"] == "cities" for w in r.json())
    assert client.get("/api/pack-builder/wordlists/cities").json()["count"] > 100
    assert client.get("/api/pack-builder/wordlists/nope").status_code == 404

    r = client.post("/api/pack-builder/analyse", json={"text": SYSLOG})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["event_list"]) == 2 and body["suggestions"] and body["highlights"]
    config = {"name": "sshd auth", "events": body["event_list"], "tokens": body["suggestions"],
              "sourcetype": "linux_secure", "count": 20, "interval": 1}

    r = client.post("/api/pack-builder/preview", json={"config": config, "n": 5, "seed": 1})
    assert r.status_code == 200 and len(r.json()["events"]) == 5

    r = client.post("/api/pack-builder/packs", json={"config": config})
    assert r.status_code == 201, r.text
    pack = r.json()
    assert pack["lint_status"] == "ok" and pack["verified"] is True
    assert pack["engines_json"] == ["eventgen"] and "pack-builder" in pack["tags_json"]
    assert os.path.realpath(pack["source_path"]).startswith(os.path.realpath(upload_dir))

    assert client.post("/api/pack-builder/packs", json={"config": config}).status_code == 409

    r = client.get("/api/pack-builder/packs/%d" % pack["id"])
    assert r.status_code == 200 and r.json()["config"]["sourcetype"] == "linux_secure"

    config["count"] = 50
    config["tokens"] = [t for t in config["tokens"] if t["field"] != "port"]
    r = client.put("/api/pack-builder/packs/%d" % pack["id"], json={"config": config})
    assert r.status_code == 200, r.text
    assert r.json()["source_path"] == pack["source_path"]
    conf = open(os.path.join(pack["source_path"], "default", "eventgen.conf")).read()
    assert "count = 50" in conf and "port (" not in conf
    assert not [d for d in os.listdir(upload_dir) if ".old-" in d or d.startswith(".builder-")]

    r = client.get("/api/packs/%d/preview" % pack["id"])
    assert r.status_code == 200

    assert client.delete("/api/packs/%d" % pack["id"]).status_code == 204
    assert not os.path.exists(pack["source_path"])


def test_api_rejects_bad_input_and_foreign_packs(client, upload_dir, db_session):
    r = client.post("/api/pack-builder/analyse", json={"text": "   "})
    assert r.status_code == 422
    r = client.post("/api/pack-builder/packs", json={"config": {"name": "x", "events": ["a"], "tokens": [
        {"pattern": "(", "replacement": {"kind": "static", "value": "b"}}]}})
    assert r.status_code == 422 and "invalid regular expression" in r.text
    from server.models import Pack
    other = Pack(name="elsewhere", source_path="/etc", tags_json=[], engines_json=["eventgen"])
    db_session.add(other)
    db_session.commit()
    assert client.get("/api/pack-builder/packs/%d" % other.id).status_code == 404
    assert client.put("/api/pack-builder/packs/%d" % other.id,
                      json={"config": {"name": "elsewhere", "events": ["a"]}}).status_code == 404
    assert client.get("/api/pack-builder/packs/999999").status_code == 404
