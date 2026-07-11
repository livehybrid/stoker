# Stoker worker contract (Phase 0)

Authoritative spec for the worker image `ghcr.io/livehybrid/stoker-worker`. The full system design lives in the AIOS workspace at `data/eventgen-orchestrator/DESIGN.md`; this file is the buildable contract for the worker only. If code and this file disagree, this file wins until amended.

## Process model

One container, two processes:

1. **Agent** (`worker/agent/`, entrypoint `python -m stoker_agent`): owns the control-plane conversation, the pacing token bucket, the HEC client and all counters. Starts first, listens on a unix socket, then spawns the engine.
2. **Engine** (eventgen subprocess): `python -m splunk_eventgen generate <rewritten.conf>` from the vendored tree. Generates events and hands every one to the agent through the output plugin over the unix socket. Knows nothing about HEC, JWTs or the control plane.

Data path: engine → `stoker` output plugin → unix socket (NDJSON) → agent reader → token bucket → HEC batch queue → gzip POST to Splunk. Backpressure: when the token bucket or HEC queue is full the agent stops reading the socket, the plugin's blocking send stalls and the engine slows. Bounded memory by construction.

## Environment contract

Managed mode (launched by a driver):

| Var | Meaning |
|---|---|
| `STOKER_RUN_ID` | run id |
| `STOKER_CONTROL_URL` | control plane base URL, e.g. `https://stoker.cloud.livehybrid.com` |
| `STOKER_RUN_JWT` | per-run bearer token, opaque to the agent |
| `STOKER_TOTAL_WORKERS` | fleet size N |
| `STOKER_HOLDER` | stable holder name; default = hostname |
| `STOKER_HINT_SLOT` | optional; k8s passes `JOB_COMPLETION_INDEX` |
| `STOKER_HEC_TOKEN` | HEC token, projected by the driver (never in the slice JSON) |

Standalone mode (Phase 0 exit test, no control plane): `STOKER_STANDALONE=1` plus:

| Var | Meaning |
|---|---|
| `STOKER_BUNDLE` | local path or URL to a bundle `.tgz` (or a bare pack directory) |
| `STOKER_BUNDLE_SHA256` | optional; verified when set and bundle is fetched over HTTP |
| `STOKER_HEC_URL` | e.g. `http://192.168.0.222:8088` |
| `STOKER_HEC_TOKEN` | HEC token |
| `STOKER_INDEX` / `STOKER_SOURCETYPE` / `STOKER_HOST_FIELD` / `STOKER_SOURCE` | event metadata overrides (index required) |
| `STOKER_RATE_MODE` | `eps` \| `per_day_gb` \| `count_interval` |
| `STOKER_RATE_VALUE` | number (EPS or GB/day; ignored for count_interval) |
| `STOKER_DURATION_S` | bounded run length; empty = unbounded |
| `STOKER_SLOT` / `STOKER_TOTAL_WORKERS` | default 0 / 1 |
| `STOKER_ENGINE` | `eventgen` (only engine in Phase 0) |

In standalone mode the agent synthesises a spec slice identical to a claim response, sets `T0 = now + 2 s`, logs heartbeat lines to stdout instead of POSTing, and ignores fencing/dead-man (there is no control plane to lose). Everything else (pacing, HEC, drain, SIGTERM) behaves identically.

Common tuning (defaults in brackets): `STOKER_OUTPUT_SOCKET` (`/tmp/stoker-output.sock`), `STOKER_HEARTBEAT_S` (5), `STOKER_OVERDRIVE` (1.15), `STOKER_CATCHUP_S` (5), `STOKER_METRICS_PORT` (9100, prometheus_client; 0 disables), `STOKER_DRAIN_BUDGET_S` (40; whole-drain deadline, kept under the 45 s SIGTERM budget), `STOKER_DEADMAN_S` (600; drivers set 1800 for eks fleets).

## Spec slice (claim response / standalone synthesis)

```json
{
  "run_id": 812, "slot": 2, "total_workers": 4, "lease_id": "le_9f",
  "engine": "eventgen",
  "bundle": {"url": "https://.../api/agent/bundles/9c1f0a2b.tgz", "sha256": "9c1f0a2b..."},
  "share": {"eps": 1543},
  "duration_s": 14400,
  "hec": {"url": "http://192.168.0.222:8088", "index": "loadtest",
          "sourcetype": null, "gzip": true, "ack": false},
  "overrides": {"host": "apigw-2"},
  "telemetry": {"interval_s": 5},
  "released": false
}
```

