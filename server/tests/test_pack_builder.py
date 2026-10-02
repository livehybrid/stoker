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
    ip = f["client"]  # the NCSA client field, an IP or a hostname
    assert ip["pattern"].startswith("^(") and ip["matches"] == 2
    assert ip["replacement"] == {"kind": "ipv4"}
    # the version number inside the user agent is not mistaken for an address
    assert not any("Chrome" in s["pattern"] for s in res["suggestions"])


def test_detects_json_fields_and_word_lists():
    f = _by_field(pb.analyse(pb.split_events(JSON_EVENTS)))
    assert f["ISO 8601 timestamp"]["replacement"]["format"] == "%Y-%m-%dT%H:%M:%S"
    assert f["sourceIPAddress"]["replacement"] == {"kind": "ipv4"}  # documentation ranges are not RFC 1918
    # userName and city both come from the identities table: linked to one row
    assert f["userName"]["replacement"] == {"kind": "linked", "table": "identities", "column": "username"}
    assert f["userName"]["alternative"] == {"kind": "list", "list": "usernames"}
    assert f["awsRegion"]["replacement"] == {"kind": "list", "list": "aws_regions"}
    assert f["eventID"]["replacement"] == {"kind": "guid"}
    assert f["city"]["replacement"] == {"kind": "linked", "table": "identities", "column": "city"}
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
    ({"events": ["a\nb"]}, "set an event breaker"),
    ({"events": ["a\nb"], "breaker": "^"}, "must not match empty"),
    ({"events": ["x1 a\nx2 b"], "breaker": r"^x\d"}, "does not survive the event breaker"),
    ({"tokens": [{"pattern": "a", "replacement": {"kind": "linked", "table": "nope", "column": "x"}}]},
     "unknown table"),
    ({"tokens": [{"pattern": "a", "replacement": {"kind": "linked", "table": "identities", "column": "x"}}]},
     "has no column"),
    ({"tokens": [{"pattern": "a", "replacement": {"kind": "list", "list": "identities"}}]}, "unknown word list"),
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
        if rtype == "mvfile":
            assert os.path.isfile(os.path.join(dest, replacement.rsplit(":", 1)[0])), replacement
    # timestamps first, then generated values, then list/value text
    group = {"timestamp": 0, "random": 1, "integerid": 1, "file": 2, "mvfile": 2, "static": 2}
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



# ---- tables and linked fields ----

def test_shipped_tables_are_well_formed():
    tables = pb.table_columns()
    assert tables["identities"][:5] == ["first_name", "last_name", "full_name", "username", "email"]
    assert "hostname" in tables["hosts"] and "ip" in tables["hosts"]
    for name, cols in tables.items():
        for row in pb.load_wordlist(name):
            cells = row.split(",")
            assert len(cells) == len(cols) and all(c and '"' not in c for c in cells), (name, row)
    kinds = {e["name"]: e["kind"] for e in pb.wordlist_index()}
    assert kinds["identities"] == "table" and kinds["cities"] == "list"
    assert "identities" not in pb.wordlist_names()


LINKED_JSON = json.dumps([
    {"user": "ada.smith", "email": "ada.smith@example.com", "department": "Finance",
     "src_user": "bob.jones", "dest_user": "carol.white", "host": "web-prd-01", "src_ip": "10.0.0.5"},
    {"user": "bob.jones", "email": "bob.jones@example.com", "department": "Legal",
     "src_user": "ada.smith", "dest_user": "dan.brown", "host": "db-prd-02", "src_ip": "10.0.0.6"},
])


def test_analyse_links_identity_and_host_fields():
    fields = _by_field(pb.analyse(pb.split_events(LINKED_JSON)))
    for key, col in (("user", "username"), ("email", "email"), ("department", "department")):
        assert fields[key]["replacement"] == {"kind": "linked", "table": "identities", "column": col}, key
        assert fields[key]["alternative"]["kind"] == "list"
    # two fields on one column would always be equal: they stay independent
    assert fields["src_user"]["replacement"] == {"kind": "list", "list": "usernames"}
    assert fields["dest_user"]["replacement"] == {"kind": "list", "list": "usernames"}
    assert fields["host"]["replacement"] == {"kind": "linked", "table": "hosts", "column": "hostname"}
    assert fields["src_ip"]["replacement"] == {"kind": "linked", "table": "hosts", "column": "ip"}


def test_a_single_identity_field_is_not_linked():
    fields = _by_field(pb.analyse(['{"user":"ada.smith","n":1}', '{"user":"bob.jones","n":2}']))
    assert fields["user"]["replacement"] == {"kind": "list", "list": "usernames"}


def test_linked_fields_share_a_row_in_preview_and_pack(tmp_path):
    cfg = _config(LINKED_JSON, name="linked")
    rows = {tuple(r.split(",")) for r in pb.load_wordlist("identities")}
    by_user = {r[3]: r for r in rows}
    for ev in pb.render_preview(cfg, n=30, seed=5)["events"]:
        doc = json.loads(ev)
        row = by_user[doc["user"]]
        assert (doc["email"], doc["department"]) == (row[4], row[5])
    dest = str(tmp_path / "pack")
    pb.write_pack(cfg, dest)
    assert bundles.lint_pack(dest).ok
    _, _, tokens = _conf_tokens(dest)
    mv = [t for t in tokens if t[1] == "mvfile"]
    assert {t[2] for t in mv} >= {"samples/lists/identities.sample:4", "samples/lists/identities.sample:5",
                                  "samples/lists/hosts.sample:1", "samples/lists/hosts.sample:2"}
    assert os.path.isfile(os.path.join(dest, "samples", "lists", "identities.sample"))
    # the wizard preview (server.preview) keeps one row per event too
    for ev in preview_pack(dest, n=20):
        doc = json.loads(ev)
        assert by_user[doc["user"]][4] == doc["email"]


# ---- event breaking ----

TRACE = (
    "2026-09-27 10:00:01 ERROR request failed user=alice\n"
    "java.lang.IllegalStateException: boom\n"
    "    at com.example.Service.call(Service.java:42)\n"
    "    at com.example.Api.handle(Api.java:7)\n"
    "2026-09-27 10:00:02 INFO request ok user=bob\n"
    "2026-09-27 10:00:03 WARN slow user=carol\n"
    "\tat com.example.Db.query(Db.java:9)\n"
)
XML = (
    "<Event xmlns='x'>\n  <System><EventID>4624</EventID></System>\n</Event>\n"
    "<Event xmlns='x'>\n  <System><EventID>4625</EventID></System>\n</Event>\n"
)


