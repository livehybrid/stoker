"""Pack builder: turn a handful of real events into an eventgen pack.

The operator pastes or uploads sample events; :func:`analyse` finds the parts
worth abstracting (timestamps, IPs, GUIDs, users, cities, status codes, sizes
...) and recommends a replacement for each, drawn from the shipped word lists
under ``wordlists/`` where one fits. The operator accepts, edits or adds
fields in the UI; :func:`render_preview` shows the generated events and
:func:`write_pack` writes an ordinary eventgen pack directory (``pack.yaml``,
``default/eventgen.conf``, ``samples/<slug>.sample`` and ``samples/lists/``)
that registers through the same lint path as any uploaded pack. The builder's
own config is kept beside it (``stoker-builder.json``) so the pack can be
reopened and rebuilt.

Semantics follow eventgen, which both worker engines implement: a token is a
regex; group 1 is replaced when the pattern has a group, otherwise the whole
match; one value per token per event is written into every match; tokens apply
in order. Generated patterns avoid constructs outside the common subset of
Python ``re`` and the Rust ``regex`` crate (no lookaround), so they run on the
vendored eventgen and on firebox alike.

Pure module: no database, no settings. The routes in ``server.routes.packbuilder``
own storage and registration.
"""
from __future__ import annotations

import datetime
import ipaddress
import json
import os
import random
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

WORDLIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wordlists")
BUILDER_FILE = "stoker-builder.json"
BUILDER_TAG = "pack-builder"

# Input limits. The analysis is linear in the input, so these bound CPU as well
# as the size of what lands on the control-plane disk.
MAX_EVENTS = 5000
MAX_EVENT_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_TOKENS = 100
MAX_PATTERN_LEN = 1000
MAX_VALUES = 10000
MAX_VALUE_LEN = 4096
ANALYSE_EVENTS = 2000          # events examined for suggestions (the rest ride along)
HIGHLIGHT_EVENTS = 25          # events returned with highlight spans
PREVIEW_MAX = 200

REPLACEMENT_KINDS = ("timestamp", "list", "values", "ipv4", "guid", "mac",
                     "integer", "float", "hex", "static", "sequence")


class BuilderError(ValueError):
    """A request the builder refuses, with an operator-facing reason."""


# --------------------------------------------------------------------------- #
# Word lists
# --------------------------------------------------------------------------- #

_LIST_CACHE = {}  # type: Dict[str, List[str]]


def wordlist_index():
    # type: () -> List[Dict[str, Any]]
    """Shipped lists with title, description, size and a few sample values."""
    with open(os.path.join(WORDLIST_DIR, "index.json"), encoding="utf-8") as fh:
        index = json.load(fh)
    out = []
    for entry in index:
        values = load_wordlist(entry["name"])
        distinct = list(dict.fromkeys(values))
        out.append(dict(entry, count=len(distinct), sample=distinct[:6]))
    return out


def wordlist_names():
    # type: () -> List[str]
    return [e["name"] for e in wordlist_index()]


def load_wordlist(name):
    # type: (str) -> List[str]
    """Values of a shipped list (repeats kept: they are the weighting)."""
    if not re.match(r"^[a-z0-9_]+$", name or ""):
        raise BuilderError("unknown word list %r" % name)
    if name not in _LIST_CACHE:
        path = os.path.join(WORDLIST_DIR, name + ".txt")
        if not os.path.isfile(path):
            raise BuilderError("unknown word list %r" % name)
        with open(path, encoding="utf-8") as fh:
            _LIST_CACHE[name] = [line.rstrip("\n") for line in fh if line.strip()]
    return _LIST_CACHE[name]


def _list_set(name):
    # type: (str) -> set
    return {v.lower() for v in load_wordlist(name)}


# --------------------------------------------------------------------------- #
# Event input
# --------------------------------------------------------------------------- #

