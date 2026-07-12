"""App-level authentication: local password users + trusted-proxy SSO.

This module owns the whole auth backend and is deliberately vendor-neutral: it
has no dependency on authentik or any specific IdP. Two identity sources feed
one :class:`~server.models.User` table:

* **Local users** — a username + a passlib **bcrypt** ``password_hash``. A
  successful ``POST /api/auth/login`` mints a signed, TTL-bounded session cookie
  (``itsdangerous`` keyed off ``STOKER_MASTER_KEY``, domain-separated from the
  Fernet / run-JWT uses of the same key). The hash is the only secret on the row
  and is never serialised or logged.

* **Trusted-proxy (SSO) users** — a reverse proxy (e.g. Traefik forward-auth to
  authentik) asserts the authenticated username in a configured header. The
  **trust model is strict**: the header is honoured *only* when the immediate
  peer (``request.client.host`` — the proxy itself) falls inside one of
  ``STOKER_TRUSTED_PROXIES``. A request from an untrusted peer has that header
  **ignored** — a client can never spoof an identity by sending the header
  directly. A proxy-asserted user is created on first sight (``source="proxy"``,
  role from ``STOKER_PROXY_DEFAULT_ROLE``).

The public surface: :func:`resolve_user` (the shared resolver used by both the
FastAPI dependencies and the request middleware), the dependencies
:func:`current_user` / :func:`require_user` / :func:`require_admin`, session
helpers :func:`issue_session` / :func:`read_session`, password helpers
:func:`hash_password` / :func:`verify_password`, and startup helpers
:func:`bootstrap_admin` / :func:`setup_needed`.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_db
from .models import User, utcnow

log = logging.getLogger("stoker.auth")

# Name of the signed session cookie. HttpOnly + SameSite=Lax; Secure is set when
# the public base URL is https (see cookie_kwargs).
SESSION_COOKIE = "stoker_session"

# itsdangerous salt: domain-separates the session signer from every other use of
# the master key (Fernet secret box, run-JWT HMAC). A leaked session token is
# useless for those and vice-versa.
_SESSION_SALT = "stoker-session-v1"

# passlib context. bcrypt only; ``deprecated="auto"`` lets us re-hash on login if
# the scheme list ever changes. Built lazily so importing this module never
# triggers backend probing before it is needed.
_pwd_context = None  # type: Optional[CryptContext]


def _context():
    # type: () -> CryptContext
    global _pwd_context
    if _pwd_context is None:
        _pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    return _pwd_context


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #

def hash_password(password):
    # type: (str) -> str
    """Return a passlib bcrypt hash of ``password`` (never the plaintext).

    bcrypt caps the effective password at 72 bytes; passlib truncates rather
    than raising, which is the documented, compatible behaviour.
    """
    if not password:
        raise ValueError("password must not be empty")
    return _context().hash(password)


def verify_password(password, password_hash):
    # type: (str, Optional[str]) -> bool
    """Constant-time verify ``password`` against a stored hash.

    Returns False (never raises) for a null hash (a proxy/SSO user has no
    password) or a malformed stored value, so a caller can treat "no local
    credential" and "wrong password" identically without leaking which.
    """
    if not password_hash:
        return False
    try:
        return _context().verify(password, password_hash)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Session cookie (signed, TTL-bounded)
# --------------------------------------------------------------------------- #

def _serializer(settings=None):
    # type: (Optional[Settings]) -> URLSafeTimedSerializer
    if settings is None:
        settings = get_settings()
    return URLSafeTimedSerializer(
        settings.master_key, salt=_SESSION_SALT)


def issue_session(user, settings=None):
    # type: (User, Optional[Settings]) -> str
    """Return the signed cookie value carrying ``user``'s id.

    The value is an ``itsdangerous`` timestamped, signed token; :func:`read_session`
    rejects it once older than ``STOKER_SESSION_TTL``. No secret is embedded — only
    the user id — so the cookie is worthless without the master key.
    """
    return _serializer(settings).dumps({"uid": int(user.id)})


def read_session(cookie_value, settings=None):
    # type: (Optional[str], Optional[Settings]) -> Optional[int]
    """Return the user id from a session cookie, or None if invalid/expired.

    Enforces the signature and the ``STOKER_SESSION_TTL`` max-age. Any failure
    (missing, tampered, expired, malformed payload) resolves to None rather than
    raising, so an unauthenticated request is simply anonymous.
    """
    if not cookie_value:
        return None
    if settings is None:
        settings = get_settings()
    try:
        data = _serializer(settings).loads(
            cookie_value, max_age=settings.session_ttl_s)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    uid = data.get("uid")
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


def _request_is_https(request):
    # type: (Optional[Request]) -> bool
    """True when the browser hop is https: the direct scheme, or the proxy's
    X-Forwarded-Proto (Traefik terminates TLS and forwards http + XFP=https).

    XFP can only TIGHTEN the Secure flag (set it True), never downgrade it, so
    honouring it here is safe even without a configured trusted proxy.
    """
    if request is None:
        return False
    if request.url.scheme == "https":
        return True
    xfp = request.headers.get("x-forwarded-proto", "")
    return xfp.split(",")[0].strip().lower() == "https"


def cookie_kwargs(settings=None, request=None):
    # type: (Optional[Settings], Optional[Request]) -> dict
    """Cookie attributes for the session (HttpOnly, SameSite, Secure).

    ``Secure`` follows the actual request scheme (via ``X-Forwarded-Proto`` when
    behind a TLS-terminating proxy), so a plain-http local dev deployment still
    works while the cookie is marked Secure over HTTPS in production. Keying off
    the worker-facing ``public_base_url`` (which is often plain http) would ship
    a non-Secure cookie even when the browser is on HTTPS.
    """
    if settings is None:
        settings = get_settings()
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": _request_is_https(request),
        "path": "/",
        "max_age": int(settings.session_ttl_s),
    }


# --------------------------------------------------------------------------- #
# Trusted-proxy header trust model
# --------------------------------------------------------------------------- #

def _peer_is_trusted(client_host, settings):
    # type: (Optional[str], Settings) -> bool
    """True when the immediate peer is inside a configured trusted network.

    ``client_host`` is ``request.client.host`` — the socket peer, i.e. the
    reverse proxy when Stoker sits behind one. It is NOT any ``X-Forwarded-For``
    value (which the client controls); only the real peer address is consulted.
    A missing/unparseable peer, or no configured networks, means "not trusted".
    """
    if not settings.trusted_proxies or not client_host:
        return False
    try:
        addr = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    return any(addr in net for net in settings.trusted_proxies)


def _proxy_asserted_username(request, settings):
    # type: (Request, Settings) -> Optional[str]
    """Return the username a *trusted* proxy asserts, or None.

    Guards the trust boundary: returns a value ONLY when the immediate peer is
    trusted AND the configured header is present and non-empty. An untrusted peer
    (a direct client) gets None even if it sends the header, so a client-supplied
    auth header is never honoured.
    """
    client_host = request.client.host if request.client else None
    if not _peer_is_trusted(client_host, settings):
        return None
    raw = request.headers.get(settings.auth_header)
    if raw is None:
        return None
    username = raw.strip()
    return username or None


def _get_or_create_proxy_user(db, username, settings):
    # type: (Session, str, Settings) -> Optional[User]
    """Resolve a proxy-asserted username to a User, creating it on first sight.

    An existing account (of either source) with that username is reused. When
    absent, a new ``source="proxy"`` user is created with the configured default
    role. An inactive account is honoured as a lockout: None is returned so a
    disabled proxy user cannot act. Never sets a password.
    """
    user = db.execute(
        select(User).where(User.username == username)
    ).scalars().first()
    if user is not None:
        if not user.active:
            return None
        return user
    user = User(
        username=username,
        password_hash=None,
        email=None,
        role=settings.proxy_default_role,
        source="proxy",
        active=True,
        last_login_at=utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info("created proxy-asserted user %r (role=%s) on first sight",
             username, user.role)
    return user


# --------------------------------------------------------------------------- #
# The shared resolver (used by both the dependencies and the middleware)
# --------------------------------------------------------------------------- #

def resolve_user(request, db, settings=None):
    # type: (Request, Session, Optional[Settings]) -> Optional[User]
    """Resolve the authenticated user for a request, or None.

    Order:

    1. **Trusted-proxy header** — when the immediate peer is inside
       ``STOKER_TRUSTED_PROXIES`` and the auth header names a user, that user is
       resolved (created on first sight, ``source="proxy"``). This wins so SSO is
       seamless behind the proxy.
    2. **Session cookie** — otherwise the signed session cookie is read and the
       referenced active user returned.

    Returns None when neither yields an active user (the request is anonymous).
    An inactive user never resolves. No secret is read or logged.
    """
    if settings is None:
        settings = get_settings()

    username = _proxy_asserted_username(request, settings)
    if username is not None:
        return _get_or_create_proxy_user(db, username, settings)

    uid = read_session(request.cookies.get(SESSION_COOKIE), settings)
    if uid is None:
        return None
    user = db.get(User, uid)
    if user is None or not user.active:
        return None
    return user


# --------------------------------------------------------------------------- #
# FastAPI dependencies
# --------------------------------------------------------------------------- #

def current_user(request: Request, db: Session = Depends(get_db)):
    # type: (Request, Session) -> Optional[User]
    """Dependency: the authenticated :class:`User` or None (never raises).

    Resolves from the trusted-proxy header (immediate peer must be in
    ``STOKER_TRUSTED_PROXIES``) else the session cookie. Use :func:`require_user`
    / :func:`require_admin` when the endpoint must reject anonymous/non-admin
    callers.
    """
    return resolve_user(request, db)


def require_user(user: Optional[User] = Depends(current_user)):
    # type: (Optional[User]) -> User
    """Dependency: the authenticated user, or 401 when anonymous."""
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require_admin(user: User = Depends(require_user)):
    # type: (User) -> User
    """Dependency: the authenticated user when an admin, else 403."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return user


