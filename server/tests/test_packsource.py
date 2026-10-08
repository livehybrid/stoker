"""Packs loaded from an object store or a git repo named by the environment.

The behaviour that matters is what happens when the source is imperfect: a
shared bucket with one bad object in it, a repo an operator has since edited by
hand, an image without boto3. None of those may stop an instance starting, and
none may silently overwrite what an operator did.
"""

from __future__ import annotations

import io
import os
import tarfile

import dataclasses

import pytest
from sqlalchemy import select

from server import config as config_mod
from server import packsource
from server.models import Pack, Repo


@pytest.fixture()
def uploads(settings, tmp_path):
    """Point ``pack_upload_dir`` at a temp dir, as test_pack_upload does.

    An imported pack is extracted exactly where an uploaded one is, so without
    this the sync tries to write to the deployment's real /data.
    """
    target = tmp_path / "uploads"
    config_mod.set_settings(dataclasses.replace(
        config_mod.get_settings(), pack_upload_dir=str(target)))
    return config_mod.get_settings()


def _archive(name="demo", conf="[demo.sample]\nmode = sample\ninterval = 1\n",
             sample="hello world\n", manifest=None):
    """A .tar.gz in the shape Download produces and Upload pack accepts."""
    manifest = manifest if manifest is not None else (
        "name: %s\ndescription: \"from the bucket\"\ntags: [imported, demo]\n" % name)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, text in [("pack.yaml", manifest),
                           ("default/eventgen.conf", conf),
                           ("samples/demo.sample", sample)]:
            raw = text.encode()
            info = tarfile.TarInfo("%s/%s" % (name, path))
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Git repos from the environment
# --------------------------------------------------------------------------- #

def test_repos_from_env_are_registered_once(db_session):
    env = {packsource.REPOS_ENV: "https://github.com/a/b, https://github.com/c/d"}
    added = packsource.register_repos_from_env(db_session, env)
    assert added == ["https://github.com/a/b", "https://github.com/c/d"]
    assert len(db_session.scalars(select(Repo)).all()) == 2

    # Restarting must not duplicate them.
    assert packsource.register_repos_from_env(db_session, env) == []
    assert len(db_session.scalars(select(Repo)).all()) == 2


def test_an_operator_edit_survives_a_restart(db_session):
    """The env names which repos exist, not what their settings are.

    Someone who pins a ref or adds a credential in the UI should not have it
    reverted on the next restart by a value baked into the deployment.
    """
    env = {packsource.REPOS_ENV: "https://github.com/a/b"}
    packsource.register_repos_from_env(db_session, env)
    repo = db_session.scalar(select(Repo))
    repo.default_ref = "v1.2.3"
    repo.trusted_code = True
    db_session.flush()

    packsource.register_repos_from_env(db_session, env)
    repo = db_session.scalar(select(Repo))
    assert repo.default_ref == "v1.2.3" and repo.trusted_code is True


def test_no_env_means_no_repos(db_session):
    assert packsource.register_repos_from_env(db_session, {}) == []


# --------------------------------------------------------------------------- #
# Opening a source
# --------------------------------------------------------------------------- #

def test_s3_and_directory_sources_both_open(tmp_path):
    s3 = packsource.open_store("s3://my-bucket/my-packs")
    assert isinstance(s3, packsource.S3Store)
    assert (s3.bucket, s3.prefix) == ("my-bucket", "my-packs")

    local = packsource.open_store(str(tmp_path))
    assert isinstance(local, packsource.DirectoryStore)


@pytest.mark.parametrize("bad,msg", [
    ("s3:///no-bucket", "no bucket"),
    ("ftp://example/packs", "only s3:// and local directories"),
    ("", "empty pack source"),
])
def test_a_bad_source_says_why(bad, msg):
    with pytest.raises(packsource.PackSourceError) as exc:
        packsource.open_store(bad)
    assert msg in str(exc.value)


def test_s3_without_boto3_says_so_rather_than_importerror(monkeypatch):
    """The image may legitimately not carry boto3; the message must explain."""
    import builtins

    real_import = builtins.__import__

    def no_boto3(name, *a, **k):
        if name == "boto3":
            raise ImportError("no module named boto3")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_boto3)
    store = packsource.S3Store("b", "p")
    with pytest.raises(packsource.PackSourceError) as exc:
        _ = store.client
    assert "boto3 is not installed" in str(exc.value)


def test_writes_are_off_unless_asked_for(monkeypatch):
    """A shared mirror several instances read must not be written by default."""
    assert packsource.writes_enabled({}) is False
    assert packsource.writes_enabled({packsource.WRITE_ENV: "0"}) is False
    for yes in ("1", "true", "yes", "on", "TRUE"):
        assert packsource.writes_enabled({packsource.WRITE_ENV: yes}) is True


