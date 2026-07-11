"""Secret encryption and per-run JWT mint/verify.

Two independent concerns, both keyed off ``STOKER_MASTER_KEY``:

* **Fernet** symmetric encryption for target HEC tokens at rest. The master key
  is a urlsafe-base64 32-byte Fernet key.
* **Per-run JWTs** (PyJWT, HS256) the worker treats as opaque bearers. The HMAC
  secret is derived from the master key so a single env var configures both;
  the derivation is domain-separated from the Fernet use.

No secret (token, JWT, master key) is ever logged. Verification failures raise
:class:`JWTError`; the routes translate that to a 401.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import logging
import secrets
from typing import Any, Optional

import jwt as pyjwt
from cryptography.fernet import Fernet, InvalidToken

from .config import Settings, get_settings

log = logging.getLogger("stoker.crypto")

JWT_ALG = "HS256"
JWT_ISSUER = "stoker-control-plane"
# Domain separation so the HS256 signing key is not the raw Fernet key.
_JWT_KEY_INFO = b"stoker-run-jwt-v1"


class CryptoError(Exception):
    """Secret encryption/decryption failure."""


class JWTError(Exception):
    """A run JWT is invalid, expired or bound to a different run."""


def _fernet(settings=None):
    # type: (Optional[Settings]) -> Fernet
    if settings is None:
        settings = get_settings()
    try:
        return Fernet(settings.master_key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise CryptoError("STOKER_MASTER_KEY is not a valid Fernet key: %s" % exc)


def encrypt(plaintext, settings=None):
    # type: (str, Optional[Settings]) -> str
    """Fernet-encrypt a secret string, returning ASCII ciphertext."""
    if plaintext is None:
        raise CryptoError("cannot encrypt None")
    token = _fernet(settings).encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt(ciphertext, settings=None):
    # type: (str, Optional[Settings]) -> str
    """Fernet-decrypt ASCII ciphertext back to the secret string."""
    try:
        plain = _fernet(settings).decrypt(ciphertext.encode("ascii"))
    except (InvalidToken, ValueError, TypeError) as exc:
        raise CryptoError("cannot decrypt secret: %s" % exc)
    return plain.decode("utf-8")


def _jwt_secret(settings):
    # type: (Settings) -> bytes
    """Derive the HS256 signing key from the master key (domain-separated)."""
    return hashlib.sha256(_JWT_KEY_INFO + settings.master_key.encode("ascii")).digest()


def new_kid():
    # type: () -> str
    """Generate a short random key id stamped on a run for JWT rotation."""
    return "k_" + secrets.token_hex(6)


def mint_run_jwt(run_id, kid, ttl_s=None, settings=None):
    # type: (Any, str, Optional[int], Optional[Settings]) -> str
    """Mint a per-run bearer JWT.

    Claims: ``run_id`` (the run this token authorises), ``kid`` (key id stored
    on the run), ``iss``, ``iat``, ``exp`` (now + ``ttl_s``). The worker treats
    the whole token as opaque; only the control plane decodes it.
    """
    if settings is None:
        settings = get_settings()
    if ttl_s is None:
        ttl_s = settings.jwt_ttl_s
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "run_id": run_id,
        "kid": kid,
        "iss": JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(seconds=int(ttl_s))).timestamp()),
    }
    return pyjwt.encode(payload, _jwt_secret(settings), algorithm=JWT_ALG)


def decode_run_jwt(token, settings=None, verify_exp=True):
    # type: (str, Optional[Settings], bool) -> dict
    """Decode and signature-verify a run JWT, returning its claims.

    Raises :class:`JWTError` on a bad signature, malformed token or (when
    ``verify_exp``) expiry.
    """
    if settings is None:
        settings = get_settings()
    try:
        return pyjwt.decode(
            token,
            _jwt_secret(settings),
            algorithms=[JWT_ALG],
            issuer=JWT_ISSUER,
            options={"verify_exp": verify_exp, "require": ["run_id", "exp"]},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise JWTError("run JWT expired: %s" % exc)
    except pyjwt.InvalidTokenError as exc:
        raise JWTError("invalid run JWT: %s" % exc)


def verify_run_jwt(token, expected_run_id, settings=None):
    # type: (str, Any, Optional[Settings]) -> dict
    """Verify a run JWT and assert its ``run_id`` claim matches the path.

    Returns the claims on success; raises :class:`JWTError` otherwise. Run id
    comparison is done as strings so an int path param matches a string claim.
    """
    claims = decode_run_jwt(token, settings=settings)
    claim_run = claims.get("run_id")
    if str(claim_run) != str(expected_run_id):
        raise JWTError(
            "run JWT run_id %r does not match path run_id %r"
            % (claim_run, expected_run_id)
        )
    return claims


def jwt_seconds_remaining(token, settings=None):
    # type: (str, Optional[Settings]) -> Optional[float]
    """Seconds until the token expires (negative if past). None if undecodable.

    Used by the heartbeat command builder to decide when to roll the JWT (the
    contract rolls within 20% of expiry). Signature is still verified.
    """
    try:
        claims = decode_run_jwt(token, settings=settings, verify_exp=False)
    except JWTError:
        return None
    exp = claims.get("exp")
    if exp is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    return float(exp) - now


def generate_master_key():
    # type: () -> str
    """Generate a fresh Fernet master key (for ``.env`` bootstrap/docs)."""
    return Fernet.generate_key().decode("ascii")


# base64 import kept for callers that construct raw keys; referenced here to
# avoid an unused-import lint while documenting intent.
_ = base64
