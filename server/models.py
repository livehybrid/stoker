"""ORM models: the DB source of truth for the control plane.

Every table from the contract's Data model section. Design points:

* ``*_json`` columns use ``JSONB().with_variant(JSON(), "sqlite")`` so prod is
  Postgres JSONB and the test suite runs on SQLite.
* Timestamps are timezone-aware UTC (``DateTime(timezone=True)``); defaults are
  set in Python via :func:`utcnow` so both backends agree.
* Secret material (target HEC tokens) lives only in ``token_encrypted`` as
  Fernet ciphertext and is never serialised into a response schema.

String enum-like columns (states, health, lint status) are stored as plain
strings; the allowed values are documented on each column and enforced by the
lifecycle/route layers, keeping the schema dialect-agnostic and migration-light.
"""

from __future__ import annotations

import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# JSON column type: Postgres JSONB in prod, generic JSON on SQLite for tests.
JSON_VARIANT = JSONB().with_variant(JSON(), "sqlite")


def utcnow():
    # type: () -> datetime.datetime
    """Timezone-aware current UTC instant (models default to this)."""
    return datetime.datetime.now(datetime.timezone.utc)


def _ts_column(**kwargs):
    # type: (Any) -> Mapped[Optional[datetime.datetime]]
    return mapped_column(DateTime(timezone=True), **kwargs)


class Target(Base):
    """A Splunk HEC destination. ``token_encrypted`` holds Fernet ciphertext."""

    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hec_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Secret: Fernet ciphertext of the HEC token. Never serialised.
    token_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    default_index: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    verify_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    env_tag: Mapped[str] = mapped_column(String(32), nullable=False, default="lab")  # lab | prod
    max_concurrent_gb_day: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # unknown | green | amber | red
    health_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    health_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_health_at: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)
    lifetime_gb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)

    specs: Mapped[list["Spec"]] = relationship(back_populates="target")


class Pack(Base):
    """A registered local eventgen pack directory plus lint metadata."""

    __tablename__ = "packs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    engines_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    sourcetypes_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    stanza_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    est_bytes_per_event: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    declared_per_day_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ok | error | unknown
    lint_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    lint_errors_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    # null this stage (git indexing arrives with gitsync).
    indexed_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)

    bundles: Mapped[list["Bundle"]] = relationship(back_populates="pack")
    specs: Mapped[list["Spec"]] = relationship(back_populates="pack")


class Bundle(Base):
    """An immutable content-addressed tarball built from a pack directory."""

    __tablename__ = "bundles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pack_id: Mapped[Optional[int]] = mapped_column(ForeignKey("packs.id"), nullable=True)
    # sha256 of the tarball bytes; content-addressed dedup key.
    digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)

    pack: Mapped[Optional[Pack]] = relationship(back_populates="bundles")
    runs: Mapped[list["Run"]] = relationship(back_populates="bundle")


class Spec(Base):
    """A JobSpec: what to run, where, how fast, how many workers."""

    __tablename__ = "specs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    pack_id: Mapped[int] = mapped_column(ForeignKey("packs.id"), nullable=False)
    ref: Mapped[str] = mapped_column(String(255), nullable=False, default="local")
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="eventgen")
    # index/sourcetype/source/host; values may contain the "{slot}" template.
    overrides_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    # eps | per_day_gb | count_interval
    rate_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="eps")
    rate_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    interval_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    workers: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    duration_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # null = unbounded
    fleet: Mapped[str] = mapped_column(String(64), nullable=False, default="swarm-local")
    strict_release: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    driver_opts_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    created_at: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)

    pack: Mapped[Pack] = relationship(back_populates="specs")
    target: Mapped[Target] = relationship(back_populates="specs")
    runs: Mapped[list["Run"]] = relationship(back_populates="spec")


class Run(Base):
    """One execution of a spec: a fleet of workers driven through the lifecycle."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    spec_id: Mapped[int] = mapped_column(ForeignKey("specs.id"), nullable=False)
    # Frozen, non-secret snapshot of the spec (+ target by id, non-secret fields).
    spec_snapshot_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    resolved_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    bundle_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bundles.id"), nullable=True)
    # pending | preparing | provisioning | releasing | running | draining |
    # completed | stopped | failed
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    jwt_kid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    driver_ref_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    started_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)
    t0: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)  # null until release
    ended_at: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)
    end_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    totals_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)

    spec: Mapped[Spec] = relationship(back_populates="runs")
    bundle: Mapped[Optional[Bundle]] = relationship(back_populates="runs")
    leases: Mapped[list["WorkerLease"]] = relationship(
        back_populates="run", cascade="all, delete-orphan")
    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan")
    samples: Mapped[list["MetricSample"]] = relationship(
        back_populates="run", cascade="all, delete-orphan")


class WorkerLease(Base):
    """A single slot's lease: the unit of fencing identity for a run.

    Unique on (run_id, slot); ``lease_id`` is globally unique. The share is a
    single-key JSON matching the run's rate mode (eps | per_day_gb | count).
    """

    __tablename__ = "worker_leases"
    __table_args__ = (UniqueConstraint("run_id", "slot", name="uq_worker_leases_run_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    # One key: eps | per_day_gb | count.
    share_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    lease_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    holder: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    node: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # free | claimed | ready | running | lost | done
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="free")
    last_heartbeat_at: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)
    effective_t0: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)
    restarts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    final_log_tail_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)

    run: Mapped[Run] = relationship(back_populates="leases")


class MetricSample(Base):
    """One heartbeat's counters, appended per successful heartbeat."""

    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)

    events_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bytes_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    eps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hec_2xx: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hec_4xx: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hec_5xx: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hec_timeouts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retries: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    queue_depth: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    lag_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rss_mb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cpu_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    run: Mapped[Run] = relationship(back_populates="samples")


class RunEvent(Base):
    """Append-only audit trail; every state transition writes one row."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    ts: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)
    actor: Mapped[str] = mapped_column(String(16), nullable=False, default="system")  # system|operator|agent
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    detail_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)

    run: Mapped[Run] = relationship(back_populates="events")


class Fleet(Base):
    """A deployment target for worker fleets (a driver + its config)."""

    __tablename__ = "fleets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    driver: Mapped[str] = mapped_column(String(16), nullable=False)  # swarm | k8s | fake
    config_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    version_info: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)
    created_at: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)


__all__ = [
    "utcnow",
    "Target",
    "Pack",
    "Bundle",
    "Spec",
    "Run",
    "WorkerLease",
    "MetricSample",
    "RunEvent",
    "Fleet",
]
