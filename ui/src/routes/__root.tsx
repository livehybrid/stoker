import {
  Link,
  Outlet,
  createRootRoute,
  useNavigate,
  useRouterState,
} from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import Button from "@splunk/react-ui/Button";
import Divider from "@splunk/react-ui/Divider";
import Heading from "@splunk/react-ui/Heading";
import Moon from "@splunk/react-icons/Moon";
import Sun from "@splunk/react-icons/Sun";
import styled from "styled-components";
import { variables } from "@splunk/themes";

import { api, ApiError, LOGIN_PATH } from "../lib/api";
import { useAuth, useRefreshAuth } from "../lib/auth";
import { useColorScheme } from "../theme";
import { useToast } from "../components/Toast";
import { Muted } from "../components/text";

/*
 * The shell: a fixed left nav and the routed outlet.
 *
 * Splunk UI has no navigation-rail component, so the rail itself is a styled
 * <aside> built from theme tokens; everything inside it (the links, the theme
 * switch, sign out) is a Splunk component. Nothing here knows a hex value, so
 * switching colour scheme switches the chrome with the pages.
 */
const Shell = styled.div`
  display: flex;
  min-height: 100vh;
`;

const Rail = styled.aside`
  display: flex;
  flex-direction: column;
  width: 208px;
  flex-shrink: 0;
  border-right: 1px solid ${variables.borderColor};
  background-color: ${variables.backgroundColorSidebar};
`;

const Brand = styled.div`
  padding: ${variables.spacingLarge} ${variables.spacingLarge} ${variables.spacingMedium};
`;

const Nav = styled.nav`
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0 ${variables.spacingSmall};
`;

const NavLink = styled(Link)`
  display: block;
  padding: ${variables.spacingSmall} ${variables.spacingMedium};
  border-radius: ${variables.borderRadius};
  color: ${variables.contentColorDefault};
  font-size: ${variables.fontSize};
  text-decoration: none;

  &:hover {
    background-color: ${variables.interactiveColorOverlayHover};
  }

  &[data-status="active"] {
    background-color: ${variables.interactiveColorOverlaySelected};
    color: ${variables.contentColorAccent};
  }
`;

const Footer = styled.div`
  border-top: 1px solid ${variables.borderColor};
  padding: ${variables.spacingMedium};
  display: flex;
  flex-direction: column;
  gap: ${variables.spacingSmall};
`;

const Username = styled.p`
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
  font-size: ${variables.fontSizeSmall};
`;

const Content = styled.main`
  flex: 1;
  overflow-x: hidden;
`;

const Page = styled.div`
  margin: 0 auto;
  max-width: 72rem;
  padding: ${variables.spacingLarge} ${variables.spacingXLarge} ${variables.spacingXXLarge};
`;

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
    <Footer>
      <div>
        <Username title={user.username}>{user.username}</Username>
        <Muted $small>{user.role}</Muted>
      </div>
      <Button
        appearance="subtle"
        inline={false}
        onClick={() => logout.mutate()}
        disabled={logout.isPending}
        label={logout.isPending ? "Signing out…" : "Log out"}
      />
    </Footer>
  );
}

/** The one control the operator has over the theme. */
function ThemeToggle() {
  const { colorScheme, toggle } = useColorScheme();
  return (
    <Button
      appearance="subtle"
      inline={false}
      icon={colorScheme === "dark" ? <Sun /> : <Moon />}
      onClick={toggle}
      label={colorScheme === "dark" ? "Light theme" : "Dark theme"}
    />
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
    <Shell>
      <Rail>
        <Brand>
          <Heading level={2}>Stoker</Heading>
          <Muted $small>load-generation control plane</Muted>
        </Brand>
        <Nav>
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              // Exact match only for the dashboard root; others match prefixes
              // so a detail route (e.g. /runs/$runId) keeps "Runs" active.
              activeOptions={{ exact: item.to === "/" }}
            >
              {item.label}
            </NavLink>
          ))}
        </Nav>
        <Divider />
        <ThemeToggle />
        <UserMenu />
      </Rail>

      <Content>
        <Page>
          <Outlet />
        </Page>
      </Content>
    </Shell>
  );
}

export const Route = createRootRoute({
  component: RootLayout,
});
