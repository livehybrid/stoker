// Metric-builder helpers: the pattern catalog (so the editor renders pattern
// params generically), sensible defaults, a client-side volume estimate and a
// light validator that mirrors the server's rules (server/bundles.lint_metrics_
// config is the authority; this is just for inline UX).

import type {
  MetricDef,
  MetricgenConfig,
  MetricKind,
  PackOut,
  PatternType,
} from "../../lib/types";

export const METRIC_KINDS: { value: MetricKind; label: string; hint: string }[] = [
  { value: "gauge", label: "gauge", hint: "the value itself (CPU %, latency)" },
  { value: "count", label: "count", hint: "per-interval integer count (requests)" },
  { value: "counter", label: "counter", hint: "monotonic cumulative total" },
];

export const RESOLUTIONS = [1, 5, 10, 30, 60, 300];

export interface PatternParam {
  key: string;
  label: string;
  default: number;
  step?: number;
}

export interface PatternSpec {
  type: PatternType;
  label: string;
  hint: string;
  params: PatternParam[];
}

// Mirrors server/metricpatterns.py. `spike` also takes a list (spikes_h), edited
// as a comma string separately in the editor, so it is not listed as a scalar.
export const PATTERNS: PatternSpec[] = [
  { type: "constant", label: "Constant", hint: "flat", params: [
    { key: "level", label: "Level (0-1)", default: 1.0, step: 0.05 },
  ] },
  { type: "sine", label: "Sine (daily)", hint: "smooth 24 h wave", params: [
    { key: "peak_h", label: "Peak hour", default: 14, step: 0.5 },
    { key: "period_h", label: "Period (h)", default: 24, step: 1 },
    { key: "trough", label: "Trough (0-1)", default: 0, step: 0.05 },
  ] },
  { type: "business_hours", label: "Business hours", hint: "ramp up, plateau, ramp down", params: [
    { key: "start_h", label: "Start hour", default: 8, step: 0.5 },
    { key: "end_h", label: "End hour", default: 18, step: 0.5 },
    { key: "ramp_h", label: "Ramp (h)", default: 1, step: 0.25 },
    { key: "baseline", label: "Overnight (0-1)", default: 0.05, step: 0.05 },
  ] },
  { type: "business_double_hump", label: "Double hump (9-11am + afternoon)", hint: "morning peak, lunch dip, afternoon spike, evening tail", params: [
    { key: "morning_peak_h", label: "Morning peak", default: 10, step: 0.5 },
    { key: "afternoon_peak_h", label: "Afternoon peak", default: 15, step: 0.5 },
    { key: "width_h", label: "Hump width (h)", default: 1.6, step: 0.1 },
    { key: "afternoon_rel", label: "Afternoon vs morning", default: 0.9, step: 0.05 },
    { key: "lunch_dip", label: "Lunch dip (0-1)", default: 0.5, step: 0.05 },
    { key: "baseline", label: "Overnight (0-1)", default: 0.05, step: 0.05 },
  ] },
  { type: "ramp", label: "Ramp (linear)", hint: "steady rise/fall over the day", params: [
    { key: "from", label: "From (0-1)", default: 0.1, step: 0.05 },
    { key: "to", label: "To (0-1)", default: 1.0, step: 0.05 },
  ] },
  { type: "spike", label: "Spike / incident", hint: "baseline with scheduled spikes", params: [
    { key: "baseline", label: "Baseline (0-1)", default: 0.1, step: 0.05 },
    { key: "amplitude", label: "Amplitude (0-1)", default: 0.9, step: 0.05 },
    { key: "width_h", label: "Spike width (h)", default: 0.25, step: 0.05 },
  ] },
  { type: "random_walk", label: "Random walk", hint: "bounded wander (memory, queue depth)", params: [
    { key: "baseline", label: "Centre (0-1)", default: 0.5, step: 0.05 },
    { key: "step", label: "Step size", default: 0.05, step: 0.01 },
    { key: "revert", label: "Mean reversion", default: 0.02, step: 0.01 },
  ] },
];

export function patternSpec(type: PatternType): PatternSpec {
  return PATTERNS.find((p) => p.type === type) ?? PATTERNS[0];
}

