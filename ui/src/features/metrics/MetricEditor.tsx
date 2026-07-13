import { useState } from "react";

import type {
  MetricDef,
  MetricDimension,
  MetricKind,
  PatternType,
} from "../../lib/types";
import { Button } from "../../components/Button";
import { Field, Select, TextInput } from "../../components/Field";
import {
  METRIC_KINDS,
  PATTERNS,
  defaultPattern,
  patternSpec,
} from "./config";

// One metric editor card: identity + value model (min/p95/max) + kind + pattern
// (with its params rendered from the catalog) + an optional per-dimension scale.

interface Props {
  metric: MetricDef;
  dimensions: MetricDimension[];
  onChange: (metric: MetricDef) => void;
  onRemove: () => void;
  onPreview: () => void;
  active: boolean;
}

function num(v: string, fallback: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

export function MetricEditor({
  metric,
  dimensions,
  onChange,
  onRemove,
  onPreview,
  active,
}: Props) {
  const [showScale, setShowScale] = useState(
    metric.scale != null && Object.keys(metric.scale).length > 0,
  );
  const patch = (p: Partial<MetricDef>) => onChange({ ...metric, ...p });
  const spec = patternSpec(metric.pattern.type as PatternType);

  function setPatternType(type: PatternType) {
    patch({ pattern: { type, ...defaultPattern(type) } });
  }
  function setPatternParam(key: string, value: number) {
    patch({ pattern: { ...metric.pattern, [key]: value } });
  }
  function setSpikes(raw: string) {
    const spikes = raw
      .split(",")
      .map((s) => Number(s.trim()))
      .filter((n) => Number.isFinite(n));
    patch({ pattern: { ...metric.pattern, spikes_h: spikes } });
  }
  function setScale(dimKey: string, value: string, mult: number) {
    const scale = { ...(metric.scale ?? {}) };
    const table = { ...(scale[dimKey] ?? {}) };
    if (mult === 1) delete table[value];
    else table[value] = mult;
    if (Object.keys(table).length === 0) delete scale[dimKey];
    else scale[dimKey] = table;
    patch({ scale: Object.keys(scale).length ? scale : undefined });
  }

  return (
    <div
      className={
        "rounded-lg border bg-surface-soft p-4 " +
        (active ? "border-sky-600/70 ring-1 ring-sky-600/40" : "border-surface-muted")
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Metric name">
          <TextInput
            placeholder="store.requests"
            value={metric.name}
            onChange={(e) => patch({ name: e.target.value })}
            autoComplete="off"
          />
        </Field>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Unit">
            <TextInput
              placeholder="requests"
              value={metric.unit ?? ""}
              onChange={(e) => patch({ unit: e.target.value })}
              autoComplete="off"
            />
          </Field>
          <Field label="Kind">
            <Select
              value={metric.kind}
              onChange={(e) => patch({ kind: e.target.value as MetricKind })}
            >
              {METRIC_KINDS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-4 gap-2">
        <Field label="min" hint="quiet floor">
          <TextInput
            type="number"
            value={String(metric.min)}
            onChange={(e) => patch({ min: num(e.target.value, metric.min) })}
          />
        </Field>
        <Field label="p95" hint="busy level">
          <TextInput
            type="number"
            value={String(metric.p95)}
            onChange={(e) => patch({ p95: num(e.target.value, metric.p95) })}
          />
        </Field>
        <Field label="max" hint="ceiling">
          <TextInput
            type="number"
            value={String(metric.max)}
            onChange={(e) => patch({ max: num(e.target.value, metric.max) })}
          />
        </Field>
        <Field label="noise">
          <TextInput
            type="number"
            step="0.05"
            value={String(metric.noise ?? 0.1)}
            onChange={(e) => patch({ noise: num(e.target.value, metric.noise ?? 0.1) })}
          />
        </Field>
      </div>

      <div className="mt-3">
        <Field label="Pattern" hint={spec.hint}>
          <Select
            value={metric.pattern.type}
            onChange={(e) => setPatternType(e.target.value as PatternType)}
          >
            {PATTERNS.map((p) => (
              <option key={p.type} value={p.type}>
                {p.label}
              </option>
            ))}
          </Select>
        </Field>
        <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3">
          {spec.params.map((p) => (
            <Field key={p.key} label={p.label}>
              <TextInput
                type="number"
                step={p.step ?? 1}
                value={String((metric.pattern[p.key] as number) ?? p.default)}
                onChange={(e) => setPatternParam(p.key, num(e.target.value, p.default))}
              />
            </Field>
          ))}
          {metric.pattern.type === "spike" && (
            <Field label="Spike hours (comma)">
              <TextInput
                placeholder="3, 15"
                defaultValue={
                  Array.isArray(metric.pattern.spikes_h)
                    ? (metric.pattern.spikes_h as number[]).join(", ")
                    : ""
                }
                onBlur={(e) => setSpikes(e.target.value)}
              />
            </Field>
          )}
        </div>
      </div>

      {dimensions.length > 0 && (
        <div className="mt-3">
          <button
            type="button"
            className="text-xs font-medium text-sky-400 hover:text-sky-300"
            onClick={() => setShowScale((s) => !s)}
          >
            {showScale ? "− Hide" : "+ Per-dimension scale"} (magnitude per value)
          </button>
          {showScale && (
            <div className="mt-2 space-y-2">
              {dimensions.map((dim) => (
                <div key={dim.key} className="flex flex-wrap items-center gap-2">
                  <span className="w-24 shrink-0 text-xs text-slate-400">{dim.key}</span>
                  {dim.values.map((val) => (
                    <label key={val} className="flex items-center gap-1 text-xs text-slate-400">
                      {val}
                      <input
                        type="number"
                        step="0.1"
                        className="w-16 rounded border border-surface-muted bg-surface px-2 py-1 text-xs text-slate-100"
                        value={String(metric.scale?.[dim.key]?.[val] ?? 1)}
                        onChange={(e) => setScale(dim.key, val, num(e.target.value, 1))}
                      />
                    </label>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="mt-3 flex items-center justify-between">
        <Button variant="ghost" onClick={onRemove}>
          Remove metric
        </Button>
        <Button variant={active ? "primary" : "secondary"} onClick={onPreview}>
          {active ? "Previewing" : "Preview this"}
        </Button>
      </div>
    </div>
  );
}