def test_split_detects_multiline_events():
    r = pb.split_input(TRACE)
    assert r["format"] == "multiline" and r["breaker"].startswith("^")
    assert len(r["events"]) == 3 and r["events"][0].count("\n") == 3
    assert r["events"][2].endswith("Db.java:9)")
    x = pb.split_input(XML)
    assert x["breaker"] == r"^<Event[\s>]" and len(x["events"]) == 2
    # line mode never breaks on anything but newlines
    assert len(pb.split_input(TRACE, "line")["events"]) == 7
    # plain one-per-line text is not multi-line
    assert pb.split_input(SYSLOG)["breaker"] is None
    # pretty-printed JSON objects become one compact event each
    j = pb.split_input('{\n  "a": 1\n}\n{\n  "a": 2\n}\n')
    assert j["format"] == "json" and j["events"] == ['{"a":1}', '{"a":2}']


def test_regex_mode_and_eventgen_split_semantics():
    r = pb.split_input("A1\nx\nA2\ny\n", "regex", r"^A\d")
    assert r["events"] == ["A1\nx", "A2\ny"]
    with pytest.raises(pb.BuilderError):
        pb.split_input("a", "regex", "(")
    with pytest.raises(pb.BuilderError):
        pb.split_input("a", "regex", "^")


def test_multiline_pack_round_trips(tmp_path):
    r = pb.split_input(TRACE)
    cfg = pb.validate_config({"name": "trace", "events": r["events"], "breaker": r["breaker"],
                              "tokens": pb.analyse(r["events"])["suggestions"]})
    dest = str(tmp_path / "pack")
    pb.write_pack(cfg, dest)
    assert bundles.lint_pack(dest).ok
    section, parser, _ = _conf_tokens(dest)
    breaker = parser.get(section, "breaker")
    text = open(os.path.join(dest, "samples", "trace.sample")).read()
    assert pb.break_events(text, breaker) == cfg["events"]
    out = preview_pack(dest, n=3)
    assert len(out) == 3 and out[0].count("\n") == 3 and "Service.java:42" in out[0]


# ---- CSV ----

CSV = (
    "time,user,email,department,src_ip,bytes\n"
    "2026-09-27 10:00:00,ada.smith,ada.smith@example.com,Finance,10.0.0.1,512\n"
    "2026-09-27 10:00:01,bob.jones,bob.jones@example.com,Legal,10.0.0.2,2048\n"
)


def test_csv_columns_become_fields():
    r = pb.split_input(CSV)
    assert r["format"] == "csv" and r["header"][0] == "time" and len(r["events"]) == 2
    fields = _by_field(pb.analyse(r["events"], r["header"]))
    assert "date-time" in fields  # the timestamp column is claimed as a timestamp first
    assert fields["user"]["kind"] == "csv"
    assert fields["user"]["replacement"]["kind"] == "linked"
    assert fields["bytes"]["replacement"]["kind"] == "integer"
    assert re.match(fields["bytes"]["pattern"], r["events"][0]).group(1) == "512"
    cfg = pb.validate_config({"name": "csv", "events": r["events"],
                              "tokens": list(fields.values())})
    for ev in pb.render_preview(cfg, n=10, seed=2)["events"]:
        assert ev.count(",") == 5


def test_csv_detection_and_splunk_exports():
    with pytest.raises(pb.BuilderError):
        pb.split_input("a b\nc d\n", "csv")
    assert pb.split_input("a,b\n1,2\n", "auto")["format"] == "lines"  # too short to call
    export = '_time,host,_raw\n1,h,"a,b ""q"""\n2,h,plain\n'
    r = pb.split_input(export)
    assert r["format"] == "splunk_csv" and r["events"] == ['a,b "q"', "plain"]


# ---- custom word lists ----

def test_custom_wordlists(tmp_path):
    pb.set_custom_dir(str(tmp_path / ".wordlists"))
    try:
        entry = pb.save_custom_wordlist("store_ids", ["S-001", "S-002", " ", "S-003"], title="Stores")
        assert entry["custom"] and entry["count"] == 3 and entry["kind"] == "list"
        assert "store_ids" in pb.wordlist_names()
        table = pb.save_custom_wordlist("stores", ["S-001,Leeds", "S-002,York"], columns=["id", "city"])
        assert table["kind"] == "table" and pb.table_columns()["stores"] == ["id", "city"]
        for name, values, cols, msg in (
                ("cities", ["x"], None, "shipped"),
                ("Bad-Name", ["x"], None, "lower-case"),
                ("t", ["a,b,c"], ["x", "y"], "row 1"),
                ("t", ['a,"b"'], ["x", "y"], "row 1"),
                ("t", [], None, "at least one")):
            with pytest.raises(pb.BuilderError) as exc:
                pb.save_custom_wordlist(name, values, columns=cols)
            assert msg in str(exc.value)
        cfg = pb.validate_config({"name": "c", "events": ["store=S-009 city=Leeds"], "tokens": [
            {"field": "store", "pattern": r"store=(\S+)", "replacement": {"kind": "linked", "table": "stores",
                                                                         "column": "id"}},
            {"field": "city", "pattern": r"city=(\S+)", "replacement": {"kind": "linked", "table": "stores",
                                                                       "column": "city"}}]})
        for ev in pb.render_preview(cfg, n=10)["events"]:
            assert ev in ("store=S-001 city=Leeds", "store=S-002 city=York")
        dest = str(tmp_path / "pack")
        pb.write_pack(cfg, dest)
        pb.delete_custom_wordlist("stores")
        assert "stores" not in pb.table_columns()
        # the built pack carries its own copy
        assert open(os.path.join(dest, "samples", "lists", "stores.sample")).read() == "S-001,Leeds\nS-002,York\n"
        with pytest.raises(pb.BuilderError):
            pb.delete_custom_wordlist("cities")
        with pytest.raises(pb.BuilderError):
            pb.delete_custom_wordlist("stores")
    finally:
        pb.set_custom_dir(None)


