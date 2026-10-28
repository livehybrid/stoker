# Stoker Observability (Splunk app)

Live run + fleet observability over Stoker's **dogfood telemetry** — the
control plane ships its own metrics to a Splunk HEC target, and this app turns
that stream into two dashboards:

- **Fleet Observability** — every run at once: active runs, fleet delivered eps
  (with target overlay), worst-worker pacing lag, **stacked throughput by run in
  both eps and MB/s** (each run's contribution to the fleet total), HEC
  outcomes, a **slots holding a run back** table (every unheld, lost, silent,
  CPU-bound, queueing, lagging or under-share slot of an active run), a runs
  table (click a row to drill in) and a recent-lifecycle feed.
- **Run Detail** — one run by id: delivered eps vs target, **delivered MB/s**,
  pacing lag, HEC outcomes (2xx/4xx/5xx/timeouts/retries), the run's
  lifecycle transitions, and **per worker**: each slot's share against what it
  delivered, its engine's CPU against the container's CPU limit, threads, lag,
  queue and RSS with a one-line verdict, plus eps and engine CPU by slot over
  time.

Throughput is derived from the telemetry's `eps` and `bps` (MB/s = `bps` ÷
1048576); the stacked areas make each run's share of the fleet total visible.

Each dashboard ships in **two builds**: a **Dashboard Studio** version (the
default in the app nav) and a **Simple XML** ("Classic") version with identical
queries. Use whichever you prefer — Studio for the modern builder, Simple XML if
you want to hand-edit or run on an older Splunk. The Studio views are generated
from `tools/gen_studio_dashboards.py` (edit the panel model there and re-run
rather than hand-editing the JSON).

### Where the data comes from

The control plane **pushes** its own telemetry to Splunk over HEC — nothing
pulls from Stoker. The load-generation workers report real measurements
(delivered eps, HEC outcomes, pacing lag) to the control plane on their
heartbeats; the control plane aggregates them per run and POSTs `stoker:job` /
`stoker:metrics` events to the HEC target you configure. These dashboards read
those events back out of Splunk.

The React run view in the control plane already shows **per-slot** live charts
for a single run. This app is the complement: a **cross-run, historical**
view in Splunk, alongside every other index you already have, that survives past
the run and aggregates the whole fleet.

## What it reads

Three sourcetypes the control plane emits when dogfood telemetry is enabled:

| sourcetype       | when                          | key fields |
|------------------|-------------------------------|------------|
| `stoker:metrics` | every `DOGFOOD_METRICS_INTERVAL_S` (default 30 s) per active run | `run_id`, `spec_id`, `state`, `eps`, `target_eps`, `bps`, `events_total`, `bytes_total`, `lag_s_max`, `live_workers`, `reporting_workers`, `hec_2xx`, `hec_4xx`, `hec_5xx`, `hec_timeouts`, `retries` |
| `stoker:worker`  | with each `stoker:metrics`, one per slot of the run, whatever its lease state | `run_id`, `spec_id`, `run_state`, `slot`, `lease_state`, `holder`, `node`, `restarts`, `engine_impl`, `assigned_work`, `share_eps` (or `share_per_day_gb` / `share_count`), `heartbeat_age_s`, `sample_age_s`, `eps`, `eps_of_share_pct`, `bps`, `events_total`, `bytes_total`, `lag_s`, `queue_depth`, `hec_*`, `retries`, `rss_mb`, `cpu_pct`, `engine_cpu_pct`, `engine_threads`, `cpu_limit_cores`, `engine_cpu_of_limit_pct` |
| `stoker:job`     | every run state transition    | `run_id`, `spec_id`, `from`, `to`, `end_reason`, `degraded` |

`eps` is the instantaneous fleet-wide delivered rate; `target_eps` is the run's
intended eps (present only for an `eps` run — `per_day_gb` / `count_interval`
runs do not pace to an eps figure). `lag_s_max` is the worst worker's pacing lag
across the fleet. `stoker:metrics` also carries `unheld_slots` (slots no worker
holds, whose share goes undelivered) and `engine_cpu_pct_max`. HEC counters are
cumulative per run.

On `stoker:worker`, `cpu_pct` is the worker agent alone; `engine_cpu_pct` is the
engine subprocess that generates the events (100 = one core, so a 4-thread
firebox can read up to 400). `cpu_limit_cores` is the container's CPU quota (or
the CPUs it may run on when there is none), and `engine_cpu_of_limit_pct` is the
engine against it: at 90 or more the slot is CPU-bound and a higher target will
not raise its rate; more workers or a bigger limit will. Engine figures need
workers built from this release; older workers leave them out.

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

1. Download `stoker_observability-<version>.tgz` from the GitHub Release for
   your Stoker version (every `v*` tag attaches it, checksum in `SHA256SUMS`)
   and install it via **Apps, Manage Apps, Install app from file**, or
   `splunk install app stoker_observability-<version>.tgz -update 1`. To build
   one from a checkout:
   ```
   python3 tools/package_splunk_apps.py --version 0.1.0 --out dist
   ```
2. Restart Splunk (or reload: `| rest /services/apps/local/_reload`).
3. Open **Stoker Observability** from the app menu.

`props.conf` in the app sets `KV_MODE = json` for all three sourcetypes so the JSON
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

- **Per-worker events are one per slot per interval.** At the default 30 s
  that is 2,880 events a day per slot; a 25-worker run sends about 72,000 a
  day, a few tens of MB.
- **HEC counters are cumulative.** The outcome panels show running totals (2xx
  climbs, errors step up); for a rate, wrap the metric in `delta`/`streamstats`.
- Simple XML, so it imports on Splunk 8/9/10 and is easy to review. To adopt it
  in Dashboard Studio, open a dashboard and use **Edit → Convert to Dashboard
  Studio** in Splunk Web.
