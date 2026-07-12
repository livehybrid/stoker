import { Link, Outlet, createRootRoute } from "@tanstack/react-router";
import { cn } from "../components/cn";

// Root layout: a fixed left nav plus the routed <Outlet/>. Every page renders
// inside this shell. File-based routing generates the tree from src/routes/;
// this file defines the application chrome shared by all routes.

interface NavItem {
  to: string;
  label: string;
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
];

function RootLayout() {
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
          {NAV.map((item) => (
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
        <div className="px-4 py-3 text-[11px] text-slate-600">
          LAN allowlist · no auth this stage
        </div>
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