def test_api_custom_lists_and_break_modes(client, upload_dir):
    r = client.post("/api/pack-builder/wordlists", json={"name": "teams", "values": ["red", "blue"]})
    assert r.status_code == 201, r.text
    assert os.path.isfile(os.path.join(upload_dir, ".wordlists", "teams.txt"))
    names = {w["name"]: w for w in client.get("/api/pack-builder/wordlists").json()}
    assert names["teams"]["custom"] and names["identities"]["kind"] == "table"
    assert client.get("/api/pack-builder/wordlists/identities").json()["columns"][0] == "first_name"
    assert client.post("/api/pack-builder/wordlists", json={"name": "cities", "values": ["x"]}).status_code == 422
    assert client.delete("/api/pack-builder/wordlists/teams").status_code == 204
    assert client.delete("/api/pack-builder/wordlists/teams").status_code == 404
    r = client.post("/api/pack-builder/analyse", json={"text": TRACE})
    body = r.json()
    assert body["format"] == "multiline" and len(body["event_list"]) == 3 and body["breaker"]
    r = client.post("/api/pack-builder/analyse", json={"text": CSV, "mode": "csv"})
    assert r.json()["header"][1] == "user"
    r = client.post("/api/pack-builder/analyse", json={"text": "x", "mode": "regex", "breaker": "("})
    assert r.status_code == 422
    r = client.post("/api/pack-builder/packs", json={"config": {
        "name": "Trace pack", "events": body["event_list"], "breaker": body["breaker"],
        "tokens": body["suggestions"]}})
    assert r.status_code == 201, r.text


# ---- analyser robustness (found importing security_content data sources) ----

WINXML = ("<Event><System><Provider Name='Microsoft-Windows-Security-Auditing'/><EventID>4624</EventID>"
          "<Computer>DC01.corp.local</Computer></System><EventData>"
          "<Data Name='TargetUserName'>alice</Data><Data Name='IpAddress'>10.0.0.5</Data>"
          "<Data Name='LogonType'>3</Data></EventData></Event>")


def test_windows_xml_data_elements_are_fields_and_name_attributes_are_not():
    fields = _by_field(pb.analyse([WINXML]))
    assert fields["TargetUserName"]["pattern"] == "<Data Name='TargetUserName'>([^<]*)</Data>"
    assert fields["TargetUserName"]["replacement"] == {"kind": "list", "list": "usernames"}
    assert fields["IpAddress"]["replacement"] == {"kind": "list", "list": "internal_ips"}
    assert fields["Computer"]["replacement"] == {"kind": "list", "list": "hostnames"}
    assert not fields["EventID"]["enabled"]
    # Name='...' repeats with different values in one event: structure, not a field
    assert "Name" not in fields
    cfg = pb.validate_config({"name": "x", "events": [WINXML], "tokens": list(fields.values())})
    out = pb.render_preview(cfg, n=5, seed=1)["events"]
    assert all("<Data Name='TargetUserName'>" in e and "<Data Name='IpAddress'>" in e for e in out)


def test_lists_must_fit_the_values():
    ev = ('{"LoginTo":"https://console.aws.amazon.com/","MFAUsed":"No","countryCode":"GB",'
          '"login":"ada.smith","id":12345678901234567,"big":123456789012345678901234,"lat":51.50735095123456}')
    fields = _by_field(pb.analyse([ev]))
    assert fields["LoginTo"]["replacement"]["kind"] == "values"       # a URL is not a username
    assert fields["MFAUsed"]["replacement"]["kind"] == "values"       # "No" is not Norway
    assert fields["countryCode"]["replacement"] == {"kind": "list", "list": "country_codes"}
    assert fields["login"]["replacement"] == {"kind": "list", "list": "usernames"}
    # Both are identifiers now: a field named `id` and a digit string too long
    # to be a number get a consistent stand-in, which keeps them correlatable
    # AND keeps the real value out of the pack. Before, `big` was copied into a
    # values list verbatim.
    assert fields["id"]["replacement"]["kind"] == "pseudonym"
    assert fields["big"]["replacement"]["kind"] == "pseudonym"
    assert fields["lat"]["replacement"]["decimals"] == 9
    blob = '{"cert":"%s","user":"ada.smith"}' % ("A" * 5000)
    f = _by_field(pb.analyse([blob]))
    assert f["cert"]["replacement"]["kind"] == "static" and not f["cert"]["enabled"]
    # every suggestion validates
    pb.validate_config({"name": "x", "events": [ev], "tokens": list(fields.values())})


def test_windows_value_shapes_and_single_values_do_not_match_lists():
    ev = ("<Event><System><Channel>Security</Channel></System><EventData>"
          "<Data Name='TargetUserSid'>S-1-5-21-1111-2222-3333-1001</Data>"
          "<Data Name='ProcessId'>0x3e4</Data><Data Name='VirtualAccount'>%%1843</Data>"
          "<Data Name='department'>Finance</Data></EventData></Event>")
    f = _by_field(pb.analyse([ev]))
    for key in ("ProcessId", "VirtualAccount", "Channel"):
        assert f[key]["replacement"]["kind"] == "values", key
    # A SID names a user, so it is an identifier: a consistent stand-in keeps
    # the correlation and keeps the real SID out of the pack, where a values
    # list would have copied it in.
    assert f["TargetUserSid"]["replacement"]["kind"] == "pseudonym"
    assert f["department"]["replacement"] == {"kind": "list", "list": "departments"}


def test_auditd_epoch_and_serial():
    ev = 'type=EXECVE msg=audit(1723044684.257:15795): argc=3 a0="sudo"'
    f = _by_field(pb.analyse([ev, ev.replace("15795", "15801")]))
    assert f["audit time"]["replacement"] == {"kind": "timestamp", "format": "%s"}
    assert f["audit serial"]["replacement"] == {"kind": "sequence", "start": 15795}


# ---- sample coverage: a count below the sample size truncates it for ever ----

def _twenty():
    return ["line-%02d payload" % i for i in range(20)]


