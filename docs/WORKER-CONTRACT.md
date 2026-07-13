# Stoker worker contract

Authoritative spec for the worker image `ghcr.io/livehybrid/stoker-worker`. The worker is one container running an **agent** plus one of two **engine** subprocesses. This file documents the process model, both engines, the full environment contract, the unix-socket protocol, pacing, drain and the exit summary. The pack/bundle format it references in full is [`PACKS.md`](PACKS.md). If code and this file disagree, the code (`worker/stoker_agent/*`, `worker/engines/eventgen`, `worker/engines/rawreplay`) wins; correct this file.

## Process model

One container, two processes:

1. **Agent** (`worker/stoker_agent/`, entrypoint `python -m stoker_agent`): owns the control-plane conversation, the pacing token bucket, the HEC client and all counters. Binds a unix socket first, then spawns the engine. Engine-agnostic: HEC delivery, pacing, metadata stamping, drain and control-plane wiring are identical for every engine.
2. **Engine** subprocess, one of:
   - **eventgen** (vendored `splunk_eventgen` 7.2.1): `python -m splunk_eventgen generate <rewritten.conf>`. Templates events from samples and streams every event to the agent through the `stoker` output plugin. Knows nothing about HEC, JWTs or the control plane.
   - **rawreplay / Piston** (`stoker_rawreplay`): `python -m stoker_rawreplay` (no conf argument; configured entirely from `STOKER_RAWREPLAY_*` env). Replays a recorded dataset byte-for-byte.
   - **metrics** (`stoker_metrics`): `python -m stoker_metrics` (no conf argument; configured from `STOKER_METRICS_*` env). Generates synthetic Splunk **metric** data points (`event: "metric"` + a `fields` object) over a shaped time series. Engine-paced (count_interval), sharding the series matrix by slot. See [metrics engine](#metrics-engine).

`STOKER_ENGINE` selects (`eventgen` default, `rawreplay` for Piston, `metrics`). The engine command can be overridden without a code change: `STOKER_ENGINE_CMD` (eventgen; shell-quoted, `{conf}` placeholder or the conf appended), `STOKER_RAWREPLAY_CMD` and `STOKER_METRICS_CMD`.

Data path: engine -> socket (NDJSON, one envelope per line) -> agent reader -> token bucket (gated modes) -> HEC batch queue -> gzip POST to Splunk. Backpressure is structural: when the token bucket is paused or the HEC queue is full the agent stops reading the socket, the kernel buffer fills and the engine's blocking `sendall` stalls. Bounded memory by construction.

## Environment contract

The agent runs in one of two modes: **managed** (a driver launches it against a control plane) or **standalone** (`STOKER_STANDALONE=1`, no control plane). `load_config` (`stoker_agent/config.py`) parses the contract into a frozen `Config`; a violation raises `ConfigError` naming the offending variable. `STOKER_HEC_TOKEN` and `STOKER_ENGINE` are read in both modes; the token is the only place a secret appears and is excluded from the config repr.

### Managed mode (driver-launched)

| Var | Required | Meaning |
|---|---|---|
| `STOKER_RUN_ID` | yes | run id |
| `STOKER_CONTROL_URL` | yes | control-plane base URL (must be `http(s)://`); trailing slash stripped |
| `STOKER_RUN_JWT` | yes | per-run bearer token, opaque to the agent |
| `STOKER_TOTAL_WORKERS` | yes | fleet size N (integer >= 1) |
| `STOKER_HEC_TOKEN` | yes | HEC token, projected by the driver (never in the slice JSON) |
| `STOKER_HOLDER` | no | stable holder name; default = `socket.gethostname()` |
| `STOKER_HINT_SLOT` | no | slot hint (integer >= 0); the K8sDriver maps `JOB_COMPLETION_INDEX` to it |
| `STOKER_ENGINE` | no | `eventgen` (default) or `rawreplay`; the driver projects it only for a non-default engine |

The control plane projects exactly `STOKER_RUN_ID`, `STOKER_CONTROL_URL` (= `PUBLIC_BASE_URL`), `STOKER_RUN_JWT`, `STOKER_TOTAL_WORKERS`, `STOKER_HEC_TOKEN`, and `STOKER_ENGINE` (non-default only). The HEC URL, index, rate, duration, bundle and overrides all arrive **in the claim response slice**, not as env vars.

### Standalone mode (`STOKER_STANDALONE=1`)

| Var | Required | Meaning |
|---|---|---|
| `STOKER_BUNDLE` | yes | local path, local `.tgz`, or `http(s)` URL to a bundle `.tgz` (bare pack dir also accepted) |
| `STOKER_HEC_URL` | yes | e.g. `http://192.168.0.222:8088` |
| `STOKER_HEC_TOKEN` | yes | HEC token |
| `STOKER_INDEX` | yes | target index (stamped as a run-declared override) |
| `STOKER_RATE_MODE` | yes | `eps` \| `per_day_gb` \| `count_interval` |
| `STOKER_RATE_VALUE` | if mode != count_interval | number > 0 (EPS or GB/day); required for `eps`/`per_day_gb`, ignored for `count_interval` |
| `STOKER_BUNDLE_SHA256` | no | verified when set and the bundle is a local `.tgz` or fetched over HTTP |
| `STOKER_SOURCETYPE` / `STOKER_HOST_FIELD` / `STOKER_SOURCE` | no | metadata overrides (host override is `STOKER_HOST_FIELD`) |
| `STOKER_DURATION_S` | no | bounded run length in seconds; empty or `0` = unbounded |
| `STOKER_SLOT` | no | this worker's slot; default `0`, must be `< STOKER_TOTAL_WORKERS` |
| `STOKER_TOTAL_WORKERS` | no | default `1` |
| `STOKER_ENGINE` | no | `eventgen` (default) or `rawreplay` |

In standalone mode the agent synthesises a spec slice identical to a claim response (`SpecSlice.from_standalone`), the `StandaloneControl` stub logs heartbeat lines to stdout and its first heartbeat returns a release with `T0 = now + 2 s`, and fencing/dead-man never fire (there is no control plane to lose). Everything else (pacing, HEC, drain, SIGTERM) behaves identically. The heartbeat cadence is `STOKER_HEARTBEAT_S`.

### rawreplay engine vars (`STOKER_RAWREPLAY_*`)

The agent sets these on the rawreplay subprocess from the pack's resolved replay config; they are the engine's own contract (`stoker_rawreplay/engine.py load_config`):

| Var | Default | Meaning |
|---|---|---|
| `STOKER_RAWREPLAY_DATASET` | — (required) | absolute path to the dataset inside the bundle; gzip-aware when it ends `.gz` |
| `STOKER_RAWREPLAY_MODE` | `rate` | `rate` \| `cadence`. The agent overrides the pack's declared mode to match the run's pacing (gated -> `rate`, `count_interval` -> `cadence`) |
| `STOKER_RAWREPLAY_TIME_MULTIPLE` | `1.0` | cadence gap scale (number >= 0); `0` collapses every gap to 0 |
| `STOKER_RAWREPLAY_FALLBACK_GAP_S` | `0.1` | cadence gap used when a line's timestamp is unparseable or out of order (number >= 0) |
| `STOKER_RAWREPLAY_TS_REGEX` | none | optional cadence regex; first capturing group (or whole match) is the timestamp text |
| `STOKER_RAWREPLAY_TS_STRPTIME` | none | optional `strptime` format applied to the `TS_REGEX` capture (assumed UTC) |
| `STOKER_RAWREPLAY_TS_FIELD` | none | reserved parity hook; `TS_REGEX` is the operative override |
| `STOKER_RAWREPLAY_CMD` | none | shell-quoted launcher override (mirrors `STOKER_ENGINE_CMD`) |

Without a `TS_REGEX`, cadence mode uses a built-in timestamp battery (ISO 8601 with optional fractional seconds and Z/offset, syslog `Mon DD HH:MM:SS`, `YYYY-MM-DD HH:MM:SS`, and delimited 10-/13-digit epoch seconds/millis).

### metrics engine vars (`STOKER_METRICS_*`)

The agent sets these on the metrics subprocess from the pack's resolved `metricgen` config; they are the engine's own contract (`stoker_metrics/engine.py load_config`):

| Variable | Default | Meaning |
|---|---|---|
| `STOKER_METRICS_CONFIG` | — (required) | absolute path to a JSON file (the agent writes the pack's `metricgen` block to the workdir) holding `{resolution_s, tz_offset_hours, seed, dimensions, metrics}` |
| `STOKER_METRICS_SLOT` | `0` | this worker's slot; with `TOTAL_WORKERS` it strides the series matrix (`series[slot::total]`) so the fleet partitions it without overlap |
| `STOKER_METRICS_TOTAL_WORKERS` | `1` | fleet size (the stride denominator) |
| `STOKER_METRICS_RESOLUTION_S` | from config `resolution_s` (else `10`) | grid period in seconds; each series emits one multi-metric event per tick |
| `STOKER_METRICS_CMD` | none | shell-quoted launcher override (mirrors `STOKER_ENGINE_CMD`) |

### Common tuning (both modes)

Defaults in brackets; each is validated with the stated minimum:

| Var | Default | Meaning |
|---|---|---|
| `STOKER_OUTPUT_SOCKET` | `/tmp/stoker-output.sock` | the agent's AF_UNIX stream listener |
| `STOKER_HEARTBEAT_S` | `5.0` | standalone heartbeat cadence (managed cadence comes from the slice `telemetry.interval_s`) |
| `STOKER_OVERDRIVE` | `1.15` | conf-rewrite headroom; the engine runs ~15 % hot (min 1.0) |
| `STOKER_CATCHUP_S` | `5.0` | bounded catch-up window (min 0) |
| `STOKER_METRICS_PORT` | `9100` | `prometheus_client` exporter port; `0` disables (and avoids importing it) |
| `STOKER_DEADMAN_S` | `600.0` | managed dead-man window (min 1). Drivers do not project it by default, so the 600 s default applies unless an operator sets it |
| `STOKER_DRAIN_BUDGET_S` | `40.0` | whole-drain deadline (min 1); kept under the 45 s SIGTERM budget |
| `STOKER_HEC_VERIFY_TLS` | `1` | verify HEC TLS certificates; `0` disables (self-signed labs) |
| `STOKER_LOG_LEVEL` | `INFO` | root log level (stderr) |
| `STOKER_ENGINE_CMD` | none | eventgen launcher override; `{conf}` placeholder or the conf is appended |
| `EVENTGEN_LOG_DIR` | `<workdir>/eventgen-logs` | eventgen's rotating log dir (created if absent); the agent sets it when unset |

## Spec slice (claim response / standalone synthesis)

```json
{
  "run_id": 812, "slot": 2, "total_workers": 4, "lease_id": "le_9f",
  "engine": "eventgen",
  "bundle": {"url": "https://.../api/agent/bundles/9c1f0a2b.tgz", "sha256": "9c1f0a2b..."},
  "share": {"eps": 1543},
  "duration_s": 14400,
  "hec": {"url": "http://192.168.0.222:8088", "index": "loadtest",
          "sourcetype": null, "source": null, "host": null,
          "gzip": true, "ack": false},
  "overrides": {"host": "apigw-2"},
  "telemetry": {"interval_s": 5},
  "released": false,
  "effective_t0": null
}
```

`share` carries exactly one key: `eps`, `per_day_gb` or `count` (which maps to `count_interval` mode). `hec.gzip` defaults `true`, `hec.ack` defaults `false`. The agent treats the JWT as opaque; it never validates or decodes it. `effective_t0` (ISO 8601), when present, is the pacing anchor for a re-issued lease.

## Control-plane conversation (managed mode)

All under `{CONTROL_URL}/api/agent/runs/{run_id}/`, `Authorization: Bearer {STOKER_RUN_JWT}`. `protocol_version: 1` is sent in the claim and heartbeat bodies (not in ready/final).

1. `POST claim {holder, protocol_version[, hint_slot]}` -> slice (above). Retried with exponential backoff + jitter (base 0.5 s, cap 30 s) until success or the dead-man window elapses (`DeadManError`).
2. Fetch the bundle, verify sha256, unpack, (eventgen) rewrite the conf, warm the engine, `POST ready {slot, lease_id}`.
3. Poll `heartbeat` until a response carries `{"command":"release","t0":"<iso8601>"}` (a missing `t0` means "now"). Wait until the absolute wall-clock T0.
4. `POST heartbeat` every `telemetry.interval_s` with `{slot, lease_id, protocol_version, state, events_total, bytes_total, eps, hec_2xx, hec_4xx, hec_5xx, hec_timeouts, retries, dropped, queue_depth, lag_s, rss_mb, cpu_pct}` (`auth_failed: true` is added once HEC auth fails). Response `command` is one of `continue` | `release` | `retarget {share}` | `drain` | `superseded`. Any response may carry `jwt` for a rolling refresh (the agent replaces its bearer). An unknown command is logged and ignored.
5. On drain / duration end / SIGTERM: stop the engine, flush the HEC queue, `POST final {slot, summary, log_tail}` with the last 50 engine log lines, exit. Every drain stage (socket join, engine grace, HEC flush, final POST) is bounded against a single deadline (`STOKER_DRAIN_BUDGET_S`) so an unreachable HEC and control plane together cannot push the drain past the SIGTERM budget. The dead-man also applies while awaiting release, so a control plane that dies before T0 self-evicts rather than hanging.

**Fencing:** a successful heartbeat ack is the lease renewal. After `FENCE_PAUSE_S` (30 s) without one the agent pauses generation (the token bucket pauses; the engine backpressures) and resumes on the next successful ack. A `superseded` response is a fatal drain-and-exit (`SupersededError`). **Dead-man:** no successful control-plane contact for `STOKER_DEADMAN_S` (600 s default) -> drain and exit 4. **Effective T0:** a re-issued lease's `effective_t0` replaces the run T0 as the pacing anchor, so replacements start with zero backlog.

## Pacing (the ~+/-1 % mechanism)

Implemented by the wall-clock token bucket (`pacing.py`). T0 is an absolute wall-clock instant shared across the fleet.

- The eventgen conf is rewritten ~15 % hot (`STOKER_OVERDRIVE`); the agent releases events against the wall clock so the delivered rate hits the exact share.
- Quota owed at time t: `owed = rate x (t - anchor)`. An event is released iff `released < owed`. For `per_day_gb` the share is converted to an EPS-equivalent for gating via the bundle's `bytes_per_event` estimate (falling back to 256 B/event when the pack declares none); byte accounting still uses real bytes.
- Bounded catch-up: when the backlog exceeds `rate x STOKER_CATCHUP_S` the anchor slides forward so at most `catchup_s` seconds of backlog is ever replayed; the discarded shortfall accumulates in `discarded_s`, and `lag_s = max(0, owed - released) / rate` reports the current backlog (capped at `catchup_s`).
- `count_interval` mode is **not** gated: the engine self-paces and the socket reader feeds the HEC queue directly.
- Retarget within +/-15 % of the current share adjusts the rate in place (owed(t) kept continuous, no burst/stall). Beyond that band (and always for `count_interval`) the conf is rewritten and the engine restarted (SIGTERM, wait, respawn) with a logged 5 to 10 s gap.

## eventgen conf rewrite (`confrewrite.py`)

> The full pack/bundle format (both engines, every `pack.yaml` and `replay:` field, git-sync discovery, the `dataset_url` safety rules and the `trusted`/`verified` distinction) is [`PACKS.md`](PACKS.md). This section covers only the conf rewrite the agent applies at run time.

Input: the pack's `default/eventgen.conf` (`RawConfigParser`, `optionxform = str`, case preserved). Output: a private copy in the workdir. Skipped entirely for rawreplay.

1. Strip every output-side key from `[default]` and every stanza (`outputMode`, `splunkHost`, `splunkPort`, `splunkMethod`, `index`, `sourcetype`, `source`, `host`, and any `httpevent*`), then set `outputMode = stoker` and `sampleDir = <pack samples dir>` on every stanza. Metadata is stamped by the agent from slice overrides, never by the engine.
2. `eps` mode: per stanza `interval = 1`, `count = max(1, round(stanza_share x overdrive))`, `randomizeCount` removed, and the diurnal shaping maps stripped (rule 5). The worker's EPS share is apportioned across stanzas proportionally to declared per-stanza estimates (count/interval), equally when undeclared, by largest remainder so integer counts sum exactly.
3. `per_day_gb` mode: scale each stanza's `perDayVolume` so the sum equals `share x overdrive`; stanzas without `perDayVolume` take the equal-split remainder.
4. `count_interval` mode: `count` split across workers by largest remainder using `slot`/`total_workers`; `interval` and everything else untouched.
5. The diurnal shaping maps (`hourOfDayRate`, `dayOfWeekRate`, `minuteOfHourRate`, `dayOfMonthRate`, `monthOfYearRate`) are preserved verbatim in `per_day_gb` and `count_interval`. In `eps` mode they are stripped from `[default]`/`[global]` and every non-replay stanza: `eps` is a flat instantaneous rate, so a diurnal map would make the engine under-produce during low-rate hours and starve the flat token bucket, breaching +/-1 %. Shaped volume belongs in `per_day_gb` mode.
6. `mode = replay` stanzas' pacing keys (`timeMultiple` etc.) are never touched: replay is engine-paced and the control plane guarantees workers = 1.

## rawreplay (Piston) engine

Piston replays a recorded dataset **byte-for-byte** (e.g. a `splunk/security_content` attack_data capture): the exact recorded lines, re-timestamped to now. Selected by `STOKER_ENGINE=rawreplay` (the driver sets it; the slice's `engine` is `rawreplay`). For a rawreplay run the agent skips the eventgen conf-rewrite and launches `python -m stoker_rawreplay`; HEC delivery, metadata stamping, the token bucket and the control-plane conversation are unchanged.

The agent derives the **engine mode from the run's pacing, not the pack's declared mode**, so the two halves always agree (it logs when they differ):

- **rate mode** (gated run, `rate_mode` = `eps` / `per_day_gb`): the engine streams the dataset HOT (blocking `sendall` backpressures) with `time = null`, so the agent stamps *now* and its token bucket delivers at the exact share. The dataset **loops** to fill the run duration. A closed socket (drain) is the normal end and is swallowed, not raised.
- **cadence mode** (ungated run, `rate_mode` = `count_interval`): the engine self-paces, sleeping the recorded inter-event gap x `time_multiple` and setting `time = base + cumulative_offset` (base = now at the first event) so the replayed timeline is contiguous from the run start. The agent does not gate. The dataset plays once. This is the existing "replay is engine-paced, workers = 1" rule.

Replay cannot be rate-sharded, so the control plane forces **workers = 1** for a rawreplay run (a multi-worker rawreplay spec is rejected `409 replay_single_worker` at submit, provision and scale). The dataset is gzip-aware (`.gz` decompressed on the fly) and decoded UTF-8 with byte-replacement so a binary-ish capture never kills the stream.

**Bundle shape (worker side).** The pack/bundle format is [`PACKS.md`](PACKS.md); a rawreplay pack declares `engine: rawreplay` and a `replay:` section and needs no `default/eventgen.conf`. What the worker reads from the built bundle's `stoker.json` `replay` block is only: `dataset` (the dataset's path **inside the bundle** — a fetched `dataset_url` lands at `dataset/replay.dat`, a local dataset keeps its pack-relative path), `mode`, `time_multiple`, and the optional `ts_regex`/`ts_strptime`/`ts_field` cadence hints. The manifest also carries `sourcetype`/`source`, but the worker ignores them and stamps metadata from the slice like eventgen. `dataset_url`/`dataset_sha256` are resolved at build time and are not in the manifest (the dataset is embedded, so the worker never re-fetches).

## metrics engine

The metrics engine generates synthetic Splunk **metric** data points over a shaped time series instead of log events. Selected by `STOKER_ENGINE=metrics` (the slice's `engine` is `metrics`); the agent skips the eventgen conf-rewrite and launches `python -m stoker_metrics`. HEC delivery, metadata stamping and the control-plane conversation are unchanged — only the payload differs (`event: "metric"` + a `fields` object; see the socket protocol below).

**Series matrix + sharding.** The pack's `metricgen` config declares `dimensions` (each a `key` + `values`); the **series matrix** is their cross-product. This worker owns `series[slot::total_workers]` — a deterministic stride, so the fleet partitions the matrix without overlap and with no cross-worker coordination. Each metric definition (`name`, `kind`, `min`/`p95`/`max`, `pattern`, optional per-dimension `scale`) is emitted for every owned series.

**Emission + pacing.** Metrics are **engine-paced** (the run uses `rate_mode = count_interval`, so the agent's socket reader is ungated, exactly like rawreplay cadence). The engine emits on a wall-clock-aligned grid of period `resolution_s`: at each tick, for each owned series, it computes every metric's value and sends **one multi-metric envelope** carrying all of them (`{"metric_name:a": .., "metric_name:b": .., <dimensions>}`). It loops until the socket closes on drain. `time` is the tick epoch. A worker that owns no series (workers > matrix size) idles until drain.

**Value model.** `value(t)` is `pattern_activity(t) in [0,1]` mapped onto `min + a*(p95 - min)` with noise, clamped to `[min, max]` — i.e. **min = quiet floor, p95 = busy-hours level, max = rare ceiling**. `kind` interprets it: `gauge` = the value; `count` = an integer per-interval count; `counter` = a monotonic cumulative total. Deterministic given the config `seed` (the control-plane preview and the worker produce the same curve). Patterns: `constant`, `sine`, `business_hours`, `business_double_hump`, `ramp`, `spike`, `random_walk` (`stoker_metrics/patterns.py`). Full pack/config format in [`PACKS.md`](PACKS.md).

**Bundle shape (worker side).** A metrics pack declares `engine: metrics` and a `metricgen` block in `stoker.json`; it ships a stub `default/eventgen.conf` only to satisfy the pack-root file contract (never executed). The worker reads the whole `metricgen` object (`resolution_s`, `tz_offset_hours`, `seed`, `dimensions`, `metrics`) and hands it to the engine via `STOKER_METRICS_CONFIG`.

## Backfill

Backfill generates a window of **history** (events/points stamped at their past time) as fast as the target accepts, up to a delivery cap. It is a per-run option: the control plane launches a **gated eps run at the cap** (so the token bucket paces delivery and a large window does not overwhelm Splunk) and carries the window in the claim slice:

```json
"backfill": {"start_s": <epoch>, "end_s": <epoch>, "resolution_s": <float|null>}
```

Both engines re-use the normal delivery path (the agent stamps nothing new; the engine sets the historical `time`):

- **metrics** — the engine (`STOKER_METRICS_BACKFILL_START_S`/`END_S`/`RESOLUTION_S`, set by the `MetricsRunner` from the slice) walks the window **in time order** (stateful `random_walk`/`counter` evolve correctly), stamps each point at its historical time, emits hot (the bucket paces), then **exits**. The agent's engine-exit path drains the run. Preserves the daily shape across the window.
- **eventgen** — `confrewrite` widens each stanza's timestamp window to `earliest = -<window>s`, `latest = now` so the sample's timestamp token stamps every event a historical time across `[now-window, now]` (text timestamp and `_time` agree). The run is bounded by the **duration deadline** (the control plane sizes it to the volume). Uniform density; the diurnal shape is not reproduced. (eventgen's native `backfill` rater is non-functional in the vendored tree, hence this approach.)

Standalone: `STOKER_BACKFILL_START_S` / `STOKER_BACKFILL_END_S` / `STOKER_BACKFILL_RESOLUTION_S`. **Caveat:** re-running a backfill appends duplicate points/events (Splunk metrics/`mstats` double-count) — run once, or clear the window first.

## Unix socket protocol (engine -> agent)

Stream socket at `STOKER_OUTPUT_SOCKET`. One NDJSON envelope per event, one line each, UTF-8. Both the eventgen `stoker` output plugin (`worker/engines/eventgen/.../plugins/output/stoker.py`) and the rawreplay engine speak it identically:

```json
{"time": 1752234567.123, "host": null, "source": null, "sourcetype": null, "index": null, "event": "<raw event text>"}
```

- `time` = the event's generated timestamp (epoch seconds, float) or `null` (agent stamps now); non-finite values are coerced to `null`. rawreplay sends `null` in rate mode and `now + offset` in cadence mode; eventgen forwards its `_time` (int from the default/perdayvolume paths, float from replay).
- Metadata fields (`host`/`source`/`sourcetype`/`index`) may be `null`. The agent fills them: a run-declared **override wins over the plugin value**; otherwise a `null` is filled from the slice `hec` default; a still-`null` value is omitted from the HEC body so Splunk applies its own default. rawreplay always leaves all four `null`.
- `event` is mandatory; a line whose `event` is `null`/absent, that is not a JSON object, or that fails to decode is counted `malformed` and dropped.
- `fields` is optional and used only by the **metrics** engine: it carries `metric_name:<name>` measurements plus dimension key/values, and `event` is the literal string `"metric"`. The agent passes `fields` through verbatim to the HEC event endpoint (Splunk stores it as a metric data point when `event == "metric"`); log-event envelopes have no `fields`. The metrics engine leaves the four metadata fields `null` (the metrics index/sourcetype come from the slice, like every other engine).
- Writes are blocking with no buffering beyond one line, so a stalled agent stalls the engine. A connect failure at engine start is fatal (the agent always binds the listener before launching the engine). The eventgen plugin's connection is sticky-dead on failure (no silent reconnect); the whole engine is restarted instead.

## HEC client (`hec_client.py`)

- Body: newline-delimited JSON of `{time, host, source, sourcetype, index, event}` (null metadata keys omitted) POSTed to `<hec_url>/services/collector/event`, header `Authorization: Splunk <token>`.
- Batch flush at 512 KiB of serialised NDJSON or 200 ms after the first event of a batch, whichever comes first. `Content-Encoding: gzip` level 6 when gzip is enabled (the default; slice `hec.gzip`).
- Bounded in-memory queue (default 5 000 envelopes) between the token bucket and 4 sender threads (each with its own pooled keep-alive `requests.Session`). A full queue blocks `put()` (backpressure).
- 5xx and timeouts/connection errors retry with exponential backoff + jitter (base 0.5 s, x2, cap 30 s, up to 5 attempts) then count `dropped`.
- `401`/`403`: fail fast, count `hec_4xx` + `dropped`, set `auth_failed` (surfaced in the next heartbeat; the control plane auto-aborts a fleet when half report it; standalone mode exits 3).
- `400`: parse the HEC body `{"text","code"}`, count `hec_4xx` + `dropped_invalid`, never retry. Other unexpected 4xx: `hec_4xx` + `dropped`, no retry.
- Thread-safe counters read by the heartbeat: `events_total`, `bytes_total` (uncompressed), `hec_2xx`, `hec_4xx`, `hec_5xx`, `hec_timeouts`, `retries`, `dropped`, `dropped_invalid`, plus live `queue_depth` and `auth_failed`. `events_total`/`bytes_total` are incremented only on a 2xx.
- Indexer ack (`hec.ack`) is parsed and stored but inactive.

## Drain, SIGTERM and the exit summary

SIGTERM/SIGINT set the drain flag (`request_drain`). The drain (`Agent._shutdown`) runs against one deadline (`STOKER_DRAIN_BUDGET_S`, 40 s): close the token bucket (unreleased socket data is dropped by design; only the HEC queue is flushed), signal the HEC client to stop (releasing any producer parked on a full queue), join the socket reader (<= 5 s), stop the engine (SIGTERM, 10 s grace, SIGKILL), flush the HEC queue (<= 20 s), then best-effort `POST final` (each attempt's timeout clamped to the time left).

`summary` in the final POST is the HEC snapshot (all counters above) plus:

- `reason` — the drain reason (`duration-complete`, `control-drain`, `superseded`, `dead-man`, `engine-exit`, `hec-auth-failed`, `setup-failure`, `signal-15`, ...).
- `flushed` — `true` when everything accepted was resolved within the flush budget (else remaining in-flight events are counted `dropped`).
- `state` — `"drained"`.
- `socket_received` / `socket_malformed` — envelopes accepted / rejected by the socket reader.
- `discarded_s` — cumulative seconds of quota discarded by bounded catch-up.

`log_tail` is the last 50 engine stdout/stderr lines (a daemon reader keeps a ring buffer). Exit codes: `0` clean drain, `2` config error, `3` HEC auth failure in standalone mode, `4` dead-man expiry.

## Engine packaging (vendored eventgen)

- `worker/engines/eventgen/` holds the vendored `splunk_eventgen` 7.2.1 tree: the API server, `splunk_app/`, controller/Redis paths and their imports deleted; upstream LICENSE and a `VENDOR.md` (exact tag, deletions, patches) kept.
- Dependency pins patched to installable-on-py3.9 versions in `worker/requirements.txt` (single source; the Dockerfile installs it).
- `stoker.py` lives in the vendored plugin directory (`lib/plugins/output/`) and registers as plugin `stoker` (the registry key is `output.<filename stem>`), so `outputMode = stoker` requires exactly this file name. `useOutputQueue = False` flushes inline on the generator worker thread so a blocked write backpressures generation directly.
- The eventgen subprocess runs with `cwd` rooted at the pack so relative file-token replacement paths (e.g. `token.N.replacement = samples/foo.sample`) and `sampleDir` resolve against the pack, and with `start_new_session=True` so the agent's SIGTERM does not hit it directly.

## Constraints

- Python 3.9 compatible everywhere under `worker/` (no PEP 604 unions, no match statements). Also runs on 3.12 for local dev/test; guard nothing on minor versions.
- Runtime deps (`worker/requirements.txt`, the single source the Dockerfile installs): `requests`, `prometheus-client` (agent) plus `jinja2`/`MarkupSafe`/`python-dateutil` (the vendored eventgen generate path). The rawreplay engine is stdlib-only. Test deps: `pytest`, `pytest-timeout`.
- No secrets in logs: the HEC token appears only in the `Authorization` header (and is excluded from `Config`/`HecClient` repr).
- Image: `python:3.9-slim`, multi-arch (amd64, arm64), cosign-signed, published as `ghcr.io/livehybrid/stoker-worker`. `PYTHONPATH=/app:/app/engines/eventgen:/app/engines/rawreplay`, runs as a non-root `stoker` user, entrypoint `python -m stoker_agent`.

## Standalone exit test

`docker run` (or a py3.9 venv) in standalone mode with the `packs/flatline` bundle at `STOKER_RATE_MODE=eps STOKER_RATE_VALUE=100` for 120 s against 192.168.0.222 `index=loadtest`: indexed event count within +/-1 % of 12 000; the agent exits 0 after a clean drain; `kill -TERM` mid-run flushes and exits 0 in < 45 s.