def split_events(text):
    # type: (str) -> List[str]
    """One event per non-blank line; a JSON array becomes one compact line per
    element (objects serialised with their key order kept)."""
    if text is None:
        raise BuilderError("no events supplied")
    if len(text.encode("utf-8", errors="replace")) > MAX_TOTAL_BYTES:
        raise BuilderError("sample is larger than %d MB" % (MAX_TOTAL_BYTES // (1024 * 1024)))
    text = text.lstrip("﻿")
    stripped = text.strip()
    events = []  # type: List[str]
    if stripped.startswith("["):
        try:
            doc = json.loads(stripped)
        except ValueError:
            doc = None
        if isinstance(doc, list):
            for item in doc:
                if isinstance(item, str):
                    events.append(item)
                else:
                    events.append(json.dumps(item, separators=(",", ":"), ensure_ascii=False))
    if not events:
        events = [line.rstrip("\r") for line in text.split("\n") if line.strip()]
    if not events:
        raise BuilderError("no events found: paste one event per line")
    if len(events) > MAX_EVENTS:
        raise BuilderError("too many events (%d, max %d)" % (len(events), MAX_EVENTS))
    for i, ev in enumerate(events):
        if len(ev.encode("utf-8", errors="replace")) > MAX_EVENT_BYTES:
            raise BuilderError("event %d is larger than %d KB" % (i + 1, MAX_EVENT_BYTES // 1024))
    return events


# --------------------------------------------------------------------------- #
# Value classification
# --------------------------------------------------------------------------- #

_IPV4 = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_MAC = r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"
_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
_IPV4_RE = re.compile(r"^%s$" % _IPV4)
_GUID_RE = re.compile(r"^%s$" % _GUID)
_MAC_RE = re.compile(r"^%s$" % _MAC)
_EMAIL_RE = re.compile(r"^%s$" % _EMAIL)
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
_HEX_RE = re.compile(r"^(?=.*[a-fA-F])[0-9a-fA-F]{8,}$")


def _is_ipv4(v):
    # type: (str) -> bool
    if not _IPV4_RE.match(v):
        return False
    return all(0 <= int(p) <= 255 for p in v.split("."))


def _is_ipv6(v):
    # type: (str) -> bool
    if ":" not in v or v.count(":") < 2:
        return False
    try:
        ipaddress.IPv6Address(v)
        return True
    except ValueError:
        return False


def classify(values):
    # type: (Sequence[str]) -> str
    """The shape every observed value shares: ipv4, ipv6, mac, guid, email,
    epoch, epoch_ms, int, float, hex, bool, or string."""
    vals = [v for v in values if v != ""]
    if not vals:
        return "string"

    def all_(pred):
        return all(pred(v) for v in vals)

    if all_(_is_ipv4):
        return "ipv4"
    if all_(lambda v: bool(_GUID_RE.match(v))):
        return "guid"
    if all_(lambda v: bool(_MAC_RE.match(v))):
        return "mac"
    if all_(_is_ipv6):
        return "ipv6"
    if all_(lambda v: bool(_EMAIL_RE.match(v))):
        return "email"
    if all_(lambda v: v.lower() in ("true", "false")):
        return "bool"
    if all_(lambda v: bool(_INT_RE.match(v))):
        if all_(lambda v: len(v) == 10 and 1000000000 <= int(v) <= 2200000000):
            return "epoch"
        if all_(lambda v: len(v) == 13 and 1000000000000 <= int(v) <= 2200000000000):
            return "epoch_ms"
        return "int"
    if all_(lambda v: bool(_FLOAT_RE.match(v)) or bool(_INT_RE.match(v))) and \
            any(_FLOAT_RE.match(v) for v in vals):
        return "float"
    if all_(lambda v: bool(_HEX_RE.match(v))):
        return "hex"
    return "string"


# Key-name hints: first match wins. Each entry is (substrings, list).
_KEY_LISTS = [
    (("useragent", "user_agent", "http_user_agent"), "user_agents"),
    (("email", "mail"), "emails"),
    (("username", "user_name", "userid", "user_id", "account", "login", "actor",
      "principal", "src_user", "dest_user", "user"), "usernames"),
    (("firstname", "first_name", "givenname", "given_name"), "first_names"),
    (("lastname", "last_name", "surname", "familyname"), "last_names"),
    (("fullname", "full_name", "displayname", "display_name"), "full_names"),
    (("city",), "cities"),
    (("countrycode", "country_code"), "country_codes"),
    (("country",), "countries"),
    (("department", "dept"), "departments"),
    (("workstation",), "workstations"),
    (("hostname", "host", "computer", "device", "dvc", "server", "node"), "hostnames"),
    (("http_method", "method", "verb"), "http_methods"),
    (("uri", "url", "path", "request"), "uri_paths"),
    (("process", "image", "exe", "command"), "process_names"),
    (("severity", "sev", "priority"), "severities"),
    (("loglevel", "log_level", "level"), "log_levels"),
    (("region",), "aws_regions"),
    (("protocol", "proto", "transport"), "protocols"),
    (("action", "disposition"), "actions"),
    (("outcome", "result"), "auth_results"),
    (("extension", "ext"), "file_extensions"),
]

# Lists whose membership is checked against observed string values.
_MEMBERSHIP_LISTS = ("cities", "countries", "country_codes", "first_names", "last_names",
                     "full_names", "usernames", "departments", "http_methods",
                     "log_levels", "severities", "actions", "aws_regions", "protocols",
                     "process_names", "auth_results")


def _norm_key(key):
    # type: (str) -> str
    return re.sub(r"[^a-z0-9_]", "", key.lower().replace("-", "_").replace(".", "_"))


def _key_list(key):
    # type: (str) -> Optional[str]
    k = _norm_key(key)
    compact = k.replace("_", "")
    for needles, name in _KEY_LISTS:
        for n in needles:
            if n in k or n.replace("_", "") in compact:
                return name
    return None


def _membership_list(values):
    # type: (Sequence[str]) -> Optional[str]
    distinct = {v.lower() for v in values if v}
    if not distinct:
        return None
    best, best_score = None, 0.0
    for name in _MEMBERSHIP_LISTS:
        score = len(distinct & _list_set(name)) / float(len(distinct))
        if score > best_score:
            best, best_score = name, score
    return best if best_score >= 0.6 else None


_RFC1918 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def _all_private(values):
    # type: (Sequence[str]) -> bool
    """RFC 1918 only: Python's ``is_private`` also counts the documentation
    ranges, which is exactly what sample data uses for public addresses."""
    try:
        return all(any(ipaddress.IPv4Address(v) in net for net in _RFC1918) for v in values)
    except ValueError:
        return False


def _decimals(values):
    # type: (Sequence[str]) -> int
    return max((len(v.split(".", 1)[1]) for v in values if "." in v), default=0)


def recommend(key, values):
    # type: (str, Sequence[str]) -> Tuple[Dict[str, Any], bool, str]
    """(replacement, enabled, why) for a field named ``key`` with these values."""
    distinct = list(dict.fromkeys(v for v in values))
    shape = classify(distinct)
    k = _norm_key(key)
    constant = len(distinct) <= 1
    if shape == "ipv4":
        if _all_private(distinct):
            return {"kind": "list", "list": "internal_ips"}, True, "private IPv4 addresses"
        return {"kind": "ipv4"}, True, "IPv4 addresses"
    if shape == "guid":
        return {"kind": "guid"}, True, "GUIDs"
    if shape == "mac":
        return {"kind": "mac"}, True, "MAC addresses"
    if shape == "ipv6":
        return {"kind": "values", "values": distinct}, not constant, "IPv6 addresses"
    if shape == "email":
        return {"kind": "list", "list": "emails"}, True, "email addresses"
    if shape == "epoch":
        return {"kind": "timestamp", "format": "%s"}, True, "epoch seconds"
    if shape == "epoch_ms":
        return {"kind": "timestamp", "format": "%s000"}, True, "epoch milliseconds"
    if shape == "bool":
        return {"kind": "values", "values": ["true", "false"]}, False, "booleans"
    if shape == "int":
        nums = [int(v) for v in distinct]
        lo, hi = min(nums), max(nums)
        if "port" in k and any(s in k for s in ("src", "source", "sport", "client")):
            return {"kind": "integer", "min": 1024, "max": 65535}, True, "source ports"
        if "port" in k:
            return {"kind": "list", "list": "ports"}, True, "ports"
        if k in ("pid", "ppid") or k.endswith("_pid") or "processid" in k.replace("_", ""):
            return {"kind": "integer", "min": 1000, "max": 65000}, True, "process ids"
        if any(s in k for s in ("status", "code", "response")) and all(100 <= n <= 599 for n in nums):
            return {"kind": "list", "list": "http_status"}, True, "HTTP status codes"
        if constant:
            return {"kind": "integer", "min": lo, "max": hi}, False, "a constant number"
        span = hi - lo
        return ({"kind": "integer", "min": max(0, lo - span // 2) if lo >= 0 else lo - span // 2,
                 "max": hi + span // 2}, True, "numbers %d to %d" % (lo, hi))
    if shape == "float":
        nums = [float(v) for v in distinct]
        lo, hi = min(nums), max(nums)
        d = _decimals(distinct) or 1
        return ({"kind": "float", "min": lo, "max": hi, "decimals": d}, not constant,
                "decimals %s to %s" % (lo, hi))
    if shape == "hex":
        length = max(len(v) for v in distinct)
        return {"kind": "hex", "length": length}, not constant, "hex strings"
    # strings
    listed = _membership_list(distinct)
    if listed:
        return {"kind": "list", "list": listed}, True, "values found in the %s list" % listed
    hinted = _key_list(key)
    if hinted:
        if hinted == "hostnames" and all(v.upper().startswith("WS") for v in distinct):
            hinted = "workstations"
        return {"kind": "list", "list": hinted}, True, "field name suggests %s" % hinted
    return ({"kind": "values", "values": distinct}, not constant,
            "a constant value" if constant else "%d distinct values" % len(distinct))


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

_TIMESTAMPS = [
    (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "%Y-%m-%dT%H:%M:%S", "ISO 8601 timestamp"),
    (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "%Y-%m-%d %H:%M:%S", "date-time"),
    (r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", "%Y/%m/%d %H:%M:%S", "date-time"),
    (r"\d{2}/(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)/\d{4}:\d{2}:\d{2}:\d{2}",
     "%d/%b/%Y:%H:%M:%S", "access-log timestamp"),
    (r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{2} \d{4} \d{2}:\d{2}:\d{2}",
     "%a %b %d %Y %H:%M:%S", "day-date-time"),
    (r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) {1,2}\d{1,2} \d{2}:\d{2}:\d{2}",
     "%b %e %H:%M:%S", "syslog timestamp"),
    (r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", "%m/%d/%Y %H:%M:%S", "US date-time"),
]

_JSON_KV = re.compile(r'"([A-Za-z0-9_.@$-]{1,64})"\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?)')
_KV = re.compile(r'(?:^|(?<=[\s,;|&?\[(]))([A-Za-z_][A-Za-z0-9_.-]{0,40})=("[^"]*"|\'[^\']*\'|[^\s,;&"\'\])]+)')
_BARE = [
    ("guid", _GUID, "GUID"),
    ("email", _EMAIL, "email address"),
    ("mac", _MAC, "MAC address"),
    ("ipv4", _IPV4, "IPv4 address"),
]
_ACCESS = [
    ("http_method", r'"(GET|POST|PUT|DELETE|HEAD|PATCH|OPTIONS) ', "HTTP method",
     {"kind": "list", "list": "http_methods"}),
    ("uri_path", r'"(?:GET|POST|PUT|DELETE|HEAD|PATCH|OPTIONS) (\S+) HTTP', "URI path",
     {"kind": "list", "list": "uri_paths"}),
    ("http_status", r'HTTP/\d(?:\.\d)?" (\d{3})', "HTTP status", {"kind": "list", "list": "http_status"}),
    ("bytes", r'HTTP/\d(?:\.\d)?" \d{3} (\d+)', "response bytes", None),
    ("user_agent", r'" "([^"]*)"\s*$', "user agent", {"kind": "list", "list": "user_agents"}),
]
_PID = r"\w\[(\d+)\]"
# Common free-text phrases (syslog, sshd, firewalls): (field, pattern, key hint).
_PHRASES = [
    ("syslog host",
     r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) {1,2}\d{1,2} \d{2}:\d{2}:\d{2} (\S+)", "hostname"),
    ("user (for ... from)", r"\bfor (?:invalid user )?([A-Za-z0-9_.@-]+) from\b", "user"),
    ("port", r"\bport (\d{1,5})\b", "src_port"),
]


class _Claims(object):
    """Character spans already taken, per event, so detectors never overlap."""

    def __init__(self, n):
        self.spans = [[] for _ in range(n)]  # type: List[List[Tuple[int, int]]]

    def free(self, i, s, e):
        # type: (int, int, int) -> bool
        return all(e <= a or s >= b for a, b in self.spans[i])

    def take(self, i, s, e):
        # type: (int, int, int) -> None
        self.spans[i].append((s, e))


def _target(m):
    # type: (re.Match) -> Tuple[int, int]
    if m.re.groups >= 1 and m.group(1) is not None:
        return m.start(1), m.end(1)
    return m.start(0), m.end(0)


def _slug(text):
    # type: (str) -> str
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:48] or "field"


def _anchor(event, start):
    # type: (str, int) -> Tuple[str, str]
    """(regex, label) anchoring a bare value on the fixed text just before it.

    Distinguishes "from 10.0.0.1" and "to 10.0.0.2" so each gets its own
    token (one token writes one value into all its matches). The literal is
    the tail of the previous whitespace-separated chunk with digits removed,
    so a variable prefix (a number, a port) never becomes part of the anchor.
    """
    if start == 0:
        return "^", "at line start"
    prefix = event[:start]
    m = re.search(r"(\S*)(\s*)$", prefix)
    chunk, ws = m.group(1), m.group(2)
    tail = re.search(r"[^\d]*$", chunk).group(0)[-24:]
    if not tail and not ws:
        return "", ""
    if not tail and ws and m.start(2) == 0:
        return r"^\s*", "at line start"
    if not tail:
        # preceded by a number and whitespace: too variable to anchor on
        return "", ""
    regex = re.escape(tail) + (r"\s+" if ws else "")
    return regex, "after '%s'" % tail.strip()


def analyse(events):
    # type: (List[str]) -> Dict[str, Any]
    """Suggest the fields to abstract. Returns ``{"suggestions": [...],
    "highlights": [...], "events": n}``; every suggestion is a ready-to-use
    builder token (``pattern`` + ``replacement``) plus ``examples``,
    ``matches`` (events matched), ``kind`` and a short ``why``."""
    sample = events[:ANALYSE_EVENTS]
    n = len(sample)
    claims = _Claims(n)
    found = []  # type: List[Dict[str, Any]]

    def add(field, kind, pattern, replacement, enabled, why):
        compiled = re.compile(pattern)
        matches, examples = 0, []  # type: int, List[str]
        for i, ev in enumerate(sample):
            hit = False
            for m in compiled.finditer(ev):
                s, e = _target(m)
                if e <= s:
                    continue
                hit = True
                claims.take(i, s, e)
                if len(examples) < 5 and ev[s:e] not in examples:
                    examples.append(ev[s:e])
            matches += 1 if hit else 0
        if not matches:
            return
        found.append({
            "id": "f%d_%s" % (len(found) + 1, _slug(field)),
            "field": field, "kind": kind, "pattern": pattern,
            "replacement": replacement, "enabled": enabled, "why": why,
            "examples": examples, "matches": matches,
        })

    # 1. Timestamps (whole match; the fraction and zone, if any, stay literal).
    for regex, fmt, label in _TIMESTAMPS:
        compiled = re.compile(regex)
        if any(_free_match(compiled, ev, claims, i) for i, ev in enumerate(sample)):
            add(label, "timestamp", regex, {"kind": "timestamp", "format": fmt}, True, "timestamp")

    # 2. JSON key/values, then key=value pairs: grouped by key across events.
    for kind_name, rx in (("json", _JSON_KV), ("kv", _KV)):
        by_key = {}  # type: Dict[Tuple[str, str], List[str]]
        order = []  # type: List[Tuple[str, str]]
        for i, ev in enumerate(sample):
            for m in rx.finditer(ev):
                key, raw = m.group(1), m.group(2)
                quote = raw[0] if raw[:1] in ('"', "'") else ""
                vs, ve = m.start(2) + (1 if quote else 0), m.end(2) - (1 if quote else 0)
                if ve <= vs or not claims.free(i, vs, ve):
                    continue
                ident = (key, quote)
                if ident not in by_key:
                    by_key[ident] = []
                    order.append(ident)
                by_key[ident].append(ev[vs:ve])
        for key, quote in order:
            values = by_key[(key, quote)]
            replacement, enabled, why = recommend(key, values)
            ek = re.escape(key)
            if kind_name == "json":
                pattern = ('"%s"\\s*:\\s*"([^"]*)"' % ek) if quote else ('"%s"\\s*:\\s*(-?\\d+(?:\\.\\d+)?)' % ek)
            elif quote == '"':
                pattern = r'\b%s="([^"]*)"' % ek
            elif quote == "'":
                pattern = r"\b%s='([^']*)'" % ek
            else:
                pattern = r"\b%s=([^\s,;&\"'\])]+)" % ek
            add(key, kind_name, pattern, replacement, enabled, why)

    # 3. Access-log (NCSA combined/common) fields.
    for field, regex, label, repl in _ACCESS:
        compiled = re.compile(regex)
        values = []  # type: List[str]
        for i, ev in enumerate(sample):
            for m in compiled.finditer(ev):
                s, e = _target(m)
                if claims.free(i, s, e):
                    values.append(ev[s:e])
        if not values:
            continue
        replacement = repl
        enabled, why = True, label
        if replacement is None:
            replacement, enabled, why = recommend(field, values)
        if replacement.get("kind") == "list" and field == "uri_path" and _membership_list(values) is None:
            # site-specific paths: keep them, they are more realistic than generic ones
            replacement = {"kind": "values", "values": list(dict.fromkeys(values))}
            why = "%d distinct paths from the sample" % len(set(values))
        add(label, "access", regex, replacement, enabled, why)

    # 4. Bare values, anchored on the text before them.
    for kind, value_rx, label in _BARE:
        compiled = re.compile(r"\b(%s)\b" % value_rx if kind != "email" else r"(%s)" % value_rx)
        groups = {}  # type: Dict[str, Dict[str, Any]]
        order = []  # type: List[str]
        for i, ev in enumerate(sample):
            for m in compiled.finditer(ev):
                s, e = m.start(1), m.end(1)
                value = ev[s:e]
                if kind == "ipv4" and not _is_ipv4(value):
                    continue
                if not claims.free(i, s, e):
                    continue
                anchor, where = _anchor(ev, s)
                if anchor not in groups:
                    groups[anchor] = {"where": where, "values": []}
                    order.append(anchor)
                groups[anchor]["values"].append(value)
        for anchor in order:
            g = groups[anchor]
            replacement, enabled, why = recommend(kind, g["values"])
            pattern = "%s(%s)" % (anchor, value_rx) if anchor else r"\b(%s)\b" % value_rx
            field = "%s %s" % (label, g["where"]) if g["where"] else label
            add(field, "bare", pattern, replacement, enabled, why)

    # 5. Common free-text phrases.
    for field, regex, hint in _PHRASES:
        compiled = re.compile(regex)
        values = []  # type: List[str]
        for i, ev in enumerate(sample):
            for m in compiled.finditer(ev):
                s, e = _target(m)
                if claims.free(i, s, e):
                    values.append(ev[s:e])
        if values:
            replacement, enabled, why = recommend(hint, values)
            add(field, "phrase", regex, replacement, enabled, why)

    # 6. Process ids like sshd[1234].
    pid_rx = re.compile(_PID)
    if any(_free_match(pid_rx, ev, claims, i) for i, ev in enumerate(sample)):
        add("process id", "bare", _PID, {"kind": "integer", "min": 1000, "max": 65000}, True, "process ids")

    # 7. Names and places in free text, matched against the shipped lists.
    for list_name, label, default_on in (("cities", "city", True), ("countries", "country", True),
                                         ("first_names", "first name", False)):
        values = load_wordlist(list_name)
        alternation = "|".join(re.escape(v) for v in sorted(set(values), key=len, reverse=True))
        compiled = re.compile(r"\b(?:%s)\b" % alternation)
        seen = []  # type: List[str]
        for i, ev in enumerate(sample):
            for m in compiled.finditer(ev):
                if claims.free(i, m.start(), m.end()) and m.group(0) not in seen:
                    seen.append(m.group(0))
        if seen:
            pattern = r"\b(?:%s)\b" % "|".join(re.escape(v) for v in sorted(seen, key=len, reverse=True))
            add("%s names" % label, "wordlist", pattern, {"kind": "list", "list": list_name}, default_on,
                "found in the %s list" % list_name)

    return {"events": len(events), "suggestions": found,
            "highlights": highlight(events[:HIGHLIGHT_EVENTS], found)}


def _free_match(compiled, ev, claims, i):
    # type: (re.Pattern, str, _Claims, int) -> bool
    for m in compiled.finditer(ev):
        s, e = _target(m)
        if claims.free(i, s, e):
            return True
    return False


def highlight(events, tokens):
    # type: (List[str], Iterable[Dict[str, Any]]) -> List[List[List[Any]]]
    """Per event, ``[start, end, token_id]`` spans each token would rewrite
    (first token wins where two overlap). Invalid patterns contribute nothing."""
    compiled = []
    for t in tokens:
        try:
            compiled.append((t.get("id"), re.compile(t["pattern"])))
        except (re.error, KeyError, TypeError):
            continue
    out = []
    for ev in events:
        spans = []  # type: List[List[Any]]
        for tid, rx in compiled:
            for m in rx.finditer(ev):
                s, e = _target(m)
                if e > s and all(e <= a or s >= b for a, b, _ in spans):
                    spans.append([s, e, tid])
        spans.sort()
        out.append(spans)
    return out


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")


def validate_config(cfg):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    """Normalise and check a builder config; raises BuilderError."""
    name = str(cfg.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise BuilderError("name must start with a letter or digit and use letters, digits, "
                           "spaces, '.', '_' or '-' (max 64)")
    events = cfg.get("events") or []
    if not isinstance(events, list) or not events:
        raise BuilderError("at least one event is required")
    if len(events) > MAX_EVENTS:
        raise BuilderError("too many events (%d, max %d)" % (len(events), MAX_EVENTS))
    total = 0
    clean_events = []
    for i, ev in enumerate(events):
        if not isinstance(ev, str) or not ev.strip():
            raise BuilderError("event %d is empty" % (i + 1))
        if "\n" in ev or "\r" in ev:
            raise BuilderError("event %d spans several lines; the builder takes one event per line" % (i + 1))
        size = len(ev.encode("utf-8"))
        if size > MAX_EVENT_BYTES:
            raise BuilderError("event %d is larger than %d KB" % (i + 1, MAX_EVENT_BYTES // 1024))
        total += size
        clean_events.append(ev)
    if total > MAX_TOTAL_BYTES:
        raise BuilderError("events exceed %d MB in total" % (MAX_TOTAL_BYTES // (1024 * 1024)))
    tokens = cfg.get("tokens") or []
    if len(tokens) > MAX_TOKENS:
        raise BuilderError("too many fields (%d, max %d)" % (len(tokens), MAX_TOKENS))
    known_lists = set(wordlist_names())
    clean_tokens = []
    for i, t in enumerate(tokens):
        label = str(t.get("field") or "field %d" % (i + 1))
        pattern = t.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise BuilderError("%s: a pattern is required" % label)
        if len(pattern) > MAX_PATTERN_LEN:
            raise BuilderError("%s: pattern longer than %d characters" % (label, MAX_PATTERN_LEN))
        if "\n" in pattern or "\r" in pattern:
            raise BuilderError("%s: pattern must be one line" % label)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise BuilderError("%s: invalid regular expression (%s)" % (label, exc))
        rep = _validate_replacement(label, t.get("replacement") or {}, known_lists)
        clean_tokens.append({"id": t.get("id") or "t%d" % (i + 1), "field": label[:80],
                             "pattern": pattern, "enabled": bool(t.get("enabled", True)),
                             "replacement": rep})
    count = _int_in(cfg.get("count", 10), 1, 100000, "count")
    interval = _int_in(cfg.get("interval", 1), 1, 86400, "interval")
    order = cfg.get("order") or "sequential"
    if order not in ("sequential", "random"):
        raise BuilderError("order must be sequential or random")
    tags = [str(x).strip()[:40] for x in (cfg.get("tags") or []) if str(x).strip()][:20]
    return {
        "name": name,
        "description": str(cfg.get("description") or "").strip()[:1000],
        "sourcetype": (str(cfg.get("sourcetype") or "").strip()[:128] or None),
        "tags": tags,
        "events": clean_events,
        "tokens": clean_tokens,
        "count": count,
        "interval": interval,
        "order": order,
    }


def _int_in(v, lo, hi, what):
    # type: (Any, int, int, str) -> int
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BuilderError("%s must be a whole number" % what)
    if not lo <= n <= hi:
        raise BuilderError("%s must be between %d and %d" % (what, lo, hi))
    return n


def _validate_replacement(label, rep, known_lists):
    # type: (str, Dict[str, Any], set) -> Dict[str, Any]
    kind = rep.get("kind")
    if kind not in REPLACEMENT_KINDS:
        raise BuilderError("%s: unknown replacement %r" % (label, kind))
    if kind == "timestamp":
        fmt = str(rep.get("format") or "")
        if "%" not in fmt or "\n" in fmt:
            raise BuilderError("%s: a timestamp needs a strftime format such as %%Y-%%m-%%d" % label)
        return {"kind": kind, "format": fmt}
    if kind == "list":
        name = rep.get("list")
        if name not in known_lists:
            raise BuilderError("%s: unknown word list %r" % (label, name))
        return {"kind": kind, "list": name}
    if kind == "values":
        values = rep.get("values") or []
        if not isinstance(values, list) or not values:
            raise BuilderError("%s: give at least one value" % label)
        if len(values) > MAX_VALUES:
            raise BuilderError("%s: at most %d values" % (label, MAX_VALUES))
        clean = []
        for v in values:
            v = str(v)
            if "\n" in v or "\r" in v or len(v) > MAX_VALUE_LEN:
                raise BuilderError("%s: values must be single lines under %d characters" % (label, MAX_VALUE_LEN))
            if v.strip():
                clean.append(v.strip())
        if not clean:
            raise BuilderError("%s: give at least one non-blank value" % label)
        return {"kind": kind, "values": clean}
    if kind == "integer":
        lo = _int_in(rep.get("min", 0), -10 ** 15, 10 ** 15, "%s minimum" % label)
        hi = _int_in(rep.get("max", 100), -10 ** 15, 10 ** 15, "%s maximum" % label)
        if hi < lo:
            raise BuilderError("%s: maximum is below the minimum" % label)
        return {"kind": kind, "min": lo, "max": hi}
    if kind == "float":
        try:
            lo, hi = float(rep.get("min", 0)), float(rep.get("max", 1))
        except (TypeError, ValueError):
            raise BuilderError("%s: minimum and maximum must be numbers" % label)
        if hi < lo:
            raise BuilderError("%s: maximum is below the minimum" % label)
        d = _int_in(rep.get("decimals", 2), 0, 9, "%s decimals" % label)
        return {"kind": kind, "min": lo, "max": hi, "decimals": d}
    if kind == "hex":
        return {"kind": kind, "length": _int_in(rep.get("length", 16), 1, 256, "%s length" % label)}
    if kind == "static":
        value = str(rep.get("value") if rep.get("value") is not None else "")
        if "\n" in value or "\r" in value:
            raise BuilderError("%s: the static value must be one line" % label)
        return {"kind": kind, "value": value}
    if kind == "sequence":
        return {"kind": kind, "start": _int_in(rep.get("start", 1), 0, 10 ** 15, "%s start" % label)}
    return {"kind": kind}


# --------------------------------------------------------------------------- #
# Token order and eventgen mapping
# --------------------------------------------------------------------------- #

_ORDER = {"timestamp": 0, "ipv4": 1, "guid": 1, "mac": 1, "integer": 1, "float": 1,
          "hex": 1, "sequence": 1, "list": 2, "values": 2, "static": 2}


def ordered_tokens(cfg):
    # type: (Dict[str, Any]) -> List[Dict[str, Any]]
    """Enabled tokens in apply order: timestamps, then generated values, then
    list/value/static replacements. Free text goes in last so no earlier
    pattern can match inside an inserted list value (an IPv4 pattern inside
    "Chrome/128.0.0.0", say). Stable within each group."""
    enabled = [t for t in cfg["tokens"] if t.get("enabled", True)]
    return sorted(enabled, key=lambda t: _ORDER.get(t["replacement"]["kind"], 3))


def _slugify(text):
    # type: (str) -> str
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "pack"


def _values_file(token, used):
    # type: (Dict[str, Any], set) -> str
    base = _slugify(token.get("field") or "values") + "-values"
    name, n = base, 2
    while name in used:
        name = "%s-%d" % (base, n)
        n += 1
    used.add(name)
    return name


def _conf_value(pattern):
    # type: (str) -> str
    """configparser strips surrounding whitespace from values: protect it."""
    lead = len(pattern) - len(pattern.lstrip(" "))
    trail = len(pattern) - len(pattern.rstrip(" "))
    core = pattern.strip(" ")
    return "[ ]" * lead + core + "[ ]" * trail


def eventgen_tokens(cfg):
    # type: (Dict[str, Any]) -> Tuple[List[Tuple[str, str, str]], Dict[str, List[str]]]
    """``(pattern, replacementType, replacement)`` in apply order, plus the list
    files the pack must carry (``samples/lists/<name>.sample`` -> values)."""
    out = []  # type: List[Tuple[str, str, str]]
    files = {}  # type: Dict[str, List[str]]
    used = set()  # type: set
    for t in ordered_tokens(cfg):
        rep = t["replacement"]
        kind = rep["kind"]
        pattern = _conf_value(t["pattern"])
        if kind == "timestamp":
            out.append((pattern, "timestamp", rep["format"]))
        elif kind == "list":
            files[rep["list"]] = load_wordlist(rep["list"])
            used.add(rep["list"])
            out.append((pattern, "file", "samples/lists/%s.sample" % rep["list"]))
        elif kind == "values":
            fname = _values_file(t, used)
            files[fname] = rep["values"]
            out.append((pattern, "file", "samples/lists/%s.sample" % fname))
        elif kind == "ipv4":
            out.append((pattern, "random", "ipv4"))
        elif kind == "guid":
            out.append((pattern, "random", "guid"))
        elif kind == "mac":
            out.append((pattern, "random", "mac"))
        elif kind == "integer":
            out.append((pattern, "random", "integer[%d:%d]" % (rep["min"], rep["max"])))
        elif kind == "float":
            d = rep["decimals"]
            out.append((pattern, "random", "float[%.*f:%.*f]" % (d, rep["min"], d, rep["max"])))
        elif kind == "hex":
            out.append((pattern, "random", "hex(%d)" % rep["length"]))
        elif kind == "static":
            out.append((pattern, "static", rep["value"]))
        elif kind == "sequence":
            out.append((pattern, "integerid", str(rep["start"])))
    return out, files


# --------------------------------------------------------------------------- #
# Writing the pack
# --------------------------------------------------------------------------- #

def _yaml_quote(text):
    # type: (str) -> str
    return '"%s"' % text.replace("\\", "/").replace('"', "'").replace("\n", " ")


def write_pack(cfg, dest):
    # type: (Dict[str, Any], str) -> str
    """Write the pack for a validated config into the new directory ``dest``."""
    slug = _slugify(cfg["name"])
    os.makedirs(os.path.join(dest, "default"))
    os.makedirs(os.path.join(dest, "samples", "lists"))
    with open(os.path.join(dest, "samples", "%s.sample" % slug), "w", encoding="utf-8") as fh:
        fh.write("\n".join(cfg["events"]) + "\n")
    tokens, files = eventgen_tokens(cfg)
    for fname, values in files.items():
        with open(os.path.join(dest, "samples", "lists", "%s.sample" % fname), "w", encoding="utf-8") as fh:
            fh.write("\n".join(values) + "\n")

    lines = [
        "# Generated by the Stoker pack builder. Reopen the pack in the builder",
        "# (Packs > Edit in builder) to change it; hand edits are overwritten there.",
        "",
        "[%s.sample]" % slug,
        "mode = sample",
        "interval = %d" % cfg["interval"],
        "count = %d" % cfg["count"],
        "earliest = -%ds" % cfg["interval"],
        "latest = now",
    ]
    if cfg["order"] == "random":
        lines.append("randomizeEvents = true")
    for i, (pattern, rtype, replacement) in enumerate(tokens):
        lines += ["",
                  "token.%d.token = %s" % (i, pattern),
                  "token.%d.replacementType = %s" % (i, rtype),
                  "token.%d.replacement = %s" % (i, replacement)]
    with open(os.path.join(dest, "default", "eventgen.conf"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    sizes = [len(e.encode("utf-8")) for e in cfg["events"]]
    yaml = [
        "# Stoker pack generated by the pack builder.",
        "name: %s" % cfg["name"],
        "tags: %s" % ", ".join([BUILDER_TAG] + [t for t in cfg["tags"] if t != BUILDER_TAG]),
        "engine: eventgen",
        "description: %s" % _yaml_quote(cfg["description"] or "Generated by the pack builder from %d sample event(s)"
                                        % len(cfg["events"])),
        "estimates:",
        "  bytes_per_event: %d" % max(1, round(sum(sizes) / float(len(sizes)))),
    ]
    if cfg.get("sourcetype"):
        yaml += ["defaults:", "  sourcetype: %s" % cfg["sourcetype"]]
    with open(os.path.join(dest, "pack.yaml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(yaml) + "\n")
    with open(os.path.join(dest, BUILDER_FILE), "w", encoding="utf-8") as fh:
        json.dump(dict(cfg, builder_version=1), fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    return dest


def read_builder_config(pack_dir):
    # type: (str) -> Optional[Dict[str, Any]]
    path = os.path.join(pack_dir, BUILDER_FILE)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    doc.pop("builder_version", None)
    return doc


# --------------------------------------------------------------------------- #
# Preview (the same semantics the engines apply)
# --------------------------------------------------------------------------- #

def _strftime(fmt, epoch):
    # type: (str, float) -> str
    dt = datetime.datetime.fromtimestamp(epoch)
    # %s is not portable in Python's strftime; both engines write the epoch.
    fmt = fmt.replace("%s", str(int(epoch)))
    try:
        return dt.strftime(fmt)
    except ValueError:
        return fmt


def _value(rep, rng, epoch, state):
    # type: (Dict[str, Any], random.Random, float, Dict[str, int]) -> str
    kind = rep["kind"]
    if kind == "timestamp":
        return _strftime(rep["format"], epoch)
    if kind == "list":
        return rng.choice(load_wordlist(rep["list"]))
    if kind == "values":
        return rng.choice(rep["values"])
    if kind == "ipv4":
        return ".".join(str(rng.randint(0, 255)) for _ in range(4))
    if kind == "guid":
        return "%08x-%04x-4%03x-%04x-%012x" % (rng.getrandbits(32), rng.getrandbits(16), rng.getrandbits(12),
                                             0x8000 | rng.getrandbits(14), rng.getrandbits(48))
    if kind == "mac":
        return ":".join("%02x" % rng.randint(0, 255) for _ in range(6))
    if kind == "integer":
        return str(rng.randint(rep["min"], rep["max"]))
    if kind == "float":
        v = round(rng.uniform(rep["min"], rep["max"]), rep["decimals"])
        return "%.*f" % (rep["decimals"], v)
    if kind == "hex":
        return "".join(rng.choice("0123456789ABCDEF") for _ in range(rep["length"]))
    if kind == "static":
        return rep["value"]
    if kind == "sequence":
        state.setdefault("next", rep["start"])
        v = state["next"]
        state["next"] += 1
        return str(v)
    return ""


def render_preview(cfg, n=20, seed=None):
    # type: (Dict[str, Any], int, Optional[int]) -> Dict[str, Any]
    """Render ``n`` events from a validated config without touching disk.

    Returns ``{"events": [...], "warnings": [...], "bytes_per_event": float}``.
    A warning names every enabled field whose pattern matches no sample event
    (it would never fire) and every field that matches inside another field's
    inserted text.
    """
    n = max(1, min(int(n), PREVIEW_MAX))
    rng = random.Random(seed)
    tokens = ordered_tokens(cfg)
    compiled = [(t, re.compile(t["pattern"])) for t in tokens]
    events = cfg["events"]
    warnings = []
    for t, rx in compiled:
        if not any(rx.search(ev) for ev in events):
            warnings.append("%s: the pattern matches none of the sample events" % t["field"])
    now = time.time()
    state = {}  # type: Dict[str, Dict[str, int]]
    out = []
    for i in range(n):
        base = rng.choice(events) if cfg.get("order") == "random" else events[i % len(events)]
        epoch = now - rng.uniform(0, cfg.get("interval", 1))
        text = base
        for t, rx in compiled:
            matches = list(rx.finditer(text))
            if not matches:
                continue
            value = _value(t["replacement"], rng, epoch, state.setdefault(t["id"], {}))
            pieces, pos = [], 0
            for m in matches:
                s, e = _target(m)
                if s < pos:
                    continue
                pieces.append(text[pos:s])
                pieces.append(value)
                pos = e
            pieces.append(text[pos:])
            text = "".join(pieces)
        out.append(text)
    size = sum(len(e.encode("utf-8")) for e in out) / float(len(out))
    return {"events": out, "warnings": warnings, "bytes_per_event": round(size, 1)}


__all__ = [
    "BuilderError", "BUILDER_FILE", "BUILDER_TAG", "analyse", "classify", "highlight",
    "load_wordlist", "ordered_tokens", "eventgen_tokens", "read_builder_config",
    "recommend", "render_preview", "split_events", "validate_config", "wordlist_index",
    "wordlist_names", "write_pack",
]