def test_a_new_pack_generates_its_whole_sample_every_interval(tmp_path):
    """The builder's default count is -1, eventgen's "the whole sample".

    A positive count below the sample size makes BOTH engines emit only that
    prefix, every interval, for ever (measured: 20 lines at count = 5 gives
    lines 00-04 four times in four intervals). A 1,000-event upload generating
    ten events reads as a broken upload, so -1 is the default.
    """
    cfg = pb.validate_config({"name": "whole", "events": _twenty(), "tokens": []})
    assert cfg["count"] == pb.WHOLE_SAMPLE == -1
    dest = str(tmp_path / "pack")
    pb.write_pack(cfg, dest)
    _, parser, _ = _conf_tokens(dest)
    assert parser.get("whole.sample", "count") == "-1"
    assert bundles.lint_pack(dest).ok
    assert pb.read_builder_config(dest)["count"] == -1


@pytest.mark.parametrize("count,expect", [
    (-1, None),
    (5, "only the first 5 of your 20 events"),
    (30, "not a whole multiple"),
    (40, None),
])
def test_coverage_warnings(count, expect):
    cfg = pb.validate_config({"name": "cov", "events": _twenty(), "tokens": [],
                              "count": count, "order": "sequential"})
    warnings = pb.coverage_warnings(cfg)
    if expect is None:
        assert warnings == []
    else:
        assert len(warnings) == 1 and expect in warnings[0]
    # random order has no passes, so neither warning applies
    rand = pb.validate_config({"name": "cov", "events": _twenty(), "tokens": [],
                               "count": count, "order": "random"})
    assert pb.coverage_warnings(rand) == []


def test_preview_mirrors_the_engines_truncation():
    # The preview must show what the run will actually generate, not lines the
    # engines will never reach.
    cfg = pb.validate_config({"name": "cov", "events": _twenty(), "tokens": [],
                              "count": 5, "order": "sequential"})
    out = pb.render_preview(cfg, n=20, seed=1)
    assert sorted({e.split()[0] for e in out["events"]}) == [
        "line-00", "line-01", "line-02", "line-03", "line-04"]
    assert any("only the first 5" in w for w in out["warnings"])

    whole = pb.validate_config({"name": "cov", "events": _twenty(), "tokens": [],
                                "count": -1, "order": "sequential"})
    out = pb.render_preview(whole, n=20, seed=1)
    assert len({e.split()[0] for e in out["events"]}) == 20
    assert out["warnings"] == []


def test_zero_count_is_still_refused():
    with pytest.raises(pb.BuilderError):
        pb.validate_config({"name": "z", "events": _twenty(), "tokens": [], "count": 0})


def test_a_whole_sample_pack_declares_its_implied_rate():
    """count_interval is engine-paced with no token bucket.

    A whole-sample pack therefore sets its own rate from the sample size, so a
    5,000-event upload emits 5,000 events per second per worker in that mode
    where the old count = 10 default emitted ten. Say so before they build.
    """
    big = pb.validate_config({"name": "big", "events": ["l%05d" % i for i in range(5000)],
                              "tokens": []})
    (note,) = pb.coverage_warnings(big)
    assert "about 5000 events per second PER WORKER" in note
    assert "eps or GB/day" in note

    # Raising the interval lowers it proportionally...
    slower = pb.validate_config({"name": "big", "events": ["l%05d" % i for i in range(5000)],
                                 "tokens": [], "interval": 10})
    assert "about 500 events per second" in pb.coverage_warnings(slower)[0]
    # ...and a small pack says nothing.
    small = pb.validate_config({"name": "small", "events": ["l%02d" % i for i in range(20)],
                                "tokens": []})
    assert pb.coverage_warnings(small) == []


# ---- consistent pseudonyms in the builder ----

PSEUDO_SAMPLE = ["user=123 action=login", "user=456 action=register",
                 "user=123 action=logout"]
PSEUDO_TOKEN = {"field": "user", "pattern": r"\buser=(\d+)", "enabled": True,
                "replacement": {"kind": "pseudonym"}}


@pytest.fixture()
def subkey():
    from server.packbuilder import pseudonym as _ps
    return _ps.derive_subkey(bytes(range(32))), _ps.fingerprint(bytes(range(32)))


def test_a_pseudonym_field_emits_no_eventgen_token(subkey):
    cfg = pb.validate_config({"name": "p", "events": PSEUDO_SAMPLE,
                              "tokens": [PSEUDO_TOKEN]})
    assert cfg["tokens"][0]["replacement"] == {
        "kind": "pseudonym", "widen": None, "rotate": False, "rewrite_residuals": False}
    # The sample text itself is rewritten, so there is nothing for the engines
    # to do and nothing that can be dropped by an engine that does not know it.
    assert pb.eventgen_tokens(cfg)[0] == []


def test_written_pack_holds_stand_ins_and_no_originals(tmp_path, subkey):
    sub, fp = subkey
    cfg = pb.validate_config({"name": "sessions", "events": PSEUDO_SAMPLE,
                              "tokens": [PSEUDO_TOKEN], "order": "sequential"})
    dest = str(tmp_path / "pack")
    pb.write_pack(cfg, dest, subkey=sub, fingerprint=fp)

    sample = open(os.path.join(dest, "samples", "sessions.sample")).read()
    assert "123" not in sample and "456" not in sample
    lines = sample.splitlines()
    assert lines[0].split()[0] == lines[2].split()[0], "the journey must still join"
    assert lines[0].split()[0] != lines[1].split()[0]
    assert all(re.match(r"^user=\d{3} action=\w+$", l) for l in lines), "format kept"

    # the stored builder config holds the stand-ins too, so reopening the pack
    # cannot show an original
    stored = pb.read_builder_config(dest)
    assert stored["events"] == lines
    (row,) = stored["pseudonymised"]
    assert (row["field"], row["spans"], row["distinct"]) == ("user", 3, 2)
    assert stored["pseudonym_key"]["fingerprint"] == fp

    yaml = open(os.path.join(dest, "pack.yaml")).read()
    assert "key_fingerprint: %s" % fp in yaml and "fields: user" in yaml
    assert bundles.lint_pack(dest).ok


