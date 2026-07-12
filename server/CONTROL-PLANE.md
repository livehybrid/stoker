# Stoker control plane contract (Phase 1, stages 1-2)

Authoritative build spec for `server/` (the FastAPI control plane) and the
SwarmDriver. The system design lives at `data/eventgen-orchestrator/DESIGN.md`
in the AIOS workspace; this file is the buildable contract for the walking
skeleton. Where this file and the design disagree for this stage, this file
wins. The worker already exists and is tested; `docs/WORKER-CONTRACT.md` is the
authoritative agent-side protocol and the control plane must match it exactly.

## Scope

**In (stages 1-2, the walking skeleton):**
- FastAPI app, one process, Postgres via SQLAlchemy 2.0. Background supervisor loop.
- Core tables: targets, packs, bundles, specs, runs, worker_leases, metric_samples, run_events, fleets.
- Agent-facing API: claim, ready, heartbeat, final, bundle download (exact worker protocol).
- Operator API: targets CRUD + test, packs (register a local directory), specs CRUD, run, run status/metrics/logs/events, stop, scale, rescale.
- Server-owned run lifecycle: lease issuance, largest-remainder apportionment, T0 release gate, heartbeat command channel, fencing/lease-lapse, dead-man detection, a subset of auto-abort policies, boot reconciliation.
- ExecutionDriver interface (six methods) + SwarmDriver (Portainer API) + FakeDriver (in-process, for tests and local-without-swarm) + shared conformance suite.
- Bundle builder from a local pack directory (lint + content-addressed tar).
- Per-run JWT mint/verify, Fernet secret encryption, config from env.
- `infra/stacks/stoker/` stack.yml + deploy.py; `server/Dockerfile`; CI extension.

**Deferred (named, not silently dropped) to later stages:**
- Git repo sync / clone / webhook (`gitsync/` beyond the local-dir bundle builder).
- Schedules (cron), users/roles/authentik (a Traefik LAN allowlist stands in; operator API is unauthenticated behind that until stage 3).
- The React UI (stage 4; the API is UI-ready and returns everything the run-detail view needs).
- K8sDriver, EKS/Terraform (stage: design Phase 2/3).
- Target health background loop and the 24 h ceiling soak (ceilings ship as the conservative config table; health is probed on demand via `/targets/{id}/test` and at submit).
- Metric rollup/pruning cron (raw 5 s inserts only for now).

State that a deferred thing is deferred in code comments and `log.info` where a user might expect it, never fail silently.

## Module layout (`server/`)

```
app.py            uvicorn entry, app factory, lifespan (supervisor loop), router registration, serves ui/dist if present
config.py         env parsing -> frozen Settings (secrets repr=False)
db.py             SQLAlchemy engine/session, get_db dependency, Base, create_all + alembic baseline
models.py         ORM models (below)
crypto.py         Fernet encrypt/decrypt; per-run JWT mint/verify (PyJWT HS256)
schemas.py        pydantic request/response models
engines/apportion.py   largest-remainder share split (port the worker's algorithm)
engines/ceilings.py    per-engine ceiling table + slice-exceeds-ceiling check
bundles.py        local-pack lint + content-addressed tar builder + store
drivers/base.py   ExecutionDriver Protocol, RunSnapshot, DriverRef, DriverStatus, DriverError
drivers/fake.py   in-process driver (records desired state; optional local worker subprocess spawner)
drivers/swarm.py  SwarmDriver via Portainer API (endpoint from fleet config)
routes/agent.py   /api/agent/* (JWT bearer)
routes/api.py     /api/* operator endpoints
lifecycle.py      run state machine + provisioning + supervisor tick (pure-ish, driver injected)
tests/            pytest incl. drivers/test_conformance.py and an end-to-end test driving the real worker agent
```

Foundation files (`config, db, models, crypto, schemas, engines/*, drivers/base, drivers/fake, bundles, app` skeleton with stub routers/lifecycle) are built first and fix every interface; feature files are filled in against them. `app.py` registers routers by importing `router` objects, so feature agents never edit `app.py`.

## Data model (SQLAlchemy 2.0, dialect-agnostic)

Use `sqlalchemy.JSON` for `*_json` columns via `JSONB().with_variant(JSON(), "sqlite")` so prod is Postgres JSONB and the test suite runs on SQLite. Timestamps are timezone-aware UTC. Secret columns store Fernet ciphertext and are never serialised into any response.

