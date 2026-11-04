"""Deleting runs, and deleting a pack from the pack source as well as locally.

Two things an operator could not do: clear failed or duplicate runs out of the
Runs list, and delete a pack that is also in the bucket without it coming back
on the next boot (the boot sync imports anything missing).
"""

from __future__ import annotations

import dataclasses
import os

import pytest
from sqlalchemy import select

from server import config as config_mod
from server import lifecycle, packsource
from server.drivers.base import DriverRef
from server.models import MetricSample, Pack, Run, RunEvent, WorkerLease, utcnow

from . import _helpers


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

def _finish(db, run, state=lifecycle.STATE_FAILED):
    run.state = state
    run.ended_at = utcnow()
    db.commit()


def test_a_finished_run_is_deleted_with_everything_it_recorded(
        client, db_session, settings, make_pack, fake_driver):
    ctx = _helpers.full_run(db_session, make_pack(), settings, driver=fake_driver,
                            workers=2, state=lifecycle.STATE_RUNNING)
    run = ctx["run"]
    db_session.add(MetricSample(run_id=run.id, slot=0, ts=utcnow(), events_total=1))
    lifecycle.append_event(db_session, run, "note", {})
    _finish(db_session, run)
    run_id, spec_id = run.id, ctx["spec"].id

    resp = client.delete("/api/runs/%d" % run_id)
    assert resp.status_code == 204, resp.text
    assert client.get("/api/runs/%d" % run_id).status_code == 404

    db_session.expire_all()
    for model in (WorkerLease, MetricSample, RunEvent):
        assert db_session.scalars(
            select(model).where(model.run_id == run_id)).first() is None
    # The spec is shared with other runs and stays.
    assert client.get("/api/specs/%d" % spec_id).status_code == 200


def test_a_live_run_cannot_be_deleted(client, db_session, settings, make_pack, fake_driver):
    ctx = _helpers.full_run(db_session, make_pack(), settings, driver=fake_driver,
                            workers=1, state=lifecycle.STATE_RUNNING)
    resp = client.delete("/api/runs/%d" % ctx["run"].id)
    assert resp.status_code == 409, resp.text
    assert "stop it" in resp.json()["detail"]
    assert client.get("/api/runs/%d" % ctx["run"].id).status_code == 200


def test_an_unreaped_fleet_is_destroyed_before_the_row_goes(
        client, db_session, settings, make_pack, fake_driver):
    """Once the row is gone nothing would ever find the workload again."""
    ctx = _helpers.full_run(db_session, make_pack(), settings, driver=fake_driver,
                            workers=1, state=lifecycle.STATE_RUNNING)
    run = ctx["run"]
    ref = DriverRef.from_json(run.driver_ref_json)
    _finish(db_session, run, lifecycle.STATE_COMPLETED)

    assert client.delete("/api/runs/%d" % run.id).status_code == 204
    assert fake_driver._resolve_state(ref).destroyed


def test_deleting_an_unknown_run_is_404(client):
    assert client.delete("/api/runs/999999").status_code == 404


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #

def test_directory_store_delete_reports_whether_it_was_there(tmp_path):
    store = packsource.DirectoryStore(str(tmp_path))
    store.put("demo.tar.gz", b"x")
    assert store.delete("demo.tar.gz") is True
    assert store.list_archives() == []
    assert store.delete("demo.tar.gz") is False


class _FakeS3:
    def __init__(self, keys=(), fail_delete=False):
        self.keys = set(keys)
        self.fail_delete = fail_delete

    def head_object(self, Bucket, Key):
        if Key not in self.keys:
            exc = Exception("Not Found")
            exc.response = {"Error": {"Code": "404"}}
            raise exc
        return {}

    def delete_object(self, Bucket, Key):
        if self.fail_delete:
            raise Exception("AccessDenied")
        self.keys.discard(Key)


def test_s3_store_delete_uses_the_prefix_and_reports_absence():
    s3 = _FakeS3({"packs/demo.tar.gz"})
    store = packsource.S3Store("bucket", "/packs/", client=s3)
    assert store.delete("demo.tar.gz") is True
    assert s3.keys == set()
    assert store.delete("demo.tar.gz") is False


