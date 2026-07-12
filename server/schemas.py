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
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Git repo url/ref allowlists (defence against option-injection into git argv).
_ALLOWED_URL_SCHEMES = ("https://", "ssh://", "git://", "file://")
_SCP_URL_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:")
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# Engines the control plane can register/run on a spec. Kept in step with
# ``server.engines.known.ENGINES`` (imported lazily in the validator to avoid a
# heavy import at module load).
from .engines.known import ENGINES as _KNOWN_ENGINES  # noqa: E402

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
# Repos (git repo sync for sample packs)
# --------------------------------------------------------------------------- #

class RepoCreate(BaseModel):
    """Register a git repo. ``secret`` is a PAT or deploy key: write-only, never
    echoed. ``auth_kind`` selects how it is applied (none | pat | deploy_key)."""

    url: str
    auth_kind: str = "none"  # none | pat | deploy_key
    secret: Optional[str] = Field(default=None, repr=False)  # write-only credential
    default_ref: str = "main"
    trusted_code: bool = False

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v):
        # type: (str) -> str
        # An unvalidated url reaches `git clone <url>` as a positional; a value
        # beginning with '-' would be parsed as an option and the ext:: transport
        # runs arbitrary commands. Allowlist real transports and reject a leading
        # dash. (GIT_ALLOW_PROTOCOL and a '--' argv guard back this up in sync.py.)
        v = (v or "").strip()
        if not v:
            raise ValueError("url is required")
        if v.startswith("-"):
            raise ValueError("url must not start with '-'")
        if not (v.startswith(_ALLOWED_URL_SCHEMES) or _SCP_URL_RE.match(v)):
            raise ValueError(
                "url must be https://, ssh://, git://, file:// or scp-style "
                "user@host:path")
        return v

    @field_validator("default_ref")
    @classmethod
    def _validate_default_ref(cls, v):
        # type: (str) -> str
        v = (v or "").strip() or "main"
        if v.startswith("-") or not _SAFE_REF_RE.match(v):
            raise ValueError(
                "default_ref may contain only letters, digits, '.', '_', '/', "
                "'-' and must not start with '-'")
        return v

    @field_validator("auth_kind")
    @classmethod
    def _validate_auth_kind(cls, v):
        # type: (str) -> str
        v = (v or "none").strip()
        if v not in ("none", "pat", "deploy_key"):
            raise ValueError("auth_kind must be none, pat or deploy_key")
        return v


class RepoOut(BaseModel):
    """Repo view. No credential field exists here by construction; ``has_secret``
    reports only whether a credential is stored, never its value.
    ``webhook_secret`` is returned once on create so the operator can configure
    the GitHub webhook; it is not a target/HEC secret."""

    model_config = _ORM

    id: int
    url: str
    auth_kind: str
    has_secret: bool
    default_ref: str
    head_sha: Optional[str] = None
    last_synced_at: Optional[datetime.datetime] = None
    sync_error: Optional[str] = None
    trusted_code: bool
    created_at: datetime.datetime


class RepoCreated(RepoOut):
    """Create response: adds the webhook secret once (so it can be configured on
    the GitHub side). Subsequent GETs never include it."""

    webhook_secret: Optional[str] = None


class RepoSyncResult(BaseModel):
    """Result of a sync (manual or webhook-triggered)."""

    head_sha: Optional[str] = None
    packs_indexed: int
    lint_failures: int


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
    repo_id: Optional[int] = None
    indexed_sha: Optional[str] = None
    created_at: datetime.datetime


class PackPreview(BaseModel):
    """Stanza names plus the first sample lines, for the operator preview."""

    stanzas: List[str]
    sample_lines: Dict[str, List[str]] = Field(default_factory=dict)
    lint_status: str
    lint_errors: List[str] = Field(default_factory=list)


class PackPreviewRun(BaseModel):
    """Rendered preview events from a pack (no fleet, no HEC target).

    A lightweight in-process render of ``n`` events by cycling the pack's sample
    lines and applying the common token replacements (timestamp / ipv4 /
    integer). For pack authoring and the new-job wizard; not a load path."""

    events: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Specs (Appendix A JobSpec)
# --------------------------------------------------------------------------- #

