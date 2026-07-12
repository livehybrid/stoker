# Stoker control plane

Reference for `server/` — the FastAPI control plane. This documents the code as
shipped: the data model and run lifecycle, the driver interface and its concrete
drivers, the two worker engines, the auth subsystem, and the HTTP surface. The
system design lives at `data/eventgen-orchestrator/DESIGN.md` in the AIOS
workspace; `docs/WORKER-CONTRACT.md` is the authoritative agent-side wire
protocol and this control plane matches it. Where prose and code disagree, the
code wins — every claim here was checked against `server/`.

The control plane never generates load. It owns state (the DB is the source of
truth), issues per-run leases, releases workers at a shared T0, drives the run
state machine, and pushes commands to workers on their heartbeat responses. The
driver is queried for desired/running counts, never trusted as a store.

## Module layout (`server/`)

```
app.py               uvicorn entry, app factory, lifespan (supervisor + maintenance + dogfood loops), auth middleware, router registration, serves ui/dist if present
config.py            env parsing -> frozen Settings (secrets repr=False)
db.py                SQLAlchemy engine/session, get_db dependency, Base, init_db (create_all)
models.py            ORM models (below)
crypto.py            Fernet encrypt/decrypt of target tokens; per-run JWT mint/verify (PyJWT HS256, domain-separated key)
auth.py              app-level auth backend: bcrypt password hashing, signed session cookie, trusted-proxy SSO resolver, bootstrap/setup helpers
schemas.py           pydantic v2 request/response models (no secret fields by construction)
engines/apportion.py largest-remainder share split
engines/ceilings.py  per-engine ceiling table + slice-exceeds-ceiling check
engines/known.py     known-engine registry
bundles.py           pack lint (eventgen + rawreplay) + content-addressed tar builder + rawreplay dataset fetch
preview.py           pack preview / preview_run rendering
gitsync/sync.py      git clone/fetch + pack indexing (custom-code + path-escape guards)
drivers/base.py      ExecutionDriver Protocol, RunSnapshot, DriverRef, DriverStatus, DriverError/NotFound
drivers/fake.py      in-process driver (records desired state; optional local worker subprocess spawner)
drivers/swarm.py     SwarmDriver via Portainer API
drivers/k8s.py       K8sDriver via the kubernetes client (Indexed Jobs)
drivers/__init__.py  get_driver(fleet) selection + per-fleet-name cache
lifecycle.py         run state machine, provision/scale/rescale/stop, supervisor tick, boot reconciliation, claim/ready/heartbeat/final, fleet seeding
metrics_lifecycle.py metric_samples roll-up + prune; optional dogfood self-telemetry to HEC
routes/agent.py      /api/agent/* (per-run JWT bearer)
routes/api.py        /api/* operator endpoints (targets/repos/hooks/packs/specs/runs)
routes/auth.py       /api/auth/* (session + setup) and /api/users/* (admin-only user CRUD)
tests/               pytest incl. drivers/test_conformance.py and an end-to-end test driving the real worker agent
```

`app.py` registers routers by importing their `router` objects, so feature
modules never edit `app.py`.

## Data model (SQLAlchemy 2.0, dialect-agnostic)

`*_json` columns use `JSONB().with_variant(JSON(), "sqlite")` so prod is
Postgres JSONB and the test suite runs on SQLite. Timestamps are timezone-aware
UTC (defaults set in Python). Secret columns store ciphertext / hashes and are
never serialised into any response schema (a test asserts no secret material
appears in any GET body).