- **targets**: id, name (unique), hec_url, token_encrypted, default_index, verify_tls (bool), env_tag (lab/prod), max_concurrent_gb_day (float), health_state (unknown/green/amber/red), health_detail, last_health_at, lifetime_gb (float, default 0).
- **packs**: id, name, source_path (absolute local dir for this stage), description, tags_json, engines_json, sourcetypes_json, stanza_count, est_bytes_per_event (float), declared_per_day_gb (float, null), verified (bool), lint_status (ok/error/unknown), lint_errors_json, indexed_sha (null this stage).
- **bundles**: id, pack_id (fk), digest (unique, sha256 of the tarball), size_bytes, path, created_at. Immutable.
- **specs**: id, name, pack_id (fk), ref (default "local"), target_id (fk), engine (`eventgen` | `rawreplay`; default eventgen), overrides_json (index/sourcetype/source/host, values may contain `{slot}`), rate_mode (eps/per_day_gb/count_interval), rate_value (float, null for count_interval), interval_s (null), workers (int), duration_s (null=unbounded), fleet (default swarm-local), strict_release (bool), driver_opts_json. A `rawreplay` spec is always workers=1 (forced at provision; a multi-worker rawreplay spec is rejected at submit).
- **runs**: id, spec_id (fk), spec_snapshot_json (frozen, non-secret only), resolved_sha, bundle_id (fk), state (pending/preparing/provisioning/releasing/running/draining/completed/stopped/failed), degraded (bool), jwt_kid, driver_ref_json, started_by, created_at, t0 (null until release), ended_at, end_reason, totals_json.
- **worker_leases**: id, run_id (fk), slot (int), share_json (one key: eps|per_day_gb|count), lease_id (unique str), holder (str null), node (str null), state (free/claimed/ready/running/lost/done), last_heartbeat_at, effective_t0, restarts (int), final_log_tail_json. Unique (run_id, slot).
- **metric_samples**: id, run_id, slot, ts, and the heartbeat counters (events_total, bytes_total, eps, bps, hec_2xx, hec_4xx, hec_5xx, hec_timeouts, retries, queue_depth, lag_s, rss_mb, cpu_pct). Batched insert per heartbeat.
- **run_events**: id, run_id, ts, actor (system/operator/agent), kind, detail_json. Append-only audit trail; every state transition writes one.
- **fleets**: id, name (unique), driver (swarm/k8s/fake), config_json (portainer endpoint id + host; or fake), version_info, last_seen_at. Seed `swarm-local` (swarm) and `fake-local` (fake) at first boot from config.

`runs.spec_snapshot_json` embeds the target by id plus non-secret fields only. A test asserts no token or secret material appears in any GET response body.

## Agent-facing API (`/api/agent`, `Authorization: Bearer <per-run JWT>`)

This must match `docs/WORKER-CONTRACT.md` and the worker's `stoker_agent/control.py` byte-for-byte on the wire. The worker treats the JWT as opaque. Every request validates the bearer: decode, check `run_id` claim equals the path run id, check not expired, else 401. A request whose `lease_id` is not the current holder of its slot gets `{"command":"superseded"}` on heartbeat, 409 on ready/final.