# --------------------------------------------------------------------------- #
# Startup / bootstrap helpers
# --------------------------------------------------------------------------- #

def user_count(db):
    # type: (Session) -> int
    """Total number of user rows (both local and proxy)."""
    return int(db.execute(select(func.count()).select_from(User)).scalar_one())


def setup_needed(db, settings=None):
    # type: (Session, Optional[Settings]) -> bool
    """True when first-access setup should be offered.

    That is: zero users exist AND no proxy trust is configured. With proxy trust
    configured, the first trusted request bootstraps an account, so the local
    setup flow is not offered. With auth disabled, setup is never needed.
    """
    if settings is None:
        settings = get_settings()
    if settings.auth_disabled:
        return False
    if settings.proxy_trust_enabled:
        return False
    return user_count(db) == 0


def auth_active(db, settings=None):
    # type: (Session, Optional[Settings]) -> bool
    """True when the API should be locked to authenticated callers.

    Auth engages once there is something to authenticate against: at least one
    user exists, or proxy trust is configured. Before that (a fresh install with
    no admin and no SSO) the control plane is in first-run/bootstrap mode and the
    operator API is open so the first admin can be created via setup. When
    ``STOKER_AUTH_DISABLED`` is set, auth never engages.
    """
    if settings is None:
        settings = get_settings()
    if settings.auth_disabled:
        return False
    if settings.proxy_trust_enabled:
        return True
    return user_count(db) > 0


