"""Per-run JWT mint/verify and Fernet secret encryption.

The worker treats the run JWT as opaque; only the control plane decodes it.
Every agent request re-verifies: signature, ``run_id`` claim == path, and
expiry. These tests prove the happy path round-trips and that tampering, a
foreign signing key, a mismatched run and an expired token are all rejected
with :class:`crypto.JWTError` (the routes turn that into a 401). Fernet covers
the target-token-at-rest path (ciphertext never contains the plaintext).
"""

from __future__ import annotations

import time

import pytest

from server import crypto
from server.config import Settings


def _other_settings(base):
    # type: (Settings) -> Settings
    """A Settings identical to ``base`` but with a different master key."""
    return Settings(
        database_url=base.database_url,
        master_key=crypto.generate_master_key(),
        jwt_ttl_s=base.jwt_ttl_s,
        public_base_url=base.public_base_url,
        worker_image=base.worker_image,
        portainer_host=None,
        portainer_token=None,
        portainer_endpoint=base.portainer_endpoint,
        bundle_dir=base.bundle_dir,
        dogfood_hec_url=None,
        dogfood_hec_token=None,
        port=base.port,
    )


# --------------------------------------------------------------------------- #
# Mint / verify round-trip.
# --------------------------------------------------------------------------- #

def test_jwt_round_trip_claims(settings):
    kid = crypto.new_kid()
    token = crypto.mint_run_jwt(812, kid, settings=settings)
    claims = crypto.verify_run_jwt(token, 812, settings=settings)
    assert str(claims["run_id"]) == "812"
    assert claims["kid"] == kid
    assert claims["iss"] == crypto.JWT_ISSUER
    assert claims["exp"] > claims["iat"]


def test_jwt_run_id_compared_as_string(settings):
    # A path run id passed as int must match a claim minted with an int.
    token = crypto.mint_run_jwt(7, crypto.new_kid(), settings=settings)
    assert crypto.verify_run_jwt(token, 7, settings=settings)
    assert crypto.verify_run_jwt(token, "7", settings=settings)


def test_new_kid_shape_and_uniqueness():
    a = crypto.new_kid()
    b = crypto.new_kid()
    assert a.startswith("k_") and b.startswith("k_")
    assert a != b


# --------------------------------------------------------------------------- #
# Rejections: wrong run, tamper, foreign key, expiry.
# --------------------------------------------------------------------------- #

def test_jwt_wrong_run_rejected(settings):
    token = crypto.mint_run_jwt(812, crypto.new_kid(), settings=settings)
    with pytest.raises(crypto.JWTError):
        crypto.verify_run_jwt(token, 999, settings=settings)


def test_jwt_tamper_rejected(settings):
    token = crypto.mint_run_jwt(812, crypto.new_kid(), settings=settings)
    # Flip the final signature chars; the HMAC no longer validates.
    tampered = token[:-3] + ("aaa" if token[-3:] != "aaa" else "bbb")
    with pytest.raises(crypto.JWTError):
        crypto.verify_run_jwt(tampered, 812, settings=settings)


def test_jwt_payload_tamper_rejected(settings):
    # Corrupt a byte in the payload segment: signature check must fail.
    token = crypto.mint_run_jwt(812, crypto.new_kid(), settings=settings)
    head, payload, sig = token.split(".")
    corrupt = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(crypto.JWTError):
        crypto.verify_run_jwt("%s.%s.%s" % (head, corrupt, sig), 812, settings=settings)


def test_jwt_foreign_signing_key_rejected(settings):
    token = crypto.mint_run_jwt(812, crypto.new_kid(), settings=settings)
    other = _other_settings(settings)
    with pytest.raises(crypto.JWTError):
        crypto.verify_run_jwt(token, 812, settings=other)


def test_jwt_expired_rejected(settings):
    # Mint with a negative TTL so it is already expired.
    token = crypto.mint_run_jwt(812, crypto.new_kid(), ttl_s=-10, settings=settings)
    with pytest.raises(crypto.JWTError):
        crypto.verify_run_jwt(token, 812, settings=settings)


def test_jwt_garbage_rejected(settings):
    with pytest.raises(crypto.JWTError):
        crypto.verify_run_jwt("not-a-jwt", 812, settings=settings)


def test_decode_can_skip_expiry(settings):
    # decode_run_jwt with verify_exp=False returns claims for an expired token
    # (used by jwt_seconds_remaining), still signature-checked.
    token = crypto.mint_run_jwt(812, crypto.new_kid(), ttl_s=-10, settings=settings)
    claims = crypto.decode_run_jwt(token, settings=settings, verify_exp=False)
    assert str(claims["run_id"]) == "812"
    with pytest.raises(crypto.JWTError):
        crypto.decode_run_jwt(token, settings=settings, verify_exp=True)


# --------------------------------------------------------------------------- #
# jwt_seconds_remaining (drives the rolling-refresh policy).
# --------------------------------------------------------------------------- #

def test_jwt_seconds_remaining_positive_and_bounded(settings):
    token = crypto.mint_run_jwt(1, crypto.new_kid(), ttl_s=3600, settings=settings)
    remaining = crypto.jwt_seconds_remaining(token, settings=settings)
    assert remaining is not None
    assert 3590 <= remaining <= 3600


def test_jwt_seconds_remaining_negative_when_expired(settings):
    token = crypto.mint_run_jwt(1, crypto.new_kid(), ttl_s=-5, settings=settings)
    remaining = crypto.jwt_seconds_remaining(token, settings=settings)
    assert remaining is not None and remaining < 0


def test_jwt_seconds_remaining_none_for_foreign_key(settings):
    token = crypto.mint_run_jwt(1, crypto.new_kid(), settings=settings)
    assert crypto.jwt_seconds_remaining(token, settings=_other_settings(settings)) is None


# --------------------------------------------------------------------------- #
# Fernet secret round-trip (target HEC tokens at rest).
# --------------------------------------------------------------------------- #

def test_fernet_round_trip(settings):
    secret = "hec-token-abc123!@#"
    ct = crypto.encrypt(secret, settings=settings)
    assert secret not in ct  # plaintext never appears in ciphertext
    assert crypto.decrypt(ct, settings=settings) == secret


def test_fernet_ciphertext_differs_each_call(settings):
    # Fernet embeds a random IV + timestamp, so two encryptions differ but both
    # decrypt to the same plaintext.
    a = crypto.encrypt("same", settings=settings)
    b = crypto.encrypt("same", settings=settings)
    assert a != b
    assert crypto.decrypt(a, settings=settings) == "same"
    assert crypto.decrypt(b, settings=settings) == "same"


def test_fernet_wrong_key_cannot_decrypt(settings):
    ct = crypto.encrypt("secret", settings=settings)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(ct, settings=_other_settings(settings))


def test_fernet_encrypt_none_raises(settings):
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt(None, settings=settings)


def test_fernet_decrypt_garbage_raises(settings):
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt("not-ciphertext", settings=settings)


def test_generate_master_key_is_valid_fernet_key():
    # A generated key must itself be usable as a master key.
    key = crypto.generate_master_key()
    s = Settings(
        database_url="sqlite://", master_key=key, jwt_ttl_s=3600,
        public_base_url="http://x", worker_image="x", portainer_host=None,
        portainer_token=None, portainer_endpoint=6, bundle_dir="/tmp",
        dogfood_hec_url=None, dogfood_hec_token=None, port=8080,
    )
    ct = crypto.encrypt("hello", settings=s)
    assert crypto.decrypt(ct, settings=s) == "hello"
