"""Spec slice model.

A SpecSlice is the worker's share of a run: either parsed from a control
plane claim response or synthesised from standalone environment variables.
Both paths produce the same object so everything downstream (bundle fetch,
conf rewrite, pacing, envelope filling) is mode-agnostic.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any, Dict, Optional

from .config import Config

# share key in claim JSON -> internal rate mode
_SHARE_KEYS = {"eps": "eps", "per_day_gb": "per_day_gb", "count": "count_interval"}


class SliceError(Exception):
    """Raised when a claim response or standalone synthesis is invalid."""


def parse_iso8601(value):
    # type: (str) -> float
    """Parse an ISO 8601 timestamp (with Z or numeric offset) to epoch seconds."""
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise SliceError("invalid ISO 8601 timestamp: %r" % value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def format_iso8601(epoch):
    # type: (float) -> str
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


@dataclasses.dataclass
class SpecSlice:
    run_id: Any
    slot: int
    total_workers: int
    lease_id: Optional[str]
    engine: str
    bundle_url: str                 # local path or http(s) URL
    bundle_sha256: Optional[str]
    rate_mode: str                  # eps | per_day_gb | count_interval
    rate_value: Optional[float]     # None in count_interval mode (conf-paced)
    duration_s: Optional[float]
    hec_url: str
    hec_index: Optional[str]
    hec_sourcetype: Optional[str]
    hec_source: Optional[str]
    hec_host: Optional[str]
    hec_gzip: bool
    hec_ack: bool
    overrides: Dict[str, str]
    telemetry_interval_s: float
    released: bool
    effective_t0: Optional[float]   # epoch; pacing anchor for re-issued leases
    # Backfill (both engines): emit a historical window [start, end) then finish.
    # None on start/end = a normal live run. The run is gated at a delivery cap
    # (rate_mode eps) and completes when the engine exits after the window.
    backfill_start_s: Optional[float] = None   # epoch (window start)
    backfill_end_s: Optional[float] = None     # epoch (window end)
    backfill_resolution_s: Optional[float] = None  # metrics step (default: pack resolution)

    @classmethod
    def from_claim(cls, doc):
        # type: (Dict[str, Any]) -> SpecSlice
        """Build a slice from a claim response body per the contract."""
        if not isinstance(doc, dict):
            raise SliceError("claim response must be a JSON object")
        try:
            share = doc["share"]
            bundle = doc["bundle"]
            hec = doc["hec"]
        except KeyError as exc:
            raise SliceError("claim response missing key: %s" % exc)
        if not isinstance(share, dict) or len(share) != 1:
            raise SliceError("share must carry exactly one key, got %r" % (share,))
        share_key = next(iter(share))
        if share_key not in _SHARE_KEYS:
            raise SliceError("unknown share key %r (expected one of %s)"
                             % (share_key, ", ".join(_SHARE_KEYS)))
        rate_mode = _SHARE_KEYS[share_key]
        raw_value = share[share_key]
        rate_value = None
        if raw_value is not None:
            try:
                rate_value = float(raw_value)
            except (TypeError, ValueError):
                raise SliceError("share.%s must be numeric, got %r"
                                 % (share_key, raw_value))
        if rate_mode != "count_interval" and (rate_value is None or rate_value <= 0):
            raise SliceError("share.%s must be > 0, got %r" % (share_key, raw_value))

        if not isinstance(bundle, dict) or not bundle.get("url"):
            raise SliceError("bundle.url is required in the claim response")
        if not isinstance(hec, dict) or not hec.get("url"):
            raise SliceError("hec.url is required in the claim response")

        effective_t0 = None
        raw_t0 = doc.get("effective_t0")
        if raw_t0:
            effective_t0 = parse_iso8601(raw_t0)

        telemetry = doc.get("telemetry") or {}
        overrides = doc.get("overrides") or {}
        overrides = {k: v for k, v in overrides.items() if v is not None}

        duration = doc.get("duration_s")
        duration_s = float(duration) if duration else None

        backfill = doc.get("backfill") or {}
        bf_start = backfill.get("start_s")
        bf_end = backfill.get("end_s")
        bf_res = backfill.get("resolution_s")

        return cls(
            run_id=doc.get("run_id"),
            slot=int(doc.get("slot", 0)),
            total_workers=int(doc.get("total_workers", 1)),
            lease_id=doc.get("lease_id"),
            engine=doc.get("engine", "eventgen"),
            bundle_url=bundle["url"],
            bundle_sha256=bundle.get("sha256"),
            rate_mode=rate_mode,
            rate_value=rate_value,
            duration_s=duration_s,
            hec_url=hec["url"],
            hec_index=hec.get("index"),
            hec_sourcetype=hec.get("sourcetype"),
            hec_source=hec.get("source"),
            hec_host=hec.get("host"),
            hec_gzip=bool(hec.get("gzip", True)),
            hec_ack=bool(hec.get("ack", False)),
            overrides=overrides,
            telemetry_interval_s=float(telemetry.get("interval_s", 5)),
            released=bool(doc.get("released", False)),
            effective_t0=effective_t0,
            backfill_start_s=float(bf_start) if bf_start is not None else None,
            backfill_end_s=float(bf_end) if bf_end is not None else None,
            backfill_resolution_s=float(bf_res) if bf_res is not None else None,
        )

    @classmethod
    def from_standalone(cls, cfg):
        # type: (Config) -> SpecSlice
        """Synthesise a slice identical to a claim response from standalone env."""
        if not cfg.standalone:
            raise SliceError("from_standalone requires a standalone Config")
        # Declared metadata env vars are run-declared overrides: they win over
        # whatever the plugin emits, matching managed-mode override semantics.
        overrides = {}
        if cfg.index:
            overrides["index"] = cfg.index
        if cfg.sourcetype:
            overrides["sourcetype"] = cfg.sourcetype
        if cfg.host_field:
            overrides["host"] = cfg.host_field
        if cfg.source:
            overrides["source"] = cfg.source

        def _envf(key):
            import os
            raw = os.environ.get(key)
            if raw is None or not raw.strip():
                return None
            try:
                return float(raw)
            except ValueError:
                raise SliceError("%s must be a number, got %r" % (key, raw))

        return cls(
            run_id="standalone",
            slot=cfg.slot,
            total_workers=cfg.total_workers,
            lease_id="standalone",
            engine=cfg.engine,
            bundle_url=cfg.bundle,
            bundle_sha256=cfg.bundle_sha256,
            rate_mode=cfg.rate_mode,
            rate_value=cfg.rate_value,
            duration_s=cfg.duration_s,
            hec_url=cfg.hec_url,
            hec_index=cfg.index,
            hec_sourcetype=cfg.sourcetype,
            hec_source=cfg.source,
            hec_host=cfg.host_field,
            hec_gzip=True,
            hec_ack=False,
            overrides=overrides,
            telemetry_interval_s=cfg.heartbeat_s,
            released=False,
            effective_t0=None,
            backfill_start_s=_envf("STOKER_BACKFILL_START_S"),
            backfill_end_s=_envf("STOKER_BACKFILL_END_S"),
            backfill_resolution_s=_envf("STOKER_BACKFILL_RESOLUTION_S"),
        )

    def hec_defaults(self):
        # type: () -> Dict[str, Optional[str]]
        """Fill-in defaults for null envelope metadata (overrides win separately)."""
        return {
            "index": self.hec_index,
            "sourcetype": self.hec_sourcetype,
            "source": self.hec_source,
            "host": self.hec_host,
        }
