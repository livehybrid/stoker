// Pack picker panel for the job wizard: a selectable list of indexed packs with
// verified / replay / trusted-code badges and a parsed-stanza + sample preview
// for the selected pack (GET /packs/{id}/preview).

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Badge, StatusBadge } from "../../components/Badge";
import { TextInput } from "../../components/Field";
import { LoadingState, ErrorState } from "../../components/States";
import { packLooksReplay, packLooksTrusted } from "./replay";
import { packIsMetrics } from "../metrics/config";
import { Between, Bullets, Grid, Inline, Label, Mono, Muted, Panel, Pre, ScrollList, Stack, Strong, Tags } from "../../components/text";
import styled from "styled-components";
import { variables } from "@splunk/themes";
import { Button } from "../../components/Button";

const Selectable = styled.div<{ $selected?: boolean; $disabled?: boolean }>`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: ${variables.spacingMedium};
  border: 1px solid
    ${(props) => (props.$selected ? variables.contentColorAccent : variables.borderColor)};
  border-radius: ${variables.borderRadius};
  background-color: ${(props) =>
    props.$selected ? variables.interactiveColorOverlaySelected : "transparent"};
  opacity: ${(props) => (props.$disabled ? 0.5 : 1)};

  &:hover {
    border-color: ${(props) =>
      props.$disabled ? variables.borderColor : variables.interactiveColorBorderHover};
  }
`;

function PackBadges({ pack }: { pack: PackOut }) {
  return (
    <Tags>
      {pack.verified ? (
        <Badge tone="green">verified</Badge>
      ) : (
        <Badge tone="slate">unverified</Badge>
      )}
      {pack.lint_status !== "ok" && <StatusBadge state={pack.lint_status} />}
      {packLooksReplay(pack) && <Badge tone="amber">replay</Badge>}
      {packLooksTrusted(pack) && <Badge tone="sky">trusted code</Badge>}
    </Tags>
  );
}

function PackPreview({ packId }: { packId: number }) {
  const q = useQuery({
    queryKey: ["pack-preview", packId],
    queryFn: () => api.packs.preview(packId),
    staleTime: 30_000,
  });
  if (q.isPending) return <LoadingState label="Loading preview…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;

  const { stanzas, sample_lines, lint_status, lint_errors } = q.data;
  return (
    <Stack>
      <Inline>
        <Muted>Lint:</Muted>
        <StatusBadge state={lint_status} />
      </Inline>
      {lint_errors.length > 0 && (
        <Bullets>
          {lint_errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </Bullets>
      )}
      <div>
        <Label>
          Stanzas ({stanzas.length})
        </Label>
        {stanzas.length === 0 ? (
          <Muted $small>No stanzas parsed.</Muted>
        ) : (
          <Stack>
            {stanzas.map((s) => {
              const lines = sample_lines[s] ?? [];
              return (
                <div
                  key={s}
                >
                  <Mono>
                    [{s}]
                  </Mono>
                  {lines.length > 0 ? (
                    <Pre>
                      {lines.join("\n")}
                    </Pre>
                  ) : (
                    <Muted $small>
                      No sample lines.
                    </Muted>
                  )}
                </div>
              );
            })}
          </Stack>
        )}
      </div>
    </Stack>
  );
}

// A pack that can join a multi-pack merge: eventgen only (replay is
// single-worker/engine-paced; metrics packs are built from a builder config).
// Mirrors the server's multi_pack_engine_unsupported gate.
export function packMergeable(pack: PackOut): boolean {
  return !packLooksReplay(pack) && !packIsMetrics(pack);
}

export function PackPicker({
  packs,
  selectedId,
  onSelect,
  extraIds,
  onToggleExtra,
}: {
  packs: PackOut[];
  selectedId: number | null;
  onSelect: (pack: PackOut) => void;
  // Multi-pack merge (optional): ids of ADDITIONAL packs merged with the
  // selected one into a single run bundle. When `onToggleExtra` is provided,
  // each non-selected mergeable row gets an "+ Add" toggle; the single-pack
  // flow is untouched when these props are omitted.
  extraIds?: number[];
  onToggleExtra?: (pack: PackOut) => void;
}) {
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? packs.filter(
        (p) =>
          p.name.toLowerCase().includes(needle) ||
          (p.description ?? "").toLowerCase().includes(needle) ||
          (p.sourcetypes_json ?? []).some((s) =>
            String(s).toLowerCase().includes(needle),
          ),
      )
    : packs;

  return (
    <Grid $min="300px">
      <Stack>
        <TextInput
          placeholder="Filter packs…"
          value={filter}
          onChange={(_e, { value }) => setFilter(value)}
        />
        <ScrollList>
          {shown.length === 0 ? (
            <Muted>
              No packs match.
            </Muted>
          ) : (
            shown.map((p) => {
              const active = p.id === selectedId;
              const inMerge = (extraIds ?? []).includes(p.id);
              // The "+ Add" toggle appears only when a mergeable primary is
              // already chosen and this row is another mergeable pack.
              const primary = packs.find((pk) => pk.id === selectedId);
              const canToggle =
                onToggleExtra != null &&
                !active &&
                selectedId != null &&
                primary != null &&
                packMergeable(primary) &&
                packMergeable(p);
              return (
                <Selectable key={p.id} $selected={active || inMerge}>
                  <button
                    type="button"
                    onClick={() => onSelect(p)}
                  >
                    <Between>
                      <Strong>
                        {p.name}
                      </Strong>
                      <Muted $small>
                        {p.stanza_count ?? "—"} stanza
                        {p.stanza_count === 1 ? "" : "s"}
                      </Muted>
                    </Between>
                    {p.description && (
                      <Muted $small>
                        {p.description}
                      </Muted>
                    )}
                    <Tags>
                      <PackBadges pack={p} />
                      {inMerge && <Badge tone="sky">in merge</Badge>}
                    </Tags>
                  </button>
                  {canToggle && (
                    <Button
                      variant={inMerge ? "primary" : "ghost"}
                      type="button"
                      title={
                        inMerge
                          ? "Remove from this run's merge"
                          : "Also send this pack in the same run"
                      }
                      onClick={() => onToggleExtra(p)}
                    >
                      {inMerge ? "✓ Added" : "+ Add"}
                    </Button>
                  )}
                </Selectable>
              );
            })
          )}
        </ScrollList>
      </Stack>

      <Panel>
        {selectedId != null ? (
          <PackPreview packId={selectedId} />
        ) : (
          <Muted>
            Select a pack to preview its stanzas and sample lines.
          </Muted>
        )}
      </Panel>
    </Grid>
  );
}
