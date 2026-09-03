#!/usr/bin/env python3
"""Declarative bootstrap for a fresh Stoker control plane.

Reconciles a desired-state document (targets, pack repos, specs, and which specs
should be running) against the Stoker operator API so a Terraform build can bring
Stoker up with everything configured and runs already generating, with no manual
UI step.

Design notes
------------
* **Stdlib only** (urllib): runs unchanged on the Stoker control-plane image, a
  plain ``python:3.12-slim``, or any Python 3.8+ image. No pip install in the Job.
* **Idempotent**: every object is create-if-absent, matched by its natural key
  (target/spec by ``name``, repo by ``url``, pack by ``name``). A run is launched
  only when the spec has no active (non-terminal) run, so re-running the Job (or
  re-applying Terraform) never stacks duplicate runs.
* **Secrets never sit in the desired-state document**: a target's HEC token is
  named indirectly (``token_env``) and read from the environment (a mounted k8s
  Secret), so the ConfigMap carrying this file holds no credentials.

Environment
-----------
    STOKER_BASE_URL     control-plane base, default http://stoker:8080
    STOKER_ADMIN_USER   admin username (to log in / first-run setup)
    STOKER_ADMIN_PASSWORD
    STOKER_DESIRED_STATE path to the desired-state JSON, default ./desired-state.json
    STOKER_WAIT_TIMEOUT_S how long to wait for /healthz, default 300
    <token_env vars>    one per target that declares token_env

Exit code is non-zero on any failure so ``terraform apply`` (Job
wait_for_completion) surfaces the error instead of returning green.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = os.environ.get("STOKER_BASE_URL", "http://stoker:8080").rstrip("/")
ADMIN_USER = os.environ.get("STOKER_ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("STOKER_ADMIN_PASSWORD", "")
STATE_PATH = os.environ.get("STOKER_DESIRED_STATE", "desired-state.json")
WAIT_TIMEOUT_S = float(os.environ.get("STOKER_WAIT_TIMEOUT_S", "300"))

TERMINAL = {"completed", "stopped", "failed"}

_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def log(msg):
    # type: (str) -> None
    print("[bootstrap] %s" % msg, flush=True)


def call(method, path, body=None, want=(200, 201, 204), quiet=False):
    # type: (str, str, object, tuple, bool) -> object
    """One API call through the shared cookie jar. Returns parsed JSON or None."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=30) as resp:
            raw = resp.read()
            if not quiet:
                log("%s %s -> %d" % (method, path, resp.status))
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code in want:
            return None
        raise SystemExit("%s %s -> %d: %s" % (method, path, exc.code, detail))


def wait_healthy():
    # type: () -> None
    deadline = time.time() + WAIT_TIMEOUT_S
    while time.time() < deadline:
        try:
            with _opener.open(BASE + "/healthz", timeout=5) as resp:
                if resp.status == 200:
                    log("control plane healthy at %s" % BASE)
                    return
        except (urllib.error.URLError, OSError) as exc:
            log("waiting for %s/healthz (%s)" % (BASE, exc))
        time.sleep(5)
    raise SystemExit("control plane never became healthy within %.0fs" % WAIT_TIMEOUT_S)


def authenticate():
    # type: () -> None
    """First-run setup if no admin exists, then log in (populates the cookie jar)."""
    if not ADMIN_USER or not ADMIN_PASSWORD:
        raise SystemExit("STOKER_ADMIN_USER / STOKER_ADMIN_PASSWORD must be set")
    status = call("GET", "/api/auth/status", quiet=True) or {}
    if status.get("setup_needed"):
        log("no admin yet: creating the first admin via /api/auth/setup")
        call("POST", "/api/auth/setup",
             {"username": ADMIN_USER, "password": ADMIN_PASSWORD}, want=(201,))
    call("POST", "/api/auth/login",
         {"username": ADMIN_USER, "password": ADMIN_PASSWORD}, want=(200,))
    log("authenticated as %s" % ADMIN_USER)


# --------------------------------------------------------------------------- #
# Reconcilers (create-if-absent, matched by natural key)
# --------------------------------------------------------------------------- #

