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
# Rotation under "align across sourcetypes": the identity is a function of the
# stand-in and the clock, not of a position in a pack-local table, which is the
# only way two independently running streams can agree without coordination.
ALIGNED_ALGORITHM = "rotate-window-v1"
ALIGNED_DEFAULT_PERIOD = 60
_MASK64 = (1 << 64) - 1

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


# --------------------------------------------------------------------------- #
# Aligned rotation: one identity per time window, shared across sourcetypes
# --------------------------------------------------------------------------- #
#
# The pass counter gives the most identities, but ``ordinal`` and the sample's
# line count are private to one stanza, so two sourcetypes are never on the same
# pass and an identity cannot be joined across them. The clock is the only thing
# two independent streams share, so this derives the identity from the stand-in
# and the window index:
#
#     ident = mix(stand-in, window) mod space(F),  window = epoch // period
#
# Three properties worth stating, because they are the trade the operator is
# ticking a box to accept:
#
# * It hashes the STAND-IN, not the original. The pack already holds stand-ins,
#   so there is no key to ship to a worker and a worker too old to know
#   ``rotate`` emits the stable stand-in rather than leaking an identifier.
# * A hash collides where the counter cannot, at exactly the birthday rate
#   ``collision_probability`` already reports for mode 1, so the same widen
#   option is the answer to both.
# * Cardinality is ``table x duration / period`` rather than
#   ``table x passes x workers``, so the period is the dial between a realistic
#   user count and a joinable one.
#
# FNV-1a and splitmix64's finaliser are used rather than SHA-256 because the
# input is already a stand-in, so this is a mixing function and not a privacy
# boundary, and both engines can implement it in a few lines with no dependency.

def fnv1a64(text):
    # type: (str) -> int
    """FNV-1a over the UTF-8 bytes, 64-bit."""
    h = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & _MASK64
    return h


def mix64(x):
    # type: (int) -> int
    """splitmix64's finaliser: avalanches, so consecutive windows do not look it."""
    x &= _MASK64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _MASK64
    return x ^ (x >> 31)


def window_index(epoch, period):
    # type: (int, int) -> int
    """The window an event falls in, anchored on the Unix epoch.

    Anchoring on the epoch means two packs need agree on nothing but the period.
    Clamped at zero so a pre-1970 replay timestamp cannot wrap.
    """
    return max(0, int(epoch)) // max(1, int(period))


def aligned_ident(text, window, size):
    # type: (str, int, int) -> int
    """The aligned identity for ``text`` in ``window``, in ``[0, size)``."""
    seeded = fnv1a64(text) ^ ((window * 0x9E3779B97F4A7C15) & _MASK64)
    lo = mix64(seeded)
    hi = mix64(lo ^ 0xA5A5A5A5A5A5A5A5)
    return ((hi << 64) | lo) % max(1, size)


def permute(x, size):
    # type: (int, int) -> int
    """Spread a counter uniformly over ``[0, size)`` without losing injectivity.

    The raw rotation identity is a counter, so rendered directly it gives
    100006, 100007, 100008 ... clustered at the bottom of the space, and
    widening only adds leading zeros (100000000000006). Real identifiers do not
    look like that, and anything in the system under test that buckets or hashes
    on the field would see a distribution it never sees in production.

    A permutation fixes the look and keeps every guarantee, because it is a
    bijection: two distinct counters still give two distinct identities, so the
    pass and worker-slot disjointness arguments are untouched.

    Four-round balanced Feistel over the next even power of two, cycle-walking
    back into range. Unkeyed: the identities are synthetic, so there is nothing
    to keep secret, and both engines must agree.
    """
    if size <= 1:
        return 0
    bits = (size - 1).bit_length()
    half = (bits + 1) // 2
    mask = (1 << half) - 1
    v = x % size
    for _ in range(64):
        left = (v >> half) & mask
        right = v & mask
        for rnd in range(4):
            f = mix64((right + rnd * 0x9E3779B9) & _MASK64) & mask
            left, right = right, left ^ f
        v = (left << half) | right
        if v < size:
            return v
    return x % size


def rotate_ident(pass_, workers, slot, distinct, k):
    # type: (int, int, int, int, int) -> int
    """The raw rotation counter: ``(pass x workers + slot) x distinct + k``.

    A mixed-radix number read right to left, so the map is injective and two
    worker slots can never mint the same identity.
    """
    return (pass_ * max(1, workers) + slot) * max(1, distinct) + k