def test_the_preview_shows_what_the_pack_will_hold(subkey):
    sub, _fp = subkey
    cfg = pb.validate_config({"name": "p", "events": PSEUDO_SAMPLE,
                              "tokens": [PSEUDO_TOKEN], "order": "sequential"})
    out = pb.render_preview(cfg, n=3, seed=1, subkey=sub)
    assert not any("123" in e or "456" in e for e in out["events"])
    assert out["events"][0].split()[0] == out["events"][2].split()[0]
    # and the collision risk is stated rather than left to be discovered
    assert any("same stand-in and merge" in w for w in out["warnings"])


def test_building_without_the_key_is_refused(tmp_path):
    cfg = pb.validate_config({"name": "p", "events": PSEUDO_SAMPLE,
                              "tokens": [PSEUDO_TOKEN]})
    with pytest.raises(pb.BuilderError) as exc:
        pb.write_pack(cfg, str(tmp_path / "p"))
    assert "pseudonym key" in str(exc.value)


def test_a_rebuild_never_pseudonymises_a_stand_in(tmp_path, subkey):
    """p(p(x)) would silently stop the pack correlating with every other one."""
    sub, fp = subkey
    cfg = pb.validate_config({"name": "s", "events": PSEUDO_SAMPLE,
                              "tokens": [PSEUDO_TOKEN]})
    first = str(tmp_path / "p1")
    pb.write_pack(cfg, first, subkey=sub, fingerprint=fp)
    built = pb.read_builder_config(first)

    # Reopen exactly as the UI does, then rebuild with the server passing the
    # events it read from disk.
    second = str(tmp_path / "p2")
    pb.write_pack(pb.validate_config(built), second, subkey=sub, fingerprint=fp,
                  already=built["events"])
    assert pb.read_builder_config(second)["events"] == built["events"]

    # The config a client sends back carries no trustworthy evidence of the
    # previous build: validate_config strips it, which is why the server must
    # supply `already` itself.
    assert "pseudonymised" not in pb.validate_config(built)


def test_a_multi_group_pseudonym_pattern_is_refused():
    with pytest.raises(pb.BuilderError) as exc:
        pb.validate_config({"name": "p", "events": PSEUDO_SAMPLE, "tokens": [
            {"field": "u", "pattern": r"(user)=(\d+)",
             "replacement": {"kind": "pseudonym"}}]})
    assert "at most one capture group" in str(exc.value)


def test_widening_a_field_changes_its_length_in_the_pack(tmp_path, subkey):
    sub, fp = subkey
    token = dict(PSEUDO_TOKEN, replacement={"kind": "pseudonym",
                                            "widen": {"shape": "digits", "length": 15}})
    cfg = pb.validate_config({"name": "w", "events": PSEUDO_SAMPLE, "tokens": [token]})
    dest = str(tmp_path / "pack")
    pb.write_pack(cfg, dest, subkey=sub, fingerprint=fp)
    lines = open(os.path.join(dest, "samples", "w.sample")).read().splitlines()
    assert all(re.match(r"^user=\d{15} action=\w+$", l) for l in lines)
    assert lines[0].split()[0] == lines[2].split()[0]
    assert bundles.lint_pack(dest).ok


def test_api_pseudonym_end_to_end(client, upload_dir, db_session):
    """Upload, preview, save, reopen, rebuild - with the instance key."""
    from server import pseudonymkeys

    # No key exists until a pseudonymising save needs one, and asking does not
    # bring one into being.
    status = client.get("/api/pack-builder/pseudonym-key").json()
    assert status["exists"] is False and status["fingerprint"] is None
    assert pseudonymkeys.peek(db_session) is None

    config = {"name": "sessions", "events": PSEUDO_SAMPLE, "order": "sequential",
              "tokens": [PSEUDO_TOKEN]}

    # The preview shows the real stand-ins, which creates the key on first use.
    preview = client.post("/api/pack-builder/preview",
                          json={"config": config, "n": 3, "seed": 1})
    assert preview.status_code == 200, preview.text
    shown = preview.json()["events"]
    assert not any("123" in e or "456" in e for e in shown)
    assert shown[0].split()[0] == shown[2].split()[0]

    status = client.get("/api/pack-builder/pseudonym-key").json()
    assert status["exists"] is True and len(status["fingerprint"]) == 8
    assert status["algorithm"] == "hmac-sha256-v1"

    created = client.post("/api/pack-builder/packs", json={"config": config})
    assert created.status_code == 201, created.text
    pack_id = created.json()["id"]
    assert created.json()["lint_status"] == "ok"

    on_disk = os.path.join(created.json()["source_path"], "samples", "sessions.sample")
    sample = open(on_disk).read()
    assert "123" not in sample and "456" not in sample
    lines = sample.splitlines()
    assert lines[0].split()[0] == lines[2].split()[0]

    # Reopening returns the stand-ins, and rebuilding is idempotent because the
    # server passes the on-disk events rather than trusting the client.
    reopened = client.get("/api/pack-builder/packs/%d" % pack_id).json()["config"]
    assert reopened["events"] == lines
    assert reopened["pseudonym_key"]["fingerprint"] == status["fingerprint"]
    rebuilt = client.put("/api/pack-builder/packs/%d" % pack_id,
                         json={"config": reopened})
    assert rebuilt.status_code == 200, rebuilt.text
    assert open(on_disk).read().splitlines() == lines, "a rebuild must not re-hash"


def test_api_refuses_a_pseudonym_save_without_a_usable_master_key(client, upload_dir):
    import dataclasses

    from server import config as config_mod

    config_mod.set_settings(dataclasses.replace(
        config_mod.get_settings(), master_key_generated=True))
    r = client.post("/api/pack-builder/packs", json={"config": {
        "name": "p", "events": PSEUDO_SAMPLE, "tokens": [PSEUDO_TOKEN]}})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "pseudonym_key_unavailable"
    assert "STOKER_MASTER_KEY" in r.json()["detail"]["detail"]


def test_a_pack_without_pseudonyms_never_touches_the_key(client, upload_dir, db_session):
    from server import pseudonymkeys

    r = client.post("/api/pack-builder/preview", json={"config": {
        "name": "plain", "events": ["a=1 b=2"], "tokens": []}, "n": 2})
    assert r.status_code == 200
    assert pseudonymkeys.peek(db_session) is None


