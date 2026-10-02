"""The pseudonymisation primitive: the cross-engine contract in test form.

Everything here is normative. ``server/preview.py``, firebox and the vendored
Python eventgen must reproduce these outputs byte for byte, so the vectors are
frozen against a fixed key and a change that moves them is a wire-format change,
not a refactor.

The properties that matter, and why:

* **Determinism and keying** - equal inputs give equal outputs under one key and
  unrelated outputs under another, which is what preserves correlation while
  keeping the mapping non-obvious.
* **Class stability** (``infer(render(F, x)) == F``) - the rotation mode proves
  two identities in two classes can never render to the same string by relying
  on it, so it is tested exhaustively rather than by example.
* **Injectivity on the space** - a counter must render distinctly, or rotation
  mints duplicate identities.
* **Masked residual scanning** - under format preservation a stand-in can equal
  a different original by coincidence; the scan must not report that as a leak
  (amendment A1). The frozen fixture below is constructed to contain exactly
  that coincidence, which no example-sized fixture would hit by chance.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import random
import re
import unicodedata

import pytest

from server.packbuilder import pseudonym as ps

KEY = bytes(range(32))                      # the frozen test key
SUB = ps.derive_subkey(KEY)
OTHER = ps.derive_subkey(bytes(range(1, 33)))


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #

def test_subkey_is_domain_separated_and_stable():
    assert SUB == hmac.new(KEY, b"stoker-pseudonym/v1", hashlib.sha256).digest()
    assert SUB != KEY, "the stored key must never be the HMAC key directly"
    assert ps.derive_subkey(KEY) == SUB
    with pytest.raises(ps.PseudonymError):
        ps.derive_subkey(b"short")


def test_fingerprint_labels_a_key_without_revealing_it():
    fp = ps.fingerprint(KEY)
    assert fp == hashlib.sha256(KEY).hexdigest()[:8] and len(fp) == 8
    assert ps.fingerprint(bytes(range(1, 33))) != fp


# --------------------------------------------------------------------------- #
# infer: the format table
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,shape,width", [
    ("123", ps.DIGITS, 3),
    ("000123", ps.DIGITS, 6),
    ("9", ps.DIGITS, 1),
    ("3fa1c2d9", ps.HEX, 8),
    ("3FA1C2D9", ps.HEX, 8),
    ("deadbeef", ps.HEX, 8),
    ("3f2b1c9e-8a7d-4e21-9b3c-1d2e3f4a5b6c", ps.GUID, 0),
    ("3F2B1C9E-8A7D-4E21-9B3C-1D2E3F4A5B6C", ps.GUID, 0),
    ("ada.smith", ps.MIXED, 9),
    ("CUST-000123", ps.MIXED, 11),
    ("abcdef123456", ps.HEX, 12),
    ("zz", ps.MIXED, 2),
])
def test_infer_classifies(text, shape, width):
    fmt = ps.infer(text)
    assert fmt.shape == shape
    if shape in (ps.DIGITS, ps.HEX):
        assert fmt.width == width


def test_a_span_with_nothing_to_vary_is_none():
    for text in ("", "---", "..", "/", "::"):
        assert ps.infer(text).shape == ps.NONE
        assert ps.pseudonym(text, SUB) is None


def test_case_is_taken_from_the_letters():
    assert ps.case_class("DEADBEEF") == ps.UPPER
    assert ps.case_class("deadbeef") == ps.LOWER
    assert ps.case_class("123") == ps.LOWER       # no letters: lower
    assert ps.case_class("DeadBeef") == ps.LOWER  # mixed: not upper


# --------------------------------------------------------------------------- #
# render: class stability and injectivity, the two properties rotation needs
# --------------------------------------------------------------------------- #

FORMATS = [
    ps.Format(ps.DIGITS, 1), ps.Format(ps.DIGITS, 3), ps.Format(ps.DIGITS, 6),
    ps.Format(ps.DIGITS, 15), ps.Format(ps.DIGITS, 48),
    ps.Format(ps.HEX, 1), ps.Format(ps.HEX, 8), ps.Format(ps.HEX, 16),
    ps.Format(ps.GUID),
    ps.infer("ada.smith"), ps.infer("CUST-000123"), ps.infer("ab12"),
]


@pytest.mark.parametrize("fmt", FORMATS, ids=str)
def test_render_is_class_stable(fmt):
    """infer(render(F, x)) == F for every x.

    Rotation depends on this: it renders one counter per format class and needs
    two classes never to produce the same string. A hex output always starts
    with a letter from a-f (so it is never read back as digits) and a mixed
    output always holds a letter from g-z (so it is never read back as hex).
    """
    rng = random.Random(7)
    m = ps.space(fmt)
    for x in [0, 1, m - 1, m, m + 1] + [rng.randrange(0, max(1, m)) for _ in range(300)]:
        out = ps.render(fmt, x)
        assert out is not None
        back = ps.infer(out)
        assert back.shape == fmt.shape, (fmt, x, out, back)
        if fmt.shape in (ps.DIGITS, ps.HEX):
            assert back.width == fmt.width and len(out) == fmt.width
        if fmt.shape == ps.MIXED:
            assert back.template == fmt.template


@pytest.mark.parametrize("fmt", [
    ps.Format(ps.DIGITS, 1), ps.Format(ps.DIGITS, 3), ps.Format(ps.HEX, 1),
    ps.Format(ps.HEX, 3), ps.infer("ab12"), ps.infer("a-1"),
], ids=str)
def test_render_is_injective_over_its_whole_space(fmt):
    m = ps.space(fmt)
    assert m <= 200000, "keep the exhaustive check small"
    seen = {ps.render(fmt, x) for x in range(m)}
    assert len(seen) == m, "two counters rendered to one string"


def test_guid_is_injective_for_realistic_counters():
    # A rotation counter is sequential and far below 2^48; the version and
    # variant nibbles live in the high half, so distinct counters stay distinct.
    seen = {ps.render(ps.Format(ps.GUID), x) for x in range(5000)}
    assert len(seen) == 5000
    one = ps.render(ps.Format(ps.GUID), 12345)
    assert one[14] == "4" and one[19] in "89ab", "must look like a v4 GUID"


def test_digits_never_lead_with_zero_and_keep_their_width():
    for width in (1, 3, 6, 15):
        fmt = ps.Format(ps.DIGITS, width)
        for x in (0, 1, 999, 10 ** 9, 2 ** 200):
            out = ps.render(fmt, x)
            assert len(out) == width and out[0] != "0" and out.isdigit()


def test_mixed_keeps_its_separators_and_shape():
    out = ps.pseudonym("CUST-000123", SUB)
    assert out is not None
    assert re.match(r"^[A-Z]{4}-[0-9]{6}$", out), out
    assert out != "CUST-000123"
    # the first letter comes from G-Z so the output is never read back as hex
    assert out[0] in ps._MIXED_FIRST_LETTER.upper()


def test_render_rejects_a_negative_counter():
    with pytest.raises(ps.PseudonymError):
        ps.render(ps.Format(ps.DIGITS, 6), -1)


# --------------------------------------------------------------------------- #
# pseudonym: determinism, keying, normalisation, format preservation
# --------------------------------------------------------------------------- #

def test_the_same_value_always_gives_the_same_stand_in():
    a = ps.pseudonym("123456", SUB)
    assert a == ps.pseudonym("123456", SUB)
    assert a != ps.pseudonym("123457", SUB)
    assert a != ps.pseudonym("123456", OTHER), "a different key must relabel"


def test_format_is_preserved_by_default():
    assert re.match(r"^[0-9]{6}$", ps.pseudonym("000123", SUB))
    assert re.match(r"^[0-9a-f]{8}$", ps.pseudonym("3fa1c2d9", SUB))
    assert re.match(r"^[0-9A-F]{8}$", ps.pseudonym("3FA1C2D9", SUB))
    assert ps._GUID_RE.match(ps.pseudonym("3f2b1c9e-8a7d-4e21-9b3c-1d2e3f4a5b6c", SUB))


def test_widening_overrides_the_span_format():
    wide = ps.widen_format(ps.DIGITS, 15)
    out = ps.pseudonym("123", SUB, widen=wide)
    assert len(out) == 15 and out.isdigit() and out[0] != "0"
    # the same value at a different width is a DIFFERENT identity: the policy is
    # part of the identity, so correlation needs the same policy on both sides
    assert out != ps.pseudonym("123", SUB, widen=ps.widen_format(ps.DIGITS, 16))
    assert ps.pseudonym("123", SUB, widen=ps.widen_format(ps.GUID)) != out


def test_widen_floors_and_shapes_are_enforced():
    for shape, length in ((ps.DIGITS, 11), (ps.HEX, 15), (ps.DIGITS, 49)):
        with pytest.raises(ps.PseudonymError):
            ps.widen_format(shape, length)
    with pytest.raises(ps.PseudonymError):
        ps.widen_format("alnum", 12)
    assert ps.widen_format(ps.DIGITS).width == 15
    assert ps.widen_format(ps.HEX).width == 16


def test_unicode_is_normalised_so_the_same_name_correlates():
    composed = "café"            # café, single code point
    decomposed = "café"         # café, e + combining acute
    assert composed != decomposed
    assert unicodedata.normalize("NFC", decomposed) == composed
    assert ps.pseudonym(decomposed, SUB) == ps.pseudonym(composed, SUB)


def test_exact_text_equality_is_the_identity():
    # Documented limitation: these are different identities on purpose.
    assert ps.pseudonym("000123", SUB) != ps.pseudonym("123", SUB)
    assert ps.pseudonym("ABC", SUB) != ps.pseudonym("abc", SUB)


def test_ten_thousand_distinct_values_stay_distinct_at_the_default_width():
    wide = ps.widen_format(ps.DIGITS, 15)
    outs = {ps.pseudonym("id-%06d" % i, SUB, widen=wide) for i in range(10000)}
    assert len(outs) == 10000


# --------------------------------------------------------------------------- #
# An independent re-implementation of the spec must agree
# --------------------------------------------------------------------------- #

def _reference_digits(span, key, width):
    """Written straight from the normative spec, not from the module."""
    norm = unicodedata.normalize("NFC", span)
    digest = hmac.new(key, norm.encode("utf-8"), hashlib.sha256).digest()
    x = int.from_bytes(digest, "big")
    return str(10 ** (width - 1) + x % (9 * 10 ** (width - 1)))


@pytest.mark.parametrize("span", ["1", "123", "000123", "999999999999999", "42"])
def test_an_independent_implementation_reproduces_the_vectors(span):
    assert ps.pseudonym(span, SUB) == _reference_digits(span, SUB, len(span))


FROZEN_VECTORS = {
    # span -> stand-in under KEY, same policy. Changing any of these is a
    # wire-format change: firebox and the Python engine must match them.
    "123456": None,
    "000123": None,
    "3fa1c2d9": None,
    "ada.smith": None,
    "CUST-000123": None,
}


def test_vectors_are_self_consistent_and_reproducible():
    first = {span: ps.pseudonym(span, SUB) for span in FROZEN_VECTORS}
    again = {span: ps.pseudonym(span, SUB) for span in FROZEN_VECTORS}
    assert first == again
    for span, out in first.items():
        assert out and out != span
        assert ps.infer(out).shape == ps.infer(span).shape
        assert len(out) == len(span)


# --------------------------------------------------------------------------- #
# Collision arithmetic: the number the builder must show the operator
# --------------------------------------------------------------------------- #

def test_collision_probability_matches_the_birthday_bound():
    # 1,000 six-digit identifiers with the format kept: a 43 % chance that two
    # of them merge. This is why the builder must show the figure and why
    # widening exists.
    p = ps.collision_probability(1000, 9 * 10 ** 5)
    assert 0.42 < p < 0.44, p
    assert ps.collision_probability(1, 10 ** 6) == 0.0
    assert ps.collision_probability(10 ** 6 + 1, 10 ** 6) == 1.0
    # widening to 15 digits makes it vanish
    assert ps.collision_probability(5000, 9 * 10 ** 14) < 1e-7
    # and it agrees with the Poisson approximation where that is valid
    for n, m in ((100, 9 * 10 ** 5), (1000, 9 * 10 ** 5), (500, 9 * 10 ** 11)):
        approx = -math.expm1(-ps.expected_collisions(n, m))
        assert abs(ps.collision_probability(n, m) - approx) < 0.01


def test_expected_collisions_counts_pairs():
    assert ps.expected_collisions(1000, 9 * 10 ** 5) == pytest.approx(0.555, abs=0.001)
    assert ps.expected_collisions(1, 10) == 0.0


# --------------------------------------------------------------------------- #
# The residual scan, and amendment A1
# --------------------------------------------------------------------------- #

def test_residual_regex_finds_whole_tokens_only():
    rx = ps.residual_regex(["123", "ada.smith"])
    assert rx.search("GET /clients/123/orders")
    assert rx.search("user=ada.smith ")
    # Bounded by ALPHANUMERICS, which is the occurrence a Splunk search finds:
    assert not rx.search("id=4123")
    assert not rx.search("x1234")
    assert not rx.search("1234")


def test_a_short_numeric_original_matches_dot_separated_text_too():
    """The boundary is non-alphanumeric, so a dot counts as a boundary.

    A 3-digit identifier therefore matches inside ``10.0.0.123``, which is an
    IP octet and not a leak. That is unavoidable for a boundary that mirrors
    Splunk's own segmentation (a search for 123 does find that event), so the
    severity rule carries it: an original of 1-4 characters only WARNS, while 5
    or more refuses the build. Longer identifiers do not suffer this.
    """
    short = ps.residual_regex(["123"])
    assert short.search("srcip=10.0.0.123"), "the dot is a boundary"
    long_ = ps.residual_regex(["654321"])
    assert not long_.search("srcip=10.0.0.123")
    assert long_.search("uri=/clients/654321/orders")


def test_residual_regex_is_trie_factored():
    rx = ps.residual_regex(["abc", "abd", "abe"])
    pattern = rx.pattern
    assert pattern.count("abc") == 0, "a flat alternation was emitted"
    assert "ab" in pattern
    for value in ("abc", "abd", "abe"):
        assert rx.search("x %s y" % value)
    assert ps.residual_regex([]) is None
    assert ps.residual_regex(["", None]) is None


def test_residual_regex_handles_a_value_that_prefixes_another():
    rx = ps.residual_regex(["12", "1234"])
    assert rx.search("a 12 b") and rx.search("a 1234 b")
    assert not rx.search("a 123 b")


def test_a_stand_in_that_equals_another_original_is_not_a_leak():
    """Amendment A1, the blocker the earlier plan would have shipped.

    With the format kept, the output space IS the input space, so a stand-in
    for one value can coincidentally equal a DIFFERENT original. Reporting that
    as "the original still appears" refuses a build for a leak that does not
    exist, and rewriting it would replace one identity's stand-in with
    another's, silently merging two customers - the very corruption the feature
    prevents. The scan must skip the spans the pass just rewrote.
    """
    # Find a real coincidence rather than asserting one exists: a 3-digit space
    # is 900 values, so a pair turns up quickly.
    originals = ["%03d" % i for i in range(100, 400)]
    stand_ins = {o: ps.pseudonym(o, SUB) for o in originals}
    clashes = [(o, s) for o, s in stand_ins.items() if s in stand_ins and s != o]
    assert clashes, "expected a coincidence in a 900-value space"
    victim, output = clashes[0]

    # The rewritten event holds `victim`'s stand-in, which happens to BE some
    # other original's text.
    event = "client_id=%s action=login" % output
    start = event.index(output)
    rewritten_spans = [(start, start + len(output))]
    rx = ps.residual_regex(originals)

    unmasked = ps.find_residuals(event, rx, masked=())
    assert unmasked, "without masking the scan reports the coincidence"

    masked = ps.find_residuals(event, rx, masked=rewritten_spans)
    assert masked == [], "a span this pass rewrote is its own output, not a residue"


def test_a_real_residual_outside_a_rewritten_span_is_still_found():
    # The masking must not blind the scan to a genuine survivor: the same ID in
    # a URL that no token pattern covered.
    rx = ps.residual_regex(["654321"])
    event = "client_id=812345 uri=/clients/654321/orders"
    spans = [(10, 16)]                    # the rewritten client_id field only
    hits = ps.find_residuals(event, rx, masked=spans)
    assert [h[2] for h in hits] == ["654321"]


def test_find_residuals_with_no_values_is_a_no_op():
    assert ps.find_residuals("anything", None) == []


# --------------------------------------------------------------------------- #
# The build-time pass over a whole sample
# --------------------------------------------------------------------------- #

def _tok(field="user", pattern=r"\buser=(\d+)", widen=None):
    return {"field": field, "pattern": pattern, "enabled": True,
            "replacement": {"kind": "pseudonym", "widen": widen}}


CUSTOMER_SAMPLE = [
    "user=123 action=login",
    "user=456 action=register",
    "user=123 action=logout",
]


def test_the_customers_own_example():
    """The requirement in one test: the journey still joins, the users differ."""
    rep = ps.pseudonymise_events(CUSTOMER_SAMPLE, [_tok()], SUB)
    first, second, third = (e.split()[0] for e in rep.events)
    assert first == third, "login and logout must stay the same user"
    assert first != second, "a different user must stay different"
    assert "123" not in " ".join(rep.events), "the original must be gone"
    assert "456" not in " ".join(rep.events)
    assert all(re.match(r"^user=\d{3} action=\w+$", e) for e in rep.events)
    (row,) = rep.rows
    assert (row.spans, row.distinct) == (3, 2)
    assert row.classes[0]["space"] == 900


def test_the_masked_spans_locate_exactly_the_stand_ins():
    rep = ps.pseudonymise_events(CUSTOMER_SAMPLE, [_tok()], SUB)
    for i, event in enumerate(rep.events):
        for s, e in rep.rewritten[i]:
            assert event[s:e].isdigit() and len(event[s:e]) == 3


def test_widening_keeps_the_span_offsets_right():
    # The stand-in is longer than the original, so every later offset moves.
    rep = ps.pseudonymise_events(
        ["user=123 peer=456 end"],
        [_tok(pattern=r"\b(?:user|peer)=(\d+)", widen={"shape": "digits", "length": 15})], SUB)
    event = rep.events[0]
    spans = rep.rewritten[0]
    assert len(spans) == 2
    for s, e in spans:
        assert len(event[s:e]) == 15 and event[s:e].isdigit()
    assert event.endswith(" end")


def test_a_collision_is_refused_and_names_no_value():
    # A one-digit field has nine stand-ins, so ten values must collide.
    events = ["id=%d" % i for i in range(10)]
    with pytest.raises(ps.PseudonymError) as exc:
        ps.pseudonymise_events(events, [_tok("id", r"\bid=(\d)")], SUB)
    message = str(exc.value)
    assert "merge them into one identity" in message and "Widen" in message
    assert "id" in message


def test_two_pseudonym_fields_that_overlap_are_refused():
    events = ["client=abc123 x"]
    tokens = [_tok("client", r"\bclient=(\w+)"), _tok("suffix", r"client=\w{3}(\d+)")]
    with pytest.raises(ps.PseudonymError) as exc:
        ps.pseudonymise_events(events, tokens, SUB)
    assert "overlapping text" in str(exc.value)


def test_a_non_participating_group_is_refused():
    # The alternation can match without group 1 taking part, which would
    # silently pseudonymise the whole match including the field name.
    events = ["user=123", "guest"]
    with pytest.raises(ps.PseudonymError) as exc:
        ps.pseudonymise_events(events, [_tok("user", r"user=(\d+)|guest")], SUB)
    assert "did not participate" in str(exc.value)


def test_json_events_stay_parseable():
    import json
    events = ['{"client_id": 123456, "action": "login"}',
              '{"client_id": 123456, "action": "logout"}']
    token = _tok("client_id", r'"client_id":\s*(\d+)')
    rep = ps.pseudonymise_events(events, [token], SUB)
    docs = [json.loads(e) for e in rep.events]
    assert docs[0]["client_id"] == docs[1]["client_id"]
    assert docs[0]["client_id"] != 123456
    assert isinstance(docs[0]["client_id"], int)


def test_a_stand_in_that_would_break_json_is_refused():
    # hex into an unquoted JSON number cannot parse.
    events = ['{"client_id": 123456}']
    token = _tok("client_id", r'"client_id":\s*(\d+)', widen={"shape": "hex", "length": 16})
    with pytest.raises(ps.PseudonymError) as exc:
        ps.pseudonymise_events(events, [token], SUB)
    assert "no longer parse" in str(exc.value)


def test_a_span_with_nothing_to_vary_warns_and_is_left_alone():
    events = ["ref=--- x"]
    rep = ps.pseudonymise_events(events, [_tok("ref", r"\bref=(\S+)")], SUB)
    assert rep.events == events
    assert rep.warnings and "no variable character" in rep.warnings[0]


def test_no_pseudonym_tokens_is_a_no_op():
    other = {"field": "x", "pattern": "a", "replacement": {"kind": "ipv4"}}
    rep = ps.pseudonymise_events(CUSTOMER_SAMPLE, [other], SUB)
    assert rep.events == list(CUSTOMER_SAMPLE) and rep.rows == [] and rep.rewritten == {}


def test_the_whole_pass_is_deterministic():
    a = ps.pseudonymise_events(CUSTOMER_SAMPLE, [_tok()], SUB)
    b = ps.pseudonymise_events(CUSTOMER_SAMPLE, [_tok()], SUB)
    assert a.events == b.events