def pass_rotation(span, pass_, workers=1, slot=0, distinct=1, k=0, widen=None):
    # type: (str, int, int, int, int, int, Optional[Format]) -> Optional[str]
    """What the engine will emit for ``span`` on ``pass_``, for the preview.

    ``distinct`` and ``k`` come from the stanza's identity tables: how many
    sample values share this value's format class, and where this one sits in
    that class. The engines derive those from the sample, so the builder has to
    reproduce the same numbers to preview honestly.
    """
    if not span:
        return None
    normalised = unicodedata.normalize("NFC", span)
    fmt = widen if widen is not None else infer(normalised)
    if fmt.shape == NONE:
        return None
    size = space(fmt)
    return render(fmt, permute(rotate_ident(pass_, workers, slot, distinct, k), size),
                  case_class(normalised))


def aligned_rotation(span, window, widen=None):
    # type: (str, int, Optional[Format]) -> Optional[str]
    """The rotated stand-in for ``span`` in ``window``, in ``span``'s format.

    ``span`` is expected to be a stand-in already (mode 1 rewrote the pack when
    it was written), so this takes no key: every engine, worker and pack
    computes the same value from the same stand-in.
    """
    if not span:
        return None
    normalised = unicodedata.normalize("NFC", span)
    fmt = widen if widen is not None else infer(normalised)
    if fmt.shape == NONE:
        return None
    ident = aligned_ident(normalised, window, space(fmt))
    return render(fmt, ident, case_class(normalised))


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
    "residual_problems", "residual_regex", "scan_residuals", "space",
    "widen_format", "RESIDUAL_MIN_REFUSE",
]


