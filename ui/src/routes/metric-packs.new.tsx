import { useEffect, useMemo, useRef, useState } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, ApiError } from "../lib/api";
import type {
  MetricgenConfig,
  MetricPreviewResponse,
} from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
import { Field, Select, TextInput } from "../components/Field";
import { useToast } from "../components/Toast";
import {
  RESOLUTIONS,
  defaultConfig,
  defaultMetric,
  estimateVolume,
  seriesCount,
  validate,
} from "../features/metrics/config";
import { DimensionEditor } from "../features/metrics/DimensionEditor";
import { MetricEditor } from "../features/metrics/MetricEditor";
import { PreviewChart } from "../features/metrics/PreviewChart";
import { Bullets, Grid, Muted, Stack, StickyBar, Strong, Tags } from "../components/text";

interface BuilderSearch {
  edit?: number;
}

function MetricBuilder() {
  const navigate = useNavigate();
  const toast = useToast();
  const { edit } = Route.useSearch();
  const editing = typeof edit === "number";

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [config, setConfig] = useState<MetricgenConfig>(defaultConfig);
  const [activeMetric, setActiveMetric] = useState(0);
  const [cell, setCell] = useState<Record<string, string>>({});

  // Edit mode: load the pack's config once and hydrate.
  const packQ = useQuery({
    queryKey: ["metric-pack", edit],
    queryFn: () => api.metricPacks.get(edit as number),
    enabled: editing,
  });
  const hydrated = useRef(false);
  useEffect(() => {
    if (!editing || hydrated.current || !packQ.data) return;
    setName(packQ.data.name);
    setDescription(packQ.data.description ?? "");
    setConfig(packQ.data.config);
    hydrated.current = true;
  }, [editing, packQ.data]);

  const patchConfig = (p: Partial<MetricgenConfig>) =>
    setConfig((c) => ({ ...c, ...p }));

  const metrics = config.metrics;
  const series = seriesCount(config);
  const volume = useMemo(() => estimateVolume(config), [config]);
  const errors = useMemo(() => validate(config), [config]);
  const activeName = metrics[activeMetric]?.name;

  // Keep the preview cell aligned to the current dimensions (first value each).
  useEffect(() => {
    setCell((prev) => {
      const next: Record<string, string> = {};
      for (const d of config.dimensions) {
        if (d.values.length === 0) continue;
        next[d.key] = prev[d.key] && d.values.includes(prev[d.key]) ? prev[d.key] : d.values[0];
      }
      return next;
    });
  }, [config.dimensions]);

  // Debounced live preview of the active metric.
  const [preview, setPreview] = useState<MetricPreviewResponse | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const previewSeq = useRef(0);
  const previewKey = JSON.stringify({ config, activeName, cell });
  useEffect(() => {
    if (!activeName) {
      setPreview(null);
      return;
    }
    const seq = ++previewSeq.current;
    setPreviewing(true);
    const timer = window.setTimeout(async () => {
      try {
        const res = await api.metricPacks.preview({
          config,
          metric: activeName,
          cell,
          points: 96,
        });
        if (seq === previewSeq.current) setPreview(res);
      } catch {
        if (seq === previewSeq.current) setPreview(null);
      } finally {
        if (seq === previewSeq.current) setPreviewing(false);
      }
    }, 300);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewKey]);

  const save = useMutation({
    mutationFn: () => {
      const body = { name: name.trim(), description: description.trim() || null, config };
      return editing
        ? api.metricPacks.update(edit as number, body)
        : api.metricPacks.create(body);
    },
    onSuccess: (pack) => {
      toast.success(editing ? "Metric pack updated." : `Metric pack "${pack.name}" created.`);
      navigate({ to: "/packs" });
    },
    onError: (err: unknown) => {
      toast.error(err instanceof ApiError ? err.message : "Could not save the metric pack.");
    },
  });

  const canSave = name.trim().length > 0 && errors.length === 0 && !save.isPending;

  return (
    <Stack $gap="large">
      <PageHeader
        title={editing ? "Edit metric pack" : "New metric pack"}
        subtitle="Build a matrix of Splunk metrics with day-shaped values. Preview updates live."
        actions={
          <Button variant="ghost" onClick={() => navigate({ to: "/packs" })}>
            Cancel
          </Button>
        }
      />

      <Card title="Pack">
        <Grid $min="180px">
          <Field label="Name">
            <TextInput
              placeholder="store-kpis"
              value={name}
              onChange={(_e, { value }) => setName(value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Description">
            <TextInput
              placeholder="Buttercup Games store KPIs"
              value={description}
              onChange={(_e, { value }) => setDescription(value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Metrics sourcetype">
            <TextInput
              value={config.sourcetype ?? "stoker:metric"}
              onChange={(_e, { value }) => patchConfig({ sourcetype: value })}
              autoComplete="off"
            />
          </Field>
          <Grid $min="180px">
            <Field label="Resolution" hint="grid period">
              <Select
                value={String(config.resolution_s)}
                onChange={(_e, { value }) => patchConfig({ resolution_s: Number(value) })}
              >
                {RESOLUTIONS.map((r) => (
                  <Select.Option key={r} value={String(r)} label={`${r}s`} />
                ))}
              </Select>
            </Field>
            <Field label="TZ offset (h)" hint="patterns in this tz">
              <TextInput
                type="number"
                value={String(config.tz_offset_hours ?? 0)}
                onChange={(_e, { value }) => patchConfig({ tz_offset_hours: Number(value) || 0 })}
              />
            </Field>
          </Grid>
        </Grid>
        <Muted $small>
          Metric packs run on the metrics engine (engine-paced on the resolution
          grid); a metrics index must exist in Splunk.
        </Muted>
      </Card>

      <Card title="Dimensions (the matrix)">
        <DimensionEditor
          dimensions={config.dimensions}
          onChange={(dimensions) => patchConfig({ dimensions })}
          seriesCount={series}
        />
      </Card>

      <Grid $min="320px">
        <Stack>
          {metrics.map((metric, i) => (
            <MetricEditor
              key={i}
              metric={metric}
              dimensions={config.dimensions}
              active={i === activeMetric}
              onPreview={() => setActiveMetric(i)}
              onRemove={() => {
                patchConfig({ metrics: metrics.filter((_, j) => j !== i) });
                setActiveMetric((a) => Math.max(0, a >= i ? a - 1 : a));
              }}
              onChange={(m) =>
                patchConfig({ metrics: metrics.map((x, j) => (j === i ? m : x)) })
              }
            />
          ))}
          <Button
            variant="secondary"
            onClick={() => {
              patchConfig({ metrics: [...metrics, defaultMetric(`metric.${metrics.length + 1}`)] });
              setActiveMetric(metrics.length);
            }}
          >
            + Add metric
          </Button>
        </Stack>

        <div>
          <Card title="Live preview (24 h)">
            <Grid $min="160px">
              <Field label="Metric">
                <Select
                  value={String(activeMetric)}
                  onChange={(_e, { value }) => setActiveMetric(Number(value))}
                >
                  {metrics.map((m, i) => (
                    <Select.Option key={i} value={String(i)} label={m.name || `#${i + 1}`} />
                  ))}
                </Select>
              </Field>
              {config.dimensions.length > 0 && (
                <Field label="Cell">
                  <Tags>
                    {config.dimensions.map((d) => (
                      <Select
                        key={d.key}
                        value={cell[d.key] ?? ""}
                        onChange={(_e, { value }) => setCell((c) => ({ ...c, [d.key]: String(value) }))}
                      >
                        {d.values.map((v) => (
                          <Select.Option key={v} value={v} label={v} />
                        ))}
                      </Select>
                    ))}
                  </Tags>
                </Field>
              )}
            </Grid>
            <PreviewChart data={preview} loading={previewing} />
          </Card>
        </div>
      </Grid>

      {errors.length > 0 && (
        <Card>
          <Bullets>
            {errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </Bullets>
        </Card>
      )}

      <StickyBar>
        <Muted $small>
          <Strong>{volume.series}</Strong> series ·{" "}
          <Strong>{volume.eventsPerSec.toFixed(2)}</Strong> events/s ·{" "}
          <Strong>{volume.measurementsPerSec.toFixed(1)}</Strong> measurements/s
        </Muted>
        <Button variant="primary" onClick={() => save.mutate()} disabled={!canSave}>
          {save.isPending ? "Saving…" : editing ? "Save changes" : "Save metric pack"}
        </Button>
      </StickyBar>
    </Stack>
  );
}

export const Route = createFileRoute("/metric-packs/new")({
  validateSearch: (search: Record<string, unknown>): BuilderSearch => {
    const raw = search.edit;
    const n = typeof raw === "number" ? raw : Number(raw);
    return Number.isFinite(n) && n > 0 ? { edit: n } : {};
  },
  component: MetricBuilder,
});
