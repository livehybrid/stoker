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
import json
import logging
import os
import tarfile
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("stoker.bundles")

CONF_RELPATH = os.path.join("default", "eventgen.conf")
# Reproducible tar: a fixed epoch for every member (Stoker epoch, arbitrary but
# stable) so rebuilding an unchanged pack yields byte-identical archives.
_FIXED_MTIME = 1_700_000_000
_SAMPLE_MODES = ("sample", "replay")
_GLOBAL_SECTIONS = frozenset(("global", "default"))


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
    """Read pack.yaml via the worker's tiny subset parser (shared semantics)."""
    path = os.path.join(pack_dir, "pack.yaml")
    if not os.path.isfile(path):
        return {}
    # Reuse the worker's parser so the control plane and worker agree on the
    # pack.yaml subset. Imported lazily to avoid a hard worker dependency at
    # module import time.
    try:
        from stoker_agent.bundle import parse_pack_yaml
    except Exception:  # pragma: no cover - worker not importable
        log.info("stoker_agent not importable; skipping pack.yaml parse")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return parse_pack_yaml(fh.read())
    except OSError as exc:
        raise BundleError("cannot read pack.yaml in %r: %s" % (pack_dir, exc))


def lint_pack(pack_dir):
    # type: (str) -> LintResult
    """Lint a local pack directory per the contract's pack rules.

    Checks: the conf parses (configparser), every ``mode = sample`` stanza's
    sample file exists under ``samples/``, no output-side keys leak (they are
    stripped by the worker but flagged here as a warning-level error is not
    raised for them; ``outputMode`` present is tolerated and ignored), and at
    least one stanza exists. Also derives stanza names, engines, sourcetypes and
    a bytes/event estimate (measured from the first sample when the pack.yaml
    omits it).
    """
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


def build_stoker_manifest(pack_dir, lint):
    # type: (str, LintResult) -> Dict[str, Any]
    """Build the stoker.json manifest embedded in the bundle."""
    name = os.path.basename(os.path.normpath(pack_dir))
    pack_yaml = _read_pack_yaml(pack_dir)
    if isinstance(pack_yaml, dict) and isinstance(pack_yaml.get("name"), str):
        name = pack_yaml["name"]
    estimates = {}  # type: Dict[str, Any]
    if lint.est_bytes_per_event is not None:
        estimates["bytes_per_event"] = lint.est_bytes_per_event
    if lint.declared_per_day_gb is not None:
        estimates["per_day_gb"] = lint.declared_per_day_gb
    return {
        "name": name,
        "engine": lint.engines[0] if lint.engines else "eventgen",
        "estimates": estimates,
        "stanzas": lint.stanzas,
        "sourcetypes": lint.sourcetypes,
    }


def _iter_pack_files(pack_dir):
    # type: (str) -> List[Tuple[str, str]]
    """Yield (absolute_path, arcname) for the pack payload, sorted.

    Includes default/eventgen.conf, everything under samples/, and pack.yaml when
    present. The arcname is prefixed with the pack directory's basename so the
    archive unpacks to ``<pack>/default/eventgen.conf`` (root-plus-one, which the
    worker accepts). stoker.json is added separately by the builder.
    """
    base = os.path.basename(os.path.normpath(pack_dir))
    members = []  # type: List[Tuple[str, str]]
    for rel in (CONF_RELPATH, "pack.yaml"):
        full = os.path.join(pack_dir, rel)
        if os.path.isfile(full):
            members.append((full, os.path.join(base, rel)))
    samples_dir = os.path.join(pack_dir, "samples")
    if os.path.isdir(samples_dir):
        for root, _dirs, files in os.walk(samples_dir):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, pack_dir)
                members.append((full, os.path.join(base, rel)))
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


def build_tarball_bytes(pack_dir, manifest):
    # type: (str, Dict[str, Any]) -> bytes
    """Build the reproducible gzip tarball bytes for a pack + manifest.

    Deterministic: sorted members, fixed mtime/uid/gid/mode, gzip written with a
    zeroed mtime so identical inputs hash identically.
    """
    base = os.path.basename(os.path.normpath(pack_dir))
    raw = io.BytesIO()
    # mtime=0 in the gzip header keeps the compressed bytes stable.
    import gzip

    gz = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
    with tarfile.open(fileobj=gz, mode="w") as tar:
        # stoker.json first (stable position).
        manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
        info = _reproducible_tarinfo(os.path.join(base, "stoker.json"), len(manifest_bytes))
        tar.addfile(info, io.BytesIO(manifest_bytes))
        for full, arcname in _iter_pack_files(pack_dir):
            with open(full, "rb") as fh:
                data = fh.read()
            info = _reproducible_tarinfo(arcname, len(data))
            tar.addfile(info, io.BytesIO(data))
    gz.close()
    return raw.getvalue()


def build_from_pack(pack_dir, bundle_dir=None):
    # type: (str, Optional[str]) -> BuiltBundle
    """Lint, build the reproducible tarball, store it content-addressed.

    Raises :class:`BundleError` when the pack fails lint. Returns a
    :class:`BuiltBundle` (digest, path, size, ``reused`` when the digest already
    existed on disk). The caller upserts the ``bundles`` row keyed by digest.
    ``bundle_dir`` defaults to the configured ``BUNDLE_DIR``.
    """
    lint = lint_pack(pack_dir)
    if not lint.ok:
        raise BundleError("pack %r failed lint: %s" % (pack_dir, "; ".join(lint.errors)))

    if bundle_dir is None:
        from .config import get_settings

        bundle_dir = get_settings().bundle_dir

    manifest = build_stoker_manifest(pack_dir, lint)
    data = build_tarball_bytes(pack_dir, manifest)
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
    "build_from_pack",
    "build_stoker_manifest",
    "build_tarball_bytes",
]