class SpecCreate(BaseModel):
    name: str
    pack_id: int
    target_id: int
    ref: str = "local"
    engine: str = "eventgen"  # eventgen | rawreplay
    overrides: Optional[Dict[str, str]] = None
    rate_mode: str = "eps"  # eps | per_day_gb | count_interval
    rate_value: Optional[float] = None
    interval_s: Optional[int] = None
    workers: int = 1
    duration_s: Optional[int] = None
    fleet: str = "swarm-local"
    strict_release: bool = False
    driver_opts: Optional[Dict[str, Any]] = None

    @field_validator("engine")
    @classmethod
    def _validate_engine(cls, v):
        # type: (str) -> str
        v = (v or "eventgen").strip() or "eventgen"
        if v not in _KNOWN_ENGINES:
            raise ValueError(
                "engine must be one of %s" % ", ".join(_KNOWN_ENGINES))
        return v


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

    @field_validator("engine")
    @classmethod
    def _validate_engine(cls, v):
        # type: (Optional[str]) -> Optional[str]
        if v is None:
            return v
        v = v.strip()
        if v not in _KNOWN_ENGINES:
            raise ValueError(
                "engine must be one of %s" % ", ".join(_KNOWN_ENGINES))
        return v


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


# --------------------------------------------------------------------------- #
# Auth (local users + trusted-proxy SSO)
# --------------------------------------------------------------------------- #

# Allowed roles, most to least privileged; mirrors config.VALID_ROLES.
_AUTH_ROLES = ("viewer", "operator", "admin")


class UserOut(BaseModel):
    """A user view for the operator API. The password hash never appears here
    (there is no field for it by construction) and is never serialised.

    ``id`` / ``created_at`` are optional so a **transient API-token principal**
    (``source="token"``, ``id=None``, no persisted row) also serialises when it
    reaches ``/api/auth/me`` or ``/api/auth/status``; a real user always carries
    both, so this is a widening that changes no persisted-user response."""

    model_config = _ORM

    id: Optional[int] = None
    username: str
    email: Optional[str] = None
    role: str
    source: str  # local | proxy | token
    active: bool
    created_at: Optional[datetime.datetime] = None
    last_login_at: Optional[datetime.datetime] = None


class UserCreate(BaseModel):
    """Create a local user (admin only). ``password`` is write-only and never
    echoed back; a proxy user is created implicitly, not through this shape."""

    username: str
    password: str = Field(repr=False)
    role: str = "operator"
    email: Optional[str] = None

    @field_validator("username")
    @classmethod
    def _validate_username(cls, v):
        # type: (str) -> str
        v = (v or "").strip()
        if not v:
            raise ValueError("username is required")
        if len(v) > 255:
            raise ValueError("username must be at most 255 characters")
        return v

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v):
        # type: (str) -> str
        if not v:
            raise ValueError("password is required")
        return v

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v):
        # type: (str) -> str
        v = (v or "operator").strip()
        if v not in _AUTH_ROLES:
            raise ValueError("role must be one of %s" % ", ".join(_AUTH_ROLES))
        return v


class UserUpdate(BaseModel):
    """Partial update of a user (admin only). Unset fields are left unchanged.
    ``password`` is write-only; setting it rehashes the account's credential."""

    role: Optional[str] = None
    password: Optional[str] = Field(default=None, repr=False)
    active: Optional[bool] = None
    email: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v):
        # type: (Optional[str]) -> Optional[str]
        if v is None:
            return v
        v = v.strip()
        if v not in _AUTH_ROLES:
            raise ValueError("role must be one of %s" % ", ".join(_AUTH_ROLES))
        return v

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v):
        # type: (Optional[str]) -> Optional[str]
        if v is None:
            return v
        if not v:
            raise ValueError("password must not be empty")
        return v


class LoginRequest(BaseModel):
    """Body of ``POST /api/auth/login``. Password is write-only."""

    username: str
    password: str = Field(repr=False)


