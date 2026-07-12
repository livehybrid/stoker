"""Auth API: sessions, first-access setup and admin user management.

Two routers live here:

* :data:`router` (prefix ``/api/auth``) — the session/identity surface the login
  page and the app use: ``login``, ``logout``, ``me``, ``status``, ``setup``.
  ``login`` / ``logout`` / ``status`` / ``setup`` are the unauthenticated
  entry points (the app.py middleware exempts them); ``me`` requires a session.

* :data:`users_router` (prefix ``/api/users``) — CRUD over local users, **admin
  only** (every handler depends on :func:`server.auth.require_admin`). Two
  integrity guards: you cannot delete or demote/deactivate the **last admin**,
  and you cannot delete **yourself**.

No password or hash is ever returned or logged; responses use
:class:`~server.schemas.UserOut`, which has no hash field by construction.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import auth
from ..config import get_settings
from ..db import get_db
from ..models import User, utcnow
from ..schemas import (
    AuthStatus,
    LoginRequest,
    SetupRequest,
    UserCreate,
    UserOut,
    UserUpdate,
)

log = logging.getLogger("stoker.routes.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])
users_router = APIRouter(prefix="/api/users", tags=["users"])


# --------------------------------------------------------------------------- #
# Session + identity (/api/auth)
# --------------------------------------------------------------------------- #

@router.post("/login", response_model=UserOut)
def login(body: LoginRequest, request: Request, response: Response,
          db: Session = Depends(get_db)):
    # type: (...) -> object
    """Authenticate a local user and set the signed session cookie.

    401 on unknown user, wrong password, an inactive account, or a proxy/SSO
    account that has no local password. The response is uniform (a single
    "invalid credentials") so it is not an oracle for which usernames exist.
    """
    settings = get_settings()
    username = (body.username or "").strip()
    user = db.execute(
        select(User).where(User.username == username)
    ).scalars().first()

    # One uniform rejection for every failure mode (no user-enumeration oracle).
    if (
        user is None
        or not user.active
        or user.source != "local"
        or not auth.verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(status_code=401, detail="invalid credentials")

    user.last_login_at = utcnow()
    db.commit()
    db.refresh(user)

    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue_session(user, settings),
        **auth.cookie_kwargs(settings, request))
    log.info("user %r logged in (role=%s)", user.username, user.role)
    return user


@router.post("/logout")
def logout(response: Response):
    # type: (...) -> object
    """Clear the session cookie. Idempotent (safe when already logged out)."""
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(auth.require_user)):
    # type: (...) -> object
    """Return the currently-authenticated user, or 401 when anonymous."""
    return user


@router.get("/status", response_model=AuthStatus)
def status(request: Request, db: Session = Depends(get_db)):
    # type: (...) -> object
    """Public auth status for the login page (safe when unauthenticated).

    Reports whether this request is already authenticated (and by whom), whether
    first-access setup is needed (zero users, no proxy trust), and whether SSO
    (a trusted proxy) is configured. Never 401s — it is how the UI decides
    whether to show login, the setup wizard, or to follow an SSO redirect.
    """
    settings = get_settings()
    user = auth.resolve_user(request, db, settings)
    return AuthStatus(
        authenticated=user is not None,
        setup_needed=auth.setup_needed(db, settings),
        sso_enabled=settings.proxy_trust_enabled,
        user=UserOut.model_validate(user) if user is not None else None,
    )


@router.post("/setup", response_model=UserOut, status_code=201)
def setup(body: SetupRequest, request: Request, response: Response,
          db: Session = Depends(get_db)):
    # type: (...) -> object
    """Create the very first admin, only when zero users exist (409 otherwise).

    First-access bootstrap for a fresh install with no env admin: the created
    user is always an admin and a local account, and the caller is logged in
    immediately (the session cookie is set on the response).
    """
    settings = get_settings()
    # Serialise concurrent setup calls so two cannot each pass the zero-user
    # gate and create two admins (Postgres advisory lock held for the txn;
    # SQLite serialises writers natively).
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        db.execute(text("SELECT pg_advisory_xact_lock(7318231)"))
    if auth.user_count(db) != 0:
        raise HTTPException(
            status_code=409,
            detail="setup already complete: at least one user exists")

    user = User(
        username=body.username.strip(),
        password_hash=auth.hash_password(body.password),
        email=None,
        role="admin",
        source="local",
        active=True,
        last_login_at=utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue_session(user, settings),
        **auth.cookie_kwargs(settings, request))
    log.info("first-access setup created initial admin %r", user.username)
    return user


# --------------------------------------------------------------------------- #
# User management (/api/users) — admin only
# --------------------------------------------------------------------------- #

def _admin_count(db, exclude_id=None):
    # type: (Session, Optional[int]) -> int
    """Number of active admins, optionally excluding one user id.

    Used by the last-admin guard: a change that would leave zero active admins
    (delete, demote, or deactivate the sole admin) is refused so the instance can
    never be locked out of user management.
    """
    stmt = select(func.count()).select_from(User).where(
        User.role == "admin", User.active.is_(True))
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return int(db.execute(stmt).scalar_one())


@users_router.get("", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db), _admin: User = Depends(auth.require_admin)):
    # type: (...) -> object
    """List all users (admin only). No password hashes by construction."""
    return list(db.execute(select(User).order_by(User.id)).scalars().all())


@users_router.post("", response_model=UserOut, status_code=201)
def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(auth.require_admin),
):
    # type: (...) -> object
    """Create a local user (admin only). 409 on a duplicate username."""
    username = body.username.strip()
    existing = db.execute(
        select(User).where(User.username == username)
    ).scalars().first()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="a user named %r already exists" % username)

    user = User(
        username=username,
        password_hash=auth.hash_password(body.password),
        email=body.email,
        role=body.role,
        source="local",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info("admin %r created user %r (role=%s)", _admin.username, user.username, user.role)
    return user


@users_router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(auth.require_admin),
):
    # type: (...) -> object
    """Update a user's role / password / active flag / email (admin only).

    Last-admin guard: a change that would drop the number of active admins to
    zero (demoting or deactivating the sole admin) is refused with 409, so the
    instance can never lock itself out of user management.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown user")

    data = body.model_dump(exclude_unset=True)

    # Would this change remove the last active admin? (demote or deactivate)
    demoting = "role" in data and data["role"] is not None and data["role"] != "admin"
    deactivating = "active" in data and data["active"] is False
    if (demoting or deactivating) and user.role == "admin" and user.active:
        if _admin_count(db, exclude_id=user.id) == 0:
            raise HTTPException(
                status_code=409,
                detail="cannot demote or deactivate the last active admin")

    if "role" in data and data["role"] is not None:
        user.role = data["role"]
    if "active" in data and data["active"] is not None:
        user.active = bool(data["active"])
    if "email" in data:
        user.email = data["email"]
    if data.get("password"):
        user.password_hash = auth.hash_password(data["password"])
        # A password makes this a local credential regardless of prior source.
        user.source = "local"

    db.commit()
    db.refresh(user)
    log.info("admin %r updated user %r", admin.username, user.username)
    return user


@users_router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(auth.require_admin),
):
    # type: (...) -> object
    """Delete a user (admin only).

    Refused (409) when the target is yourself, or when it is the last active
    admin — both would compromise continued administration of the instance.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown user")
    if user.id == admin.id:
        raise HTTPException(status_code=409, detail="you cannot delete yourself")
    if user.role == "admin" and user.active and _admin_count(db, exclude_id=user.id) == 0:
        raise HTTPException(
            status_code=409, detail="cannot delete the last active admin")

    db.delete(user)
    db.commit()
    log.info("admin %r deleted user %r", admin.username, user.username)
    return Response(status_code=204)


__all__ = ["router", "users_router"]