@pytest.mark.parametrize("key,values,expect", [
    # Named identifiers, opaque values: the point of the feature.
    ("client_id", ["123456", "789012"], "pseudonym"),
    ("clientId", ["123456", "789012"], "pseudonym"),
    ("clientid", ["123456", "789012"], "pseudonym"),
    ("order_no", ["100001", "100002"], "pseudonym"),
    ("TargetUserSid", ["S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002"], "pseudonym"),
    ("trace_id", ["a1b2c3d4e5f6", "998877665544"], "pseudonym"),
    # A person-shaped key holding digits is an account number, not a name.
    ("user", ["123", "456", "123"], "pseudonym"),
    # Code sets: short numbers, few of them. Pseudonymising a Windows EventID
    # would break every search that looks for 4624.
    ("EventID", ["4624", "4625", "4624"], "integer"),
    ("LogonType", ["3", "2"], "integer"),
    ("priority", ["1", "2", "3"], "integer"),
    # Not identifier names at all: v1's rule put pseudonyms on these.
    ("request_method", ["GET", "POST"], "list"),
    ("user_agent", ["Mozilla/5.0 (X11)", "curl/8.9.1"], "list"),
    ("status", ["200", "404"], "list"),
    ("city", ["London", "Paris"], "list"),
    ("bytes", ["512", "2048"], "integer"),
    # Words that merely end in "id".
    ("overpaid", ["12", "13"], "integer"),
    ("valid", ["true", "false"], "values"),
    # A name stays a name: the word list gives realistic people.
    ("username", ["ada.smith", "bob.jones"], "list"),
])
def test_the_identifier_rule_is_narrow(key, values, expect):
    replacement, _enabled, _why = pb.recommend(key, values)
    assert replacement["kind"] == expect, (key, replacement)


def test_a_guid_is_only_an_identifier_when_it_recurs():
    one = "3f2b1c9e-8a7d-4e21-9b3c-1d2e3f4a5b6c"
    two = "4a2b1c9e-8a7d-4e21-9b3c-1d2e3f4a5b6d"
    # Unique per event: the ordinary random GUID already keeps the original out
    # of the pack and gives unlimited cardinality, so a pseudonym would cap it
    # at the sample's distinct count for no privacy gain.
    assert pb.recommend("eventID", [one, two])[0]["kind"] == "guid"
    # Recurring: it identifies a session, so the correlation must survive.
    assert pb.recommend("session_uuid", [one, two, one])[0]["kind"] == "pseudonym"


# ---- residuals: the identifier surviving where no field looked ----

RESIDUAL_SAMPLE = ["client_id=654321 uri=/clients/654321/orders status=200",
                   "client_id=111222 uri=/clients/111222/basket status=200"]


def _residual_token(rewrite=False):
    return {"field": "client_id", "pattern": r"\bclient_id=(\d+)", "enabled": True,
            "replacement": {"kind": "pseudonym", "rewrite_residuals": rewrite}}


def test_a_save_is_refused_when_the_identifier_survives_elsewhere(tmp_path, subkey):
    """Pseudonymising the field does not remove the value from the URL.

    The pack travels (git-sync, the export button), so shipping it would carry
    the identifier the operator asked to hide.
    """
    sub, fp = subkey
    cfg = pb.validate_config({"name": "c", "events": RESIDUAL_SAMPLE,
                              "tokens": [_residual_token()]})
    with pytest.raises(pb.BuilderError) as exc:
        pb.write_pack(cfg, str(tmp_path / "p"), subkey=sub, fingerprint=fp)
    message = str(exc.value)
    assert "still appears outside the field itself" in message
    assert "654321" not in message, "a refusal must not quote the value"


def test_the_preview_warns_where_the_save_refuses(subkey):
    # The operator is still editing; failing the live preview would just look
    # broken, so it reports the same problem as a warning.
    sub, _fp = subkey
    cfg = pb.validate_config({"name": "c", "events": RESIDUAL_SAMPLE,
                              "tokens": [_residual_token()]})
    out = pb.render_preview(cfg, n=2, seed=1, subkey=sub)
    assert any("outside the field itself" in w for w in out["warnings"])


def test_replace_everywhere_fixes_it_and_correlates(tmp_path, subkey):
    sub, fp = subkey
    cfg = pb.validate_config({"name": "c", "events": RESIDUAL_SAMPLE,
                              "tokens": [_residual_token(rewrite=True)]})
    dest = str(tmp_path / "p")
    pb.write_pack(cfg, dest, subkey=sub, fingerprint=fp)
    lines = open(os.path.join(dest, "samples", "c.sample")).read().splitlines()
    assert "654321" not in " ".join(lines) and "111222" not in " ".join(lines)
    for line in lines:
        field = re.search(r"client_id=(\d+)", line).group(1)
        in_uri = re.search(r"/clients/(\d+)/", line).group(1)
        assert field == in_uri, "the same identifier must appear in both places"
    assert bundles.lint_pack(dest).ok


def test_a_short_identifier_only_warns(subkey):
    # 3 digits genuinely appears inside an address or a size, so refusing would
    # make the feature unusable.
    sub, _fp = subkey
    cfg = pb.validate_config({"name": "c", "events": ["id=123 srcip=10.0.0.123"],
                              "tokens": [{"field": "id", "pattern": r"\bid=(\d+)",
                                          "replacement": {"kind": "pseudonym"}}]})
    out = pb.render_preview(cfg, n=1, seed=1, subkey=sub)
    assert any("may be a coincidence" in w for w in out["warnings"])
    assert not any("still appears outside" in w for w in out["warnings"])


def test_a_stand_in_coinciding_with_another_original_is_not_a_leak(subkey):
    """The blocker this scan would otherwise have shipped.

    With the format kept, the output space IS the input space, so a stand-in can
    equal a DIFFERENT original by chance. Reporting it refuses a build for a
    leak that does not exist; rewriting it would merge two identities.
    """
    from server.packbuilder import pseudonym as _ps
    sub, _fp = subkey
    pair = None
    for n in range(100, 1000):
        a = str(n)
        s = _ps.pseudonym(a, sub)
        if s != a and _ps.pseudonym(s, sub) not in (s, a):
            pair = (a, s)
            break
    assert pair, "no usable coincidence in a 900-value space"
    a, s = pair
    cfg = pb.validate_config({"name": "c", "events": ["id=%s x" % a, "id=%s x" % s],
                              "tokens": [{"field": "id", "pattern": r"\bid=(\d+)",
                                          "replacement": {"kind": "pseudonym"}}]})
    out = pb.render_preview(cfg, n=2, seed=1, subkey=sub)
    # event 1's stand-in literally IS event 2's original, and that is fine
    assert out["events"][0] == "id=%s x" % s
    assert not any("outside the field" in w or "coincidence" in w
                   for w in out["warnings"])


