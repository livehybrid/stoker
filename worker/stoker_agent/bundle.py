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
    return Bundle(pack_dir=pack_dir, conf_path=conf_path,
                  samples_dir=samples_dir, estimates=estimates)


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