# --------------------------------------------------------------------------- #
# Rewriting a whole sample (the build-time pass)
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class TokenRows:
    """What one pseudonym field did, for the operator and for the pack."""

    field: str
    pattern: str
    widen: Optional[Dict[str, Any]]
    spans: int = 0
    distinct: int = 0
    classes: List[Dict[str, Any]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class PseudonymReport:
    """The outcome of one build-time pass."""

    events: List[str]
    rewritten: Dict[int, List[Tuple[int, int]]]
    rows: List[TokenRows]
    originals: List[str]
    outputs: Dict[str, str]
    warnings: List[str] = dataclasses.field(default_factory=list)
    # normalised original -> its stand-in, and -> the field it came from. Only
    # ever held in memory, for the residual pass; never written anywhere.
    mapping: Dict[str, str] = dataclasses.field(default_factory=dict)
    origin: Dict[str, str] = dataclasses.field(default_factory=dict)
    residuals: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

    def total_spans(self):
        # type: () -> int
        return sum(r.spans for r in self.rows)


def _json_parses(text):
    # type: (str) -> bool
    import json
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def pseudonymise_events(events, tokens, subkey, widen_of=None):
    # type: (Sequence[str], Sequence[Dict[str, Any]], bytes, Optional[Any]) -> PseudonymReport
    """Rewrite every pseudonym field's spans in ``events``.

    ``tokens`` are the builder's token dicts; only those whose replacement kind
    is ``pseudonym`` are applied here. Returns the rewritten events plus, per
    event, the span ranges this pass wrote **in the rewritten text** - which is
    what the residual scan masks (see :func:`find_residuals`; without it a
    coincidence between a stand-in and a different original reads as a leak).

    Raises :class:`PseudonymError` for the four ways this can be wrong rather
    than silently producing a corrupt pack:

    * two distinct originals mapping to one stand-in (a merge, which destroys
      the correlation the feature exists to preserve);
    * two pseudonym fields matching overlapping text (one would hash the
      other's output);
    * a pattern whose capture group did not participate in a match (the span
      would silently become the whole match, hashing the field name too);
    * a JSON event that no longer parses after the rewrite (a non-digit
      stand-in written into an unquoted number).
    """
    out_events = []  # type: List[str]
    rewritten = {}   # type: Dict[int, List[Tuple[int, int]]]
    rows = []        # type: List[TokenRows]
    originals = {}   # type: Dict[str, str]
    outputs = {}     # type: Dict[str, str]
    mapping = {}     # type: Dict[str, str]
    origin = {}      # type: Dict[str, str]
    warnings = []    # type: List[str]
    memo = {}        # type: Dict[Tuple[str, Any], Optional[str]]
    by_output = {}   # type: Dict[Tuple[str, str], str]

    pseudo = [t for t in tokens
              if (t.get("replacement") or {}).get("kind") == "pseudonym"]
    compiled = []
    for token in pseudo:
        rep = token["replacement"]
        widen = None
        if rep.get("widen"):
            widen = widen_format(rep["widen"].get("shape"), rep["widen"].get("length"))
        row = TokenRows(field=token.get("field") or "", pattern=token["pattern"],
                        widen=rep.get("widen"))
        rows.append(row)
        compiled.append((re.compile(token["pattern"]), widen, row, token))

    if not compiled:
        return PseudonymReport(list(events), {}, [], [], {}, [])

    # field -> format class -> the distinct originals seen in it, collected in
    # the single rewrite pass below and rolled up afterwards for the operator's
    # collision figure.
    seen = {}  # type: Dict[str, Dict[Tuple[str, int], set]]
    for _rx, _w, row, _t in compiled:
        seen.setdefault(row.field, {})

    for index, event in enumerate(events):
        edits = []  # type: List[Tuple[int, int, str, TokenRows]]
        for rx, widen, row, token in compiled:
            for m in rx.finditer(event):
                if rx.groups >= 1:
                    if m.group(1) is None:
                        raise PseudonymError(
                            "%s: the pattern's capture group did not participate in a "
                            "match on event %d, so the whole match would be "
                            "pseudonymised; tighten the pattern"
                            % (row.field or row.pattern, index + 1))
                    s_i, e_i = m.start(1), m.end(1)
                else:
                    s_i, e_i = m.start(0), m.end(0)
                if e_i <= s_i:
                    continue
                span = event[s_i:e_i]
                policy = (widen.shape, widen.width) if widen is not None else None
                memo_key = (span, policy)
                if memo_key not in memo:
                    memo[memo_key] = pseudonym(span, subkey, widen=widen)
                stand_in = memo[memo_key]
                normalised = unicodedata.normalize("NFC", span)
                fmt = widen if widen is not None else infer(normalised)
                if stand_in is None:
                    note = ("%s: %r has no variable character, so it is left as it is"
                            % (row.field or row.pattern, span))
                    if note not in warnings:
                        warnings.append(note)
                    continue
                clash = by_output.get((fmt.shape, stand_in))
                if clash is not None and clash != normalised:
                    raise PseudonymError(
                        "%s: two different values would become the same stand-in %r, "
                        "which would merge them into one identity. Widen this field to "
                        "a fixed length so the space is large enough."
                        % (row.field or row.pattern, stand_in))
                by_output[(fmt.shape, stand_in)] = normalised
                originals[normalised] = normalised
                mapping[normalised] = stand_in
                origin.setdefault(normalised, row.field)
                outputs[stand_in] = fmt.shape
                seen[row.field].setdefault((fmt.shape, fmt.width), set()).add(normalised)
                edits.append((s_i, e_i, stand_in, row))

        # Two pseudonym fields matching overlapping text would have one hashing
        # the other's output; refuse rather than produce a corrupt pack.
        edits.sort(key=lambda t: (t[0], t[1]))
        for (s1, e1, _o1, r1), (s2, e2, _o2, r2) in zip(edits, edits[1:]):
            if s2 < e1:
                raise PseudonymError(
                    "%s and %s match overlapping text in event %d; one would "
                    "pseudonymise the other's output. Narrow one of the patterns."
                    % (r1.field or r1.pattern, r2.field or r2.pattern, index + 1))
        if not edits:
            out_events.append(event)
            continue
        pieces, pos, spans_out = [], 0, []  # type: List[str], int, List[Tuple[int, int]]
        written = 0
        for s_i, e_i, stand_in, row in edits:
            pieces.append(event[pos:s_i])
            written += s_i - pos
            spans_out.append((written, written + len(stand_in)))
            pieces.append(stand_in)
            written += len(stand_in)
            row.spans += 1
            pos = e_i
        pieces.append(event[pos:])
        new_event = "".join(pieces)
        if _json_parses(event) and not _json_parses(new_event):
            raise PseudonymError(
                "event %d is JSON and would no longer parse after pseudonymisation; "
                "a non-numeric stand-in was written into an unquoted number. Keep the "
                "format, or widen that field to digits." % (index + 1))
        out_events.append(new_event)
        rewritten[index] = spans_out

    for _rx, widen, row, _t in compiled:
        classes, every = [], set()
        for (shape, width), values in sorted(seen.get(row.field, {}).items()):
            if shape == MIXED:
                # the template varies per value; report the tightest space seen
                m = min(space(infer(v)) for v in values)
            else:
                m = space(Format(shape, width))
            classes.append({
                "shape": shape, "width": width, "distinct": len(values), "space": m,
                "collision_probability": round(collision_probability(len(values), m), 6),
                "expected_collisions": round(expected_collisions(len(values), m), 6),
            })
            every |= values
        row.classes = classes
        row.distinct = len(every)

    return PseudonymReport(out_events, rewritten, rows, sorted(originals), outputs,
                           warnings, mapping, origin)


# --------------------------------------------------------------------------- #
# Residuals: the same identifier surviving somewhere no field covered
# --------------------------------------------------------------------------- #

# An original this short cannot be told from coincidence (a 3-digit id matches
# inside an IP octet, a byte count, a port), so it warns instead of refusing.
RESIDUAL_MIN_REFUSE = 5


def scan_residuals(report, texts=(), rewrite_fields=()):
    # type: (PseudonymReport, Sequence[Tuple[str, str]], Sequence[str]) -> PseudonymReport
    """Find originals that survive outside the spans the pass rewrote.

    Pseudonymising ``client_id`` does not remove the same value from a URL, a
    message string or a JSON blob that no marked field matched - and the pack
    travels, through git-sync and the export button. So after the rewrite, every
    original is searched for again, in the rewritten events and in ``texts``
    (``(where, text)`` pairs: other fields' value lists, patterns, labels, the
    pack name and description).

    **Hits inside a span this pass wrote are skipped.** With the format kept the
    output space IS the input space, so a stand-in can coincidentally equal a
    different original: at 1,000 six-digit identifiers that happens in about two
    builds out of three. Reporting it would refuse a build for a leak that does
    not exist, and rewriting it would replace one identity's stand-in with
    another's, merging them.

    A field named in ``rewrite_fields`` has its own values rewritten in the
    events as well, with the same stand-in, which both removes the residue and
    makes the identifier correlate everywhere it appears. Patterns, labels and
    value lists are never rewritten: they are configuration the operator wrote,
    and silently editing them would change what the pack matches.
    """
    if not report.mapping:
        return report
    pattern = residual_regex(report.mapping)
    wanted = set(rewrite_fields or ())
    findings = []  # type: List[Dict[str, Any]]

    for index, event in enumerate(report.events):
        masked = list(report.rewritten.get(index, ()))
        while True:
            hits = [h for h in find_residuals(event, pattern, masked)
                    if h[2] in report.mapping]
            target = next((h for h in hits
                           if report.origin.get(h[2]) in wanted), None)
            if target is None:
                break
            start, end, value = target
            stand_in = report.mapping[value]
            event = event[:start] + stand_in + event[end:]
            shift = len(stand_in) - (end - start)
            masked = [(s + shift if s >= end else s, e + shift if e >= end else e)
                      for s, e in masked]
            masked.append((start, start + len(stand_in)))
            for row in report.rows:
                if row.field == report.origin.get(value):
                    row.spans += 1
        report.events[index] = event
        report.rewritten[index] = masked
        for start, end, value in find_residuals(event, pattern, masked):
            if value in report.mapping:
                findings.append({"where": "event %d" % (index + 1), "value": value,
                                 "field": report.origin.get(value), "rewritten": False})

    for where, text in texts or ():
        for _s, _e, value in find_residuals(text or "", pattern):
            if value in report.mapping:
                findings.append({"where": where, "value": value,
                                 "field": report.origin.get(value), "rewritten": False})

    report.residuals = findings
    return report


def residual_problems(report):
    # type: (PseudonymReport) -> Tuple[List[str], List[str]]
    """``(blocking, warnings)`` messages for the residuals found.

    Blocking for an original of %d characters or more, because that is long
    enough that an occurrence is the real value rather than a coincidence, and
    leaving it means the pack ships the identifier the operator asked to hide.
    Shorter values only warn: a 3-digit identifier genuinely does appear inside
    an IP address or a byte count, and refusing those would make the feature
    unusable. No message ever contains the value.
    """ % RESIDUAL_MIN_REFUSE
    blocking, warnings = [], []  # type: List[str], List[str]
    by_field = {}  # type: Dict[Tuple[str, bool], List[str]]
    for finding in report.residuals:
        long_enough = len(finding["value"]) >= RESIDUAL_MIN_REFUSE
        by_field.setdefault((finding.get("field") or "", long_enough), []).append(
            finding["where"])
    for (field, long_enough), wheres in sorted(by_field.items()):
        where = ", ".join(wheres[:3]) + (" and %d more" % (len(wheres) - 3)
                                         if len(wheres) > 3 else "")
        if long_enough:
            blocking.append(
                "%s: a value of this field still appears outside the field itself "
                "(%s), so the pack would carry the identifier you asked to hide. "
                "Turn on 'replace it everywhere' for this field, widen the pattern "
                "to cover those occurrences, or remove them from the sample."
                % (field or "a pseudonym field", where))
        else:
            warnings.append(
                "%s: a value of this field also appears outside the field (%s). It "
                "is short enough that this may be a coincidence, such as a digit "
                "run inside an address or a size, so it is not treated as a leak."
                % (field or "a pseudonym field", where))
    return blocking, warnings