def test_an_identifier_left_in_a_values_list_is_reported(tmp_path, subkey):
    # "Values I list" copies what it saw into the pack, so an identifier that
    # also appeared in another field would ship verbatim.
    sub, fp = subkey
    cfg = pb.validate_config({"name": "c", "events": RESIDUAL_SAMPLE, "tokens": [
        _residual_token(),
        {"field": "note", "pattern": r"status=(\d+)",
         "replacement": {"kind": "values", "values": ["200", "654321"]}}]})
    with pytest.raises(pb.BuilderError) as exc:
        pb.write_pack(cfg, str(tmp_path / "p"), subkey=sub, fingerprint=fp)
    assert "listed values of note" in str(exc.value)


def test_api_lookup_tells_the_operator_what_to_search_for(client, upload_dir):
    """Without this an operator cannot act on their own data: they know client
    654321 exists but not which stand-in to put in a Splunk search."""
    config = {"name": "look", "events": RESIDUAL_SAMPLE,
              "tokens": [_residual_token(rewrite=True)]}
    created = client.post("/api/pack-builder/packs", json={"config": config})
    assert created.status_code == 201, created.text
    sample = open(os.path.join(created.json()["source_path"], "samples",
                               "look.sample")).read()

    r = client.post("/api/pack-builder/pseudonym-lookup",
                    json={"values": ["654321", "111222", "not-in-the-pack"]})
    assert r.status_code == 200, r.text
    body = r.json()
    results = {row["value"]: row["stand_in"] for row in body["results"]}
    # the answer is literally what is in the pack
    assert results["654321"] in sample and results["111222"] in sample
    assert len(body["key_fingerprint"]) == 8
    # a value that was never in a pack still resolves: the mapping is a keyed
    # function, not a stored table
    assert results["not-in-the-pack"]

    # The policy is part of the identity, so a different widen gives a
    # different answer and the caller must match the field.
    wide = client.post("/api/pack-builder/pseudonym-lookup",
                       json={"values": ["654321"],
                             "widen": {"shape": "digits", "length": 15}}).json()
    assert wide["results"][0]["stand_in"] != results["654321"]
    assert len(wide["results"][0]["stand_in"]) == 15


def test_api_lookup_never_creates_a_key(client, upload_dir, db_session):
    from server import pseudonymkeys

    r = client.post("/api/pack-builder/pseudonym-lookup", json={"values": ["1"]})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "pseudonym_key_unavailable"
    assert pseudonymkeys.peek(db_session) is None


# --------------------------------------------------------------------------- #
# Identity rotation: a new identity per replay, optionally aligned
# --------------------------------------------------------------------------- #

from server.packbuilder import pseudonym as _ps  # noqa: E402

ROT_SUB = _ps.derive_subkey(bytes(range(32)))
ROT_EVENTS = ["user=483920 action=login",
              "user=771045 action=register",
              "user=483920 action=logout"]


def _rot_cfg(rotate=True, scope="pass", period=None, order="sequential", widen=None):
    rep = {"kind": "pseudonym", "rotate": rotate}
    if widen is not None:
        rep["widen"] = widen
    cfg = {"name": "Sessions", "events": list(ROT_EVENTS), "order": order,
           "count": -1, "interval": 1,
           "tokens": [{"field": "user", "pattern": r"user=(\d+)", "replacement": rep}]}
    if rotate:
        cfg["rotation"] = {"scope": scope}
        if period is not None:
            cfg["rotation"]["period"] = period
    return cfg


def _conf_of(tmp_path, cfg, name="pack"):
    clean = pb.validate_config(cfg)
    out = pb.write_pack(clean, os.path.join(str(tmp_path), name),
                        subkey=ROT_SUB, fingerprint="fp123")
    with open(os.path.join(out, "default", "eventgen.conf"), encoding="utf-8") as fh:
        conf = fh.read()
    with open(os.path.join(out, "pack.yaml"), encoding="utf-8") as fh:
        manifest = fh.read()
    with open(os.path.join(out, "samples", "sessions.sample"), encoding="utf-8") as fh:
        sample = fh.read()
    return clean, conf, manifest, sample, out


def test_rotation_writes_the_token_and_the_stanza_setting(tmp_path):
    clean, conf, manifest, sample, _ = _conf_of(tmp_path, _rot_cfg())
    assert clean["rotation"] == {"scope": "pass", "fields": ["user"]}
    assert "rotate.scope = pass" in conf
    assert "rotate.period" not in conf, "a period only means something for windows"
    assert "token.0.replacementType = rotate" in conf
    assert "token.0.replacement = keep" in conf
    # The sample still holds build-time stand-ins, never the originals: rotation
    # is applied on top of them by the engine, so an engine that does not know
    # the token emits a stable pseudonym rather than a real identifier.
    assert "483920" not in sample and "771045" not in sample
    assert "rotation:" in manifest
    assert "algorithm: rotate-counter-v1" in manifest
    assert "engine: firebox" in manifest


def test_rotation_is_the_first_token_in_the_conf(tmp_path):
    """It has to see the stand-in the pack was written with.

    The engines apply tokens in conf order to the same event text, so a token
    that rewrote the span first would leave the rotation matching nothing (or
    worse, rotating some other field's inserted value).
    """
    cfg = _rot_cfg()
    cfg["tokens"].append({"field": "action", "pattern": r"action=(\w+)",
                          "replacement": {"kind": "values",
                                          "values": ["login", "logout"]}})
    _clean, conf, _m, _s, _out = _conf_of(tmp_path, cfg)
    assert "token.0.replacementType = rotate" in conf
    assert "token.1.replacementType = file" in conf


def test_aligned_rotation_writes_its_period(tmp_path):
    clean, conf, manifest, _s, _out = _conf_of(tmp_path, _rot_cfg(scope="window", period=300))
    assert clean["rotation"]["period"] == 300
    assert "rotate.scope = window" in conf
    assert "rotate.period = 300" in conf
    assert "algorithm: rotate-window-v1" in manifest
    assert "period_s: 300" in manifest


