"""Export a registered pack as a portable archive — the mirror of packupload.

``GET /api/packs/{id}/export`` streams a pack back out as a ``.tar.gz`` that
``POST /api/packs/upload`` accepts **on another Stoker instance**, so a pack
built here (typically in the pack builder) can be moved to a colleague's
instance, a customer's air-gapped control plane, or into version control,
without git access on either side.

The archive is rooted at a single top-level directory named after the pack —
one of the two shapes :func:`server.packupload.find_pack_root` accepts — and is
**reproducible** (sorted members, fixed mtime/uid/gid/mode, zeroed gzip mtime),
so exporting an unchanged pack twice yields identical bytes and a checksum is
worth comparing across instances.

What travels
------------
A fixed list of pack-relative paths: ``pack.yaml``, ``default/eventgen.conf``,
``stoker.json`` (a metrics pack's ``metricgen``), ``stoker-builder.json`` (so a
builder pack reopens in the *receiving* instance's builder, editable), any
``README.md``, everything under ``samples/`` and, for a rawreplay pack, its
dataset.

**Why an allowlist and not a directory walk.** ``POST /api/packs`` registers an
arbitrary operator-supplied ``source_path``: nothing stops a pack row pointing
at ``/`` or ``/app``. A whole-tree export would turn "may register a path" into
"may download any file the control plane can read" — the master key included —
which is a privilege escalation over what the operator role otherwise has. The
allowlist bounds a hostile row to a handful of specifically-named files, the
same exposure the existing bundle build already has. Symlinks and any path
whose real location escapes the pack root are refused on top (reusing
``bundles._iter_pack_files``'s guards for the ``samples/`` walk), so a pack from
an untrusted git repo cannot smuggle ``/etc/passwd`` into the archive either.

Self-contained rawreplay
------------------------
A rawreplay pack that only declares ``dataset_url`` is **not portable**: the
receiving instance would have to fetch that URL when it builds a bundle, which
an air-gapped one cannot. With ``include_dataset`` (the default) the dataset is
fetched here through the existing SSRF-safe, size-capped, gzip-decompressing
path and embedded at ``dataset/replay.dat``, and a ``dataset:`` key is added to
the exported ``pack.yaml``'s ``replay:`` block. A local ``dataset`` always wins
over ``dataset_url`` (see docs/PACKS.md), so the original URL stays in the file
as provenance and the pack replays offline.

Caps
----
The export is checked against the **receiving** instance's upload limits
(``PACK_UPLOAD_MAX_*``): member count, per-member bytes, total uncompressed
bytes and the compressed archive body. Exceeding one raises
:class:`PackExportError` naming the limit, rather than handing back an archive
that the other end would refuse.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import os
import re
import tarfile
from typing import Any, Dict, List, Optional, Tuple

from . import bundles
# The single definition of a reproducible tar member (fixed mtime/uid/gid/mode)
# lives in bundles; importing it keeps exported and bundled archives from
# drifting apart on determinism.
from .bundles import CONF_RELPATH, _iter_pack_files, _reproducible_tarinfo

log = logging.getLogger("stoker.packexport")

# Pack-relative files that travel besides the ``samples/`` tree. Kept explicit:
# see the module docstring on why this is not a directory walk.
EXPORT_RELPATHS = (
    "pack.yaml",
    CONF_RELPATH,
    "stoker.json",
    "stoker-builder.json",
    "README.md",
)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class PackExportError(ValueError):
    """An export the control plane refuses, with an operator-facing reason."""


@dataclasses.dataclass
class ExportResult:
    """A built export: the archive bytes plus what the caller should report."""

    filename: str
    data: bytes
    members: int
    uncompressed_bytes: int
    dataset_embedded: bool


def safe_basename(name, fallback="pack"):
    # type: (str, str) -> str
    """A single safe path segment for the archive's top-level directory.

    The extracted directory is named after the pack, so it is recognisable on
    the far side; a pack name is free text, so strip it to ``[A-Za-z0-9._-]``
    and refuse the degenerate results (``""``, ``.``, ``..``).
    """
    slug = _SAFE_NAME_RE.sub("-", str(name or "")).strip("-._")
    slug = slug[:64].strip("-._")
    return slug or fallback


def _limits(settings):
    # type: (Any) -> Dict[str, int]
    return {
        "members": int(getattr(settings, "pack_upload_max_members", 10000)),
        "member_bytes": int(getattr(settings, "pack_upload_max_member_bytes", 512 * 1024 * 1024)),
        "total_bytes": int(getattr(settings, "pack_upload_max_total_bytes", 1024 * 1024 * 1024)),
        "archive_bytes": int(getattr(settings, "pack_upload_max_archive_bytes", 256 * 1024 * 1024)),
    }


def _add_local_dataset_key(text, relpath):
    # type: (str, str) -> Optional[str]
    """Add ``dataset: <relpath>`` to a pack.yaml ``replay:`` block.

    Returns None when there is no ``replay:`` line to extend (the caller then
    refuses rather than shipping a dataset nothing references). A block that
    already names a local ``dataset`` is returned unchanged.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    in_block = False
    for line in lines:
        if re.match(r"^replay:\s*(#.*)?$", line):
            in_block = True
            continue
        if in_block:
            if line.strip() and not line.startswith((" ", "\t")):
                break  # left the block
            if re.match(r"^\s+dataset:\s*\S", line):
                return text  # already local
    out = []  # type: List[str]
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and re.match(r"^replay:\s*(#.*)?$", line):
            out.append("  # dataset embedded by the pack export; any dataset_url "
                       "below is provenance only (a local dataset wins).")
            out.append("  dataset: %s" % relpath)
            inserted = True
    return "\n".join(out) if inserted else None


