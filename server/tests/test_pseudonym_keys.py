"""Key store behaviour: creation, refusal, decryption and the read-only path."""
from __future__ import annotations

import dataclasses

import pytest

from server import config as config_mod
from server import pseudonymkeys as pk
from server.packbuilder import pseudonym as ps
from server.models import PseudonymKey


def test_a_key_is_created_once_and_reused(db_session, settings):
    first = pk.ensure(db_session, actor="will")
    db_session.commit()
    assert len(first.fingerprint) == 8 and first.algorithm == ps.ALGORITHM
    assert first.created_by == "will"
    again = pk.ensure(db_session)
    assert again.id == first.id, "a second save must reuse the instance key"
    # the raw key round-trips and derives a stable subkey
    raw = pk.raw_key(first)
    assert len(raw) == pk.KEY_BYTES
    assert ps.fingerprint(raw) == first.fingerprint
    sub, row = pk.subkey(db_session)
    assert sub == ps.derive_subkey(raw) and row.id == first.id


def test_the_ciphertext_is_not_the_key(db_session, settings):
    row = pk.ensure(db_session)
    raw = pk.raw_key(row)
    assert raw.hex() not in row.key_encrypted
    assert row.key_encrypted != raw.hex()


def test_reading_never_creates_a_key(db_session, settings):
    assert pk.peek(db_session) is None
    assert pk.describe(pk.peek(db_session))["exists"] is False
    with pytest.raises(pk.PseudonymKeyError):
        pk.subkey(db_session, create=False)
    assert pk.peek(db_session) is None, "a read must not mint a key"


def test_creation_is_refused_under_an_ephemeral_master_key(db_session, settings):
    """A key stored under a generated master key is unreadable after a restart,
    which would orphan every pack built with it."""
    ephemeral = dataclasses.replace(config_mod.get_settings(), master_key_generated=True)
    with pytest.raises(pk.PseudonymKeyError) as exc:
        pk.ensure(db_session, settings=ephemeral)
    assert "STOKER_MASTER_KEY" in str(exc.value)
    assert pk.peek(db_session) is None


def test_an_undecryptable_key_says_what_it_means(db_session, settings):
    row = pk.ensure(db_session)
    row.key_encrypted = "not-fernet-ciphertext"
    db_session.flush()
    with pytest.raises(pk.PseudonymKeyError) as exc:
        pk.raw_key(row)
    message = str(exc.value)
    assert "different master key" in message and "no longer correlate" in message


def test_describe_never_leaks_the_key(db_session, settings):
    row = pk.ensure(db_session)
    out = pk.describe(row)
    assert set(out) == {"exists", "name", "fingerprint", "algorithm", "created_at"}
    assert out["fingerprint"] == row.fingerprint
    assert "key_encrypted" not in out and pk.raw_key(row).hex() not in str(out)


def test_two_instances_give_different_stand_ins(db_session, settings):
    row = pk.ensure(db_session)
    sub_a = ps.derive_subkey(pk.raw_key(row))
    db_session.delete(row)
    db_session.flush()
    sub_b = ps.derive_subkey(pk.raw_key(pk.ensure(db_session)))
    assert sub_a != sub_b
    assert ps.pseudonym("123456", sub_a) != ps.pseudonym("123456", sub_b)
