"""GET /api/specs/{id}/export — reproduce a spec as a POST body + curl, for CI/CD."""

from __future__ import annotations

from . import _helpers


def test_export_round_trips_into_a_new_spec(
        client, db_session, settings, make_pack, fake_driver):
    ctx = _helpers.full_run(
        db_session, make_pack(), settings, driver=fake_driver, workers=2,
        rate_mode="eps", rate_value=1000.0,
        overrides={"index": "loadtest", "source": "src-{slot}"})
    spec = ctx["spec"]

    resp = client.get("/api/specs/%d/export" % spec.id)
    assert resp.status_code == 200
    data = resp.json()
    body = data["spec"]

    assert body["pack_id"] == spec.pack_id
    assert body["target_id"] == spec.target_id
    assert body["rate_mode"] == "eps"
    assert body["rate_value"] == 1000.0
    assert body["overrides"]["source"] == "src-{slot}"

    # references carry names so the body can be remapped in another environment.
    assert data["references"]["pack"]["name"] == ctx["pack"].name
    assert data["references"]["target"]["name"] == ctx["target"].name

    # A runnable curl carrying the JSON, with env placeholders (no live secrets).
    assert '"$STOKER_URL/api/specs"' in data["curl"]
    assert "Bearer $STOKER_TOKEN" in data["curl"]

    # The exported body must actually recreate a valid spec (round-trip).
    body["name"] = body["name"] + "-copy"
    resp2 = client.post("/api/specs", json=body)
    assert resp2.status_code == 201, resp2.text


def test_export_carries_no_secret(
        client, db_session, settings, make_pack, fake_driver):
    ctx = _helpers.full_run(db_session, make_pack(), settings,
                            driver=fake_driver, workers=1)
    resp = client.get("/api/specs/%d/export" % ctx["spec"].id)
    assert resp.status_code == 200
    # The target's HEC token (the fixture default) must never appear anywhere.
    assert "hec-secret-token" not in resp.text


def test_export_unknown_spec_404(client):
    assert client.get("/api/specs/999999/export").status_code == 404
