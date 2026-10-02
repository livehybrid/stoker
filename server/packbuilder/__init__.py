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
own storage and registration, and call :func:`set_custom_dir` so operator
word lists persist beside the uploaded packs.

Beyond one-token-one-value the builder writes two eventgen features: linked
fields (``replacementType = mvfile``: every column drawn from one table row
within an event, so a username, its email and its department agree) and
multi-line events (``breaker``: each event runs from one breaker match to the
next, with the regex applied per line exactly as eventgen's ``re.M``).
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

from . import pseudonym as _ps

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
MAX_INT = 10 ** 18             # integer bounds (both engines draw in 64 bits)
MAX_VALUE_LEN = 4096
ANALYSE_EVENTS = 2000          # events examined for suggestions (the rest ride along)
HIGHLIGHT_EVENTS = 25          # events returned with highlight spans
PREVIEW_MAX = 200
# eventgen's "generate the whole sample every interval". Both engines agree
# (verified: 20 lines, count = -1, 3 intervals -> every line exactly 3 times),
# and it is the only count the agent's count_interval rewrite does not split
# across workers, so every worker emits the whole sample.
WHOLE_SAMPLE = -1
# Above this implied per-worker rate, say so: a whole-sample pack in
# count_interval mode paces itself off the sample size.
WHOLE_SAMPLE_RATE_NOTE = 500

REPLACEMENT_KINDS = ("timestamp", "list", "linked", "values", "ipv4", "guid", "mac",
                     "integer", "float", "hex", "static", "sequence", "pseudonym")
BREAK_MODES = ("auto", "line", "csv", "regex")
MAX_BREAKER_LEN = 200
MAX_CUSTOM_LISTS = 200
MAX_CUSTOM_VALUES = 20000
MAX_TABLE_COLUMNS = 20


class BuilderError(ValueError):
    """A request the builder refuses, with an operator-facing reason."""


# --------------------------------------------------------------------------- #
# Word lists
# --------------------------------------------------------------------------- #

_LIST_CACHE = {}  # type: Dict[str, List[str]]   (shipped lists only)
_INDEX_CACHE = []  # type: List[Dict[str, Any]]
_CUSTOM_DIR = None  # type: Optional[str]
_LIST_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_CELL_RE = re.compile(r'^[^,"\r\n]*$')


def set_custom_dir(path):
    # type: (Optional[str]) -> None
    """Where operator word lists live (``<name>.txt`` + ``<name>.json``)."""
    global _CUSTOM_DIR
    _CUSTOM_DIR = path


def _shipped_index():
    # type: () -> List[Dict[str, Any]]
    if not _INDEX_CACHE:
        with open(os.path.join(WORDLIST_DIR, "index.json"), encoding="utf-8") as fh:
            _INDEX_CACHE.extend(json.load(fh))
    return _INDEX_CACHE


def _shipped_names():
    # type: () -> set
    return {e["name"] for e in _shipped_index()}


def _custom_index():
    # type: () -> List[Dict[str, Any]]
    if not _CUSTOM_DIR or not os.path.isdir(_CUSTOM_DIR):
        return []
    shipped = _shipped_names()
    out = []
    for fname in sorted(os.listdir(_CUSTOM_DIR)):
        name = fname[:-5]
        if not fname.endswith(".json") or not _LIST_NAME_RE.match(name) or name in shipped:
            continue
        if not os.path.isfile(os.path.join(_CUSTOM_DIR, name + ".txt")):
            continue
        try:
            with open(os.path.join(_CUSTOM_DIR, fname), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        entry = {"name": name, "title": str(meta.get("title") or name),
                 "description": str(meta.get("description") or ""), "custom": True}
        cols = meta.get("columns")
        if isinstance(cols, list) and len(cols) >= 2:
            entry["columns"] = [str(c) for c in cols]
        out.append(entry)
    return out


def _entries():
    # type: () -> List[Dict[str, Any]]
    return [dict(e, custom=False) for e in _shipped_index()] + _custom_index()


def wordlist_index():
    # type: () -> List[Dict[str, Any]]
    """Shipped and custom lists with title, description, size and a few sample
    values. ``kind`` is ``table`` (with ``columns``) for multi-column lists
    whose rows feed linked fields, else ``list``."""
    out = []
    for entry in _entries():
        values = load_wordlist(entry["name"])
        distinct = list(dict.fromkeys(values))
        kind = "table" if entry.get("columns") else "list"
        out.append(dict(entry, kind=kind, count=len(distinct), sample=distinct[:6]))
    return out


def wordlist_names():
    # type: () -> List[str]
    """Single-column lists (what a ``list`` replacement may name)."""
    return [e["name"] for e in _entries() if not e.get("columns")]


def table_columns():
    # type: () -> Dict[str, List[str]]
    """Multi-column tables and their column names (``linked`` replacements)."""
    return {e["name"]: list(e["columns"]) for e in _entries() if e.get("columns")}


def _read_lines(path):
    # type: (str) -> List[str]
    with open(path, encoding="utf-8") as fh:
        return [line.rstrip("\r\n") for line in fh if line.strip()]


def load_wordlist(name):
    # type: (str) -> List[str]
    """Values of a list, or rows of a table (repeats kept: they are the
    weighting). Shipped lists are cached; custom lists are read each time."""
    if not _LIST_NAME_RE.match(name or ""):
        raise BuilderError("unknown word list %r" % name)
    if name in _LIST_CACHE:
        return _LIST_CACHE[name]
    path = os.path.join(WORDLIST_DIR, name + ".txt")
    if os.path.isfile(path):
        _LIST_CACHE[name] = _read_lines(path)
        return _LIST_CACHE[name]
    if _CUSTOM_DIR:
        path = os.path.join(_CUSTOM_DIR, name + ".txt")
        if os.path.isfile(path) and os.path.isfile(os.path.join(_CUSTOM_DIR, name + ".json")):
            return _read_lines(path)
    raise BuilderError("unknown word list %r" % name)


def _list_set(name):
    # type: (str) -> set
    return {v.lower() for v in load_wordlist(name)}


def save_custom_wordlist(name, values, title="", description="", columns=None):
    # type: (str, Sequence[Any], str, str, Optional[Sequence[Any]]) -> Dict[str, Any]
    """Create or replace an operator word list. With ``columns`` it is a table:
    every value is one comma-separated row with a cell per column (no quotes,
    no commas inside a cell), usable for linked fields."""
    if not _CUSTOM_DIR:
        raise BuilderError("custom word lists are not configured")
    name = str(name or "").strip()
    if not _LIST_NAME_RE.match(name):
        raise BuilderError("list names use lower-case letters, digits and '_' (max 40)")
    if name in _shipped_names():
        raise BuilderError("%r is a shipped list; choose another name" % name)
    existing = {e["name"] for e in _custom_index()}
    if name not in existing and len(existing) >= MAX_CUSTOM_LISTS:
        raise BuilderError("at most %d custom lists" % MAX_CUSTOM_LISTS)
    if not isinstance(values, (list, tuple)):
        raise BuilderError("values must be a list")
    clean = []
    for v in values:
        v = str(v).strip()
        if not v:
            continue
        if "\n" in v or "\r" in v or len(v) > MAX_VALUE_LEN:
            raise BuilderError("values must be single lines under %d characters" % MAX_VALUE_LEN)
        clean.append(v)
    if not clean:
        raise BuilderError("give at least one value")
    if len(clean) > MAX_CUSTOM_VALUES:
        raise BuilderError("at most %d values" % MAX_CUSTOM_VALUES)
    meta = {"title": str(title or "").strip()[:80] or name,
            "description": str(description or "").strip()[:500]}  # type: Dict[str, Any]
    if columns:
        cols = [str(c).strip() for c in columns]
        if not 2 <= len(cols) <= MAX_TABLE_COLUMNS:
            raise BuilderError("a table needs 2 to %d columns" % MAX_TABLE_COLUMNS)
        if any(not _LIST_NAME_RE.match(c) for c in cols) or len(set(cols)) != len(cols):
            raise BuilderError("column names must be distinct and use lower-case letters, digits and '_'")
        for i, row in enumerate(clean):
            cells = row.split(",")
            if len(cells) != len(cols) or not all(_CELL_RE.match(c) for c in cells):
                raise BuilderError("row %d must have %d comma-separated cells without quotes" % (i + 1, len(cols)))
        meta["columns"] = cols
    os.makedirs(_CUSTOM_DIR, exist_ok=True)
    for suffix, body in ((".txt", "\n".join(clean) + "\n"),
                         (".json", json.dumps(meta, indent=1, ensure_ascii=False) + "\n")):
        tmp = os.path.join(_CUSTOM_DIR, ".%s%s.tmp" % (name, suffix))
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, os.path.join(_CUSTOM_DIR, name + suffix))
    return next(e for e in wordlist_index() if e["name"] == name)


def delete_custom_wordlist(name):
    # type: (str) -> None
    if name in _shipped_names():
        raise BuilderError("shipped lists cannot be deleted")
    if not _CUSTOM_DIR or not _LIST_NAME_RE.match(name or "") \
            or not os.path.isfile(os.path.join(_CUSTOM_DIR, name + ".json")):
        raise BuilderError("unknown word list %r" % name)
    for suffix in (".json", ".txt"):
        try:
            os.remove(os.path.join(_CUSTOM_DIR, name + suffix))
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# Event input
# --------------------------------------------------------------------------- #

def _check_events(events):
    # type: (List[str]) -> List[str]
    if not events:
        raise BuilderError("no events found: paste one event per line")
    if len(events) > MAX_EVENTS:
        raise BuilderError("too many events (%d, max %d)" % (len(events), MAX_EVENTS))
    for i, ev in enumerate(events):
        if len(ev.encode("utf-8", errors="replace")) > MAX_EVENT_BYTES:
            raise BuilderError("event %d is larger than %d KB" % (i + 1, MAX_EVENT_BYTES // 1024))
    return events


def _json_array(stripped):
    # type: (str) -> Optional[List[str]]
    if not stripped.startswith("["):
        return None
    try:
        doc = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(doc, list):
        return None
    return [item if isinstance(item, str) else json.dumps(item, separators=(",", ":"), ensure_ascii=False)
            for item in doc]


def _json_stream(stripped):
    # type: (str) -> Optional[List[str]]
    """Concatenated (usually pretty-printed) JSON objects, one compact line each."""
    if not stripped.startswith("{") or "\n" not in stripped:
        return None
    decoder = json.JSONDecoder()
    pos, out = 0, []
    while pos < len(stripped):
        while pos < len(stripped) and stripped[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(stripped):
            break
        try:
            doc, pos = decoder.raw_decode(stripped, pos)
        except ValueError:
            return None
        if not isinstance(doc, dict):
            return None
        out.append(json.dumps(doc, separators=(",", ":"), ensure_ascii=False))
    return out or None


def check_breaker(breaker):
    # type: (Any) -> str
    if not isinstance(breaker, str) or not breaker.strip():
        raise BuilderError("the event breaker must be a regular expression")
    if len(breaker) > MAX_BREAKER_LEN or "\n" in breaker or "\r" in breaker:
        raise BuilderError("the event breaker must be one line of at most %d characters" % MAX_BREAKER_LEN)
    try:
        rx = re.compile(breaker, re.M)
    except re.error as exc:
        raise BuilderError("the event breaker is not a valid regular expression (%s)" % exc)
    if rx.match("") is not None:
        raise BuilderError("the event breaker must not match empty text")
    return breaker


def break_events(text, breaker):
    # type: (str, str) -> List[str]
    """Split text exactly as eventgen does with ``breaker``: each event runs
    from one breaker match (``re.M``) to the next; a match at offset 0 does not
    open an empty event. Trailing newlines are dropped and blank pieces skipped."""
    rx = re.compile(check_breaker(breaker), re.M)
    text = text.replace("\r\n", "\n")
    pieces, extract = [], 0
    for m in rx.finditer(text):
        if m.end() == m.start():
            raise BuilderError("the event breaker must not match empty text")
        if m.start() != 0:
            pieces.append(text[extract:m.start()])
            extract = m.start()
    pieces.append(text[extract:])
    return [p.rstrip("\r\n") for p in pieces if p.strip()]


_BREAKER_CANDIDATES = None  # type: Optional[List[str]]


def _breaker_candidates():
    # type: () -> List[str]
    global _BREAKER_CANDIDATES
    if _BREAKER_CANDIDATES is None:
        _BREAKER_CANDIDATES = ([r"^\[?" + ts for ts, _, _ in _TIMESTAMPS]
                               + [r"^\d{10}(?:\.\d+)?\b", r"^<Event[\s>]", r"^\S"])
    return _BREAKER_CANDIDATES


_CLOSER_RE = re.compile(r"^[\]\)}>]+[,;]?\s*$")


def detect_breaker(text):
    # type: (str) -> Optional[str]
    """A breaker for multi-line text, or None when it is one event per line.

    Tries each timestamp shape, epoch seconds, ``<Event`` and finally "a line
    that does not start with whitespace" (stack traces, indented
    continuations). A candidate fits when the first line matches it and at
    least one later line does not (a continuation line); the first fitting
    candidate wins."""
    lines = [l for l in text.replace("\r\n", "\n").split("\n") if l.strip()]
    if len(lines) < 2:
        return None
    for cand in _breaker_candidates():
        rx = re.compile(cand)
        starts = [bool(rx.match(l)) for l in lines]
        if not starts[0] or all(starts):
            continue
        if cand == r"^\S" and any(_CLOSER_RE.match(l) for l, st in zip(lines, starts) if st):
            continue  # pretty-printed structures, not events
        return cand
    return None


_HEADER_CELL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ .-]{0,63}$")


def _detect_csv(lines):
    # type: (List[str]) -> Optional[List[str]]
    """The header of a simple CSV (no quoting), or None."""
    if len(lines) < 3 or any('"' in l for l in lines):
        return None
    header = [c.strip() for c in lines[0].split(",")]
    if len(header) < 2 or len(set(header)) != len(header):
        return None
    if not all(_HEADER_CELL_RE.match(c) for c in header):
        return None
    commas = lines[0].count(",")
    if any(l.count(",") != commas for l in lines[1:]):
        return None
    return header


def _splunk_csv(text):
    # type: (str) -> Optional[List[str]]
    """``_raw`` values from a Splunk CSV export (exporttool, outputcsv)."""
    first = text.split("\n", 1)[0]
    if "_raw" not in first:
        return None
    import csv
    import io
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return None
    if not rows or "_raw" not in rows[0]:
        return None
    col = rows[0].index("_raw")
    return [r[col] for r in rows[1:] if len(r) > col and r[col].strip()]


def split_input(text, mode="auto", breaker=None):
    # type: (str, str, Optional[str]) -> Dict[str, Any]
    """Split pasted or uploaded text into events.

    Returns ``{"events", "format", "breaker", "header"}``. ``format`` is one of
    ``lines``, ``json`` (a JSON array or concatenated objects), ``csv`` (a
    simple CSV: the header row is dropped and names the columns),
    ``splunk_csv`` (the ``_raw`` column of a Splunk export) or ``multiline``
    (split by ``breaker``). ``mode`` forces one: ``line`` (never breaks on
    anything but newlines), ``csv`` or ``regex`` (with ``breaker``)."""
    if text is None:
        raise BuilderError("no events supplied")
    if mode not in BREAK_MODES:
        raise BuilderError("mode must be one of %s" % ", ".join(BREAK_MODES))
    if len(text.encode("utf-8", errors="replace")) > MAX_TOTAL_BYTES:
        raise BuilderError("sample is larger than %d MB" % (MAX_TOTAL_BYTES // (1024 * 1024)))
    text = text.lstrip("﻿").replace("\r\n", "\n")
    stripped = text.strip()
    result = {"events": [], "format": "lines", "breaker": None, "header": None}  # type: Dict[str, Any]

    if mode == "regex":
        result.update(events=_check_events(break_events(stripped, breaker)), format="multiline",
                      breaker=breaker)
        return result

    lines = [l for l in text.split("\n") if l.strip()]
    if mode in ("auto", "csv"):
        raw = _splunk_csv(stripped)
        if raw is not None:
            result.update(events=raw, format="splunk_csv")
            if any("\n" in e for e in raw):
                found = detect_breaker("\n".join(raw))
                if not found or break_events("\n".join(raw), found) != [e.rstrip("\r\n") for e in raw]:
                    raise BuilderError("the export has multi-line events that no breaker splits cleanly; "
                                       "paste the _raw text and choose a breaker")
                result.update(events=[e.rstrip("\r\n") for e in raw], breaker=found)
            _check_events(result["events"])
            return result
        header = _detect_csv(lines)
        if header:
            result.update(events=_check_events(lines[1:]), format="csv", header=header)
            return result
        if mode == "csv":
            raise BuilderError("not a simple CSV: it needs a header row, the same number of columns "
                               "on every row and no quoted cells")

    for parse in (_json_array, _json_stream):
        events = parse(stripped)
        if events:
            result.update(events=_check_events(events), format="json")
            return result

    if mode == "auto":
        found = detect_breaker(stripped)
        if found:
            result.update(events=_check_events(break_events(stripped, found)), format="multiline",
                          breaker=found)
            return result
    result["events"] = _check_events(lines)
    return result


def split_events(text):
    # type: (str) -> List[str]
    """One event per non-blank line; a JSON array becomes one compact line per
    element (objects serialised with their key order kept)."""
    return split_input(text, "line")["events"]


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


# Shapes that are never a list value: a Windows SID, a 0x number, a %%1234
# message placeholder, a {GUID}.
_NOT_A_WORD_RE = re.compile(r"^(?:S-1-\d[\d-]*|0x[0-9A-Fa-f]+|%%\d+|\{[0-9A-Fa-f-]{36}\})$")
# Lists whose single observed value is strong evidence on its own.
_TYPED_LISTS = ("aws_regions", "http_methods", "country_codes")

_WORD_LISTS = ("usernames", "hostnames", "workstations", "emails", "http_methods", "log_levels",
               "severities", "actions", "aws_regions", "protocols", "auth_results", "file_extensions")


def _fits(list_name, values, key=""):
    # type: (str, Sequence[str], str) -> bool
    """Whether values of this shape could plausibly come from the list: a URL
    is never a username, "No" is not Norway unless the key says country."""
    vals = [v for v in values if v]
    if not vals:
        return False
    if any("://" in v for v in vals) and list_name != "uri_paths":
        return False
    if any(_NOT_A_WORD_RE.match(v) for v in vals):
        return False
    if list_name in _WORD_LISTS and any(re.search(r"\s", v) or len(v) > 64 for v in vals):
        return False
    if list_name == "country_codes":
        k = _norm_key(key)
        return all(re.match(r"^[A-Z]{2}$", v) for v in vals) and ("country" in k or k.endswith("cc"))
    if list_name == "uri_paths":
        return all(v.startswith("/") or "://" in v for v in vals)
    return all(len(v) <= 80 for v in vals)


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


# Key segments that name an identifier rather than a described thing. Matched
# against the LAST segment only (underscore or camelCase), because a leading
# "id_" is usually a different idea ("identity_provider") and v1's
# leading-segment rule put "Consistent pseudonym" on request_method.
# "account" is deliberately NOT here: Windows "VirtualAccount" is a yes/no
# field, not an identifier. account_number and account_id are still caught, by
# their own last segment.
_ID_SEGMENTS = frozenset(("id", "uid", "uuid", "guid", "sid", "ref", "no", "num",
                          "number", "msisdn", "iban", "nino", "ssn"))
# The all-lower-case compact spellings, listed rather than matched by suffix: a
# generic "ends in id" rule also catches "valid", "hybrid" and "overpaid", and
# no stop-list of English words is ever complete.
_COMPACT_IDS = frozenset(("clientid", "sessionid", "userid", "orderid", "accountid",
                          "deviceid", "traceid", "spanid", "requestid", "customerid",
                          "transactionid", "tenantid", "subscriberid", "msgid",
                          "correlationid", "externalid", "payerid", "merchantid"))
# Below this many characters a numeric field is a CODE, not an identifier: a
# Windows EventID of 4624 names a kind of event, and pseudonymising it would
# break every search that looks for it.
_ID_MIN_DIGITS = 6
_ID_MIN_DISTINCT = 10
_CAMEL_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
# Values that look like an identifier's shape but are not one: a Windows
# message-table placeholder (%%1843 renders as "No") and a hex flag word.
_PLACEHOLDER_RE = re.compile(r"^(?:%%\d+|0x[0-9a-fA-F]+)$")


def _key_segments(key):
    # type: (str) -> List[str]
    """The key's words, splitting on separators AND camelCase.

    ``client_id`` and ``clientId`` must both end in the segment ``id``; the
    analyser missed the camelCase form before.
    """
    out = []  # type: List[str]
    for chunk in re.split(r"[^A-Za-z0-9]+", str(key or "")):
        out.extend(m.group(0).lower() for m in _CAMEL_RE.finditer(chunk))
    return out


def looks_like_identifier(key, values):
    # type: (str, Sequence[str]) -> Optional[str]
    """Why this field is an opaque identifier, or None.

    An identifier is a value whose only job is to be equal to itself
    elsewhere, so varying it randomly destroys the correlation the data had and
    the honest treatment is a consistent pseudonym. The rules are deliberately
    narrow: a field is only an identifier when its NAME says so and its VALUES
    are opaque, or when the values are self-evidently opaque identifiers.
    """
    distinct = [v for v in dict.fromkeys(values) if v]
    if not distinct:
        return None
    segments = _key_segments(key)
    named = bool(segments) and (segments[-1] in _ID_SEGMENTS
                                or "".join(segments) in _COMPACT_IDS)
    shape = classify(distinct)
    recurs = len(values) > len(distinct)

    if named and shape == "guid" and not recurs:
        # A GUID that appears once per event correlates with nothing, and the
        # ordinary random-GUID replacement already keeps the original out of
        # the pack while giving unlimited cardinality. A pseudonym would cap it
        # at the sample's own distinct count for no privacy gain.
        return None
    if named and shape in ("int", "epoch", "epoch_ms"):
        # Short numeric values, few of them, are a CODE SET (an event type, a
        # status, a priority) rather than identifiers. A single distinct value
        # is NOT by itself a reason to decline: a one-event sample is exactly
        # where a real identifier is least likely to be noticed, so the
        # privacy-preserving suggestion still belongs there.
        longest = max(len(v.lstrip("-")) for v in distinct)
        if longest < _ID_MIN_DIGITS and len(distinct) < _ID_MIN_DISTINCT:
            return None
    opaque = shape in ("int", "hex", "guid", "epoch", "epoch_ms")
    if named and opaque:
        return "an identifier (%s), so a consistent stand-in keeps it correlatable" % shape
    if named and shape == "string" and all(len(v) >= 6 for v in distinct) \
            and not any(_PLACEHOLDER_RE.match(v) for v in distinct):
        return "an identifier, so a consistent stand-in keeps it correlatable"
    # Values that are identifiers whatever the field is called: a digit string
    # too long to be a number, or a GUID that RECURS (a GUID appearing once per
    # event is just noise; one that comes back is identifying something).
    if shape == "int" and any(len(v.lstrip("-")) > 18 for v in distinct):
        return "a digit string too long to be a number, so it is an identifier"
    if shape == "guid" and len(values) > len(distinct):
        return "a GUID that recurs, so it identifies something"
    # A person-shaped key holding digits is an opaque account number, not a name.
    if _key_list(key) in ("usernames", "emails", "full_names") and shape in ("int", "hex"):
        return "an account identifier rather than a name"
    return None


def recommend(key, values):
    # type: (str, Sequence[str]) -> Tuple[Dict[str, Any], bool, str]
    """(replacement, enabled, why) for a field named ``key`` with these values."""
    distinct = list(dict.fromkeys(v for v in values))
    if any(len(v) > MAX_VALUE_LEN for v in distinct):
        # a blob (certificate, SAML document, encoded payload): not a field to vary
        return {"kind": "static", "value": distinct[0]}, False, "a long value"
    why_identifier = looks_like_identifier(key, values)
    if why_identifier:
        return {"kind": "pseudonym", "widen": None, "rotate": False,
                "rewrite_residuals": False}, True, why_identifier
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
    if shape == "int" and any(len(v.lstrip("-")) > 18 for v in distinct):
        # a digit string too long for a 64-bit draw (an id, a digest): keep the seen values
        return {"kind": "values", "values": distinct}, not constant, "long numeric ids"
    if shape == "int":
        nums = [int(v) for v in distinct]
        lo, hi = min(nums), max(nums)
        if "port" in k and any(s in k for s in ("src", "source", "sport", "client")):
            return {"kind": "integer", "min": 1024, "max": 65535}, True, "source ports"
        if "port" in k:
            return {"kind": "list", "list": "ports"}, True, "ports"
        if k in ("serial", "seq", "sequence", "record_id", "eventrecordid") or k.endswith("_seq"):
            return {"kind": "sequence", "start": max(0, lo)}, True, "a counter"
        if k in ("pid", "ppid") or k.endswith("_pid") or "processid" in k.replace("_", ""):
            return {"kind": "integer", "min": 1000, "max": 65000}, True, "process ids"
        if any(s in k for s in ("status", "code", "response")) and all(100 <= n <= 599 for n in nums):
            return {"kind": "list", "list": "http_status"}, True, "HTTP status codes"
        if constant:
            return {"kind": "integer", "min": lo, "max": hi}, False, "a constant number"
        span = hi - lo
        return ({"kind": "integer", "min": max(-MAX_INT, max(0, lo - span // 2) if lo >= 0 else lo - span // 2),
                 "max": min(MAX_INT, hi + span // 2)}, True, "numbers %d to %d" % (lo, hi))
    if shape == "float":
        nums = [float(v) for v in distinct]
        lo, hi = min(nums), max(nums)
        d = min(9, _decimals(distinct) or 1)
        return ({"kind": "float", "min": lo, "max": hi, "decimals": d}, not constant,
                "decimals %s to %s" % (lo, hi))
    if shape == "hex":
        length = max(len(v) for v in distinct)
        return {"kind": "hex", "length": length}, not constant, "hex strings"
    # strings
    listed = _membership_list(distinct)
    if listed and not _fits(listed, distinct, key):
        listed = None
    if listed and len(distinct) == 1 and listed not in _TYPED_LISTS and _key_list(key) != listed:
        # one value that happens to be in a list ("Security" is a department)
        listed = None
    if listed in ("first_names", "last_names") and _key_list(key) == "usernames":
        # user=alice: a first name used as a login, still a username field
        listed = None
    if listed:
        return {"kind": "list", "list": listed}, True, "values found in the %s list" % listed
    hinted = _key_list(key)
    if hinted and not _fits(hinted, distinct, key):
        hinted = None
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

_XML_DATA = re.compile(r"<Data Name=(?P<q>['\"])(?P<k>[A-Za-z0-9_.-]{1,64})(?P=q)>(?P<v>[^<]*)</Data>")
_XML_LEAF = re.compile(r"<(?P<k>[A-Za-z][A-Za-z0-9_.-]{0,63})>(?P<v>[^<>]+)</(?P=k)>")
_JSON_KV = re.compile(r'"([A-Za-z0-9_.@$-]{1,64})"\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?)')
_KV = re.compile(r'(?:^|(?<=[\s,;|&?\[(]))([A-Za-z_][A-Za-z0-9_.-]{0,40})=("[^"]*"|\'[^\']*\'|[^\s,;&"\'\])]+)')
_BARE = [
    ("guid", _GUID, "GUID"),
    ("email", _EMAIL, "email address"),
    ("mac", _MAC, "MAC address"),
    ("ipv4", _IPV4, "IPv4 address"),
]
_ACCESS = [
    # NCSA client: an IP or a resolved hostname, before "ident user [time]"
    ("client", r'^(\S+) \S+ \S+ \[\d', "client", None),
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
# Phrases claimed straight after timestamps, before key=value can swallow them.
_EARLY_PHRASES = [
    # Linux auditd: msg=audit(<epoch>.<ms>:<serial>)
    ("audit time", r"\baudit\((\d{10})\.\d+:\d+\)", "epoch"),
    ("audit serial", r"\baudit\(\d{10}\.\d+:(\d+)\)", "serial"),
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


def _phrases(phrases, sample, claims, add):
    # type: (Sequence[Tuple[str, str, str]], List[str], _Claims, Any) -> None
    for field, regex, hint in phrases:
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


def analyse(events, header=None):
    # type: (List[str], Optional[List[str]]) -> Dict[str, Any]
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

    # 1a. Timestamps and counters embedded in a known wrapper (auditd).
    _phrases(_EARLY_PHRASES, sample, claims, add)

    # 1b. CSV columns, named from the header row.
    for k, col in enumerate(header or []):
        pattern = r"^(?:[^,\n]*,){%d}([^,\n]*)" % k if k else r"^([^,\n]*)"
        compiled = re.compile(pattern)
        values = []  # type: List[str]
        for i, ev in enumerate(sample):
            m = compiled.match(ev)
            if m and m.end(1) > m.start(1) and claims.free(i, m.start(1), m.end(1)):
                values.append(m.group(1))
        if values:
            replacement, enabled, why = recommend(col, values)
            add(col, "csv", pattern, replacement, enabled, why)

    # 1c. XML: Windows <Data Name='Key'>value</Data>, then leaf elements
    # <Tag>value</Tag>, grouped by name. Claimed before key=value so the
    # attribute that NAMES a field (Name='Key') is never mistaken for one.
    for kind_name, rx in (("xml", _XML_DATA), ("xml", _XML_LEAF)):
        groups = {}  # type: Dict[Tuple[str, str], List[str]]
        order = []  # type: List[Tuple[str, str]]
        for i, ev in enumerate(sample):
            for m in rx.finditer(ev):
                vs, ve = m.start("v"), m.end("v")
                if ve <= vs or not claims.free(i, vs, ve):
                    continue
                ident = (m.group("k"), m.groupdict().get("q") or "")
                if ident not in groups:
                    groups[ident] = []
                    order.append(ident)
                groups[ident].append(m.group("v"))
        for key, quote in order:
            replacement, enabled, why = recommend(key, groups[(key, quote)])
            ek = re.escape(key)
            if rx is _XML_DATA:
                pattern = "<Data Name=%s%s%s>([^<]*)</Data>" % (quote, ek, quote)
            else:
                pattern = "<%s>([^<]*)</%s>" % (ek, ek)
            add(key, kind_name, pattern, replacement, enabled, why)

    # 2. JSON key/values, then key=value pairs: grouped by key across events.
    for kind_name, rx in (("json", _JSON_KV), ("kv", _KV)):
        by_key = {}  # type: Dict[Tuple[str, str], List[str]]
        order = []  # type: List[Tuple[str, str]]
        structural = set()  # type: set
        for i, ev in enumerate(sample):
            seen_here = {}  # type: Dict[Tuple[str, str], str]
            for m in rx.finditer(ev):
                key, raw = m.group(1), m.group(2)
                quote = raw[0] if raw[:1] in ('"', "'") else ""
                vs, ve = m.start(2) + (1 if quote else 0), m.end(2) - (1 if quote else 0)
                if ve <= vs or not claims.free(i, vs, ve):
                    continue
                ident = (key, quote)
                # One token writes one value into every match: a key that
                # repeats with different values inside one event (an XML
                # Name='...' attribute, a nested JSON "name") is structure.
                if seen_here.setdefault(ident, ev[vs:ve]) != ev[vs:ve]:
                    structural.add(ident)
                if ident not in by_key:
                    by_key[ident] = []
                    order.append(ident)
                by_key[ident].append(ev[vs:ve])
        for key, quote in order:
            if (key, quote) in structural:
                continue
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
    _phrases(_PHRASES, sample, claims, add)

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

    _auto_link(found)
    # Never hand the UI a suggestion its own validation would refuse.
    known, tables = set(wordlist_names()), table_columns()
    kept = []
    for f in found:
        try:
            f["replacement"] = _validate_replacement(f["field"], f["replacement"], known, tables)
        except BuilderError:
            continue
        kept.append(f)
    found = kept
    return {"events": len(events), "suggestions": found,
            "highlights": highlight(events[:HIGHLIGHT_EVENTS], found)}


# list -> column of the shipped tables. A field is linked when at least two
# fields of an event draw from the same table, so they come from one row.
_LINKS = [
    ("identities", {"usernames": "username", "emails": "email", "full_names": "full_name",
                    "first_names": "first_name", "last_names": "last_name",
                    "departments": "department", "cities": "city", "countries": "country"},
     None),
    # hostnames and private IPs are only linked when both are named keys: a
    # bare IP anywhere in the text is as likely a peer as the host itself.
    ("hosts", {"hostnames": "hostname", "internal_ips": "ip"}, ("json", "kv", "csv")),
]


_PEER_RE = re.compile(r"(?:^|_)(?:src|source|dst|dest|destination|target|remote|peer|from|to|orig|origin|"
                      r"sender|recipient|client|server)(?:_|$)|^(?:src|dst|dest|source|target)")


def _auto_link(found):
    # type: (List[Dict[str, Any]]) -> None
    """Turn list suggestions into linked fields where two or more share a
    table. Where several fields claim one column (user, src_user and
    dest_user), only the unqualified one is linked; the rest stay independent
    (one row would make them always equal), and none is linked when that is
    ambiguous. Bare (unkeyed) values are
    never linked. The original replacement is kept as ``alternative``."""
    for table, colmap, kinds in _LINKS:
        by_col = {}  # type: Dict[str, List[Dict[str, Any]]]
        for f in found:
            rep = f["replacement"]
            if not f["enabled"] or rep.get("kind") != "list" or rep.get("list") not in colmap:
                continue
            if f["kind"] == "bare" or (kinds and f["kind"] not in kinds):
                continue
            by_col.setdefault(colmap[rep["list"]], []).append(f)
        single = {}  # type: Dict[str, Dict[str, Any]]
        for col, fs in by_col.items():
            if len(fs) > 1:
                # user + src_user + dest_user: the unqualified one is the event's
                # subject; the peers stay independent draws.
                fs = [f for f in fs if not _PEER_RE.search(_norm_key(f["field"]))]
            if len(fs) == 1:
                single[col] = fs[0]
        if len(single) < 2:
            continue
        for col, f in single.items():
            f["alternative"] = f["replacement"]
            f["replacement"] = {"kind": "linked", "table": table, "column": col}
            f["why"] = "%s; linked to one %s row per event" % (f["why"], table)


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
    breaker = cfg.get("breaker") or None
    if breaker is not None:
        check_breaker(breaker)
    total = 0
    clean_events = []
    for i, ev in enumerate(events):
        if not isinstance(ev, str) or not ev.strip():
            raise BuilderError("event %d is empty" % (i + 1))
        ev = ev.replace("\r\n", "\n").rstrip("\n")
        if "\r" in ev or ("\n" in ev and breaker is None):
            raise BuilderError("event %d spans several lines; set an event breaker for multi-line events"
                               % (i + 1))
        size = len(ev.encode("utf-8"))
        if size > MAX_EVENT_BYTES:
            raise BuilderError("event %d is larger than %d KB" % (i + 1, MAX_EVENT_BYTES // 1024))
        total += size
        clean_events.append(ev)
    if total > MAX_TOTAL_BYTES:
        raise BuilderError("events exceed %d MB in total" % (MAX_TOTAL_BYTES // (1024 * 1024)))
    if breaker is not None:
        resplit = break_events("\n".join(clean_events), breaker)
        if resplit != clean_events:
            bad = next((i for i, (a, b) in enumerate(zip(resplit, clean_events)) if a != b),
                       min(len(resplit), len(clean_events)))
            raise BuilderError("event %d does not survive the event breaker: every event must start "
                               "with a breaker match and no later line may match it" % (bad + 1))
    tokens = cfg.get("tokens") or []
    if len(tokens) > MAX_TOKENS:
        raise BuilderError("too many fields (%d, max %d)" % (len(tokens), MAX_TOKENS))
    known_lists = set(wordlist_names())
    tables = table_columns()
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
        rep = _validate_replacement(label, t.get("replacement") or {}, known_lists, tables)
        if rep["kind"] == "pseudonym" and re.compile(pattern).groups > 1:
            raise BuilderError(
                "%s: a consistent-pseudonym pattern must have at most one capture "
                "group, so there is no doubt which text is the identifier" % label)
        clean_tokens.append({"id": t.get("id") or "t%d" % (i + 1), "field": label[:80],
                             "pattern": pattern, "enabled": bool(t.get("enabled", True)),
                             "replacement": rep})
    # -1 is eventgen's "the whole sample, every interval". It is the default for
    # a new pack because a positive count below the sample size silently emits
    # only the first `count` events for ever (measured on both engines), which
    # reads as "my upload did not work". eps and per_day_gb runs overwrite count
    # anyway; count_interval is the mode that keeps it, and -1 is the one value
    # the agent's count_interval rewrite leaves alone instead of splitting.
    count = cfg.get("count", WHOLE_SAMPLE)
    count = WHOLE_SAMPLE if _as_int(count) == WHOLE_SAMPLE else _int_in(count, 1, 100000, "count")
    interval = _int_in(cfg.get("interval", 1), 1, 86400, "interval")
    order = cfg.get("order") or "sequential"
    if order not in ("sequential", "random"):
        raise BuilderError("order must be sequential or random")
    rotation = _validate_rotation(cfg.get("rotation"), clean_tokens, order)
    tags = [str(x).strip()[:40] for x in (cfg.get("tags") or []) if str(x).strip()][:20]
    return {
        "name": name,
        "description": str(cfg.get("description") or "").strip()[:1000],
        "sourcetype": (str(cfg.get("sourcetype") or "").strip()[:128] or None),
        "tags": tags,
        "events": clean_events,
        "breaker": breaker,
        "tokens": clean_tokens,
        "count": count,
        "interval": interval,
        "order": order,
        "rotation": rotation,
    }


ROTATE_SCOPES = ("pass", "window")
ROTATE_PERIOD_MIN, ROTATE_PERIOD_MAX, ROTATE_PERIOD_DEFAULT = 1, 86400, 60


def _validate_rotation(rotation, tokens, order):
    # type: (Any, Sequence[Dict[str, Any]], str) -> Optional[Dict[str, Any]]
    """The pack's rotation settings, or None when no field rotates.

    The scope is per pack rather than per field because it is one setting on the
    eventgen stanza: a stanza counts its passes, or its clock windows, once for
    all of its tokens. Which fields rotate stays per field.
    """
    rotating = [t for t in tokens
                if t.get("enabled", True)
                and t["replacement"].get("kind") == "pseudonym"
                and t["replacement"].get("rotate")]
    if not rotating:
        return None
    if order == "random":
        # randomizeEvents picks a line at random, so there is no pass over the
        # sample and no journey to hold together: the three events of one user
        # would land in three different identities. Refusing beats emitting
        # something that looks like rotation but correlates nothing.
        raise BuilderError(
            "a field is set to rotate per replay, which needs the events replayed "
            "in order; set the event order to sequential, or turn rotation off")
    rotation = rotation or {}
    if not isinstance(rotation, dict):
        raise BuilderError("rotation must be an object or null")
    scope = str(rotation.get("scope") or "pass").strip().lower()
    if scope not in ROTATE_SCOPES:
        raise BuilderError("rotation scope must be one of %s" % ", ".join(ROTATE_SCOPES))
    out = {"scope": scope, "fields": [t["field"] for t in rotating]}
    if scope == "window":
        out["period"] = _int_in(rotation.get("period", ROTATE_PERIOD_DEFAULT),
                                ROTATE_PERIOD_MIN, ROTATE_PERIOD_MAX, "rotation period")
    return out


def rotating_fields(cfg):
    # type: (Dict[str, Any]) -> List[Dict[str, Any]]
    """The enabled pseudonym fields that rotate per replay."""
    return [t for t in pseudonym_fields(cfg) if t["replacement"].get("rotate")]


def _rotate_replacement(rep):
    # type: (Dict[str, Any]) -> str
    """``token.N.replacement`` for a rotate token: the format to render in.

    ``keep`` follows the stand-in's own shape, so widening the stand-in widens
    the rotation with it and one width is chosen in one place.
    """
    widen = rep.get("widen")
    if not widen:
        return "keep"
    shape = widen.get("shape")
    if shape == _ps.GUID:
        return "guid"
    return "%s(%d)" % (shape, widen["length"])


def _as_int(v):
    # type: (Any) -> Optional[int]
    """``v`` as an int, or None when it is not a whole number."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _int_in(v, lo, hi, what):
    # type: (Any, int, int, str) -> int
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BuilderError("%s must be a whole number" % what)
    if not lo <= n <= hi:
        raise BuilderError("%s must be between %d and %d" % (what, lo, hi))
    return n


def _validate_replacement(label, rep, known_lists, tables=None):
    # type: (str, Dict[str, Any], set, Optional[Dict[str, List[str]]]) -> Dict[str, Any]
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
    if kind == "linked":
        table, column = rep.get("table"), rep.get("column")
        cols = (tables if tables is not None else table_columns()).get(table)
        if cols is None:
            raise BuilderError("%s: unknown table %r" % (label, table))
        if column not in cols:
            raise BuilderError("%s: table %s has no column %r" % (label, table, column))
        return {"kind": kind, "table": table, "column": column}
    if kind == "pseudonym":
        # A stand-in derived from the matched value, applied when the pack is
        # WRITTEN, so no eventgen token is emitted for this field and the
        # originals never reach the pack. See server/packbuilder/pseudonym.py.
        out = {"kind": kind, "widen": None, "rotate": False,
               "rewrite_residuals": bool(rep.get("rewrite_residuals"))}
        widen = rep.get("widen")
        if widen is not None:
            if not isinstance(widen, dict):
                raise BuilderError("%s: widen must be an object or null" % label)
            try:
                fmt = _ps.widen_format(widen.get("shape"), widen.get("length"))
            except _ps.PseudonymError as exc:
                raise BuilderError("%s: %s" % (label, exc))
            out["widen"] = {"shape": fmt.shape}
            if fmt.shape != _ps.GUID:
                out["widen"]["length"] = fmt.width
        out["rotate"] = bool(rep.get("rotate"))
        return out
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
        lo = _int_in(rep.get("min", 0), -MAX_INT, MAX_INT, "%s minimum" % label)
        hi = _int_in(rep.get("max", 100), -MAX_INT, MAX_INT, "%s maximum" % label)
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
        return {"kind": kind, "start": _int_in(rep.get("start", 1), 0, MAX_INT, "%s start" % label)}
    return {"kind": kind}


# --------------------------------------------------------------------------- #
# Token order and eventgen mapping
# --------------------------------------------------------------------------- #

_ORDER = {"timestamp": 0, "ipv4": 1, "guid": 1, "mac": 1, "integer": 1, "float": 1,
          "hex": 1, "sequence": 1, "list": 2, "linked": 2, "values": 2, "static": 2}


def ordered_tokens(cfg):
    # type: (Dict[str, Any]) -> List[Dict[str, Any]]
    """Enabled tokens in apply order: timestamps, then generated values, then
    list/value/static replacements. Free text goes in last so no earlier
    pattern can match inside an inserted list value (an IPv4 pattern inside
    "Chrome/128.0.0.0", say). Stable within each group."""
    enabled = [t for t in cfg["tokens"] if t.get("enabled", True)
               and t["replacement"]["kind"] != "pseudonym"]
    return sorted(enabled, key=lambda t: _ORDER.get(t["replacement"]["kind"], 3))


def pseudonym_fields(cfg):
    # type: (Dict[str, Any]) -> List[Dict[str, Any]]
    """The enabled pseudonym tokens of a config (possibly none)."""
    return [t for t in (cfg.get("tokens") or [])
            if t.get("enabled", True)
            and (t.get("replacement") or {}).get("kind") == "pseudonym"]


def _residual_texts(cfg, fields):
    # type: (Dict[str, Any], Sequence[Dict[str, Any]]) -> List[Tuple[str, str]]
    """Everything besides the events that a pack carries operator text into.

    These are built from the ORIGINAL sample, so an identifier in one of them is
    a real survivor and needs no masking: the analyser anchors a bare-value
    pattern on the text before it, and a "values I list" field copies what it
    saw. They are reported, never rewritten - they are configuration the
    operator wrote, and editing it silently would change what the pack matches.
    """
    out = [("the pack name", cfg.get("name") or ""),
           ("the description", cfg.get("description") or "")]
    pseudo_patterns = {t["pattern"] for t in fields}
    for token in cfg.get("tokens") or []:
        label = token.get("field") or token.get("pattern") or "a field"
        if token["pattern"] not in pseudo_patterns:
            out.append(("the pattern of %s" % label, token["pattern"]))
        out.append(("the name of %s" % label, token.get("field") or ""))
        rep = token.get("replacement") or {}
        if rep.get("kind") == "values":
            out.append(("the listed values of %s" % label, "\n".join(rep.get("values") or [])))
        elif rep.get("kind") == "static":
            out.append(("the fixed value of %s" % label, str(rep.get("value") or "")))
    return out


def apply_pseudonyms(cfg, subkey=None, fingerprint=None, already=None, strict=True):
    # type: (Dict[str, Any], Optional[bytes], Optional[str], Optional[Sequence[str]]) -> Tuple[Dict[str, Any], Optional[Any]]
    """``(cfg with its events pseudonymised, report)`` - or the cfg unchanged.

    The single path shared by the live preview and ``write_pack``, so what the
    operator is shown is exactly what the pack will contain. A config with no
    pseudonym field is returned untouched and needs no key.

    The returned config's ``events`` hold the stand-ins, which is what makes the
    originals absent from the pack: ``write_pack`` writes these events into both
    the sample file and ``stoker-builder.json``.

    **Never pseudonymise a stand-in.** Reopening a built pack hands back a
    config whose events are already stand-ins; hashing them again would give
    ``p(p(x))``, a different value, so the rebuilt pack would silently stop
    correlating with the one it replaced and with every other pack on the
    instance. ``already`` is the list of events the server read from the pack on
    disk, which are known to be pseudonymised; they are passed through
    untouched.

    The caller MUST supply it when rebuilding an existing pack. There is
    deliberately no fallback inferred from the config, because
    :func:`validate_config` strips the server-written ``pseudonymised`` key from
    anything a client sends, so a config arriving over the API carries no
    trustworthy evidence of a previous build. The pack on disk is the only
    honest source of that, and it is the server that holds it.
    """
    fields = pseudonym_fields(cfg)
    if not fields:
        return cfg, None
    if not subkey:
        raise BuilderError(
            "this pack has a consistent-pseudonym field, which needs the "
            "instance's pseudonym key; none was supplied")
    done = set(already or ())
    events = list(cfg["events"])
    fresh = [(i, e) for i, e in enumerate(events) if e not in done]
    try:
        report = _ps.pseudonymise_events([e for _i, e in fresh], fields, subkey)
    except _ps.PseudonymError as exc:
        raise BuilderError(str(exc))
    merged = list(events)
    remap = {}  # type: Dict[int, List[Tuple[int, int]]]
    for position, (original_index, _text) in enumerate(fresh):
        merged[original_index] = report.events[position]
        if position in report.rewritten:
            remap[original_index] = report.rewritten[position]
    report.events = merged
    report.rewritten = remap
    # An identifier can survive outside the span its field matched - in a URL, a
    # message string, another field's value list, a pattern the analyser anchored
    # on neighbouring text. The pack travels, so look for it.
    _ps.scan_residuals(
        report,
        texts=_residual_texts(cfg, fields),
        rewrite_fields=[t.get("field") for t in fields
                        if (t.get("replacement") or {}).get("rewrite_residuals")])
    blocking, residual_warnings = _ps.residual_problems(report)
    report.warnings.extend(residual_warnings)
    if blocking:
        if strict:
            raise BuilderError(" ".join(blocking))
        report.warnings.extend(blocking)
    merged = report.events
    out = dict(cfg, events=merged)
    out["pseudonymised"] = [
        {"field": row.field, "pattern": row.pattern, "widen": row.widen,
         "spans": row.spans, "distinct": row.distinct, "classes": row.classes}
        for row in report.rows
    ]
    if fingerprint:
        out["pseudonym_key"] = {"name": "default", "fingerprint": fingerprint,
                                "algorithm": _ps.ALGORITHM}
    return out, report


def pseudonym_warnings(report):
    # type: (Optional[Any]) -> List[str]
    """Operator-facing notes for a pseudonymising build: the collision risk.

    A collision merges two identities, which corrupts exactly the correlation
    the operator is testing, so the probability is stated rather than left to
    be discovered. A real collision is refused outright by the pass itself.
    """
    if report is None:
        return []
    out = list(report.warnings)
    for row in report.rows:
        for cls in row.classes:
            p = cls.get("collision_probability") or 0.0
            if p >= 0.001 and cls["distinct"] > 1:
                out.append(
                    "%s: %d distinct values in a %s space of %d, so there is about a "
                    "%.1f%% chance two of them would become the same stand-in and merge. "
                    "Widen this field to a fixed length if that matters."
                    % (row.field, cls["distinct"], cls["shape"], cls["space"], p * 100.0))
    return out


def rotation_tables(cfg):
    # type: (Dict[str, Any]) -> Dict[str, Dict[str, Tuple[int, int]]]
    """``{format class: {matched text: (k, D)}}`` for the rotating fields.

    ONE table per class, shared by every rotating field, exactly as the engine
    builds it: two fields holding the same identifier must rotate to the same
    new identifier, or a pack with ``src_user`` and ``dest_user`` would stop
    joining the moment it rotated. A table per field would also give a different
    ``D`` from the engine's and so a different identity for every value.

    Walked line by line in file order, then token by token in conf order, then
    match by match left to right. That walk defines ``k``, ``k`` is part of the
    identity, and the engines do it independently from the sample, so the order
    is a contract rather than an implementation detail. See
    ``build_rotation_tables`` in firebox.
    """
    fields = [(t, re.compile(t["pattern"])) for t in rotating_fields(cfg)]
    if not fields:
        return {}
    order = {}  # type: Dict[str, List[str]]
    for event in cfg.get("events") or []:
        for token, rx in fields:
            for m in rx.finditer(event):
                text = m.group(1) if rx.groups else m.group(0)
                if text is None:
                    continue
                fmt = _rotation_format(token["replacement"], text)
                if fmt.shape == _ps.NONE:
                    continue
                cls = order.setdefault(_class_key(fmt), [])
                if text not in cls:
                    cls.append(text)
    out = {}  # type: Dict[str, Dict[str, Tuple[int, int]]]
    for key, values in order.items():
        out[key] = {text: (k, len(values)) for k, text in enumerate(values)}
    return out


def _rotation_format(rep, text):
    # type: (Dict[str, Any], str) -> Any
    """The format a rotate token renders in: its widen, else the text's own."""
    widen = rep.get("widen")
    if widen:
        return _ps.widen_format(widen.get("shape"), widen.get("length"))
    return _ps.infer(text)


def _class_key(fmt):
    # type: (Any) -> str
    """A format class as a dict key; `mixed` carries its template signature."""
    if fmt.shape == _ps.MIXED:
        return "m" + "|".join(str(e) for e in fmt.template)
    return "%s%s" % (fmt.shape, fmt.width or "")


def rotation_capacity(cfg, report):
    # type: (Dict[str, Any], Optional[Any]) -> Optional[Dict[str, Any]]
    """How much rotation a pack has before its identities start repeating.

    Under ``pass`` scope the identity is a counter in the field's own format
    space, so the space divided by the number of distinct values sharing it is
    how many passes each worker gets. That bound is easy to underestimate: a
    6-digit id over a 1000-line sample has 900 passes per worker, which at
    load-test rates is seconds, not hours. Returning the number lets the builder
    say so and the run gate refuse.

    None when nothing rotates. ``passes`` is per worker, so a run over N workers
    consumes it N times as fast.
    """
    if not cfg.get("rotation") or report is None:
        return None
    rotating = {t["field"] for t in rotating_fields(cfg)}
    passes, tightest = None, None
    for row in report.rows:
        if row.field not in rotating:
            continue
        for cls in row.classes:
            available = int(cls["space"]) // max(1, int(cls["distinct"]))
            if passes is None or available < passes:
                passes, tightest = available, dict(cls, field=row.field)
    if passes is None:
        return None
    events = len(cfg.get("events") or []) or 1
    return {"passes": passes, "events": passes * events, "tightest": tightest,
            "scope": cfg["rotation"]["scope"]}


def _rotate_preview(cfg, text, tables, index, epoch):
    # type: (Dict[str, Any], str, Dict[str, Dict[str, Tuple[int, int]]], int, float) -> str
    """Apply the rotating fields to one preview event.

    ``index`` is the event's ordinal, so the pass is ``index // sample size``:
    the same arithmetic the engine does, which is what makes the preview show
    the login and the logout of one pass sharing an identity.
    """
    rotation = cfg.get("rotation") or {}
    sample = len(cfg.get("events") or []) or 1
    for token in rotating_fields(cfg):
        rx = re.compile(token["pattern"])
        widen = token["replacement"].get("widen")
        fmt = (_ps.widen_format(widen.get("shape"), widen.get("length"))
               if widen else None)
        pieces, pos = [], 0
        for m in rx.finditer(text):
            s_, e_ = (m.span(1) if rx.groups else m.span(0))
            if s_ < pos or s_ < 0:
                continue
            span = text[s_:e_]
            if rotation.get("scope") == "window":
                new = _ps.aligned_rotation(
                    span, _ps.window_index(int(epoch), rotation.get("period") or 60),
                    widen=fmt)
            else:
                cls = _class_key(fmt if fmt is not None else _ps.infer(span))
                k, distinct = (tables.get(cls) or {}).get(span, (0, 1))
                # Preview is one worker: slot 0 of 1, as a standalone run is.
                new = _ps.pass_rotation(span, index // sample, 1, 0, distinct, k, widen=fmt)
            pieces.append(text[pos:s_])
            pieces.append(new if new is not None else span)
            pos = e_
        pieces.append(text[pos:])
        text = "".join(pieces)
    return text


def rotation_warnings(cfg, report):
    # type: (Dict[str, Any], Optional[Any]) -> List[str]
    """Operator-facing notes for a rotating pack.

    Both scopes have a cost the operator is choosing, and neither is visible
    from the builder form, so both are stated.
    """
    rotation = cfg.get("rotation")
    if not rotation:
        return []
    out = []
    fields = ", ".join(rotation["fields"])
    if rotation["scope"] == "window":
        out.append(
            "%s: aligned rotation gives each %ds window one identity per value, so "
            "the same id appears in every sourcetype, pack and run of that window. "
            "That is what makes it joinable, and it also means the number of distinct "
            "identities is set by the window length rather than by the volume: a "
            "shorter period gives more of them."
            % (fields, rotation["period"]))
    cap = rotation_capacity(cfg, report)
    if cap and rotation["scope"] == "pass":
        t = cap["tightest"]
        out.append(
            "%s: rotation has %s identities per worker before they repeat (%d distinct "
            "value(s) in a %s space of %d, over a %d-event sample, so about %s events "
            "per worker). Widen the field to a fixed length if the run will send more "
            "than that."
            % (t["field"], "{:,}".format(cap["passes"]), t["distinct"], t["shape"],
               int(t["space"]), len(cfg.get("events") or []), "{:,}".format(cap["events"])))
    return out


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
    tables = None  # type: Optional[Dict[str, List[str]]]
    # Rotation goes FIRST. The engines apply tokens in conf order to the same
    # event text, so a rotate token has to see the stand-in the pack was written
    # with; if an earlier token had already rewritten that span, the rotation
    # would either miss it or rotate something else's value.
    for t in rotating_fields(cfg):
        out.append((_conf_value(t["pattern"]), "rotate", _rotate_replacement(t["replacement"])))
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
        elif kind == "linked":
            if tables is None:
                tables = table_columns()
            files[rep["table"]] = load_wordlist(rep["table"])
            used.add(rep["table"])
            column = tables[rep["table"]].index(rep["column"]) + 1
            out.append((pattern, "mvfile", "samples/lists/%s.sample:%d" % (rep["table"], column)))
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


def write_pack(cfg, dest, subkey=None, fingerprint=None, already=None):
    # type: (Dict[str, Any], str, Optional[bytes], Optional[str], Optional[Sequence[str]]) -> str
    """Write the pack for a validated config into the new directory ``dest``.

    A config with a consistent-pseudonym field is rewritten first, so the
    sample file and the stored builder config both hold stand-ins and the
    originals are written nowhere.
    """
    cfg, report = apply_pseudonyms(cfg, subkey=subkey, fingerprint=fingerprint,
                                   already=already)
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
    rotation = cfg.get("rotation")
    if rotation:
        lines.append("rotate.scope = %s" % rotation["scope"])
        if rotation["scope"] == "window":
            lines.append("rotate.period = %d" % rotation["period"])
    if cfg.get("breaker"):
        lines.append("breaker = %s" % _conf_value(cfg["breaker"]))
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
    if cfg.get("pseudonym_key"):
        key = cfg["pseudonym_key"]
        yaml += ["pseudonym:",
                 "  algorithm: %s" % key["algorithm"],
                 "  key_fingerprint: %s" % key["fingerprint"],
                 "  fields: %s" % ", ".join(r["field"] for r in cfg.get("pseudonymised") or [])]
    if cfg.get("rotation"):
        rot = cfg["rotation"]
        # Recorded so an operator reading the pack can see why the ids move and
        # which engine it needs. The actual refusal is the worker's, from the
        # conf, because the worker is where the engine is chosen.
        yaml += ["rotation:",
                 "  algorithm: %s" % (_ps.ALIGNED_ALGORITHM if rot["scope"] == "window"
                                      else _ps.ROTATE_ALGORITHM),
                 "  scope: %s" % rot["scope"],
                 "  engine: firebox",
                 "  fields: %s" % ", ".join(rot["fields"])]
        if rot["scope"] == "window":
            yaml += ["  period_s: %d" % rot["period"]]
    if cfg.get("sourcetype"):
        yaml += ["defaults:", "  sourcetype: %s" % cfg["sourcetype"]]
    with open(os.path.join(dest, "pack.yaml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(yaml) + "\n")
    with open(os.path.join(dest, BUILDER_FILE), "w", encoding="utf-8") as fh:
        json.dump(dict(cfg, builder_version=2), fh, indent=1, ensure_ascii=False)
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


def _value(rep, rng, epoch, state, rows=None):
    # type: (Dict[str, Any], random.Random, float, Dict[str, int], Optional[Dict[str, List[str]]]) -> str
    kind = rep["kind"]
    if kind == "linked":
        rows = {} if rows is None else rows
        if rep["table"] not in rows:
            rows[rep["table"]] = rng.choice(load_wordlist(rep["table"])).split(",")
        cols = table_columns()[rep["table"]]
        row = rows[rep["table"]]
        idx = cols.index(rep["column"])
        return row[idx] if idx < len(row) else ""
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


def coverage_warnings(cfg):
    # type: (Dict[str, Any]) -> List[str]
    """Warn when the pack's own count/interval will not generate every event.

    Each interval emits ``count`` events starting again at the sample's first
    line, so a sequential pack whose count is below its sample size emits only
    that prefix, for ever; and a count that is not a whole multiple of the
    sample leaves the last pass of every interval half-finished. An eps or
    per-day-GB run replaces count with the worker's share, so this only bites
    count-per-interval runs - which is what a first smoke test usually is.
    """
    out = []  # type: List[str]
    events = cfg.get("events") or []
    count = _as_int(cfg.get("count", WHOLE_SAMPLE))
    if not events or count is None:
        return out
    n = len(events)
    interval = cfg.get("interval", 1)
    if count < 0:
        # Whole-sample packs set their own rate in count_interval mode, which is
        # engine-paced with no token bucket: a big sample at interval 1 is a big
        # rate. eps and GB/day runs replace count, so they are unaffected.
        implied = int(n / max(1, interval))
        if implied >= WHOLE_SAMPLE_RATE_NOTE:
            out.append(
                "a count-per-interval run of this pack emits about %d events per second PER WORKER "
                "(%d events every %s s, engine-paced with no rate cap). Launch it as eps or GB/day "
                "to choose the rate, or raise the interval." % (implied, n, interval))
        return out
    if cfg.get("order") == "random":
        return out
    if count < n:
        out.append(
            "a count-per-interval run generates only the first %d of your %d events every %s s; "
            "the other %d are never generated (eps and GB/day runs use the whole sample). Set "
            "\"whole sample every interval\" to generate all of them."
            % (count, n, interval, n - count))
    elif count % n:
        out.append(
            "count %d is not a whole multiple of your %d events, so the last pass of every "
            "interval stops part-way through the sample" % (count, n))
    return out


def render_preview(cfg, n=20, seed=None, subkey=None, already=None):
    # type: (Dict[str, Any], int, Optional[int]) -> Dict[str, Any]
    """Render ``n`` events from a validated config without touching disk.

    Returns ``{"events": [...], "warnings": [...], "bytes_per_event": float}``.
    A warning names every enabled field whose pattern matches no sample event
    (it would never fire) and every field that matches inside another field's
    inserted text.
    """
    n = max(1, min(int(n), PREVIEW_MAX))
    rng = random.Random(seed)
    # Pseudonymise first so the preview shows the stand-ins the pack will hold,
    # and so a later token can never match an original that will not be there.
    # A preview REPORTS a blocking residual rather than failing, so the operator
    # sees what to fix while they are still editing; the save refuses it.
    cfg, pseudo_report = apply_pseudonyms(cfg, subkey=subkey, already=already,
                                          strict=False)
    tokens = ordered_tokens(cfg)
    compiled = [(t, re.compile(t["pattern"])) for t in tokens]
    events = cfg["events"]
    warnings = []
    for t, rx in compiled:
        if not any(rx.search(ev) for ev in events):
            warnings.append("%s: the pattern matches none of the sample events" % t["field"])
    warnings.extend(pseudonym_warnings(pseudo_report))
    warnings.extend(rotation_warnings(cfg, pseudo_report))
    warnings.extend(coverage_warnings(cfg))
    now = time.time()
    state = {}  # type: Dict[str, Dict[str, int]]
    out = []
    count = _as_int(cfg.get("count", WHOLE_SAMPLE))
    rot_tables = rotation_tables(cfg)
    for i in range(n):
        if cfg.get("order") == "random":
            base = rng.choice(events)
        elif count is not None and count > 0:
            # Mirror the engines: each interval emits `count` events starting
            # again at line 0, so the preview must repeat the same truncation
            # rather than showing lines the run will never generate.
            base = events[(i % count) % len(events)]
        else:
            base = events[i % len(events)]
        epoch = now - rng.uniform(0, cfg.get("interval", 1))
        rows = {}  # type: Dict[str, List[str]]   one table row per event (mvfile)
        text = base
        # Rotation first, as the conf orders it, and before any other token can
        # rewrite the span. Without this the preview would show the pack's
        # stand-ins, which is what the pack HOLDS but not what Splunk receives:
        # the whole point of rotation is that the value moves every replay.
        if rot_tables:
            text = _rotate_preview(cfg, text, rot_tables, i, epoch)
        for t, rx in compiled:
            matches = list(rx.finditer(text))
            if not matches:
                continue
            value = _value(t["replacement"], rng, epoch, state.setdefault(t["id"], {}), rows)
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
    "BuilderError", "BUILDER_FILE", "BUILDER_TAG", "analyse", "break_events", "check_breaker",
    "apply_pseudonyms", "classify", "coverage_warnings", "delete_custom_wordlist",
    "detect_breaker", "highlight", "looks_like_identifier", "pseudonym_fields",
    "pseudonym_warnings", "rotating_fields", "rotation_capacity", "rotation_tables",
    "rotation_warnings",
    "load_wordlist", "WHOLE_SAMPLE",
    "ordered_tokens", "eventgen_tokens", "read_builder_config", "recommend", "render_preview",
    "save_custom_wordlist", "set_custom_dir", "split_events", "split_input", "table_columns",
    "validate_config", "wordlist_index", "wordlist_names", "write_pack",
]
