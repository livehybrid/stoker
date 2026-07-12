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
    <div className="flex min-h-screen items-center justify-center bg-surface px-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="text-center">
          <h1 className="text-2xl font-semibold tracking-tight text-slate-100">
            Stoker
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            load-generation control plane
          </p>
        </div>

        {isPending ? (
          <Card>
            <LoadingState label="Checking sign-in…" />
          </Card>
        ) : isError ? (
          <Card title="Cannot reach the control plane">
            <p className="text-sm text-slate-400">
              The sign-in service did not respond. Check the control plane is
              running, then try again.
            </p>
            <Button
              variant="secondary"
              className="mt-4"
              onClick={() => refetch()}
            >
              Retry
            </Button>
          </Card>
        ) : setupNeeded ? (
          <Card title="Create the first administrator">
            <p className="mb-4 text-sm text-slate-400">
              No users exist yet. Set up the initial admin account to secure this
              instance. You will be signed in straight away.
            </p>
            <form onSubmit={submitSetup} className="space-y-4">
              <Field label="Admin username">
                <TextInput
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="admin"
                  autoComplete="username"
                  autoFocus
                />
              </Field>
              <Field label="Password" hint="At least 8 characters.">
                <TextInput
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="new-password"
                />
              </Field>
              <Field label="Confirm password">
                <TextInput
                  type="password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  autoComplete="new-password"
                />
              </Field>
              {formError && <p className="text-sm text-red-400">{formError}</p>}
              <Button
                type="submit"
                variant="primary"
                className="w-full"
                disabled={busy}
              >
                {setup.isPending ? "Creating…" : "Create admin and sign in"}
              </Button>
            </form>
          </Card>
        ) : (
          <Card title="Sign in">
            <form onSubmit={submitLogin} className="space-y-4">
              <Field label="Username">
                <TextInput
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="you"
                  autoComplete="username"
                  autoFocus
                />
              </Field>
              <Field label="Password">
                <TextInput
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                />
              </Field>
              {formError && <p className="text-sm text-red-400">{formError}</p>}
              <Button
                type="submit"
                variant="primary"
                className="w-full"
                disabled={busy}
              >
                {login.isPending ? "Signing in…" : "Sign in"}
              </Button>
            </form>
            {ssoEnabled && (
              <p className="mt-4 border-t border-surface-muted pt-4 text-xs text-slate-500">
                Single sign-on is enabled. If your organisation uses an identity
                provider, you may already be signed in automatically through it.
              </p>
            )}
          </Card>
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/login")({
  component: LoginScreen,
});
