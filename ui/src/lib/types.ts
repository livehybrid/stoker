// TypeScript mirrors of server/schemas.py (Pydantic v2 response/request models).
// Field names match the API on the wire exactly. Secret fields are write-only in
// the request models and never present in the response models by construction.
//
// Datetimes arrive as ISO 8601 strings over JSON; they are typed `string` here.

// --------------------------------------------------------------------------- //
// Targets
// --------------------------------------------------------------------------- //

export interface TargetCreate {
  name: string;
  hec_url: string;
  token: string; // write-only; never echoed back
  default_index?: string | null;
  env_tag?: string; // default "lab"
  max_concurrent_gb_day?: number | null;
  verify_tls?: boolean; // default true
}

// Partial update (PATCH). Only the fields present are changed; omit `token`
// (or send "") to keep the stored HEC token, send a new value to rotate it.
export interface TargetUpdate {
  name?: string;
  hec_url?: string;
  token?: string;
  default_index?: string | null;
  env_tag?: string;
  max_concurrent_gb_day?: number | null;
  verify_tls?: boolean;
}

export interface TargetOut {
  id: number;
  name: string;
  hec_url: string;
  default_index?: string | null;
  verify_tls: boolean;
  env_tag: string;
  max_concurrent_gb_day?: number | null;
  health_state: string; // unknown | green | amber | red
  health_detail?: string | null;
  last_health_at?: string | null;
  lifetime_gb: number;
  created_at: string;
}

export interface TargetTestResult {
  ok: boolean;
  health?: string | null; // up | down
  auth?: string | null; // ok | denied | unknown | error
  latency_ms?: number | null;
  detail?: string | null;
}

// --------------------------------------------------------------------------- //
// Repos
// --------------------------------------------------------------------------- //

export type RepoAuthKind = "none" | "pat" | "deploy_key";

export interface RepoCreate {
  url: string;
  auth_kind?: RepoAuthKind; // default "none"
  secret?: string | null; // write-only credential (PAT / deploy key)
  default_ref?: string; // default "main"
  trusted_code?: boolean; // default false
}

export interface RepoOut {
  id: number;
  url: string;
  auth_kind: string;
  has_secret: boolean;
  default_ref: string;
  head_sha?: string | null;
  last_synced_at?: string | null;
  sync_error?: string | null;
  trusted_code: boolean;
  created_at: string;
}

export interface RepoCreated extends RepoOut {
  // Returned once on create so the operator can configure the GitHub webhook.
  webhook_secret?: string | null;
}

export interface RepoSyncResult {
  head_sha?: string | null;
  packs_indexed: number;
  lint_failures: number;
}

// --------------------------------------------------------------------------- //
// Packs
// --------------------------------------------------------------------------- //

export interface PackCreate {
  name: string;
  source_path: string;
  description?: string | null;
}

export interface PackOut {
  id: number;
  name: string;
  source_path: string;
  description?: string | null;
  tags_json?: string[] | null;
  engines_json?: string[] | null;
  sourcetypes_json?: string[] | null;
  stanza_count?: number | null;
  est_bytes_per_event?: number | null;
  declared_per_day_gb?: number | null;
  verified: boolean;
  lint_status: string; // ok | error
  lint_errors_json?: string[] | null;
  repo_id?: number | null;
  indexed_sha?: string | null;
  created_at: string;
}

export interface PackPreview {
  stanzas: string[];
  sample_lines: Record<string, string[]>;
  lint_status: string;
  lint_errors: string[];
}

export interface PackPreviewRun {
  events: string[];
}

// --------------------------------------------------------------------------- //
// Metric packs (UI-authored `metricgen` config -> engine: metrics)
// --------------------------------------------------------------------------- //

export type MetricKind = "gauge" | "count" | "counter";

export type PatternType =
  | "constant"
  | "sine"
  | "business_hours"
  | "business_double_hump"
  | "ramp"
  | "spike"
  | "random_walk";

export interface MetricDimension {
  key: string;
  values: string[];
}

export interface MetricPattern {
  type: PatternType;
  [param: string]: unknown;
}

export interface MetricDef {
  name: string;
  kind: MetricKind;
  unit?: string;
  min: number;
  p95: number;
  max: number;
  noise?: number;
  pattern: MetricPattern;
  // scale[dimensionKey][dimensionValue] = multiplier applied to this metric's
  // min/p95/max for series carrying that dimension value.
  scale?: Record<string, Record<string, number>>;
}

export interface MetricgenConfig {
  resolution_s: number;
  tz_offset_hours?: number;
  seed?: number;
  sourcetype?: string;
  dimensions: MetricDimension[];
  metrics: MetricDef[];
}

export interface MetricPackCreate {
  name: string;
  description?: string | null;
  config: MetricgenConfig;
}

export interface MetricPackDetail {
  id: number;
  name: string;
  description?: string | null;
  engines_json?: string[] | null;
  sourcetypes_json?: string[] | null;
  verified: boolean;
  lint_status: string;
  lint_errors_json?: string[] | null;
  created_at: string;
  config: MetricgenConfig;
  series_count: number;
}

export interface MetricPreviewRequest {
  config: MetricgenConfig;
  metric?: string | null;
  cell?: Record<string, string> | null;
  points?: number;
}

export interface MetricPreviewPoint {
  hour: number;
  activity: number;
  center: number;
  value: number;
}

export interface MetricPreviewResponse {
  metric: string;
  unit?: string | null;
  kind: string;
  guides: { min: number; p95: number; max: number };
  points: MetricPreviewPoint[];
  series_count: number;
}

