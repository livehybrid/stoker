"""Consistent pseudonymisation of identifier values in a pack's sample events.

The problem this solves. An operator uploads real events to the pack builder and
marks a field like ``client_id`` as one to vary. Every other replacement kind
draws a value that has nothing to do with what it matched, so ``client_id=123``
in event 1 and ``client_id=123`` in event 3 become two unrelated values and the
correlation the data had is destroyed. This module replaces each value with a
**stand-in derived from the value itself**, so equal inputs give equal outputs
and the structure survives, while the original is not recoverable from the pack.

It runs at **build time**, not generation time: ``write_pack`` stores the
rewritten events, so the originals never enter the pack, the stand-ins reach
Splunk through every engine (firebox, the vendored Python eventgen) with no
engine change, and a worker that has never heard of this feature cannot leak
anything. The alternative, a new ``replacementType`` computed by the engines,
was rejected: both engines DROP a token whose type they do not recognise and
emit the matched text verbatim with exit 0, so a pack carrying real identifiers
would leak them on any un-patched worker.

Honest naming. This is **pseudonymisation**, not anonymisation. The mapping is a
keyed hash, so anyone holding the key can rebuild it, and an identifier space
small enough to enumerate (six digits is a million) can be brute-forced from the
key alone in seconds. Under GDPR Art 4(5) pseudonymised data about people is
still personal data. What the operator gets is: the pack and the replayed events
do not contain the originals, and correlation is preserved.

Format preservation. By default a stand-in keeps the span's own shape and width
(``same``), so ``\\d+`` extractions, numeric comparisons and dashboards keep
working: six digits in, six different digits out. That caps the output space,
which is why :func:`collision_probability` exists and why the builder can widen
a field to a fixed shape and length instead. A collision silently merges two
identities, which is precisely the correlation failure the feature exists to
prevent, so the caller must treat one as an error rather than a warning.

The format table (:func:`infer` / :func:`render`) is **class-stable**:
``infer(render(F, x, c)) == F``. A ``hex`` output always begins with a letter
from ``a-f`` so it can never be read back as digits, and a ``mixed`` output
always reserves a letter from ``g-z`` so it is never read back as hex. The
engine-side rotation mode relies on that property to prove two identities in two
classes can never render to the same string.

Stdlib only (``hashlib``, ``hmac``, ``unicodedata``): this module is imported by
the control plane, mirrored by ``server/preview.py``, and its golden vectors are
the cross-engine contract, so it must not acquire dependencies.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import math
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ALGORITHM = "hmac-sha256-v1"
ROTATE_ALGORITHM = "rotate-counter-v1"

# Domain separation: the stored instance key is never used as an HMAC key
# directly, so a future second purpose cannot be confused with this one.
SUBKEY_INFO = b"stoker-pseudonym/v1"

# Shapes. "none" is a span with no variable position at all (say "---"): there
# is nothing to pseudonymise, so it is left alone and the caller warns.
DIGITS, HEX, GUID, MIXED, NONE = "digits", "hex", "guid", "mixed", "none"
SHAPES_WIDENABLE = (DIGITS, HEX, GUID)

# Floors for an explicit widen, from the collision arithmetic: 12 digits is
# 9e11 and 16 hex is 6e18, both of which keep the expected merge count below
# 1e-5 for a 5,000-value pack. `same` has no floor - it is the span's own width.
WIDEN_FLOORS = {DIGITS: 12, HEX: 16}
WIDEN_DEFAULTS = {DIGITS: 15, HEX: 16}
WIDEN_MAX = 48

_DIGITS_RE = re.compile(r"^[0-9]+$")
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX_LOWER_RE = re.compile(r"^(?=.*[a-f])[0-9a-f]+$")
_HEX_UPPER_RE = re.compile(r"^(?=.*[A-F])[0-9A-F]+$")

_HEX_FIRST = "abcdef"            # radix 6: guarantees a hex output has a letter
_MIXED_FIRST_LETTER = "ghijklmnopqrstuvwxyz"   # radix 20, outside a-f
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_UPPER = _LOWER.upper()
_DEC = "0123456789"

LOWER, UPPER = "lower", "upper"


class PseudonymError(ValueError):
    """A pseudonymisation the builder refuses, with an operator-facing reason."""


@dataclasses.dataclass(frozen=True)
class Format:
    """A rendering class: ``(shape, width)``, or ``mixed`` with its template.

    ``template`` is one entry per character of the original span, each either
    ``("v", ch)`` for a position copied verbatim (separators, non-ASCII) or
    ``("c", radix, alphabet)`` for a variable one. It is part of the format
    because it defines both the output's shape and the size of the space.
    """

    shape: str
    width: int = 0
    template: Tuple[Any, ...] = ()

    def __str__(self):
        # type: () -> str
        if self.shape == GUID:
            return GUID
        if self.shape in (DIGITS, HEX):
            return "%s(%d)" % (self.shape, self.width)
        return self.shape


FORMAT_NONE = Format(NONE)


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #

def derive_subkey(key):
    # type: (bytes) -> bytes
    """The HMAC key actually used, derived from the stored instance key."""
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise PseudonymError("the pseudonym key must be at least 16 bytes")
    return hmac.new(bytes(key), SUBKEY_INFO, hashlib.sha256).digest()


def fingerprint(key):
    # type: (bytes) -> str
    """A short public label for a key: the first 8 hex of its SHA-256.

    Written into the pack so two packs can be seen to share a key (and so a
    pack built under a lost key is identifiable) without carrying the key.
    """
    return hashlib.sha256(bytes(key)).hexdigest()[:8]


# --------------------------------------------------------------------------- #
# The format table (normative; mirrored by every other implementation)
# --------------------------------------------------------------------------- #

def case_class(text):
    # type: (str) -> str
    """``upper`` when the span's letters are upper case, else ``lower``."""
    letters = [c for c in text if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return UPPER
    return LOWER


def infer(text):
    # type: (str) -> Format
    """The span's own format: the first matching rule wins.

    Order matters and is part of the contract: digits, then GUID, then hex,
    then the general ``mixed`` template.
    """
    if not text:
        return FORMAT_NONE
    if _DIGITS_RE.match(text):
        return Format(DIGITS, len(text))
    if _GUID_RE.match(text):
        letters = [c for c in text if c.isalpha()]
        if not letters or all(c.islower() for c in letters) or all(c.isupper() for c in letters):
            return Format(GUID)
    if _HEX_LOWER_RE.match(text) or _HEX_UPPER_RE.match(text):
        return Format(HEX, len(text))
    template = []  # type: List[Any]
    first_letter_used = False
    for ch in text:
        if ch in _DEC:
            template.append(("c", 10, _DEC))
        elif ch in _LOWER:
            if not first_letter_used:
                template.append(("c", 20, _MIXED_FIRST_LETTER))
                first_letter_used = True
            else:
                template.append(("c", 26, _LOWER))
        elif ch in _UPPER:
            if not first_letter_used:
                template.append(("c", 20, _MIXED_FIRST_LETTER.upper()))
                first_letter_used = True
            else:
                template.append(("c", 26, _UPPER))
        else:
            template.append(("v", ch))
    if all(e[0] == "v" for e in template):
        return FORMAT_NONE
    return Format(MIXED, len(text), tuple(template))


def space(fmt):
    # type: (Format) -> int
    """How many distinct strings this format can produce."""
    if fmt.shape == DIGITS:
        return 9 * 10 ** (fmt.width - 1)
    if fmt.shape == HEX:
        return 6 * 16 ** (fmt.width - 1)
    if fmt.shape == GUID:
        return 2 ** 122
    if fmt.shape == MIXED:
        total = 1
        for entry in fmt.template:
            if entry[0] == "c":
                total *= entry[1]
        return total
    return 1


def render(fmt, x, case=LOWER):
    # type: (Format, int, str) -> Optional[str]
    """Render the non-negative integer ``x`` in ``fmt``.

    Injective in ``x`` on ``[0, space(fmt))`` and class-stable, so
    ``infer(render(F, x, c)) == F``. Returns None for ``none`` (nothing to
    render), which the caller treats as "leave the span alone".
    """
    if x < 0:
        raise PseudonymError("render needs a non-negative integer")
    if fmt.shape == NONE:
        return None
    if fmt.shape == DIGITS:
        w = fmt.width
        return str(10 ** (w - 1) + (x % (9 * 10 ** (w - 1))))
    if fmt.shape == HEX:
        w = fmt.width
        v = x % (6 * 16 ** (w - 1))
        first, rest = divmod(v, 16 ** (w - 1))
        out = _HEX_FIRST[first] + (("%0*x" % (w - 1, rest)) if w > 1 else "")
        return out.upper() if case == UPPER else out
    if fmt.shape == GUID:
        b = bytearray((x % (2 ** 128)).to_bytes(16, "big"))
        b[6] = (b[6] & 0x0F) | 0x40      # version 4
        b[8] = (b[8] & 0x3F) | 0x80      # RFC 4122 variant
        h = bytes(b).hex()
        out = "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])
        return out.upper() if case == UPPER else out
    if fmt.shape == MIXED:
        v = x % space(fmt)
        # Decode most-significant-first so the leftmost variable position moves
        # fastest in the output; every implementation must agree on this order.
        radices = [e[1] for e in fmt.template if e[0] == "c"]
        picks = []  # type: List[int]
        for radix in reversed(radices):
            v, rem = divmod(v, radix)
            picks.append(rem)
        picks.reverse()
        out, i = [], 0
        for entry in fmt.template:
            if entry[0] == "v":
                out.append(entry[1])
            else:
                out.append(entry[2][picks[i]])
                i += 1
        return "".join(out)
    raise PseudonymError("unknown format %r" % (fmt.shape,))


def widen_format(shape, length=None):
    # type: (str, Optional[int]) -> Format
    """The format for an explicit widen, validating shape and length."""
    if shape not in SHAPES_WIDENABLE:
        raise PseudonymError("widen shape must be one of %s"
                             % ", ".join(SHAPES_WIDENABLE))
    if shape == GUID:
        return Format(GUID)
    if length is None:
        length = WIDEN_DEFAULTS[shape]
    floor = WIDEN_FLOORS[shape]
    if not floor <= int(length) <= WIDEN_MAX:
        raise PseudonymError("%s length must be between %d and %d"
                             % (shape, floor, WIDEN_MAX))
    return Format(shape, int(length))


# --------------------------------------------------------------------------- #
# The pseudonym itself
# --------------------------------------------------------------------------- #

def pseudonym(span, subkey, widen=None):
    # type: (str, bytes, Optional[Format]) -> Optional[str]
    """The stand-in for ``span``: same input and policy always give the same output.

    ``widen`` None keeps the span's own format (``same``). Returns None when the
    span has nothing to vary (empty, or no variable position), which the caller
    leaves untouched.
    """
    if not span:
        return None
    normalised = unicodedata.normalize("NFC", span)
    fmt = widen if widen is not None else infer(normalised)
    if fmt.shape == NONE:
        return None
    digest = hmac.new(subkey, normalised.encode("utf-8"), hashlib.sha256).digest()
    return render(fmt, int.from_bytes(digest, "big"), case_class(normalised))


def collision_probability(n, m):
    # type: (int, int) -> float
    """P(at least two of ``n`` uniform draws from ``m`` values coincide).

    Exact birthday probability, ``1 - prod(1 - i/m)``, summed in log space so
    it stays accurate for the small probabilities that matter here. This is
    what the builder shows before a save: a collision merges two identities,
    which corrupts the correlation the operator is testing.
    """
    if n < 2 or m <= 0:
        return 0.0
    if n > m:
        return 1.0
    total = math.fsum(math.log1p(-i / float(m)) for i in range(1, int(n)))
    return -math.expm1(total)


def expected_collisions(n, m):
    # type: (int, int) -> float
    """Expected number of coinciding pairs, ``n(n-1)/2m`` - the figure to show
    when the probability has saturated at 1."""
    if n < 2 or m <= 0:
        return 0.0
    return n * (n - 1) / (2.0 * m)


# --------------------------------------------------------------------------- #
# Residual scan: find an original that survived somewhere else
# --------------------------------------------------------------------------- #

def residual_regex(values):
    # type: (Iterable[str]) -> Optional[re.Pattern]
    """One trie-factored regex matching any of ``values`` as a whole token.

    Bounded by non-alphanumerics on both sides, which is the occurrence a
    Splunk search for the value would find: ``123`` matches in
    ``/clients/123/`` but not inside ``10.0.0.123`` or ``4123``. Factored into
    a trie because a flat alternation of a few thousand values is orders of
    magnitude slower over a 2 MB sample.
    """
    vals = sorted({v for v in values if v})
    if not vals:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % _trie_pattern(vals))