def _collect(pack_dir, base):
    # type: (str, str) -> List[Tuple[str, bytes]]
    """Read the allowlisted files plus the samples tree as ``(arcname, bytes)``.

    ``samples/`` comes from :func:`bundles._iter_pack_files`, which already
    refuses symlinks and paths escaping the pack root; the same check is applied
    to the allowlisted files here. Arcnames are rewritten onto ``base`` (the
    pack's name) rather than the on-disk directory basename.
    """
    entries = []  # type: List[Tuple[str, bytes]]
    seen = set()  # type: set
    root_real = os.path.realpath(pack_dir)
    on_disk_base = os.path.basename(os.path.normpath(pack_dir))

    def _read(full, rel):
        # type: (str, str) -> None
        if rel in seen:
            return
        if os.path.islink(full):
            log.warning("pack export: skipping symlink %s", full)
            return
        real = os.path.realpath(full)
        if real != root_real and not real.startswith(root_real + os.sep):
            log.warning("pack export: skipping %s (escapes the pack root)", full)
            return
        with open(full, "rb") as fh:
            entries.append(("%s/%s" % (base, rel), fh.read()))
        seen.add(rel)

    for rel in EXPORT_RELPATHS:
        full = os.path.join(pack_dir, rel)
        if os.path.isfile(full):
            _read(full, rel)
    # samples/** (and nothing else) via the bundle builder's guarded walk.
    for full, arc in _iter_pack_files(pack_dir):
        rel = arc[len(on_disk_base) + 1:] if arc.startswith(on_disk_base + os.sep) else arc
        rel = rel.replace(os.sep, "/")
        if rel.startswith("samples/"):
            _read(full, rel)
    return entries


def _pack_relative(pack_dir, rel):
    # type: (str, str) -> Optional[str]
    full = bundles._safe_join_pack(pack_dir, rel)
    return full if full and os.path.isfile(full) else None


def export_pack_bytes(pack_dir, pack_name, include_dataset=True, settings=None):
    # type: (str, str, bool, Optional[Any]) -> ExportResult
    """Build a portable ``.tar.gz`` for the pack directory ``pack_dir``.

    ``include_dataset`` embeds a rawreplay ``dataset_url`` payload (fetched
    here) so the exported pack replays without network access on the far side;
    a pack with a local ``dataset`` already carries it either way. Raises
    :class:`PackExportError` for a missing directory, a cap breach, or a dataset
    that cannot be fetched or wired into pack.yaml.
    """
    if settings is None:
        from .config import get_settings

        settings = get_settings()
    if not pack_dir or not os.path.isdir(pack_dir):
        raise PackExportError(
            "the pack has no directory on this control plane (%r): nothing to export"
            % (pack_dir or ""))
    limits = _limits(settings)
    base = safe_basename(pack_name)
    entries = _collect(pack_dir, base)
    if not entries:
        raise PackExportError(
            "the pack directory holds no exportable files (expected pack.yaml, "
            "%s, stoker.json or samples/)" % CONF_RELPATH)

    dataset_embedded = False
    replay = None  # type: Optional[Dict[str, Any]]
    try:
        if bundles.is_rawreplay_pack(pack_dir):
            # (config, errors); a pack that fails replay lint still exports — the
            # receiving instance reports the same lint errors on its pack card.
            replay = bundles.parse_replay_config(pack_dir)[0]
    except bundles.BundleError as exc:
        raise PackExportError("cannot read the pack's replay config: %s" % exc)

    if replay:
        local = replay.get("dataset")
        if local:
            # A dataset outside samples/ is not in the allowlist walk: add it.
            full = _pack_relative(pack_dir, local)
            if full is None:
                raise PackExportError(
                    "the pack declares dataset %r but the file is missing or escapes "
                    "the pack" % local)
            rel = os.path.relpath(full, os.path.realpath(pack_dir)).replace(os.sep, "/")
            if not any(arc == "%s/%s" % (base, rel) for arc, _ in entries):
                with open(full, "rb") as fh:
                    entries.append(("%s/%s" % (base, rel), fh.read()))
            dataset_embedded = True
        elif replay.get("fetch_url") and include_dataset:
            try:
                _, extra_files, rel = bundles._rawreplay_dataset_members(
                    pack_dir, replay, settings)
            except bundles.BundleError as exc:
                raise PackExportError(
                    "could not fetch the pack's dataset_url for export: %s" % exc)
            yaml_arc = "%s/pack.yaml" % base
            rewritten = None
            for i, (arc, data) in enumerate(entries):
                if arc == yaml_arc:
                    rewritten = _add_local_dataset_key(data.decode("utf-8", "replace"), rel)
                    if rewritten is None:
                        break
                    entries[i] = (arc, rewritten.encode("utf-8"))
                    break
            if rewritten is None:
                raise PackExportError(
                    "the pack's pack.yaml has no `replay:` block to point at the "
                    "embedded dataset; export with include_dataset=false instead")
            for extra_rel, data in (extra_files or []):
                entries.append(("%s/%s" % (base, extra_rel.replace(os.sep, "/")), data))
            dataset_embedded = True

    entries.sort(key=lambda pair: pair[0])
    total = sum(len(data) for _, data in entries)
    if len(entries) > limits["members"]:
        raise PackExportError("the pack has %d files, over the %d-member upload limit"
                              % (len(entries), limits["members"]))
    for arc, data in entries:
        if len(data) > limits["member_bytes"]:
            raise PackExportError(
                "%s is %d bytes, over the %d-byte per-file upload limit"
                % (arc, len(data), limits["member_bytes"]))
    if total > limits["total_bytes"]:
        raise PackExportError(
            "the pack unpacks to %d bytes, over the %d-byte upload limit"
            % (total, limits["total_bytes"]))

    data = _tar_gz(entries)
    if len(data) > limits["archive_bytes"]:
        raise PackExportError(
            "the export is %d bytes compressed, over the %d-byte upload-body limit; "
            "export with include_dataset=false and move the dataset separately"
            % (len(data), limits["archive_bytes"]))
    log.info("exported pack %r from %s (%d files, %d bytes, dataset=%s)",
             pack_name, pack_dir, len(entries), len(data), dataset_embedded)
    return ExportResult(filename="%s.tar.gz" % base, data=data, members=len(entries),
                        uncompressed_bytes=total, dataset_embedded=dataset_embedded)


