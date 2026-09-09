import { useEffect, useState, type FormEvent } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";

import { api, ApiError } from "../lib/api";
import { useAuth, useRefreshAuth } from "../lib/auth";
import type { LoginRequest, SetupRequest, UserOut } from "../lib/types";
import { Card } from "../components/Card";
import { Field, TextInput } from "../components/Field";
import { Button } from "../components/Button";
import { LoadingState } from "../components/States";
import { BigNumber, Centre, Form, Muted, Narrow, Negative } from "../components/text";

// Login page. It drives three states off `GET /api/auth/status`:
//   - already authenticated  -> redirect straight to the dashboard;
//   - setup_needed           -> show the "Create the first admin" form (POST
//                               /api/auth/setup), which logs the new admin in;
//   - otherwise              -> show the username/password login form (POST
//                               /api/auth/login).
// On success it invalidates the cached auth status (so the nav updates) and
// navigates to the dashboard. It is intentionally outside the app chrome so it
// reads as a standalone sign-in screen.

function LoginScreen() {
  const navigate = useNavigate();
  const refreshAuth = useRefreshAuth();
  const {
    isAuthenticated,
    setupNeeded,
    ssoEnabled,
    isPending,
    isError,
    refetch,
  } = useAuth();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  // Already signed in (session or SSO): leave the login page for the dashboard.
  useEffect(() => {
    if (isAuthenticated) {
      navigate({ to: "/", replace: true });
    }
  }, [isAuthenticated, navigate]);

  async function onAuthenticated(user: UserOut) {
    await refreshAuth();
    // Reset local state before we leave so a back-navigation shows a clean form.
    setPassword("");
    setConfirm("");
    navigate({ to: "/", replace: true });
    return user;
  }

  const login = useMutation({
    mutationFn: (body: LoginRequest) => api.auth.login(body),
    onSuccess: onAuthenticated,
    onError: (err: unknown) => {
      setFormError(
        err instanceof ApiError
          ? // 401 is the uniform "invalid credentials" from the API.
            err.status === 401
            ? "Incorrect username or password."
            : err.message
          : "Sign-in failed. Please try again.",
      );
    },
  });

  const setup = useMutation({
    mutationFn: (body: SetupRequest) => api.auth.setup(body),
    onSuccess: onAuthenticated,
    onError: (err: unknown) => {
      setFormError(
        err instanceof ApiError
          ? err.status === 409
            ? "Setup is already complete. Please sign in."
            : err.message
          : "Could not create the first administrator.",
      );
    },
  });

  const busy = login.isPending || setup.isPending;

  function submitLogin(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    const u = username.trim();
    if (!u) return setFormError("Enter your username.");
    if (!password) return setFormError("Enter your password.");
    login.mutate({ username: u, password });
  }

  function submitSetup(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    const u = username.trim();
    if (!u) return setFormError("Choose a username for the administrator.");
    if (password.length < 8) {
      return setFormError("Use a password of at least 8 characters.");
    }
    if (password !== confirm) return setFormError("The passwords do not match.");
    setup.mutate({ username: u, password });
  }

  return (
    <Centre>
      <Narrow>
        <div>
          <BigNumber>
            Stoker
          </BigNumber>
          <Muted>
            load-generation control plane
          </Muted>
        </div>

        {isPending ? (
          <Card>
            <LoadingState label="Checking sign-in…" />
          </Card>
        ) : isError ? (
          <Card title="Cannot reach the control plane">
            <Muted>
              The sign-in service did not respond. Check the control plane is
              running, then try again.
            </Muted>
            <Button
              variant="secondary"
              onClick={() => refetch()}
            >
              Retry
            </Button>
          </Card>
        ) : setupNeeded ? (
          <Card title="Create the first administrator">
            <Muted>
              No users exist yet. Set up the initial admin account to secure this
              instance. You will be signed in straight away.
            </Muted>
            <Form onSubmit={submitSetup}>
              <Field label="Admin username">
                <TextInput
                  value={username}
                  onChange={(_e, { value }) => setUsername(value)}
                  placeholder="admin"
                  autoComplete="username"
                  autoFocus
                />
              </Field>
              <Field label="Password" hint="At least 8 characters.">
                <TextInput
                  type="password"
                  value={password}
                  onChange={(_e, { value }) => setPassword(value)}
                  autoComplete="new-password"
                />
              </Field>
              <Field label="Confirm password">
                <TextInput
                  type="password"
                  value={confirm}
                  onChange={(_e, { value }) => setConfirm(value)}
                  autoComplete="new-password"
                />
              </Field>
              {formError && <Negative>{formError}</Negative>}
              <Button
                type="submit"
                variant="primary"
                inline={false}
                disabled={busy}
              >
                {setup.isPending ? "Creating…" : "Create admin and sign in"}
              </Button>
            </Form>
          </Card>
        ) : (
          <Card title="Sign in">
            <Form onSubmit={submitLogin}>
              <Field label="Username">
                <TextInput
                  value={username}
                  onChange={(_e, { value }) => setUsername(value)}
                  placeholder="you"
                  autoComplete="username"
                  autoFocus
                />
              </Field>
              <Field label="Password">
                <TextInput
                  type="password"
                  value={password}
                  onChange={(_e, { value }) => setPassword(value)}
                  autoComplete="current-password"
                />
              </Field>
              {formError && <Negative>{formError}</Negative>}
              <Button
                type="submit"
                variant="primary"
                inline={false}
                disabled={busy}
              >
                {login.isPending ? "Signing in…" : "Sign in"}
              </Button>
            </Form>
            {ssoEnabled && (
              <Muted $small>
                Single sign-on is enabled. If your organisation uses an identity
                provider, you may already be signed in automatically through it.
              </Muted>
            )}
          </Card>
        )}
      </Narrow>
    </Centre>
  );
}

export const Route = createFileRoute("/login")({
  component: LoginScreen,
});
