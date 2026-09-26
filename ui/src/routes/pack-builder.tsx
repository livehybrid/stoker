import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import TextArea from "@splunk/react-ui/TextArea";
import Switch from "@splunk/react-ui/Switch";

import { api, ApiError } from "../lib/api";
import type {
  BuilderConfig,
  BuilderHighlights,
  BuilderReplacement,
  BuilderReplacementKind,
  BuilderSuggestion,
  WordlistInfo,
} from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
import { Badge } from "../components/Badge";
import { Field, Select, TextInput } from "../components/Field";
import { useToast } from "../components/Toast";
import { ErrorState, LoadingState } from "../components/States";
import {
  Between,
  Bullets,
  Callout,
  Grid,
  Inline,
  Mono,
  Muted,
  Panel,
  Stack,
  StickyBar,
  Strong,
  Warn,
} from "../components/text";

// Pack builder: paste or upload real events, let the server suggest which
// parts to abstract (timestamps, IPs, users, cities, status codes ...), pick
// a replacement for each (a shipped word list, the values seen, a random
// generator), preview the generated events and save an ordinary eventgen pack.

interface BuilderSearch {
  edit?: number;
}

// One highlight colour per field, readable in both themes.
const PALETTE = [
  "rgba(66, 135, 245, 0.30)",
  "rgba(245, 166, 35, 0.35)",
  "rgba(80, 200, 120, 0.32)",
  "rgba(220, 80, 160, 0.30)",
  "rgba(150, 110, 230, 0.32)",
  "rgba(40, 190, 200, 0.32)",
  "rgba(230, 90, 70, 0.30)",
  "rgba(170, 170, 60, 0.35)",
];

const SIMPLE_KINDS: Array<[BuilderReplacementKind, string]> = [
  ["timestamp", "Timestamp (the event time)"],
  ["values", "Values I list"],
  ["ipv4", "Random IPv4"],
  ["guid", "Random GUID"],
  ["mac", "Random MAC"],
  ["integer", "Random integer"],
  ["float", "Random decimal"],
  ["hex", "Random hex"],
  ["sequence", "Sequence number"],
  ["static", "Fixed value"],
];

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function replacementKey(r: BuilderReplacement): string {
  return r.kind === "list" ? `list:${r.list}` : r.kind;
}

function defaultReplacement(key: string, examples: string[]): BuilderReplacement {
  if (key.startsWith("list:")) return { kind: "list", list: key.slice(5) };
  switch (key as BuilderReplacementKind) {
    case "timestamp":
      return { kind: "timestamp", format: "%Y-%m-%dT%H:%M:%S" };
    case "values":
      return { kind: "values", values: examples.length ? examples : ["value"] };
    case "integer":
      return { kind: "integer", min: 0, max: 1000 };
    case "float":
      return { kind: "float", min: 0, max: 100, decimals: 2 };
    case "hex":
      return { kind: "hex", length: 16 };
    case "static":
      return { kind: "static", value: examples[0] ?? "" };
    case "sequence":
      return { kind: "sequence", start: 1 };
    default:
      return { kind: key as BuilderReplacementKind };
  }
}

