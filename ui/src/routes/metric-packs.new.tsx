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
    <div className="space-y-5">
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
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name">
            <TextInput
              placeholder="store-kpis"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Description">
            <TextInput
              placeholder="Buttercup Games store KPIs"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Metrics sourcetype">
            <TextInput
              value={config.sourcetype ?? "stoker:metric"}
              onChange={(e) => patchConfig({ sourcetype: e.target.value })}
              autoComplete="off"
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Resolution" hint="grid period">
              <Select
                value={String(config.resolution_s)}
                onChange={(e) => patchConfig({ resolution_s: Number(e.target.value) })}
              >
                {RESOLUTIONS.map((r) => (
                  <option key={r} value={String(r)}>
                    {r}s
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="TZ offset (h)" hint="patterns in this tz">
              <TextInput
                type="number"
                value={String(config.tz_offset_hours ?? 0)}
                onChange={(e) => patchConfig({ tz_offset_hours: Number(e.target.value) || 0 })}
              />
            </Field>
          </div>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          Metric packs run on the metrics engine (engine-paced on the resolution
          grid); a metrics index must exist in Splunk.
        </p>
      </Card>

      <Card title="Dimensions (the matrix)">
        <DimensionEditor
          dimensions={config.dimensions}
          onChange={(dimensions) => patchConfig({ dimensions })}
          seriesCount={series}
        />
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <div className="space-y-3">
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
        </div>

        <div className="lg:sticky lg:top-4 lg:self-start">
          <Card title="Live preview (24 h)">
            <div className="mb-3 grid grid-cols-2 gap-2">
              <Field label="Metric">
                <Select
                  value={String(activeMetric)}
                  onChange={(e) => setActiveMetric(Number(e.target.value))}
                >
                  {metrics.map((m, i) => (
                    <option key={i} value={String(i)}>
                      {m.name || `#${i + 1}`}
                    </option>
                  ))}
                </Select>
              </Field>
              {config.dimensions.length > 0 && (
                <Field label="Cell">
                  <div className="flex flex-wrap gap-1">
                    {config.dimensions.map((d) => (
                      <Select
                        key={d.key}
                        value={cell[d.key] ?? ""}
                        onChange={(e) => setCell((c) => ({ ...c, [d.key]: e.target.value }))}
                        className="w-auto"
                      >
                        {d.values.map((v) => (
                          <option key={v} value={v}>
                            {v}
                          </option>
                        ))}
                      </Select>
                    ))}
                  </div>
                </Field>
              )}
            </div>
            <PreviewChart data={preview} loading={previewing} />
          </Card>
        </div>
      </div>

      {errors.length > 0 && (
        <Card>
          <ul className="list-disc space-y-0.5 pl-5 text-xs text-amber-300">
            {errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </Card>
      )}

      <div className="sticky bottom-0 flex items-center justify-between gap-3 rounded-lg border border-surface-muted bg-surface-soft/95 px-4 py-3 backdrop-blur">
        <p className="text-xs text-slate-400">
          <span className="font-medium text-slate-200">{volume.series}</span> series ·{" "}
          <span className="font-medium text-slate-200">{volume.eventsPerSec.toFixed(2)}</span> events/s ·{" "}
          <span className="font-medium text-slate-200">{volume.measurementsPerSec.toFixed(1)}</span> measurements/s
        </p>
        <Button variant="primary" onClick={() => save.mutate()} disabled={!canSave}>
          {save.isPending ? "Saving…" : editing ? "Save changes" : "Save metric pack"}
        </Button>
      </div>
    </div>
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