def ensure_target(spec):
    # type: (dict) -> None
    name = spec["name"]
    existing = {t["name"]: t for t in (call("GET", "/api/targets", quiet=True) or [])}
    if name in existing:
        log("target %r already present (id=%s)" % (name, existing[name]["id"]))
        return
    token = spec.get("token")
    if spec.get("token_env"):
        token = os.environ.get(spec["token_env"])
        if not token:
            raise SystemExit("target %r: token_env %r is not set in the environment"
                             % (name, spec["token_env"]))
    body = {
        "name": name,
        "hec_url": spec["hec_url"],
        "token": token,
        "default_index": spec.get("default_index"),
        "env_tag": spec.get("env_tag", "lab"),
        "verify_tls": spec.get("verify_tls", True),
    }
    if spec.get("max_concurrent_gb_day") is not None:
        body["max_concurrent_gb_day"] = spec["max_concurrent_gb_day"]
    call("POST", "/api/targets", body, want=(201,))
    log("created target %r" % name)


def ensure_repo(spec):
    # type: (dict) -> None
    url = spec["url"]
    existing = {r["url"]: r for r in (call("GET", "/api/repos", quiet=True) or [])}
    repo = existing.get(url)
    if repo is None:
        body = {
            "url": url,
            "auth_kind": spec.get("auth_kind", "none"),
            "default_ref": spec.get("default_ref", "main"),
            "trusted_code": spec.get("trusted_code", False),
        }
        if spec.get("secret_env"):
            body["secret"] = os.environ.get(spec["secret_env"])
        repo = call("POST", "/api/repos", body, want=(201,))
        log("registered repo %s (id=%s)" % (url, repo.get("id")))
    else:
        log("repo %s already registered (id=%s)" % (url, repo["id"]))
    if spec.get("sync", True):
        result = call("POST", "/api/repos/%d/sync" % repo["id"], {}, want=(200,)) or {}
        log("synced repo %s: packs_indexed=%s failures=%s"
            % (url, result.get("packs_indexed"), result.get("lint_failures")))


def pack_id_by_name(name):
    # type: (str) -> int
    for pack in (call("GET", "/api/packs", quiet=True) or []):
        if pack["name"] == name:
            return pack["id"]
    raise SystemExit("pack %r not found: register/sync the repo that provides it first"
                     % name)


def target_id_by_name(name):
    # type: (str) -> int
    for target in (call("GET", "/api/targets", quiet=True) or []):
        if target["name"] == name:
            return target["id"]
    raise SystemExit("target %r not found" % name)


def ensure_spec(spec):
    # type: (dict) -> int
    name = spec["name"]
    existing = {s["name"]: s for s in (call("GET", "/api/specs", quiet=True) or [])}
    if name in existing:
        log("spec %r already present (id=%s)" % (name, existing[name]["id"]))
        return existing[name]["id"]
    body = {
        "name": name,
        "pack_id": pack_id_by_name(spec["pack"]),
        "target_id": target_id_by_name(spec["target"]),
        "engine": spec.get("engine", "eventgen"),
        "rate_mode": spec.get("rate_mode", "eps"),
        "rate_value": spec.get("rate_value"),
        "interval_s": spec.get("interval_s"),
        "workers": spec.get("workers", 1),
        "duration_s": spec.get("duration_s"),
        "fleet": spec.get("fleet", "k8s-local"),
        "overrides": spec.get("overrides"),
    }
    if spec.get("extra_packs"):
        body["extra_pack_ids"] = [pack_id_by_name(p) for p in spec["extra_packs"]]
    created = call("POST", "/api/specs", body, want=(201,))
    log("created spec %r (id=%s)" % (name, created["id"]))
    return created["id"]


def has_active_run(spec_id):
    # type: (int) -> bool
    for run in (call("GET", "/api/runs", quiet=True) or []):
        if run["spec_id"] == spec_id and run["state"] not in TERMINAL:
            log("spec %d already has an active run %d (state=%s)"
                % (spec_id, run["id"], run["state"]))
            return True
    return False


def maybe_launch(spec, spec_id):
    # type: (dict, int) -> None
    if not spec.get("run"):
        return
    if has_active_run(spec_id):
        return
    body = {}
    if spec.get("backfill"):
        body["backfill"] = spec["backfill"]  # e.g. {"hours": 24} — see docs/PACKS.md
    created = call("POST", "/api/specs/%d/run" % spec_id, body, want=(201,))
    log("launched run %s for spec %r (state=%s)"
        % (created["run_id"], spec["name"], created["state"]))


def main():
    # type: () -> None
    with open(STATE_PATH) as fh:
        desired = json.load(fh)
    wait_healthy()
    authenticate()
    for target in desired.get("targets", []):
        ensure_target(target)
    for repo in desired.get("repos", []):
        ensure_repo(repo)
    for spec in desired.get("specs", []):
        spec_id = ensure_spec(spec)
        maybe_launch(spec, spec_id)
    log("bootstrap complete")


if __name__ == "__main__":
    main()