/** A pattern object with every param at its default for the given type. */
export function defaultPattern(type: PatternType): Record<string, unknown> {
  const spec = patternSpec(type);
  const out: Record<string, unknown> = { type };
  for (const p of spec.params) out[p.key] = p.default;
  if (type === "spike") out.spikes_h = [3, 15];
  return out;
}

export function defaultMetric(name = "store.requests"): MetricDef {
  return {
    name,
    kind: "count",
    unit: "requests",
    min: 5,
    p95: 800,
    max: 1500,
    noise: 0.15,
    pattern: { type: "business_double_hump", ...defaultPattern("business_double_hump") },
  };
}

export function defaultConfig(): MetricgenConfig {
  return {
    resolution_s: 10,
    tz_offset_hours: 0,
    seed: 1974,
    sourcetype: "stoker:metric",
    dimensions: [
      { key: "product", values: ["checkout", "search", "catalog"] },
      { key: "region", values: ["eu-west-1", "us-east-1"] },
    ],
    metrics: [defaultMetric()],
  };
}

/** True when a pack is a UI-authored metrics pack. */
export function packIsMetrics(pack: PackOut | undefined | null): boolean {
  if (!pack) return false;
  const engines = Array.isArray(pack.engines_json)
    ? pack.engines_json.map(String)
    : [];
  return engines.includes("metrics");
}

export function seriesCount(config: MetricgenConfig): number {
  let n = 1;
  for (const d of config.dimensions) {
    if (d.values.length > 0) n *= d.values.length;
  }
  return n;
}

export interface VolumeEstimate {
  series: number;
  eventsPerSec: number;
  measurementsPerSec: number;
}

export function estimateVolume(config: MetricgenConfig): VolumeEstimate {
  const series = seriesCount(config);
  const res = config.resolution_s > 0 ? config.resolution_s : 1;
  return {
    series,
    eventsPerSec: series / res,
    measurementsPerSec: (series * config.metrics.length) / res,
  };
}

const KNOWN_PATTERNS = new Set(PATTERNS.map((p) => p.type as string));
const KNOWN_KINDS = new Set(["gauge", "count", "counter"]);
const MAX_SERIES = 5000;

/** Light client-side validation mirroring the server; the server is authority. */
export function validate(config: MetricgenConfig): string[] {
  const errors: string[] = [];
  if (!(config.resolution_s > 0)) errors.push("Resolution must be greater than 0.");
  const dimKeys = new Set<string>();
  config.dimensions.forEach((d, i) => {
    if (!d.key.trim()) errors.push(`Dimension ${i + 1} needs a name.`);
    else dimKeys.add(d.key.trim());
    if (d.values.length === 0)
      errors.push(`Dimension "${d.key || i + 1}" needs at least one value.`);
  });
  if (seriesCount(config) > MAX_SERIES)
    errors.push(`The matrix is ${seriesCount(config)} series (max ${MAX_SERIES}); reduce dimensions or values.`);
  if (config.metrics.length === 0) errors.push("Add at least one metric.");
  const seen = new Set<string>();
  config.metrics.forEach((m, i) => {
    const label = m.name?.trim() || `#${i + 1}`;
    if (!m.name?.trim()) errors.push(`Metric ${label} needs a name.`);
    else if (seen.has(m.name)) errors.push(`Duplicate metric name "${m.name}".`);
    else seen.add(m.name);
    if (!KNOWN_KINDS.has(m.kind)) errors.push(`Metric ${label}: unknown kind.`);
    if (!(m.min <= m.p95 && m.p95 <= m.max))
      errors.push(`Metric ${label}: needs min <= p95 <= max.`);
    if ((m.noise ?? 0) < 0) errors.push(`Metric ${label}: noise must be >= 0.`);
    if (!KNOWN_PATTERNS.has(m.pattern?.type))
      errors.push(`Metric ${label}: unknown pattern.`);
    if (m.scale) {
      for (const dk of Object.keys(m.scale))
        if (!dimKeys.has(dk))
          errors.push(`Metric ${label}: scale references unknown dimension "${dk}".`);
    }
  });
  return errors;
}
