// Typed extraction of the spec/run API's structured rejection bodies.
//
// POST /specs/{id}/run and POST /specs (and the wizard's Save & Run) return
// FastAPI errors whose `detail` is either a plain string or a structured object.
// ApiError already unwraps `detail`; here we recognise the specific shapes the
// contract defines and turn them into a clear operator-facing message.
//
// Structured bodies (from server/routes/api.py):
//   422 {"error":"slice_exceeds_ceiling","suggested_workers":N,"limiting_factor":..,"detail":..}
//   409 {"error":"replay_single_worker","detail":..,"workers":N}
//   409 {"error":"target_unhealthy","health_state":..,"detail":..}
//   409 {"error":"target_cap_exceeded","headroom_gb_day":N,"detail":..}
//   422 {"error":"pack_lint_failed","errors":[..]}
//   502 {"error":"provision_failed","detail":..}
// Plus plain-string details for the simpler 404/422 validation errors.

import { ApiError } from "../../lib/api";

export type RunErrorKind =
  | "slice_exceeds_ceiling"
  | "replay_single_worker"
  | "target_unhealthy"
  | "target_cap_exceeded"
  | "pack_lint_failed"
  | "provision_failed"
  | "generic";

export interface ParsedApiError {
  kind: RunErrorKind;
  message: string;
  /** slice_exceeds_ceiling / replay_single_worker: the fleet size to use. */
  suggestedWorkers?: number;
  /** target_cap_exceeded: remaining GB/day headroom on the target. */
  headroomGbDay?: number;
  /** pack_lint_failed: the lint errors to surface. */
  lintErrors?: string[];
  status?: number;
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
}

function num(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function str(v: unknown): string | undefined {
  return typeof v === "string" ? v : undefined;
}

/**
 * Turn any error thrown by the api client into a ParsedApiError. Non-ApiError
 * values (network faults, bugs) fall through to a generic message.
 */
export function parseApiError(err: unknown): ParsedApiError {
  if (!(err instanceof ApiError)) {
    const message = err instanceof Error ? err.message : "Something went wrong.";
    return { kind: "generic", message };
  }
  const status = err.status;
  const detail = asRecord(err.detail);
  const code = detail ? str(detail.error) : undefined;
  const inner = detail ? str(detail.detail) : undefined;

  switch (code) {
    case "slice_exceeds_ceiling": {
      const suggested = detail ? num(detail.suggested_workers) : undefined;
      const factor = detail ? str(detail.limiting_factor) : undefined;
      const base =
        inner ??
        `The per-worker rate exceeds the engine ceiling${factor ? ` (${factor})` : ""}.`;
      const message = suggested
        ? `${base} Use at least ${suggested} workers.`
        : base;
      return { kind: "slice_exceeds_ceiling", message, suggestedWorkers: suggested, status };
    }
    case "replay_single_worker": {
      const message =
        inner ??
        "This pack contains a replay stanza; replay runs must use exactly 1 worker.";
      return { kind: "replay_single_worker", message, suggestedWorkers: 1, status };
    }
    case "target_unhealthy": {
      const health = detail ? str(detail.health_state) : undefined;
      const message =
        inner ??
        `The target last probed unhealthy${health ? ` (${health})` : ""}. Test the target before launching.`;
      return { kind: "target_unhealthy", message, status };
    }
    case "target_cap_exceeded": {
      const headroom = detail ? num(detail.headroom_gb_day) : undefined;
      const message =
        inner ??
        "The target's concurrent GB/day cap would be exceeded by this run.";
      return { kind: "target_cap_exceeded", message, headroomGbDay: headroom, status };
    }
    case "pack_lint_failed": {
      const errsRaw = detail ? detail.errors : undefined;
      const lintErrors = Array.isArray(errsRaw) ? errsRaw.map(String) : undefined;
      const message =
        "The pack no longer lints clean; fix the pack before launching.";
      return { kind: "pack_lint_failed", message, lintErrors, status };
    }
    case "provision_failed": {
      const message = inner
        ? `Provisioning failed: ${inner}`
        : "Provisioning failed: the fleet could not be materialised.";
      return { kind: "provision_failed", message, status };
    }
    default:
      // Plain-string detail (most 404/422 validation errors) or an unrecognised
      // structured body: ApiError.message already carries the best available text.
      return { kind: "generic", message: err.message, status };
  }
}
