"""Environment contract parsing for the Stoker worker agent.

Two modes per docs/WORKER-CONTRACT.md: managed (driver-launched, control
plane) and standalone (STOKER_STANDALONE=1, no control plane). Parsing
produces a frozen Config; violations raise ConfigError with a message that
names the offending variable.
"""

from __future__ import annotations

import dataclasses
import os
import socket as _socket
from typing import Mapping, Optional

RATE_MODES = ("eps", "per_day_gb", "count_interval")
ENGINES = ("eventgen",)

DEFAULT_OUTPUT_SOCKET = "/tmp/stoker-output.sock"
DEFAULT_HEARTBEAT_S = 5.0
DEFAULT_OVERDRIVE = 1.15
DEFAULT_CATCHUP_S = 5.0
DEFAULT_METRICS_PORT = 9100
DEFAULT_DEADMAN_S = 600.0
# Whole-drain budget; the contract requires SIGTERM to exit within 45 s, so the
# default leaves margin. Every drain stage is clamped against this.
DEFAULT_DRAIN_BUDGET_S = 40.0


class ConfigError(Exception):
    """Raised when the environment contract is violated."""


@dataclasses.dataclass(frozen=True)
class Config:
    standalone: bool
    # managed mode
    run_id: Optional[str]
    control_url: Optional[str]
    # secret: excluded from repr so an accidental log of the config never leaks
    run_jwt: Optional[str] = dataclasses.field(repr=False)
    holder: str
    hint_slot: Optional[int]
    # standalone mode
    bundle: Optional[str]
    bundle_sha256: Optional[str]
    hec_url: Optional[str]
    index: Optional[str]
    sourcetype: Optional[str]
    host_field: Optional[str]
    source: Optional[str]
    rate_mode: Optional[str]
    rate_value: Optional[float]
    duration_s: Optional[float]
    slot: int
    engine: str
    # both modes; secret, excluded from repr
    hec_token: str = dataclasses.field(repr=False)
    total_workers: int
    # common tuning
    output_socket: str
    heartbeat_s: float
    overdrive: float
    catchup_s: float
    metrics_port: int
    deadman_s: float
    drain_budget_s: float
    hec_verify_tls: bool


def _get(env, key):
    # type: (Mapping[str, str], str) -> Optional[str]
    val = env.get(key)
    if val is None:
        return None
    val = val.strip()
    return val if val else None


def _require(env, key):
    # type: (Mapping[str, str], str) -> str
    val = _get(env, key)
    if val is None:
        raise ConfigError("%s is required and not set" % key)
    return val


def _as_int(key, val, minimum=None):
    # type: (str, str, Optional[int]) -> int
    try:
        parsed = int(val)
    except ValueError:
        raise ConfigError("%s must be an integer, got %r" % (key, val))
    if minimum is not None and parsed < minimum:
        raise ConfigError("%s must be >= %d, got %d" % (key, minimum, parsed))
    return parsed


def _as_float(key, val, minimum=None):
    # type: (str, str, Optional[float]) -> float
    try:
        parsed = float(val)
    except ValueError:
        raise ConfigError("%s must be a number, got %r" % (key, val))
    if minimum is not None and parsed < minimum:
        raise ConfigError("%s must be >= %s, got %s" % (key, minimum, parsed))
    return parsed


def _as_bool(key, val):
    # type: (str, str) -> bool
    low = val.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ConfigError("%s must be a boolean (0/1), got %r" % (key, val))


