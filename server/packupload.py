"""Safe extraction of an operator-uploaded pack archive (tar or zip).

``POST /api/packs/upload`` exists for the customer who has no git access: they
tar/zip a pack directory and upload it through the UI, and it must end up as an
ordinary local :class:`~server.models.Pack` row — same lint, same bundle build,
same runs — with its extracted directory persisted under ``PACK_UPLOAD_DIR``
(a control-plane volume, so uploads survive a restart).

The archive is **untrusted input supplied over HTTP**, which makes extraction
the security-critical step. The stdlib extractors are deliberately not used:
``tarfile.extractall`` was traversal-unsafe for most of its life (its
``filter=`` parameter fixes that, but member-by-member extraction lets us
enforce byte caps *while streaming*, which ``filter=`` does not), and
``zipfile.extractall`` neither caps decompressed output nor surfaces symlinks.
Instead every member is validated and copied by hand:

* **Path traversal** — any member whose path is absolute, carries a Windows
  drive/UNC prefix, or resolves outside the destination (``..`` segments) is
  refused. Paths are normalised (backslashes treated as separators — a zip
  built on Windows) before the check, and the joined destination is re-checked
  with ``realpath`` as a belt-and-braces second layer.
* **Symlinks and hardlinks are never created.** ``bundles._iter_pack_files``
  refuses symlinks at bundle time because a link pointing outside the pack
  would embed arbitrary control-plane files (the master key, /etc/passwd) into
  a tarball any worker can download. Extraction upholds the same invariant one
  layer earlier: a link member is refused outright rather than skipped, so an
  archive that tries it fails loudly instead of half-registering.
* **Device / fifo / other special members** are refused — nothing but plain
  files and directories is ever created.
* **Extraction bombs** — the member count, each member's uncompressed size and
  the TOTAL uncompressed size are capped (``PACK_UPLOAD_MAX_*``). The byte caps
  are enforced on the bytes actually produced during streaming, never on the
  archive's declared sizes, which a malicious archive lies about.
* **Nothing left behind** — extraction happens in a throwaway staging
  directory; on any rejection it is removed, so a bad upload leaves no trace.

Every rejection raises :class:`PackUploadError` with an operator-facing message
naming the offending member, so the person uploading learns *what* was wrong
rather than getting a bare 400.

The archive format is detected from the CONTENT (magic bytes), not the
filename, so a ``.tar.gz`` renamed ``.zip`` still extracts correctly and a
disguised non-archive is refused with a clear message.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import os
import re
import shutil
import stat
import tarfile
import uuid
import zipfile
from typing import Any, List, Optional

log = logging.getLogger("stoker.packupload")

# Magic bytes for content-based format detection. Zip: local-file header, or
# the empty/spanned end-of-central-directory records (a zip of zero members is
# still recognised, then rejected for having no pack). Gzip: the two-byte
# header of a .tar.gz/.tgz. A plain uncompressed tar has its magic at offset
# 257 ("ustar", POSIX or GNU flavour).
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_GZIP_MAGIC = b"\x1f\x8b"
_TAR_MAGIC_OFFSET = 257
_TAR_MAGICS = (b"ustar\x00", b"ustar ")

_COPY_CHUNK = 1 << 16

# Windows drive-letter prefix ("C:...") — meaningless on the server but present
# in archives built by some Windows tools; treated as an absolute path.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Top-level archive entries that are packaging junk, not pack content: they are
# skipped when deciding whether the archive wraps a single pack directory.
# macOS `tar`/Finder add `__MACOSX/` and `.DS_Store`; `._*` are AppleDouble
# resource forks.
_JUNK_NAMES = frozenset(("__MACOSX", ".DS_Store"))

# Final pack directory names are slugged from the operator's name / the
# archive's directory name: anything else becomes '-' so the name is safe as a
# path component (it is only cosmetic — containment never relies on it).
_SLUG_BAD_RE = re.compile(r"[^A-Za-z0-9._-]+")


class PackUploadError(Exception):
    """The uploaded archive was rejected; the message is operator-facing."""


@dataclasses.dataclass(frozen=True)
class UploadLimits:
    """Extraction-bomb caps, resolved from Settings (see config.py)."""

    max_members: int
    max_member_bytes: int
    max_total_bytes: int

    @classmethod
    def from_settings(cls, settings):
        # type: (Any) -> UploadLimits
        return cls(
            max_members=int(settings.pack_upload_max_members),
            max_member_bytes=int(settings.pack_upload_max_member_bytes),
            max_total_bytes=int(settings.pack_upload_max_total_bytes),
        )


def detect_format(data):
    # type: (bytes) -> Optional[str]
    """``"zip"`` / ``"tar"`` from the archive's magic bytes, else ``None``.

    Content-based on purpose: the filename extension is attacker/typo
    territory. Gzip data is assumed to be a compressed tar (``tarfile`` with
    ``mode="r:*"`` verifies that when it opens); a bare tar is recognised by
    the ustar magic at offset 257.
    """
    if any(data.startswith(m) for m in _ZIP_MAGICS):
        return "zip"
    if data.startswith(_GZIP_MAGIC):
        return "tar"
    at = data[_TAR_MAGIC_OFFSET:_TAR_MAGIC_OFFSET + 6]
    if any(at.startswith(m) for m in _TAR_MAGICS):
        return "tar"
    return None


def _member_dest(dest_dir, name):
    # type: (str, str) -> str
    """Resolve an archive member name to its destination path, or refuse.

    The single traversal chokepoint for both formats. Backslashes are treated
    as separators (Windows-built zips), then the path must be relative (no
    leading ``/``, no drive letter, no ``//`` UNC prefix) and free of ``..``
    segments. The joined result is re-verified with ``realpath`` containment so
    even a check bug cannot place a file outside ``dest_dir``.
    """
    normalised = name.replace("\\", "/")
    if not normalised or normalised.startswith("/") or normalised.startswith("//"):
        raise PackUploadError(
            "archive member %r has an absolute path (refused)" % name)
    if _WINDOWS_DRIVE_RE.match(normalised):
        raise PackUploadError(
            "archive member %r has a drive-letter path (refused)" % name)
    parts = [p for p in normalised.split("/") if p not in ("", ".")]
    if not parts:
        raise PackUploadError("archive member %r has an empty path (refused)" % name)
    if ".." in parts:
        raise PackUploadError(
            "archive member %r escapes the extraction directory (refused)" % name)
    dest = os.path.join(dest_dir, *parts)
    # Belt-and-braces containment: nothing above can produce an escaping path,
    # but the realpath check makes that a verified property, not an assumption.
    root = os.path.realpath(dest_dir)
    real = os.path.realpath(dest)
    if real != root and not real.startswith(root + os.sep):
        raise PackUploadError(
            "archive member %r escapes the extraction directory (refused)" % name)
    return dest


def _copy_capped(src, dest, name, member_cap, total_so_far, total_cap):
    # type: (Any, str, str, int, int, int) -> int
    """Stream ``src`` to ``dest``, enforcing the byte caps on ACTUAL output.

    Returns the new running total. The caps bind on the bytes produced by the
    stream, not on any size the archive declared — a lying header (the classic
    zip-bomb trick) is caught the moment the real output crosses a cap, with
    the partial file removed by the caller's staging cleanup.
    """
    written = 0
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as out:
        while True:
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > member_cap:
                raise PackUploadError(
                    "archive member %r exceeds the %d-byte per-file limit "
                    "(refused)" % (name, member_cap))
            if total_so_far + written > total_cap:
                raise PackUploadError(
                    "archive exceeds the %d-byte total uncompressed limit "
                    "(refused)" % total_cap)
            out.write(chunk)
    return total_so_far + written


def _extract_tar(data, dest_dir, limits):
    # type: (bytes, str, UploadLimits) -> None
    """Extract a (possibly gzipped) tar member-by-member with every guard.

    Never calls ``extractall``/``extract``: each member is validated (type,
    path, caps) and its bytes copied by hand, so no tar feature — link, device,
    setuid mode, traversal name — ever reaches the filesystem.
    """
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as exc:
        raise PackUploadError("could not read the upload as a tar archive: %s" % exc)
    members = 0
    total = 0
    with archive:
        for member in archive:
            members += 1
            if members > limits.max_members:
                raise PackUploadError(
                    "archive has more than %d members (refused)" % limits.max_members)
            if member.issym():
                raise PackUploadError(
                    "archive member %r is a symlink (refused: links are never "
                    "extracted)" % member.name)
            if member.islnk():
                raise PackUploadError(
                    "archive member %r is a hardlink (refused: links are never "
                    "extracted)" % member.name)
            if member.isdev() or member.isfifo():
                raise PackUploadError(
                    "archive member %r is a device/fifo node (refused)" % member.name)
            if member.isdir():
                os.makedirs(_member_dest(dest_dir, member.name), exist_ok=True)
                continue
            if not member.isreg():
                raise PackUploadError(
                    "archive member %r has unsupported type %r (refused)"
                    % (member.name, member.type))
            dest = _member_dest(dest_dir, member.name)
            src = archive.extractfile(member)
            if src is None:  # pragma: no cover - regular members always yield a stream
                raise PackUploadError(
                    "archive member %r could not be read (refused)" % member.name)
            with src:
                total = _copy_capped(src, dest, member.name,
                                     limits.max_member_bytes, total,
                                     limits.max_total_bytes)


def _extract_zip(data, dest_dir, limits):
    # type: (bytes, str, UploadLimits) -> None
    """Extract a zip member-by-member with every guard.

    A zip's central directory can lie about uncompressed sizes, so the byte
    caps are enforced by :func:`_copy_capped` on the decompressed stream, not
    on ``ZipInfo.file_size``. Unix mode bits ride ``external_attr >> 16``: a
    symlink or any other non-regular type stored there is refused (Info-ZIP
    encodes symlinks this way; extracting one as a plain file would silently
    change the pack, and creating the link would be the exfiltration hazard
    ``bundles._iter_pack_files`` exists to stop).
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PackUploadError("could not read the upload as a zip archive: %s" % exc)
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_members:
            raise PackUploadError(
                "archive has more than %d members (refused)" % limits.max_members)
        total = 0
        for info in infos:
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PackUploadError(
                    "archive member %r is a symlink (refused: links are never "
                    "extracted)" % info.filename)
            # Only the FILE-TYPE bits matter here: many tools store bare
            # permission bits (or nothing — Windows-built zips), so an absent
            # type (0) is treated as a regular file. Any explicit non-file,
            # non-directory type (device, fifo, socket) is refused.
            ftype = stat.S_IFMT(mode)
            if ftype not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise PackUploadError(
                    "archive member %r is not a regular file or directory "
                    "(refused)" % info.filename)
            if info.is_dir():
                os.makedirs(_member_dest(dest_dir, info.filename), exist_ok=True)
                continue
            dest = _member_dest(dest_dir, info.filename)
            try:
                with archive.open(info) as src:
                    total = _copy_capped(src, dest, info.filename,
                                         limits.max_member_bytes, total,
                                         limits.max_total_bytes)
            except zipfile.BadZipFile as exc:
                # Corrupt/lying entry (bad CRC, inconsistent sizes) surfaced
                # mid-stream: an operator-facing rejection, not a 500.
                raise PackUploadError(
                    "archive member %r is corrupt: %s" % (info.filename, exc))


def find_pack_root(extract_dir):
    # type: (str) -> str
    """Locate the pack root inside an extracted archive, or refuse.

    A customer usually tars up a *folder*, so the archive commonly holds one
    top-level directory that is the pack (``mypack/default/eventgen.conf``);
    equally it may be rooted at the pack itself (``default/eventgen.conf`` at
    the archive top). Both shapes are accepted:

    * the extraction root itself looks like a pack root -> use it;
    * otherwise, exactly one real top-level directory (macOS packaging junk
      ignored) that looks like a pack root -> use that.

    "Looks like a pack root" is :func:`lifecycle._looks_like_pack_root` — the
    same test the boot-time builtin-pack seeding uses — so upload and every
    other registration path agree on what a pack is. Anything else raises with
    a message describing the two accepted shapes.
    """
    from .lifecycle import _looks_like_pack_root

    if _looks_like_pack_root(extract_dir):
        return extract_dir
    dirs = []  # type: List[str]
    for entry in sorted(os.listdir(extract_dir)):
        if entry in _JUNK_NAMES or entry.startswith("._"):
            continue
        full = os.path.join(extract_dir, entry)
        if os.path.isdir(full):
            dirs.append(full)
    if len(dirs) == 1 and _looks_like_pack_root(dirs[0]):
        return dirs[0]
    raise PackUploadError(
        "no pack found in the archive: expected default/eventgen.conf, "
        "pack.yaml or stoker.json at the archive root, or inside a single "
        "top-level directory")


def _unique_pack_dir(upload_dir, hint):
    # type: (str, str) -> str
    """A fresh directory path under ``upload_dir`` derived from ``hint``.

    The slug is cosmetic (containment never depends on it): non-portable
    characters become ``-``, leading dots are stripped so the result is never
    hidden (and never collides with the ``.extract-*`` staging prefix), and a
    numeric suffix disambiguates repeat uploads of the same pack.
    """
    slug = _SLUG_BAD_RE.sub("-", hint or "").strip("-").lstrip(".") or "pack"
    candidate = os.path.join(upload_dir, slug)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(upload_dir, "%s-%d" % (slug, counter))
        counter += 1
    return candidate


def store_uploaded_pack(data, upload_dir, limits, name_hint=None):
    # type: (bytes, str, UploadLimits, Optional[str]) -> str
    """Extract an uploaded archive and persist its pack root under ``upload_dir``.

    The full pipeline: detect the format by content, extract safely into a
    throwaway ``.extract-*`` staging directory (every guard in this module),
    locate the pack root (wrapped or unwrapped), then move that root to a
    stable, uniquely-named directory the returned path points at. Any failure
    removes the staging directory entirely, so a rejected upload leaves
    nothing on disk. Raises :class:`PackUploadError` with an operator-facing
    reason on any rejection.
    """
    fmt = detect_format(data)
    if fmt is None:
        raise PackUploadError(
            "unrecognised archive: upload a .tar.gz/.tgz/.tar or .zip (the "
            "format is detected from the file content, not its name)")
    os.makedirs(upload_dir, exist_ok=True)
    staging = os.path.join(upload_dir, ".extract-%s" % uuid.uuid4().hex)
    os.makedirs(staging)
    try:
        if fmt == "zip":
            _extract_zip(data, staging, limits)
        else:
            _extract_tar(data, staging, limits)
        root = find_pack_root(staging)
        final = _unique_pack_dir(
            upload_dir, name_hint or os.path.basename(os.path.normpath(root)))
        # Same filesystem (both under upload_dir), so this is an atomic rename;
        # the pack appears at its final path complete or not at all.
        os.rename(root, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # The staging dir may survive the move (the wrapped-directory case leaves
    # the emptied wrapper plus any packaging junk); it is never the pack.
    shutil.rmtree(staging, ignore_errors=True)
    log.info("stored uploaded pack at %s (%s archive, %d bytes)",
             final, fmt, len(data))
    return final


__all__ = [
    "PackUploadError",
    "UploadLimits",
    "detect_format",
    "find_pack_root",
    "store_uploaded_pack",
]
