"""Renaming a pack: the row, its pack.yaml, and its copy in the pack source.

The id is the pack's identity, so specs and runs follow a rename without being
touched. The name also lives in pack.yaml and in the bucket key, and both
have to move with it or the boot sync brings the old name back as a second
pack.
"""

from __future__ import annotations

import dataclasses
import json
import os

import pytest

from server import config as config_mod
from server import packsource
from server.gitsync import local_pack_metadata
from server.models import Pack


@pytest.fixture()
def upload_dir(settings, tmp_path):
    target = tmp_path / "uploads"
    target.mkdir()
    config_mod.set_settings(dataclasses.replace(
        config_mod.get_settings(), pack_upload_dir=str(target)))
    return target


@pytest.fixture()
def bucket(tmp_path, monkeypatch):
    path = tmp_path / "bucket"
    path.mkdir()
    monkeypatch.setenv(packsource.SOURCE_ENV, str(path))
    monkeypatch.setenv(packsource.WRITE_ENV, "1")
    return path


def _owned_pack(db, make_pack, upload_dir, name):
    """A pack whose directory Stoker owns (as an upload or bucket import is)."""
    src = make_pack(name)
    dest = upload_dir / name
    os.rename(src, dest)
    pack = Pack(name=name, source_path=str(dest), lint_status="ok", verified=True)
    db.add(pack)
    db.commit()
    return pack


def _rename(client, pack_id, name):
    return client.patch("/api/packs/%d" % pack_id, json={"name": name})


def test_rename_keeps_the_id(
        client, db_session, make_pack, upload_dir):
    pack = _owned_pack(db_session, make_pack, upload_dir, "before")
    resp = _rename(client, pack.id, "after")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == pack.id
    assert body["name"] == "after" and body["old_name"] == "before"
    assert client.get("/api/packs/%d" % pack.id).json()["name"] == "after"


def test_rename_rewrites_pack_yaml_so_exports_carry_the_new_name(
        client, db_session, make_pack, upload_dir):
    pack = _owned_pack(db_session, make_pack, upload_dir, "yaml-old")
    yaml_path = os.path.join(pack.source_path, "pack.yaml")
    before = open(yaml_path, encoding="utf-8").read()

    assert _rename(client, pack.id, "yaml new # with hash").status_code == 200

    after = open(yaml_path, encoding="utf-8").read()
    assert local_pack_metadata(pack.source_path)["name"] == "yaml new # with hash"
    # Every other line is untouched.
    others = lambda text: [l for l in text.splitlines() if not l.startswith("name:")]  # noqa: E731
    assert others(after) == others(before)


