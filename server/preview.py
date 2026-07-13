"""Lightweight, side-effect-free pack preview renderer.

Generates a handful of sample events from a pack **without launching a fleet or
touching a HEC target** — for pack authoring and the new-job wizard. The
control-plane image does not ship eventgen, so this is a deliberately partial
in-process render, not a full eventgen run: it reads the pack's sample line(s)
and the ``eventgen.conf`` token stanzas (reusing :mod:`server.bundles` parsing),
then cycles the sample lines and applies the common token replacements a worker
would produce closely enough for a *visual* preview.

Supported token replacements (mirroring the vendored eventgen's semantics —
``token.<n>.token`` regex, ``token.<n>.replacementType``, ``token.<n>.replacement``):

* ``replacementType = timestamp`` — the token match is replaced with ``now``
  formatted through the configured ``replacement`` strftime pattern.
* ``replacementType = random`` (or ``rated``) with ``replacement = ipv4`` — a
  random dotted-quad IPv4 address.
* ``replacementType = random`` (or ``rated``) with ``replacement = integer[a:b]``
  — a random integer in ``[a, b]``.

Any other replacement type is left as-is (the token's matched text is kept), so
an unsupported token never corrupts the preview; it simply is not substituted.

Guarantees:

* **Pure**: no network, no subprocess, no writes. The only impurity is the wall
  clock (``now`` for timestamp tokens) and ``random`` for ipv4/integer tokens.
* **Confined**: only files that resolve *inside* the pack directory are read. A
  ``sampleFile`` / ``mvfile`` token whose value would escape the pack root
  (``..`` traversal or an absolute path) is refused — its stanza contributes no
  sample lines rather than reading an arbitrary host file.
"""

from __future__ import annotations

import configparser
import datetime
import logging
import os
import random
import re
from typing import Dict, List, Optional, Tuple

from . import bundles

log = logging.getLogger("stoker.preview")

# Clamp the requested event count to a sane range: a preview is a visual aid,
# never a load path. A caller asking for 0/negative gets 1; a caller asking for
# a huge number is capped so the control plane never renders an unbounded body.
PREVIEW_N_MIN = 1
PREVIEW_N_MAX = 200
PREVIEW_N_DEFAULT = 10

# Read at most this many sample lines from a pack file for the render pool; a
# preview only ever cycles a small pool, so an enormous sample is bounded.
_MAX_SAMPLE_LINES = 1000

# The stanza name -> sample line resolution mirrors the linter: eventgen resolves
# an explicit ``sampleFile`` (else the stanza name) under ``samples/`` then the
# pack root.
_GLOBAL_SECTIONS = frozenset(("global", "default"))

# integer[<a>:<b>] range token (case-insensitive), matching eventgentoken.py.
_INTEGER_RE = re.compile(r"integer\[([-]?\d+):([-]?\d+)\]", re.IGNORECASE)


def clamp_n(n):
    # type: (Optional[int]) -> int
    """Clamp a requested event count into ``[PREVIEW_N_MIN, PREVIEW_N_MAX]``.

    ``None`` / non-int yields the default. This is the single source of truth for
    the cap so the route and the renderer agree.
    """
    if n is None:
        return PREVIEW_N_DEFAULT
    try:
        value = int(n)
    except (TypeError, ValueError):
        return PREVIEW_N_DEFAULT
    return max(PREVIEW_N_MIN, min(value, PREVIEW_N_MAX))


def preview_pack(pack_dir, n=PREVIEW_N_DEFAULT):
    # type: (str, int) -> List[str]
    """Render up to ``n`` preview events from a pack, purely and side-effect-free.

    Reads the first sample-mode stanza's sample line pool and its token stanzas,
    then cycles the pool applying the supported token replacements. Returns an
    empty list when the pack has no usable stanza / sample file (e.g. a missing
    conf, or a sample file that would escape the pack root). Never reads a file
    outside ``pack_dir``.
    """
    n = clamp_n(n)
    pack_root = os.path.realpath(pack_dir)
    if not os.path.isdir(pack_root):
        return []

    conf_path = os.path.join(pack_root, bundles.CONF_RELPATH)
    parser = _read_conf(conf_path)
    if parser is None:
        return []

    stanza = _first_sample_stanza(parser)
    if stanza is None:
        return []

    lines = _sample_pool(parser, stanza, pack_root)
    if not lines:
        return []

    tokens = _stanza_tokens(parser, stanza)
    now = datetime.datetime.now()

    out = []  # type: List[str]
    for i in range(n):
        base = lines[i % len(lines)]
        out.append(_render_line(base, tokens, now))
    return out