def load_config(env=None):
    # type: (Optional[Mapping[str, str]]) -> Config
    """Parse the environment contract into a frozen Config."""
    if env is None:
        env = os.environ

    standalone = _get(env, "STOKER_STANDALONE") in ("1", "true", "yes")

    # Common tuning (defaults per contract).
    output_socket = _get(env, "STOKER_OUTPUT_SOCKET") or DEFAULT_OUTPUT_SOCKET
    heartbeat_s = _as_float("STOKER_HEARTBEAT_S",
                            _get(env, "STOKER_HEARTBEAT_S") or str(DEFAULT_HEARTBEAT_S),
                            minimum=0.1)
    overdrive = _as_float("STOKER_OVERDRIVE",
                          _get(env, "STOKER_OVERDRIVE") or str(DEFAULT_OVERDRIVE),
                          minimum=1.0)
    catchup_s = _as_float("STOKER_CATCHUP_S",
                          _get(env, "STOKER_CATCHUP_S") or str(DEFAULT_CATCHUP_S),
                          minimum=0.0)
    metrics_port = _as_int("STOKER_METRICS_PORT",
                           _get(env, "STOKER_METRICS_PORT") or str(DEFAULT_METRICS_PORT),
                           minimum=0)
    deadman_s = _as_float("STOKER_DEADMAN_S",
                          _get(env, "STOKER_DEADMAN_S") or str(DEFAULT_DEADMAN_S),
                          minimum=1.0)
    drain_budget_s = _as_float("STOKER_DRAIN_BUDGET_S",
                               _get(env, "STOKER_DRAIN_BUDGET_S")
                               or str(DEFAULT_DRAIN_BUDGET_S),
                               minimum=1.0)
    hec_verify_tls = _as_bool("STOKER_HEC_VERIFY_TLS",
                              _get(env, "STOKER_HEC_VERIFY_TLS") or "1")

    hec_token = _require(env, "STOKER_HEC_TOKEN")

    engine = _get(env, "STOKER_ENGINE") or "eventgen"
    if engine not in ENGINES:
        raise ConfigError("STOKER_ENGINE must be one of %s, got %r"
                          % (", ".join(ENGINES), engine))

    if standalone:
        bundle = _require(env, "STOKER_BUNDLE")
        hec_url = _require(env, "STOKER_HEC_URL")
        index = _require(env, "STOKER_INDEX")
        rate_mode = _require(env, "STOKER_RATE_MODE")
        if rate_mode not in RATE_MODES:
            raise ConfigError("STOKER_RATE_MODE must be one of %s, got %r"
                              % (", ".join(RATE_MODES), rate_mode))
        rate_value = None
        raw_rate = _get(env, "STOKER_RATE_VALUE")
        if rate_mode != "count_interval":
            if raw_rate is None:
                raise ConfigError("STOKER_RATE_VALUE is required for rate mode %r"
                                  % rate_mode)
            rate_value = _as_float("STOKER_RATE_VALUE", raw_rate, minimum=0.0)
            if rate_value <= 0:
                raise ConfigError("STOKER_RATE_VALUE must be > 0 for rate mode %r"
                                  % rate_mode)
        duration_raw = _get(env, "STOKER_DURATION_S")
        duration_s = None
        if duration_raw is not None:
            duration_s = _as_float("STOKER_DURATION_S", duration_raw, minimum=0.0)
            if duration_s == 0:
                duration_s = None
        slot = _as_int("STOKER_SLOT", _get(env, "STOKER_SLOT") or "0", minimum=0)
        total_workers = _as_int("STOKER_TOTAL_WORKERS",
                                _get(env, "STOKER_TOTAL_WORKERS") or "1", minimum=1)
        if slot >= total_workers:
            raise ConfigError("STOKER_SLOT (%d) must be < STOKER_TOTAL_WORKERS (%d)"
                              % (slot, total_workers))
        return Config(
            standalone=True,
            run_id=None, control_url=None, run_jwt=None,
            holder=_get(env, "STOKER_HOLDER") or _socket.gethostname(),
            hint_slot=None,
            bundle=bundle,
            bundle_sha256=_get(env, "STOKER_BUNDLE_SHA256"),
            hec_url=hec_url,
            index=index,
            sourcetype=_get(env, "STOKER_SOURCETYPE"),
            host_field=_get(env, "STOKER_HOST_FIELD"),
            source=_get(env, "STOKER_SOURCE"),
            rate_mode=rate_mode,
            rate_value=rate_value,
            duration_s=duration_s,
            slot=slot,
            engine=engine,
            hec_token=hec_token,
            total_workers=total_workers,
            output_socket=output_socket,
            heartbeat_s=heartbeat_s,
            overdrive=overdrive,
            catchup_s=catchup_s,
            metrics_port=metrics_port,
            deadman_s=deadman_s,
            drain_budget_s=drain_budget_s,
            hec_verify_tls=hec_verify_tls,
        )

    # Managed mode.
    run_id = _require(env, "STOKER_RUN_ID")
    control_url = _require(env, "STOKER_CONTROL_URL")
    if not (control_url.startswith("http://") or control_url.startswith("https://")):
        raise ConfigError("STOKER_CONTROL_URL must be an http(s) URL, got %r"
                          % control_url)
    run_jwt = _require(env, "STOKER_RUN_JWT")
    total_workers = _as_int("STOKER_TOTAL_WORKERS",
                            _require(env, "STOKER_TOTAL_WORKERS"), minimum=1)
    hint_slot = None
    raw_hint = _get(env, "STOKER_HINT_SLOT")
    if raw_hint is not None:
        hint_slot = _as_int("STOKER_HINT_SLOT", raw_hint, minimum=0)

    return Config(
        standalone=False,
        run_id=run_id,
        control_url=control_url.rstrip("/"),
        run_jwt=run_jwt,
        holder=_get(env, "STOKER_HOLDER") or _socket.gethostname(),
        hint_slot=hint_slot,
        bundle=None, bundle_sha256=None,
        hec_url=None, index=None, sourcetype=None, host_field=None, source=None,
        rate_mode=None, rate_value=None, duration_s=None,
        slot=0,
        engine=engine,
        hec_token=hec_token,
        total_workers=total_workers,
        output_socket=output_socket,
        heartbeat_s=heartbeat_s,
        overdrive=overdrive,
        catchup_s=catchup_s,
        metrics_port=metrics_port,
        deadman_s=deadman_s,
        drain_budget_s=drain_budget_s,
        hec_verify_tls=hec_verify_tls,
    )
