import { memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import TextArea from "@splunk/react-ui/TextArea";
import Switch from "@splunk/react-ui/Switch";

import { api, ApiError } from "../lib/api";
import type {
  BuilderAnalyseResponse,
  BuilderBreakMode,
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

// A pack this wide keeps its field editors closed until one is opened, so a
// keystroke is not spent laying out hundreds of forms nobody is looking at.
const OPEN_ALL_AT_MOST = 30;
// Field rows rendered before "show more"; the filter finds the rest.
const FIELD_PAGE = 200;
// Above this many enabled fields a preview costs seconds of server time, so it
// runs when asked rather than after every edit.
const LIVE_PREVIEW_MAX_FIELDS = 300;

const SIMPLE_KINDS: Array<[BuilderReplacementKind, string]> = [
  ["pseudonym", "Consistent pseudonym (keyed hash)"],
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
  if (r.kind === "list") return `list:${r.list}`;
  if (r.kind === "linked") return `linked:${r.table}:${r.column}`;
  return r.kind;
}

function defaultReplacement(key: string, examples: string[]): BuilderReplacement {
  if (key.startsWith("list:")) return { kind: "list", list: key.slice(5) };
  if (key.startsWith("linked:")) {
    const [, table, column] = key.split(":");
    return { kind: "linked", table, column };
  }
  switch (key as BuilderReplacementKind) {
    case "pseudonym":
      return { kind: "pseudonym", widen: null, rotate: false, rewrite_residuals: false };
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

/** One line for a closed field: what it is replaced with. */
function describeReplacement(r: BuilderReplacement, lists: WordlistInfo[]): string {
  if (r.kind === "list") return `Word list: ${lists.find((l) => l.name === r.list)?.title ?? r.list}`;
  if (r.kind === "linked") return `Linked: ${r.table} → ${r.column}`;
  const label = SIMPLE_KINDS.find(([k]) => k === r.kind)?.[1] ?? r.kind;
  if (r.kind !== "pseudonym") return label;
  const extra = [
    r.widen?.shape === "guid" ? "a GUID" : r.widen ? `widened to ${r.widen.shape}` : null,
    r.rotate ? "new each replay" : null,
    r.rewrite_residuals ? "everywhere in the event" : null,
  ].filter(Boolean);
  return extra.length ? `Consistent pseudonym: ${extra.join(", ")}` : label;
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
const MarkedEvent = memo(function MarkedEvent({
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
});

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
  const plain = lists.filter((l) => l.kind !== "table");
  const tables = lists.filter((l) => l.kind === "table");
  const table = value.kind === "linked" ? tables.find((l) => l.name === value.table) : undefined;
  return (
    <Grid $min="180px">
      <Field label="Replace with">
        <Select
          value={key}
          filter
          onChange={(_e, { value: v }) => onChange(defaultReplacement(String(v), examples))}
        >
          {plain.map((l) => (
            <Select.Option key={l.name} value={`list:${l.name}`} label={`Word list: ${l.title}`} />
          ))}
          {tables.flatMap((t) =>
            (t.columns ?? []).map((c) => (
              <Select.Option key={`${t.name}:${c}`} value={`linked:${t.name}:${c}`} label={`Linked: ${t.title} → ${c}`} />
            )),
          )}
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
      {value.kind === "linked" && (
        <Field label="Linked row">
          <Muted $small>
            Every field linked to {table?.title ?? value.table} takes the same row within an event, so they agree
            {table?.sample[0] ? ` (e.g. ${table.sample[0]})` : ""}.
          </Muted>
        </Field>
      )}
      {value.kind === "pseudonym" && (
        <>
          <Field
            label="Stand-in"
            hint="the same value always gets the same stand-in, so correlation survives; the original is not in the pack"
          >
            <Select
              value={value.widen ? `${value.widen.shape}` : "same"}
              onChange={(_e, { value: v }) =>
                onChange(
                  String(v) === "same"
                    ? { ...value, widen: null }
                    : {
                        ...value,
                        widen:
                          String(v) === "guid"
                            ? { shape: "guid" }
                            : {
                                shape: String(v) as "digits" | "hex",
                                length: String(v) === "digits" ? 15 : 16,
                              },
                      },
                )
              }
            >
              <Select.Option value="same" label="Keep the same format and length" />
              <Select.Option value="digits" label="Widen to fixed-length digits" />
              <Select.Option value="hex" label="Widen to fixed-length hex" />
              <Select.Option value="guid" label="Replace with a GUID" />
            </Select>
          </Field>
          {value.widen && value.widen.shape !== "guid" && (
            <Field
              label="Length"
              hint={value.widen.shape === "digits" ? "12 to 48" : "16 to 48"}
            >
              <TextInput
                value={String(value.widen.length ?? (value.widen.shape === "digits" ? 15 : 16))}
                onChange={(_e, { value: v }) =>
                  onChange({
                    ...value,
                    widen: {
                      shape: value.widen!.shape,
                      length: num(v, value.widen!.shape === "digits" ? 15 : 16),
                    },
                  })
                }
              />
            </Field>
          )}
          <Field label="Each replay">
            <Switch
              appearance="checkbox"
              selected={value.rotate === true}
              onClick={() => onChange({ ...value, rotate: !value.rotate })}
            >
              Give every replay of the sample a new identity
            </Switch>
          </Field>
          <Field label="Elsewhere in the event">
            <Switch
              appearance="checkbox"
              selected={value.rewrite_residuals === true}
              onClick={() =>
                onChange({ ...value, rewrite_residuals: !value.rewrite_residuals })
              }
            >
              Replace this value everywhere it appears
            </Switch>
          </Field>
          <Field label=" ">
            <Muted $small>
              Keeping the format means the stand-in has the same shape and width, so
              field extractions keep working, but the space is small enough that two
              values can merge. Widening removes that at the cost of the shape. Turn
              on "replace everywhere" when the same value also appears in a URL or a
              message, which is otherwise left in the pack and blocks the save. Without
              "new identity each replay" one value keeps one stand-in for ever, so a
              three-event journey replays as the same user logging in thousands of
              times; with it, each pass over the sample is a new user whose events still
              belong together.
            </Muted>
          </Field>
        </>
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

/**
 * One field. Memoised, with stable callbacks from the page, so editing one
 * field re-renders that field rather than every field in a wide pack.
 */
const FieldPanel = memo(function FieldPanel({
  token: t,
  colour,
  eventCount,
  lists,
  open,
  onPatch,
  onRemove,
  onToggle,
}: {
  token: BuilderSuggestion;
  colour: string;
  eventCount: number;
  lists: WordlistInfo[];
  open: boolean | null; // null: always open, so no toggle
  onPatch: (id: string, p: Partial<BuilderSuggestion>) => void;
  onRemove: (id: string) => void;
  onToggle: (id: string) => void;
}) {
  const expanded = open !== false;
  return (
    <Panel style={{ borderLeft: `6px solid ${colour}` }}>
      <Stack $gap="small">
        <Between>
          <Inline>
            <Switch
              appearance="checkbox"
              selected={t.enabled}
              onClick={() => onPatch(t.id, { enabled: !t.enabled })}
            >
              <Strong>{t.field}</Strong>
            </Switch>
            {t.why && <Badge tone="slate">{t.why}</Badge>}
            {t.replacement.kind === "linked" && <Badge tone="sky">linked</Badge>}
            {t.kind !== "saved" && (
              <Muted $small>
                matches {t.matches} of {eventCount}
              </Muted>
            )}
            {!expanded && t.enabled && <Muted $small>{describeReplacement(t.replacement, lists)}</Muted>}
          </Inline>
          <Inline>
            {open !== null && t.enabled && (
              <Button variant="ghost" onClick={() => onToggle(t.id)}>
                {expanded ? "Close" : "Edit"}
              </Button>
            )}
            <Button variant="ghost" onClick={() => onRemove(t.id)}>
              Remove
            </Button>
          </Inline>
        </Between>
        {expanded && t.examples.length > 0 && (
          <Muted $small>
            seen: <Mono>{t.examples.slice(0, 4).join("  ·  ")}</Mono>
          </Muted>
        )}
        {expanded && t.enabled && (
          <>
            <ReplacementEditor
              value={t.replacement}
              examples={t.examples}
              lists={lists}
              onChange={(r) => onPatch(t.id, { replacement: r })}
            />
            <Field
              label="Pattern"
              hint="Regular expression; the first capture group is replaced, otherwise the whole match"
            >
              <TextInput value={t.pattern} onChange={(_e, { value }) => onPatch(t.id, { pattern: value })} />
            </Field>
          </>
        )}
      </Stack>
    </Panel>
  );
});

/** Operator word lists: saved on the control plane and reusable in any pack. */
function CustomLists({ lists }: { lists: WordlistInfo[] }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [listName, setListName] = useState("");
  const [title, setTitle] = useState("");
  const [columns, setColumns] = useState("");
  const [values, setValues] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["pack-builder", "wordlists"] });
  const saveList = useMutation({
    mutationFn: () =>
      api.packBuilder.saveWordlist({
        name: listName.trim(),
        title: title.trim(),
        values: values.split("\n"),
        columns: columns.trim()
          ? columns
              .split(",")
              .map((c) => c.trim())
              .filter(Boolean)
          : null,
      }),
    onSuccess: (w) => {
      toast.success(`Saved ${w.kind === "table" ? "table" : "list"} "${w.title}" (${w.count})`);
      setListName("");
      setTitle("");
      setColumns("");
      setValues("");
      refresh();
    },
    onError: (err) => toast.error(apiMessage(err)),
  });
  const deleteList = useMutation({
    mutationFn: (n: string) => api.packBuilder.deleteWordlist(n),
    onSuccess: () => refresh(),
    onError: (err) => toast.error(apiMessage(err)),
  });
  const mine = lists.filter((l) => l.custom);
  return (
    <Panel>
      <Stack $gap="small">
        <Between>
          <Muted $small>
            Your word lists ({mine.length}): saved on the server and offered in every pack. A pack keeps its own copy
            of the values it uses.
          </Muted>
          <Button variant="ghost" onClick={() => setOpen(!open)}>
            {open ? "Close" : "Manage lists"}
          </Button>
        </Between>
        {open && (
          <>
            {mine.map((l) => (
              <Between key={l.name}>
                <Muted $small>
                  <Strong>{l.title}</Strong> <Mono>{l.name}</Mono> · {l.count} {l.kind === "table" ? "rows" : "values"}
                  {l.columns ? ` · columns ${l.columns.join(", ")}` : ""}
                </Muted>
                <Button variant="ghost" onClick={() => deleteList.mutate(l.name)} disabled={deleteList.isPending}>
                  Delete
                </Button>
              </Between>
            ))}
            <Grid $min="200px">
              <Field label="Name" hint="lower-case letters, digits and _">
                <TextInput value={listName} onChange={(_e, { value }) => setListName(value)} placeholder="store_ids" />
              </Field>
              <Field label="Title">
                <TextInput value={title} onChange={(_e, { value }) => setTitle(value)} placeholder="Store IDs" />
              </Field>
              <Field label="Columns" hint="optional: comma separated, makes a table for linked fields">
                <TextInput value={columns} onChange={(_e, { value }) => setColumns(value)} placeholder="id, city" />
              </Field>
            </Grid>
            <Field
              label="Values"
              hint="one per line; for a table, one comma-separated row per line (no quotes); repeat a line to weight it"
            >
              <TextArea rowsMin={3} rowsMax={10} value={values} onChange={(_e, { value }) => setValues(String(value))} />
            </Field>
            <Inline>
              <Button
                variant="secondary"
                onClick={() => saveList.mutate()}
                disabled={!listName.trim() || !values.trim() || saveList.isPending}
              >
                {saveList.isPending ? "Saving…" : "Save list"}
              </Button>
            </Inline>
          </>
        )}
      </Stack>
    </Panel>
  );
}

const BREAK_MODES: Array<[BuilderBreakMode, string]> = [
  ["auto", "Detect automatically"],
  ["line", "One event per line"],
  ["csv", "CSV with a header row"],
  ["regex", "Multi-line, split by a regex"],
];

function describeSplit(res: Pick<BuilderAnalyseResponse, "format" | "breaker" | "header">): string {
  switch (res.format) {
    case "multiline":
      return `Multi-line events: each starts where the breaker matches (${res.breaker}).`;
    case "csv":
      return `CSV with ${res.header?.length ?? 0} columns (${(res.header ?? []).join(", ")}); the header row is not an event.`;
    case "splunk_csv":
      return res.breaker
        ? `Splunk export: the _raw column, multi-line events split by ${res.breaker}.`
        : "Splunk export: the _raw column, one event per row.";
    case "json":
      return "JSON: one compact event per object.";
    default:
      return "One event per line.";
  }
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
  // -1 is eventgen's "the whole sample every interval" and the default for a new
  // pack: a positive count below the sample size emits only that prefix for
  // ever. The switch and the number are separate so toggling back restores the
  // operator's last figure.
  const [wholeSample, setWholeSample] = useState(true);
  const [count, setCount] = useState("10");
  const [interval, setInterval] = useState("1");
  const [order, setOrder] = useState<"sequential" | "random">("sequential");
  // Aligned rotation: off by default, because the pass counter gives far more
  // identities and nothing needs the join until a correlation search spans
  // sourcetypes. Switching it on is cheap; switching it on later is not,
  // because the identities move.
  const [rotateAligned, setRotateAligned] = useState(false);
  const [rotatePeriod, setRotatePeriod] = useState("60");
  const [custom, setCustom] = useState("");
  const [customIsRegex, setCustomIsRegex] = useState(false);
  const [analyseError, setAnalyseError] = useState<string | null>(null);
  const [mode, setMode] = useState<BuilderBreakMode>("auto");
  const [breakerInput, setBreakerInput] = useState("");
  const [breaker, setBreaker] = useState<string | null>(null);
  const [splitNote, setSplitNote] = useState<string | null>(null);
  // The events as the reopened pack stores them: already pseudonymised, so the
  // preview must not hash their stand-ins again. Cleared on a re-analyse,
  // which replaces the sample wholesale.
  const [builtEvents, setBuiltEvents] = useState<string[]>([]);

  const listsQ = useQuery({ queryKey: ["pack-builder", "wordlists"], queryFn: api.packBuilder.wordlists });
  const keyQ = useQuery({ queryKey: ["pack-builder", "pseudonym-key"], queryFn: api.packBuilder.pseudonymKey });
  // Stable while loading too: a fresh [] each render would re-render every field.
  const lists = useMemo(() => listsQ.data ?? [], [listsQ.data]);

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
    setBuiltEvents(c.events);
    setText(c.events.join("\n"));
    setBreaker(c.breaker ?? null);
    if (c.breaker) {
      setMode("regex");
      setBreakerInput(c.breaker);
      setSplitNote(`Multi-line events: each starts where the breaker matches (${c.breaker}).`);
    }
    setTokens(c.tokens.map((t) => ({ ...t, kind: "saved", why: "", examples: [], matches: 0 })));
    setName(c.name);
    setDescription(c.description ?? "");
    setSourcetype(c.sourcetype ?? "");
    setTags((c.tags ?? []).join(", "));
    setWholeSample(c.count < 0);
    if (c.count > 0) setCount(String(c.count));
    setInterval(String(c.interval));
    setOrder(c.order ?? "sequential");
    setRotateAligned((c.rotation?.scope ?? "pass") === "window");
    setRotatePeriod(String(c.rotation?.period ?? 60));
    hydrated.current = true;
  }, [editing, packQ.data]);

  const analyse = useMutation({
    mutationFn: () => api.packBuilder.analyse(text, mode, mode === "regex" ? breakerInput : null),
    onSuccess: (res) => {
      setAnalyseError(null);
      setBuiltEvents([]);
      setEvents(res.event_list);
      setBreaker(res.breaker ?? null);
      setSplitNote(describeSplit(res));
      if (res.breaker && mode !== "regex") setBreakerInput(res.breaker);
      setTokens(res.suggestions);
      setHighlights(res.highlights);
      if (!name) setName("");
    },
    onError: (err) => setAnalyseError(apiMessage(err)),
  });

  // Several files are joined in the order selected, because a customer's
  // samples usually arrive as one file per host or per day while the builder's
  // input is a single blob. The guard is on the COMBINED size so it matches the
  // server's MAX_TOTAL_BYTES and cannot pass here only to fail on analyse.
  async function onFiles(files: FileList | null) {
    const list = Array.from(files ?? []);
    if (list.length === 0) return;
    const total = list.reduce((n, f) => n + f.size, 0);
    if (total > 2 * 1024 * 1024) {
      toast.error(
        list.length === 1
          ? "That file is over 2 MB; trim it to a representative sample."
          : `Those ${list.length} files total ${Math.round(total / 1024)} KB, over the 2 MB limit; select fewer or trim them.`,
      );
      return;
    }
    const parts = await Promise.all(list.map((f) => f.text()));
    setText(parts.map((t) => t.replace(/\s+$/, "")).join("\n"));
    if (!name) {
      const base = list[0].name.replace(/\.[^.]+$/, "");
      setName(base.replace(/[^A-Za-z0-9 _.-]/g, "-").slice(0, 64));
    }
    if (list.length > 1) {
      toast.success(`Loaded ${list.length} files (${Math.round(total / 1024)} KB)`);
    }
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
      breaker,
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
      count: wholeSample ? -1 : Math.max(1, Math.floor(num(count, 10))),
      interval: Math.max(1, Math.floor(num(interval, 1))),
      order,
      rotation: tokens.some((t) => t.enabled && t.replacement.kind === "pseudonym" && t.replacement.rotate)
        ? rotateAligned
          ? { scope: "window" as const, period: Math.max(1, Math.floor(num(rotatePeriod, 60))) }
          : { scope: "pass" as const }
        : null,
    }),
    [
      name, description, sourcetype, tags, events, breaker, tokens, wholeSample, count,
      interval, order, rotateAligned, rotatePeriod,
    ],
  );

  // Live preview, debounced. One request at a time: an edit made while one is
  // running queues a single rerun with the newest config, rather than stacking
  // requests that each take seconds on a wide pack and finish out of order.
  const [preview, setPreview] = useState<{
    events: string[];
    warnings: string[];
    blocking: string[];
    bpe: number;
  } | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewed, setPreviewed] = useState<BuilderConfig | null>(null);
  const enabledCount = tokens.filter((t) => t.enabled).length;
  // null follows the field count; the switch on the Preview card overrides it.
  const [livePref, setLivePref] = useState<boolean | null>(null);
  const live = livePref ?? enabledCount <= LIVE_PREVIEW_MAX_FIELDS;
  const latest = useRef({ config, builtEvents });
  latest.current = { config, builtEvents };
  const running = useRef(false);
  const again = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const runPreview = useCallback(async () => {
    if (running.current) {
      again.current = true;
      return;
    }
    running.current = true;
    setPreviewing(true);
    try {
      do {
        again.current = false;
        const { config: cfg, builtEvents: built } = latest.current;
        try {
          const res = await api.packBuilder.preview(cfg, 12, undefined, built);
          if (!alive.current) return;
          setPreview({
            events: res.events,
            warnings: res.warnings,
            blocking: res.blocking ?? [],
            bpe: res.bytes_per_event,
          });
          setHighlights(res.highlights);
          setPreviewError(null);
        } catch (err) {
          if (!alive.current) return;
          setPreviewError(apiMessage(err));
        }
        setPreviewed(cfg);
      } while (again.current);
    } finally {
      running.current = false;
      if (alive.current) setPreviewing(false);
    }
  }, []);
  // Without live preview a new sample still gets one preview by itself.
  const previewedEvents = useRef<string[] | null>(null);
  useEffect(() => {
    if (events.length === 0) return;
    if (!live && previewedEvents.current === events) return;
    const timer = window.setTimeout(() => {
      previewedEvents.current = events;
      runPreview();
    }, 450);
    return () => window.clearTimeout(timer);
  }, [config, events, builtEvents, live, runPreview]);
  const previewStale = !live && preview !== null && previewed !== config;

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

  // Keyed on the fields' ids and names only, which an edit to a field does not
  // change, so the marked events are not redrawn on every keystroke.
  const fieldKey = tokens.map((t) => `${t.id}\u0000${t.field}`).join("\u0001");
  const { colourOf, labelOf } = useMemo(() => {
    const colours = new Map<string, string>();
    const labels = new Map<string, string>();
    tokens.forEach((t, i) => {
      colours.set(t.id, PALETTE[i % PALETTE.length]);
      labels.set(t.id, t.field);
    });
    return {
      colourOf: (id: string) => colours.get(id) ?? PALETTE[0],
      labelOf: (id: string) => labels.get(id) ?? id,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fieldKey]);

  const patchToken = useCallback(
    (id: string, p: Partial<BuilderSuggestion>) =>
      setTokens((ts) => ts.map((t) => (t.id === id ? { ...t, ...p } : t))),
    [],
  );
  const removeToken = useCallback((id: string) => setTokens((ts) => ts.filter((t) => t.id !== id)), []);

  const [openIds, setOpenIds] = useState<Set<string>>(() => new Set());
  const toggleOpen = useCallback(
    (id: string) =>
      setOpenIds((s) => {
        const next = new Set(s);
        if (!next.delete(id)) next.add(id);
        return next;
      }),
    [],
  );
  const openAll = tokens.length <= OPEN_ALL_AT_MOST;
  const [fieldFilter, setFieldFilter] = useState("");
  const [kindFilter, setKindFilter] = useState("any");
  const [fieldLimit, setFieldLimit] = useState(FIELD_PAGE);
  const shownTokens = useMemo(() => {
    const needle = fieldFilter.trim().toLowerCase();
    return tokens.filter(
      (t) =>
        (!needle || t.field.toLowerCase().includes(needle)) &&
        (kindFilter === "any" ||
          (kindFilter === "on" && t.enabled) ||
          (kindFilter === "off" && !t.enabled) ||
          (t.enabled && t.replacement.kind === kindFilter)),
    );
  }, [tokens, fieldFilter, kindFilter]);

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
  const blocked = (preview?.blocking.length ?? 0) > 0;
  const canSave = events.length > 0 && nameOk && !save.isPending && !previewError && !blocked;

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
            hint="One event per line (an access log, syslog, key=value or JSON lines), a JSON array, a CSV with a header row, or multi-line events such as stack traces or XML. A few dozen varied events give the best suggestions."
          >
            <TextArea
              rowsMin={6}
              rowsMax={16}
              value={text}
              onChange={(_e, { value }) => setText(String(value))}
            />
          </Field>
          <Grid $min="220px">
            <Field label="Event breaking">
              <Select value={mode} onChange={(_e, { value }) => setMode(value as BuilderBreakMode)}>
                {BREAK_MODES.map(([m, label]) => (
                  <Select.Option key={m} value={m} label={label} />
                ))}
              </Select>
            </Field>
            {mode === "regex" && (
              <Field label="Breaker" hint="regex matching the start of each event, applied per line (e.g. ^\d{4}-\d{2}-\d{2})">
                <TextInput value={breakerInput} onChange={(_e, { value }) => setBreakerInput(value)} placeholder="^\d{4}-\d{2}-\d{2}" />
              </Field>
            )}
          </Grid>
          <Between>
            <Inline>
              <input
                type="file"
                multiple
                accept=".log,.txt,.json,.csv,.ndjson,text/plain,application/json"
                onChange={(e) => onFiles(e.target.files)}
                aria-label="Upload one or more sample files"
              />
              <Muted $small>or upload one or more files (2 MB total)</Muted>
            </Inline>
            <Button variant="primary" onClick={() => analyse.mutate()} disabled={!text.trim() || analyse.isPending}>
              {analyse.isPending ? "Analysing…" : events.length ? "Re-analyse" : "Analyse events"}
            </Button>
          </Between>
          {analyseError && <Callout $tone="error">{analyseError}</Callout>}
          {splitNote && !analyseError && events.length > 0 && <Muted $small>{splitNote}</Muted>}
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

              {!openAll && (
                <Grid $min="220px">
                  <Field label="Find a field" hint={`${shownTokens.length} of ${tokens.length} shown`}>
                    <TextInput
                      value={fieldFilter}
                      onChange={(_e, { value }) => {
                        setFieldFilter(value);
                        setFieldLimit(FIELD_PAGE);
                      }}
                      placeholder="part of the field name"
                    />
                  </Field>
                  <Field label="Show">
                    <Select
                      value={kindFilter}
                      onChange={(_e, { value }) => {
                        setKindFilter(String(value));
                        setFieldLimit(FIELD_PAGE);
                      }}
                    >
                      <Select.Option value="any" label="All fields" />
                      <Select.Option value="on" label="Fields that are on" />
                      <Select.Option value="off" label="Fields that are off" />
                      <Select.Option value="list" label="Replaced from a word list" />
                      <Select.Option value="linked" label="Linked to a table" />
                      {SIMPLE_KINDS.map(([k, label]) => (
                        <Select.Option key={k} value={k} label={`Replaced with: ${label}`} />
                      ))}
                    </Select>
                  </Field>
                </Grid>
              )}

              {shownTokens.slice(0, fieldLimit).map((t) => (
                <FieldPanel
                  key={t.id}
                  token={t}
                  colour={colourOf(t.id)}
                  eventCount={events.length}
                  lists={lists}
                  open={openAll ? null : openIds.has(t.id)}
                  onPatch={patchToken}
                  onRemove={removeToken}
                  onToggle={toggleOpen}
                />
              ))}
              {shownTokens.length > fieldLimit && (
                <Between>
                  <Muted $small>
                    Showing {fieldLimit} of {shownTokens.length}. Find a field by name to go straight to it.
                  </Muted>
                  <Button variant="secondary" onClick={() => setFieldLimit((n) => n + FIELD_PAGE)}>
                    Show {Math.min(FIELD_PAGE, shownTokens.length - fieldLimit)} more
                  </Button>
                </Between>
              )}

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
              {tokens.some((t) => t.enabled && t.replacement.kind === "pseudonym") && (
                <Callout $tone="info">
                  <Strong>Consistent pseudonyms.</Strong> The marked fields are rewritten
                  when the pack is saved, so this instance's key decides the stand-ins and
                  the originals are not stored in the pack. Packs built here share the key
                  {keyQ.data?.fingerprint ? ` (${keyQ.data.fingerprint})` : ""}, so the same
                  value correlates across all of them. Everything you upload that is NOT
                  marked is stored in the pack as you supplied it, so remove anything else
                  sensitive before uploading. This is pseudonymisation, not anonymisation:
                  whoever holds the control plane can rebuild the mapping.
                </Callout>
              )}
              {tokens.some(
                (t) => t.enabled && t.replacement.kind === "pseudonym" && t.replacement.rotate,
              ) && (
                <Panel>
                  <Stack $gap="small">
                    <Field label="Rotation">
                      <Switch
                        appearance="checkbox"
                        selected={rotateAligned}
                        onClick={() => setRotateAligned(!rotateAligned)}
                      >
                        Line the identities up across sourcetypes, packs and runs
                      </Switch>
                    </Field>
                    {rotateAligned && (
                      <Field
                        label="Window (seconds)"
                        hint="one identity per value per window; shorter gives more identities"
                      >
                        <TextInput
                          value={rotatePeriod}
                          onChange={(_e, { value }) => setRotatePeriod(value)}
                        />
                      </Field>
                    )}
                    <Muted $small>
                      {rotateAligned
                        ? "The identity comes from the value and the clock, so the same client" +
                          " id appears in every sourcetype, pack and run of the same window," +
                          " which is what a correlation search needs to join them. The cost is" +
                          " that the number of distinct identities is set by the window length" +
                          " rather than by the volume, and that two values can merge into one" +
                          " identity (the same risk as keeping the format, answered by the same" +
                          " widen setting)."
                        : "Each replay of the sample gets its own identities, which gives the" +
                          " most of them and cannot merge two values. They will not line up with" +
                          " another sourcetype or another run, because each pack counts its own" +
                          " replays. Turn this on if a correlation search has to join this field" +
                          " across sources: doing it now costs nothing, doing it later moves" +
                          " every identity."}
                    </Muted>
                    <Muted $small>
                      Rotation needs firebox on the workers. A worker running the fallback
                      Python engine fails the run rather than quietly replaying one identity.
                      The worker count is part of the identity, so a rotating run cannot be
                      scaled unless the identities are aligned.
                    </Muted>
                  </Stack>
                </Panel>
              )}
              <CustomLists lists={lists} />
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
              <Field
                label="Events per interval"
                hint="eps and GB/day runs replace this; a count-per-interval run keeps it and splits it across workers"
              >
                <Stack $gap="small">
                  <Switch
                    appearance="checkbox"
                    selected={wholeSample}
                    onClick={() => setWholeSample(!wholeSample)}
                  >
                    Whole sample every interval
                  </Switch>
                  {!wholeSample && (
                    <TextInput value={count} onChange={(_e, { value }) => setCount(value)} />
                  )}
                </Stack>
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
              <Inline>
                <Switch appearance="toggle" selected={live} onClick={() => setLivePref(!live)}>
                  Live
                </Switch>
                <Button variant="secondary" onClick={() => runPreview()} disabled={previewing}>
                  {previewing ? "Generating…" : live ? "Regenerate" : "Refresh preview"}
                </Button>
              </Inline>
            }
          >
            <Stack $gap="small">
              {!live && livePref === null && (
                <Muted $small>
                  With {enabledCount} fields on, each preview takes a while, so it updates when you press Refresh
                  preview rather than after every change. Turn Live on to change that.
                </Muted>
              )}
              {previewStale && (
                <Callout $tone="info">
                  The preview is from before your latest changes. Press Refresh preview to update it.
                </Callout>
              )}
              {previewError && <Callout $tone="error">{previewError}</Callout>}
              {preview?.blocking.length ? (
                <Callout $tone="error">
                  <Stack $gap="small">
                    <Strong>Fix these before saving</Strong>
                    <Bullets>
                      {preview.blocking.map((w) => (
                        <li key={w}>{w}</li>
                      ))}
                    </Bullets>
                  </Stack>
                </Callout>
              ) : null}
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
              {blocked && <Warn> · fix the problems shown in Preview</Warn>}
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