# --------------------------------------------------------------------------- #
# Conf + stanza resolution (reuses the linter's parser construction).
# --------------------------------------------------------------------------- #

def _read_conf(conf_path):
    # type: (str) -> Optional[configparser.RawConfigParser]
    """Parse a pack's eventgen.conf, or ``None`` when absent/unparseable."""
    if not os.path.isfile(conf_path):
        return None
    parser = configparser.RawConfigParser(
        delimiters=("=",), strict=False, allow_no_value=True, interpolation=None)
    parser.optionxform = str
    try:
        read = parser.read(conf_path, encoding="utf-8")
    except configparser.Error:
        return None
    if not read:
        return None
    return parser


def _first_sample_stanza(parser):
    # type: (configparser.RawConfigParser) -> Optional[str]
    """The first non-global stanza that is sample-mode (or mode-unset -> sample).

    Replay stanzas are engine-paced streams and a lightweight render of them is
    misleading, so they are skipped for the preview; a pack that is replay-only
    yields no preview.
    """
    for section in parser.sections():
        if section.lower() in _GLOBAL_SECTIONS:
            continue
        mode = (parser.get(section, "mode", fallback="sample") or "sample").strip()
        if mode == "sample":
            return section
    return None


def _stanza_tokens(parser, section):
    # type: (configparser.RawConfigParser, str) -> List[Tuple[re.Pattern, str, str]]
    """Collect ``(compiled_regex, replacementType, replacement)`` for a stanza.

    Groups the ``token.<n>.token`` / ``.replacementType`` / ``.replacement`` keys
    by their index ``<n>`` and returns them in ascending index order (the order
    eventgen applies them). A token whose regex will not compile is skipped (the
    linter flags it separately); a token with no ``token`` pattern is skipped.
    """
    by_index = {}  # type: Dict[str, Dict[str, str]]
    for key in parser.options(section):
        parts = key.split(".")
        # token.<index>.<field>
        if len(parts) == 3 and parts[0] == "token":
            index, field = parts[1], parts[2]
            by_index.setdefault(index, {})[field] = parser.get(section, key)

    tokens = []  # type: List[Tuple[re.Pattern, str, str]]
    for index in sorted(by_index, key=_index_sort_key):
        spec = by_index[index]
        pattern = spec.get("token")
        if not pattern:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        rtype = (spec.get("replacementType") or "").strip()
        replacement = spec.get("replacement") or ""
        tokens.append((compiled, rtype, replacement))
    return tokens


def _index_sort_key(index):
    # type: (str) -> Tuple[int, object]
    """Sort token indices numerically when possible, else lexicographically."""
    try:
        return (0, int(index))
    except ValueError:
        return (1, index)


# --------------------------------------------------------------------------- #
# Sample-file resolution (path-confined to the pack root).
# --------------------------------------------------------------------------- #

def _sample_pool(parser, section, pack_root):
    # type: (configparser.RawConfigParser, str, str) -> List[str]
    """Read the first non-empty sample lines for a stanza, confined to the pack.

    Resolves ``sampleFile`` (else the stanza name) under ``samples/`` then the
    pack root, exactly like the linter. A resolved path that escapes the pack
    root (``..`` or absolute) is refused (returns ``[]``) so the preview can
    never read an arbitrary host file.
    """
    sample_name = parser.get(section, "sampleFile", fallback=None) or section
    samples_dir = os.path.join(pack_root, "samples")
    for base in (samples_dir, pack_root):
        path = _safe_join(pack_root, base, sample_name)
        if path is not None and os.path.isfile(path):
            return _read_lines(path)
    return []


