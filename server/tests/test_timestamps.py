"""Timestamps read back tz-aware UTC and serialise with a zone suffix.

SQLite drops tzinfo on round-trip, so without the UtcDateTime type decorator a
stored UTC instant reads back naive and the API emits a bare
``2026-09-03T12:04:51`` — which a browser parses as LOCAL time, showing "now" as
"60 minutes ago" under BST. These pin the fix.
"""

from __future__ import annotations

from server import db as dbmod
from server.models import Target


def test_read_back_is_tz_aware(db_session):
    t = Target(name="tz", hec_url="http://h:8088", default_index="i")
    db_session.add(t)
    db_session.commit()
    fresh = dbmod.SessionLocal().get(Target, t.id)
    assert fresh.created_at.tzinfo is not None
    assert fresh.created_at.utcoffset().total_seconds() == 0  # UTC


def test_api_timestamp_has_zone_suffix(client, db_session, settings):
    resp = client.post("/api/targets", json={
        "name": "tz-api", "hec_url": "http://h:8088", "token": "t",
        "default_index": "i", "env_tag": "lab", "verify_tls": False})
    assert resp.status_code == 201, resp.text
    created = resp.json()["created_at"]
    # A browser must parse this as UTC, so it has to carry a zone: Z or +00:00.
    assert created.endswith("Z") or created.endswith("+00:00"), created