- `POST /api/agent/runs/{run_id}/claim` body `{holder, hint_slot?, protocol_version}` -> the **spec slice** (exact shape in `WORKER-CONTRACT.md` and DESIGN Appendix A):
  ```json
  {"run_id":812,"slot":2,"total_workers":4,"lease_id":"le_9f","engine":"eventgen",
   "bundle":{"url":"{PUBLIC_BASE_URL}/api/agent/bundles/<digest>.tgz","sha256":"<digest>"},
   "share":{"per_day_gb":22.5},"duration_s":14400,
   "hec":{"url":"...","index":"loadtest","sourcetype":null,"gzip":true,"ack":false},
   "overrides":{"host":"apigw-2"},"telemetry":{"interval_s":5},
   "released":false,"effective_t0":null}
  ```
  Issue the lowest free lease (honour `hint_slot` when that slot's lease is free). Record holder, set state claimed, stamp last_heartbeat_at, set effective_t0 (run t0 on first claim, now on re-issue). `share` carries exactly one key matching rate_mode. `overrides.host` etc. have `{slot}` substituted. A re-claim of a still-held lease by the same holder is idempotent.
- `POST /api/agent/runs/{run_id}/ready` body `{slot, lease_id}` -> `{}` 200. Marks the lease ready. When all N are ready (or the 120 s provisioning->releasing timeout fires in the supervisor) set `t0 = now + 2 s` and move the run to releasing/running.
- `POST /api/agent/runs/{run_id}/heartbeat` body `{slot, lease_id, protocol_version, ...counters..., state}` -> a command:
  - `{"command":"continue"}` normally,
  - `{"command":"release","t0":"<iso8601 Z>"}` once T0 is set and until the agent has acked past it,
  - `{"command":"retarget","share":{...}}` when the stored slot share changed (scale/rescale),
  - `{"command":"drain"}` when the run is draining/stopping or `protocol_version` is unsupported,
  - `{"command":"superseded"}` when this lease_id is no longer the slot holder.
  May also carry `"jwt":"<fresh>"` when the current token is within 20 % of expiry. A successful heartbeat renews the lease (updates last_heartbeat_at) and appends the counters to metric_samples. Parse counters defensively.
- `POST /api/agent/runs/{run_id}/final` body `{slot, summary, log_tail}` -> `{}`. Store final_log_tail, fold summary into runs.totals_json, mark the lease done. When all leases are done/lost move the run to completed/stopped/failed as the drain reason dictates.
- `GET /api/agent/bundles/{digest}.tgz` -> the tarball bytes (JWT-checked; the run must reference that bundle). 404 if unknown.

## Operator API (`/api`, unauthenticated behind the Traefik LAN allowlist this stage)

- `POST /api/targets` `{name,hec_url,token,default_index,env_tag,max_concurrent_gb_day,verify_tls?}` -> target (no token echoed). `POST /api/targets/{id}/test` probes `/services/collector/health` + an auth ping -> `{ok,health,auth,latency_ms}` and updates health_state. `GET /api/targets`, `GET /api/targets/{id}`, `DELETE` (guarded when referenced).
- `POST /api/packs` `{name, source_path}` registers and lints a local pack directory (configparser parse, every sample-mode stanza's sample file exists, tokens compile, outputMode absent-or-ignored); sets verified/lint_status; measures est_bytes_per_event when the pack.yaml omits it. `GET /api/packs`, `GET /api/packs/{id}`, `GET /api/packs/{id}/preview` (stanzas + first 10 sample lines).
- `POST /api/specs` (Appendix A JobSpec), `GET`, `GET /api/specs/{id}/estimate` (per-worker share, pct of ceiling, approx eps/gb, ok bool), `PUT`, `DELETE`.
- `POST /api/specs/{id}/run` `{overrides?}` -> `201 {run_id,state}` after validate (target health, ceiling, replay-single-worker, engine/pack consistency, per-target cap, lint ok) + snapshot + bundle + apportion + provision. Rejections: `422 slice_exceeds_ceiling{suggested_workers}`, `409 target_unhealthy`, `409 target_cap_exceeded{headroom_gb_day}`, `409 replay_single_worker` (a rawreplay engine/pack, or a `mode = replay` stanza, with workers>1), `422 engine_pack_mismatch` (a rawreplay spec on a non-rawreplay pack). A rawreplay run is forced to workers=1 and its worker env carries `STOKER_ENGINE=rawreplay`.
- `GET /api/runs`, `GET /api/runs/{id}` (state, snapshot, totals, lease roster, event log), `GET /api/runs/{id}/metrics?res=5s&window=15m`, `GET /api/runs/{id}/logs?slot=&tail=`, `POST /api/runs/{id}/stop {force?}`, `POST /api/runs/{id}/scale {workers}`, `POST /api/runs/{id}/rescale {rate_value}`.

## Run lifecycle (server-owned)

State machine per DESIGN section 5: `pending -> preparing -> provisioning -> releasing -> running -> draining -> {completed|stopped}`, with `failed` from provisioning/auto-abort. Every transition appends a run_event.

- **Provision** (`POST /specs/{id}/run`): validate, freeze spec_snapshot, resolve bundle (build if absent), apportion shares across `workers` slots by largest-remainder (seed the worker_leases rows, all `free`), mint the per-run JWT (kid stored on the run), then `driver.create(RunSnapshot, workers)` and store the DriverRef. State -> provisioning.
- **Claim/ready/release**: agents claim free leases; when all ready or the 120 s timeout, set T0. Supervisor and heartbeat both can trigger the release evaluation.
- **Supervisor tick** (every ~2 s in the lifespan loop):
  - lease lapse: a claimed/running lease with `now - last_heartbeat_at > 30 s` -> state `lost`, share freed for re-claim; run_event. Lapse uses `max(last_heartbeat_at, boot_time) + 60 s` grace so a control-plane restart never lapses the estate.
  - release timeout: provisioning run past 120 s with >=1 ready -> release the ready subset, re-apportion across ready slots, mark `degraded` (unless strict_release -> fail).
  - auto-abort subset: >50 % leases lost for 5 min -> fail; sustained hec auth-fail flag across half the fleet (from heartbeat auth_failed) -> fail + flag target unhealthy; duration elapsed -> drain.
  - dead-man is the worker's own job (600 s); the server just observes the resulting lapse.
  - completion: all leases done/lost -> terminal state from end_reason.
- **Boot reconciliation**: on startup list driver workloads labelled `stoker.run`; adopt those matching live runs, destroy strays the control plane owns, fail orphaned DB runs whose workload is gone.
- **Stop/scale/rescale**: stop -> state draining, next heartbeats answer `drain`, driver.stop(grace 45), then destroy after grace. scale -> driver.scale + re-apportion + `retarget` shares pushed via heartbeat. rescale -> re-apportion the same worker count + `retarget`.

## ExecutionDriver

```python
@dataclass
class RunSnapshot: run_id:int; image:str; env:dict[str,str]; labels:dict[str,str]; \
    driver_opts:dict; stop_grace_s:int = 45
@dataclass
class DriverRef: kind:str; id:str; raw:dict            # opaque handle stored on the run
@dataclass
class DriverStatus: desired:int; running:int; tasks:list[dict]  # slot/holder/node/state

class ExecutionDriver(Protocol):
    def create(self, run: RunSnapshot, workers: int) -> DriverRef: ...
    def scale(self, ref: DriverRef, workers: int) -> None: ...
    def stop(self, ref: DriverRef, grace_s: int) -> None: ...
    def destroy(self, ref: DriverRef) -> None: ...
    def status(self, ref: DriverRef) -> DriverStatus: ...
    def logs(self, ref: DriverRef, slot: int | None, tail: int) -> str: ...
```

- **SwarmDriver**: Portainer API at `{PORTAINER_HOST}` with `X-API-Key: {PORTAINER_TOKEN}`, endpoint id from the fleet config (default 6). `create` = `POST /api/endpoints/{ep}/docker/services/create` with a service named `stoker-run-<id>`, `Mode.Replicated.Replicas=N`, `TaskTemplate.ContainerSpec.Image=<image>` + `Env` (RUN_ID/CONTROL_URL/RUN_JWT/TOTAL_WORKERS + HEC token projection via `STOKER_HEC_TOKEN`), labels `stoker.run=<id>`, `RestartPolicy.Condition=on-failure`, `StopGracePeriod` 45 s (ns), placement spread across nodes. `scale`/`stop`/`destroy` via service update/remove (`X-Registry-Auth` not needed for public GHCR pulls; image pinned by digest). `status` reads `/tasks?filters={service}`; map DesiredState/Status.State to slots is best-effort (swarm has no stable slot; identity is the lease, not the task). `logs` via `/services/{id}/logs` or per-task logs. Never mount docker.sock. All calls: `verify=False` for the self-signed Portainer cert, short timeouts, raise `DriverError` on non-2xx.
- **FakeDriver**: records desired replicas in memory and returns a synthetic DriverRef/DriverStatus; used by conformance and the operator-API tests. Optional mode spawns the real worker as a local subprocess (managed mode env pointed at the test server) for the end-to-end test.
- **Conformance suite** (`tests/drivers/test_conformance.py`): parametrised over every driver that can run in this environment (always FakeDriver; SwarmDriver only when `STOKER_TEST_PORTAINER=1`), asserting create->status(desired=N)->scale->stop->destroy transitions and idempotent destroy.

## Bundles

`bundles.build_from_pack(pack) -> Bundle`: lint the local pack dir, write `stoker.json` (name, estimates, engine), tar the pack (default/eventgen.conf + samples/ + pack.yaml + stoker.json) reproducibly, sha256 the bytes, store at `{BUNDLE_DIR}/<digest>.tgz`, upsert the bundles row (content-addressed, reused if the digest exists). The worker fetches it over `/api/agent/bundles/{digest}.tgz` with the run JWT and verifies the sha256 (its `bundle.py` already does). The tar must unpack to a directory containing `default/eventgen.conf` (the worker's `_find_pack_root` accepts root or one level down).

### rawreplay (Piston) packs

A rawreplay pack declares `engine: rawreplay` and a `replay:` section (`dataset` a pack-relative path, **or** `dataset_url` an https URL; `mode: rate|cadence`; `time_multiple`) with sourcetype/source in `defaults`, and needs **no** eventgen.conf. Lint (`bundles.lint_rawreplay_pack`): the local `dataset` exists and is path-contained (reuses the safe-join containment), or a `dataset_url` is a valid https URL; `est_bytes_per_event` is measured from the dataset. `build_from_pack` includes the dataset in the tarball; a `dataset_url` is **fetched at build time** (https only, size-capped via `RAWREPLAY_MAX_DATASET_BYTES`, sha-verified via `replay.dataset_sha256`) and embedded at `dataset/replay.dat`. `stoker.json` records the `replay` block (in-bundle dataset path, mode, time_multiple, sourcetype, source) so the worker reads it. A local `dataset` alongside a `dataset_url` treats the URL as provenance only (never fetched). Git-sync (`index_packs`) recognises a rawreplay pack (engine/`replay:` in pack.yaml, no conf) as a valid pack, sets `engines_json=["rawreplay"]`, and keeps the custom-code + path-escape guards. Per-worker ceiling: reuses eventgen's (25 GB/day, 5000 EPS) — a rawreplay run is workers=1, so per-worker == whole-run.

## Config (env)

`DATABASE_URL` (default `sqlite:///./stoker.db` for local dev; prod `postgresql+psycopg://...`), `STOKER_MASTER_KEY` (Fernet key; dev default generated with a loud warning), `STOKER_JWT_TTL_S` (default 3600), `PUBLIC_BASE_URL` (what workers use to reach the control plane and bundles; e.g. `https://stoker.cloud.livehybrid.com` or `http://<host>:8080` locally), `WORKER_IMAGE` (`ghcr.io/livehybrid/stoker-worker@sha256:<digest>` or `:latest` for local), `PORTAINER_HOST`, `PORTAINER_TOKEN` (tier-0), `PORTAINER_ENDPOINT` (default 6), `BUNDLE_DIR` (default `/data/bundles`), `DOGFOOD_HEC_URL`/`DOGFOOD_HEC_TOKEN` (optional self-telemetry, off if unset), `RAWREPLAY_MAX_DATASET_BYTES` (default 512 MiB; cap on a rawreplay `dataset_url` fetch), `RAWREPLAY_FETCH_TIMEOUT_S` (default 120), `PORT` (default 8080). Secrets excluded from Settings repr.

## Testing

- Unit: apportionment (sums exactly), ceilings, lease state machine, JWT round-trip + tamper reject, bundle build/dedup, target-token never echoed, driver conformance (FakeDriver).
- Integration/e2e (the proof): start the app with a TestClient/ASGI transport and a FakeDriver, create target+pack+spec, `POST /run`, then run the **real** `stoker_agent` (managed mode, `STOKER_CONTROL_URL` -> the test server via an httpx transport shim or a live uvicorn on a port) through claim -> ready -> release at T0 -> heartbeats -> final, asserting the run reaches `completed`, leases end `done`, and metric_samples accrued. This reuses the vendored engine + a tiny pack to prove the whole path without a swarm.
- Test DB is SQLite (dialect-agnostic models); prod is Postgres. CI runs the server suite on 3.12 alongside the worker suite.
- No secret material in any GET body (asserted).

## Deploy

`infra/stacks/stoker/stack.yml` per DESIGN section 11 (stoker app unpinned + `postgres:16` pinned to hpelite + `tiredofit/db-backup`, networks traefik_default + stoker_internal, the Router 1 / Router 2 auth split, `stop_grace_period: 45s`). `infra/stacks/stoker/deploy.py` cloned from `infra/stacks/fleet-telemetry` (Portainer, endpoint 6, env from `/opt/aios/.env`, `--dry-run`/`--status`). `.env.example` lists every var above. `server/Dockerfile` (python:3.12-slim, non-root, uvicorn, serves `ui/dist` when present). CI: add a `server` test job and a `stoker` (control plane) image build mirroring the worker image job.

## Conventions

British English in prose; no secrets in logs (JWT/token/master key never logged); the control plane never generates load; the DB is the source of truth and the driver is queried, never trusted as a store; push not pull (commands ride heartbeat responses). Python 3.12, FastAPI, SQLAlchemy 2.0, pydantic v2, httpx, PyJWT, cryptography.
