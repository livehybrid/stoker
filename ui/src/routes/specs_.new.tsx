import { useEffect, useMemo, useRef, useState } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import type {
  PackOut,
  RateMode,
  SpecCreate,
  SpecOut,
  SpecUpdate,
  TargetOut,
} from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
import { Field, Select, TextInput } from "../components/Field";
import { Badge } from "../components/Badge";
import { ErrorState, LoadingState } from "../components/States";
import { useToast } from "../components/Toast";

import { PackPicker } from "../features/specs/PackPicker";
import { TargetPicker } from "../features/specs/TargetPicker";
import { EstimatePanel } from "../features/specs/EstimatePanel";
import { localEstimate } from "../features/specs/estimate";
import { parseApiError } from "../features/specs/errors";
import { packLooksReplay } from "../features/specs/replay";
import { packIsMetrics } from "../features/metrics/config";
import { RATE_MODE_LABEL } from "../features/specs/format";

// Search params: ?edit=<id> reopens a saved spec for editing; ?clone=<id>
// prefills a new spec from an existing one; ?pack=<id> pre-selects a pack (the
// "New job from pack" deep-link from the Packs page). Absent = a fresh spec.
interface WizardSearch {
  edit?: number;
  clone?: number;
  pack?: number;
}

const FLEETS = ["swarm-local", "k3s-local", "eks"];
const ENGINES = ["eventgen"];
const RATE_MODES: RateMode[] = ["eps", "per_day_gb", "count_interval"];

// Overrides the wizard exposes (JobSpec index/sourcetype/source/host). Empty
// values are dropped so an unset override is omitted from the request.
const OVERRIDE_KEYS = ["index", "sourcetype", "source", "host"] as const;
type OverrideKey = (typeof OVERRIDE_KEYS)[number];

interface FormState {
  name: string;
  pack_id: number | null;
  target_id: number | null;
  engine: string;
  rate_mode: RateMode;
  rate_value: string; // kept as string for the input; parsed on submit
  interval_s: string;
  workers: string;
  duration_s: string;
  fleet: string;
  strict_release: boolean;
  overrides: Record<OverrideKey, string>;
}

function emptyForm(): FormState {
  return {
    name: "",
    pack_id: null,
    target_id: null,
    engine: "eventgen",
    rate_mode: "eps",
    rate_value: "1000",
    interval_s: "",
    workers: "1",
    duration_s: "",
    fleet: "swarm-local",
    strict_release: false,
    overrides: { index: "", sourcetype: "", source: "", host: "" },
  };
}

function formFromSpec(spec: SpecOut): FormState {
  const ov = (spec.overrides_json ?? {}) as Record<string, unknown>;
  return {
    name: spec.name,
    pack_id: spec.pack_id,
    target_id: spec.target_id,
    engine: spec.engine,
    rate_mode: (spec.rate_mode as RateMode) ?? "eps",
    rate_value: spec.rate_value != null ? String(spec.rate_value) : "",
    interval_s: spec.interval_s != null ? String(spec.interval_s) : "",
    workers: String(spec.workers ?? 1),
    duration_s: spec.duration_s != null ? String(spec.duration_s) : "",
    fleet: spec.fleet ?? "swarm-local",
    strict_release: !!spec.strict_release,
    overrides: {
      index: str(ov.index),
      sourcetype: str(ov.sourcetype),
      source: str(ov.source),
      host: str(ov.host),
    },
  };
}

function str(v: unknown): string {
  return typeof v === "string" ? v : v != null ? String(v) : "";
}

function numOrNull(v: string): number | null {
  const t = v.trim();
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

function collectOverrides(
  overrides: Record<OverrideKey, string>,
): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const k of OVERRIDE_KEYS) {
    const v = overrides[k].trim();
    if (v) out[k] = v;
  }
  return Object.keys(out).length ? out : null;
}

