"""Local-pack lint + content-addressed bundle builder.

``build_from_pack`` lints a pack directory, writes a ``stoker.json`` manifest,
tars the pack **reproducibly** (sorted members, zeroed mtime/uid/gid, fixed
mode), sha256s the bytes and stores the tarball at ``{BUNDLE_DIR}/<digest>.tgz``.
The bundle is content-addressed: an identical pack produces an identical digest,
so a rebuild reuses the existing row (dedup).

The worker fetches ``/api/agent/bundles/<digest>.tgz`` with the run JWT and
verifies the sha256 (its ``bundle.py`` already does). The tar must unpack to a
directory containing ``default/eventgen.conf`` (the worker's ``_find_pack_root``
accepts the pack at the archive root or one level down; we place it at the
root, prefixed by the pack directory name).

``lint_pack`` is the standalone linter used by the operator API's
register/preview endpoints.
"""

from __future__ import annotations

import configparser
import dataclasses
import hashlib
import io
import ipaddress
import json
import logging
import os
import shutil
import socket
import tarfile
import tempfile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

log = logging.getLogger("stoker.bundles")

CONF_RELPATH = os.path.join("default", "eventgen.conf")
# Reproducible tar: a fixed epoch for every member (Stoker epoch, arbitrary but
# stable) so rebuilding an unchanged pack yields byte-identical archives.
_FIXED_MTIME = 1_700_000_000
_SAMPLE_MODES = ("sample", "replay")
_GLOBAL_SECTIONS = frozenset(("global", "default"))
# rawreplay dataset_url fetch: follow at most this many redirects, re-validating
# the host at every hop (a public URL must not 30x into an internal one).
_MAX_FETCH_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))

# rawreplay (Piston) pack support. A rawreplay pack declares `engine: rawreplay`
# and a `replay:` section instead of a default/eventgen.conf. The dataset is
# either a path under the pack (`dataset:`) or an https URL fetched at build
# time (`dataset_url:`). CADENCE mode reproduces the recorded inter-event gaps;
# RATE mode emits hot and lets the agent token bucket pace + loop the dataset.
RAWREPLAY_ENGINE = "rawreplay"
METRICS_ENGINE = "metrics"
# A metrics pack's dimension cross-product must stay bounded (each combo is a
# runtime series emitted every resolution tick). Rejected past this at lint.
_MAX_METRIC_SERIES = 5000
# The default metric sourcetype (a metrics-type index sourcetype).
_DEFAULT_METRIC_SOURCETYPE = "stoker:metric"
# Stub eventgen.conf shipped inside a metrics bundle. The metrics engine reads
# its config from stoker.json's `metricgen` block and the agent skips the
# conf-rewrite; this file exists only to satisfy the worker's pack-root file
# contract (fetch_bundle requires default/eventgen.conf) and is never executed.
_METRICS_STUB_CONF = (
    "# Auto-generated stub for a metrics pack (engine=metrics). The metrics\n"
    "# engine reads its config from stoker.json's `metricgen` block; this file\n"
    "# exists only to satisfy the pack-root file contract and is never run.\n"
    "[stub]\n"
    "mode = sample\n"
)
_REPLAY_MODES = ("rate", "cadence")
_DEFAULT_TIME_MULTIPLE = 1.0
# Where a fetched `dataset_url` payload lands inside the built bundle (so the
# worker always finds the dataset at a stable relative path).
_FETCHED_DATASET_RELPATH = os.path.join("dataset", "replay.dat")
_DOWNLOAD_CHUNK = 1 << 16
# Fallback caps when a Settings object does not carry them (defensive; settings
# is always resolved in build_from_pack). Mirrors server.config defaults.
_FALLBACK_MAX_DATASET_BYTES = 512 * 1024 * 1024  # 512 MiB
_FALLBACK_FETCH_TIMEOUT_S = 120.0


class BundleError(Exception):
    """Pack lint failed or the bundle could not be built/stored."""


@dataclasses.dataclass
class LintResult:
    """Outcome of linting a pack directory."""

    ok: bool
    errors: List[str]
    stanzas: List[str]
    engines: List[str]
    sourcetypes: List[str]
    stanza_count: int
    est_bytes_per_event: Optional[float]
    declared_per_day_gb: Optional[float]
    declared_bytes_per_event: Optional[float]
    # rawreplay only: the validated replay config (dataset/dataset_url, mode,
    # time_multiple, sourcetype, source). ``None`` for an eventgen pack.
    replay: Optional[Dict[str, Any]] = None

    @property
    def engine(self):
        # type: () -> str
        """The pack's primary engine (first of ``engines``, default eventgen)."""
        return self.engines[0] if self.engines else "eventgen"


@dataclasses.dataclass
class BuiltBundle:
    """A built (or reused) bundle: enough for the caller to upsert the row."""

    digest: str
    path: str
    size_bytes: int
    reused: bool


def _make_parser():
    # type: () -> configparser.RawConfigParser
    parser = configparser.RawConfigParser(
        delimiters=("=",), strict=False, allow_no_value=True, interpolation=None)
    parser.optionxform = str
    return parser


