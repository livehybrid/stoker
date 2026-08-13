"""Boot-time builtin pack seeding (``STOKER_BUILTIN_PACKS_DIR``).

``lifecycle.seed_builtin_packs`` registers every pack root under the configured
directory as a local Pack row at boot, so the image's bundled starter packs
appear with no sideloading. Covered here: the off-by-default no-op, discovery
and registration, idempotency/refresh, name-clash skip vs dead-path repoint,
boot wiring through ``create_app`` — and, as an end-to-end guarantee, that the
REAL bundled ``packs/`` set in this repo seeds clean (every pack lints ok, the
metric packs carry their builder config, apigw registers under its declared
name ``api-gateway``).
"""

from __future__ import annotations

import dataclasses
import os

from sqlalchemy import select

from server import config as config_mod
from server import lifecycle
from server.models import Pack

from . import _helpers

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BUNDLED_PACKS_DIR = os.path.join(_REPO_ROOT, "packs")


def _with_dir(settings, path):
    return dataclasses.replace(settings, builtin_packs_dir=path)


def _rows(db):
    return list(db.execute(select(Pack)).scalars().all())


# --------------------------------------------------------------------------- #
# Off by default; missing dir is a warned no-op.
# --------------------------------------------------------------------------- #

def test_seed_is_a_noop_without_a_dir(db_session, settings):
    counts = lifecycle.seed_builtin_packs(db_session, settings=settings)
    assert counts == {"packs_seeded": 0, "packs_updated": 0, "packs_skipped": 0}
    assert _rows(db_session) == []


def test_seed_missing_dir_is_a_noop(db_session, settings, tmp_path):
    counts = lifecycle.seed_builtin_packs(
        db_session, settings=_with_dir(settings, str(tmp_path / "nope")))
    assert counts["packs_seeded"] == 0
    assert _rows(db_session) == []


# --------------------------------------------------------------------------- #
# Discovery + registration.
# --------------------------------------------------------------------------- #

def test_seed_registers_pack_roots_and_ignores_strays(db_session, settings,
                                                      make_pack, tmp_path):
    make_pack()  # -> tmp_path/flatline-test (pack.yaml name: flatline-test)
    (tmp_path / "not-a-pack").mkdir()
    (tmp_path / "not-a-pack" / "README.md").write_text("nope", encoding="utf-8")
    (tmp_path / "stray-file.txt").write_text("nope", encoding="utf-8")

    counts = lifecycle.seed_builtin_packs(
        db_session, settings=_with_dir(settings, str(tmp_path)))
    assert counts["packs_seeded"] == 1

    rows = _rows(db_session)
    assert len(rows) == 1
    pack = rows[0]
    assert pack.name == "flatline-test"          # the pack.yaml name
    assert pack.repo_id is None                  # a local pack, not repo-synced
    assert pack.lint_status == "ok" and pack.verified
    assert pack.engines_json == ["eventgen"]
    assert "flat test pack" in (pack.description or "")


def test_seed_is_idempotent_and_refreshes_metadata(db_session, settings,
                                                   make_pack, tmp_path):
    pack_dir = make_pack()
    cfg = _with_dir(settings, str(tmp_path))
    lifecycle.seed_builtin_packs(db_session, settings=cfg)

    counts = lifecycle.seed_builtin_packs(db_session, settings=cfg)
    assert counts == {"packs_seeded": 0, "packs_updated": 1, "packs_skipped": 0}
    assert len(_rows(db_session)) == 1

    # An image upgrade edits a pack: the next boot re-lints and refreshes.
    yaml_path = os.path.join(pack_dir, "pack.yaml")
    with open(yaml_path, "r", encoding="utf-8") as fh:
        body = fh.read()
    with open(yaml_path, "w", encoding="utf-8") as fh:
        fh.write(body.replace("tiny flat test pack", "renovated pack"))
    lifecycle.seed_builtin_packs(db_session, settings=cfg)
    pack = _rows(db_session)[0]
    assert "renovated pack" in (pack.description or "")


def test_seed_skips_a_name_owned_by_another_pack(db_session, settings,
                                                 make_pack, tmp_path):
    import shutil

    # A builtin pack whose pack.yaml declares name "flatline-test", isolated in
    # its own seeding dir.
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    shutil.move(make_pack(name="flatline-test"),
                str(builtin_dir / "flatline-test"))
    # The operator already registered a pack under that NAME from a different,
    # still-existing directory; the builtin copy must not displace it.
    theirs_dir = make_pack(name="theirs-dir")
    theirs = _helpers.make_pack(db_session, theirs_dir, name="flatline-test")
    db_session.commit()

    counts = lifecycle.seed_builtin_packs(
        db_session, settings=_with_dir(settings, str(builtin_dir)))
    assert counts["packs_skipped"] == 1
    rows = _rows(db_session)
    assert len(rows) == 1
    assert rows[0].id == theirs.id
    assert rows[0].source_path == theirs_dir  # untouched


def test_seed_repoints_a_dead_path_local_row(db_session, settings, make_pack,
                                             tmp_path):
    # The same builtin pack registered from an old image layout whose path no
    # longer exists: the row is adopted and repointed, not stranded broken.
    pack_dir = make_pack()
    stale = Pack(name="flatline-test", source_path=str(tmp_path / "gone-away"),
                 lint_status="ok", verified=True)
    db_session.add(stale)
    db_session.commit()

    counts = lifecycle.seed_builtin_packs(
        db_session, settings=_with_dir(settings, str(tmp_path)))
    assert counts == {"packs_seeded": 0, "packs_updated": 1, "packs_skipped": 0}
    rows = _rows(db_session)
    assert len(rows) == 1
    assert rows[0].id == stale.id
    assert rows[0].source_path == pack_dir
    assert rows[0].lint_status == "ok"


# --------------------------------------------------------------------------- #
# The real bundled set + boot wiring.
# --------------------------------------------------------------------------- #

def test_seed_the_repo_bundled_packs_end_to_end(db_session, settings):
    counts = lifecycle.seed_builtin_packs(
        db_session, settings=_with_dir(settings, _BUNDLED_PACKS_DIR))
    assert counts["packs_skipped"] == 0
    assert counts["packs_seeded"] >= 15  # the full starter set

    by_name = {p.name: p for p in _rows(db_session)}
    # Every bundled pack must lint clean — a broken starter pack is a bug.
    assert all(p.lint_status == "ok" for p in by_name.values()), {
        n: p.lint_errors_json for n, p in by_name.items() if p.lint_status != "ok"}
    # Named by pack.yaml, not directory (packs/apigw declares api-gateway).
    assert "api-gateway" in by_name and "apigw" not in by_name
    assert by_name["flatline"].engines_json == ["eventgen"]
    assert by_name["attack-replay"].engines_json == ["rawreplay"]
    # Directory metric packs carry their metricgen builder config.
    assert by_name["api-service-red-metrics"].builder_config_json is not None


def test_boot_seeds_builtin_packs(settings, db_engine, make_pack, tmp_path):
    make_pack()
    config_mod.set_settings(_with_dir(settings, str(tmp_path)))
    from server.app import create_app
    from server.db import SessionLocal

    create_app()
    with SessionLocal() as db:
        names = [p.name for p in _rows(db)]
    assert names == ["flatline-test"]