`share` carries exactly one key: `eps`, `per_day_gb` or `count` (count_interval mode). The agent treats the JWT as opaque; it never validates or decodes it.

## Control-plane conversation (managed mode)

All under `{CONTROL_URL}/api/agent/runs/{run_id}/`, `Authorization: Bearer {STOKER_RUN_JWT}`, `protocol_version: 1` in claim and heartbeat bodies.

1. `POST claim {holder, hint_slot, protocol_version}` → slice (above). Retry with backoff until success or dead-man.
2. Fetch bundle, verify sha256, unpack, rewrite conf (below), warm engine, `POST ready {slot, lease_id}`.
3. Poll heartbeat until response carries `{"command":"release","t0":"<iso8601>"}`. Start generating at exactly T0 (absolute wall clock).
4. `POST heartbeat` every `telemetry.interval_s` with `{slot, lease_id, protocol_version, events_total, bytes_total, eps, hec_2xx, hec_4xx, hec_5xx, hec_timeouts, retries, queue_depth, lag_s, rss_mb, cpu_pct, state}`. Response commands: `continue` | `release {t0}` | `retarget {share}` | `drain`. Response may carry `jwt` (rolling refresh): replace the bearer.
5. On drain/duration end/SIGTERM: stop engine, flush HEC queue (bounded 20 s), `POST final {slot, summary, log_tail}` with the last 50 engine log lines, exit 0. The whole drain is clamped to `STOKER_DRAIN_BUDGET_S` (40 s): every stage (socket join, engine grace, HEC flush, final POST) is bounded against a single deadline so an unreachable HEC and control plane cannot together push the drain past the SIGTERM budget. The dead-man also applies while waiting for release, so a control plane that dies before T0 self-evicts rather than hanging.

**Fencing:** a successful heartbeat ack is the lease renewal. After 30 s without one, pause generation (stop releasing tokens; the engine backpressures). Resume only when a heartbeat succeeds and confirms this `lease_id` is still the holder. A `superseded` response is a fatal drain-and-exit. **Dead-man:** no successful heartbeat for `STOKER_DEADMAN_S` (600; drivers set 1800 for eks fleets) → drain and exit. **Effective T0:** on a re-issued lease the claim response's `effective_t0` (claim time) replaces the run T0 as the pacing anchor, so replacements start with zero backlog.

## Pacing (the ±1 % mechanism)

- Conf rewrite runs the engine 15 % hot (`STOKER_OVERDRIVE`); the agent releases events to the HEC queue against the wall clock.
- Quota owed at time t: `owed = share_eps × (t − effective_t0)` (per_day_gb converts via bundle `bytes_per_event` estimate to an EPS equivalent for gating; byte accounting still uses real bytes).
- Token bucket: release the next event iff `released < owed`. Capacity bounds catch-up: if `owed − released > share_eps × STOKER_CATCHUP_S`, slide the anchor forward so at most 5 s of backlog is ever replayed, and expose the discarded shortfall as `lag_s`.
- `count_interval` mode: no gating (engine-paced); the socket reader feeds the HEC queue directly.
- `lag_s` = `max(0, (owed − released)) / share_eps` measured against the current anchor.
- Retarget within ±15 % of the current share: adjust `share_eps` in place. Beyond that: rewrite conf, restart engine (SIGTERM subprocess, wait, respawn), a logged 5 to 10 s gap.

## Conf rewrite rules

Input: the bundle's `default/eventgen.conf` (RawConfigParser, `optionxform = str`, case preserved). Output: a private copy in the workdir.

1. Strip every `outputMode`, `httpevent*`, `splunkHost`, `splunkPort`, `splunkMethod`, `index`, `sourcetype`, `source`, `host` output-side key from every stanza; set `outputMode = stoker`. Metadata is stamped by the agent from slice overrides, never by the engine.
2. `eps` mode: per stanza `interval = 1`, `count = max(1, round(stanza_share × overdrive))`, `randomizeCount` removed. The worker's EPS share is apportioned across stanzas proportionally to declared per-stanza estimates, equally when undeclared (largest-remainder so integer counts sum exactly).
3. `per_day_gb` mode: scale each stanza's `perDayVolume` proportionally so the sum equals `share × overdrive`; stanzas without `perDayVolume` get the equal-split remainder.
4. `count_interval` mode: `count` split across workers by largest-remainder; `interval` untouched; everything else untouched.
5. `hourOfDayRate` / `dayOfWeekRate` are preserved verbatim in all modes.
6. Never touch `mode = replay` stanzas' pacing keys (`timeMultiple` etc.); replay is engine-paced and the control plane guarantees workers = 1.