def _read_pack_yaml(pack_dir):
    # type: (str) -> Dict[str, Any]
    """Read pack.yaml with a self-contained flat two-level subset parser.

    The control-plane image does NOT ship the worker package, so this must not
    depend on stoker_agent; the parser here mirrors the worker's pack.yaml subset
    (top-level `key: value` and one indented level under a bare `key:`), so the
    control plane and worker agree on the format.
    """
    path = os.path.join(pack_dir, "pack.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return _parse_pack_yaml(fh.read())
    except OSError as exc:
        raise BundleError("cannot read pack.yaml in %r: %s" % (pack_dir, exc))


def _pack_yaml_strip_comment(line):
    # type: (str) -> str
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _pack_yaml_scalar(text):
    # type: (str) -> Any
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def _parse_pack_yaml(text):
    # type: (str) -> Dict[str, Any]
    """Flat two-level scalar subset (mirrors stoker_agent.bundle.parse_pack_yaml)."""
    result = {}     # type: Dict[str, Any]
    section = None  # type: Optional[str]
    for raw in text.splitlines():
        line = _pack_yaml_strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if stripped.startswith("- ") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        if indent == 0:
            if value == "":
                section = key
                result[key] = {}
            else:
                section = None
                result[key] = _pack_yaml_scalar(value)
        elif section is not None and value != "":
            if isinstance(result.get(section), dict):
                result[section][key] = _pack_yaml_scalar(value)
    return result


def is_rawreplay_pack(pack_dir):
    # type: (str) -> bool
    """True when ``pack_dir`` is a rawreplay (Piston) pack.

    Detected from ``pack.yaml``: ``engine: rawreplay`` OR a ``replay:`` section
    carrying a ``dataset`` / ``dataset_url``. A rawreplay pack has no
    ``default/eventgen.conf``; this is how the linter and bundler pick the
    rawreplay code path over the eventgen one without a conf to inspect.
    """
    pack_yaml = _read_pack_yaml(pack_dir)
    if not isinstance(pack_yaml, dict):
        return False
    if str(pack_yaml.get("engine") or "").strip().lower() == RAWREPLAY_ENGINE:
        return True
    replay = pack_yaml.get("replay")
    if isinstance(replay, dict) and (replay.get("dataset") or replay.get("dataset_url")):
        return True
    return False


def lint_pack(pack_dir):
    # type: (str) -> LintResult
    """Lint a local pack directory per the contract's pack rules.

    Dispatches on the pack's engine: a rawreplay (Piston) pack is linted by
    :func:`lint_rawreplay_pack` (no eventgen.conf; a ``replay:`` section instead);
    every other pack is linted as an eventgen pack below.

    eventgen checks: the conf parses (configparser), every ``mode = sample``
    stanza's sample file exists under ``samples/``, no output-side keys leak
    (they are stripped by the worker but flagged here as a warning-level error is
    not raised for them; ``outputMode`` present is tolerated and ignored), and at
    least one stanza exists. Also derives stanza names, engines, sourcetypes and
    a bytes/event estimate (measured from the first sample when the pack.yaml
    omits it).
    """
    if os.path.isdir(pack_dir) and is_rawreplay_pack(pack_dir):
        return lint_rawreplay_pack(pack_dir)

    errors = []  # type: List[str]
    stanzas = []  # type: List[str]
    sourcetypes = []  # type: List[str]

    if not os.path.isdir(pack_dir):
        return LintResult(False, ["pack directory not found: %s" % pack_dir],
                          [], [], [], 0, None, None, None)

    conf_path = os.path.join(pack_dir, CONF_RELPATH)
    if not os.path.isfile(conf_path):
        errors.append("missing required %s" % CONF_RELPATH)
        return LintResult(False, errors, [], [], [], 0, None, None, None)

    parser = _make_parser()
    try:
        read = parser.read(conf_path, encoding="utf-8")
        if not read:
            errors.append("could not read %s" % CONF_RELPATH)
    except configparser.Error as exc:
        errors.append("eventgen.conf parse error: %s" % exc)
        return LintResult(False, errors, [], [], [], 0, None, None, None)

    samples_dir = os.path.join(pack_dir, "samples")
    all_sections = [s for s in parser.sections() if s.lower() not in _GLOBAL_SECTIONS]

    for section in all_sections:
        stanzas.append(section)
        st = parser.get(section, "sourcetype", fallback=None)
        if st:
            sourcetypes.append(st)
        mode = (parser.get(section, "mode", fallback="sample") or "sample").strip()
        if mode not in _SAMPLE_MODES:
            errors.append("stanza [%s]: unsupported mode %r" % (section, mode))
        # sample-mode stanzas need a resolvable sample file. eventgen resolves
        # the stanza name (or an explicit sampleFile / source) against sampleDir.
        if mode in _SAMPLE_MODES:
            sample_name = parser.get(section, "sampleFile", fallback=None) or section
            candidates = [
                os.path.join(samples_dir, sample_name),
                os.path.join(pack_dir, sample_name),
            ]
            if not any(os.path.isfile(c) for c in candidates):
                errors.append(
                    "stanza [%s]: sample file %r not found under samples/"
                    % (section, sample_name))
        # token replacement compile check (regex tokens must compile).
        for key in parser.options(section):
            if key.endswith(".token"):
                import re

                pattern = parser.get(section, key)
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append("stanza [%s] %s: bad regex %r (%s)"
                                  % (section, key, pattern, exc))

    if not all_sections:
        errors.append("no sample stanzas found in %s" % CONF_RELPATH)

    pack_yaml = _read_pack_yaml(pack_dir)
    # The sourcetype usually lives in pack.yaml defaults (output-side keys are
    # stripped from the conf), so read it there too, not only from the stanza.
    defaults = pack_yaml.get("defaults") if isinstance(pack_yaml, dict) else {}
    if isinstance(defaults, dict) and defaults.get("sourcetype"):
        sourcetypes.append(str(defaults["sourcetype"]))
    engines = _collect_engines(pack_yaml, parser)
    estimates = pack_yaml.get("estimates") if isinstance(pack_yaml, dict) else {}
    estimates = estimates if isinstance(estimates, dict) else {}
    declared_bpe = _as_float(estimates.get("bytes_per_event"))
    declared_gb = _as_float(estimates.get("per_day_gb"))
    if declared_gb is None:
        declared_gb = _as_float(pack_yaml.get("declared_per_day_gb"))

    est_bpe = declared_bpe
    if est_bpe is None:
        est_bpe = _measure_bytes_per_event(samples_dir, pack_dir, all_sections, parser)

    ok = len(errors) == 0
    # de-dup sourcetypes/engines while preserving order
    return LintResult(
        ok=ok,
        errors=errors,
        stanzas=stanzas,
        engines=_unique(engines),
        sourcetypes=_unique(sourcetypes),
        stanza_count=len(all_sections),
        est_bytes_per_event=est_bpe,
        declared_per_day_gb=declared_gb,
        declared_bytes_per_event=declared_bpe,
    )


# --------------------------------------------------------------------------- #
# rawreplay (Piston) lint + replay-config parsing
# --------------------------------------------------------------------------- #

def parse_replay_config(pack_dir, pack_yaml=None):
    # type: (str, Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]
    """Parse + validate a rawreplay pack's ``replay:`` section.

    Returns ``(config, errors)``. ``config`` carries the normalised replay knobs
    the worker reads from the bundle's ``stoker.json``:

    * ``dataset`` — a pack-relative dataset path (mutually exclusive with
      ``dataset_url``); validated to stay inside the pack root.
    * ``dataset_url`` — an https URL fetched at build time.
    * ``dataset_sha256`` — optional; verifies a fetched ``dataset_url``.
    * ``mode`` — ``rate`` (agent-paced, dataset loops) or ``cadence``
      (engine-paced from the recorded gaps).
    * ``time_multiple`` — cadence stretch/compress factor (> 0; default 1.0).
    * ``sourcetype`` / ``source`` — from the pack ``defaults`` (stamped by the
      agent, since a rawreplay pack has no eventgen output-side keys).

    ``errors`` is empty when the config is valid; each entry is a human-readable
    lint failure. This is the single validator the linter and the build use.
    """
    if pack_yaml is None:
        pack_yaml = _read_pack_yaml(pack_dir)
    errors = []  # type: List[str]
    replay_raw = pack_yaml.get("replay") if isinstance(pack_yaml, dict) else None
    if not isinstance(replay_raw, dict):
        errors.append(
            "rawreplay pack.yaml must declare a 'replay:' section with a "
            "'dataset' or 'dataset_url'")
        replay_raw = {}

    dataset = replay_raw.get("dataset")
    dataset_url = replay_raw.get("dataset_url")
    dataset = str(dataset).strip() if dataset else None
    dataset_url = str(dataset_url).strip() if dataset_url else None

    # A local ``dataset`` wins: when it is present the ``dataset_url`` is treated
    # as provenance only (where the capture came from) and is NOT fetched. Only
    # when there is no local dataset is ``dataset_url`` the actionable https fetch
    # source. ``provenance_only`` records that distinction for the build.
    provenance_only = bool(dataset and dataset_url)
    if not dataset and not dataset_url:
        errors.append("replay: a 'dataset' or 'dataset_url' is required")

    # A pack-relative dataset must exist and stay inside the pack root.
    if dataset:
        resolved = _safe_join_pack(pack_dir, dataset)
        if resolved is None:
            errors.append(
                "replay: dataset %r escapes the pack root (rejected)" % dataset)
        elif not os.path.isfile(resolved):
            errors.append("replay: dataset file %r not found under the pack" % dataset)

    # A dataset_url that will actually be fetched (no local dataset) must be
    # https to a public host. A provenance-only URL alongside a local dataset is
    # documentation, so it is not checked (it is never fetched). The host is
    # fully resolved + re-validated at fetch time (_assert_fetchable_url); here we
    # cheaply reject an obvious internal/loopback/link-local IP literal at
    # lint/index time (a hostname is left to the fetch-time resolve so a flaky DNS
    # does not fail lint).
    if dataset_url and not provenance_only:
        if not dataset_url.lower().startswith("https://"):
            errors.append(
                "replay: dataset_url must be an https:// URL, got %r" % dataset_url)
        else:
            host = urlsplit(dataset_url).hostname or ""
            try:
                addr = ipaddress.ip_address(host)
            except ValueError:
                addr = None  # a hostname; resolved + checked at fetch time
            if addr is not None and (not addr.is_global or addr.is_multicast):
                errors.append(
                    "replay: dataset_url host %s is not a public address (refused)" % host)

    mode = str(replay_raw.get("mode") or "rate").strip().lower()
    if mode not in _REPLAY_MODES:
        errors.append(
            "replay: mode must be one of %s, got %r" % (", ".join(_REPLAY_MODES), mode))

    time_multiple = _as_float(replay_raw.get("time_multiple"))
    if time_multiple is None:
        time_multiple = _DEFAULT_TIME_MULTIPLE
    elif time_multiple <= 0:
        errors.append("replay: time_multiple must be > 0, got %s" % time_multiple)

    dataset_sha256 = replay_raw.get("dataset_sha256")
    dataset_sha256 = str(dataset_sha256).strip().lower() if dataset_sha256 else None

    # Optional cadence-mode timestamp hints the worker's engine consumes verbatim
    # from the replay section (``ts_regex`` / ``ts_strptime`` / ``ts_field``);
    # passed straight through so a cadence pack's re-timestamping reaches Piston.
    def _opt(key):
        # type: (str) -> Optional[str]
        val = replay_raw.get(key)
        return str(val) if val not in (None, "") else None

    defaults = pack_yaml.get("defaults") if isinstance(pack_yaml, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}

    config = {
        "dataset": dataset,
        "dataset_url": dataset_url,
        # The URL to actually fetch at build time: only when there is no local
        # dataset. A provenance-only URL (alongside a local dataset) is None here.
        "fetch_url": None if dataset else dataset_url,
        "dataset_sha256": dataset_sha256,
        "mode": mode if mode in _REPLAY_MODES else "rate",
        "time_multiple": float(time_multiple),
        "ts_regex": _opt("ts_regex"),
        "ts_strptime": _opt("ts_strptime"),
        "ts_field": _opt("ts_field"),
        "sourcetype": str(defaults["sourcetype"]) if defaults.get("sourcetype") else None,
        "source": str(defaults["source"]) if defaults.get("source") else None,
    }
    return config, errors


def lint_rawreplay_pack(pack_dir):
    # type: (str) -> LintResult
    """Lint a rawreplay (Piston) pack directory.

    A rawreplay pack has no ``default/eventgen.conf``: it declares
    ``engine: rawreplay`` and a ``replay:`` section (validated by
    :func:`parse_replay_config`). The bytes/event estimate is measured from the
    local dataset (a ``dataset_url`` is fetched at build time, so its estimate is
    the pack.yaml value when supplied, else unknown here).
    """
    pack_yaml = _read_pack_yaml(pack_dir)
    replay, errors = parse_replay_config(pack_dir, pack_yaml)

    sourcetypes = []  # type: List[str]
    if replay.get("sourcetype"):
        sourcetypes.append(replay["sourcetype"])

    estimates = pack_yaml.get("estimates") if isinstance(pack_yaml, dict) else {}
    estimates = estimates if isinstance(estimates, dict) else {}
    declared_bpe = _as_float(estimates.get("bytes_per_event"))
    declared_gb = _as_float(estimates.get("per_day_gb"))
    if declared_gb is None:
        declared_gb = _as_float(pack_yaml.get("declared_per_day_gb"))

    est_bpe = declared_bpe
    if est_bpe is None and replay.get("dataset"):
        resolved = _safe_join_pack(pack_dir, replay["dataset"])
        if resolved is not None and os.path.isfile(resolved):
            est_bpe = _mean_line_bytes(resolved)

    ok = len(errors) == 0
    return LintResult(
        ok=ok,
        errors=errors,
        stanzas=[],
        engines=[RAWREPLAY_ENGINE],
        sourcetypes=_unique(sourcetypes),
        stanza_count=0,
        est_bytes_per_event=est_bpe,
        declared_per_day_gb=declared_gb,
        declared_bytes_per_event=declared_bpe,
        replay=replay,
    )


# --------------------------------------------------------------------------- #
# metrics pack: lint + build from a UI-authored config
# --------------------------------------------------------------------------- #

def metrics_series_count(config):
    # type: (Dict[str, Any]) -> int
    """Size of the dimension cross-product (the number of runtime series)."""
    dims = config.get("dimensions") or [] if isinstance(config, dict) else []
    count = 1
    for d in dims:
        if isinstance(d, dict) and isinstance(d.get("values"), list) and d["values"]:
            count *= len(d["values"])
    return count


def lint_metrics_config(config):
    # type: (Dict[str, Any]) -> List[str]
    """Validate a metrics pack's ``metricgen`` config; return a list of errors.

    Shape: ``{resolution_s, tz_offset_hours?, seed?, sourcetype?, dimensions?,
    metrics: [ {name, kind, min, p95, max, noise?, pattern:{type,...}, scale?} ]}``.
    Checks the resolution, the dimension cross-product size, and each metric's
    name uniqueness, kind, min<=p95<=max ordering, noise, pattern type and scale
    references. The value/pattern maths itself lives in ``metricpatterns``.
    """
    from . import metricpatterns

    errors = []  # type: List[str]
    if not isinstance(config, dict):
        return ["metricgen config must be an object"]

    res = config.get("resolution_s", 10)
    try:
        if float(res) <= 0:
            errors.append("resolution_s must be > 0")
    except (TypeError, ValueError):
        errors.append("resolution_s must be a number")

    dim_keys = set()  # type: set
    dims = config.get("dimensions", [])
    if dims and not isinstance(dims, list):
        errors.append("dimensions must be a list")
    elif isinstance(dims, list):
        for i, d in enumerate(dims):
            if not isinstance(d, dict):
                errors.append("dimension %d must be an object" % i)
                continue
            key = d.get("key")
            vals = d.get("values")
            if not key or not isinstance(key, str):
                errors.append("dimension %d needs a non-empty string key" % i)
            else:
                dim_keys.add(key)
            if not isinstance(vals, list) or not vals:
                errors.append("dimension %r needs a non-empty values list" % key)
        n_series = metrics_series_count(config)
        if n_series > _MAX_METRIC_SERIES:
            errors.append("dimension cross-product is %d series (max %d); reduce "
                          "dimensions or values" % (n_series, _MAX_METRIC_SERIES))

    metrics = config.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        errors.append("metrics must be a non-empty list")
        return errors

    seen = set()  # type: set
    for i, m in enumerate(metrics):
        if not isinstance(m, dict):
            errors.append("metric %d must be an object" % i)
            continue
        name = m.get("name")
        label = name if isinstance(name, str) and name else ("#%d" % i)
        if not name or not isinstance(name, str):
            errors.append("metric %s needs a non-empty string name" % label)
        elif name in seen:
            errors.append("duplicate metric name %r" % name)
        else:
            seen.add(name)
        kind = m.get("kind", "gauge")
        if kind not in metricpatterns.VALUE_KINDS:
            errors.append("metric %r: kind must be one of %s"
                          % (label, ", ".join(metricpatterns.VALUE_KINDS)))
        try:
            vmin = float(m.get("min", 0))
            p95 = float(m.get("p95", m.get("max", 1)))
            vmax = float(m.get("max", p95))
            if not (vmin <= p95 <= vmax):
                errors.append("metric %r: require min <= p95 <= max (got %s / %s / %s)"
                              % (label, vmin, p95, vmax))
        except (TypeError, ValueError):
            errors.append("metric %r: min/p95/max must be numbers" % label)
        try:
            if float(m.get("noise", 0.1)) < 0:
                errors.append("metric %r: noise must be >= 0" % label)
        except (TypeError, ValueError):
            errors.append("metric %r: noise must be a number" % label)
        pattern = m.get("pattern") or {}
        ptype = (pattern.get("type") if isinstance(pattern, dict) else None) or "constant"
        if ptype not in metricpatterns.PATTERN_TYPES:
            errors.append("metric %r: unknown pattern type %r (known: %s)"
                          % (label, ptype, ", ".join(metricpatterns.PATTERN_TYPES)))
        scale = m.get("scale") or {}
        if scale and isinstance(scale, dict):
            for dk, table in scale.items():
                if dk not in dim_keys:
                    errors.append("metric %r: scale references unknown dimension %r"
                                  % (label, dk))
                if not isinstance(table, dict):
                    errors.append("metric %r: scale[%r] must be an object mapping "
                                  "value -> multiplier" % (label, dk))
    return errors


def build_from_metrics_config(name, config, bundle_dir=None, settings=None):
    # type: (str, Dict[str, Any], Optional[str], Optional[Any]) -> BuiltBundle
    """Build a content-addressed bundle for a UI-authored metrics pack.

    The pack has no source directory: its ``metricgen`` config (validated by
    :func:`lint_metrics_config`) is written into a synthesised ``stoker.json`` and
    a stub ``default/eventgen.conf`` so the built bundle satisfies the worker's
    pack-root file contract. Identical config -> identical digest (dedup), like
    every other bundle.
    """
    errors = lint_metrics_config(config)
    if errors:
        raise BundleError("metrics pack %r failed lint: %s" % (name, "; ".join(errors)))

    if settings is None:
        from .config import get_settings

        settings = get_settings()
    if bundle_dir is None:
        bundle_dir = settings.bundle_dir

    n_metrics = len(config.get("metrics") or [])
    # Rough multi-metric envelope size: base + per-measurement key/value.
    est_bpe = round(120.0 + 45.0 * n_metrics, 1)
    sourcetype = config.get("sourcetype") or _DEFAULT_METRIC_SOURCETYPE
    manifest = {
        "name": name,
        "engine": METRICS_ENGINE,
        "estimates": {"bytes_per_event": est_bpe},
        "stanzas": [],
        "sourcetypes": [sourcetype],
        "metricgen": config,
    }

    tmp = tempfile.mkdtemp(prefix="stoker-metricpack-")
    try:
        # A FIXED pack-dir basename so the archive arcnames (prefixed with the
        # basename) are stable across builds -> reproducible digest / dedup. The
        # real pack name lives in the manifest, not the arcname prefix.
        pack_dir = os.path.join(tmp, "metricpack")
        os.makedirs(os.path.join(pack_dir, "default"))
        with open(os.path.join(pack_dir, CONF_RELPATH), "w", encoding="utf-8") as fh:
            fh.write(_METRICS_STUB_CONF)
        data = build_tarball_bytes(pack_dir, manifest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    digest = hashlib.sha256(data).hexdigest()
    os.makedirs(bundle_dir, exist_ok=True)
    path = os.path.join(bundle_dir, "%s.tgz" % digest)
    if os.path.isfile(path) and os.path.getsize(path) == len(data):
        log.info("metrics bundle %s already present (dedup)", digest[:12])
        return BuiltBundle(digest=digest, path=path, size_bytes=len(data), reused=True)
    tmp_path = path + ".tmp.%d" % os.getpid()
    with open(tmp_path, "wb") as fh:
        fh.write(data)
    os.replace(tmp_path, path)
    log.info("built metrics bundle %s (%d bytes) for pack %r", digest[:12], len(data), name)
    return BuiltBundle(digest=digest, path=path, size_bytes=len(data), reused=False)


def _safe_join_pack(pack_dir, name):
    # type: (str, str) -> Optional[str]
    """Join ``name`` onto ``pack_dir`` iff it stays inside the pack root.

    Reuses the same containment as the preview's ``_safe_join`` (refuses absolute
    paths and ``..`` traversal), so a rawreplay dataset path is subject to the
    identical path-escape guard the eventgen file-token paths already are.
    Returns ``None`` when the path would escape.
    """
    from .preview import _safe_join

    return _safe_join(pack_dir, pack_dir, name)


def _collect_engines(pack_yaml, parser):
    # type: (Dict[str, Any], configparser.RawConfigParser) -> List[str]
    engines = []  # type: List[str]
    if isinstance(pack_yaml, dict):
        eng = pack_yaml.get("engine")
        if isinstance(eng, str):
            engines.append(eng)
    if not engines:
        engines.append("eventgen")
    return engines


def _measure_bytes_per_event(samples_dir, pack_dir, sections, parser):
    # type: (str, str, List[str], configparser.RawConfigParser) -> Optional[float]
    """Estimate bytes/event from the mean line length of the first sample file."""
    for section in sections:
        sample_name = parser.get(section, "sampleFile", fallback=None) or section
        for base in (samples_dir, pack_dir):
            path = os.path.join(base, sample_name)
            if os.path.isfile(path):
                mean = _mean_line_bytes(path)
                if mean is not None:
                    return mean
    return None


def _mean_line_bytes(path, max_lines=200):
    # type: (str, int) -> Optional[float]
    total = 0
    count = 0
    try:
        with open(path, "rb") as fh:
            for line in fh:
                stripped = line.rstrip(b"\r\n")
                if not stripped:
                    continue
                total += len(stripped)
                count += 1
                if count >= max_lines:
                    break
    except OSError:
        return None
    if count == 0:
        return None
    return round(total / count, 2)


def build_stoker_manifest(pack_dir, lint, dataset_relpath=None):
    # type: (str, LintResult, Optional[str]) -> Dict[str, Any]
    """Build the stoker.json manifest embedded in the bundle.

    For a rawreplay pack a ``replay`` block is added so the worker reads the
    replay config from the bundle: the dataset's **pack-relative path inside the
    bundle** (``dataset_relpath``, which the builder passes as the location the
    dataset was written to — a fetched ``dataset_url`` lands at a fixed path),
    plus the mode, time_multiple, sourcetype and source. ``dataset_url`` /
    ``dataset_sha256`` are not carried into the manifest: the dataset is embedded
    in the bundle by build time, so the worker never needs to re-fetch it.
    """
    name = os.path.basename(os.path.normpath(pack_dir))
    pack_yaml = _read_pack_yaml(pack_dir)
    if isinstance(pack_yaml, dict) and isinstance(pack_yaml.get("name"), str):
        name = pack_yaml["name"]
    estimates = {}  # type: Dict[str, Any]
    if lint.est_bytes_per_event is not None:
        estimates["bytes_per_event"] = lint.est_bytes_per_event
    if lint.declared_per_day_gb is not None:
        estimates["per_day_gb"] = lint.declared_per_day_gb
    manifest = {
        "name": name,
        "engine": lint.engine,
        "estimates": estimates,
        "stanzas": lint.stanzas,
        "sourcetypes": lint.sourcetypes,
    }  # type: Dict[str, Any]
    if lint.engine == RAWREPLAY_ENGINE and lint.replay is not None:
        replay = lint.replay
        # The dataset always ships inside the bundle; the manifest points the
        # worker at its bundle-relative path (a local `dataset:` keeps its path,
        # a fetched `dataset_url` lands at the fixed _FETCHED_DATASET_RELPATH).
        rel = dataset_relpath or replay.get("dataset")
        replay_block = {
            "dataset": rel,
            "mode": replay.get("mode") or "rate",
            "time_multiple": replay.get("time_multiple") or _DEFAULT_TIME_MULTIPLE,
            "sourcetype": replay.get("sourcetype"),
            "source": replay.get("source"),
        }  # type: Dict[str, Any]
        # Pass through the optional cadence timestamp hints the worker's engine
        # reads verbatim (only when declared, to keep the manifest minimal).
        for key in ("ts_regex", "ts_strptime", "ts_field"):
            if replay.get(key):
                replay_block[key] = replay[key]
        manifest["replay"] = replay_block
    return manifest


def _iter_pack_files(pack_dir, extra_relpaths=None):
    # type: (str, Optional[List[str]]) -> List[Tuple[str, str]]
    """Yield (absolute_path, arcname) for the pack payload, sorted.

    Includes default/eventgen.conf (when present), everything under samples/, and
    pack.yaml when present. The arcname is prefixed with the pack directory's
    basename so the archive unpacks to ``<pack>/default/eventgen.conf`` (root-
    plus-one, which the worker accepts). stoker.json is added separately by the
    builder.

    ``extra_relpaths`` names additional pack-relative files to include (e.g. a
    rawreplay ``dataset:`` that lives outside ``samples/``). Each is added once,
    with the same basename prefix, and de-duplicated against the samples walk.
    """
    base = os.path.basename(os.path.normpath(pack_dir))
    members = []  # type: List[Tuple[str, str]]
    seen = set()  # type: set

    def _add(full, rel):
        # type: (str, str) -> None
        arc = os.path.join(base, rel)
        if arc in seen:
            return
        seen.add(arc)
        members.append((full, arc))

    for rel in (CONF_RELPATH, "pack.yaml"):
        full = os.path.join(pack_dir, rel)
        if os.path.isfile(full):
            _add(full, rel)
    samples_dir = os.path.join(pack_dir, "samples")
    if os.path.isdir(samples_dir):
        for root, _dirs, files in os.walk(samples_dir):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, pack_dir)
                _add(full, rel)
    for rel in (extra_relpaths or []):
        full = os.path.join(pack_dir, rel)
        if os.path.isfile(full):
            _add(full, rel.replace(os.sep, "/") if os.sep != "/" else rel)
    members.sort(key=lambda pair: pair[1])
    return members


def _reproducible_tarinfo(arcname, size):
    # type: (str, int) -> tarfile.TarInfo
    info = tarfile.TarInfo(name=arcname)
    info.size = size
    info.mtime = _FIXED_MTIME
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def build_tarball_bytes(pack_dir, manifest, extra_relpaths=None, extra_files=None):
    # type: (str, Dict[str, Any], Optional[List[str]], Optional[List[Tuple[str, bytes]]]) -> bytes
    """Build the reproducible gzip tarball bytes for a pack + manifest.

    Deterministic: sorted members, fixed mtime/uid/gid/mode, gzip written with a
    zeroed mtime so identical inputs hash identically.

    ``extra_relpaths`` names additional pack-relative on-disk files to include
    (e.g. a rawreplay ``dataset:`` outside ``samples/``). ``extra_files`` is a
    list of ``(arcname_relpath, bytes)`` in-memory members (e.g. a fetched
    ``dataset_url`` payload) that are not on disk under the pack; their arcnames
    are prefixed with the pack basename just like the on-disk members, and the
    combined member list is sorted so the archive stays reproducible.
    """
    base = os.path.basename(os.path.normpath(pack_dir))
    raw = io.BytesIO()
    # mtime=0 in the gzip header keeps the compressed bytes stable.
    import gzip

    # Collate every member as (arcname, bytes) then emit sorted by arcname so the
    # archive is byte-identical regardless of on-disk vs in-memory origin.
    entries = []  # type: List[Tuple[str, bytes]]
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    entries.append((os.path.join(base, "stoker.json"), manifest_bytes))
    for full, arcname in _iter_pack_files(pack_dir, extra_relpaths=extra_relpaths):
        with open(full, "rb") as fh:
            entries.append((arcname, fh.read()))
    seen = {arc for arc, _ in entries}
    for rel, data in (extra_files or []):
        arc = os.path.join(base, rel)
        if arc in seen:
            continue
        seen.add(arc)
        entries.append((arc, data))
    entries.sort(key=lambda pair: pair[0])

    gz = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
    with tarfile.open(fileobj=gz, mode="w") as tar:
        for arcname, data in entries:
            info = _reproducible_tarinfo(arcname, len(data))
            tar.addfile(info, io.BytesIO(data))
    gz.close()
    return raw.getvalue()


def _rawreplay_dataset_members(pack_dir, replay, settings):
    # type: (str, Dict[str, Any], Any) -> Tuple[Optional[List[str]], Optional[List[Tuple[str, bytes]]], Optional[str]]
    """Resolve the dataset that must ship inside a rawreplay bundle.

    Returns ``(extra_relpaths, extra_files, dataset_relpath)``:

    * a local ``dataset:`` -> it is already under the pack; include it as an
      ``extra_relpath`` (in case it lives outside ``samples/``) and keep its path
      as the bundle-relative dataset path.
    * a ``dataset_url:`` -> fetch it (https only, size-capped, sha-verified when
      the pack declares ``dataset_sha256``) and embed the bytes at the fixed
      ``_FETCHED_DATASET_RELPATH`` as an in-memory member.

    Raises :class:`BundleError` on a fetch/verify failure or an escaping path.

    A local ``dataset`` always ships the on-disk file (any ``dataset_url`` beside
    it is provenance only). ``fetch_url`` (set only when there is no local
    dataset) is the URL that is actually fetched.
    """
    dataset = replay.get("dataset")
    fetch_url = replay.get("fetch_url")
    if dataset:
        resolved = _safe_join_pack(pack_dir, dataset)
        if resolved is None or not os.path.isfile(resolved):
            raise BundleError(
                "rawreplay dataset %r not found under the pack (or escapes it)" % dataset)
        rel = os.path.relpath(resolved, os.path.abspath(pack_dir))
        rel = rel.replace(os.sep, "/")
        return [rel], None, rel
    if fetch_url:
        data = _fetch_dataset_url(
            fetch_url,
            max_bytes=int(getattr(settings, "rawreplay_max_dataset_bytes", _FALLBACK_MAX_DATASET_BYTES)),
            timeout_s=float(getattr(settings, "rawreplay_fetch_timeout_s", _FALLBACK_FETCH_TIMEOUT_S)),
            expected_sha256=replay.get("dataset_sha256"))
        rel = _FETCHED_DATASET_RELPATH.replace(os.sep, "/")
        return None, [(rel, data)], rel
    raise BundleError("rawreplay pack declares neither a dataset nor a dataset_url")


def _assert_fetchable_url(url):
    # type: (str) -> None
    """SSRF guard for a rawreplay ``dataset_url`` fetched by the CONTROL PLANE.

    A ``dataset_url`` originates from a pack.yaml in a (possibly untrusted) synced
    git repo, and the fetched bytes are embedded in the bundle then replayed to a
    HEC target, so an unguarded fetch is both an SSRF and a read/exfiltration
    primitive. This refuses anything that is not a plain https URL to a PUBLIC
    host: no embedded credentials, and every address the host resolves to must be
    a global unicast address (blocks loopback ``127/8``/``::1``, link-local
    ``169.254/16`` incl. the cloud-metadata IP, private ``10/8``/``172.16/12``/
    ``192.168/16``/``fc00::/7``, and reserved/multicast ranges).

    Called for the initial URL and re-called for every redirect hop (see
    :func:`_fetch_dataset_url`), so a public URL cannot 30x into an internal one.

    Residual: a sub-second DNS rebind between this resolve and the socket connect
    is not defeated here (that needs connection pinning); the practical hole
    (an internal literal or a redirect to one) is closed, and reaching this fetch
    already requires an operator to have wired an untrusted repo + a rawreplay pack.

    Raises :class:`BundleError` when the URL is not safe to fetch.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise BundleError("rawreplay dataset_url must be https, refusing %r" % url)
    if parts.username or parts.password:
        raise BundleError("rawreplay dataset_url must not embed credentials: %r" % url)
    host = parts.hostname
    if not host:
        raise BundleError("rawreplay dataset_url has no host: %r" % url)
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise BundleError("cannot resolve rawreplay dataset host %r: %s" % (host, exc))
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise BundleError(
                "rawreplay dataset host %r resolved to a bad address %r" % (host, ip))
        if not addr.is_global or addr.is_multicast:
            raise BundleError(
                "rawreplay dataset host %r resolves to non-public address %s (refused)"
                % (host, ip))


def _fetch_dataset_url(url, max_bytes, timeout_s, expected_sha256=None):
    # type: (str, int, float, Optional[str]) -> bytes
    """Fetch a rawreplay ``dataset_url`` into memory (https-only, size-capped).

    * validates the URL host is PUBLIC (see :func:`_assert_fetchable_url`) and
      re-validates it on every redirect hop, with auto-redirects disabled, so a
      public URL cannot bounce into an internal one (the SSRF guard);
    * streams the body and aborts once ``max_bytes`` is exceeded, so a hostile or
      accidentally-huge URL cannot exhaust the control-plane disk/memory;
    * verifies the sha256 when ``expected_sha256`` is supplied (pinned dataset).

    Raises :class:`BundleError` on any transport error, a refused/oversize body or
    a sha mismatch. The URL is safe to log (no credential is attached).
    """
    import requests  # local import: the control-plane image ships requests

    digest = hashlib.sha256()
    chunks = []  # type: List[bytes]
    total = 0
    current = url
    try:
        for _hop in range(_MAX_FETCH_REDIRECTS + 1):
            _assert_fetchable_url(current)  # re-check EVERY hop, not just the first
            with requests.get(
                current, stream=True, timeout=timeout_s, allow_redirects=False
            ) as resp:
                if resp.status_code in _REDIRECT_STATUSES:
                    loc = resp.headers.get("Location")
                    if not loc:
                        raise BundleError(
                            "rawreplay dataset %s: redirect with no Location" % current)
                    current = urljoin(current, loc)
                    continue
                if resp.status_code >= 400:
                    raise BundleError(
                        "rawreplay dataset fetch %s returned HTTP %d"
                        % (current, resp.status_code))
                for chunk in resp.iter_content(_DOWNLOAD_CHUNK):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise BundleError(
                            "rawreplay dataset %s exceeds the %d-byte cap" % (url, max_bytes))
                    digest.update(chunk)
                    chunks.append(chunk)
                break
        else:
            raise BundleError(
                "rawreplay dataset %s: too many redirects (> %d)" % (url, _MAX_FETCH_REDIRECTS))
    except BundleError:
        raise
    except requests.exceptions.RequestException as exc:
        raise BundleError("rawreplay dataset fetch %s failed: %s" % (url, exc))
    if total == 0:
        raise BundleError("rawreplay dataset %s is empty" % url)
    if expected_sha256 and digest.hexdigest() != expected_sha256.lower():
        raise BundleError(
            "rawreplay dataset %s sha256 mismatch: expected %s got %s"
            % (url, expected_sha256, digest.hexdigest()))
    log.info("fetched rawreplay dataset %s (%d bytes)", url, total)
    return b"".join(chunks)


def build_from_pack(pack_dir, bundle_dir=None, settings=None):
    # type: (str, Optional[str], Optional[Any]) -> BuiltBundle
    """Lint, build the reproducible tarball, store it content-addressed.

    Raises :class:`BundleError` when the pack fails lint. Returns a
    :class:`BuiltBundle` (digest, path, size, ``reused`` when the digest already
    existed on disk). The caller upserts the ``bundles`` row keyed by digest.
    ``bundle_dir`` defaults to the configured ``BUNDLE_DIR``.

    rawreplay: the dataset always ships inside the bundle. A local ``dataset:``
    file is tarred in place; a ``dataset_url`` is fetched at build time (https
    only, size-capped, sha-verified when declared) and embedded at a fixed
    bundle-relative path. The manifest's ``replay`` block then points the worker
    at the in-bundle dataset, so the worker never re-fetches.
    """
    lint = lint_pack(pack_dir)
    if not lint.ok:
        raise BundleError("pack %r failed lint: %s" % (pack_dir, "; ".join(lint.errors)))

    if settings is None:
        from .config import get_settings

        settings = get_settings()
    if bundle_dir is None:
        bundle_dir = settings.bundle_dir

    extra_relpaths = None  # type: Optional[List[str]]
    extra_files = None  # type: Optional[List[Tuple[str, bytes]]]
    dataset_relpath = None  # type: Optional[str]
    if lint.engine == RAWREPLAY_ENGINE and lint.replay is not None:
        extra_relpaths, extra_files, dataset_relpath = _rawreplay_dataset_members(
            pack_dir, lint.replay, settings)

    manifest = build_stoker_manifest(pack_dir, lint, dataset_relpath=dataset_relpath)
    data = build_tarball_bytes(
        pack_dir, manifest, extra_relpaths=extra_relpaths, extra_files=extra_files)
    digest = hashlib.sha256(data).hexdigest()

    os.makedirs(bundle_dir, exist_ok=True)
    path = os.path.join(bundle_dir, "%s.tgz" % digest)
    if os.path.isfile(path) and os.path.getsize(path) == len(data):
        log.info("bundle %s already present (dedup)", digest[:12])
        return BuiltBundle(digest=digest, path=path, size_bytes=len(data), reused=True)

    # Atomic write: temp then rename, so a concurrent reader never sees a partial.
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    log.info("built bundle %s (%d bytes) from %s", digest[:12], len(data), pack_dir)
    return BuiltBundle(digest=digest, path=path, size_bytes=len(data), reused=False)


def _as_float(value):
    # type: (Any) -> Optional[float]
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unique(items):
    # type: (List[str]) -> List[str]
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


__all__ = [
    "BundleError",
    "LintResult",
    "BuiltBundle",
    "lint_pack",
    "lint_rawreplay_pack",
    "is_rawreplay_pack",
    "parse_replay_config",
    "build_from_pack",
    "build_stoker_manifest",
    "build_tarball_bytes",
    "RAWREPLAY_ENGINE",
]