function JobWizard() {
  const navigate = useNavigate();
  const toast = useToast();
  const { edit, clone, pack } = Route.useSearch();
  const editing = typeof edit === "number";
  const loadId = editing ? edit : typeof clone === "number" ? clone : null;

  const packsQ = useQuery({
    queryKey: ["wizard-packs"],
    queryFn: () => api.packs.list(),
  });
  const targetsQ = useQuery({
    queryKey: ["wizard-targets"],
    queryFn: () => api.targets.list(),
  });
  const specQ = useQuery({
    queryKey: ["spec", loadId],
    queryFn: () => api.specs.get(loadId as number),
    enabled: loadId != null,
  });

  const [form, setForm] = useState<FormState>(emptyForm);
  // Prefill from the loaded spec once (edit/clone). A clone keeps every field
  // but is saved as a new spec; the name gets a "(copy)" suffix.
  const prefilled = useRef(false);
  useEffect(() => {
    if (loadId == null || prefilled.current || !specQ.data) return;
    const next = formFromSpec(specQ.data);
    if (!editing) next.name = `${next.name} (copy)`;
    setForm(next);
    prefilled.current = true;
  }, [loadId, editing, specQ.data]);

  // Fresh-spec deep-link: ?pack=<id> from the Packs page pre-selects that pack
  // (once, only when not editing/cloning and no pack chosen yet).
  const packPrefilled = useRef(false);
  useEffect(() => {
    if (loadId != null || packPrefilled.current || pack == null) return;
    if (!packsQ.data?.some((p) => p.id === pack)) return;
    setForm((f) => (f.pack_id == null ? { ...f, pack_id: pack } : f));
    packPrefilled.current = true;
  }, [loadId, pack, packsQ.data]);

  const patch = (p: Partial<FormState>) => setForm((f) => ({ ...f, ...p }));

  const selectedPack: PackOut | undefined = useMemo(
    () => packsQ.data?.find((p) => p.id === form.pack_id),
    [packsQ.data, form.pack_id],
  );
  const selectedTarget: TargetOut | undefined = useMemo(
    () => targetsQ.data?.find((t) => t.id === form.target_id),
    [targetsQ.data, form.target_id],
  );

  const isReplay = packLooksReplay(selectedPack);
  const isMetrics = packIsMetrics(selectedPack);
  const bytesPerEvent = selectedPack?.est_bytes_per_event ?? null;

  // Metrics packs need the metrics engine + count_interval pacing (engine-paced
  // on a fixed grid); load the pack's config to align interval/count to it.
  const metricDetailQ = useQuery({
    queryKey: ["wizard-metric-pack", form.pack_id],
    queryFn: () => api.metricPacks.get(form.pack_id as number),
    enabled: isMetrics && form.pack_id != null,
  });
  const workersNum = Math.max(1, Math.floor(numOrNull(form.workers) ?? 1));
  const rateValueNum = numOrNull(form.rate_value);

  // Live arithmetic preview (mirrors the server estimate; see estimate.ts).
  const estimate = useMemo(
    () =>
      localEstimate({
        rateMode: form.rate_mode,
        rateValue: rateValueNum,
        workers: workersNum,
        engine: form.engine,
        bytesPerEvent,
      }),
    [form.rate_mode, rateValueNum, workersNum, form.engine, bytesPerEvent],
  );

  // A replay pack must run on exactly 1 worker; lock the field and coerce.
  useEffect(() => {
    if (isReplay && form.workers !== "1") patch({ workers: "1" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isReplay]);

  // A metrics pack must run on the metrics engine with count_interval pacing;
  // align interval to the pack's resolution, count to its series and cap workers
  // at the series count. The engine + rate-mode fields are locked below.
  useEffect(() => {
    if (!isMetrics) return;
    setForm((f) => {
      const next: FormState = {
        ...f,
        engine: "metrics",
        rate_mode: "count_interval",
      };
      const detail = metricDetailQ.data;
      if (detail) {
        next.interval_s = String(detail.config.resolution_s);
        next.rate_value = String(detail.series_count);
        const w = Math.max(1, Math.floor(Number(f.workers) || 1));
        if (w > detail.series_count) next.workers = String(detail.series_count);
      }
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMetrics, metricDetailQ.data]);

  const [saving, setSaving] = useState<null | "save" | "run">(null);
  const [errText, setErrText] = useState<string | null>(null);
  const [lintErrors, setLintErrors] = useState<string[] | null>(null);

  // Validation gates that must pass before either save action is allowed.
  const rateNeedsValue =
    form.rate_mode === "eps" || form.rate_mode === "per_day_gb";
  const missing: string[] = [];
  if (!form.name.trim()) missing.push("name");
  if (form.pack_id == null) missing.push("pack");
  if (form.target_id == null) missing.push("target");
  if (rateNeedsValue && (rateValueNum == null || rateValueNum <= 0))
    missing.push("a rate value > 0");

  const overCeiling = !estimate.ok;
  const canSave = missing.length === 0 && saving === null;
  // Start is additionally blocked when the slice is over the ceiling (the API
  // rejects it anyway) or the chosen target last probed red.
  const targetRed = selectedTarget?.health_state === "red";
  const canRun = canSave && !overCeiling;

  function buildCreate(): SpecCreate {
    return {
      name: form.name.trim(),
      pack_id: form.pack_id as number,
      target_id: form.target_id as number,
      engine: form.engine,
      rate_mode: form.rate_mode,
      rate_value: rateNeedsValue ? rateValueNum : numOrNull(form.rate_value),
      interval_s: numOrNull(form.interval_s),
      workers: workersNum,
      duration_s: numOrNull(form.duration_s),
      fleet: form.fleet,
      strict_release: form.strict_release,
      overrides: collectOverrides(form.overrides),
    };
  }

  function buildUpdate(): SpecUpdate {
    return {
      name: form.name.trim(),
      pack_id: form.pack_id as number,
      target_id: form.target_id as number,
      engine: form.engine,
      rate_mode: form.rate_mode,
      rate_value: rateNeedsValue ? rateValueNum : numOrNull(form.rate_value),
      interval_s: numOrNull(form.interval_s),
      workers: workersNum,
      duration_s: numOrNull(form.duration_s),
      fleet: form.fleet,
      strict_release: form.strict_release,
      overrides: collectOverrides(form.overrides),
    };
  }

  // Persist the spec (create or update) and return its id.
  async function persist(): Promise<number> {
    if (editing) {
      const updated = await api.specs.update(edit as number, buildUpdate());
      return updated.id;
    }
    const created = await api.specs.create(buildCreate());
    return created.id;
  }

  async function onSave() {
    setErrText(null);
    setLintErrors(null);
    setSaving("save");
    try {
      const id = await persist();
      toast.success(editing ? "Spec updated." : "Spec saved.");
      navigate({ to: "/specs", search: { highlight: id } });
    } catch (err) {
      const parsed = parseApiError(err);
      setErrText(parsed.message);
      if (parsed.lintErrors) setLintErrors(parsed.lintErrors);
      toast.error(parsed.message);
    } finally {
      setSaving(null);
    }
  }

  async function onSaveAndRun() {
    setErrText(null);
    setLintErrors(null);
    setSaving("run");
    let specId: number;
    try {
      specId = await persist();
    } catch (err) {
      const parsed = parseApiError(err);
      setErrText(parsed.message);
      if (parsed.lintErrors) setLintErrors(parsed.lintErrors);
      toast.error(parsed.message);
      setSaving(null);
      return;
    }
    // Spec saved; now launch. A launch rejection leaves the spec saved so the
    // operator can adjust and retry from the list without losing their work.
    try {
      const run = await api.specs.run(specId);
      toast.success(`Run #${run.run_id} launched.`);
      navigate({ to: "/runs/$runId", params: { runId: String(run.run_id) } });
    } catch (err) {
      const parsed = parseApiError(err);
      // A one-tap fix for the two rejections that map to a field value.
      if (
        parsed.kind === "replay_single_worker" ||
        (parsed.kind === "slice_exceeds_ceiling" && parsed.suggestedWorkers)
      ) {
        const w = parsed.suggestedWorkers ?? 1;
        setErrText(
          `${parsed.message} The spec was saved; workers set to ${w} — Save & Run again to launch.`,
        );
        patch({ workers: String(w) });
      } else {
        setErrText(`${parsed.message} The spec was saved.`);
      }
      if (parsed.lintErrors) setLintErrors(parsed.lintErrors);
      toast.error(parsed.message);
    } finally {
      setSaving(null);
    }
  }

  // Loading / error shells for the reference data the wizard depends on.
  if (packsQ.isPending || targetsQ.isPending || (loadId != null && specQ.isPending)) {
    return (
      <div className="space-y-5">
        <PageHeader title={editing ? "Edit spec" : "New spec"} />
        <Card>
          <LoadingState />
        </Card>
      </div>
    );
  }
  if (packsQ.isError) {
    return <ErrorState error={packsQ.error} onRetry={() => packsQ.refetch()} />;
  }
  if (targetsQ.isError) {
    return <ErrorState error={targetsQ.error} onRetry={() => targetsQ.refetch()} />;
  }

  const title = editing ? "Edit spec" : clone != null ? "Clone spec" : "New spec";

  return (
    <div className="space-y-5">
      <PageHeader
        title={title}
        subtitle="Pick a pack and target, set the rate, then save or launch."
        actions={
          <Button variant="ghost" onClick={() => navigate({ to: "/specs" })}>
            Cancel
          </Button>
        }
      />

      {/* Panel 1: name + pack */}
      <Card title="1 · Pack">
        <div className="space-y-4">
          <Field label="Spec name">
            <TextInput
              placeholder="e.g. apigw-soak"
              value={form.name}
              onChange={(e) => patch({ name: e.target.value })}
            />
          </Field>
          <PackPicker
            packs={packsQ.data}
            selectedId={form.pack_id}
            onSelect={(p) => patch({ pack_id: p.id })}
          />
          {isReplay && (
            <div className="rounded-md border border-amber-800/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
              This pack looks like a replay pack: replay is engine-paced and runs
              on exactly 1 worker (the rate share cannot throttle it). Workers is
              locked to 1. The control plane enforces this at launch regardless.
            </div>
          )}
          {isMetrics && (
            <div className="rounded-md border border-sky-800/60 bg-sky-950/30 px-3 py-2 text-xs text-sky-200">
              Metric pack: runs on the metrics engine, engine-paced on its
              resolution grid. Engine and rate mode are locked to count_interval;
              interval and count follow the pack (resolution and series count) and
              the series matrix is sharded across workers.
            </div>
          )}
        </div>
      </Card>

      {/* Panel 2: target */}
      <Card title="2 · Target">
        <TargetPicker
          targets={targetsQ.data}
          selectedId={form.target_id}
          onSelect={(t) => patch({ target_id: t.id })}
        />
      </Card>

      {/* Panel 3: rate + fleet + overrides, with live arithmetic */}
      <Card title="3 · Rate and fleet">
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Engine">
              <Select
                value={form.engine}
                disabled={isMetrics}
                onChange={(e) => patch({ engine: e.target.value })}
              >
                {(isMetrics ? ["metrics"] : ENGINES).map((e) => (
                  <option key={e} value={e}>
                    {e}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Rate mode">
              <Select
                value={form.rate_mode}
                disabled={isMetrics}
                onChange={(e) => patch({ rate_mode: e.target.value as RateMode })}
              >
                {RATE_MODES.map((m) => (
                  <option key={m} value={m}>
                    {RATE_MODE_LABEL[m]}
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label={
                form.rate_mode === "count_interval"
                  ? "Count (per interval)"
                  : form.rate_mode === "per_day_gb"
                    ? "Rate (GB/day)"
                    : "Rate (events/s)"
              }
              hint={
                rateNeedsValue ? "Total across the fleet, > 0." : "Optional count."
              }
            >
              <TextInput
                type="number"
                min={0}
                value={form.rate_value}
                onChange={(e) => patch({ rate_value: e.target.value })}
              />
            </Field>
          </div>

          <div className="grid gap-3 sm:grid-cols-4">
            <Field
              label="Workers"
              hint={isReplay ? "Locked to 1 for replay." : undefined}
            >
              <TextInput
                type="number"
                min={1}
                value={form.workers}
                disabled={isReplay}
                onChange={(e) => patch({ workers: e.target.value })}
              />
            </Field>
            <Field
              label="Interval (s)"
              hint={
                form.rate_mode === "count_interval" ? "Engine interval." : "Optional."
              }
            >
              <TextInput
                type="number"
                min={0}
                value={form.interval_s}
                onChange={(e) => patch({ interval_s: e.target.value })}
              />
            </Field>
            <Field label="Duration (s)" hint="Blank = unbounded.">
              <TextInput
                type="number"
                min={0}
                value={form.duration_s}
                onChange={(e) => patch({ duration_s: e.target.value })}
              />
            </Field>
            <Field label="Fleet">
              <Select
                value={form.fleet}
                onChange={(e) => patch({ fleet: e.target.value })}
              >
                {FLEETS.map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </Select>
            </Field>
          </div>

          {bytesPerEvent == null && form.pack_id != null && (
            <p className="text-xs text-slate-500">
              This pack has no bytes/event estimate, so the EPS/GB conversion is
              approximate; only the {form.rate_mode === "per_day_gb" ? "GB/day" : "EPS"}{" "}
              ceiling is enforced.
            </p>
          )}

          <div className="rounded-md border border-surface-muted bg-surface-soft p-3">
            <EstimatePanel
              estimate={estimate}
              workers={workersNum}
              source="preview"
            />
          </div>

          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={form.strict_release}
              onChange={(e) => patch({ strict_release: e.target.checked })}
              className="h-4 w-4 rounded border-surface-muted bg-surface"
            />
            Strict release (abort if not all workers are ready at T0, rather than
            running a degraded subset)
          </label>

          <details className="rounded-md border border-surface-muted bg-surface p-3">
            <summary className="cursor-pointer text-sm text-slate-300">
              Overrides (index / sourcetype / source / host)
            </summary>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              {OVERRIDE_KEYS.map((k) => (
                <Field key={k} label={k}>
                  <TextInput
                    placeholder={
                      k === "index" ? selectedTarget?.default_index ?? "" : ""
                    }
                    value={form.overrides[k]}
                    onChange={(e) =>
                      patch({
                        overrides: { ...form.overrides, [k]: e.target.value },
                      })
                    }
                  />
                </Field>
              ))}
            </div>
          </details>
        </div>
      </Card>

      {/* Sticky action bar */}
      <div className="sticky bottom-0 -mx-6 border-t border-surface-muted bg-surface-soft/95 px-6 py-3 backdrop-blur">
        {errText && (
          <div className="mb-3 rounded-md border border-red-800/60 bg-red-950/40 px-3 py-2 text-sm text-red-200">
            {errText}
            {lintErrors && lintErrors.length > 0 && (
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-red-300">
                {lintErrors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-xs text-slate-500">
            {missing.length > 0 ? (
              <span>Provide {missing.join(", ")} to continue.</span>
            ) : overCeiling ? (
              <span className="text-red-300">
                Over the engine ceiling — reduce the rate or add workers to launch.
              </span>
            ) : targetRed ? (
              <span className="text-amber-300">
                Target last probed unhealthy; launch may be rejected.{" "}
                <Badge tone="amber">warning</Badge>
              </span>
            ) : (
              <span>Ready to save.</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="secondary" disabled={!canSave} onClick={onSave}>
              {saving === "save"
                ? "Saving…"
                : editing
                  ? "Save changes"
                  : "Save"}
            </Button>
            <Button variant="primary" disabled={!canRun} onClick={onSaveAndRun}>
              {saving === "run" ? "Launching…" : "Save & Run"}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

export const Route = createFileRoute("/specs_/new")({
  validateSearch: (search: Record<string, unknown>): WizardSearch => {
    const out: WizardSearch = {};
    const edit = Number(search.edit);
    const clone = Number(search.clone);
    if (search.edit != null && Number.isFinite(edit)) out.edit = edit;
    if (search.clone != null && Number.isFinite(clone)) out.clone = clone;
    return out;
  },
  component: JobWizard,
});