- **targets**: id, name (unique), hec_url, token_encrypted (Fernet ciphertext of the HEC token), default_index, verify_tls (bool, default true), env_tag (lab/prod, default lab), max_concurrent_gb_day (float, null), health_state (unknown/green/amber/red), health_detail, last_health_at, lifetime_gb (float, default 0), created_at.
- **repos**: id, url, auth_kind (none/pat/deploy_key), secret_encrypted (Fernet ciphertext of a PAT or deploy key, write-only), default_ref (default main), head_sha, last_synced_at, sync_error, webhook_secret (per-repo HMAC secret, generated on create), trusted_code (bool — gates the custom-code default-deny), created_at.
- **packs**: id, name, source_path, description, tags_json, engines_json, sourcetypes_json, stanza_count, est_bytes_per_event (float), declared_per_day_gb (float, null), verified (bool), lint_status (ok/error/unknown), lint_errors_json, repo_id (fk, null for a locally-registered pack), indexed_sha (repo head SHA when git-synced, null for a local pack), created_at.
- **bundles**: id, pack_id (fk, null), digest (unique, sha256 of the tarball), size_bytes, path, created_at. Immutable, content-addressed.
- **specs**: id, name, pack_id (fk), ref (default local), target_id (fk), engine (`eventgen` | `rawreplay`, default eventgen), overrides_json (index/sourcetype/source/host, values may contain `{slot}`), rate_mode (eps/per_day_gb/count_interval, default eps), rate_value (float, null), interval_s (null), workers (int, default 1), duration_s (null=unbounded), fleet (default swarm-local), strict_release (bool), driver_opts_json, created_at.
- **runs**: id, spec_id (fk), spec_snapshot_json (frozen, non-secret only), resolved_sha, bundle_id (fk), state (pending/preparing/provisioning/releasing/running/draining/completed/stopped/failed), degraded (bool), jwt_kid, driver_ref_json, started_by, created_at, t0 (null until release), ended_at, end_reason, totals_json.
- **worker_leases**: id, run_id (fk), slot (int), share_json (one key: eps|per_day_gb|count), lease_id (unique str), holder (str null), node (str null), state (free/claimed/ready/running/lost/done), last_heartbeat_at, effective_t0, restarts (int), final_log_tail_json. Unique (run_id, slot). The lease is the unit of fencing identity.
- **metric_samples**: id, run_id, slot, ts, and the heartbeat counters (events_total, bytes_total, eps, bps, hec_2xx, hec_4xx, hec_5xx, hec_timeouts, retries, queue_depth, lag_s, rss_mb, cpu_pct). Appended per successful heartbeat; rolled up and pruned by the maintenance loop.
- **run_events**: id, run_id, ts, actor (system/operator/agent), kind, detail_json. Append-only audit trail; every state transition writes one.
- **fleets**: id, name (unique), driver (swarm/k8s/fake), config_json (portainer endpoint id + host; or kubeconfig context + namespace; or fake), version_info, last_seen_at, created_at. `fake-local` (fake) and `swarm-local` (swarm) are seeded at first boot from config; a k8s fleet is added by an operator (there is no seeded k8s fleet).
- **users**: id, username (unique), password_hash (passlib bcrypt, null for proxy/SSO users, never serialised), email, role (viewer/operator/admin, default operator), source (local/proxy, default local), active (bool, default true), created_at, last_login_at.

`runs.spec_snapshot_json` embeds the target by id plus non-secret fields only.

## Run lifecycle (server-owned)

State machine: `pending -> preparing -> provisioning -> releasing -> running ->
draining -> {completed | stopped}`, with `failed` reachable from
provisioning/auto-abort/boot-reconcile. Terminal states are
`{completed, stopped, failed}`. Every transition appends a `run_event`.