function num(v: string, fallback: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function apiMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { detail?: unknown } | string | undefined;
    if (d && typeof d === "object" && typeof d.detail === "string") return d.detail;
    if (typeof d === "string") return d;
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

/** One event with the spans each field would rewrite marked in its colour. */
function MarkedEvent({
  text,
  spans,
  colour,
  label,
}: {
  text: string;
  spans: Array<[number, number, string]>;
  colour: (id: string) => string;
  label: (id: string) => string;
}) {
  const parts: ReactNode[] = [];
  let pos = 0;
  spans.forEach(([s, e, id], i) => {
    if (s < pos) return;
    if (s > pos) parts.push(text.slice(pos, s));
    parts.push(
      <mark
        key={i}
        title={label(id)}
        style={{ background: colour(id), color: "inherit", borderRadius: 3, padding: "0 1px" }}
      >
        {text.slice(s, e)}
      </mark>,
    );
    pos = e;
  });
  parts.push(text.slice(pos));
  return (
    <Mono $break style={{ display: "block", whiteSpace: "pre-wrap", padding: "2px 0" }}>
      {parts}
    </Mono>
  );
}

function ReplacementEditor({
  value,
  examples,
  lists,
  onChange,
}: {
  value: BuilderReplacement;
  examples: string[];
  lists: WordlistInfo[];
  onChange: (r: BuilderReplacement) => void;
}) {
  const key = replacementKey(value);
  return (
    <Grid $min="180px">
      <Field label="Replace with">
        <Select value={key} onChange={(_e, { value: v }) => onChange(defaultReplacement(String(v), examples))}>
          {lists.map((l) => (
            <Select.Option key={l.name} value={`list:${l.name}`} label={`Word list: ${l.title}`} />
          ))}
          {SIMPLE_KINDS.map(([k, label]) => (
            <Select.Option key={k} value={k} label={label} />
          ))}
        </Select>
      </Field>
      {value.kind === "timestamp" && (
        <Field label="Format" hint="strftime, e.g. %Y-%m-%dT%H:%M:%S or %s for epoch">
          <TextInput value={value.format ?? ""} onChange={(_e, { value: v }) => onChange({ ...value, format: v })} />
        </Field>
      )}
      {value.kind === "list" && (
        <Field label="List">
          <Muted $small>
            {lists.find((l) => l.name === value.list)?.description ?? value.list}
            {" · e.g. "}
            {(lists.find((l) => l.name === value.list)?.sample ?? []).slice(0, 3).join(", ")}
          </Muted>
        </Field>
      )}
      {value.kind === "values" && (
        <Field label="Values" hint="one per line; repeat a value to make it more likely">
          <TextArea
            rowsMin={2}
            rowsMax={8}
            value={(value.values ?? []).join("\n")}
            onChange={(_e, { value: v }) => onChange({ ...value, values: String(v).split("\n") })}
          />
        </Field>
      )}
      {(value.kind === "integer" || value.kind === "float") && (
        <>
          <Field label="Minimum">
            <TextInput
              value={String(value.min ?? 0)}
              onChange={(_e, { value: v }) => onChange({ ...value, min: num(v, 0) })}
            />
          </Field>
          <Field label="Maximum">
            <TextInput
              value={String(value.max ?? 0)}
              onChange={(_e, { value: v }) => onChange({ ...value, max: num(v, 0) })}
            />
          </Field>
        </>
      )}
      {value.kind === "float" && (
        <Field label="Decimals">
          <TextInput
            value={String(value.decimals ?? 2)}
            onChange={(_e, { value: v }) => onChange({ ...value, decimals: num(v, 2) })}
          />
        </Field>
      )}
      {value.kind === "hex" && (
        <Field label="Length">
          <TextInput
            value={String(value.length ?? 16)}
            onChange={(_e, { value: v }) => onChange({ ...value, length: num(v, 16) })}
          />
        </Field>
      )}
      {value.kind === "static" && (
        <Field label="Value">
          <TextInput value={value.value ?? ""} onChange={(_e, { value: v }) => onChange({ ...value, value: v })} />
        </Field>
      )}
      {value.kind === "sequence" && (
        <Field label="Start at">
          <TextInput
            value={String(value.start ?? 1)}
            onChange={(_e, { value: v }) => onChange({ ...value, start: num(v, 1) })}
          />
        </Field>
      )}
    </Grid>
  );
}

function PackBuilder() {
  const navigate = useNavigate();
  const toast = useToast();
  const { edit } = Route.useSearch();
  const editing = typeof edit === "number";

  const [text, setText] = useState("");
  const [events, setEvents] = useState<string[]>([]);
  const [tokens, setTokens] = useState<BuilderSuggestion[]>([]);
  const [highlights, setHighlights] = useState<BuilderHighlights>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [sourcetype, setSourcetype] = useState("");
  const [tags, setTags] = useState("");
  const [count, setCount] = useState("10");
  const [interval, setInterval] = useState("1");
  const [order, setOrder] = useState<"sequential" | "random">("sequential");
  const [custom, setCustom] = useState("");
  const [customIsRegex, setCustomIsRegex] = useState(false);
  const [analyseError, setAnalyseError] = useState<string | null>(null);

  const listsQ = useQuery({ queryKey: ["pack-builder", "wordlists"], queryFn: api.packBuilder.wordlists });
  const lists = listsQ.data ?? [];

  // Edit mode: reopen a builder pack's saved config once.
  const packQ = useQuery({
    queryKey: ["pack-builder", "pack", edit],
    queryFn: () => api.packBuilder.get(edit as number),
    enabled: editing,
  });
  const hydrated = useRef(false);
  useEffect(() => {
    if (!editing || hydrated.current || !packQ.data) return;
    const c = packQ.data.config;
    setEvents(c.events);
    setText(c.events.join("\n"));
    setTokens(c.tokens.map((t) => ({ ...t, kind: "saved", why: "", examples: [], matches: 0 })));
    setName(c.name);
    setDescription(c.description ?? "");
    setSourcetype(c.sourcetype ?? "");
    setTags((c.tags ?? []).join(", "));
    setCount(String(c.count));
    setInterval(String(c.interval));
    setOrder(c.order ?? "sequential");
    hydrated.current = true;
  }, [editing, packQ.data]);

  const analyse = useMutation({
    mutationFn: () => api.packBuilder.analyse(text),
    onSuccess: (res) => {
      setAnalyseError(null);
      setEvents(res.event_list);
      setTokens(res.suggestions);
      setHighlights(res.highlights);
      if (!name) setName("");
    },
    onError: (err) => setAnalyseError(apiMessage(err)),
  });

  async function onFile(file: File | undefined) {
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
      toast.error("That file is over 2 MB; trim it to a representative sample.");
      return;
    }
    setText(await file.text());
    if (!name) setName(file.name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9 _.-]/g, "-").slice(0, 64));
  }

  const config: BuilderConfig = useMemo(
    () => ({
      name: name.trim() || "preview",
      description: description.trim(),
      sourcetype: sourcetype.trim() || null,
      tags: tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      events,
      tokens: tokens.map(({ id, field, pattern, enabled, replacement }) => ({
        id,
        field,
        pattern,
        enabled,
        replacement:
          replacement.kind === "values"
            ? { ...replacement, values: (replacement.values ?? []).map((v) => v.trim()).filter(Boolean) }
            : replacement,
      })),
      count: Math.max(1, Math.floor(num(count, 10))),
      interval: Math.max(1, Math.floor(num(interval, 1))),
      order,
    }),
    [name, description, sourcetype, tags, events, tokens, count, interval, order],
  );

  // Debounced live preview whenever the config changes.
  const [preview, setPreview] = useState<{ events: string[]; warnings: string[]; bpe: number } | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (events.length === 0) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      setPreviewing(true);
      try {
        const res = await api.packBuilder.preview(config, 12);
        if (cancelled) return;
        setPreview({ events: res.events, warnings: res.warnings, bpe: res.bytes_per_event });
        setHighlights(res.highlights);
        setPreviewError(null);
      } catch (err) {
        if (!cancelled) setPreviewError(apiMessage(err));
      } finally {
        if (!cancelled) setPreviewing(false);
      }
    }, 450);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [config, events.length, nonce]);

  const save = useMutation({
    mutationFn: () =>
      editing ? api.packBuilder.update(edit as number, config) : api.packBuilder.create(config),
    onSuccess: (pack) => {
      toast.success(
        pack.lint_status === "ok"
          ? `Pack "${pack.name}" ${editing ? "rebuilt" : "created"} and verified`
          : `Pack "${pack.name}" saved but failed lint: see the pack card`,
      );
      navigate({ to: "/packs" });
    },
    onError: (err) => toast.error(apiMessage(err)),
  });

  const colourOf = useMemo(() => {
    const map = new Map<string, string>();
    tokens.forEach((t, i) => map.set(t.id, PALETTE[i % PALETTE.length]));
    return (id: string) => map.get(id) ?? PALETTE[0];
  }, [tokens]);
  const labelOf = (id: string) => tokens.find((t) => t.id === id)?.field ?? id;

  const patchToken = (id: string, p: Partial<BuilderSuggestion>) =>
    setTokens((ts) => ts.map((t) => (t.id === id ? { ...t, ...p } : t)));
  const removeToken = (id: string) => setTokens((ts) => ts.filter((t) => t.id !== id));

  function addCustom() {
    const raw = custom.trim();
    if (!raw) return;
    const pattern = customIsRegex ? raw : escapeRegex(raw);
    const id = `c${Date.now()}`;
    setTokens((ts) => [
      ...ts,
      {
        id,
        field: customIsRegex ? `custom ${ts.length + 1}` : raw.slice(0, 40),
        pattern,
        enabled: true,
        replacement: { kind: "values", values: [raw] },
        kind: "custom",
        why: "added by you",
        examples: customIsRegex ? [] : [raw],
        matches: events.filter((e) => {
          try {
            return new RegExp(pattern).test(e);
          } catch {
            return false;
          }
        }).length,
      },
    ]);
    setCustom("");
  }

  // Selecting text in an event pre-fills the "add a field" box.
  function onEventsMouseUp() {
    const sel = window.getSelection?.()?.toString() ?? "";
    if (sel && sel.length <= 200 && !sel.includes("\n")) {
      setCustom(sel);
      setCustomIsRegex(false);
    }
  }

  const nameOk = /^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$/.test(name.trim());
  const canSave = events.length > 0 && nameOk && !save.isPending && !previewError;
  const enabledCount = tokens.filter((t) => t.enabled).length;

  if (editing && packQ.isPending) return <LoadingState />;
  if (editing && packQ.isError) return <ErrorState error={packQ.error} onRetry={() => packQ.refetch()} />;

  return (
    <Stack $gap="large">
      <PageHeader
        title={editing ? "Edit pack in builder" : "Build a pack from events"}
        subtitle="Paste or upload a few real events. Stoker suggests the parts to vary, you choose what to replace them with, and it saves an ordinary eventgen pack."
      />

      <Card title="1. Sample events">
        <Stack>
          <Field
            label="Events"
            hint="One event per line (an access log, syslog, key=value or JSON lines), or a JSON array of objects. A few dozen varied lines give the best suggestions."
          >
            <TextArea
              rowsMin={6}
              rowsMax={16}
              value={text}
              onChange={(_e, { value }) => setText(String(value))}
            />
          </Field>
          <Between>
            <Inline>
              <input
                type="file"
                accept=".log,.txt,.json,.csv,.ndjson,text/plain,application/json"
                onChange={(e) => onFile(e.target.files?.[0])}
                aria-label="Upload a sample file"
              />
              <Muted $small>or upload a file (up to 2 MB)</Muted>
            </Inline>
            <Button variant="primary" onClick={() => analyse.mutate()} disabled={!text.trim() || analyse.isPending}>
              {analyse.isPending ? "Analysing…" : events.length ? "Re-analyse" : "Analyse events"}
            </Button>
          </Between>
          {analyseError && <Callout $tone="error">{analyseError}</Callout>}
          {editing && events.length > 0 && !analyse.data && (
            <Muted $small>
              Re-analysing replaces the saved fields with fresh suggestions. Edit the fields below to keep them.
            </Muted>
          )}
        </Stack>
      </Card>

      {events.length > 0 && (
        <>
          <Card
            title={`2. Fields to vary (${enabledCount} of ${tokens.length} on)`}
            actions={<Muted $small>{events.length} sample event{events.length === 1 ? "" : "s"}</Muted>}
          >
            <Stack>
              <Panel onMouseUp={onEventsMouseUp}>
                <Stack $gap="small">
                  <Muted $small>
                    Highlighted parts are rewritten on every generated event. Select any other text to make it a
                    field.
                  </Muted>
                  {events.slice(0, 6).map((ev, i) => (
                    <MarkedEvent
                      key={i}
                      text={ev}
                      spans={highlights[i] ?? []}
                      colour={colourOf}
                      label={labelOf}
                    />
                  ))}
                  {events.length > 6 && <Muted $small>… and {events.length - 6} more</Muted>}
                </Stack>
              </Panel>

              {tokens.length === 0 && (
                <Muted>No fields detected. Add one below: select text in an event or type a value.</Muted>
              )}

              {tokens.map((t) => (
                <Panel key={t.id} style={{ borderLeft: `6px solid ${colourOf(t.id)}` }}>
                  <Stack $gap="small">
                    <Between>
                      <Inline>
                        <Switch
                          appearance="checkbox"
                          selected={t.enabled}
                          onClick={() => patchToken(t.id, { enabled: !t.enabled })}
                        >
                          <Strong>{t.field}</Strong>
                        </Switch>
                        {t.why && <Badge tone="slate">{t.why}</Badge>}
                        {t.kind !== "saved" && (
                          <Muted $small>
                            matches {t.matches} of {events.length}
                          </Muted>
                        )}
                      </Inline>
                      <Button variant="ghost" onClick={() => removeToken(t.id)}>
                        Remove
                      </Button>
                    </Between>
                    {t.examples.length > 0 && (
                      <Muted $small>
                        seen: <Mono>{t.examples.slice(0, 4).join("  ·  ")}</Mono>
                      </Muted>
                    )}
                    {t.enabled && (
                      <>
                        <ReplacementEditor
                          value={t.replacement}
                          examples={t.examples}
                          lists={lists}
                          onChange={(r) => patchToken(t.id, { replacement: r })}
                        />
                        <Field
                          label="Pattern"
                          hint="Regular expression; the first capture group is replaced, otherwise the whole match"
                        >
                          <TextInput value={t.pattern} onChange={(_e, { value }) => patchToken(t.id, { pattern: value })} />
                        </Field>
                      </>
                    )}
                  </Stack>
                </Panel>
              ))}

              <Panel>
                <Grid $min="220px">
                  <Field label="Add a field" hint="text to replace (select it in an event) or a regex">
                    <TextInput value={custom} onChange={(_e, { value }) => setCustom(value)} placeholder="e.g. checkout-svc" />
                  </Field>
                  <Field label=" ">
                    <Inline>
                      <Switch appearance="checkbox" selected={customIsRegex} onClick={() => setCustomIsRegex(!customIsRegex)}>
                        It is a regex
                      </Switch>
                      <Button variant="secondary" onClick={addCustom} disabled={!custom.trim()}>
                        Add field
                      </Button>
                    </Inline>
                  </Field>
                </Grid>
              </Panel>
            </Stack>
          </Card>

          <Card title="3. Pack details">
            <Grid $min="220px">
              <Field label="Name" error={name && !nameOk ? "letters, digits, spaces, . _ - (max 64)" : undefined}>
                <TextInput value={name} onChange={(_e, { value }) => setName(value)} placeholder="checkout-api" />
              </Field>
              <Field label="Sourcetype" hint="default sourcetype for the pack (a run can override)">
                <TextInput value={sourcetype} onChange={(_e, { value }) => setSourcetype(value)} placeholder="access_combined" />
              </Field>
              <Field label="Tags" hint="comma separated">
                <TextInput value={tags} onChange={(_e, { value }) => setTags(value)} placeholder="web, demo" />
              </Field>
              <Field label="Events per interval" hint="a run's rate setting replaces this">
                <TextInput value={count} onChange={(_e, { value }) => setCount(value)} />
              </Field>
              <Field label="Interval (seconds)">
                <TextInput value={interval} onChange={(_e, { value }) => setInterval(value)} />
              </Field>
              <Field label="Event order">
                <Select value={order} onChange={(_e, { value }) => setOrder(value as "sequential" | "random")}>
                  <Select.Option value="sequential" label="In sample order" />
                  <Select.Option value="random" label="Random sample lines" />
                </Select>
              </Field>
            </Grid>
            <Field label="Description">
              <TextInput value={description} onChange={(_e, { value }) => setDescription(value)} />
            </Field>
          </Card>

          <Card
            title="4. Preview"
            actions={
              <Button variant="secondary" onClick={() => setNonce((n) => n + 1)} disabled={previewing}>
                {previewing ? "Generating…" : "Regenerate"}
              </Button>
            }
          >
            <Stack $gap="small">
              {previewError && <Callout $tone="error">{previewError}</Callout>}
              {preview?.warnings.length ? (
                <Callout $tone="warning">
                  <Bullets>
                    {preview.warnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </Bullets>
                </Callout>
              ) : null}
              {preview ? (
                <>
                  {preview.events.map((ev, i) => (
                    <Mono key={i} $break style={{ display: "block", whiteSpace: "pre-wrap" }}>
                      {ev}
                    </Mono>
                  ))}
                  <Muted $small>about {Math.round(preview.bpe)} bytes per event</Muted>
                </>
              ) : (
                <Muted>{previewing ? "Generating…" : "The preview appears once the events are analysed."}</Muted>
              )}
            </Stack>
          </Card>
        </>
      )}

      <StickyBar>
        <Muted $small>
          {events.length ? (
            <>
              <Strong>{enabledCount}</Strong> field{enabledCount === 1 ? "" : "s"} · <Strong>{events.length}</Strong>{" "}
              sample event{events.length === 1 ? "" : "s"}
              {!nameOk && <Warn> · give the pack a name</Warn>}
            </>
          ) : (
            "Analyse some events to start"
          )}
        </Muted>
        <Button variant="primary" onClick={() => save.mutate()} disabled={!canSave}>
          {save.isPending ? "Saving…" : editing ? "Rebuild pack" : "Create pack"}
        </Button>
      </StickyBar>
    </Stack>
  );
}

export const Route = createFileRoute("/pack-builder")({
  validateSearch: (search: Record<string, unknown>): BuilderSearch => {
    const raw = search.edit;
    const n = typeof raw === "number" ? raw : Number(raw);
    return Number.isFinite(n) && n > 0 ? { edit: n } : {};
  },
  component: PackBuilder,
});
