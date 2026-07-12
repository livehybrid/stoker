"""Auth subsystem tests for the Stoker control plane.

The maintainer is replacing the Traefik basic-auth stopgap with app-level auth
that is **vendor-neutral** (no hard dependency on authentik or any IdP). This
file owns the behavioural contract for that subsystem. It is written to the
requirements, not to a specific implementation, and it is the source of truth
the backend builders (User model + config + ``server/auth.py`` +
``server/routes/auth.py`` + app wiring) must satisfy.

What it asserts:

* **Default-admin bootstrap** — with ``STOKER_ADMIN_USER`` / ``STOKER_ADMIN_PASSWORD``
  set, the admin exists after startup and can log in; ``GET /api/auth/me`` returns
  it and never carries ``password_hash`` or the master key.
* **First-access setup** — with zero users, ``GET /api/auth/status`` reports
  ``setup_needed: true``; ``POST /api/auth/setup`` creates the first admin; a
  second setup call is ``409``.
* **Login / logout / session** — a bad password is ``401``; a good password sets
  the session cookie; a protected ops endpoint (``GET /api/targets``) succeeds
  with the cookie and is ``401`` without it; logout clears the session.
* **Exempt paths** — the agent wire protocol
  (``POST /api/agent/runs/{id}/heartbeat``) and the GitHub webhook
  (``POST /api/hooks/github``) are reachable **without a browser session**: they
  answer with their *own* app-level rejection (agent JWT 401 /
  webhook HMAC 401), whose body shape is distinguishable from the auth
  middleware's session-401.
* **Trusted-proxy SSO (security-critical)** — a request whose *immediate peer*
  (``request.client.host``) is inside ``STOKER_TRUSTED_PROXIES`` and which
  carries ``STOKER_AUTH_HEADER: alice`` authenticates as ``alice`` (created
  ``source="proxy"``, role from ``STOKER_PROXY_DEFAULT_ROLE``). The **same
  header from an untrusted peer is ignored** (401). A client-supplied auth
  header is never trusted.
* **Role enforcement** — a non-admin cannot list/manage users (``403``); an admin
  can. The last admin cannot be deleted, and a user cannot delete themselves.
* **No-secret-in-body** — no auth response ever contains ``password_hash`` or the
  Fernet master key.

How the trusted-proxy peer is simulated
----------------------------------------
Starlette's ``TestClient`` sets ``request.client.host`` from the ``client=``
constructor tuple. Verified in this environment:

    TestClient(app)                         -> request.client.host == "testclient"
    TestClient(app, client=("10.9.8.7", 4)) -> request.client.host == "10.9.8.7"

``base_url`` does **not** affect ``client.host``; request headers
(``X-Forwarded-For`` / the configured auth header) are independent and always
settable. So a *trusted proxy* is a ``TestClient`` built with
``client=(<ip-in-a-trusted-CIDR>, port)`` and an *untrusted peer* is one built
with ``client=(<ip-outside-every-CIDR>, port)``; both carry the auth header. The
trust decision is driven solely by the transport-level peer, exactly as the
middleware must decide it.

Resilience / capability guards
------------------------------
The auth backend was built as a parallel workstream (User model, auth config
fields, auth routes, middleware). These tests hard-assert the contract against
it. Lightweight capability guards remain so the file degrades gracefully rather
than erroring if it is ever run against a checkout where auth is only partially
wired: if the ``Settings`` auth fields are absent the whole module skips, and if
the auth routes are not mounted an individual test skips with a precise reason
(mirroring ``test_operator_api.py``'s skip-on-501 convention). The
security-critical trust-model tests never skip on the boundary itself: once auth
is active they *fail* if an untrusted peer's header is honoured — a
mis-implemented trust boundary must never pass silently.

Every auth-configured app is built via :func:`server.config.load_settings` from
an explicit env mapping (so ``STOKER_TRUSTED_PROXIES`` is parsed into real
``ip_network`` objects and roles are validated exactly as in production), then
grafted onto the ``settings`` fixture's isolated temp DB. Each test builds its
own ``TestClient`` (with its own ``client=`` peer where the trust model is under
test) rather than the shared ``client`` fixture, which carries no session and a
fixed peer.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Dict, Optional, Tuple

import pytest

from server import config as config_mod
from server import db as db_mod
from server import drivers as drivers_mod
from server.config import Settings, load_settings

# --------------------------------------------------------------------------- #
# Contract constants (the requirements). If the backend chooses different names
# it changes these in one place; the security invariants below do not move.
# --------------------------------------------------------------------------- #

ADMIN_USER = "root-admin"
ADMIN_PASSWORD = "corr3ct-h0rse-battery-staple"  # noqa: S105 (test fixture value)

# Trusted-proxy CIDR + peers used across the SSO tests. 10.10.0.0/24 is the
# "reverse proxy" network; 10.10.0.7 is inside it (trusted), 203.0.113.9 is a
# public/untrusted peer outside every configured CIDR.
TRUSTED_CIDR = "10.10.0.0/24"
TRUSTED_PEER = ("10.10.0.7", 51000)
UNTRUSTED_PEER = ("203.0.113.9", 51000)

AUTH_HEADER = "X-Forwarded-User"
PROXY_DEFAULT_ROLE = "operator"

# Endpoint paths (vendor-neutral, app-level).
ME_PATH = "/api/auth/me"
STATUS_PATH = "/api/auth/status"
SETUP_PATH = "/api/auth/setup"
LOGIN_PATH = "/api/auth/login"
LOGOUT_PATH = "/api/auth/logout"
USERS_PATH = "/api/users"
PROTECTED_OPS_PATH = "/api/targets"  # a representative protected operator route

# Secret substrings that must never appear in an auth response body.
_FORBIDDEN_BODY_SUBSTRINGS = ("password_hash", "master_key")

# The lower-cased distinguishing fragment of the session-middleware's 401 body
# (``{"detail": "authentication required"}``). Exempt-path tests assert this does
# NOT appear, proving the request reached the agent/webhook layer rather than
# being short-circuited by the guard.
_MIDDLEWARE_401_DETAIL = "authentication required"


# --------------------------------------------------------------------------- #
# Capability probes: is the auth subsystem present yet?
# --------------------------------------------------------------------------- #

def _settings_supports_auth():
    # type: () -> bool
    """True when the Settings dataclass has gained the auth config fields.

    We only require the trust-model fields plus the admin bootstrap fields; the
    exact attribute names are the contract the backend must expose.
    """
    field_names = {f.name for f in dataclasses.fields(Settings)}
    required = {
        "admin_user",
        "admin_password",
        "trusted_proxies",
        "auth_header",
        "proxy_default_role",
    }
    return required.issubset(field_names)


def _user_model_present():
    # type: () -> bool
    """True when a User ORM model with the contract columns exists."""
    try:
        from server import models
    except Exception:  # pragma: no cover - defensive
        return False
    user = getattr(models, "User", None)
    if user is None:
        return False
    cols = set(getattr(user, "__table__").columns.keys()) if hasattr(user, "__table__") else set()
    # id, email, role, source (per the contract) + password_hash for local auth.
    return {"id", "email", "role", "source"}.issubset(cols)


def _auth_routes_present(app):
    # type: (Any) -> bool
    """True when the auth routes are mounted on the built app.

    Checks the OpenAPI path table for the status endpoint; if the router is not
    registered the app simply does not have it and the tests skip.
    """
    try:
        paths = set(app.openapi().get("paths", {}).keys())
    except Exception:  # pragma: no cover - defensive
        return False
    return STATUS_PATH in paths or LOGIN_PATH in paths


AUTH_CONFIG_READY = _settings_supports_auth()
AUTH_USER_READY = _user_model_present()

# Applied to every test: when the config surface is absent there is nothing to
# build an auth-configured app from, so the whole file skips cleanly.
pytestmark = pytest.mark.skipif(
    not AUTH_CONFIG_READY,
    reason=(
        "auth subsystem not built yet: Settings lacks the auth config fields "
        "(admin_user/admin_password/trusted_proxies/auth_header/"
        "proxy_default_role). Tests activate once server.config exposes them."
    ),
)


# --------------------------------------------------------------------------- #
# App builder: an auth-configured app + TestClient, isolated per test.
# --------------------------------------------------------------------------- #

# Env keys the auth config is parsed from (the documented contract surface).
_ENV_ADMIN_USER = "STOKER_ADMIN_USER"
_ENV_ADMIN_PASSWORD = "STOKER_ADMIN_PASSWORD"
_ENV_TRUSTED_PROXIES = "STOKER_TRUSTED_PROXIES"
_ENV_AUTH_HEADER = "STOKER_AUTH_HEADER"
_ENV_PROXY_ROLE = "STOKER_PROXY_DEFAULT_ROLE"


def _make_auth_settings(
    base,
    admin_user=None,
    admin_password=None,
    trusted_proxies="",
    auth_header=AUTH_HEADER,
    proxy_default_role=PROXY_DEFAULT_ROLE,
):
    # type: (Settings, Optional[str], Optional[str], str, str, str) -> Settings
    """Build auth-configured Settings via the REAL parser, on the temp DB.

    The auth config is produced by :func:`server.config.load_settings` from an
    explicit env mapping, so the exact contract surface is exercised
    (``STOKER_TRUSTED_PROXIES`` is parsed into ``ip_network`` objects, roles are
    validated, etc.) rather than hand-built. The temp DB / bundle / repo paths
    from the ``settings`` fixture are then grafted on so the app stays isolated,
    and the fixture's master key is reused so its Fernet/JWT state is consistent.

    ``trusted_proxies`` is a comma-separated CIDR string (the env form); ``""``
    means no proxy is trusted.
    """
    env = {
        "STOKER_MASTER_KEY": base.master_key,
        "PUBLIC_BASE_URL": base.public_base_url,
        _ENV_AUTH_HEADER: auth_header,
        _ENV_PROXY_ROLE: proxy_default_role,
    }
    if admin_user is not None:
        env[_ENV_ADMIN_USER] = admin_user
    if admin_password is not None:
        env[_ENV_ADMIN_PASSWORD] = admin_password
    if trusted_proxies:
        env[_ENV_TRUSTED_PROXIES] = trusted_proxies

    parsed = load_settings(env=env)
    # Graft the fixture's isolated storage paths + non-auth infra onto the parsed
    # (auth-correct) settings, keeping everything else from the real parser.
    return dataclasses.replace(
        parsed,
        database_url=base.database_url,
        bundle_dir=base.bundle_dir,
        repo_clone_dir=base.repo_clone_dir,
        worker_image=base.worker_image,
        portainer_host=base.portainer_host,
        portainer_token=base.portainer_token,
        portainer_endpoint=base.portainer_endpoint,
    )


def _install(settings):
    # type: (Settings) -> None
    """Install ``settings`` as the process singleton and (re)bind the engine."""
    config_mod.set_settings(settings)
    db_mod.configure(settings.database_url)
    # Ensure the schema (incl. any users table) exists on this engine.
    db_mod.create_all()


def _build_client(settings, fake_driver, peer=None):
    # type: (Settings, Any, Optional[Tuple[str, int]]) -> Any
    """Build the app against ``settings`` and return a lifespan-run TestClient.

    ``peer`` sets the transport-level ``request.client`` tuple so a test can
    present itself as a specific immediate peer (a trusted or untrusted proxy).
    The returned object is a context manager; callers use ``with``.
    """
    from fastapi.testclient import TestClient

    from server.app import create_app

    _install(settings)
    # Rebind the seeded fleets to the shared FakeDriver so app startup + any
    # ops route resolves a driver without reaching Portainer.
    drivers_mod.clear_cache()
    drivers_mod.register_driver("fake-local", fake_driver)
    drivers_mod.register_driver("swarm-local", fake_driver)

    app = create_app()
    if peer is not None:
        return TestClient(app, client=peer)
    return TestClient(app)


def _skip_if_no_routes(client):
    # type: (Any) -> None
    """Skip when the auth routes are not mounted on this app build."""
    if not _auth_routes_present(client.app):
        pytest.skip(
            "auth routes not mounted yet (no %s/%s in the app); the auth router "
            "is a parallel workstream." % (STATUS_PATH, LOGIN_PATH)
        )


def _body_text(resp):
    # type: (Any) -> str
    """Serialise a response body to text for substring assertions."""
    try:
        return json.dumps(resp.json())
    except ValueError:
        return resp.text or ""


def _assert_no_secret_leak(resp):
    # type: (Any) -> None
    """Assert no forbidden secret substring appears in a response body."""
    text = _body_text(resp)
    for needle in _FORBIDDEN_BODY_SUBSTRINGS:
        assert needle not in text, "auth response leaked %r: %s" % (needle, text[:200])


def _login(client, identity, password):
    # type: (Any, str, str) -> Any
    """POST the login endpoint.

    The requirements fix the *behaviour* (bad password 401; good password sets a
    session cookie) but leave the identity field name to the implementation. We
    send both ``username`` and ``email`` so either binding accepts a correct
    credential; the tests assert on the *outcome*, not the key.
    """
    return client.post(
        LOGIN_PATH,
        json={"username": identity, "email": identity, "password": password},
    )


def _setup_body(identity, password):
    # type: (str, str) -> Dict[str, str]
    """First-admin setup body, tolerant of username/email identity binding."""
    return {"username": identity, "email": identity, "password": password}


# --------------------------------------------------------------------------- #
# 1. Default-admin bootstrap.
# --------------------------------------------------------------------------- #

def test_default_admin_bootstrap_and_login(settings, fake_driver):
    """With STOKER_ADMIN_USER/PASSWORD set, the admin exists after startup, can
    log in, and GET /me returns it with no password_hash / master key in the body.
    """
    auth_settings = _make_auth_settings(
        settings,
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PASSWORD,
        trusted_proxies="",
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    with _build_client(auth_settings, fake_driver) as client:
        _skip_if_no_routes(client)

        if not AUTH_USER_READY:
            pytest.skip("User model with (id,email,role,source) not present yet")

        # The admin was created on startup: setup is not needed.
        status = client.get(STATUS_PATH)
        assert status.status_code == 200, _body_text(status)
        assert status.json().get("setup_needed") is False

        # Confirmed in the DB: exactly one admin, source local, hashed password.
        from sqlalchemy import select

        from server.models import User

        with db_mod.SessionLocal() as db:
            admins = list(db.execute(select(User).where(User.role == "admin")).scalars().all())
        assert len(admins) >= 1
        admin = admins[0]
        assert admin.source == "local"
        # The stored credential is a hash, never the plaintext password.
        pw_hash = getattr(admin, "password_hash", None)
        assert pw_hash, "admin must have a stored password hash"
        assert ADMIN_PASSWORD not in str(pw_hash)

        # A wrong password is refused.
        bad = _login(client, ADMIN_USER, "not-the-password")
        assert bad.status_code == 401, _body_text(bad)

        # The correct password authenticates and sets a session cookie.
        ok = _login(client, ADMIN_USER, ADMIN_PASSWORD)
        assert ok.status_code in (200, 204), _body_text(ok)
        assert ok.cookies, "login must set a session cookie"
        _assert_no_secret_leak(ok)

        # GET /me returns the admin identity, no secret material.
        me = client.get(ME_PATH)
        assert me.status_code == 200, _body_text(me)
        me_body = me.json()
        assert me_body.get("role") == "admin"
        # The identity round-trips (email or username carries ADMIN_USER).
        identity_values = {str(v) for v in me_body.values() if isinstance(v, str)}
        assert ADMIN_USER in identity_values, me_body
        assert "password_hash" not in me_body
        _assert_no_secret_leak(me)


# --------------------------------------------------------------------------- #
# 2. First-access setup (zero users).
# --------------------------------------------------------------------------- #

def test_first_access_setup_creates_admin_then_409(settings, fake_driver):
    """With zero users and no env admin, /api/auth/status.setup_needed is true;
    POST /api/auth/setup creates the first admin; a second setup call is 409.
    """
    auth_settings = _make_auth_settings(
        settings,
        admin_user=None,
        admin_password=None,
        trusted_proxies="",
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    with _build_client(auth_settings, fake_driver) as client:
        _skip_if_no_routes(client)

        status = client.get(STATUS_PATH)
        assert status.status_code == 200, _body_text(status)
        assert status.json().get("setup_needed") is True, (
            "zero users + no env admin must report setup_needed"
        )

        created = client.post(SETUP_PATH, json=_setup_body(ADMIN_USER, ADMIN_PASSWORD))
        assert created.status_code in (200, 201), _body_text(created)
        _assert_no_secret_leak(created)

        # Setup is now closed.
        status2 = client.get(STATUS_PATH)
        assert status2.status_code == 200
        assert status2.json().get("setup_needed") is False

        # A second setup attempt must be rejected (an admin already exists).
        again = client.post(SETUP_PATH, json=_setup_body("intruder", "intruder-pw-123456"))
        assert again.status_code == 409, _body_text(again)

        # The freshly-created admin can log in.
        ok = _login(client, ADMIN_USER, ADMIN_PASSWORD)
        assert ok.status_code in (200, 204), _body_text(ok)
        assert ok.cookies


# --------------------------------------------------------------------------- #
# 3. Login / logout / session on a protected ops route.
# --------------------------------------------------------------------------- #

def test_protected_ops_requires_session(settings, fake_driver):
    """GET /api/targets is 401 without a session and 200 with the login cookie;
    logout clears the session so the route is protected again.
    """
    auth_settings = _make_auth_settings(
        settings,
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PASSWORD,
        trusted_proxies="",
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    with _build_client(auth_settings, fake_driver) as client:
        _skip_if_no_routes(client)

        # No session -> the middleware refuses the protected ops route.
        anon = client.get(PROTECTED_OPS_PATH)
        assert anon.status_code == 401, _body_text(anon)

        # Log in, then the same route succeeds with the session cookie.
        ok = _login(client, ADMIN_USER, ADMIN_PASSWORD)
        assert ok.status_code in (200, 204), _body_text(ok)
        assert ok.cookies

        authed = client.get(PROTECTED_OPS_PATH)
        assert authed.status_code == 200, _body_text(authed)
        assert isinstance(authed.json(), list)  # the targets list view

        # Log out; the session is cleared and the route is protected again. The
        # TestClient carries a cookie jar, so this exercises the real round-trip.
        out = client.post(LOGOUT_PATH)
        assert out.status_code in (200, 204), _body_text(out)
        client.cookies.clear()
        anon_again = client.get(PROTECTED_OPS_PATH)
        assert anon_again.status_code == 401, _body_text(anon_again)


# --------------------------------------------------------------------------- #
# 4. Exempt paths: agent wire protocol + GitHub webhook bypass the session
#    middleware and answer with their OWN app-level rejection.
# --------------------------------------------------------------------------- #

def test_agent_heartbeat_is_exempt_from_session_auth(settings, fake_driver):
    """POST /api/agent/runs/1/heartbeat is reachable without a browser session and
    returns the agent JWT 401 (its own auth), distinguishable from the auth
    middleware's session-401.
    """
    auth_settings = _make_auth_settings(
        settings,
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PASSWORD,
        trusted_proxies="",
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    with _build_client(auth_settings, fake_driver) as client:
        _skip_if_no_routes(client)

        # No session cookie and no bearer: the request must reach the agent
        # router (not be short-circuited by the session middleware) and be
        # rejected there for the missing bearer token.
        resp = client.post(
            "/api/agent/runs/1/heartbeat",
            json={"slot": 0, "protocol_version": 1},
        )
        assert resp.status_code == 401, _body_text(resp)
        detail = resp.json().get("detail")
        # The agent router phrases its 401 in terms of the bearer/run token; the
        # session middleware phrases it as "authentication required" (see
        # _MIDDLEWARE_401_DETAIL). Asserting the agent phrasing AND the absence of
        # the middleware phrasing proves the request reached the agent layer, i.e.
        # the agent path is exempt from the session guard.
        assert isinstance(detail, str), resp.json()
        low = detail.lower()
        assert ("bearer" in low) or ("run token" in low) or ("token" in low), (
            "expected the agent JWT rejection, got: %r" % detail
        )
        assert _MIDDLEWARE_401_DETAIL not in low, (
            "agent path was intercepted by the session middleware: %r" % detail
        )


def test_github_webhook_is_exempt_from_session_auth(settings, fake_driver):
    """POST /api/hooks/github is reachable without a session and returns the
    webhook's own HMAC 401 (invalid/unrecognised signature), not a session-401.
    """
    auth_settings = _make_auth_settings(
        settings,
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PASSWORD,
        trusted_proxies="",
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    with _build_client(auth_settings, fake_driver) as client:
        _skip_if_no_routes(client)

        # No session, a bogus signature: must reach the webhook handler and be
        # rejected on the HMAC, proving the path is exempt from session auth.
        resp = client.post(
            "/api/hooks/github",
            headers={
                "X-Hub-Signature-256": "sha256=" + ("0" * 64),
                "X-GitHub-Event": "push",
            },
            content=b'{"zen": "keep it simple"}',
        )
        assert resp.status_code == 401, _body_text(resp)
        detail = str(resp.json().get("detail", "")).lower()
        # The webhook's own phrasing (signature), never the session middleware's
        # "authentication required" — the latter would mean the guard intercepted
        # the webhook instead of letting it verify its HMAC.
        assert "signature" in detail, (
            "expected the webhook HMAC rejection, got: %r" % resp.json()
        )
        assert _MIDDLEWARE_401_DETAIL not in detail, (
            "webhook path was intercepted by the session middleware: %r" % resp.json()
        )


# --------------------------------------------------------------------------- #
# 5. Trusted-proxy SSO — the security-critical trust model.
# --------------------------------------------------------------------------- #

def test_trusted_proxy_header_authenticates_user(settings, fake_driver):
    """A request whose immediate peer is inside STOKER_TRUSTED_PROXIES and which
    carries STOKER_AUTH_HEADER=alice authenticates as alice, created source=proxy
    with the default proxy role. The peer is simulated via the TestClient
    ``client=`` tuple (request.client.host), NOT via a header a client could set.
    """
    auth_settings = _make_auth_settings(
        settings,
        admin_user=None,
        admin_password=None,
        trusted_proxies=TRUSTED_CIDR,
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    # The immediate peer (10.10.0.7) is inside the trusted CIDR.
    with _build_client(auth_settings, fake_driver, peer=TRUSTED_PEER) as client:
        _skip_if_no_routes(client)

        # A protected ops route, no session cookie, but the proxy asserts the user.
        resp = client.get(PROTECTED_OPS_PATH, headers={AUTH_HEADER: "alice"})
        assert resp.status_code == 200, (
            "trusted proxy asserting %s=alice must authenticate; got %s: %s"
            % (AUTH_HEADER, resp.status_code, _body_text(resp))
        )

        # /me reflects the proxy-asserted identity, and the user was persisted
        # created-on-first-sight with source=proxy and the default role.
        me = client.get(ME_PATH, headers={AUTH_HEADER: "alice"})
        assert me.status_code == 200, _body_text(me)
        me_body = me.json()
        identity_values = {str(v) for v in me_body.values() if isinstance(v, str)}
        assert "alice" in identity_values, me_body
        assert me_body.get("role") == PROXY_DEFAULT_ROLE, me_body
        assert me_body.get("source") == "proxy", me_body
        _assert_no_secret_leak(me)

        if AUTH_USER_READY:
            from sqlalchemy import select

            from server.models import User

            with db_mod.SessionLocal() as db:
                rows = list(db.execute(select(User)).scalars().all())
            proxy_users = [u for u in rows if getattr(u, "source", None) == "proxy"]
            assert proxy_users, "the proxy-asserted user must be persisted"
            alice = proxy_users[0]
            assert alice.role == PROXY_DEFAULT_ROLE
            # A proxy user has no local password (SSO identity, no local login).
            assert not getattr(alice, "password_hash", None)


def test_untrusted_peer_auth_header_is_ignored(settings, fake_driver):
    """SECURITY: the SAME STOKER_AUTH_HEADER from an UNTRUSTED immediate peer is
    ignored. A client-supplied auth header must never authenticate. This must be
    a *failure* (not a skip) once auth is wired: a broken trust boundary cannot
    pass silently.
    """
    auth_settings = _make_auth_settings(
        settings,
        admin_user=None,
        admin_password=None,
        trusted_proxies=TRUSTED_CIDR,
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    # The immediate peer (203.0.113.9) is OUTSIDE every trusted CIDR.
    with _build_client(auth_settings, fake_driver, peer=UNTRUSTED_PEER) as client:
        _skip_if_no_routes(client)

        # Same header, untrusted peer: the header must be ignored -> 401.
        resp = client.get(PROTECTED_OPS_PATH, headers={AUTH_HEADER: "mallory"})
        assert resp.status_code == 401, (
            "SECURITY FAILURE: an untrusted peer's %s header authenticated "
            "(status %s). A client-supplied auth header must never be trusted."
            % (AUTH_HEADER, resp.status_code)
        )

        # And nothing must be forged: 'mallory' must not have been created.
        if AUTH_USER_READY:
            from sqlalchemy import select

            from server.models import User

            with db_mod.SessionLocal() as db:
                rows = list(db.execute(select(User)).scalars().all())
            names = set()
            for u in rows:
                names.update(str(getattr(u, attr, "")) for attr in ("email", "username"))
            assert "mallory" not in names, (
                "an untrusted peer's asserted user was created: trust boundary breached"
            )


def test_spoofed_forwarded_for_does_not_grant_trust(settings, fake_driver):
    """A client cannot smuggle trust by faking X-Forwarded-For to a trusted IP:
    the trust decision keys on the transport peer (request.client.host), so a
    spoofed XFF from an untrusted peer is still ignored.
    """
    auth_settings = _make_auth_settings(
        settings,
        admin_user=None,
        admin_password=None,
        trusted_proxies=TRUSTED_CIDR,
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    with _build_client(auth_settings, fake_driver, peer=UNTRUSTED_PEER) as client:
        _skip_if_no_routes(client)

        # Untrusted transport peer, but the client fakes XFF to a trusted IP and
        # sets the auth header. This must NOT authenticate.
        resp = client.get(
            PROTECTED_OPS_PATH,
            headers={
                "X-Forwarded-For": "%s, 198.51.100.4" % TRUSTED_PEER[0],
                AUTH_HEADER: "mallory",
            },
        )
        assert resp.status_code == 401, (
            "SECURITY FAILURE: a spoofed X-Forwarded-For granted trust to an "
            "untrusted transport peer (status %s)." % resp.status_code
        )


# --------------------------------------------------------------------------- #
# 6. Role enforcement (admin-only user management + last-admin / self guards).
# --------------------------------------------------------------------------- #

def _create_user_as_admin(client, email, password, role):
    # type: (Any, str, str, str) -> Any
    """Admin-side user creation (POST /api/users), tolerant of the identity key."""
    return client.post(
        USERS_PATH,
        json={"username": email, "email": email, "password": password, "role": role},
    )


def test_non_admin_cannot_manage_users_admin_can(settings, fake_driver):
    """A non-admin session cannot list users (403); an admin session can (200)."""
    if not AUTH_USER_READY:
        pytest.skip("User model with (id,email,role,source) not present yet")

    auth_settings = _make_auth_settings(
        settings,
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PASSWORD,
        trusted_proxies="",
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    with _build_client(auth_settings, fake_driver) as client:
        _skip_if_no_routes(client)

        # Admin logs in and lists users -> allowed.
        assert _login(client, ADMIN_USER, ADMIN_PASSWORD).status_code in (200, 204)
        listed = client.get(USERS_PATH)
        assert listed.status_code == 200, _body_text(listed)
        _assert_no_secret_leak(listed)

        # Admin creates a non-admin operator user.
        operator_pw = "operator-pw-abcdef-123456"
        created = _create_user_as_admin(client, "op@example.test", operator_pw, "operator")
        assert created.status_code in (200, 201), _body_text(created)
        _assert_no_secret_leak(created)

        # Log out of admin, log in as the operator.
        client.post(LOGOUT_PATH)
        client.cookies.clear()
        assert _login(client, "op@example.test", operator_pw).status_code in (200, 204)

        # The operator cannot list/manage users -> 403 (authenticated but
        # unauthorised), distinct from the 401 an anonymous caller gets.
        forbidden = client.get(USERS_PATH)
        assert forbidden.status_code == 403, _body_text(forbidden)


def test_cannot_delete_last_admin_or_self(settings, fake_driver):
    """The last remaining admin cannot be deleted, and an admin cannot delete
    their own account (both guarded, so the system is never left admin-less or
    an operator locked out of their own session mid-request).
    """
    if not AUTH_USER_READY:
        pytest.skip("User model with (id,email,role,source) not present yet")

    auth_settings = _make_auth_settings(
        settings,
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PASSWORD,
        trusted_proxies="",
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    with _build_client(auth_settings, fake_driver) as client:
        _skip_if_no_routes(client)
        assert _login(client, ADMIN_USER, ADMIN_PASSWORD).status_code in (200, 204)

        from sqlalchemy import select

        from server.models import User

        with db_mod.SessionLocal() as db:
            admins = list(db.execute(select(User).where(User.role == "admin")).scalars().all())
        assert admins, "bootstrap admin must exist"
        admin_id = admins[0].id

        # Deleting the last admin (also self here) must be refused. Accept either
        # the last-admin guard or the self-delete guard: both are 4xx refusals
        # that leave the admin in place.
        resp = client.delete("%s/%s" % (USERS_PATH, admin_id))
        assert resp.status_code in (400, 403, 409), (
            "deleting the last admin / self must be refused, got %s: %s"
            % (resp.status_code, _body_text(resp))
        )

        # The admin is still present after the refused delete.
        with db_mod.SessionLocal() as db:
            still = list(db.execute(select(User).where(User.role == "admin")).scalars().all())
        assert still, "the last admin must survive a refused delete"


# --------------------------------------------------------------------------- #
# 7. No-secret-in-body: an authenticated /me and the users list never carry the
#    password hash or the master key. (Standalone, model-agnostic guard.)
# --------------------------------------------------------------------------- #

def test_no_secret_material_in_auth_bodies(settings, fake_driver):
    """No auth response body contains password_hash or the Fernet master key."""
    auth_settings = _make_auth_settings(
        settings,
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PASSWORD,
        trusted_proxies="",
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    master_key = auth_settings.master_key
    with _build_client(auth_settings, fake_driver) as client:
        _skip_if_no_routes(client)
        assert _login(client, ADMIN_USER, ADMIN_PASSWORD).status_code in (200, 204)

        for path in (ME_PATH, USERS_PATH, STATUS_PATH):
            resp = client.get(path)
            if resp.status_code >= 400:
                # Not every build exposes every endpoint yet; only assert on the
                # bodies we can actually read.
                continue
            text = _body_text(resp)
            assert "password_hash" not in text, "%s leaked password_hash" % path
            assert master_key not in text, "%s leaked the master key" % path


# --------------------------------------------------------------------------- #
# 8. Trusted-proxy configurability: non-default role + a custom header name +
#    an inactive proxy user is locked out. (Extends section 5; the prompt calls
#    out STOKER_PROXY_DEFAULT_ROLE and "X-Forwarded-User / Remote-User".)
# --------------------------------------------------------------------------- #

def test_proxy_default_role_applies_a_non_default_role(settings, fake_driver):
    """A proxy user's first-sight role is STOKER_PROXY_DEFAULT_ROLE verbatim.

    Section 5 covers the "operator" default; here the configured role is the
    least-privileged ``viewer`` to prove the value is honoured (not hard-coded)
    and validated (a bad role would have failed at ``load_settings``).
    """
    auth_settings = _make_auth_settings(
        settings,
        admin_user=None,
        admin_password=None,
        trusted_proxies=TRUSTED_CIDR,
        auth_header=AUTH_HEADER,
        proxy_default_role="viewer",
    )
    with _build_client(auth_settings, fake_driver, peer=TRUSTED_PEER) as client:
        _skip_if_no_routes(client)
        me = client.get(ME_PATH, headers={AUTH_HEADER: "bob"})
        assert me.status_code == 200, _body_text(me)
        assert me.json().get("role") == "viewer", me.json()
        assert me.json().get("source") == "proxy", me.json()


def test_trusted_proxy_honours_custom_auth_header_name(settings, fake_driver):
    """STOKER_AUTH_HEADER selects which header carries the username.

    With the header configured as ``Remote-User``, a trusted peer asserting
    ``Remote-User: carol`` authenticates, while the default ``X-Forwarded-User``
    is inert (it is no longer the configured header).
    """
    custom_header = "Remote-User"
    auth_settings = _make_auth_settings(
        settings,
        admin_user=None,
        admin_password=None,
        trusted_proxies=TRUSTED_CIDR,
        auth_header=custom_header,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    with _build_client(auth_settings, fake_driver, peer=TRUSTED_PEER) as client:
        _skip_if_no_routes(client)

        # The default header is NOT the configured one now: it must be ignored.
        ignored = client.get(ME_PATH, headers={"X-Forwarded-User": "not-carol"})
        assert ignored.status_code == 401, _body_text(ignored)

        # The configured header authenticates.
        me = client.get(ME_PATH, headers={custom_header: "carol"})
        assert me.status_code == 200, _body_text(me)
        identity_values = {str(v) for v in me.json().values() if isinstance(v, str)}
        assert "carol" in identity_values, me.json()


def test_inactive_proxy_user_is_locked_out(settings, fake_driver):
    """A deactivated proxy account cannot act, even from a trusted peer.

    Deactivation is a lockout regardless of identity source: an ``active=False``
    proxy user resolving via the header must be refused (401), so an admin can
    revoke an SSO identity without deleting its audit trail.
    """
    if not AUTH_USER_READY:
        pytest.skip("User model with (id,email,role,source) not present yet")

    auth_settings = _make_auth_settings(
        settings,
        admin_user=None,
        admin_password=None,
        trusted_proxies=TRUSTED_CIDR,
        auth_header=AUTH_HEADER,
        proxy_default_role=PROXY_DEFAULT_ROLE,
    )
    with _build_client(auth_settings, fake_driver, peer=TRUSTED_PEER) as client:
        _skip_if_no_routes(client)

        # Seed a deactivated proxy user directly.
        from server.models import User

        with db_mod.SessionLocal() as db:
            db.add(User(
                username="dan", password_hash=None, role="operator",
                source="proxy", active=False))
            db.commit()

        # The trusted proxy asserts 'dan', but the account is inactive -> 401.
        resp = client.get(ME_PATH, headers={AUTH_HEADER: "dan"})
        assert resp.status_code == 401, _body_text(resp)


def test_role_gate_viewer_is_read_only_operator_can_write(settings, fake_driver):
    # Review (medium): the operator API must enforce roles — a viewer can read
    # but not mutate; only operator+ may write; /api/users is admin-only.
    s = _make_auth_settings(settings, admin_user="root", admin_password="rootpw12345")
    with _build_client(s, fake_driver) as admin_c:
        _skip_if_no_routes(admin_c)
        assert _login(admin_c, "root", "rootpw12345").status_code == 200
        assert admin_c.post("/api/users", json={
            "username": "val", "password": "viewerpw12345", "role": "viewer"}).status_code == 201
        assert admin_c.post("/api/users", json={
            "username": "opie", "password": "operatorpw12345", "role": "operator"}).status_code == 201

    with _build_client(s, fake_driver) as vc:
        assert _login(vc, "val", "viewerpw12345").status_code == 200
        assert vc.get("/api/targets").status_code == 200                     # read ok
        assert vc.post("/api/targets", json={
            "name": "x", "hec_url": "http://h:8088", "token": "t"}).status_code == 403  # write denied
        assert vc.delete("/api/targets/1").status_code == 403
        assert vc.get("/api/users").status_code == 403                        # admin only

    with _build_client(s, fake_driver) as oc:
        assert _login(oc, "opie", "operatorpw12345").status_code == 200
        r = oc.post("/api/targets", json={
            "name": "opx", "hec_url": "http://h:8088", "token": "t", "env_tag": "lab"})
        assert r.status_code != 403                                          # operator may write
        assert oc.get("/api/users").status_code == 403                        # still not admin


def test_auth_exempt_matches_only_on_segment_boundary():
    # Review (low): a sibling path must NOT inherit an exempt prefix's exemption.
    from server.app import _is_auth_exempt
    assert _is_auth_exempt("/api/agent/runs/1/claim") is True
    assert _is_auth_exempt("/api/hooks/github") is True
    assert _is_auth_exempt("/api/auth/login") is True
    assert _is_auth_exempt("/api/agents") is False
    assert _is_auth_exempt("/api/hooks-admin") is False
    assert _is_auth_exempt("/api/auth/status-internal") is False
    assert _is_auth_exempt("/api/targets") is False


def test_session_cookie_secure_follows_request_scheme(settings, fake_driver):
    # Review (low): Secure must follow the browser hop (X-Forwarded-Proto), not
    # the worker-facing PUBLIC_BASE_URL (which is plain http in the deploy).
    s = _make_auth_settings(settings, admin_user="root", admin_password="rootpw12345")
    with _build_client(s, fake_driver) as c:
        _skip_if_no_routes(c)
        https = c.post(LOGIN_PATH, json={"username": "root", "password": "rootpw12345"},
                       headers={"X-Forwarded-Proto": "https"})
        assert https.status_code == 200
        assert "secure" in (https.headers.get("set-cookie", "").lower())
    with _build_client(s, fake_driver) as c2:
        plain = c2.post(LOGIN_PATH, json={"username": "root", "password": "rootpw12345"})
        assert plain.status_code == 200
        assert "secure" not in (plain.headers.get("set-cookie", "").lower())
