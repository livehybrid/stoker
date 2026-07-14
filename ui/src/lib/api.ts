// Typed fetch client for the Stoker operator API.
//
// Same-origin: the control plane serves this UI and exposes the operator API
// under `/api`. Every function here maps to exactly one endpoint in
// server/routes/api.py; request/response shapes come from src/lib/types.ts,
// which mirrors server/schemas.py.
//
// Error handling is centralised: any non-2xx throws an `ApiError` carrying the
// status and the API's error body (FastAPI's `{"detail": ...}`, where `detail`
// may be a string or a structured object like `slice_exceeds_ceiling`). No
// secret values are ever sent in query strings or logged.

import type {
  AuthStatus,
  BackfillEstimate,
  BackfillEstimateRequest,
  LoginRequest,
  MetricPackCreate,
  MetricPackDetail,
  MetricPreviewRequest,
  MetricPreviewResponse,
  MetricsOut,
  PackOut,
  PackPreview,
  PackPreviewRun,
  RepoCreate,
  RepoCreated,
  RepoOut,
  RepoSyncResult,
  RescaleRequest,
  RunCreated,
  RunDetail,
  RunEventOut,
  RunLaunch,
  RunLogsOut,
  RunOut,
  ScaleRequest,
  SetupRequest,
  SpecCreate,
  SpecEstimate,
  SpecOut,
  SpecUpdate,
  StopRequest,
  TargetCreate,
  TargetUpdate,
  TargetOut,
  TargetTestResult,
  UserCreate,
  UserOut,
  UserUpdate,
} from "./types";

export const API_BASE = "/api";

// The login route; a central 401 handler sends the browser here when a session
// has expired or is missing. Kept as a constant so the router and the fetch
// wrapper agree on the path.
export const LOGIN_PATH = "/login";

// Paths (relative to API_BASE) that must NEVER trigger the 401 -> /login
// redirect: the login/setup POSTs report bad-credential 401s to their own forms,
// and the public status probe is expected to be callable while signed out. A
// redirect here would either loop (already on /login) or swallow the form error.
const NO_REDIRECT_ON_401 = ["/auth/login", "/auth/setup", "/auth/status"];

// Set by the app at startup to perform the actual navigation on a 401. Kept
// injectable so this module has no hard dependency on the router instance (and
// so tests can stub it). Falls back to a location assignment.
type RedirectFn = () => void;
let onUnauthorized: RedirectFn | null = null;

/** Register the handler invoked once when an API call returns 401. */
export function setUnauthorizedHandler(fn: RedirectFn | null): void {
  onUnauthorized = fn;
}

function redirectToLogin(): void {
  if (onUnauthorized) {
    onUnauthorized();
    return;
  }
  // Fallback: hard navigation (avoids a redirect loop when already on /login).
  if (
    typeof window !== "undefined" &&
    window.location.pathname !== LOGIN_PATH
  ) {
    window.location.assign(LOGIN_PATH);
  }
}

/**
 * Thrown on any non-2xx response. `detail` is the API's error payload: a plain
 * string for simple errors, or a structured object for the typed rejections the
 * contract defines (e.g. `{error: "slice_exceeds_ceiling", suggested_workers}`).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

function buildUrl(path: string, query?: Query): string {
  const url = `${API_BASE}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    params.append(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

function messageFromDetail(status: number, detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    if (typeof d.detail === "string") return d.detail;
    if (typeof d.error === "string") return d.error;
    try {
      return JSON.stringify(detail);
    } catch {
      /* fall through */
    }
  }
  return `request failed with status ${status}`;
}

async function request<T>(
  method: string,
  path: string,
  opts: { query?: Query; body?: unknown } = {},
): Promise<T> {
  const init: RequestInit = {
    method,
    headers: { Accept: "application/json" },
  };
  if (opts.body !== undefined) {
    (init.headers as Record<string, string>)["Content-Type"] =
      "application/json";
    init.body = JSON.stringify(opts.body);
  }

  const res = await fetch(buildUrl(path, opts.query), init);

  // 204 No Content (deletes) — nothing to parse.
  if (res.status === 204) {
    return undefined as T;
  }

  const text = await res.text();
  let payload: unknown = undefined;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text; // non-JSON body (unexpected); keep raw
    }
  }

  if (!res.ok) {
    // FastAPI puts the error under `detail`; unwrap it when present.
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? (payload as { detail: unknown }).detail
        : payload;
    // Central session handling: a 401 on any endpoint other than the auth
    // endpoints themselves means the session is gone -> go to the login page.
    // The error is still thrown so a caller mid-flight can react/cleanup.
    if (res.status === 401 && !NO_REDIRECT_ON_401.includes(path)) {
      redirectToLogin();
    }
    throw new ApiError(res.status, detail, messageFromDetail(res.status, detail));
  }

  return payload as T;
}

// --------------------------------------------------------------------------- //
// Targets
// --------------------------------------------------------------------------- //

export const targets = {
  list: () => request<TargetOut[]>("GET", "/targets"),
  get: (id: number) => request<TargetOut>("GET", `/targets/${id}`),
  create: (body: TargetCreate) =>
    request<TargetOut>("POST", "/targets", { body }),
  update: (id: number, body: TargetUpdate) =>
    request<TargetOut>("PATCH", `/targets/${id}`, { body }),
  delete: (id: number) => request<void>("DELETE", `/targets/${id}`),
  test: (id: number) =>
    request<TargetTestResult>("POST", `/targets/${id}/test`),
};

// --------------------------------------------------------------------------- //
// Repos
// --------------------------------------------------------------------------- //