# --------------------------------------------------------------------------- #
# Pulling packs
# --------------------------------------------------------------------------- #

def test_packs_are_imported_from_the_source(db_session, uploads, tmp_path):
    store = packsource.DirectoryStore(str(tmp_path / "bucket"))
    store.put("demo.tar.gz", _archive("demo"))

    report = packsource.sync_from_store(db_session, store, uploads)
    assert report["errors"] == [], report["errors"]
    assert report["imported"] == ["demo"]

    pack = db_session.scalar(select(Pack).where(Pack.name == "demo"))
    assert pack is not None
    assert pack.description == "from the bucket"
    # The tag fix applies to an imported pack too: no stray brackets.
    assert pack.tags_json == ["imported", "demo"]


def test_syncing_twice_imports_nothing_new(db_session, uploads, tmp_path):
    store = packsource.DirectoryStore(str(tmp_path / "bucket"))
    store.put("demo.tar.gz", _archive("demo"))
    packsource.sync_from_store(db_session, store, uploads)

    second = packsource.sync_from_store(db_session, store, uploads)
    assert second["imported"] == [] and second["skipped"] == ["demo"]
    assert len(db_session.scalars(select(Pack).where(Pack.name == "demo")).all()) == 1


def test_one_bad_object_does_not_stop_the_others(db_session, uploads, tmp_path):
    """A shared bucket will eventually contain something broken.

    Aborting the sync would mean one bad object stops an instance from starting
    with any of its packs, so a failure is reported against its own archive and
    the rest still import.
    """
    store = packsource.DirectoryStore(str(tmp_path / "bucket"))
    store.put("good.tar.gz", _archive("good"))
    store.put("broken.tar.gz", b"this is not a tar.gz at all")

    report = packsource.sync_from_store(db_session, store, uploads)
    assert report["imported"] == ["good"]
    assert any("broken.tar.gz" in e for e in report["errors"])
    assert db_session.scalar(select(Pack).where(Pack.name == "good")) is not None


def test_an_unreachable_source_is_reported_not_raised(db_session, settings):
    class Dead(packsource.Store):
        def list_archives(self):
            raise RuntimeError("connection refused")

    report = packsource.sync_from_store(db_session, Dead(), settings)
    assert any("cannot list the pack source" in e for e in report["errors"])


def test_a_missing_directory_is_simply_empty(db_session, uploads, tmp_path):
    store = packsource.DirectoryStore(str(tmp_path / "never-created"))
    report = packsource.sync_from_store(db_session, store, uploads)
    assert report == {"imported": [], "skipped": [], "errors": []}


# --------------------------------------------------------------------------- #
# Pushing packs back
# --------------------------------------------------------------------------- #

def test_a_pack_round_trips_through_the_store(db_session, uploads, tmp_path):
    """Export to the bucket, import on another instance, same pack.

    This is the whole point of reusing the Download/Upload archive format
    rather than inventing a layout.
    """
    store = packsource.DirectoryStore(str(tmp_path / "bucket"))
    store.put("demo.tar.gz", _archive("demo"))
    packsource.sync_from_store(db_session, store, uploads)
    pack = db_session.scalar(select(Pack).where(Pack.name == "demo"))

    out = packsource.DirectoryStore(str(tmp_path / "out"))
    key = packsource.publish_pack(pack, uploads, store=out)
    assert key == "demo.tar.gz"
    assert out.list_archives() == ["demo.tar.gz"]

    # ...and a fresh instance picks it up from there.
    db_session.delete(pack)
    db_session.flush()
    report = packsource.sync_from_store(db_session, out, uploads)
    assert report["imported"] == ["demo"]


def test_publishing_is_a_no_op_without_the_write_switch(db_session, uploads, tmp_path):
    store = packsource.DirectoryStore(str(tmp_path / "bucket"))
    store.put("demo.tar.gz", _archive("demo"))
    packsource.sync_from_store(db_session, store, uploads)
    pack = db_session.scalar(select(Pack).where(Pack.name == "demo"))

    assert packsource.publish_pack(pack, uploads, env={}) is None
    assert packsource.publish_pack(
        pack, uploads,
        env={packsource.SOURCE_ENV: str(tmp_path / "x")}) is None


def test_a_failed_publish_never_fails_the_save(db_session, uploads, tmp_path):
    """Losing a mirror copy is annoying; losing the operator's work is not ok."""
    store = packsource.DirectoryStore(str(tmp_path / "bucket"))
    store.put("demo.tar.gz", _archive("demo"))
    packsource.sync_from_store(db_session, store, uploads)
    pack = db_session.scalar(select(Pack).where(Pack.name == "demo"))

    class Refusing(packsource.Store):
        def put(self, key, data):
            raise RuntimeError("AccessDenied")

    assert packsource.publish_pack(pack, uploads, store=Refusing()) is None


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #

