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
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
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


class Repo(Base):
    """A git repository containing one or more sample packs.

    ``secret_encrypted`` holds Fernet ciphertext of the credential (a PAT or an
    SSH deploy key) and is never serialised into any response. ``webhook_secret``
    is the shared secret the GitHub push webhook is HMAC-verified against.
    ``trusted_code`` gates the custom-code default-deny: only a repo an admin has
    explicitly flagged trusted keeps ``bin/`` and ``generator =`` stanzas; every
    other repo has them stripped/rejected at index time.
    """

    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    # none | pat | deploy_key
    auth_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # Secret: Fernet ciphertext of the PAT / deploy key. Write-only, never echoed.
    secret_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    default_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    head_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)
    sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Shared secret for the GitHub push webhook HMAC. Generated on create.
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    trusted_code: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)

    packs: Mapped[list["Pack"]] = relationship(back_populates="repo")


class Pack(Base):
    """A registered eventgen pack (a local directory, or one indexed from a repo).

    A pack registered via ``POST /api/packs`` has a local ``source_path`` and a
    null ``repo_id``. A pack discovered by the git-sync engine carries the
    ``repo_id`` of the repo it came from and ``indexed_sha`` = the repo head SHA
    it was indexed at; its ``source_path`` is the pack's directory inside the
    repo clone at that point.
    """

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
    # Repo this pack was indexed from (null for a locally-registered pack).
    repo_id: Mapped[Optional[int]] = mapped_column(ForeignKey("repos.id"), nullable=True)
    # The repo head SHA this pack was last indexed at (null for a local pack).
    indexed_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # UI-authored builder config (currently metrics packs): the `metricgen`
    # object the bundle is synthesised from. Null for a normal (directory/repo)
    # pack. When set, the pack has no meaningful source_path and its bundle is
    # built via bundles.build_from_metrics_config rather than from disk.
    builder_config_json: Mapped[Optional[Any]] = mapped_column(JSON_VARIANT, nullable=True)
    created_at: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)

    bundles: Mapped[list["Bundle"]] = relationship(back_populates="pack")
    specs: Mapped[list["Spec"]] = relationship(back_populates="pack")
    repo: Mapped[Optional["Repo"]] = relationship(back_populates="packs")


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
    # A long soak appends one row per slot every ~5 s, so this table grows
    # unbounded. Index the hot access paths so they never full-scan it: the
    # per-run/slot latest-sample read (supervisor + dogfood aggregate + UI) hits
    # the composite, and the maintenance roll-up/prune (which scans by ``ts``)
    # hits the time index. Postgres does not auto-index foreign keys, so the
    # composite's leading ``run_id`` also serves the by-run cascade/read.
    __table_args__ = (
        Index("ix_metric_samples_run_slot_ts", "run_id", "slot", "ts"),
        Index("ix_metric_samples_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)

    # Cumulative, monotonic counters (the agent reports a running total each
    # heartbeat). BigInteger, NOT Integer: on Postgres a 32-bit column overflows
    # at ~2.1e9 (bytes_total crosses 2.1 GB in minutes of load, events_total on a
    # long high-eps soak), and the failing INSERT would 500 every subsequent
    # heartbeat and abort the run. SQLite renders BigInteger as its dynamic
    # INTEGER, so the test backend is unaffected either way.
    events_total: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    bytes_total: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
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
    # Append-only and never pruned, so it grows for the life of the instance.
    # Index (run_id, ts) so the per-run audit reads (UI run detail, the
    # last-transition and auth-failed-slot lookups) never full-scan it.
    __table_args__ = (
        Index("ix_run_events_run_ts", "run_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    ts: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)
    # Holds the same identity range as runs.started_by (String(255)): "system",
    # "operator", "agent", a real username, or a "token:<name>" service-token
    # principal. It was String(16) ("system|operator|agent") before per-actor
    # audit attribution landed; a longer principal then overflowed it on Postgres
    # (a 500 at provision), which SQLite's non-enforcement of varchar length hid.
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="system")
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


class User(Base):
    """An operator/admin/viewer account for the app-level auth subsystem.

    Two kinds of user, distinguished by ``source``:

    * ``local`` — a password account. ``password_hash`` holds a passlib bcrypt
      hash (never a plaintext, never serialised). Created from the env admin,
      via first-access setup, or by an admin through ``/api/users``.
    * ``proxy`` — asserted by a trusted reverse proxy (Traefik forward-auth to
      an IdP) via the configured auth header. Has no password (``password_hash``
      is null); created-on-first-sight the first time the trusted proxy names it.

    ``role`` is one of ``viewer`` | ``operator`` | ``admin`` and is the sole
    authorisation input (admin gates user management). ``active`` False locks an
    account out of login without deleting its audit trail. The bcrypt hash is
    the only secret on the row and is excluded from every response schema.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Secret: passlib bcrypt hash. Null for proxy/SSO users. Never serialised.
    password_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # viewer | operator | admin
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="operator")
    # local | proxy
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)
    last_login_at: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)


class ApiToken(Base):
    """A non-interactive API token for CI/CD and machine callers.

    A token is a bearer credential presented as ``Authorization: Bearer stk_...``
    so an automated caller can drive the operator API without a browser session.
    The secret (``stk_`` + url-safe random) is shown exactly once, at create time;
    only its ``token_hash`` (sha256 hex) is stored, so the plaintext can never be
    recovered from the row and is never serialised or logged. Lookup is by the
    indexed hash column, which is inherently constant-time (no string compare of
    the raw secret).

    ``role`` (viewer | operator | admin) is the sole authorisation input, exactly
    like a :class:`User`: a token authenticates as a **transient** principal with
    that role; it is never a persisted user and holds no session. ``token_prefix``
    (the first ~12 chars, e.g. ``stk_ab12cd34``) is safe to display so the operator
    can tell tokens apart in the list. ``revoked_at`` is a soft-revoke: setting it
    disables the token while keeping the audit row (``created_by`` / ``created_at``
    / ``last_used_at``) intact. ``expires_at`` is an optional hard expiry.
    """

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # sha256 hex of the secret. The only representation of the token we keep;
    # indexed for the auth-time lookup. Never the plaintext.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False)
    # First ~12 chars of the secret (e.g. "stk_ab12cd34"): display-only, not a
    # credential (far too short to guess the rest of the entropy).
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # viewer | operator | admin
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="viewer")
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = _ts_column(nullable=False, default=utcnow)
    expires_at: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)
    last_used_at: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)
    # Soft-revoke: set to disable the token while keeping the audit row.
    revoked_at: Mapped[Optional[datetime.datetime]] = _ts_column(nullable=True)


__all__ = [
    "utcnow",
    "Target",
    "Repo",
    "Pack",
    "Bundle",
    "Spec",
    "Run",
    "WorkerLease",
    "MetricSample",
    "RunEvent",
    "Fleet",
    "User",
    "ApiToken",
]
