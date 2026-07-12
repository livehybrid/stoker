// Current-user hook + role helpers, shared by the nav shell and the pages.
//
// The signed-in identity comes from `GET /api/auth/status`, which is public
// (safe while signed out) and reports `authenticated`, `setup_needed`,
// `sso_enabled` and (when authenticated) the resolved `user`. Using the status
// probe rather than `me` means a signed-out request never triggers the central
// 401 -> /login redirect, so the login page can render itself.

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";
import type { AuthStatus, Role, UserOut } from "./types";

// Query key for the auth status; invalidate this after login/logout/setup so the
// nav (username, Logout, admin-only links) re-renders immediately.
export const AUTH_QUERY_KEY = ["auth", "status"] as const;

export interface UseAuth {
  status?: AuthStatus;
  user?: UserOut | null;
  isAdmin: boolean;
  isAuthenticated: boolean;
  setupNeeded: boolean;
  ssoEnabled: boolean;
  isPending: boolean;
  isError: boolean;
  refetch: () => void;
}

/**
 * Read the current auth status. Cached and lightly polled so a session that
 * expires elsewhere (or an admin-driven deactivation) surfaces without a manual
 * reload. Never throws to the caller: a failed probe reads as "not authenticated".
 */
export function useAuth(): UseAuth {
  const q = useQuery({
    queryKey: AUTH_QUERY_KEY,
    queryFn: () => api.auth.status(),
    staleTime: 10_000,
    // A signed-out probe still returns 200 with authenticated=false; a real
    // failure should not spam retries or force a redirect.
    retry: false,
    refetchOnWindowFocus: true,
  });

  const status = q.data;
  const user = status?.user ?? null;
  return {
    status,
    user,
    isAdmin: (user?.role ?? "") === "admin",
    isAuthenticated: !!status?.authenticated,
    setupNeeded: !!status?.setup_needed,
    ssoEnabled: !!status?.sso_enabled,
    isPending: q.isPending,
    isError: q.isError,
    refetch: () => q.refetch(),
  };
}

/** Invalidate the cached auth status (call after login / logout / setup). */
export function useRefreshAuth(): () => Promise<void> {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: AUTH_QUERY_KEY });
}

// Role precedence for any future "at least this role" checks. Kept here so the
// UI and the server (config.VALID_ROLES) share one ordering.
const ROLE_RANK: Record<Role, number> = { viewer: 0, operator: 1, admin: 2 };

/** True when `role` is at least `required` in the viewer < operator < admin order. */
export function roleAtLeast(role: string | null | undefined, required: Role): boolean {
  const have = ROLE_RANK[(role ?? "") as Role];
  if (have === undefined) return false;
  return have >= ROLE_RANK[required];
}
