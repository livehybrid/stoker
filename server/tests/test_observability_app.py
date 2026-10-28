"""The shipped Splunk app (dashboards/stoker-observability) reads what the
control plane sends.

The dashboards' SPL names fields of the dogfood events; a renamed body key
would leave a panel silently empty on a customer's search head. These tests
pin the views to the event builders, the Studio JSON to its generator, and
every Simple XML token to an input.
"""

from __future__ import annotations

import importlib.util
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from server import metrics_lifecycle
from server.models import MetricSample

from . import _helpers

APP = Path(__file__).resolve().parents[2] / "dashboards" / "stoker-observability"
VIEWS = APP / "app" / "default" / "data" / "ui" / "views"
SIMPLE = sorted(p for p in VIEWS.glob("*.xml") if not p.stem.endswith("_studio"))
TOKEN = re.compile(r"\$([A-Za-z0-9_.]+)(?:\|s)?\$")
BUILT_IN = ("row.", "click.", "form.", "env:", "job.", "result.")


def _generator():
    spec = importlib.util.spec_from_file_location(
        "gen_studio_dashboards", APP / "tools" / "gen_studio_dashboards.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _studio_definition(path):
    text = path.read_text(encoding="utf-8")
    body = text.split("<definition><![CDATA[", 1)[1].split("]]></definition>", 1)[0]
    return json.loads(body)


@pytest.mark.parametrize("path", SIMPLE, ids=lambda p: p.stem)
def test_simple_xml_parses_and_every_token_has_an_input(path):
    root = ET.parse(path).getroot()
    defined = set()
    for node in root.iter("input"):
        token = node.get("token")
        defined.add(token)
        if node.get("type") == "time":
            defined |= {token + ".earliest", token + ".latest"}
    for query in root.iter("query"):
        for token in TOKEN.findall(query.text or ""):
            assert token in defined or token.startswith(BUILT_IN), (
                "%s: $%s$ has no input" % (path.name, token))


def test_studio_views_match_their_generator():
    gen = _generator()
    assert _studio_definition(VIEWS / "stoker_fleet_observability_studio.xml") == gen.fleet()
    assert _studio_definition(VIEWS / "stoker_run_detail_studio.xml") == gen.detail()


def test_every_props_stanza_covers_a_sourcetype_the_views_read():
    props = (APP / "app" / "default" / "props.conf").read_text()
    stanzas = set(re.findall(r"^\[(stoker:[a-z]+)\]", props, re.M))
    read = set()
    for path in VIEWS.glob("*.xml"):
        read |= set(re.findall(r"sourcetype=(stoker:[a-z]+)", path.read_text()))
    assert read <= stanzas, "no KV_MODE=json for %s" % sorted(read - stanzas)


def _worker_fields_read():
    """Fields the views take latest()/avg()/max() of, or filter on, from
    stoker:worker searches."""
    fields = set()
    for path in VIEWS.glob("*.xml"):
        text = path.read_text()
        for chunk in text.split("sourcetype=stoker:worker")[1:]:
            chunk = chunk.split("</query>", 1)[0].split('"name"', 1)[0]
            fields |= set(re.findall(r"(?:latest|avg|max)\(([a-z_]+)\)", chunk))
            fields |= set(re.findall(r"\b(lease_state|run_state)\s*(?:=|IN)", chunk))
    return fields - {"_time"}


def test_the_worker_panels_read_fields_the_worker_events_carry(
        db_session, make_pack, settings):
    ctx = _helpers.full_run(db_session, make_pack(), settings, workers=1,
                            rate_mode="eps", rate_value=1000.0)
    run = ctx["run"]
    lease = _helpers.leases_by_slot(db_session, run)[0]
    lease.state = "running"
    lease.holder = "w"
    lease.share_json = {"eps": 1000.0, "_engine_impl": "firebox"}
    lease.last_heartbeat_at = run.created_at
    db_session.add(MetricSample(
        run_id=run.id, slot=0, eps=900.0, bps=1.0, events_total=1,
        bytes_total=1, lag_s=0.0, queue_depth=0, hec_2xx=1, hec_4xx=0,
        hec_5xx=0, hec_timeouts=0, retries=0, rss_mb=1.0, cpu_pct=1.0,
        engine_cpu_pct=50.0, engine_threads=1, cpu_limit_cores=1.0))
    db_session.commit()
    rows = metrics_lifecycle._lease_samples(db_session, run)
    body = metrics_lifecycle._worker_bodies(run, rows)[0]

    read = _worker_fields_read()
    assert read, "no stoker:worker panels found"
    missing = read - set(body)
    assert not missing, "dashboards read %s, which stoker:worker never sends" % sorted(missing)