def _safe_join(pack_root, base, name):
    # type: (str, str, str) -> Optional[str]
    """Join ``base``/``name`` and return it only if it stays inside ``pack_root``.

    Refuses absolute ``name`` values and any ``..`` traversal by resolving the
    real path and checking it is contained by the pack root. Returns ``None`` for
    a path that would escape (the caller then reads nothing for that stanza).
    """
    candidate = os.path.realpath(os.path.join(base, name))
    root = os.path.realpath(pack_root)
    # Contained iff candidate == root or candidate is under root/ .
    if candidate == root:
        return candidate
    prefix = root + os.sep
    if candidate.startswith(prefix):
        return candidate
    return None


def _read_lines(path, limit=_MAX_SAMPLE_LINES):
    # type: (str, int) -> List[str]
    """Read up to ``limit`` non-empty lines from a file, tolerating bad bytes."""
    lines = []  # type: List[str]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                stripped = raw.rstrip("\r\n")
                if not stripped:
                    continue
                lines.append(stripped)
                if len(lines) >= limit:
                    break
    except OSError:
        return []
    return lines


# --------------------------------------------------------------------------- #
# Token rendering (pure per token; matches vendored eventgen closely enough).
# --------------------------------------------------------------------------- #

def _render_line(line, tokens, now):
    # type: (str, List[Tuple[re.Pattern, str, str]], datetime.datetime) -> str
    """Apply each token replacement to a single sample line, in order."""
    for compiled, rtype, replacement in tokens:
        line = _apply_token(line, compiled, rtype, replacement, now)
    return line


def _apply_token(line, compiled, rtype, replacement, now):
    # type: (str, re.Pattern, str, str, datetime.datetime) -> str
    """Substitute every match of one token in ``line``.

    A fresh replacement value is drawn per match for random tokens (ipv4 /
    integer), so two ipv4 tokens on one line differ, mirroring eventgen. An
    unsupported/unknown token type leaves the matched text untouched.

    When the token regex has a capturing group, only **group 1** is replaced and
    the literal text on either side of it within the match is preserved — exactly
    as the vendored eventgen does (it substitutes ``match.start(1)..end(1)``, not
    the whole match). This keeps structural context intact: a ``srcip=(...)``
    token keeps ``srcip=``, a ``"sourceIPAddress":"(...)"`` token keeps the JSON
    key and quotes, and a ``] (ip)`` token keeps the ``] `` before the address.
    A groupless token replaces the whole match (e.g. a bare timestamp regex).
    """
    has_group = compiled.groups >= 1

    def _sub(_match):
        # type: (re.Match) -> str
        value = _replacement_value(rtype, replacement, now)
        # Unsupported token: keep the original matched text verbatim.
        if value is None:
            return _match.group(0)
        if has_group and _match.group(1) is not None:
            whole = _match.group(0)
            base = _match.start(0)
            return whole[: _match.start(1) - base] + value + whole[_match.end(1) - base :]
        return value

    return compiled.sub(_sub, line)


def _replacement_value(rtype, replacement, now):
    # type: (str, str, datetime.datetime) -> Optional[str]
    """Compute one replacement value, or ``None`` for an unsupported token.

    ``timestamp`` -> ``now`` via the strftime ``replacement`` pattern.
    ``random``/``rated`` + ``ipv4`` -> a random dotted-quad.
    ``random``/``rated`` + ``integer[a:b]`` -> a random int in range.
    Anything else -> ``None`` (leave the token's matched text in place).
    """
    rtype = (rtype or "").lower()
    if rtype == "timestamp":
        pattern = replacement or "%Y-%m-%dT%H:%M:%S"
        try:
            return now.strftime(pattern)
        except (ValueError, TypeError):
            return None
    if rtype in ("random", "rated"):
        value = (replacement or "").strip()
        if value.lower() == "ipv4":
            return _random_ipv4()
        m = _INTEGER_RE.match(value)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if end >= start:
                return str(random.randint(start, end))
            return None
    return None


def _random_ipv4():
    # type: () -> str
    """A random dotted-quad IPv4 (matches eventgen's ipv4 replacement)."""
    return ".".join(str(random.randint(0, 255)) for _ in range(4))


__all__ = [
    "preview_pack",
    "clamp_n",
    "PREVIEW_N_MIN",
    "PREVIEW_N_MAX",
    "PREVIEW_N_DEFAULT",
]
