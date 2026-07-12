// Tiny classNames joiner (no clsx dependency needed at this scale).
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
