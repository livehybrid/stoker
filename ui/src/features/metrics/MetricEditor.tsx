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
import { Between, Grid, Inline, Muted, Stack } from "../../components/text";
import styled from "styled-components";
import { variables } from "@splunk/themes";

const Editing = styled.div<{ $active?: boolean }>`
  border: 1px solid
    ${(props) => (props.$active ? variables.contentColorAccent : variables.borderColor)};
  border-radius: ${variables.borderRadius};
  background-color: ${variables.backgroundColorSection};
  padding: ${variables.spacingLarge};
  box-shadow: ${(props) => (props.$active ? variables.focusShadow : "none")};
`;

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
    <Editing $active={active}>
      <Grid $min="180px">
        <Field label="Metric name">
          <TextInput
            placeholder="store.requests"
            value={metric.name}
            onChange={(_e, { value }) => patch({ name: value })}
            autoComplete="off"
          />
        </Field>
        <Grid $min="180px">
          <Field label="Unit">
            <TextInput
              placeholder="requests"
              value={metric.unit ?? ""}
              onChange={(_e, { value }) => patch({ unit: value })}
              autoComplete="off"
            />
          </Field>
          <Field label="Kind">
            <Select
              value={metric.kind}
              onChange={(_e, { value }) => patch({ kind: value as MetricKind })}
            >
              {METRIC_KINDS.map((k) => (
                <Select.Option key={k.value} value={k.value} label={k.label} />
              ))}
            </Select>
          </Field>
        </Grid>
      </Grid>

      <Grid $min="140px">
        <Field label="min" hint="quiet floor">
          <TextInput
            type="number"
            value={String(metric.min)}
            onChange={(_e, { value }) => patch({ min: num(value, metric.min) })}
          />
        </Field>
        <Field label="p95" hint="busy level">
          <TextInput
            type="number"
            value={String(metric.p95)}
            onChange={(_e, { value }) => patch({ p95: num(value, metric.p95) })}
          />
        </Field>
        <Field label="max" hint="ceiling">
          <TextInput
            type="number"
            value={String(metric.max)}
            onChange={(_e, { value }) => patch({ max: num(value, metric.max) })}
          />
        </Field>
        <Field label="noise">
          <TextInput
            type="number"
            value={String(metric.noise ?? 0.1)}
            onChange={(_e, { value }) => patch({ noise: num(value, metric.noise ?? 0.1) })}
          />
        </Field>
      </Grid>

      <div>
        <Field label="Pattern" hint={spec.hint}>
          <Select
            value={metric.pattern.type}
            onChange={(_e, { value }) => setPatternType(value as PatternType)}
          >
            {PATTERNS.map((p) => (
              <Select.Option key={p.type} value={p.type} label={p.label} />
            ))}
          </Select>
        </Field>
        <Grid $min="160px">
          {spec.params.map((p) => (
            <Field key={p.key} label={p.label}>
              <TextInput
                type="number"
                value={String((metric.pattern[p.key] as number) ?? p.default)}
                onChange={(_e, { value }) => setPatternParam(p.key, num(value, p.default))}
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
        </Grid>
      </div>

      {dimensions.length > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setShowScale((s) => !s)}
          >
            {showScale ? "− Hide" : "+ Per-dimension scale"} (magnitude per value)
          </button>
          {showScale && (
            <Stack>
              {dimensions.map((dim) => (
                <Inline key={dim.key}>
                  <Muted $small>{dim.key}</Muted>
                  {dim.values.map((val) => (
                    <Inline key={val}>
                      {val}
                      <TextInput
                        type="number"
                        inline
                        value={String(metric.scale?.[dim.key]?.[val] ?? 1)}
                        onChange={(_e, { value }) => setScale(dim.key, val, num(value, 1))}
                      />
                    </Inline>
                  ))}
                </Inline>
              ))}
            </Stack>
          )}
        </div>
      )}

      <Between>
        <Button variant="ghost" onClick={onRemove}>
          Remove metric
        </Button>
        <Button variant={active ? "primary" : "secondary"} onClick={onPreview}>
          {active ? "Previewing" : "Preview this"}
        </Button>
      </Between>
    </Editing>
  );
}
