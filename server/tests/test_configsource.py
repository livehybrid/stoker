"""The shared config copy: pulled at boot and on demand, pushed when it changes.

The trip that matters is an engineer's: configure one instance, have it land in
the bucket, bring up another (or the same one rebuilt) and find it configured.
The failure that matters most is quieter: a push that deletes from the shared
copy something this instance merely could not apply yet.
"""

from __future__ import annotations

import json

import pytest

from server import configio, configsource
from server.models import Pack, Spec, Target

from .test_api_tokens import ADMIN_PASSWORD, ADMIN_USER, _admin_client, _make_auth_settings
from .test_configio import _seed


@pytest.fixture(autouse=True)
def _fresh_state():
    configsource._reset_for_tests()
    yield
    configsource._reset_for_tests()


@pytest.fixture
def admin(settings, fake_driver):
    with _admin_client(_make_auth_settings(settings, ADMIN_USER, ADMIN_PASSWORD),
                       fake_driver) as client:
        yield client


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / "bucket"
    monkeypatch.setenv(configsource.SOURCE_ENV, str(path))
    monkeypatch.delenv(configsource.WRITE_ENV, raising=False)
    return path


def _writable(monkeypatch):
    monkeypatch.setenv(configsource.WRITE_ENV, "1")


def _stored(source):
    return json.loads((source / configsource.OBJECT_NAME).read_text())


def test_a_push_lands_as_one_json_document(db_session, source, monkeypatch):
    _writable(monkeypatch)
    _seed(db_session)
    assert configsource.maybe_push(db_session) is True

    doc = _stored(source)
    assert sorted(t["name"] for t in doc["targets"]) == ["homelab", "sok-dev"]
    assert {s["name"] for s in doc["specs"]} == {"nightly-soak", "smoke"}
    # Secrets travel as the ciphertext they are stored as.
    assert doc["includes_secrets"] is True
    assert any(t["token_encrypted"] == "gAAAAABciphertext1" for t in doc["targets"])


def test_nothing_is_pushed_until_something_changes(db_session, source, monkeypatch):
    _writable(monkeypatch)
    _seed(db_session)
    assert configsource.maybe_push(db_session) is True
    assert configsource.maybe_push(db_session) is False

    db_session.add(Target(name="third", hec_url="https://third.example:8088"))
    db_session.flush()
    assert configsource.maybe_push(db_session) is True
    assert "third" in {t["name"] for t in _stored(source)["targets"]}


def test_a_read_only_instance_never_writes(db_session, source):
    _seed(db_session)
    assert configsource.maybe_push(db_session) is False
    assert not (source / configsource.OBJECT_NAME).exists()


def test_a_fresh_instance_pulls_the_copy(db_session, source, monkeypatch):
    _writable(monkeypatch)
    _seed(db_session)
    configsource.push(db_session)
    doc = _stored(source)

    # Simulate the rebuilt instance: an empty database, the same bucket.
    for row in db_session.query(Spec).all():
        db_session.delete(row)
    for row in db_session.query(Target).all():
        db_session.delete(row)
    db_session.flush()

    report = configsource.load_from_env(db_session)
    assert report["found"] is True
    assert {t.name for t in db_session.query(Target).all()} == {"homelab", "sok-dev"}
    assert {s.name for s in db_session.query(Spec).all()} == {"nightly-soak", "smoke"}
    # Pulling what is already there does not count as a change to push back.
    assert configsource.maybe_push(db_session) is False
    assert _stored(source)["exported_at"] == doc["exported_at"]


def test_an_empty_bucket_is_not_an_error(db_session, source):
    report = configsource.load_from_env(db_session)
    assert report["found"] is False
    assert configsource.describe()["last_pull"]["ok"] is True


def test_an_unreachable_source_never_stops_boot(db_session, monkeypatch):
    monkeypatch.setenv(configsource.SOURCE_ENV, "ftp://nope/nowhere")
    report = configsource.load_from_env(db_session)
    assert "error" in report
    assert configsource.describe()["error"]


def test_a_spec_the_pull_could_not_apply_is_not_deleted_by_the_next_push(
        db_session, source, monkeypatch):
    """The quiet data loss: a spec whose pack is not indexed here yet.

    Pushing this instance's view verbatim would drop it from the shared copy
    on the first unrelated edit.
    """
    _writable(monkeypatch)
    source.mkdir()
    (source / configsource.OBJECT_NAME).write_text(json.dumps({
        "stoker_config_version": configio.CONFIG_VERSION,
        "targets": [{"name": "homelab", "hec_url": "https://hec.example:8088"}],
        "repos": [],
        "specs": [{"name": "needs-a-git-pack", "target": "homelab",
                   "pack": "from-a-repo-not-synced-yet", "engine": "eventgen",
                   "rate_mode": "eps", "rate_value": 10.0, "workers": 1}],
    }))
    report = configsource.load_from_env(db_session)
    assert report["skipped"]
    assert configsource.describe()["carried"] == 1

    db_session.add(Target(name="an-unrelated-edit", hec_url="https://x.example:8088"))
    db_session.flush()
    assert configsource.maybe_push(db_session) is True
    assert "needs-a-git-pack" in {s["name"] for s in _stored(source)["specs"]}

    # Once this instance has the pack and re-pulls, the spec is its own and
    # nothing is carried any more.
    db_session.add(Pack(name="from-a-repo-not-synced-yet", source_path="/packs/x"))
    db_session.flush()
    configsource.pull(db_session)
    assert configsource.describe()["carried"] == 0
    assert db_session.query(Spec).filter_by(name="needs-a-git-pack").one()


def test_a_copy_under_another_master_key_keeps_the_secrets_set_here(
        db_session, source, monkeypatch):
    """Ciphertext this instance cannot read must not replace one it can."""
    monkeypatch.setenv("STOKER_MASTER_KEY", "key-here")
    db_session.add(Target(name="homelab", hec_url="https://hec.example:8088",
                          token_encrypted="readable-here"))
    db_session.flush()
    source.mkdir()
    (source / configsource.OBJECT_NAME).write_text(json.dumps({
        "stoker_config_version": configio.CONFIG_VERSION,
        "master_key_fingerprint": "deadbeef",
        "targets": [{"name": "homelab", "hec_url": "https://moved.example:8088",
                     "token_encrypted": "unreadable-here"}],
    }))
    report = configsource.pull(db_session)
    target = db_session.query(Target).filter_by(name="homelab").one()
    assert target.token_encrypted == "readable-here"
    assert target.hec_url == "https://moved.example:8088"
    assert any("kept" in w for w in report["warnings"])


def test_the_page_can_pull_and_push(admin, source, monkeypatch):
    client = admin
    info = client.get("/api/config/source").json()
    assert info["configured"] is True and info["writable"] is False
    assert info["kind"] == "directory"

    # Read-only: Push is refused rather than bypassing the switch.
    assert client.post("/api/config/source/push").status_code == 409
    assert client.post("/api/config/source/pull").json()["found"] is False

    _writable(monkeypatch)
    pushed = client.post("/api/config/source/push")
    assert pushed.status_code == 200, pushed.text
    assert (source / configsource.OBJECT_NAME).exists()
    assert client.get("/api/config/source").json()["last_push"]["ok"] is True


def test_the_page_says_when_no_source_is_set(admin, monkeypatch):
    client = admin
    monkeypatch.delenv(configsource.SOURCE_ENV, raising=False)
    assert client.get("/api/config/source").json()["configured"] is False
    assert client.post("/api/config/source/pull").status_code == 409