def _trie_pattern(values):
    # type: (Sequence[str]) -> str
    root = {}  # type: Dict[str, Any]
    for value in values:
        node = root
        for ch in value:
            node = node.setdefault(ch, {})
        node[""] = True          # terminal
    return _trie_to_regex(root)


def _trie_to_regex(node):
    # type: (Dict[str, Any]) -> str
    terminal = node.pop("", None) is not None
    if not node:
        return ""
    parts = []
    for ch in sorted(node):
        rest = _trie_to_regex(node[ch])
        parts.append(re.escape(ch) + rest)
    body = parts[0] if len(parts) == 1 else "(?:%s)" % "|".join(parts)
    return "(?:%s)?" % body if terminal else body


def find_residuals(text, pattern, masked=()):
    # type: (str, Optional[re.Pattern], Sequence[Tuple[int, int]]) -> List[Tuple[int, int, str]]
    """Occurrences of any original in ``text``, skipping ``masked`` ranges.

    **The masking is the whole point** (amendment A1). Under the default
    ``same`` policy the output space IS the input space, so a stand-in for one
    value can coincidentally equal a DIFFERENT original: with 1,000 six-digit
    identifiers the expected number of such coincidences is about 1.1, so two
    builds in three contain one. Scanning the rewritten events for "any
    original" without masking therefore refuses the save for a leak that does
    not exist, and rewriting the hit would replace one identity's stand-in with
    another's - silently merging them, which is the exact corruption the
    feature exists to prevent. ``masked`` is the set of spans this pass just
    rewrote; a hit intersecting one is the pass's own output, not a residue.
    """
    if pattern is None:
        return []
    out = []  # type: List[Tuple[int, int, str]]
    for m in pattern.finditer(text):
        s, e = m.start(), m.end()
        if any(s < me and ms < e for ms, me in masked):
            continue
        out.append((s, e, m.group(0)))
    return out


__all__ = [
    "ALGORITHM", "ROTATE_ALGORITHM", "DIGITS", "HEX", "GUID", "MIXED", "NONE",
    "FORMAT_NONE", "Format", "LOWER", "UPPER", "PseudonymError",
    "SHAPES_WIDENABLE", "WIDEN_DEFAULTS", "WIDEN_FLOORS", "WIDEN_MAX",
    "case_class", "collision_probability", "derive_subkey", "expected_collisions",
    "find_residuals", "fingerprint", "infer", "pseudonym", "render",
    "residual_regex", "space", "widen_format",
]