def test_s3_store_delete_raises_on_a_permission_error():
    s3 = _FakeS3()

    def denied(Bucket, Key):
        exc = Exception("Forbidden")
        exc.response = {"Error": {"Code": "403"}}
        raise exc

    s3.head_object = denied
    with pytest.raises(Exception, match="Forbidden"):
        packsource.S3Store("b", "p", client=s3).delete("demo.tar.gz")


# --------------------------------------------------------------------------- #
# Pack delete with ?remote=true
# --------------------------------------------------------------------------- #

@pytest.fixture()
def bucket(tmp_path, monkeypatch):
    path = tmp_path / "bucket"
    path.mkdir()
    monkeypatch.setenv(packsource.SOURCE_ENV, str(path))
    monkeypatch.setenv(packsource.WRITE_ENV, "1")
    return path


def _register(client, make_pack, name):
    resp = client.post("/api/packs", json={"name": name, "source_path": make_pack(name)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_remote_delete_removes_the_archive_then_the_pack(client, make_pack, bucket):
    pack = _register(client, make_pack, "in-bucket")
    (bucket / "in-bucket.tar.gz").write_bytes(b"x")

    resp = client.delete("/api/packs/%d?remote=true" % pack["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"remote_key": "in-bucket.tar.gz", "remote_existed": True}
    assert not (bucket / "in-bucket.tar.gz").exists()
    assert client.get("/api/packs/%d" % pack["id"]).status_code == 404


def test_remote_delete_of_a_pack_not_in_the_bucket_still_deletes(client, make_pack, bucket):
    pack = _register(client, make_pack, "never-pushed")
    resp = client.delete("/api/packs/%d?remote=true" % pack["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["remote_existed"] is False
    assert client.get("/api/packs/%d" % pack["id"]).status_code == 404


def test_a_failed_remote_delete_keeps_the_local_pack(client, make_pack, bucket, monkeypatch):
    pack = _register(client, make_pack, "stuck")

    def refuse(self, key):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(packsource.DirectoryStore, "delete", refuse)
    resp = client.delete("/api/packs/%d?remote=true" % pack["id"])
    assert resp.status_code == 502, resp.text
    assert "local pack was kept" in resp.json()["detail"]
    assert client.get("/api/packs/%d" % pack["id"]).status_code == 200


def test_remote_delete_is_refused_on_a_read_only_source(client, make_pack, bucket, monkeypatch):
    monkeypatch.delenv(packsource.WRITE_ENV)
    pack = _register(client, make_pack, "read-only")
    (bucket / "read-only.tar.gz").write_bytes(b"x")
    resp = client.delete("/api/packs/%d?remote=true" % pack["id"])
    assert resp.status_code == 409, resp.text
    assert (bucket / "read-only.tar.gz").exists()
    assert client.get("/api/packs/%d" % pack["id"]).status_code == 200


def test_remote_delete_is_refused_with_no_source(client, make_pack, monkeypatch):
    monkeypatch.delenv(packsource.SOURCE_ENV, raising=False)
    pack = _register(client, make_pack, "no-source")
    resp = client.delete("/api/packs/%d?remote=true" % pack["id"])
    assert resp.status_code == 409, resp.text


def test_a_plain_delete_never_touches_the_bucket(client, make_pack, bucket):
    pack = _register(client, make_pack, "local-only")
    (bucket / "local-only.tar.gz").write_bytes(b"x")
    assert client.delete("/api/packs/%d" % pack["id"]).status_code == 204
    assert (bucket / "local-only.tar.gz").exists()


# --------------------------------------------------------------------------- #
# Builtin packs
# --------------------------------------------------------------------------- #

def test_a_builtin_pack_is_flagged_and_cannot_be_deleted(
        client, db_session, settings, make_pack, tmp_path):
    pack_dir = make_pack("shipped")
    config_mod.set_settings(dataclasses.replace(
        config_mod.get_settings(), builtin_packs_dir=os.path.dirname(pack_dir)))
    pack = Pack(name="shipped", source_path=pack_dir, lint_status="ok", verified=True)
    db_session.add(pack)
    db_session.commit()

    listed = {p["id"]: p for p in client.get("/api/packs").json()}
    assert listed[pack.id]["builtin"] is True
    assert client.get("/api/packs/%d" % pack.id).json()["builtin"] is True

    resp = client.delete("/api/packs/%d" % pack.id)
    assert resp.status_code == 409, resp.text
    assert "image" in resp.json()["detail"]
    assert os.path.isdir(pack_dir)
