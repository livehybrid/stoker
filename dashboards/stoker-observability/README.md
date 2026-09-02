# Stoker Observability (Splunk app)

Live run + fleet observability over Stoker's **dogfood telemetry** — the
control plane ships its own metrics to a Splunk HEC target, and this app turns
that stream into two dashboards:

- **Fleet Observability** — every run at once: active runs, fleet delivered eps
  (with target overlay), worst-worker pacing lag, HEC outcomes, a runs table
  (click a row to drill in) and a recent-lifecycle feed.
- **Run Detail** — one run by id: delivered eps vs target, pacing lag, HEC
  outcomes (2xx/4xx/5xx/timeouts/retries) and the run's lifecycle transitions.

The React run view in the control plane already shows **per-slot** live charts
for a single run. This app is the complement: a **cross-run, historical**
view in Splunk, alongside every other index you already have, that survives past
the run and aggregates the whole fleet.

## What it reads

Two sourcetypes the control plane emits when dogfood telemetry is enabled:

| sourcetype       | when                          | key fields |
|------------------|-------------------------------|------------|
| `stoker:metrics` | every `DOGFOOD_METRICS_INTERVAL_S` (default 30 s) per active run | `run_id`, `spec_id`, `state`, `eps`, `target_eps`, `bps`, `events_total`, `bytes_total`, `lag_s_max`, `live_workers`, `reporting_workers`, `hec_2xx`, `hec_4xx`, `hec_5xx`, `hec_timeouts`, `retries` |
| `stoker:job`     | every run state transition    | `run_id`, `spec_id`, `from`, `to`, `end_reason`, `degraded` |

`eps` is the instantaneous fleet-wide delivered rate; `target_eps` is the run's
intended eps (present only for an `eps` run — `per_day_gb` / `count_interval`
runs do not pace to an eps figure). `lag_s_max` is the worst worker's pacing lag
across the fleet — the aggregate stream is run-level, so per-worker granularity
lives in the control plane's own React run view, not here. HEC counters are
cumulative per run.

## Enable the telemetry (control plane)

Point the control plane at a HEC target and it starts shipping. On the SOK
Terraform Deployment (or any env):

```
DOGFOOD_HEC_URL   = https://<your-splunk-hec>:8088
DOGFOOD_HEC_TOKEN = <a HEC token whose allowed indexes include the one you want>
# optional:
DOGFOOD_METRICS_INTERVAL_S = 30      # aggregate cadence
DOGFOOD_GZIP               = 1
```

The events land in the **HEC token's default index** (the envelope sets no
index of its own), so pick a token whose default index is where you want them —
e.g. a dedicated `stoker_telemetry` index.

## Install the app

1. Copy the `app/` directory into `$SPLUNK_HOME/etc/apps/stoker_observability/`
   (rename `app/` to `stoker_observability`), or package it:
   ```
   cd dashboards/stoker-observability
   cp -r app stoker_observability
   tar czf stoker_observability.tgz stoker_observability
   ```
   then install `stoker_observability.tgz` via **Apps → Manage Apps → Install
   app from file**.
2. Restart Splunk (or reload: `| rest /services/apps/local/_reload`).
3. Open **Stoker Observability** from the app menu.

`props.conf` in the app sets `KV_MODE = json` for both sourcetypes so the JSON
fields extract at search time — no re-indexing, safe to add to an existing
index.

## Using it

- Set the **Index** input to the index your dogfood token writes to (defaults to
  `*`, which works but is slower). Set the **Time range** to cover the run.
- On **Fleet Observability**, click any row in the runs table to open **Run
  Detail** for that run id, carrying the index and time range across.
- Both dashboards `autoRun` and honour the time picker, so they update live
  during a run (set the range to e.g. *Last 15 minutes*, real-time optional).

## Notes / limits

- **Cross-run, not per-worker.** The dogfood aggregate is per run; for per-slot
  pacing error use the control plane's React run view.
- **HEC counters are cumulative.** The outcome panels show running totals (2xx
  climbs, errors step up); for a rate, wrap the metric in `delta`/`streamstats`.
- Simple XML, so it imports on Splunk 8/9/10 and is easy to review. To adopt it
  in Dashboard Studio, open a dashboard and use **Edit → Convert to Dashboard
  Studio** in Splunk Web.
