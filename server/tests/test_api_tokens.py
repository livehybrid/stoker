"""API-token subsystem tests for the Stoker control plane.

Covers the two things the maintainer added on top of the existing app-level auth:

* **Token management (`/api/tokens`, admin only)**: `POST` returns an `stk_`
  secret exactly once; `GET` lists metadata but never the secret or its hash; the
  secret is unretrievable after create; a non-admin (operator token or operator
  session) gets 403; `DELETE` soft-revokes.
* **Token authentication (`Authorization: Bearer stk_...`)**: an operator token
  can GET a guarded route and do an operator mutation but is 403 on `/api/users`
  and `/api/tokens`; a viewer token GETs but is 403 on a mutation; an admin token
  has full access. An expired token, a revoked token and a non-`stk_` bearer each
  resolve to anonymous (401 on a guarded route). `last_used_at` is set after a
  token auth. The OpenAPI spec exposes `securitySchemes.bearerAuth` and the
  `/api/tokens` path, and `/docs` is reachable through the middleware.

The app is built exactly like ``test_auth.py``: an auth-configured ``Settings``
produced by the real :func:`server.config.load_settings` parser and grafted onto
the ``settings`` fixture's isolated temp DB, with a bootstrap admin so auth is
active. Token secrets are created through the API (as a real operator would) and
then presented as bearer credentials.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import json
from typing import Any, Optional

from server import config as config_mod
from server import db as db_mod
from server import drivers as drivers_mod
from server.config import Settings, load_settings

# --------------------------------------------------------------------------- #
# Contract constants.
# --------------------------------------------------------------------------- #

ADMIN_USER = "root-admin"
ADMIN_PASSWORD = "corr3ct-h0rse-battery-staple"  # noqa: S105 (test fixture value)

LOGIN_PATH = "/api/auth/login"
ME_PATH = "/api/auth/me"
USERS_PATH = "/api/users"
TOKENS_PATH = "/api/tokens"
GUARDED_GET_PATH = "/api/targets"      # a representative guarded GET (viewer+)
OPENAPI_PATH = "/openapi.json"
DOCS_PATH = "/docs"

# Secret substrings that must never appear in a token-listing / metadata body.
_FORBIDDEN_BODY_SUBSTRINGS = ("token_hash", "password_hash", "master_key")


# --------------------------------------------------------------------------- #
# App builder (mirrors test_auth.py).
# --------------------------------------------------------------------------- #

def _make_auth_settings(base, admin_user=None, admin_password=None):
    # type: (Settings, Optional[str], Optional[str]) -> Settings
    """Auth-configured Settings via the real parser, on the fixture's temp DB."""
    env = {
        "STOKER_MASTER_KEY": base.master_key,
        "PUBLIC_BASE_URL": base.public_base_url,
    }
    if admin_user is not None:
        env["STOKER_ADMIN_USER"] = admin_user
    if admin_password is not None:
        env["STOKER_ADMIN_PASSWORD"] = admin_password
    parsed = load_settings(env=env)
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
    config_mod.set_settings(settings)
    db_mod.configure(settings.database_url)
    db_mod.create_all()


def _build_client(settings, fake_driver):
    # type: (Settings, Any) -> Any
    """Build the app against ``settings`` and return a lifespan-run TestClient."""
    from fastapi.testclient import TestClient

    from server.app import create_app

    _install(settings)
    drivers_mod.clear_cache()
    drivers_mod.register_driver("fake-local", fake_driver)
    drivers_mod.register_driver("swarm-local", fake_driver)
    return TestClient(create_app())


def _body_text(resp):
    # type: (Any) -> str
    try:
        return json.dumps(resp.json())
    except ValueError:
        return resp.text or ""


def _assert_no_secret_leak(resp):
    # type: (Any) -> None
    text = _body_text(resp)
    for needle in _FORBIDDEN_BODY_SUBSTRINGS:
        assert needle not in text, "response leaked %r: %s" % (needle, text[:200])