class SetupRequest(BaseModel):
    """Body of ``POST /api/auth/setup``: create the very first admin.

    Only honoured when zero users exist; the created user is always an admin.
    Password is write-only.
    """

    username: str
    password: str = Field(repr=False)

    @field_validator("username")
    @classmethod
    def _validate_username(cls, v):
        # type: (str) -> str
        v = (v or "").strip()
        if not v:
            raise ValueError("username is required")
        if len(v) > 255:
            raise ValueError("username must be at most 255 characters")
        return v

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v):
        # type: (str) -> str
        if not v:
            raise ValueError("password is required")
        return v


# --------------------------------------------------------------------------- #
# API tokens (non-interactive bearer credentials)
# --------------------------------------------------------------------------- #

class ApiTokenCreate(BaseModel):
    """Create an API token (admin only).

    ``name`` is a unique human label. ``role`` is the authorisation the token
    grants (viewer | operator | admin). ``expires_in_days`` optionally sets a
    hard expiry that many days from now; omit for a non-expiring token.
    """

    name: str
    role: str = "viewer"
    expires_in_days: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v):
        # type: (str) -> str
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        if len(v) > 255:
            raise ValueError("name must be at most 255 characters")
        return v

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v):
        # type: (str) -> str
        v = (v or "viewer").strip()
        if v not in _AUTH_ROLES:
            raise ValueError("role must be one of %s" % ", ".join(_AUTH_ROLES))
        return v

    @field_validator("expires_in_days")
    @classmethod
    def _validate_expires(cls, v):
        # type: (Optional[int]) -> Optional[int]
        if v is None:
            return v
        if v <= 0:
            raise ValueError("expires_in_days must be a positive integer")
        return v


class ApiTokenOut(BaseModel):
    """Token metadata (list / management view). Never carries the secret or its
    hash: there is no field for either by construction, so a listing can never
    leak a usable credential."""

    model_config = _ORM

    id: int
    name: str
    role: str
    prefix: str = Field(validation_alias="token_prefix")
    created_by: Optional[str] = None
    created_at: datetime.datetime
    expires_at: Optional[datetime.datetime] = None
    last_used_at: Optional[datetime.datetime] = None
    revoked_at: Optional[datetime.datetime] = None


class ApiTokenCreated(BaseModel):
    """Create response: the ONLY place the plaintext ``token`` is returned.

    The secret is shown once here and is unrecoverable afterwards (only its hash
    is stored). ``prefix`` is the display label that later appears in listings."""

    id: int
    name: str
    role: str
    token: str = Field(repr=False)  # the plaintext secret, returned once
    prefix: str
    created_at: datetime.datetime
    expires_at: Optional[datetime.datetime] = None


class AuthStatus(BaseModel):
    """Public login-page status (safe when unauthenticated).

    * ``authenticated`` — whether this request already carries a valid session
      or a trusted-proxy identity.
    * ``setup_needed`` — zero users exist and no proxy trust is configured, so
      the first-access setup flow should be shown.
    * ``sso_enabled`` — a trusted proxy is configured, so an SSO sign-in path
      exists (the UI may offer it / rely on the proxy redirect).
    * ``user`` — the resolved user when authenticated, else null.
    """

    authenticated: bool
    setup_needed: bool
    sso_enabled: bool
    user: Optional[UserOut] = None


__all__ = [
    # targets
    "TargetCreate", "TargetOut", "TargetTestResult",
    # repos
    "RepoCreate", "RepoOut", "RepoCreated", "RepoSyncResult",
    # packs
    "PackCreate", "PackOut", "PackPreview", "PackPreviewRun",
    # specs
    "SpecCreate", "SpecUpdate", "SpecOut", "SpecEstimate",
    # runs
    "RunLaunch", "RunCreated", "LeaseOut", "RunEventOut", "RunOut", "RunDetail",
    "MetricSampleOut", "MetricsOut", "RunLogsOut",
    "StopRequest", "ScaleRequest", "RescaleRequest",
    # agent
    "ClaimRequest", "BundleRef", "HecSlice", "TelemetrySlice", "SpecSliceOut",
    "ReadyRequest", "HeartbeatRequest", "HeartbeatCommand", "FinalRequest", "Empty",
    # auth
    "UserOut", "UserCreate", "UserUpdate", "LoginRequest", "SetupRequest", "AuthStatus",
    # api tokens
    "ApiTokenCreate", "ApiTokenOut", "ApiTokenCreated",
]
