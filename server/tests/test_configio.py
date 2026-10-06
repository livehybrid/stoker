"""Configuration backup and restore.

The feature exists for a disposable deployment: configure an instance, export
the JSON, destroy the environment including its PersistentVolumeClaims, and
rebuild from the file. So the tests that matter are about surviving that trip -
nothing addressed by a primary key that will not exist on the other side, and a
clear answer when the master key does not come with it.
"""

from __future__ import annotations

import json

import pytest

from server import configio
from server.models import Pack, Repo, Spec, Target


def _seed(db):
    """A small but representative instance: two targets, a repo, two specs."""
    t1 = Target(name="homelab", hec_url="https://hec.example:8088",
                token_encrypted="gAAAAABciphertext1", default_index="main",
                verify_tls=True, env_tag="dev", max_concurrent_gb_day=50.0)
    t2 = Target(name="sok-dev", hec_url="https://sok.example:8088",
                token_encrypted="gAAAAABciphertext2", verify_tls=False)
    repo = Repo(url="https://github.com/livehybrid/stoker-packs",
                auth_kind="pat", secret_encrypted="gAAAAABpat",
                default_ref="main", trusted_code=True,
                webhook_secret="whsec")
    pack = Pack(name="nginx-access", source_path="/packs/nginx-access")
    db.add_all([t1, t2, repo, pack])
    db.flush()
    db.add_all([
        Spec(name="nightly-soak", pack_id=pack.id, target_id=t1.id,
             engine="eventgen", rate_mode="eps", rate_value=5000.0, workers=4,
             duration_s=3600, fleet="swarm-local"),
        Spec(name="smoke", pack_id=pack.id, target_id=t2.id, engine="eventgen",
             rate_mode="count_interval", interval_s=10, workers=1),
    ])
    db.flush()
    return {"targets": [t1, t2], "repo": repo, "pack": pack}


def test_export_carries_configuration_and_not_run_history(db_session):
    _seed(db_session)
    doc = configio.export_config(db_session)

    assert doc["stoker_config_version"] == configio.CONFIG_VERSION
    assert [t["name"] for t in doc["targets"]] == ["homelab", "sok-dev"]
    assert [r["url"] for r in doc["repos"]] == [
        "https://github.com/livehybrid/stoker-packs"]
    assert sorted(s["name"] for s in doc["specs"]) == ["nightly-soak", "smoke"]
    # Configuration only: nothing a run produced belongs in a config backup.
    assert "runs" not in doc and "metrics" not in doc and "packs" not in doc


def test_nothing_is_addressed_by_a_primary_key(db_session):
    """The restored instance allocates its own ids, so none may travel.

    This is the property that makes the file portable at all; an exported
    ``target_id: 7`` would silently attach a spec to whatever happened to be
    row 7 on the new instance.
    """
    _seed(db_session)
    doc = configio.export_config(db_session)

    spec = next(s for s in doc["specs"] if s["name"] == "nightly-soak")
    assert spec["target"] == "homelab"
    assert spec["pack"] == "nginx-access"
    assert "target_id" not in spec and "pack_id" not in spec and "id" not in spec
    for row in doc["targets"] + doc["repos"]:
        assert "id" not in row


def test_a_round_trip_onto_an_empty_instance_restores_everything(db_session, db_engine):
    """The whole point: export, destroy, rebuild from the file."""
    _seed(db_session)
    doc = json.loads(json.dumps(configio.export_config(db_session), default=str))

    # A fresh instance: no targets, no repos, no specs. The pack is present
    # because packs come back from their repo or the bundled set, not from the
    # config file.
    for model in (Spec, Target, Repo):
        for row in db_session.scalars(__import__("sqlalchemy").select(model)).all():
            db_session.delete(row)
    db_session.flush()

    report = configio.import_config(db_session, doc)
    assert (report["targets"], report["repos"], report["specs"]) == (2, 1, 2)
    assert report["skipped"] == []

    from sqlalchemy import select

    homelab = db_session.scalar(select(Target).where(Target.name == "homelab"))
    assert homelab.hec_url == "https://hec.example:8088"
    assert homelab.default_index == "main"
    assert homelab.max_concurrent_gb_day == 50.0
    # Secrets travel as the ciphertext they are stored as.
    assert homelab.token_encrypted == "gAAAAABciphertext1"

    repo = db_session.scalar(select(Repo))
    assert repo.auth_kind == "pat" and repo.trusted_code is True
    assert repo.secret_encrypted == "gAAAAABpat"

    spec = db_session.scalar(select(Spec).where(Spec.name == "nightly-soak"))
    assert spec.rate_value == 5000.0 and spec.workers == 4
    assert spec.target.name == "homelab"
    assert spec.pack.name == "nginx-access"


def test_importing_twice_changes_nothing(db_session):
    """Idempotent, so a half-finished restore can simply be run again."""
    _seed(db_session)
    doc = json.loads(json.dumps(configio.export_config(db_session), default=str))

    second = configio.import_config(db_session, doc)
    assert (second["targets"], second["repos"], second["specs"]) == (0, 0, 0)

    from sqlalchemy import select
    assert len(db_session.scalars(select(Target)).all()) == 2
    assert len(db_session.scalars(select(Spec)).all()) == 2


