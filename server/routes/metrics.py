"""Metric-pack authoring + preview API.

A **metric pack** is a UI-authored pack (`engine: metrics`) whose builder config
(the `metricgen` object: dimensions + metrics + patterns) is stored on the Pack
row (`builder_config_json`). Its bundle is synthesised from that config
(`bundles.build_from_metrics_config`), so it flows through specs/runs exactly like
any other pack. These routes let the builder create/update/read a metric pack and
**preview** a metric's daily curve without running anything — the preview uses the
same pattern maths (`server.metricpatterns`, vendored byte-for-byte from the
worker engine) as the worker, so what you see is what you get.

`/api/runs/{id}/metrics` (run telemetry) is a different, pre-existing endpoint;
this router is `/api/metric-packs` to avoid the name clash.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import bundles, metricpatterns
from ..db import get_db
from ..models import Pack
from ..schemas import (MetricPackCreate, MetricPackDetail, MetricPreviewPoint,
                       MetricPreviewRequest, MetricPreviewResponse, PackOut)

router = APIRouter(prefix="/api/metric-packs", tags=["metrics"])

# A metric pack has no source directory; its bundle is built from the stored
# config. This sentinel makes that obvious in the packs list / API.
_BUILDER_SOURCE = "builder://metrics"
_DEFAULT_SOURCETYPE = "stoker:metric"
_PREVIEW_POINTS_MIN = 12
_PREVIEW_POINTS_MAX = 288


def _seed(*parts):
    # type: (...) -> int
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


def _apply_metric_pack(pack, body):
    # type: (Pack, MetricPackCreate) -> None
    """Set the mutable fields of a metric pack from a builder config body."""
    config = body.config
    pack.name = body.name
    pack.description = body.description
    pack.engines_json = [bundles.METRICS_ENGINE]
    pack.sourcetypes_json = [config.get("sourcetype") or _DEFAULT_SOURCETYPE]
    pack.stanza_count = 0
    n_metrics = len(config.get("metrics") or [])
    pack.est_bytes_per_event = round(120.0 + 45.0 * n_metrics, 1)
    pack.verified = True
    pack.lint_status = "ok"
    pack.lint_errors_json = []
    pack.builder_config_json = config


@router.post("", response_model=PackOut, status_code=201)
def create_metric_pack(body: MetricPackCreate, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Validate a builder config and register it as a metric pack."""
    errors = bundles.lint_metrics_config(body.config)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    pack = Pack(source_path=_BUILDER_SOURCE, tags_json=[])
    _apply_metric_pack(pack, body)
    db.add(pack)
    db.commit()
    db.refresh(pack)
    return pack


@router.put("/{pack_id}", response_model=PackOut)
def update_metric_pack(pack_id: int, body: MetricPackCreate,
                       db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Update an existing metric pack's config (re-validated)."""
    pack = db.get(Pack, pack_id)
    if pack is None or pack.builder_config_json is None:
        raise HTTPException(status_code=404, detail="unknown metric pack")
    errors = bundles.lint_metrics_config(body.config)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    _apply_metric_pack(pack, body)
    db.commit()
    db.refresh(pack)
    return pack


@router.get("/{pack_id}", response_model=MetricPackDetail)
def get_metric_pack(pack_id: int, db: Session = Depends(get_db)):
    # type: (...) -> Any
    """Return a metric pack with its builder config, for the editor."""
    pack = db.get(Pack, pack_id)
    if pack is None or pack.builder_config_json is None:
        raise HTTPException(status_code=404, detail="unknown metric pack")
    return MetricPackDetail(
        id=pack.id, name=pack.name, description=pack.description,
        engines_json=pack.engines_json, sourcetypes_json=pack.sourcetypes_json,
        verified=pack.verified, lint_status=pack.lint_status,
        lint_errors_json=pack.lint_errors_json, created_at=pack.created_at,
        config=pack.builder_config_json,
        series_count=bundles.metrics_series_count(pack.builder_config_json))


@router.post("/preview", response_model=MetricPreviewResponse)
def preview_metric(body: MetricPreviewRequest):
    # type: (...) -> Any
    """Compute one metric's daily curve from a builder config (no run).

    Returns ``points`` samples across 24 h: the normalised ``activity``, the
    ``center`` value (``min + activity*(p95-min)``) and a noisy ``value``, plus
    the ``guides`` (min/p95/max, scaled for the chosen ``cell``). Deliberately
    lenient: it validates only the chosen metric so a half-finished config still
    previews. ``counter`` metrics preview their per-interval magnitude (kind
    ``count``) so the shape is visible rather than a monotonic ramp.
    """
    config = body.config or {}
    metrics = config.get("metrics") or []
    if not isinstance(metrics, list) or not metrics:
        raise HTTPException(status_code=400, detail="config has no metrics to preview")
    metric = None
    if body.metric:
        metric = next((m for m in metrics if isinstance(m, dict)
                       and m.get("name") == body.metric), None)
    if metric is None:
        metric = next((m for m in metrics if isinstance(m, dict)), None)
    if metric is None:
        raise HTTPException(status_code=400, detail="no valid metric to preview")

    name = metric.get("name") or "metric"
    # Cell scale multiplier (product of the chosen combo's per-dimension scales).
    mult = 1.0
    scale = metric.get("scale") or {}
    for dim_key, value in (body.cell or {}).items():
        table = scale.get(dim_key) if isinstance(scale, dict) else None
        if isinstance(table, dict) and value in table:
            try:
                mult *= float(table[value])
            except (TypeError, ValueError):
                pass
    try:
        vmin = float(metric.get("min", 0.0)) * mult
        p95 = float(metric.get("p95", metric.get("max", 1.0))) * mult
        vmax = float(metric.get("max", p95)) * mult
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="metric min/p95/max must be numbers")

    kind = metric.get("kind", "gauge")
    preview_kind = "count" if kind in ("count", "counter") else "gauge"
    try:
        noise = float(metric.get("noise", 0.1) or 0.0)
    except (TypeError, ValueError):
        noise = 0.1
    pattern = metric.get("pattern") or {}
    points = max(_PREVIEW_POINTS_MIN, min(_PREVIEW_POINTS_MAX, int(body.points or 96)))

    seed = config.get("seed", 1234)
    rng = random.Random(_seed(seed, name, "preview"))
    state = {"rng": rng}  # random_walk walks across the preview points

    out = []
    for i in range(points):
        hour = 24.0 * i / points
        a = metricpatterns.activity(pattern, hour, state=state)
        center = vmin + a * (p95 - vmin)
        value = metricpatterns.sample_value(a, vmin, p95, vmax, preview_kind,
                                            noise, rng)
        out.append(MetricPreviewPoint(hour=round(hour, 3), activity=round(a, 4),
                                      center=round(center, 4), value=value))

    return MetricPreviewResponse(
        metric=name, unit=metric.get("unit"), kind=kind,
        guides={"min": round(vmin, 4), "p95": round(p95, 4), "max": round(vmax, 4)},
        points=out, series_count=bundles.metrics_series_count(config))