def test_aligned_rotation_defaults_its_period(tmp_path):
    clean, conf, _m, _s, _out = _conf_of(tmp_path, _rot_cfg(scope="window"))
    assert clean["rotation"]["period"] == pb.ROTATE_PERIOD_DEFAULT
    assert "rotate.period = 60" in conf


def test_a_widen_flows_into_the_rotate_replacement(tmp_path):
    """One width setting covers the stand-in and the rotation.

    The pack's stand-ins are already widened, so `keep` would widen the
    rotation anyway, but writing it explicitly means a hand-read conf says what
    it does.
    """
    for widen, expected in [({"shape": "digits", "length": 15}, "digits(15)"),
                            ({"shape": "hex", "length": 16}, "hex(16)"),
                            ({"shape": "guid"}, "guid")]:
        _c, conf, _m, _s, _o = _conf_of(tmp_path, _rot_cfg(widen=widen),
                                        name="w-%s" % expected[:5])
        assert "token.0.replacement = %s" % expected in conf


def test_rotation_refuses_random_event_order():
    """randomizeEvents picks a line at random, so there is no pass.

    The three events of one user would land in three different identities,
    which destroys exactly the correlation rotation exists to keep. Refusing
    beats emitting something that looks rotated and correlates nothing.
    """
    with pytest.raises(pb.BuilderError) as exc:
        pb.validate_config(_rot_cfg(order="random"))
    assert "sequential" in str(exc.value)
    # ...and the same pack without rotation is fine in random order.
    pb.validate_config(_rot_cfg(rotate=False, order="random"))


def test_no_rotation_means_no_token_and_no_setting(tmp_path):
    clean, conf, manifest, _s, _out = _conf_of(tmp_path, _rot_cfg(rotate=False),
                                               name="plain")
    assert clean["rotation"] is None
    assert "rotate" not in conf
    assert "rotation:" not in manifest


@pytest.mark.parametrize("scope", ["hourly", "", "PASS?"])
def test_rotation_rejects_an_unknown_scope(scope):
    cfg = _rot_cfg()
    cfg["rotation"] = {"scope": scope}
    if scope == "":
        # empty falls back to the default rather than erroring
        assert pb.validate_config(cfg)["rotation"]["scope"] == "pass"
        return
    with pytest.raises(pb.BuilderError):
        pb.validate_config(cfg)


def test_the_capacity_warning_states_how_long_rotation_lasts():
    """Easy to underestimate, and silent when it runs out.

    A 6-digit id with 2 distinct values has 450,000 identities per worker; at
    load-test rates that is minutes, so the number has to be on the screen
    while the operator can still widen the field.
    """
    clean = pb.validate_config(_rot_cfg())
    cfg, report = pb.apply_pseudonyms(clean, subkey=ROT_SUB)
    cap = pb.rotation_capacity(cfg, report)
    assert cap["passes"] == 900000 // 2
    assert cap["events"] == cap["passes"] * 3
    assert cap["tightest"]["field"] == "user"
    notes = " ".join(pb.rotation_warnings(cfg, report))
    assert "450,000 identities per worker" in notes
    assert "Widen" in notes

    # Widening makes it effectively unlimited, and the warning says a bigger number.
    wide = pb.validate_config(_rot_cfg(widen={"shape": "digits", "length": 15}))
    wcfg, wreport = pb.apply_pseudonyms(wide, subkey=ROT_SUB)
    assert pb.rotation_capacity(wcfg, wreport)["passes"] > 10 ** 14


def test_the_aligned_warning_explains_the_cardinality_trade():
    clean = pb.validate_config(_rot_cfg(scope="window", period=60))
    cfg, report = pb.apply_pseudonyms(clean, subkey=ROT_SUB)
    notes = " ".join(pb.rotation_warnings(cfg, report))
    assert "every sourcetype" in notes
    assert "60s window" in notes


def test_the_preview_warns_about_rotation():
    clean = pb.validate_config(_rot_cfg())
    out = pb.render_preview(clean, 3, 1, subkey=ROT_SUB)
    assert any("identities per worker" in w for w in out["warnings"])

# The tests that run the real firebox binary live in
# worker/tests/test_rotation_parity.py, which is the suite CI runs AFTER it
# builds firebox. Here they would find no binary and skip, which reads as
# passing while proving nothing.


@pytest.mark.parametrize("scope,period", [("pass", None), ("window", 300)])
def test_reopening_a_rotating_pack_keeps_its_settings(tmp_path, scope, period):
    """An operator who reopens a pack to change one word must not lose rotation.

    The settings live in stoker-builder.json, so a round trip through
    write_pack / read_builder_config / validate_config has to preserve the
    scope, the period, the per-field flag and the widen. Silently dropping any
    of them would turn rotation off on the next save, and the pack would look
    fine.
    """
    cfg = _rot_cfg(scope=scope, period=period,
                   widen={"shape": "digits", "length": 15})
    clean = pb.validate_config(cfg)
    out = pb.write_pack(clean, os.path.join(str(tmp_path), "first"),
                        subkey=ROT_SUB, fingerprint="fp")
    reopened = pb.read_builder_config(out)
    again = pb.validate_config(reopened)

    assert again["rotation"]["scope"] == scope
    assert again["rotation"]["fields"] == ["user"]
    if period is not None:
        assert again["rotation"]["period"] == period
    rep = again["tokens"][0]["replacement"]
    assert rep["rotate"] is True
    assert rep["widen"] == {"shape": "digits", "length": 15}

    # And a rebuild must not hash the stand-ins a second time, which would
    # silently stop the pack correlating with every other one.
    out2 = pb.write_pack(again, os.path.join(str(tmp_path), "second"),
                         subkey=ROT_SUB, fingerprint="fp",
                         already=reopened["events"])
    with open(os.path.join(out, "samples", "sessions.sample"), encoding="utf-8") as fh:
        first = fh.read()
    with open(os.path.join(out2, "samples", "sessions.sample"), encoding="utf-8") as fh:
        second = fh.read()
    assert first == second, "a rebuild changed the stand-ins"