def test_rename_updates_the_builder_config(client, db_session, make_pack, upload_dir):
    pack = _owned_pack(db_session, make_pack, upload_dir, "built")
    cfg_path = os.path.join(pack.source_path, "stoker-builder.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"name": "built", "events": ["x"]}, fh)
    assert _rename(client, pack.id, "built-renamed").status_code == 200
    with open(cfg_path, encoding="utf-8") as fh:
        assert json.load(fh) == {"name": "built-renamed", "events": ["x"]}


def test_an_operator_directory_is_renamed_without_touching_its_files(
        client, make_pack, upload_dir):
    pack_dir = make_pack("operator-owned")
    pack = client.post("/api/packs", json={"name": "operator-owned",
                                          "source_path": pack_dir}).json()
    before = open(os.path.join(pack_dir, "pack.yaml"), encoding="utf-8").read()
    assert _rename(client, pack["id"], "operator-renamed").status_code == 200
    assert open(os.path.join(pack_dir, "pack.yaml"), encoding="utf-8").read() == before


def test_rename_moves_the_bucket_copy(client, db_session, make_pack, upload_dir, bucket):
    pack = _owned_pack(db_session, make_pack, upload_dir, "moving")
    (bucket / "moving.tar.gz").write_bytes(b"old")

    resp = _rename(client, pack.id, "moved")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["remote_key"] == "moved.tar.gz"
    assert body["old_remote_key"] == "moving.tar.gz"
    assert body["old_remote_removed"] is True
    assert body["warning"] is None
    assert (bucket / "moved.tar.gz").exists()
    assert not (bucket / "moving.tar.gz").exists()


def test_the_bucket_sync_does_not_bring_the_old_name_back(
        client, db_session, settings, make_pack, upload_dir, bucket):
    pack = _owned_pack(db_session, make_pack, upload_dir, "ghost")
    packsource.publish_pack(pack, config_mod.get_settings(),
                            store=packsource.DirectoryStore(str(bucket)))
    assert _rename(client, pack.id, "ghost-renamed").status_code == 200

    db_session.expire_all()
    report = packsource.sync_from_store(
        db_session, packsource.DirectoryStore(str(bucket)), config_mod.get_settings())
    assert report["imported"] == [], report
    names = sorted(p.name for p in db_session.query(Pack).all())
    assert names == ["ghost-renamed"]


def test_names_that_slug_to_the_same_key_keep_the_archive(
        client, db_session, make_pack, upload_dir, bucket):
    pack = _owned_pack(db_session, make_pack, upload_dir, "same-key")
    resp = _rename(client, pack.id, "same key")
    assert resp.status_code == 200, resp.text
    assert resp.json()["old_remote_key"] is None
    assert (bucket / "same-key.tar.gz").exists()


def test_a_failed_old_key_delete_is_a_warning_not_an_error(
        client, db_session, make_pack, upload_dir, bucket, monkeypatch):
    pack = _owned_pack(db_session, make_pack, upload_dir, "sticky")
    (bucket / "sticky.tar.gz").write_bytes(b"old")

    def refuse(self, key):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(packsource.DirectoryStore, "delete", refuse)
    resp = _rename(client, pack.id, "unstuck")
    assert resp.status_code == 200, resp.text
    assert "sticky.tar.gz" in resp.json()["warning"]
    assert client.get("/api/packs/%d" % pack.id).json()["name"] == "unstuck"


def test_a_read_only_source_warns_that_the_old_copy_remains(
        client, db_session, make_pack, upload_dir, bucket, monkeypatch):
    monkeypatch.delenv(packsource.WRITE_ENV)
    pack = _owned_pack(db_session, make_pack, upload_dir, "ro")
    resp = _rename(client, pack.id, "ro-renamed")
    assert resp.status_code == 200, resp.text
    assert "read-only" in resp.json()["warning"]


def test_a_plain_rename_without_a_source_has_no_warning(
        client, db_session, make_pack, upload_dir, monkeypatch):
    monkeypatch.delenv(packsource.SOURCE_ENV, raising=False)
    pack = _owned_pack(db_session, make_pack, upload_dir, "nosrc")
    resp = _rename(client, pack.id, "nosrc-renamed")
    assert resp.status_code == 200 and resp.json()["warning"] is None


def test_a_taken_name_is_refused(client, db_session, make_pack, upload_dir):
    a = _owned_pack(db_session, make_pack, upload_dir, "first")
    _owned_pack(db_session, make_pack, upload_dir, "second")
    resp = _rename(client, a.id, "second")
    assert resp.status_code == 409, resp.text
    assert client.get("/api/packs/%d" % a.id).json()["name"] == "first"


@pytest.mark.parametrize("name", ["", "   ", "a\nb", "x" * 256, "both ' and \""])
def test_unstorable_names_are_refused(client, db_session, make_pack, upload_dir, name):
    pack = _owned_pack(db_session, make_pack, upload_dir, "valid")
    assert _rename(client, pack.id, name).status_code == 422


def test_the_same_name_is_a_no_op(client, db_session, make_pack, upload_dir, bucket):
    pack = _owned_pack(db_session, make_pack, upload_dir, "unchanged")
    resp = _rename(client, pack.id, "  unchanged  ")
    assert resp.status_code == 200 and resp.json()["remote_key"] is None
    assert list(bucket.iterdir()) == []


def test_unknown_pack_is_404(client):
    assert _rename(client, 9999, "x").status_code == 404


def test_a_builtin_pack_cannot_be_renamed(client, db_session, make_pack):
    pack_dir = make_pack("shipped")
    config_mod.set_settings(dataclasses.replace(
        config_mod.get_settings(), builtin_packs_dir=os.path.dirname(pack_dir)))
    pack = Pack(name="shipped", source_path=pack_dir, lint_status="ok", verified=True)
    db_session.add(pack)
    db_session.commit()
    resp = _rename(client, pack.id, "not-shipped")
    assert resp.status_code == 409 and "image" in resp.json()["detail"]


def test_a_repo_pack_cannot_be_renamed(client, db_session, make_pack):
    from server.models import Repo

    repo = Repo(url="https://example.invalid/packs.git")
    db_session.add(repo)
    db_session.flush()
    pack = Pack(name="from-repo", source_path=make_pack("from-repo"), repo_id=repo.id,
                lint_status="ok", verified=True)
    db_session.add(pack)
    db_session.commit()
    resp = _rename(client, pack.id, "elsewhere")
    assert resp.status_code == 409 and "repo" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Pack builder: a save that changes the name
# --------------------------------------------------------------------------- #

def _built_pack(client, name):
    from .test_pack_builder import SYSLOG

    analysed = client.post("/api/pack-builder/analyse", json={"text": SYSLOG}).json()
    config = {"name": name, "events": analysed["event_list"], "tokens": analysed["suggestions"],
              "sourcetype": "linux_secure", "count": 20, "interval": 1}
    resp = client.post("/api/pack-builder/packs", json={"config": config})
    assert resp.status_code == 201, resp.text
    return resp.json(), config


def test_a_builder_save_with_a_new_name_moves_the_bucket_copy(client, upload_dir, bucket):
    pack, config = _built_pack(client, "builder-old")
    assert (bucket / "builder-old.tar.gz").exists()

    config["name"] = "builder-new"
    resp = client.put("/api/pack-builder/packs/%d" % pack["id"], json={"config": config})
    assert resp.status_code == 200, resp.text
    assert (bucket / "builder-new.tar.gz").exists()
    assert not (bucket / "builder-old.tar.gz").exists()


def test_a_renamed_builder_pack_reopens_under_its_new_name(client, upload_dir):
    pack, _config = _built_pack(client, "editor-old")
    assert _rename(client, pack["id"], "editor-new").status_code == 200
    reopened = client.get("/api/pack-builder/packs/%d" % pack["id"]).json()
    assert reopened["config"]["name"] == "editor-new"
    assert local_pack_metadata(pack["source_path"])["name"] == "editor-new"
