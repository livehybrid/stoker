"""Bounded soak of live Stoker against the .222 `loadtest` index.

Steps (eps, workers) run one after another for STEP_S seconds each on the
swarm fleet through the real control plane, firebox engine, batched agent and
HEC. Per step: the run's own totals (events, bytes, HEC 2xx/4xx/5xx,
timeouts, retries, dropped) and Splunk's indexed count for that step's
unique source. Results go to results.json beside this file (docs/BENCHMARKS.md, "Live soak").

  set -a; . /var/lib/aios/secrets/stoker_harness.env; set +a
  python soak.py
"""
import json
import os
import sys
import time
import uuid

sys.path.insert(0, "/opt/aios/apps/stoker/harness")
from clients import Splunk as SplunkClient, StokerClient  # noqa: E402

STEPS = [(2000, 1), (5000, 1), (10000, 1), (20000, 2)]
STEP_S = 120
PACK = "nginx-access"
HERE = os.path.dirname(os.path.abspath(__file__))

api = StokerClient(os.environ["STOKER_URL"], os.environ["STOKER_TOKEN"])
splunk = SplunkClient(os.environ["SPLUNK_URL"], user=os.environ.get("SPLUNK_USERNAME"),
                      password=os.environ.get("SPLUNK_PASSWORD"))
pack = next(p for p in api.ok(api.get("/api/packs")) if p["name"] == PACK)
target = api.ok(api.post("/api/targets", json={
    "name": "soak-222-" + uuid.uuid4().hex[:6], "hec_url": os.environ["STOKER_TEST_HEC_URL"],
    "token": os.environ["STOKER_TEST_HEC_TOKEN"], "default_index": "loadtest",
    "env_tag": "soak", "verify_tls": False}), 201)
results = []
try:
    for eps, workers in STEPS:
        source = "stoker-soak-%d-%s" % (eps, uuid.uuid4().hex[:6])
        spec = api.ok(api.post("/api/specs", json={
            "name": "soak-%d" % eps, "pack_id": pack["id"], "target_id": target["id"],
            "workers": workers, "engine": "eventgen", "rate_mode": "eps", "rate_value": eps,
            "duration_s": STEP_S, "overrides": {"source": source, "index": "loadtest"}}), 201)
        t0 = time.time()
        run = api.wait_for_run(api.launch_run(spec["id"])["run_id"], timeout_s=STEP_S + 600)
        wall = time.time() - t0
        tot = run.get("totals_json") or {}
        indexed = splunk.count('index=loadtest source="%s"' % source, earliest="-%ds" % (STEP_S + 1200),
                               poll_until=int(tot.get("events_total") or 0), timeout_s=300)
        row = {"target_eps": eps, "workers": workers, "state": run["state"], "end_reason": run.get("end_reason"),
               "events": tot.get("events_total"), "bytes": tot.get("bytes_total"),
               "delivered_eps": round((tot.get("events_total") or 0) / float(STEP_S), 1),
               "achieved_pct": round(100.0 * (tot.get("events_total") or 0) / (eps * STEP_S), 1),
               "indexed": indexed, "hec_2xx": tot.get("hec_2xx"), "hec_4xx": tot.get("hec_4xx"),
               "hec_5xx": tot.get("hec_5xx"), "timeouts": tot.get("hec_timeouts"), "retries": tot.get("retries"),
               "dropped": tot.get("dropped"), "degraded": run.get("degraded"), "wall_s": round(wall),
               "run_id": run["id"]}
        results.append(row)
        print(json.dumps(row), flush=True)
        api.delete("/api/specs/%d" % spec["id"])
        json.dump(results, open(os.path.join(HERE, "results.json"), "w"), indent=1)
        time.sleep(20)
finally:
    api.delete("/api/targets/%d" % target["id"])
