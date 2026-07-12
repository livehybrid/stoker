// Small presentational helpers shared by the two pages this builder owns
// (Repos + Packs). Kept out of src/components/* deliberately: these are page
// utilities, not part of the shared component kit. No secret values pass here.

/** Short git SHA (first 12 chars) or an em dash when absent. */
export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 12) : "—";
}

/**
 * A compact, human relative time ("just now", "5 min ago", "3 h ago", "2 d
 * ago"), falling back to a locale date for older timestamps. Returns "—" for a
 * null/unparseable input. Purely presentational; the API sends ISO 8601 strings.
 */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 0) return "just now";
  if (secs < 45) return "just now";
  if (secs < 90) return "1 min ago";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days} d ago`;
  return new Date(then).toLocaleDateString();
}

/** A full, absolute timestamp for tooltips (locale string) or "" when absent. */
export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  return Number.isNaN(t) ? "" : new Date(t).toLocaleString();
}

/**
 * Format a byte count as a compact size ("~1.0 KB", "512 B"). Used for a pack's
 * estimated bytes/event. Returns "—" for null/undefined.
 */
export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  if (n < 1024) return `${Math.round(n)} B`;
  const kb = n / 1024;
  if (kb < 1024) return `${kb.toFixed(kb < 10 ? 1 : 0)} KB`;
  const mb = kb / 1024;
  return `${mb.toFixed(mb < 10 ? 1 : 0)} MB`;
}

/** Format a GB/day figure with a fixed unit, or "—" when null. */
export function formatGbDay(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${n < 10 ? n.toFixed(2) : n.toFixed(1)} GB/day`;
}

/**
 * Copy text to the clipboard, resolving true on success. Falls back to a hidden
 * textarea + execCommand for non-secure-context / older browsers so the copy
 * button still works on a LAN host served over plain HTTP.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the textarea path */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
