"""Renaming a pack: the row, the files that carry its name, and the bucket copy.

A pack's identity is its integer id (specs, runs and bundles all point at
``pack_id``), so a rename never touches history. The name still lives in three
other places, and each has to follow or the old name comes back:

* ``pack.yaml`` (and ``stoker-builder.json`` for a builder pack) inside the
  pack directory. Export copies these files, and an import takes the name from
  ``pack.yaml``, so a stale file re-imports the pack under its old name.
* The pack source (``STOKER_PACK_SOURCE``), where the archive is keyed by the
  name. The boot sync imports any archive whose name is not registered, so an
  archive left under the old key comes back at the next boot as a duplicate.

Only packs this control plane owns can be renamed: ones whose directory is in
``PACK_UPLOAD_DIR`` (uploaded, built, or imported from the pack source) and
operator-registered directories. A repo pack is named by its repo and a
builtin by its image; both are re-registered under that name on the next
sync or boot, so a rename there would only appear to work.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

from . import packsource
from .packbuilder import BUILDER_FILE

log = logging.getLogger(__name__)

MAX_NAME_LEN = 255

_NAME_LINE_RE = re.compile(r"^name\s*:")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class RenameError(ValueError):
    """A new name the pack cannot take (422)."""


def clean_name(name):
    # type: (Any) -> str
    """The new name, trimmed, or :class:`RenameError` if it cannot be stored."""
    text = str(name or "").strip()
    if not text:
        raise RenameError("a pack name cannot be empty")
    if len(text) > MAX_NAME_LEN:
        raise RenameError("a pack name is at most %d characters" % MAX_NAME_LEN)
    if _CONTROL_RE.search(text):
        raise RenameError("a pack name cannot contain control characters")
    if '"' in text and "'" in text:
        # pack.yaml's parser strips one pair of quotes and has no escapes, so
        # a name holding both quote characters cannot be written back.
        raise RenameError("a pack name cannot contain both ' and \"")
    return text


def _yaml_value(name):
    # type: (str) -> str
    quote = '"' if '"' not in name else "'"
    return "%s%s%s" % (quote, name, quote)


def rewrite_pack_yaml(pack_dir, name):
    # type: (str, str) -> bool
    """Set the top-level ``name:`` in ``pack.yaml``; add it if absent.

    Line-based so comments and every other key survive untouched. Returns
    False when the pack has no ``pack.yaml`` (its name is then its row alone).
    """
    path = os.path.join(pack_dir, "pack.yaml")
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    line = "name: %s" % _yaml_value(name)
    for i, existing in enumerate(lines):
        if _NAME_LINE_RE.match(existing):
            lines[i] = line
            break
    else:
        # Keep a leading comment block first, then the name.
        at = 0
        while at < len(lines) and lines[at].lstrip().startswith("#"):
            at += 1
        lines.insert(at, line)
    _write_atomic(path, "\n".join(lines) + "\n")
    return True


def rewrite_builder_config(pack_dir, name):
    # type: (str, str) -> bool
    """Set ``name`` in ``stoker-builder.json`` so the editor reopens it renamed."""
    path = os.path.join(pack_dir, BUILDER_FILE)
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["name"] = name
    _write_atomic(path, json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    return True


def _write_atomic(path, text):
    # type: (str, str) -> None
    tmp = "%s.rename-tmp" % path
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def owns_directory(pack, upload_dir):
    # type: (Any, str) -> bool
    """True when the pack's directory is inside ``upload_dir`` (Stoker wrote it)."""
    if not pack.source_path or not upload_dir:
        return False
    root = os.path.realpath(upload_dir)
    real = os.path.realpath(pack.source_path)
    return real.startswith(root + os.sep)


def swap_remote(pack, old_name, settings, store=None, env=None):
    # type: (Any, str, Any, Optional[packsource.Store], Optional[Dict[str, str]]) -> Dict[str, Any]
    """Publish ``pack`` under its new name, then remove the old archive.

    Returns what happened, for the response. Never raises: the rename has
    already been committed, so a bucket failure is reported as a ``warning``
    the page shows rather than undone. The old key is removed only after the
    new one is written, so a failure part way never leaves the pack absent
    from the bucket, only (at worst) present twice, which the warning names.
    """
    out = {"remote_key": None, "old_remote_key": None,
           "old_remote_removed": False, "warning": None}  # type: Dict[str, Any]
    if store is None:
        if not packsource.writes_enabled(env):
            info = packsource.describe(env)
            if info.get("configured"):
                out["warning"] = (
                    "the pack source is read-only, so its copy is still named %r "
                    "and will be imported again at the next boot" % old_name)
            return out
        store = packsource.store_from_env(env)
    if store is None:
        return out
    old_key = packsource.archive_name(old_name)
    new_key = packsource.publish_pack(pack, settings, store=store)
    out["remote_key"] = new_key
    if new_key is None:
        out["warning"] = ("the pack source refused the new archive; its copy is "
                          "still named %r" % old_name)
        return out
    if new_key == old_key:
        # Both names slug to the same key: the put above replaced it.
        return out
    out["old_remote_key"] = old_key
    try:
        out["old_remote_removed"] = bool(store.delete(old_key))
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        log.warning("renamed pack %r: could not remove %s from the pack source: %s",
                    pack.name, old_key, exc)
        out["warning"] = ("could not remove the old archive %s from the pack source "
                          "(%s); delete it there or it returns at the next boot"
                          % (old_key, exc))
    return out


__all__ = ["RenameError", "clean_name", "owns_directory", "rewrite_builder_config",
           "rewrite_pack_yaml", "swap_remote"]
