"""Pydantic v2 request/response models for the operator and agent APIs.

Response models never carry secret material (a test asserts no HEC token or
other secret appears in any GET body). Request models validate the shapes the
contract defines; the agent-facing slice / heartbeat-command models must match
``docs/WORKER-CONTRACT.md`` and the worker's ``SpecSlice.from_claim`` /
``ControlClient.heartbeat`` byte-for-byte on the wire.

Field names on the agent models mirror the JSON keys the worker sends and
expects exactly; do not rename them without updating the worker.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Shared config: allow ORM attribute reads for response models.
# --------------------------------------------------------------------------- #

_ORM = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #

class TargetCreate(BaseModel):
    name: str
    hec_url: str
    token: str = Field(repr=False)  # secret in; never echoed back
    default_index: Optional[str] = None
    env_tag: str = "lab"
    max_concurrent_gb_day: Optional[float] = None
    verify_tls: bool = True


class TargetOut(BaseModel):
    """Target view. No token field exists here by construction."""

    model_config = _ORM

    id: int
    name: str
    hec_url: str
    default_index: Optional[str] = None
    verify_tls: bool
    env_tag: str
    max_concurrent_gb_day: Optional[float] = None
    health_state: str
    health_detail: Optional[str] = None
    last_health_at: Optional[datetime.datetime] = None
    lifetime_gb: float
    created_at: datetime.datetime


class TargetTestResult(BaseModel):
    ok: bool
    health: Optional[str] = None
    auth: Optional[str] = None
    latency_ms: Optional[float] = None
    detail: Optional[str] = None


# --------------------------------------------------------------------------- #
# Packs
# --------------------------------------------------------------------------- #

class PackCreate(BaseModel):
    name: str
    source_path: str
    description: Optional[str] = None


class PackOut(BaseModel):
    model_config = _ORM

    id: int
    name: str
    source_path: str
    description: Optional[str] = None
    tags_json: Optional[Any] = None
    engines_json: Optional[Any] = None
    sourcetypes_json: Optional[Any] = None
    stanza_count: Optional[int] = None
    est_bytes_per_event: Optional[float] = None
    declared_per_day_gb: Optional[float] = None
    verified: bool
    lint_status: str
    lint_errors_json: Optional[Any] = None
    indexed_sha: Optional[str] = None
    created_at: datetime.datetime


class PackPreview(BaseModel):
    """Stanza names plus the first sample lines, for the operator preview."""

    stanzas: List[str]
    sample_lines: Dict[str, List[str]] = Field(default_factory=dict)
    lint_status: str
    lint_errors: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Specs (Appendix A JobSpec)
# --------------------------------------------------------------------------- #

class SpecCreate(BaseModel):
    name: str
    pack_id: int
    target_id: int
    ref: str = "local"
    engine: str = "eventgen"
    overrides: Optional[Dict[str, str]] = None
    rate_mode: str = "eps"  # eps | per_day_gb | count_interval
    rate_value: Optional[float] = None
    interval_s: Optional[int] = None
    workers: int = 1
    duration_s: Optional[int] = None
    fleet: str = "swarm-local"
    strict_release: bool = False
    driver_opts: Optional[Dict[str, Any]] = None


class SpecUpdate(BaseModel):
    """Partial update; unset fields are left unchanged."""

    name: Optional[str] = None
    pack_id: Optional[int] = None
    target_id: Optional[int] = None
    ref: Optional[str] = None
    engine: Optional[str] = None
    overrides: Optional[Dict[str, str]] = None
    rate_mode: Optional[str] = None
    rate_value: Optional[float] = None
    interval_s: Optional[int] = None
    workers: Optional[int] = None
    duration_s: Optional[int] = None
    fleet: Optional[str] = None
    strict_release: Optional[bool] = None
    driver_opts: Optional[Dict[str, Any]] = None


class SpecOut(BaseModel):
    model_config = _ORM

    id: int
    name: str
    pack_id: int
    target_id: int
    ref: str
    engine: str
    overrides_json: Optional[Dict[str, Any]] = None
    rate_mode: str
    rate_value: Optional[float] = None
    interval_s: Optional[int] = None
    workers: int
    duration_s: Optional[int] = None
    fleet: str
    strict_release: bool
    driver_opts_json: Optional[Dict[str, Any]] = None
    created_at: datetime.datetime


class SpecEstimate(BaseModel):
    """Per-worker share, ceiling headroom and approximate rates for the UI."""

    workers: int
    rate_mode: str
    per_worker_share: Optional[float] = None
    per_worker_eps: Optional[float] = None
    per_worker_gb_day: Optional[float] = None
    ceiling_pct: Optional[float] = None
    ceiling_limit: Optional[float] = None
    limiting_factor: Optional[str] = None
    ok: bool
    suggested_workers: Optional[int] = None
    detail: Optional[str] = None


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

class RunLaunch(BaseModel):
    """Body for POST /specs/{id}/run: last-minute override values."""

    overrides: Optional[Dict[str, str]] = None


class RunCreated(BaseModel):
    run_id: int
    state: str


class LeaseOut(BaseModel):
    """A lease roster entry (non-secret). ``final_log_tail`` is exposed via
    the logs endpoint, not here."""

    model_config = _ORM

    slot: int
    lease_id: str
    share_json: Optional[Dict[str, Any]] = None
    holder: Optional[str] = None
    node: Optional[str] = None
    state: str
    last_heartbeat_at: Optional[datetime.datetime] = None
    effective_t0: Optional[datetime.datetime] = None
    restarts: int


class RunEventOut(BaseModel):
    model_config = _ORM

    ts: datetime.datetime
    actor: str
    kind: str
    detail_json: Optional[Any] = None


class RunOut(BaseModel):
    """Run summary (list view)."""

    model_config = _ORM

    id: int
    spec_id: int
    state: str
    degraded: bool
    resolved_sha: Optional[str] = None
    bundle_id: Optional[int] = None
    started_by: Optional[str] = None
    created_at: datetime.datetime
    t0: Optional[datetime.datetime] = None
    ended_at: Optional[datetime.datetime] = None
    end_reason: Optional[str] = None
    totals_json: Optional[Any] = None


class RunDetail(RunOut):
    """Run detail (single view): adds the frozen snapshot, roster and events."""

    spec_snapshot_json: Optional[Any] = None
    leases: List[LeaseOut] = Field(default_factory=list)
    events: List[RunEventOut] = Field(default_factory=list)


class MetricSampleOut(BaseModel):
    model_config = _ORM

    slot: int
    ts: datetime.datetime
    events_total: Optional[int] = None
    bytes_total: Optional[int] = None
    eps: Optional[float] = None
    bps: Optional[float] = None
    hec_2xx: Optional[int] = None
    hec_4xx: Optional[int] = None
    hec_5xx: Optional[int] = None
    hec_timeouts: Optional[int] = None
    retries: Optional[int] = None
    queue_depth: Optional[int] = None
    lag_s: Optional[float] = None
    rss_mb: Optional[float] = None
    cpu_pct: Optional[float] = None


class MetricsOut(BaseModel):
    run_id: int
    resolution: str
    window: str
    samples: List[MetricSampleOut] = Field(default_factory=list)


class RunLogsOut(BaseModel):
    run_id: int
    slot: Optional[int] = None
    tail: int
    lines: List[str] = Field(default_factory=list)


class StopRequest(BaseModel):
    force: bool = False


class ScaleRequest(BaseModel):
    workers: int


class RescaleRequest(BaseModel):
    rate_value: float


# --------------------------------------------------------------------------- #
# Agent-facing API (must match the worker on the wire)
# --------------------------------------------------------------------------- #

class ClaimRequest(BaseModel):
    """Body of POST /api/agent/runs/{run_id}/claim (from the worker)."""

    holder: str
    hint_slot: Optional[int] = None
    protocol_version: int = 1


class BundleRef(BaseModel):
    url: str
    sha256: Optional[str] = None


class HecSlice(BaseModel):
    """The ``hec`` object in a slice. The HEC token is NEVER here (the driver
    projects it as an env var; see the worker contract)."""

    url: str
    index: Optional[str] = None
    sourcetype: Optional[str] = None
    gzip: bool = True
    ack: bool = False


class TelemetrySlice(BaseModel):
    interval_s: float = 5


class SpecSliceOut(BaseModel):
    """The claim response: the worker's share of a run.

    Mirrors ``docs/WORKER-CONTRACT.md`` and ``SpecSlice.from_claim`` exactly.
    ``share`` carries exactly one key (eps | per_day_gb | count). ``effective_t0``
    is an ISO 8601 Z string or null. No secret fields.
    """

    run_id: int
    slot: int
    total_workers: int
    lease_id: str
    engine: str
    bundle: BundleRef
    share: Dict[str, float]
    duration_s: Optional[float] = None
    hec: HecSlice
    overrides: Dict[str, str] = Field(default_factory=dict)
    telemetry: TelemetrySlice = Field(default_factory=TelemetrySlice)
    released: bool = False
    effective_t0: Optional[str] = None


class ReadyRequest(BaseModel):
    slot: int
    lease_id: Optional[str] = None


class HeartbeatRequest(BaseModel):
    """Body of POST heartbeat. Counters are parsed defensively (all optional);
    unknown extra keys are ignored so a newer worker never 422s an old server."""

    model_config = ConfigDict(extra="ignore")

    slot: int
    lease_id: Optional[str] = None
    protocol_version: int = 1
    state: Optional[str] = None
    events_total: Optional[int] = None
    bytes_total: Optional[int] = None
    eps: Optional[float] = None
    bps: Optional[float] = None
    hec_2xx: Optional[int] = None
    hec_4xx: Optional[int] = None
    hec_5xx: Optional[int] = None
    hec_timeouts: Optional[int] = None
    retries: Optional[int] = None
    dropped: Optional[int] = None
    queue_depth: Optional[int] = None
    lag_s: Optional[float] = None
    rss_mb: Optional[float] = None
    cpu_pct: Optional[float] = None
    auth_failed: Optional[bool] = None


class HeartbeatCommand(BaseModel):
    """The heartbeat response command.

    ``command`` is one of continue | release | retarget | drain | superseded.
    ``t0`` accompanies release (ISO 8601 Z), ``share`` accompanies retarget, and
    ``jwt`` may ride any response for a rolling refresh. ``exclude_none`` at
    serialisation keeps the body minimal (the worker reads only what it needs).
    """

    model_config = ConfigDict(extra="ignore")

    command: str
    t0: Optional[str] = None
    share: Optional[Dict[str, float]] = None
    jwt: Optional[str] = Field(default=None, repr=False)


class FinalRequest(BaseModel):
    slot: int
    summary: Dict[str, Any] = Field(default_factory=dict)
    log_tail: List[str] = Field(default_factory=list)


class Empty(BaseModel):
    """Empty ``{}`` response body (ready / final / heartbeat-less acks)."""


__all__ = [
    # targets
    "TargetCreate", "TargetOut", "TargetTestResult",
    # packs
    "PackCreate", "PackOut", "PackPreview",
    # specs
    "SpecCreate", "SpecUpdate", "SpecOut", "SpecEstimate",
    # runs
    "RunLaunch", "RunCreated", "LeaseOut", "RunEventOut", "RunOut", "RunDetail",
    "MetricSampleOut", "MetricsOut", "RunLogsOut",
    "StopRequest", "ScaleRequest", "RescaleRequest",
    # agent
    "ClaimRequest", "BundleRef", "HecSlice", "TelemetrySlice", "SpecSliceOut",
    "ReadyRequest", "HeartbeatRequest", "HeartbeatCommand", "FinalRequest", "Empty",
]
