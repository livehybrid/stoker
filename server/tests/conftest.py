"""Shared pytest fixtures for the control plane suite.

Provides:

* ``settings`` — a fresh :class:`~server.config.Settings` bound to a temp SQLite
  file and a fixed Fernet master key, installed as the process singleton so
  every module that calls :func:`get_settings` sees the test config.
* ``db_engine`` / ``db_session`` — an isolated SQLite engine with the schema
  created, and a session for direct DB assertions.
* ``app`` / ``client`` — the FastAPI app built against the temp DB and a
  ``TestClient``. The app's own ``get_db`` already uses the configured
  ``SessionLocal`` (same engine), so no override is strictly required; an
  explicit override is still installed for clarity and to let a test inject a
  transactional session if it wants.
* ``fake_driver`` — a shared :class:`~server.drivers.fake.FakeDriver`, also
  registered for the ``fake-local`` and ``swarm-local`` fleets so lifecycle code
  that resolves a driver by fleet name gets this instance.
* ``make_pack`` — a factory building a tiny flat eventgen pack in a tmp dir.

The fixtures deliberately reconfigure the global engine (the app is a
process-level singleton keyed off settings); each test module runs against its
own temp DB file to stay isolated.
"""

from __future__ import annotations

import os
from typing import Callable

import pytest

from server import config as config_mod
from server import db as db_mod
from server import drivers as drivers_mod
from server.config import Settings
from server.crypto import generate_master_key


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    # type: (...) -> Settings
    """Install a fresh Settings singleton bound to a temp SQLite DB + bundle dir."""
    db_path = tmp_path / "stoker-test.db"
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    repo_clone_dir = tmp_path / "repos"
    repo_clone_dir.mkdir()
    test_settings = Settings(
        database_url="sqlite:///%s" % db_path,
        master_key=generate_master_key(),
        master_key_generated=False,
        jwt_ttl_s=3600,
        public_base_url="http://testserver",
        worker_image="ghcr.io/livehybrid/stoker-worker:test",
        portainer_host=None,
        portainer_token=None,
        portainer_endpoint=6,
        bundle_dir=str(bundle_dir),
        repo_clone_dir=str(repo_clone_dir),
        dogfood_hec_url=None,
        dogfood_hec_token=None,
        port=8080,
    )
    config_mod.set_settings(test_settings)
    # Point the engine at this DB before anything connects.
    db_mod.configure(test_settings.database_url)
    yield test_settings
    config_mod.reset_settings()


@pytest.fixture()
def db_engine(settings):
    # type: (...) -> object
    """Create the schema on the configured engine and hand it back."""
    db_mod.create_all()
    engine = db_mod.get_engine()
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    # type: (...) -> object
    """A plain session for direct DB assertions (commits are the test's job)."""
    session = db_mod.SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def fake_driver():
    # type: (...) -> object
    """A shared FakeDriver, registered for the seeded fleet names."""
    from server.drivers.fake import FakeDriver

    driver = FakeDriver()
    drivers_mod.clear_cache()
    drivers_mod.register_driver("fake-local", driver)
    drivers_mod.register_driver("swarm-local", driver)
    yield driver
    drivers_mod.clear_cache()


@pytest.fixture()
def app(settings, db_engine, fake_driver):
    # type: (...) -> object
    """Build the FastAPI app against the temp DB (schema already created)."""
    from server.app import create_app
    from server.db import get_db

    application = create_app()

    # Explicit get_db override sharing the configured SessionLocal. This is the
    # same engine the app already uses; the override documents the seam and lets
    # a future test swap in a transactional session.
    def _override_get_db():
        session = db_mod.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    application.dependency_overrides[get_db] = _override_get_db
    yield application
    application.dependency_overrides.clear()


@pytest.fixture()
def client(app):
    # type: (...) -> object
    """A TestClient that runs the app's lifespan (supervisor loop included)."""
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def make_pack(tmp_path):
    # type: (...) -> Callable[..., str]
    """Return a factory that writes a tiny flat eventgen pack and returns its dir.

    The pack has one sample-mode stanza, a matching sample file, a timestamp
    token and a ``pack.yaml`` declaring ``bytes_per_event``. It passes
    :func:`server.bundles.lint_pack` and builds into a bundle unchanged.
    """
    def _make(name="flatline-test", count=100, bytes_per_event=120):
        # type: (str, int, int) -> str
        pack_dir = tmp_path / name
        (pack_dir / "default").mkdir(parents=True)
        (pack_dir / "samples").mkdir()
        conf = (
            "[%s.sample]\n"
            "mode = sample\n"
            "interval = 1\n"
            "count = %d\n"
            "earliest = -1s\n"
            "latest = now\n"
            "\n"
            "token.0.token = \\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\n"
            "token.0.replacementType = timestamp\n"
            "token.0.replacement = %%Y-%%m-%%dT%%H:%%M:%%S\n"
        ) % (name, count)
        # The sample file name must match the stanza name (eventgen resolves the
        # stanza against sampleDir); here "<name>.sample".
        (pack_dir / "default" / "eventgen.conf").write_text(conf, encoding="utf-8")
        sample_lines = "\n".join(
            "2026-01-01T00:00:%02d event line %d payload=abcdefghij" % (i % 60, i)
            for i in range(20)
        )
        (pack_dir / "samples" / ("%s.sample" % name)).write_text(
            sample_lines + "\n", encoding="utf-8")
        pack_yaml = (
            "name: %s\n"
            "engine: eventgen\n"
            "description: \"tiny flat test pack\"\n"
            "estimates:\n"
            "  bytes_per_event: %d\n"
            "defaults:\n"
            "  index: main\n"
            "  sourcetype: stoker:%s\n"
        ) % (name, bytes_per_event, name)
        (pack_dir / "pack.yaml").write_text(pack_yaml, encoding="utf-8")
        return str(pack_dir)

    return _make


def _ensure_worker_importable():
    # type: () -> None
    """Best-effort: add worker/ to sys.path so ``stoker_agent`` imports.

    ``bundles._read_pack_yaml`` reuses the worker's pack.yaml parser when
    available; the e2e test (later) also imports the real agent. This makes both
    importable when the suite runs from the repo root without an editable
    install. Absence is tolerated by the callers.
    """
    import sys

    worker_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "worker")
    if os.path.isdir(worker_dir) and worker_dir not in sys.path:
        sys.path.insert(0, worker_dir)


_ensure_worker_importable()