@contextlib.contextmanager
def _admin_client(settings, fake_driver):
    # type: (Settings, Any) -> Any
    """A lifespan-run TestClient logged in as the bootstrap admin.

    The bootstrap admin is created in the app lifespan, which only runs once the
    ``TestClient`` context is entered, so the login must happen *inside* the
    ``with``. This context manager enters the client, logs in, and yields the
    authenticated client.
    """
    with _build_client(settings, fake_driver) as client:
        ok = client.post(LOGIN_PATH, json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        assert ok.status_code == 200, _body_text(ok)
        yield client


def _bearer(token):
    # type: (str) -> dict
    return {"Authorization": "Bearer %s" % token}


def _mint_token(client, name, role, expires_in_days=None):
    # type: (Any, str, str, Optional[int]) -> Any
    """Create a token via the admin API and return the response."""
    body = {"name": name, "role": role}  # type: dict
    if expires_in_days is not None:
        body["expires_in_days"] = expires_in_days
    return client.post(TOKENS_PATH, json=body)


def _make_target_body(name):
    # type: (str) -> dict
    """A minimal valid POST /api/targets body (an operator mutation)."""
    return {
        "name": name,
        "hec_url": "https://hec.example:8088",
        "token": "hec-secret-not-echoed",
        "env_tag": "lab",
        "verify_tls": True,
    }


# --------------------------------------------------------------------------- #
# 1. Token management: create returns a secret once; list is metadata only.
# --------------------------------------------------------------------------- #

def test_create_returns_secret_once_and_list_is_metadata_only(settings, fake_driver):
    """POST returns an `stk_` secret + prefix; GET lists metadata but never the
    secret or hash; the secret is unretrievable after create."""
    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    with _admin_client(auth_settings, fake_driver) as client:
        created = _mint_token(client, "ci-deploy", "operator")
        assert created.status_code == 201, _body_text(created)
        body = created.json()

        secret = body["token"]
        assert secret.startswith("stk_"), body
        assert body["prefix"] == secret[:12]
        assert body["role"] == "operator"
        assert body["name"] == "ci-deploy"
        assert "id" in body

        # List: metadata only, never the secret or the hash.
        listed = client.get(TOKENS_PATH)
        assert listed.status_code == 200, _body_text(listed)
        rows = listed.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "ci-deploy"
        assert row["role"] == "operator"
        assert row["prefix"] == secret[:12]
        # No secret / hash field anywhere in the listing.
        assert "token" not in row
        assert "token_hash" not in row
        assert secret not in _body_text(listed)
        _assert_no_secret_leak(listed)

        # The plaintext is unretrievable after create: it appears in no GET body.
        assert secret not in _body_text(client.get(TOKENS_PATH))


def test_duplicate_token_name_conflicts(settings, fake_driver):
    """A duplicate token name is 409."""
    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    with _admin_client(auth_settings, fake_driver) as client:
        assert _mint_token(client, "dupe", "viewer").status_code == 201
        assert _mint_token(client, "dupe", "viewer").status_code == 409


# --------------------------------------------------------------------------- #
# 2. Token authentication + role enforcement.
# --------------------------------------------------------------------------- #

def test_operator_token_can_read_and_mutate_but_not_admin_surfaces(settings, fake_driver):
    """An operator token GETs a guarded route AND does an operator mutation, but
    is 403 on both /api/users and /api/tokens (admin-only surfaces)."""
    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    with _admin_client(auth_settings, fake_driver) as client:
        secret = _mint_token(client, "op-token", "operator").json()["token"]

    # A fresh client with NO session cookie: identity comes purely from the token.
    with _build_client(auth_settings, fake_driver) as anon:
        hdr = _bearer(secret)

        # Guarded GET (viewer+) succeeds.
        got = anon.get(GUARDED_GET_PATH, headers=hdr)
        assert got.status_code == 200, _body_text(got)

        # Operator mutation succeeds.
        created = anon.post(GUARDED_GET_PATH, json=_make_target_body("t-op"), headers=hdr)
        assert created.status_code == 201, _body_text(created)

        # Admin-only surfaces are 403 for an operator token.
        assert anon.get(USERS_PATH, headers=hdr).status_code == 403
        assert anon.get(TOKENS_PATH, headers=hdr).status_code == 403
        assert anon.post(
            TOKENS_PATH, json={"name": "nope", "role": "admin"}, headers=hdr
        ).status_code == 403


def test_viewer_token_reads_but_cannot_mutate(settings, fake_driver):
    """A viewer token GETs a guarded route but is 403 on an operator mutation."""
    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    with _admin_client(auth_settings, fake_driver) as client:
        secret = _mint_token(client, "ro-token", "viewer").json()["token"]

    with _build_client(auth_settings, fake_driver) as anon:
        hdr = _bearer(secret)
        assert anon.get(GUARDED_GET_PATH, headers=hdr).status_code == 200
        mutate = anon.post(GUARDED_GET_PATH, json=_make_target_body("t-ro"), headers=hdr)
        assert mutate.status_code == 403, _body_text(mutate)


def test_admin_token_has_full_access(settings, fake_driver):
    """An admin token can hit the admin-only surfaces (list users + tokens) and
    mint another token."""
    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    with _admin_client(auth_settings, fake_driver) as client:
        secret = _mint_token(client, "admin-token", "admin").json()["token"]

    with _build_client(auth_settings, fake_driver) as anon:
        hdr = _bearer(secret)
        assert anon.get(GUARDED_GET_PATH, headers=hdr).status_code == 200
        assert anon.get(USERS_PATH, headers=hdr).status_code == 200
        assert anon.get(TOKENS_PATH, headers=hdr).status_code == 200
        # Full access includes minting another token.
        minted = anon.post(TOKENS_PATH, json={"name": "second", "role": "viewer"}, headers=hdr)
        assert minted.status_code == 201, _body_text(minted)

        # /api/auth/me reports the transient token principal (id is null, source
        # is "token", username is token:<name>) without erroring on serialisation.
        me = anon.get(ME_PATH, headers=hdr)
        assert me.status_code == 200, _body_text(me)
        me_body = me.json()
        assert me_body["role"] == "admin"
        assert me_body["source"] == "token"
        assert me_body["username"] == "token:admin-token"
        assert me_body["id"] is None


# --------------------------------------------------------------------------- #
# 3. Invalid tokens resolve to anonymous (401 on a guarded route).
# --------------------------------------------------------------------------- #

def test_expired_revoked_and_non_stk_bearers_are_anonymous(settings, fake_driver):
    """An expired token, a revoked token, and a non-`stk_` bearer each get 401 on
    a guarded route (they resolve to no principal)."""
    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    with _admin_client(auth_settings, fake_driver) as client:
        good = _mint_token(client, "revoke-me", "operator").json()
        good_secret, good_id = good["token"], good["id"]
        expired_secret = _mint_token(client, "expiring", "operator", expires_in_days=1).json()["token"]

    from sqlalchemy import select

    from server.models import ApiToken, utcnow

    # Force the "expiring" token into the past and revoke the "revoke-me" token
    # directly in the DB (revocation is also exercised via the API below).
    with db_mod.SessionLocal() as db:
        exp = db.execute(select(ApiToken).where(ApiToken.name == "expiring")).scalars().first()
        exp.expires_at = utcnow() - datetime.timedelta(seconds=1)
        rev = db.get(ApiToken, good_id)
        rev.revoked_at = utcnow()
        db.commit()

    with _build_client(auth_settings, fake_driver) as anon:
        # Expired -> anonymous.
        assert anon.get(GUARDED_GET_PATH, headers=_bearer(expired_secret)).status_code == 401
        # Revoked -> anonymous.
        assert anon.get(GUARDED_GET_PATH, headers=_bearer(good_secret)).status_code == 401
        # A non-stk bearer (looks like a run JWT) -> anonymous, never a 500.
        jwt_like = "eyJhbGciOiJIUzI1NiJ9.eyJydW5faWQiOjF9.sig"
        assert anon.get(GUARDED_GET_PATH, headers=_bearer(jwt_like)).status_code == 401
        # A garbage stk_ token -> anonymous.
        assert anon.get(GUARDED_GET_PATH, headers=_bearer("stk_notarealtoken")).status_code == 401


def test_revoke_via_api_disables_the_token(settings, fake_driver):
    """DELETE /api/tokens/{id} soft-revokes: 204, revoked_at set, token stops
    authenticating, and the audit row survives in the listing."""
    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    with _admin_client(auth_settings, fake_driver) as client:
        created = _mint_token(client, "kill-via-api", "operator").json()
        secret, tid = created["token"], created["id"]

        # It works before revocation (bearer against the same client).
        assert client.get(GUARDED_GET_PATH, headers=_bearer(secret)).status_code == 200

        # Soft-revoke.
        deleted = client.delete("%s/%s" % (TOKENS_PATH, tid))
        assert deleted.status_code == 204, _body_text(deleted)

        # The audit row survives with revoked_at populated.
        rows = client.get(TOKENS_PATH).json()
        row = next(r for r in rows if r["id"] == tid)
        assert row["revoked_at"] is not None

        # It no longer authenticates.
        assert client.get(GUARDED_GET_PATH, headers=_bearer(secret)).status_code == 401

        # Deleting again is idempotent (still 204); unknown id is 404.
        assert client.delete("%s/%s" % (TOKENS_PATH, tid)).status_code == 204
        assert client.delete("%s/999999" % TOKENS_PATH).status_code == 404


# --------------------------------------------------------------------------- #
# 4. Token management itself requires admin.
# --------------------------------------------------------------------------- #

def test_token_management_requires_admin(settings, fake_driver):
    """An operator (token or session) is 403 on every /api/tokens management
    call; only an admin may manage tokens."""
    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    with _admin_client(auth_settings, fake_driver) as client:
        op_secret = _mint_token(client, "op-mgmt", "operator").json()["token"]
        # Seed one token for the operator to (fail to) list/delete.
        target = _mint_token(client, "victim", "viewer").json()
        victim_id = target["id"]

    with _build_client(auth_settings, fake_driver) as anon:
        hdr = _bearer(op_secret)
        assert anon.get(TOKENS_PATH, headers=hdr).status_code == 403
        assert anon.post(TOKENS_PATH, json={"name": "x", "role": "viewer"}, headers=hdr).status_code == 403
        assert anon.delete("%s/%s" % (TOKENS_PATH, victim_id), headers=hdr).status_code == 403


# --------------------------------------------------------------------------- #
# 5. last_used_at is set after a token auth.
# --------------------------------------------------------------------------- #

def test_last_used_at_is_set_after_token_auth(settings, fake_driver):
    """last_used_at is null on a fresh token and populated once it authenticates."""
    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    with _admin_client(auth_settings, fake_driver) as client:
        created = _mint_token(client, "used-token", "viewer").json()
        tid = created["id"]

        # Fresh token: never used.
        rows = client.get(TOKENS_PATH).json()
        assert next(r for r in rows if r["id"] == tid)["last_used_at"] is None

        # Authenticate with it against a guarded route.
        assert client.get(GUARDED_GET_PATH, headers=_bearer(created["token"])).status_code == 200

    # last_used_at is now populated in the DB.
    from server.models import ApiToken

    with db_mod.SessionLocal() as db:
        assert db.get(ApiToken, tid).last_used_at is not None


# --------------------------------------------------------------------------- #
# 6. OpenAPI / Swagger: bearerAuth scheme + /api/tokens path + reachable docs.
# --------------------------------------------------------------------------- #

def test_openapi_documents_bearer_scheme_and_tokens_path(settings, fake_driver):
    """/openapi.json is 200 and contains securitySchemes.bearerAuth and the
    /api/tokens path; /docs is 200 (reachable through the middleware)."""
    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    with _build_client(auth_settings, fake_driver) as anon:
        spec_resp = anon.get(OPENAPI_PATH)
        assert spec_resp.status_code == 200, _body_text(spec_resp)
        spec = spec_resp.json()

        schemes = spec.get("components", {}).get("securitySchemes", {})
        assert "bearerAuth" in schemes, schemes
        assert schemes["bearerAuth"]["type"] == "http"
        assert schemes["bearerAuth"]["scheme"] == "bearer"
        assert spec.get("security") == [{"bearerAuth": []}]

        assert TOKENS_PATH in spec.get("paths", {}), sorted(spec.get("paths", {}))

        # /docs (Swagger UI) is reachable and not gated behind auth.
        docs = anon.get(DOCS_PATH)
        assert docs.status_code == 200, docs.status_code


# --------------------------------------------------------------------------- #
# 7. Attribution: a token-launched run records started_by = token:<name>.
# --------------------------------------------------------------------------- #

def test_token_launched_run_is_attributed_to_the_token(settings, fake_driver):
    """A run launched with an operator token records started_by='token:<name>'
    (not the generic 'operator'), so a CI/CD action is attributable in the audit
    trail. Guards the fix for the middleware discarding the resolved caller."""
    import os

    from . import _helpers

    auth_settings = _make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD)
    pack_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "packs", "flatline")

    with _admin_client(auth_settings, fake_driver) as client:
        op_secret = _mint_token(client, "ci-run", "operator").json()["token"]

    # Seed a green target + a real on-disk pack + a spec in the same DB.
    with db_mod.SessionLocal() as db:
        target = _helpers.make_target(db, name="attrib-target", settings=auth_settings)
        pack = _helpers.make_pack(db, pack_dir, name="attrib-pack")
        spec = _helpers.make_spec(
            db, pack, target, name="attrib-spec", engine="eventgen",
            rate_mode="eps", rate_value=100.0, workers=1, fleet="fake-local")
        db.commit()
        spec_id = spec.id

    # Launch it with ONLY the operator token (no session cookie).
    with _build_client(auth_settings, fake_driver) as anon:
        launched = anon.post("/api/specs/%d/run" % spec_id, headers=_bearer(op_secret), json={})
        assert launched.status_code == 201, _body_text(launched)
        run_id = launched.json()["run_id"]

        run = anon.get("/api/runs/%d" % run_id, headers=_bearer(op_secret))
        assert run.status_code == 200, _body_text(run)
        assert run.json()["started_by"] == "token:ci-run", run.json()

        # The audit trail's operator-initiated events carry the same actor.
        events = anon.get("/api/runs/%d/events" % run_id, headers=_bearer(op_secret)).json()
        assert any(e.get("actor") == "token:ci-run" for e in events), events