## Unix socket protocol (plugin → agent)

Stream socket at `STOKER_OUTPUT_SOCKET`. One NDJSON envelope per event, one line each, UTF-8:

```json
{"time": 1752234567.123, "host": "apigw-2", "source": null, "sourcetype": null, "index": null, "event": "<raw event text>"}
```

`time` = the event's generated timestamp (epoch seconds, float) or null (agent stamps now). Fields other than `event` may be null; the agent fills nulls from slice `hec`/`overrides` (slice wins over plugin for index/sourcetype/source/host when the run declares overrides). The plugin uses blocking writes and no buffering beyond one line; a connect failure at plugin start is fatal for the engine (agent always listens first).

## HEC client

- Envelope `{time, host, source, sourcetype, index, event}` per event, newline-delimited JSON in the POST body to `/services/collector/event`.
- Batch flush at 512 KiB or 200 ms, whichever first. `Content-Encoding: gzip` level 6, always.
- Bounded in-memory queue (default 5 000 envelopes) between token bucket and senders; when full, the reader stalls (backpressure).
- 4 sender threads with pooled keep-alive connections (one `requests.Session` each).
- Retry 5xx/timeouts with exponential backoff plus jitter (0.5 s base, ×2, cap 30 s, max 5 attempts, then count `dropped`).
- 401/403: fail fast. Mark `auth_failed`, surface in the next heartbeat, stop retrying that batch (auto-abort is the control plane's job; standalone mode exits non-zero).
- 400: parse the HEC body `{"text": ..., "code": N}`; count the batch as `dropped_invalid`, never retry.
- Counters (thread-safe, read by heartbeat): `events_total, bytes_total (uncompressed), hec_2xx, hec_4xx, hec_5xx, hec_timeouts, retries, dropped, queue_depth`.
- Indexer ack: `ack: true` honoured later; Phase 0 ships the flag parsed but inactive.

## Engine packaging (vendored eventgen)

- `worker/engines/eventgen/` holds the vendored `splunk_eventgen` 7.2.1 tree: `eventgen_api_server/`, `splunk_app/`, controller/Redis paths and their imports **deleted**; upstream LICENSE and a `VENDOR.md` (exact tag, deletions, patches) kept.
- Dependency pins patched to installable-on-py3.9 versions in `worker/requirements.txt` (single source; Dockerfile installs it). No Flask, no Redis, no ujson unless the generate path genuinely imports them.
- `stoker.py` lives inside the vendored plugin directory (`lib/plugins/output/`), registered exactly like the stock output plugins. The registry key is `output.<filename stem>`, so `outputMode = stoker` requires this exact file name (amended from `stoker_output.py`, which would register as plugin `stoker_output`).
- The engine subprocess runs with its working directory rooted at the pack, so eventgen resolves relative file-token replacement paths (e.g. `token.N.replacement = samples/foo.sample`) against the pack rather than the container working directory.

## Constraints

- Python 3.9 compatible everywhere under `worker/` (no PEP 604 unions, no match statements). Local dev/test also runs on 3.12: guard nothing on minor versions.
- Runtime deps allowlist for the agent: stdlib, `requests`, `prometheus_client`. Vendored eventgen adds its own patched pins. Test deps: `pytest`, `pytest-timeout`.
- No secrets in logs. The HEC token appears in exactly one place: the `Authorization` header.
- Image: `python:3.9-slim` base, multi-arch (amd64, arm64), target < 400 MB.

## Phase 0 exit test

`docker run` (or a py3.9 venv) in standalone mode with the `packs/flatline` bundle at `STOKER_RATE_MODE=eps STOKER_RATE_VALUE=100` for 120 s against 192.168.0.222 `index=loadtest`: indexed event count within ±1 % of 12 000; agent exits 0 after a clean drain; `kill -TERM` mid-run flushes and exits 0 in < 45 s.