export const repos = {
  list: () => request<RepoOut[]>("GET", "/repos"),
  get: (id: number) => request<RepoOut>("GET", `/repos/${id}`),
  create: (body: RepoCreate) =>
    request<RepoCreated>("POST", "/repos", { body }),
  delete: (id: number) => request<void>("DELETE", `/repos/${id}`),
  sync: (id: number) => request<RepoSyncResult>("POST", `/repos/${id}/sync`),
};

// --------------------------------------------------------------------------- //
// Packs
// --------------------------------------------------------------------------- //

export const packs = {
  // `repo` filters to packs indexed from that repo id (alias: repo_id).
  list: (repo?: number) =>
    request<PackOut[]>("GET", "/packs", { query: { repo } }),
  get: (id: number) => request<PackOut>("GET", `/packs/${id}`),
  preview: (id: number) =>
    request<PackPreview>("GET", `/packs/${id}/preview`),
  // Render a few sample events in-process (no fleet, no HEC target). `n` is
  // clamped server-side to a sane maximum.
  previewRun: (id: number, n = 10) =>
    request<PackPreviewRun>("GET", `/packs/${id}/preview_run`, {
      query: { n },
    }),
};

// --------------------------------------------------------------------------- //
// Metric packs (UI-authored metricgen config -> engine: metrics)
// --------------------------------------------------------------------------- //

export const metricPacks = {
  create: (body: MetricPackCreate) =>
    request<PackOut>("POST", "/metric-packs", { body }),
  update: (id: number, body: MetricPackCreate) =>
    request<PackOut>("PUT", `/metric-packs/${id}`, { body }),
  get: (id: number) =>
    request<MetricPackDetail>("GET", `/metric-packs/${id}`),
  // Compute one metric's 24 h curve from a (possibly in-progress) config.
  preview: (body: MetricPreviewRequest) =>
    request<MetricPreviewResponse>("POST", "/metric-packs/preview", { body }),
};

// --------------------------------------------------------------------------- //
// Specs
// --------------------------------------------------------------------------- //

export const specs = {
  list: () => request<SpecOut[]>("GET", "/specs"),
  get: (id: number) => request<SpecOut>("GET", `/specs/${id}`),
  create: (body: SpecCreate) => request<SpecOut>("POST", "/specs", { body }),
  update: (id: number, body: SpecUpdate) =>
    request<SpecOut>("PUT", `/specs/${id}`, { body }),
  delete: (id: number) => request<void>("DELETE", `/specs/${id}`),
  estimate: (id: number) =>
    request<SpecEstimate>("GET", `/specs/${id}/estimate`),
  // POST /specs/{id}/run — validate + provision the spec into a run.
  run: (id: number, body: RunLaunch = {}) =>
    request<RunCreated>("POST", `/specs/${id}/run`, { body }),
  backfillEstimate: (id: number, body: BackfillEstimateRequest) =>
    request<BackfillEstimate>("POST", `/specs/${id}/backfill_estimate`, { body }),
};

// --------------------------------------------------------------------------- //
// Runs
// --------------------------------------------------------------------------- //

export const runs = {
  list: () => request<RunOut[]>("GET", "/runs"),
  get: (id: number) => request<RunDetail>("GET", `/runs/${id}`),
  metrics: (id: number, res = "5s", window = "15m") =>
    request<MetricsOut>("GET", `/runs/${id}/metrics`, {
      query: { res, window },
    }),
  logs: (id: number, opts: { slot?: number; tail?: number } = {}) =>
    request<RunLogsOut>("GET", `/runs/${id}/logs`, {
      query: { slot: opts.slot, tail: opts.tail },
    }),
  events: (id: number) =>
    request<RunEventOut[]>("GET", `/runs/${id}/events`),
  stop: (id: number, body: StopRequest = {}) =>
    request<RunOut>("POST", `/runs/${id}/stop`, { body }),
  scale: (id: number, body: ScaleRequest) =>
    request<RunOut>("POST", `/runs/${id}/scale`, { body }),
  rescale: (id: number, body: RescaleRequest) =>
    request<RunOut>("POST", `/runs/${id}/rescale`, { body }),
};

// --------------------------------------------------------------------------- //
// Auth (session lifecycle + first-access setup)
// --------------------------------------------------------------------------- //

export const auth = {
  // Public: safe to call while signed out. Reports whether a session/SSO is
  // active, whether first-access setup is needed, and whether SSO is configured.
  status: () => request<AuthStatus>("GET", "/auth/status"),
  // The signed-in user (401 when there is no session -> central redirect).
  me: () => request<UserOut>("GET", "/auth/me"),
  login: (body: LoginRequest) =>
    request<UserOut>("POST", "/auth/login", { body }),
  logout: () => request<void>("POST", "/auth/logout"),
  // Create the very first admin (only honoured while zero users exist).
  setup: (body: SetupRequest) =>
    request<UserOut>("POST", "/auth/setup", { body }),
};

// --------------------------------------------------------------------------- //
// Users (admin-only management)
// --------------------------------------------------------------------------- //

export const users = {
  list: () => request<UserOut[]>("GET", "/users"),
  get: (id: number) => request<UserOut>("GET", `/users/${id}`),
  create: (body: UserCreate) => request<UserOut>("POST", "/users", { body }),
  update: (id: number, body: UserUpdate) =>
    request<UserOut>("PATCH", `/users/${id}`, { body }),
  delete: (id: number) => request<void>("DELETE", `/users/${id}`),
};

// Grouped export for `import { api } from "@/lib/api"` ergonomics.
export const api = {
  targets,
  repos,
  packs,
  metricPacks,
  specs,
  runs,
  auth,
  users,
};
export default api;
