import type { ReactNode } from "react";
import { ApiError } from "../lib/api";
import { Button } from "./Button";

/** Neutral placeholder for an empty list / no-data state. */
export function EmptyState({
  title = "Nothing here yet",
  message,
  action,
}: {
  title?: string;
  message?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-md border border-dashed border-surface-muted px-6 py-10 text-center">
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {message && <p className="max-w-md text-sm text-slate-500">{message}</p>}
      {action}
    </div>
  );
}

/** Error panel with the API's message and an optional retry. */
export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  const message =
    error instanceof ApiError
      ? error.message
      : error instanceof Error
        ? error.message
        : "Something went wrong.";
  const status = error instanceof ApiError ? error.status : undefined;
  return (
    <div className="rounded-md border border-red-800/60 bg-red-950/40 px-4 py-3 text-sm text-red-200">
      <p className="font-medium">
        {status ? `Request failed (${status})` : "Request failed"}
      </p>
      <p className="mt-1 text-red-300/90">{message}</p>
      {onRetry && (
        <Button variant="secondary" className="mt-3" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

/** Simple centred loading line for query pending states. */
export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="px-4 py-10 text-center text-sm text-slate-500">{label}</div>
  );
}