- **Provision** (`lifecycle.provision_run`, called by `POST /api/specs/{id}/run` after the route's submit gates): force workers=1 for a replay engine; freeze `spec_snapshot_json` (non-secret, target embedded by id); resolve the bundle (build from the pack dir if absent, reuse the content-addressed digest otherwise); create the run row (pending) to allocate its id; transition to preparing; apportion shares across `workers` slots by largest-remainder and seed the `worker_leases` rows (all `free`); mint the per-run JWT (store `jwt_kid` on the run); `driver.create(RunSnapshot, workers)` and store the `DriverRef`; transition to provisioning. A failed `driver.create` fails the run loudly (never a silent hang).
- **Claim / ready / release** (`lifecycle.claim_lease` / `mark_ready` / `evaluate_release`): agents claim free leases (lowest free slot, honouring `hint_slot`); a claim records the holder, sets the lease `claimed`, stamps `last_heartbeat_at` and `effective_t0` (run t0 on first claim, now on re-issue). When every non-lost lease is `ready` (or the supervisor forces a partial release on timeout), `t0 = now + RELEASE_DELAY_S` (2 s) and the run moves provisioning -> releasing, then -> running on the first post-T0 heartbeat.
- **Supervisor tick** (`lifecycle.supervisor_tick`, every ~2 s in the lifespan):
  - **lease lapse**: a claimed/ready/running lease whose deadline `max(last_heartbeat_at + LEASE_LAPSE_S, boot_time + BOOT_GRACE_S)` has passed (30 s heartbeat window; 60 s extra grace after a control-plane restart so a restart never lapses the estate) becomes `lost`, its share freed; run_event.
  - **release timeout**: a provisioning run past `PROVISION_TIMEOUT_S` (120 s) with >=1 ready releases the ready subset, re-apportions across ready slots and marks `degraded` — unless `strict_release`, which fails the run instead.
  - **auto-abort**: more than `AUTO_ABORT_LOST_FRACTION` (0.5) of running leases lost, sustained for `AUTO_ABORT_LOST_S` (300 s) -> fail; a sustained HEC auth-fail flag across half the fleet -> fail; duration elapsed -> drain.
  - **drain -> destroy**: a draining run past `STOP_GRACE_S` (45 s) since the drain event is destroyed via the driver.
  - **completion**: when all leases are done/lost the run reaches its terminal state from `end_reason`.
  - The 600 s dead-man is the worker's own job; the server just observes the resulting lapse.
- **Boot reconciliation** (`lifecycle.reconcile_on_boot`, one-shot on startup): phase 1 probes each non-terminal DB run's stored `DriverRef` via `driver.status` — adopt when the workload is present, fail as orphaned when it is gone or was never launched. Phase 2 sweeps stray workloads: for each driver that supports `list_run_ids`, destroy any labelled workload whose run id is not a live DB run. A driver that cannot enumerate (`NotImplementedError`) or whose enumeration errors is skipped for the sweep (never treated as "all strays").
- **Stop / scale / rescale**: `stop_run` -> draining, next heartbeats answer `drain`, `driver.stop(grace 45)`, then destroy after grace (or immediately when `force`). `scale_run` -> `driver.scale`, add/remove lease rows and re-apportion, changed shares pushed as `retarget` on the next heartbeat (a shrunk slot's worker self-supersedes). `rescale_run` -> re-apportion the same worker count and `retarget`. Replay runs are clamped to 1 worker in `scale_run` (belt-and-braces; the route rejects a grow up front).

## Engines

Two worker engines select via `STOKER_ENGINE` (default `eventgen`); the control
plane sets it in the worker env only for a non-default engine so the eventgen
env stays byte-for-byte unchanged.

- **eventgen** (vendored 7.2.1): templates events from a pack's samples per its `eventgen.conf`.
- **rawreplay ("Piston")**: replays a recorded dataset. RATE mode is agent-paced (the same token bucket as eventgen; the dataset loops and is re-stamped to now); CADENCE mode is engine-paced from the recorded inter-event gaps. This is the `splunk/security_content` attack_data use case.

**Replay is single-worker**, enforced in three places:
- **Submit** (`routes/api.py` `run_spec`): `_is_replay_run(spec, pack)` is true when the spec engine is `rawreplay`, the pack is a rawreplay pack (`pack.yaml` `engine`/`replay:`), or the pack's `eventgen.conf` has a `mode = replay` stanza. If so and `workers != 1`, `409 replay_single_worker`.
- **Provision** (`lifecycle.provision_run`): `effective_workers` clamps a `rawreplay` spec to 1 (and corrects the snapshot's worker count).
- **Scale** (`routes/api.py` `scale_run_endpoint` and `lifecycle.scale_run`): a grow of a replay run is rejected `409 replay_single_worker` at the route and clamped to 1 in the lifecycle.

Per-worker ceilings (`engines/ceilings.py`) are 25 GB/day and 5000 EPS for both
engines; a replay run is one worker so per-worker == whole-run.

### rawreplay (Piston) packs

A rawreplay pack declares `engine: rawreplay` and a `replay:` section (`dataset`
a pack-relative path **or** `dataset_url` an https URL; `mode: rate|cadence`;
`time_multiple`) with sourcetype/source in `defaults`, and needs no
`eventgen.conf`. Lint (`bundles.lint_rawreplay_pack`): a local `dataset` exists
and is path-contained, or a `dataset_url` is a valid https URL to a public host;
`est_bytes_per_event` is measured from a local dataset. `build_from_pack`
includes a local dataset in the tarball; a `dataset_url` (with no local dataset)
is fetched at build time (https only, public-host only, size-capped via
`RAWREPLAY_MAX_DATASET_BYTES`, sha-verified when `dataset_sha256` is declared)
and embedded at `dataset/replay.dat`. A local `dataset` alongside a
`dataset_url` treats the URL as provenance only (never fetched). `stoker.json`
records the resolved `replay` block so the worker reads it. Git-sync recognises
a rawreplay pack (engine/`replay:`, no conf) as valid, sets
`engines_json=["rawreplay"]`, and keeps the custom-code + path-escape guards.

## ExecutionDriver

```python
@dataclass
class RunSnapshot: run_id:int; image:str; env:dict[str,str]; labels:dict[str,str]; \
    driver_opts:dict; stop_grace_s:int = 45
@dataclass
class DriverRef: kind:str; id:str; raw:dict            # opaque handle stored on runs.driver_ref_json
@dataclass
class DriverStatus: desired:int; running:int; tasks:list[dict]  # slot/holder/node/state, best-effort

class ExecutionDriver(Protocol):
    def create(self, run: RunSnapshot, workers: int) -> DriverRef: ...
    def scale(self, ref: DriverRef, workers: int) -> None: ...
    def stop(self, ref: DriverRef, grace_s: int) -> None: ...
    def destroy(self, ref: DriverRef) -> None: ...
    def status(self, ref: DriverRef) -> DriverStatus: ...
    def logs(self, ref: DriverRef, slot: int | None, tail: int) -> str: ...
    def list_run_ids(self) -> set[int]: ...   # OPTIONAL 7th method (discovery only), raises NotImplementedError if unsupported
```

Six methods are the conformance contract; `list_run_ids` is an optional
discovery-only 7th method used solely by the boot stray-sweep. `DriverError` on
any backend failure; `NotFound` (a subclass) when a workload is genuinely absent,
so callers can tell "gone" from "transient". `get_driver(fleet)` maps a fleet
row (or a driver-name string) to a driver and caches one instance per fleet name.

- **SwarmDriver** (`drivers/swarm.py`): drives the Portainer Docker API at `{host}/api/endpoints/{ep}/docker/...` with `X-API-Key: <portainer_token>`, endpoint id from the fleet config (default 6), `verify=False` for the self-signed cert, short timeouts. One run == one replicated swarm service named `stoker-run-<id>` labelled `stoker.run=<id>`. `create` = `POST /docker/services/create` with `Mode.Replicated.Replicas=N`, `TaskTemplate.ContainerSpec.Image` + `Env`, `RestartPolicy.Condition=on-failure`, `StopGracePeriod` (ns) and a node-spread placement preference. `scale` re-reads the current spec and mutates only `Mode.Replicated.Replicas`. `stop`/`destroy` via service update/remove. `status` reads `/tasks?filters={"service":["stoker-run-<id>"]}` (best-effort slot mapping; swarm has no stable slot). `logs` via the service/task log endpoints. `list_run_ids` parses the `stoker.run` label (or the `stoker-run-<id>` name). Never mounts docker.sock.
- **K8sDriver** (`drivers/k8s.py`): drives the kubernetes client (BatchV1/CoreV1), namespace from the fleet config (default `stoker`), kubeconfig context selecting k3s (local) or EKS. One run == one Indexed `batch/v1` Job named `stoker-run-<id>` labelled `stoker.run=<id>`. `create` writes a per-run ephemeral **Secret** carrying the HEC token (referenced via `secretKeyRef`, never inline in the pod env) then the **Job** with `completionMode: Indexed`, `parallelism == completions == N`, `backoffLimit`, and `ttlSecondsAfterFinished` as the stray-catcher; the Secret is adopted under the Job via an `ownerReference` so it is GC'd with the Job. `scale` is Elastic Indexed Jobs (k8s >= 1.27): patch `parallelism` and `completions` together. `stop`/`destroy` delete the Job with `propagationPolicy: Foreground` (the Secret GCs via its ownerRef). `status` reads the Job (desired = `spec.parallelism`) and lists pods (the Indexed-Job pod env carries each pod's completion index, which the worker reads as its slot). `logs` reads pod logs. EKS Terraform for the cluster lives under `infra/`.
- **FakeDriver** (`drivers/fake.py`): records desired replicas in memory and returns a synthetic `DriverRef`/`DriverStatus`; used by conformance and the operator-API tests. An optional mode spawns the real worker as a local subprocess for the end-to-end test.

## Auth

App-level auth (`auth.py`, `routes/auth.py`, and the middleware in `app.py`).
Vendor-neutral: no dependency on any specific IdP. Two identity sources feed one
`users` table.

- **Local password users**: passlib **bcrypt** `password_hash`. `POST /api/auth/login` verifies the password and sets a signed, TTL-bounded **session cookie** (`stoker_session`; `itsdangerous` `URLSafeTimedSerializer` keyed off `STOKER_MASTER_KEY` with the salt `stoker-session-v1`, domain-separated from the Fernet and run-JWT uses of the same key; the cookie carries only the user id). Cookie is HttpOnly + SameSite=Lax; `Secure` follows the actual request scheme (honouring `X-Forwarded-Proto` behind a TLS-terminating proxy). Login failures are uniform ("invalid credentials") so they are not a user-enumeration oracle; a proxy/SSO account (no password) cannot log in via the password form.
- **Trusted-proxy SSO**: a reverse proxy (e.g. Traefik forward-auth to an IdP) asserts the authenticated username in the configured header (`STOKER_AUTH_HEADER`, default `X-Forwarded-User`). The header is honoured **only** when the immediate socket peer (`request.client.host` — the proxy) falls inside one of `STOKER_TRUSTED_PROXIES`; a direct client that sends the header is ignored (no spoofing). It is the real peer address, never any `X-Forwarded-For`. A proxy-asserted user is created on first sight (`source="proxy"`, role from `STOKER_PROXY_DEFAULT_ROLE`, default operator). Proxy-header resolution wins over the session cookie.
- **API tokens (CI/CD)**: admin-issued service credentials for non-interactive callers (`api_tokens` table). `POST /api/tokens` (admin only) returns a one-time secret `stk_<random>`; only its **sha256 hash** and a display prefix (`stk_ab12cd34`) are stored, never the plaintext. A caller presents it as `Authorization: Bearer stk_...`; `resolve_api_token` looks it up by the hash column, rejects a revoked (`DELETE /api/tokens/{id}` soft-revoke) or expired token, and throttles the `last_used_at` write. Each token carries its own role, so a CI token can launch runs as `operator` without holding admin. It resolves to a **transient principal** (`username="token:<name>"`, no `users` row) so the audit trail attributes runs and actions to the specific token. The `stk_` prefix domain-separates a token from the worker's per-run JWT (`eyJ…`), and `/api/agent` is exempt regardless, so the two never collide. Resolution order: trusted-proxy header, then API token, then session cookie.
- **Roles** (`viewer` < `operator` < `admin`), enforced by the `_auth_guard` middleware over guarded `/api/*`:
  - `/api/users` and `/api/users/*` require **admin** (also enforced per-route via `require_admin`).
  - any other **mutating** request (non GET/HEAD/OPTIONS) requires **operator** or **admin** — a `viewer` gets 403 and cannot create/delete targets, launch runs, or register repos.
  - **safe** methods (GET/HEAD/OPTIONS) need only an authenticated user of any role.
- **Bootstrap and first-run**: on startup `bootstrap_admin` creates a local admin from `STOKER_ADMIN_USER` + `STOKER_ADMIN_PASSWORD` when both are set and that username is absent (idempotent; never resets an existing password, never logs it). With no admin and no proxy trust, the instance is in first-run mode: the guard stands down (`auth_active` is false) so the first admin can be created via `POST /api/auth/setup` (only allowed while zero users exist; a Postgres advisory lock serialises concurrent setup). Once any user exists, or proxy trust is configured, auth engages.
- **Kill switch**: `STOKER_AUTH_DISABLED` skips the guard entirely with a loud warning (local dev only). Traefik basic-auth has been removed; auth is this subsystem.
- **User management** (`/api/users`, admin only): list / create / patch (role, password, active, email) / delete. Two integrity guards: you cannot delete or demote/deactivate the **last active admin**, and you cannot delete **yourself**. No hash is ever returned (`UserOut` has no hash field).
- **Exempt from the session guard**: `/api/agent/*` (per-run JWT) and `/api/hooks/*` (per-repo webhook HMAC) authenticate their own way, plus the unauthenticated auth entry points the login page needs before a session exists (`/api/auth/login`, `/logout`, `/status`, `/setup`). The SPA shell, hashed `/assets`, `/healthz` and the OpenAPI docs are public (the HTML is public; the API it calls is what is protected, so the UI redirects to login on a 401).
- **OpenAPI / Swagger**: the interactive docs are at `/docs` (Swagger UI) and `/redoc`, and the spec at `/openapi.json`. A custom `app.openapi()` declares the `bearerAuth` security scheme (the API token) as a global requirement, so Swagger UI shows an **Authorize** box (paste an `stk_` token) and the spec is usable for client codegen. The docs surface the API shape only, never a secret.

## Agent-facing API (`/api/agent`, `Authorization: Bearer <per-run JWT>`)

Matches `docs/WORKER-CONTRACT.md` and the worker's `control.py` on the wire. The
worker treats the JWT as opaque. `require_run_jwt` decodes the bearer (PyJWT
HS256, key domain-separated from the Fernet use), checks the `run_id` claim
equals the path run id and that it is unexpired, else 401. A request whose
`lease_id` is not the current holder of its slot gets `{"command":"superseded"}`
on heartbeat (200) and 409 on ready/final.

- `POST /api/agent/runs/{run_id}/claim` `{holder, hint_slot?, protocol_version}` -> the spec slice (`run_id`, `slot`, `total_workers`, `lease_id`, `engine`, `bundle{url,sha256}`, `share{<one key>}`, `duration_s`, `hec{url,index,sourcetype,gzip,ack}`, `overrides`, `telemetry{interval_s}`, `released`, `effective_t0`). Issues the lowest free lease (honouring `hint_slot`), records holder, state `claimed`, stamps the heartbeat clock and `effective_t0`. `share` carries exactly one key matching `rate_mode`. `overrides.host` etc. have `{slot}` substituted. A re-claim of a still-held lease by the same holder is idempotent.
- `POST /api/agent/runs/{run_id}/ready` `{slot, lease_id}` -> `{}`. Marks the lease ready; may trigger release evaluation. 409 when `lease_id` is not the slot holder.
- `POST /api/agent/runs/{run_id}/heartbeat` `{slot, lease_id, protocol_version, ...counters..., state}` -> a command: `continue` normally; `release` with `t0` once T0 is set; `retarget` with `share` when the stored slot share changed (scale/rescale); `drain` when draining/stopping or the protocol version is unsupported; `superseded` when this lease_id is no longer the slot holder. May carry a fresh `jwt` when the token is near expiry. A successful heartbeat renews the lease and appends the counters to `metric_samples`.
- `POST /api/agent/runs/{run_id}/final` `{slot, summary, log_tail}` -> `{}`. Stores `final_log_tail`, folds `summary` into `runs.totals_json`, marks the lease done; when all leases are done/lost the run reaches its terminal state.
- `GET /api/agent/bundles/{digest}.tgz` -> the tarball bytes (JWT-checked; the run must reference that bundle by `bundle_id` or `resolved_sha`). 404 unknown/missing, 401 bad token, 403 when the run does not reference the bundle.

## Operator API (`/api`, session/role-guarded)

All of `/api` (except `/api/hooks/github`) sits behind the auth middleware:
reads need an authenticated viewer, writes need operator+, `/api/users` and
`/api/tokens` need admin. Callers authenticate with a session cookie, a
trusted-proxy header, or an API token (`Authorization: Bearer stk_...`).

**Targets**
- `POST /api/targets` `{name, hec_url, token, default_index, env_tag, max_concurrent_gb_day, verify_tls?}` -> target (token Fernet-encrypted at rest, never echoed). 409 on a duplicate name.
- `GET /api/targets`, `GET /api/targets/{id}`.
- `POST /api/targets/{id}/test` probes `/services/collector/health` + an auth ping -> `{ok, health, auth, latency_ms, detail}` and updates `health_state`.
- `DELETE /api/targets/{id}` (409 when referenced by a spec).

**Repos (git-sync of sample packs)**
- `POST /api/repos` `{url, auth_kind(none|pat|deploy_key), secret?, default_ref?, trusted_code?}` -> repo with a one-time `webhook_secret` (credential Fernet-encrypted, never echoed; only its presence is reported on later GETs).
- `GET /api/repos`, `GET /api/repos/{id}`, `DELETE /api/repos/{id}` (409 when a pack it indexed is referenced by a spec; otherwise its packs are removed with it).
- `POST /api/repos/{id}/sync` clones/fetches and indexes packs -> sync counts (502 `sync_failed` with a secret-free reason on git/index failure).
- `POST /api/hooks/github` — GitHub push webhook, HMAC-verified against the matching repo's `webhook_secret` over the raw body (`X-Hub-Signature-256`); a `push` event resyncs, others are acknowledged and ignored. Uniform 401 on a bad/unrecognised signature (no secret-probing oracle). Not session-guarded (GitHub cannot present a session).

**Packs**
- `POST /api/packs` `{name, source_path, description?}` registers and lints a local pack directory; sets `verified`/`lint_status`/engines/sourcetypes/`est_bytes_per_event`.
- `GET /api/packs` (optional `?repo=<id>` / `?repo_id=<id>` filter), `GET /api/packs/{id}`.
- `GET /api/packs/{id}/preview` -> stanzas + first 10 sample lines per stanza + lint status.
- `GET /api/packs/{id}/preview_run?n=<N>` -> N rendered events (a dry-run render for review).

**Specs**
- `POST /api/specs` (JobSpec), `GET /api/specs`, `GET /api/specs/{id}`.
- `GET /api/specs/{id}/estimate` -> per-worker share, pct of ceiling, approx eps/gb, `ok` bool.
- `PUT /api/specs/{id}`, `DELETE /api/specs/{id}` (409 when the spec has runs).
- `POST /api/specs/{id}/run` `{overrides?}` -> `201 {run_id, state}` after the submit gates (in order): pack lint ok (`422 pack_lint_failed`); engine/pack consistency (`422 engine_pack_mismatch` — a rawreplay spec on a non-rawreplay pack); replay-single-worker (`409 replay_single_worker` when a replay run has workers>1); per-worker slice vs ceiling (`422 slice_exceeds_ceiling{suggested_workers, limiting_factor, detail}`); per-target concurrent-GB cap (`409 target_cap_exceeded{headroom_gb_day, detail}`); target health (`409 target_unhealthy` when red; unknown/amber pass). Then provision. A driver failure surfaces as `502 provision_failed`. (`started_by` records the resolving caller — a username, or `token:<name>` for an API token — falling back to `operator` only in the bootstrap / auth-disabled window; the same actor is stamped on the run's audit events for stop/scale/rescale.)

**Runs**
- `GET /api/runs`, `GET /api/runs/{id}` (state, snapshot, totals, lease roster, event log).
- `GET /api/runs/{id}/metrics?res=5s&window=15m` -> samples within the window (raw samples ordered by ts/slot; `res` is echoed for the UI).
- `GET /api/runs/{id}/logs?slot=&tail=` -> recent worker log lines from the driver (falls back to a lease's stored `final_log_tail` for a finished run whose workload is gone). `tail` clamped to [1, 5000].
- `GET /api/runs/{id}/events` -> the append-only audit trail.
- `POST /api/runs/{id}/stop {force?}` -> drain (or immediate destroy when `force`).
- `POST /api/runs/{id}/scale {workers}` -> re-apportion + push `retarget` (`409 replay_single_worker` for a replay run; `422` when workers<1).
- `POST /api/runs/{id}/rescale {rate_value}` -> re-apportion the same worker count + push `retarget` (`422` when rate_value<=0).

**Ops / auth**
- `GET /healthz` -> liveness with build/db info (public, no secrets).
- `GET /api/auth/status` (public), `POST /api/auth/login`, `POST /api/auth/logout`, `POST /api/auth/setup`, `GET /api/auth/me` (session required).
- `GET|POST /api/users`, `PATCH|DELETE /api/users/{id}` (admin only).

**API tokens** (admin only; see Auth)
- `POST /api/tokens` `{name, role, expires_in_days?}` -> `201 {id, name, role, token, prefix, created_at, expires_at}` — the secret `token` (`stk_...`) is returned **only** here. 409 on a duplicate name.
- `GET /api/tokens` -> metadata only (id, name, role, prefix, created_by, created_at, expires_at, last_used_at, revoked_at); never the secret or its hash.
- `DELETE /api/tokens/{id}` -> `204` soft-revoke (idempotent; 404 unknown). The row survives with `revoked_at` set for the audit trail.

## Metric roll-up + prune, and dogfood telemetry

`metrics_lifecycle.py`, driven by two slow background loops in the lifespan
alongside the fast supervisor.

- **Roll-up + prune** (`roll_up_and_prune`, ~hourly via `metric_maintenance_interval_s`): fine-grained `metric_samples` older than `METRIC_ROLLUP_AFTER_H` (48 h) are grouped by `(run_id, slot, epoch // METRIC_ROLLUP_BUCKET_S)` (60 s buckets) and each multi-row bucket is collapsed to one aggregate row (`last` of cumulative counters, `mean` of gauges, `sum` of interval deltas; the aggregate's `ts` is the bucket floor). Rows older than `METRIC_PRUNE_AFTER_D` (30 days) are hard-deleted. Work is chunked (`METRIC_DELETE_CHUNK`, 5000) and committed per batch so a huge prune never blocks the supervisor; the pass is idempotent.
- **Dogfood self-telemetry** (optional, enabled only when both `DOGFOOD_HEC_URL` and `DOGFOOD_HEC_TOKEN` are set): a `stoker:job` HEC event per run state transition, and a periodic `stoker:metrics` aggregate per active run (`dogfood_metrics_interval_s`, ~30 s). Every dogfood path is a best-effort no-op when disabled; HEC failures are swallowed and the token is never logged. `host` on the envelope is the public base URL's host.

## Config (env)

Parsed once into a frozen `Settings` (`config.py`); secret fields are `repr=False`.

- **Core**: `DATABASE_URL` (default `sqlite:///./stoker.db`; prod `postgresql+psycopg://...`), `STOKER_MASTER_KEY` (Fernet key; or `STOKER_MASTER_KEY_FILE` for a mounted secret; a dev key is generated with a loud warning if neither is set), `STOKER_JWT_TTL_S` (default 3600), `PUBLIC_BASE_URL` (what workers use to reach the control plane and bundles; defaults to `http://localhost:<PORT>`), `WORKER_IMAGE` (default `ghcr.io/livehybrid/stoker-worker:latest`; prod pins `@sha256:<digest>`), `BUNDLE_DIR` (default `/data/bundles`), `REPO_CLONE_DIR` (default `/data/repos`), `PORT` (default 8080).
- **Swarm**: `PORTAINER_HOST`, `PORTAINER_TOKEN` (tier-0, secret), `PORTAINER_ENDPOINT` (default 6).
- **Auth**: `STOKER_ADMIN_USER` + `STOKER_ADMIN_PASSWORD` (bootstrap admin; password secret), `STOKER_SESSION_TTL` (default 43200 = 12 h), `STOKER_TRUSTED_PROXIES` (comma-separated CIDR/IP; empty = no proxy trusted; a malformed entry is a hard boot error), `STOKER_AUTH_HEADER` (default `X-Forwarded-User`), `STOKER_PROXY_DEFAULT_ROLE` (default operator; validated against viewer/operator/admin at boot), `STOKER_AUTH_DISABLED` (kill switch).
- **Metric maintenance**: `METRIC_ROLLUP_AFTER_H` (48), `METRIC_PRUNE_AFTER_D` (30), `METRIC_ROLLUP_BUCKET_S` (60), `METRIC_MAINTENANCE_INTERVAL_S` (3600), `METRIC_DELETE_CHUNK` (5000).
- **Dogfood**: `DOGFOOD_HEC_URL`, `DOGFOOD_HEC_TOKEN` (secret; both required to enable), `DOGFOOD_METRICS_INTERVAL_S` (30), `DOGFOOD_GZIP` (true).
- **rawreplay**: `RAWREPLAY_MAX_DATASET_BYTES` (default 512 MiB; cap on a `dataset_url` fetch), `RAWREPLAY_FETCH_TIMEOUT_S` (default 120).

## Crypto

- **Fernet** (`crypto.encrypt`/`decrypt`) for target HEC tokens and repo credentials at rest; the master key is a urlsafe-base64 32-byte Fernet key.
- **Per-run JWT** (PyJWT, HS256): claims `run_id`, `kid` (key id stored on the run), `iss`, `iat`, `exp` (now + `jwt_ttl_s`). The signing key is derived from the master key, domain-separated so it is not the raw Fernet key. The worker treats the token as opaque.

## Testing

- Unit: apportionment (sums exactly), ceilings, lease state machine, JWT round-trip + tamper reject, bundle build/dedup, rawreplay lint/fetch, gitsync + gitsync security guards, auth (password hashing, session, proxy trust, role gating, last-admin/self-delete guards), boot reconcile, release gate, heartbeat, driver conformance (FakeDriver; K8sDriver against a mock), target-token never echoed.
- Integration/e2e (`tests/test_e2e.py`): start the app with a FakeDriver, create target+pack+spec, `POST /run`, then drive the **real** `stoker_agent` through claim -> ready -> release at T0 -> heartbeats -> final, asserting the run reaches `completed`, leases end `done`, and `metric_samples` accrued — reusing the vendored engine and a tiny pack to prove the whole path without a swarm.
- Test DB is SQLite (dialect-agnostic models); prod is Postgres. CI runs the server suite alongside the worker suite.

## Deploy

`infra/stacks/stoker/` holds the deploy stack (the stoker app + `postgres:16` +
`tiredofit/db-backup`, on the traefik + internal networks, `stop_grace_period:
45s`) and `deploy.py` (Portainer, env from `/opt/aios/.env`). `server/Dockerfile`
is python:3.12-slim, non-root, uvicorn, serving `ui/dist` when present. Live at
https://stoker.cloud.livehybrid.com and LAN http://192.168.0.112:8091 (Portainer
swarm, stack 107). Images `ghcr.io/livehybrid/stoker` +
`ghcr.io/livehybrid/stoker-worker` (multi-arch, cosign-signed). Postgres 16 backing store.

## Conventions

British English in prose; no secret material in logs (JWT / token / master key /
bcrypt hash / webhook secret never logged); the control plane never generates
load; the DB is the source of truth and the driver is queried, never trusted as a
store; push not pull (commands ride heartbeat responses). Python 3.12, FastAPI,
SQLAlchemy 2.0, pydantic v2, httpx, PyJWT, cryptography, passlib/bcrypt,
itsdangerous, kubernetes.