def test_boot_never_raises_on_a_bad_source(db_session, settings, monkeypatch):
    monkeypatch.setenv(packsource.SOURCE_ENV, "ftp://nope/packs")
    out = packsource.load_from_env(db_session, settings)
    assert out and "packs_error" in out


def test_boot_with_nothing_configured_does_nothing(db_session, settings, monkeypatch):
    monkeypatch.delenv(packsource.SOURCE_ENV, raising=False)
    monkeypatch.delenv(packsource.REPOS_ENV, raising=False)
    assert packsource.load_from_env(db_session, settings) is None


# --------------------------------------------------------------------------- #
# Describe / push by hand
# --------------------------------------------------------------------------- #

def test_describe_reports_nothing_when_unconfigured(monkeypatch):
    monkeypatch.delenv(packsource.SOURCE_ENV, raising=False)
    monkeypatch.delenv(packsource.WRITE_ENV, raising=False)
    info = packsource.describe()
    assert info == {"configured": False, "writable": False, "kind": None,
                    "location": None, "error": None}


def test_describe_names_the_destination(monkeypatch, tmp_path):
    monkeypatch.setenv(packsource.SOURCE_ENV, "s3://my-bucket/my-packs")
    monkeypatch.setenv(packsource.WRITE_ENV, "1")
    info = packsource.describe()
    assert info["configured"] and info["writable"]
    assert info["kind"] == "s3"
    assert info["location"] == "s3://my-bucket/my-packs"
    assert info["error"] is None
    # Read-only is the default, and is a distinct state from unconfigured: the
    # Push button must not appear for it.
    monkeypatch.delenv(packsource.WRITE_ENV)
    monkeypatch.setenv(packsource.SOURCE_ENV, str(tmp_path))
    info = packsource.describe()
    assert info["configured"] and not info["writable"]
    assert info["kind"] == "directory"


def test_describe_reports_a_bad_source_instead_of_raising(monkeypatch):
    # A typo in a ConfigMap belongs on the Packs page, not in a 500.
    monkeypatch.setenv(packsource.SOURCE_ENV, "ftp://nope/packs")
    info = packsource.describe()
    assert info["configured"] and info["error"]
    assert info["kind"] is None


def test_publish_endpoint_refuses_a_read_only_source(
        client, db_session, uploads, tmp_path, monkeypatch):
    """Both "no source" and "read-only source" are configuration, so they are
    409s an operator can act on rather than a silently successful no-op."""
    store = packsource.DirectoryStore(str(tmp_path / "bucket"))
    store.put("demo.tar.gz", _archive("demo"))
    packsource.sync_from_store(db_session, store, uploads)
    db_session.commit()
    pack = db_session.scalar(select(Pack).where(Pack.name == "demo"))

    monkeypatch.delenv(packsource.SOURCE_ENV, raising=False)
    monkeypatch.delenv(packsource.WRITE_ENV, raising=False)
    assert client.get("/api/pack-source").json()["configured"] is False
    r = client.post("/api/packs/%d/publish" % pack.id)
    assert r.status_code == 409 and "STOKER_PACK_SOURCE" in r.json()["detail"]

    monkeypatch.setenv(packsource.SOURCE_ENV, str(tmp_path / "bucket"))
    r = client.post("/api/packs/%d/publish" % pack.id)
    assert r.status_code == 409 and "read-only" in r.json()["detail"]


def test_publish_endpoint_writes_the_archive(
        client, db_session, uploads, tmp_path, monkeypatch):
    bucket = tmp_path / "bucket"
    store = packsource.DirectoryStore(str(bucket))
    store.put("demo.tar.gz", _archive("demo"))
    packsource.sync_from_store(db_session, store, uploads)
    db_session.commit()
    pack = db_session.scalar(select(Pack).where(Pack.name == "demo"))

    out = tmp_path / "push-target"
    monkeypatch.setenv(packsource.SOURCE_ENV, str(out))
    monkeypatch.setenv(packsource.WRITE_ENV, "1")
    info = client.get("/api/pack-source").json()
    assert info["writable"] is True and info["location"] == str(out)

    r = client.post("/api/packs/%d/publish" % pack.id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["published"] is True
    # The same filename Download produces, so a key in the bucket and a file on
    # disk are one artefact.
    assert body["key"].endswith(packsource.ARCHIVE_SUFFIX)
    assert (out / body["key"]).is_file()


def test_publish_endpoint_404s_an_unknown_pack(client, monkeypatch, tmp_path):
    monkeypatch.setenv(packsource.SOURCE_ENV, str(tmp_path))
    monkeypatch.setenv(packsource.WRITE_ENV, "1")
    assert client.post("/api/packs/999999/publish").status_code == 404
