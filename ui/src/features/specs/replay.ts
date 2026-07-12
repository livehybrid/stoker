// Heuristic replay-pack detection for the wizard's proactive hint.
//
// The API does not persist a per-pack replay flag: replay is detected from the
// pack's eventgen.conf at launch time (api._pack_has_replay) and enforced with a
// 409 replay_single_worker. So the wizard cannot know for certain that a pack is
// replay before it launches. This heuristic surfaces a *soft* hint (lock workers
// to 1, explain why) when a pack declares replay via its pack.yaml tags/engines
// or its name; the server remains the authority and rejects a multi-worker
// replay run regardless, which the run-error handler surfaces with a one-tap fix.

import type { PackOut } from "../../lib/types";

export function packLooksReplay(pack: PackOut | undefined | null): boolean {
  if (!pack) return false;
  const haystacks: string[] = [];
  if (Array.isArray(pack.tags_json)) haystacks.push(...pack.tags_json.map(String));
  if (Array.isArray(pack.engines_json))
    haystacks.push(...pack.engines_json.map(String));
  haystacks.push(pack.name ?? "");
  haystacks.push(pack.description ?? "");
  return haystacks.some((h) => h.toLowerCase().includes("replay"));
}

/** True when the pack's pack.yaml/engines/tags mark it trusted-code. */
export function packLooksTrusted(pack: PackOut | undefined | null): boolean {
  if (!pack) return false;
  const tags = Array.isArray(pack.tags_json) ? pack.tags_json.map(String) : [];
  return tags.some((t) => t.toLowerCase().includes("trusted"));
}