def test_an_edit_to_the_file_is_applied_on_reimport(db_session):
    _seed(db_session)
    doc = json.loads(json.dumps(configio.export_config(db_session), default=str))
    for t in doc["targets"]:
        if t["name"] == "homelab":
            t["default_index"] = "loadtest"
    configio.import_config(db_session, doc)

    from sqlalchemy import select
    assert db_session.scalar(
        select(Target).where(Target.name == "homelab")).default_index == "loadtest"


def test_a_spec_whose_pack_is_not_indexed_is_reported_not_fatal(db_session):
    """The normal partial case, and it must not lose the rest of the restore.

    A spec names a pack whose repo this same import just created; the repo has
    not synced yet, so the pack does not exist. Failing the whole restore would
    leave the operator with nothing, so it is skipped by name and the next
    import picks it up.
    """
    _seed(db_session)
    doc = json.loads(json.dumps(configio.export_config(db_session), default=str))
    doc["specs"].append({"name": "future", "pack": "not-indexed-yet",
                         "target": "homelab", "engine": "eventgen"})

    report = configio.import_config(db_session, doc)
    assert any("not-indexed-yet" in s for s in report["skipped"])
    # ...and everything else still applied.
    from sqlalchemy import select
    assert db_session.scalar(select(Spec).where(Spec.name == "nightly-soak")) is not None


def test_excluding_secrets_gives_a_file_safe_to_commit(db_session):
    _seed(db_session)
    doc = configio.export_config(db_session, include_secrets=False)

    assert doc["includes_secrets"] is False
    blob = json.dumps(doc)
    assert "gAAAAABciphertext1" not in blob
    assert "gAAAAABpat" not in blob and "whsec" not in blob
    # The configuration itself is still all there, ready for the secrets to be
    # filled in on the restored instance.
    assert [t["name"] for t in doc["targets"]] == ["homelab", "sok-dev"]
    assert doc["targets"][0]["hec_url"] == "https://hec.example:8088"


def test_a_restore_under_the_wrong_master_key_says_so(db_session, monkeypatch):
    """Otherwise it surfaces later as a pile of unexplained auth failures.

    The configuration still restores, because it is still useful and the
    operator may be deliberately re-keying; what must not happen is silence.
    """
    monkeypatch.setenv("STOKER_MASTER_KEY", "key-one")
    _seed(db_session)
    doc = json.loads(json.dumps(configio.export_config(db_session), default=str))
    assert doc["master_key_fingerprint"]

    monkeypatch.setenv("STOKER_MASTER_KEY", "key-two")
    report = configio.import_config(db_session, doc)
    assert any("cannot be decrypted" in w for w in report["warnings"])


def test_the_fingerprint_is_not_the_key(db_session, monkeypatch):
    monkeypatch.setenv("STOKER_MASTER_KEY", "super-secret-master-key")
    fp = configio.master_key_fingerprint()
    assert fp and len(fp) == 8
    assert "super-secret" not in fp


# --------------------------------------------------------------------------- #
# Loading the document
# --------------------------------------------------------------------------- #

def test_the_source_may_be_a_file_or_inline_json(tmp_path):
    """Both are natural in a container: a mounted file, or a plain env var."""
    doc = {"stoker_config_version": 1, "targets": [{"name": "t", "hec_url": "u"}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    assert configio.load_document(str(path))["targets"][0]["name"] == "t"
    assert configio.load_document(json.dumps(doc))["targets"][0]["name"] == "t"


@pytest.mark.parametrize("bad,msg", [
    ("/no/such/file.json", "neither a readable file nor inline JSON"),
    ("{not json", "not valid JSON"),
    ("[1,2,3]", "must be a JSON object"),
])
def test_a_bad_source_says_which_way_it_is_bad(bad, msg):
    with pytest.raises(configio.ConfigError) as exc:
        configio.load_document(bad)
    assert msg in str(exc.value)


def test_a_newer_config_version_is_refused(tmp_path):
    with pytest.raises(configio.ConfigError) as exc:
        configio.load_document(json.dumps({"stoker_config_version": 999}))
    assert "newer Stoker" in str(exc.value)


def test_boot_import_never_raises(db_session, tmp_path, monkeypatch):
    """A typo in a mounted ConfigMap must not stop the control plane starting.

    If it did there would be no way in to fix the typo.
    """
    monkeypatch.setenv(configio.IMPORT_ENV, "/nonexistent/config.json")
    report = configio.import_from_env(db_session)
    assert report is not None and "error" in report

    monkeypatch.delenv(configio.IMPORT_ENV, raising=False)
    assert configio.import_from_env(db_session) is None


def test_boot_import_applies_a_mounted_file(db_session, tmp_path, monkeypatch):
    path = tmp_path / "stoker-config.json"
    path.write_text(json.dumps({
        "stoker_config_version": 1,
        "targets": [{"name": "restored", "hec_url": "https://x:8088",
                     "default_index": "main"}],
    }), encoding="utf-8")
    monkeypatch.setenv(configio.IMPORT_ENV, str(path))

    report = configio.import_from_env(db_session)
    assert report["targets"] == 1

    from sqlalchemy import select
    assert db_session.scalar(
        select(Target).where(Target.name == "restored")).hec_url == "https://x:8088"
