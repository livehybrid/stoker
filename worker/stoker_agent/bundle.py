"""Bundle acquisition and pack metadata.

A bundle is a pack directory, a local .tgz or an http(s) URL to a .tgz
(fetched with the run JWT). The unpacked pack must contain
default/eventgen.conf; pack.yaml is optional metadata.

pack.yaml is parsed with a deliberately tiny hand-written parser covering a
flat two-level mapping of scalars only (the documented pack.yaml subset):

    name: flatline
    estimates:
      bytes_per_event: 180

No pyyaml at runtime. Lists, anchors, multi-line scalars and deeper nesting
are ignored with a warning rather than failing the run. When the pack ships
a stoker.json it is preferred over pack.yaml (exact JSON, no subset caveats).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
from typing import Any, Dict, Optional

import requests

log = logging.getLogger("stoker.bundle")

CONF_RELPATH = os.path.join("default", "eventgen.conf")
_DOWNLOAD_CHUNK = 1 << 16


class BundleError(Exception):
    """Bundle unavailable, corrupt or not a valid pack."""


@dataclasses.dataclass
class Bundle:
    pack_dir: str
    conf_path: str
    samples_dir: str
    estimates: Dict[str, Any]
    # Raw-replay (PISTON) config, resolved from the pack's `replay:` section
    # (stoker.json preferred, else pack.yaml). None when the pack declares no
    # replay config (an eventgen-only pack). See resolve_replay_config.
    replay: Optional["ReplayConfig"] = None


@dataclasses.dataclass
class ReplayConfig:
    """The pack's raw-replay config for the rawreplay (PISTON) engine.

    Resolved from the pack's ``replay:`` section. ``dataset`` is absolutised
    against the pack directory here so the engine receives an absolute path
    (the engine env carries ``STOKER_RAWREPLAY_DATASET`` = this path).

    Fields:
        dataset: absolute path to the recorded dataset inside the pack
            (gzip-aware when it ends ``.gz``).
        mode: ``rate`` | ``cadence`` (default ``rate``).
        time_multiple: cadence gap scale (default 1.0).
        ts_field / ts_regex / ts_strptime: optional cadence timestamp hints.
    """

    dataset: str
    mode: str
    time_multiple: float
    ts_field: Optional[str] = None
    ts_regex: Optional[str] = None
    ts_strptime: Optional[str] = None


def fetch_bundle(source, workdir, sha256=None, jwt=None):
    # type: (str, str, Optional[str], Optional[str]) -> Bundle
    """Resolve `source` (dir, .tgz path or URL) into an unpacked Bundle."""
    if source.startswith("http://") or source.startswith("https://"):
        archive = _download(source, workdir, sha256=sha256, jwt=jwt)
        pack_dir = _unpack(archive, workdir)
    elif os.path.isdir(source):
        pack_dir = source
    elif os.path.isfile(source):
        if sha256:
            _verify_sha256(source, sha256)
        pack_dir = _unpack(source, workdir)
    else:
        raise BundleError("bundle not found: %r" % source)
    return _load_pack(pack_dir)


def _download(url, workdir, sha256=None, jwt=None):
    # type: (str, str, Optional[str], Optional[str]) -> str
    headers = {}
    if jwt:
        headers["Authorization"] = "Bearer " + jwt
    dest = os.path.join(workdir, "bundle.tgz")
    digest = hashlib.sha256()
    try:
        with requests.get(url, headers=headers, stream=True, timeout=60) as resp:
            if resp.status_code >= 400:
                raise BundleError("bundle fetch %s returned HTTP %d"
                                  % (url, resp.status_code))
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(_DOWNLOAD_CHUNK):
                    fh.write(chunk)
                    digest.update(chunk)
    except requests.exceptions.RequestException as exc:
        raise BundleError("bundle fetch %s failed: %s" % (url, exc))
    if sha256 and digest.hexdigest() != sha256.lower():
        raise BundleError("bundle sha256 mismatch: expected %s got %s"
                          % (sha256, digest.hexdigest()))
    return dest


def _verify_sha256(path, expected):
    # type: (str, str) -> None
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_DOWNLOAD_CHUNK), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected.lower():
        raise BundleError("bundle sha256 mismatch: expected %s got %s"
                          % (expected, digest.hexdigest()))


def _safe_members(tar, dest):
    # py3.9 tarfile has no extraction filter: reject traversal by hand
    dest_real = os.path.realpath(dest)
    for member in tar.getmembers():
        if member.islnk() or member.issym():
            log.warning("skipping link member %s in bundle", member.name)
            continue
        target = os.path.realpath(os.path.join(dest, member.name))
        if not (target == dest_real or target.startswith(dest_real + os.sep)):
            raise BundleError("bundle member escapes extraction dir: %r"
                              % member.name)
        yield member


def _unpack(archive, workdir):
    # type: (str, str) -> str
    dest = tempfile.mkdtemp(prefix="pack-", dir=workdir)
    try:
        with tarfile.open(archive, "r:*") as tar:
            tar.extractall(dest, members=_safe_members(tar, dest))
    except (tarfile.TarError, OSError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise BundleError("bundle unpack failed for %r: %s" % (archive, exc))
    return _find_pack_root(dest)


def _find_pack_root(dest):
    # type: (str) -> str
    """The pack root holds default/eventgen.conf: at dest or one level down."""
    if os.path.isfile(os.path.join(dest, CONF_RELPATH)):
        return dest
    entries = [e for e in sorted(os.listdir(dest))
               if os.path.isdir(os.path.join(dest, e))]
    for entry in entries:
        candidate = os.path.join(dest, entry)
        if os.path.isfile(os.path.join(candidate, CONF_RELPATH)):
            return candidate
    raise BundleError("no %s found in unpacked bundle at %s"
                      % (CONF_RELPATH, dest))


def _load_pack(pack_dir):
    # type: (str) -> Bundle
    # The rewritten conf lives in a workdir elsewhere; eventgen resolves a
    # relative sampleDir against that conf's directory, so a relative pack
    # path would silently generate zero events. Absolutise here.
    pack_dir = os.path.abspath(pack_dir)
    conf_path = os.path.join(pack_dir, CONF_RELPATH)
    if not os.path.isfile(conf_path):
        raise BundleError("pack %r is missing required %s"
                          % (pack_dir, CONF_RELPATH))
    samples_dir = os.path.join(pack_dir, "samples")
    if not os.path.isdir(samples_dir):
        samples_dir = pack_dir
    estimates = _load_estimates(pack_dir)
    replay = resolve_replay_config(pack_dir)
    return Bundle(pack_dir=pack_dir, conf_path=conf_path,
                  samples_dir=samples_dir, estimates=estimates, replay=replay)


# Recognised replay modes (mirrors stoker_rawreplay.engine's MODE_* constants;
# duplicated here so bundle.py stays free of an engine import).
_REPLAY_MODES = ("rate", "cadence")


def _load_pack_doc(pack_dir):
    # type: (str) -> Dict[str, Any]
    """Return the pack's metadata doc: stoker.json if present, else pack.yaml.

    stoker.json is exact JSON (no subset caveats) and is preferred; pack.yaml is
    parsed with the flat two-level scalar subset parser. Returns ``{}`` when the
    pack ships neither.
    """
    json_path = os.path.join(pack_dir, "stoker.json")
    if os.path.isfile(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (ValueError, OSError) as exc:
            raise BundleError("stoker.json unreadable in %r: %s" % (pack_dir, exc))
        return doc if isinstance(doc, dict) else {}
    yaml_path = os.path.join(pack_dir, "pack.yaml")
    if os.path.isfile(yaml_path):
        with open(yaml_path, "r", encoding="utf-8") as fh:
            return parse_pack_yaml(fh.read())
    return {}


def resolve_replay_config(pack_dir):
    # type: (str) -> Optional[ReplayConfig]
    """Resolve the pack's ``replay:`` section into a :class:`ReplayConfig`.

    Shape (stoker.json preferred over pack.yaml)::

        replay:
          dataset: samples/capture.log        # or dataset/capture.log.gz
          mode: cadence                       # rate (default) | cadence
          time_multiple: 1.0                  # cadence gap scale
          ts_regex: "..."                     # optional cadence hints
          ts_strptime: "%Y-%m-%dT%H:%M:%S"
          ts_field: _time

    Returns ``None`` when the pack declares no ``replay`` section (an
    eventgen-only pack). ``dataset`` is required when a ``replay`` section is
    present and is absolutised against ``pack_dir`` (a relative path is resolved
    inside the pack; an absolute path that escapes the pack is rejected so a
    crafted pack cannot read arbitrary host files).
    """
    doc = _load_pack_doc(pack_dir)
    section = doc.get("replay") if isinstance(doc, dict) else None
    if not isinstance(section, dict):
        return None

    dataset_rel = section.get("dataset")
    if not dataset_rel or not isinstance(dataset_rel, str):
        raise BundleError(
            "pack %r declares a replay section without a 'dataset' path" % pack_dir)

    pack_abs = os.path.abspath(pack_dir)
    if os.path.isabs(dataset_rel):
        dataset_abs = os.path.realpath(dataset_rel)
    else:
        dataset_abs = os.path.realpath(os.path.join(pack_abs, dataset_rel))
    pack_real = os.path.realpath(pack_abs)
    if dataset_abs != pack_real and not dataset_abs.startswith(pack_real + os.sep):
        raise BundleError(
            "replay dataset %r escapes the pack root (rejected)" % dataset_rel)
    if not os.path.isfile(dataset_abs):
        raise BundleError(
            "replay dataset not found in pack %r: %r" % (pack_dir, dataset_rel))

    mode = str(section.get("mode") or "rate").lower()
    if mode not in _REPLAY_MODES:
        raise BundleError(
            "replay mode must be one of %s, got %r"
            % (", ".join(_REPLAY_MODES), mode))

    time_multiple = 1.0
    tm_raw = section.get("time_multiple")
    if tm_raw is not None:
        try:
            time_multiple = float(tm_raw)
        except (TypeError, ValueError):
            raise BundleError("replay time_multiple must be a number, got %r" % tm_raw)
        if time_multiple < 0:
            raise BundleError("replay time_multiple must be >= 0, got %s" % time_multiple)

    def _opt_str(key):
        # type: (str) -> Optional[str]
        val = section.get(key)
        if val is None:
            return None
        return str(val)

    return ReplayConfig(
        dataset=dataset_abs,
        mode=mode,
        time_multiple=time_multiple,
        ts_field=_opt_str("ts_field"),
        ts_regex=_opt_str("ts_regex"),
        ts_strptime=_opt_str("ts_strptime"),
    )


def _load_estimates(pack_dir):
    # type: (str) -> Dict[str, Any]
    json_path = os.path.join(pack_dir, "stoker.json")
    if os.path.isfile(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (ValueError, OSError) as exc:
            raise BundleError("stoker.json unreadable in %r: %s" % (pack_dir, exc))
        estimates = doc.get("estimates") or {}
        if "bytes_per_event" in doc:
            estimates.setdefault("bytes_per_event", doc["bytes_per_event"])
        return estimates
    yaml_path = os.path.join(pack_dir, "pack.yaml")
    if os.path.isfile(yaml_path):
        with open(yaml_path, "r", encoding="utf-8") as fh:
            doc = parse_pack_yaml(fh.read())
        estimates = doc.get("estimates")
        if isinstance(estimates, dict):
            return estimates
        return {}
    return {}


def _coerce_scalar(text):
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
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def parse_pack_yaml(text):
    # type: (str) -> Dict[str, Any]
    """Parse the pack.yaml subset: a flat two-level mapping of scalars.

    Top-level `key: value` pairs and one indented level of `key: value`
    beneath a bare `key:` line. Comments (# to end of line, outside quotes)
    and blank lines are ignored; anything deeper or list-shaped is skipped
    with a warning. This is intentionally not a YAML parser.
    """
    result = {}     # type: Dict[str, Any]
    section = None  # type: Optional[str]
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if stripped.startswith("- "):
            log.warning("pack.yaml line %d: lists unsupported, skipped", lineno)
            continue
        if ":" not in stripped:
            log.warning("pack.yaml line %d: not a mapping entry, skipped", lineno)
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if indent == 0:
            if value == "":
                section = key
                result[key] = {}
            else:
                section = None
                result[key] = _coerce_scalar(value)
        elif section is not None:
            if value == "":
                log.warning("pack.yaml line %d: nesting beyond two levels "
                            "unsupported, skipped", lineno)
                continue
            result[section][key] = _coerce_scalar(value)
        else:
            log.warning("pack.yaml line %d: orphan indented entry, skipped",
                        lineno)
    return result


def _strip_comment(line):
    # type: (str) -> str
    out = []
    quote = None
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