def bootstrap_admin(db, settings=None):
    # type: (Session, Optional[Settings]) -> Optional[User]
    """Create the env-configured default admin at startup, if needed.

    When ``STOKER_ADMIN_USER`` + ``STOKER_ADMIN_PASSWORD`` are both set and no
    user with that username exists, a local **admin** is created with a bcrypt
    hash of the password. Idempotent: an existing username is left untouched (the
    password is never reset here). Returns the created user, or None when nothing
    was created. The password is never logged. Also emits the loud dev warning
    when auth is disabled.
    """
    if settings is None:
        settings = get_settings()

    if settings.auth_disabled:
        log.warning(
            "STOKER_AUTH_DISABLED is set: authentication is DISABLED. The "
            "operator API is unprotected. Never use this in production.")
        return None

    username = (settings.admin_user or "").strip()
    password = settings.admin_password or ""
    if not username or not password:
        return None

    existing = db.execute(
        select(User).where(User.username == username)
    ).scalars().first()
    if existing is not None:
        log.info("bootstrap admin %r already present; leaving it unchanged",
                 username)
        return None

    user = User(
        username=username,
        password_hash=hash_password(password),
        email=None,
        role="admin",
        source="local",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info("bootstrapped default admin %r from the environment", username)
    return user


__all__ = [
    "SESSION_COOKIE",
    "hash_password",
    "verify_password",
    "issue_session",
    "read_session",
    "cookie_kwargs",
    "resolve_user",
    "current_user",
    "require_user",
    "require_admin",
    "user_count",
    "setup_needed",
    "auth_active",
    "bootstrap_admin",
]
