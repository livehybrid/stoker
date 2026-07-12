import {
  Link,
  Outlet,
  createRootRoute,
  useNavigate,
  useRouterState,
} from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { cn } from "../components/cn";
import { api, ApiError, LOGIN_PATH } from "../lib/api";
import { useAuth, useRefreshAuth } from "../lib/auth";
import { useToast } from "../components/Toast";

// Root layout: a fixed left nav plus the routed <Outlet/>. Every page renders
// inside this shell EXCEPT the login screen, which is a standalone full-page
// view (it renders its own chrome, so the shell steps aside for it).
// File-based routing generates the tree from src/routes/.

interface NavItem {
  to: string;
  label: string;
  adminOnly?: boolean;
}

// `to` values are the generated route paths (see routeTree.gen.ts). Adding a
// page = add a src/routes/<name>.tsx file + an entry here for the nav link.
const NAV: NavItem[] = [
  { to: "/", label: "Dashboard" },
  { to: "/runs", label: "Runs" },
  { to: "/specs", label: "Specs" },
  { to: "/packs", label: "Packs" },
  { to: "/repos", label: "Repos" },
  { to: "/targets", label: "Targets" },
  { to: "/users", label: "Users", adminOnly: true },
];

function UserMenu() {
  const navigate = useNavigate();
  const refreshAuth = useRefreshAuth();
  const toast = useToast();
  const { user, isAuthenticated } = useAuth();

  const logout = useMutation({
    mutationFn: () => api.auth.logout(),
    onSuccess: async () => {
      await refreshAuth();
      navigate({ to: "/login", replace: true });
    },
    onError: (err: unknown) => {
      toast.error(
        err instanceof ApiError ? err.message : "Could not sign out.",
      );
    },
  });

  // With a trusted proxy (SSO) there may be no local session to end; only offer
  // sign-out when there is an authenticated local/proxy identity to clear.
  if (!isAuthenticated || !user) {
    return null;
  }

  return (
    <div className="border-t border-surface-muted px-4 py-3">
      <p className="truncate text-xs font-medium text-slate-300" title={user.username}>
        {user.username}
      </p>
      <p className="mb-2 text-[11px] uppercase tracking-wide text-slate-600">
        {user.role}
      </p>
      <button
        onClick={() => logout.mutate()}
        disabled={logout.isPending}
        className={cn(
          "w-full rounded-md border border-surface-muted px-3 py-1.5 text-left text-sm font-medium text-slate-300",
          "hover:bg-surface-muted hover:text-slate-100 disabled:opacity-50",
        )}
      >
        {logout.isPending ? "Signing out…" : "Log out"}
      </button>
    </div>
  );
}

function RootLayout() {
  const { isAdmin } = useAuth();
  // The login page owns the full viewport; do not wrap it in the app chrome.
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  if (pathname === LOGIN_PATH) {
    return <Outlet />;
  }

  const items = NAV.filter((item) => !item.adminOnly || isAdmin);

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-52 shrink-0 flex-col border-r border-surface-muted bg-surface-soft">
        <div className="px-4 py-4">
          <span className="text-lg font-semibold tracking-tight text-slate-100">
            Stoker
          </span>
          <p className="text-xs text-slate-500">load-generation control plane</p>
        </div>
        <nav className="flex-1 space-y-0.5 px-2">
          {items.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              // Exact match only for the dashboard root; others match prefixes
              // so a detail route (e.g. /runs/$runId) keeps "Runs" active.
              activeOptions={{ exact: item.to === "/" }}
              className={cn(
                "block rounded-md px-3 py-2 text-sm font-medium text-slate-300",
                "hover:bg-surface-muted hover:text-slate-100",
              )}
              activeProps={{ className: "bg-surface-muted text-white" }}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <UserMenu />
      </aside>

      <main className="flex-1 overflow-x-hidden">
        <div className="mx-auto max-w-6xl px-6 py-6">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

export const Route = createRootRoute({
  component: RootLayout,
});