// --------------------------------------------------------------------------- //
// Specs
// --------------------------------------------------------------------------- //

export type RateMode = "eps" | "per_day_gb" | "count_interval";

export interface SpecCreate {
  name: string;
  pack_id: number;
  target_id: number;
  ref?: string; // default "local"
  engine?: string; // default "eventgen"
  overrides?: Record<string, string> | null;
  rate_mode?: RateMode; // default "eps"
  rate_value?: number | null;
  interval_s?: number | null;
  workers?: number; // default 1
  duration_s?: number | null;
  fleet?: string; // default "swarm-local"
  strict_release?: boolean; // default false
  driver_opts?: Record<string, unknown> | null;
}

// Partial update; unset fields are left unchanged (send only what changes).
export type SpecUpdate = Partial<{
  name: string;
  pack_id: number;
  target_id: number;
  ref: string;
  engine: string;
  overrides: Record<string, string> | null;
  rate_mode: RateMode;
  rate_value: number | null;
  interval_s: number | null;
  workers: number;
  duration_s: number | null;
  fleet: string;
  strict_release: boolean;
  driver_opts: Record<string, unknown> | null;
}>;

export interface SpecOut {
  id: number;
  name: string;
  pack_id: number;
  target_id: number;
  ref: string;
  engine: string;
  overrides_json?: Record<string, unknown> | null;
  rate_mode: string;
  rate_value?: number | null;
  interval_s?: number | null;
  workers: number;
  duration_s?: number | null;
  fleet: string;
  strict_release: boolean;
  driver_opts_json?: Record<string, unknown> | null;
  created_at: string;
}

export interface SpecEstimate {
  workers: number;
  rate_mode: string;
  per_worker_share?: number | null;
  per_worker_eps?: number | null;
  per_worker_gb_day?: number | null;
  ceiling_pct?: number | null;
  ceiling_limit?: number | null;
  limiting_factor?: string | null;
  ok: boolean;
  suggested_workers?: number | null;
  detail?: string | null;
}

// --------------------------------------------------------------------------- //
// Runs
// --------------------------------------------------------------------------- //

export interface RunLaunch {
  overrides?: Record<string, string> | null;
}

export interface RunCreated {
  run_id: number;
  state: string;
}

export interface LeaseOut {
  slot: number;
  lease_id: string;
  share_json?: Record<string, unknown> | null;
  holder?: string | null;
  node?: string | null;
  state: string;
  last_heartbeat_at?: string | null;
  effective_t0?: string | null;
  restarts: number;
}

export interface RunEventOut {
  ts: string;
  actor: string;
  kind: string;
  detail_json?: unknown;
}

export interface RunOut {
  id: number;
  spec_id: number;
  state: string;
  degraded: boolean;
  resolved_sha?: string | null;
  bundle_id?: number | null;
  started_by?: string | null;
  created_at: string;
  t0?: string | null;
  ended_at?: string | null;
  end_reason?: string | null;
  totals_json?: unknown;
}

export interface RunDetail extends RunOut {
  spec_snapshot_json?: unknown;
  leases: LeaseOut[];
  events: RunEventOut[];
}

export interface MetricSampleOut {
  slot: number;
  ts: string;
  events_total?: number | null;
  bytes_total?: number | null;
  eps?: number | null;
  bps?: number | null;
  hec_2xx?: number | null;
  hec_4xx?: number | null;
  hec_5xx?: number | null;
  hec_timeouts?: number | null;
  retries?: number | null;
  queue_depth?: number | null;
  lag_s?: number | null;
  rss_mb?: number | null;
  cpu_pct?: number | null;
}

export interface MetricsOut {
  run_id: number;
  resolution: string;
  window: string;
  samples: MetricSampleOut[];
}

export interface RunLogsOut {
  run_id: number;
  slot?: number | null;
  tail: number;
  lines: string[];
}

export interface StopRequest {
  force?: boolean; // default false
}

export interface ScaleRequest {
  workers: number;
}

export interface RescaleRequest {
  rate_value: number;
}

// --------------------------------------------------------------------------- //
// Auth (local users + trusted-proxy SSO)
// --------------------------------------------------------------------------- //

// Authorisation roles, most to least privileged. `admin` gates user management.
export type Role = "viewer" | "operator" | "admin";
export const ROLES: Role[] = ["viewer", "operator", "admin"];

// How an identity was established: a local password account, or asserted by a
// trusted reverse proxy (SSO).
export type UserSource = "local" | "proxy";

export interface UserOut {
  id: number;
  username: string;
  email?: string | null;
  role: string; // one of Role; kept wide so an unknown server value still renders
  source: string; // local | proxy
  active: boolean;
  created_at: string;
  last_login_at?: string | null;
}

export interface UserCreate {
  username: string;
  password: string; // write-only; never echoed back
  role?: Role; // default "operator"
  email?: string | null;
}

// Partial update; unset fields are left unchanged. `password` rehashes the
// account's credential when present (write-only).
export type UserUpdate = Partial<{
  role: Role;
  password: string;
  active: boolean;
  email: string | null;
}>;

export interface LoginRequest {
  username: string;
  password: string; // write-only
}

export interface SetupRequest {
  username: string;
  password: string; // write-only; creates the very first admin
}

export interface AuthStatus {
  authenticated: boolean;
  setup_needed: boolean;
  sso_enabled: boolean;
  user?: UserOut | null;
}