def export_metric_pack_bytes(pack_name, config, description=None, tags=None, settings=None):
    # type: (str, Dict[str, Any], Optional[str], Optional[List[str]], Optional[Any]) -> ExportResult
    """Build a portable archive for a UI-authored metric pack (no directory).

    A metric pack's whole content is its ``metricgen`` config, so the archive is
    synthesised: ``stoker.json`` carries the config and ``pack.yaml`` declares
    ``engine: metrics``. The receiving instance lints that as a directory metric
    pack and stores the config as the pack's builder config, so it is editable
    in the metric builder there.
    """
    if settings is None:
        from .config import get_settings

        settings = get_settings()
    errors = bundles.lint_metrics_config(config or {})
    if errors:
        raise PackExportError("the metric pack fails lint, so it would not import: %s"
                              % "; ".join(errors))
    base = safe_basename(pack_name)
    n_metrics = len(config.get("metrics") or [])
    sourcetype = config.get("sourcetype") or bundles._DEFAULT_METRIC_SOURCETYPE
    yaml_lines = [
        "# Stoker metric pack exported from another instance. The metricgen block",
        "# in stoker.json is the pack; this file is its metadata.",
        "name: %s" % pack_name,
        "engine: %s" % bundles.METRICS_ENGINE,
    ]
    tag_list = [t for t in (tags or []) if str(t).strip()]
    if tag_list:
        yaml_lines.append("tags: %s" % ", ".join(str(t).strip() for t in tag_list))
    if description:
        yaml_lines.append("description: %s" % _yaml_scalar(description))
    yaml_lines += [
        "estimates:",
        "  bytes_per_event: %s" % round(120.0 + 45.0 * n_metrics, 1),
        "defaults:",
        "  sourcetype: %s" % sourcetype,
        "",
    ]
    manifest = {"name": pack_name, "engine": bundles.METRICS_ENGINE, "metricgen": config}
    entries = [
        ("%s/pack.yaml" % base, "\n".join(yaml_lines).encode("utf-8")),
        ("%s/stoker.json" % base,
         (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")),
    ]
    entries.sort(key=lambda pair: pair[0])
    data = _tar_gz(entries)
    return ExportResult(filename="%s.tar.gz" % base, data=data, members=len(entries),
                        uncompressed_bytes=sum(len(d) for _, d in entries),
                        dataset_embedded=False)


def _yaml_scalar(text):
    # type: (str) -> str
    """A single-line scalar for the flat pack.yaml subset (no newlines, no #)."""
    one = " ".join(str(text).split())
    return '"%s"' % one.replace("\\", "/").replace('"', "'")


def _tar_gz(entries):
    # type: (List[Tuple[str, bytes]]) -> bytes
    """Deterministic gzip tarball of ``(arcname, bytes)``, sorted by arcname."""
    import gzip

    raw = io.BytesIO()
    gz = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
    with tarfile.open(fileobj=gz, mode="w") as tar:
        for arcname, data in sorted(entries, key=lambda pair: pair[0]):
            tar.addfile(_reproducible_tarinfo(arcname, len(data)), io.BytesIO(data))
    gz.close()
    return raw.getvalue()


__all__ = ["ExportResult", "PackExportError", "EXPORT_RELPATHS", "export_pack_bytes",
           "export_metric_pack_bytes", "safe_basename"]
