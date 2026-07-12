# -*- coding: utf-8 -*-
"""Best-effort timestamp extraction for CADENCE replay (stdlib only).

Piston's CADENCE mode reproduces the recorded inter-event gaps, so it needs the
epoch time of each raw line. Real capture datasets (e.g. splunk/security_content
attack_data) are heterogeneous, so extraction is deliberately best-effort:

* an optional operator-supplied regex (``STOKER_RAWREPLAY_TS_REGEX``) whose first
  capturing group (or whole match) is the timestamp text; else
* a small battery of built-in patterns covering the common shapes (ISO 8601 with
  optional fractional seconds and Z/offset, ``syslog``-style ``Mon DD HH:MM:SS``,
  ``YYYY-MM-DD HH:MM:SS``, epoch seconds/millis) scanned left-to-right.

The parsed value is epoch **seconds as a float** (fractional preserved). A line
whose timestamp cannot be parsed yields ``None``; the caller then falls back to a
fixed small gap so replay never stalls on one unparseable line. This is not a
general timestamp engine (eventgen's own is far larger); it covers the cases a
byte-for-byte capture replay realistically needs, and the operator can always
pin the format with an explicit regex + strptime field.

No third-party dependencies (the worker's rawreplay engine is stdlib-only).
"""

from __future__ import absolute_import

import datetime
import re
import time

# Epoch reference for naive datetimes (assume UTC, matching the eventgen
# replay generator's own ``event_time - datetime(1970,1,1)`` convention).
_EPOCH = datetime.datetime(1970, 1, 1)

# Month abbreviations for syslog-style timestamps (locale-independent: we never
# rely on the C locale, which strptime %b would).
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ISO 8601: 2026-07-10T08:00:00.000Z / 2026-07-10 08:00:00+01:00 / ...T08:00:00
_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ]"
    r"(\d{2}):(\d{2}):(\d{2})"
    r"(?:[.,](\d{1,9}))?"
    r"(Z|[+-]\d{2}:?\d{2})?"
)

# syslog: Jul 10 08:00:00 (no year -> assume the current year)
_SYSLOG_RE = re.compile(
    r"([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})"
)

# Bare epoch seconds or milliseconds, delimited so we do not grab a random
# digit run out of the middle of a request id. 10 digits = seconds (~2001-2286),
# 13 digits = milliseconds.
_EPOCH_RE = re.compile(r"(?<![\d.])(\d{13}|\d{10})(?:\.(\d{1,9}))?(?![\d])")


def _frac_to_seconds(frac):
    # type: (str) -> float
    """Convert a fractional-seconds digit string to a float in [0, 1)."""
    if not frac:
        return 0.0
    # Pad/truncate to nanosecond precision then scale (avoids float("0."+frac)
    # surprises on very long fraction strings).
    frac = (frac + "000000000")[:9]
    return int(frac) / 1_000_000_000.0


def _offset_seconds(offset):
    # type: (str) -> float
    """Seconds to subtract to reach UTC for an ISO offset (``Z`` or ``+HH:MM``)."""
    if not offset or offset in ("Z", "z"):
        return 0.0
    sign = 1 if offset[0] == "+" else -1
    body = offset[1:].replace(":", "")
    hours = int(body[0:2])
    minutes = int(body[2:4]) if len(body) >= 4 else 0
    return sign * (hours * 3600 + minutes * 60)


def _iso_to_epoch(m):
    # type: (re.Match) -> float
    year, month, day, hh, mm, ss, frac, offset = m.groups()
    dt = datetime.datetime(int(year), int(month), int(day),
                           int(hh), int(mm), int(ss))
    epoch = (dt - _EPOCH).total_seconds() + _frac_to_seconds(frac)
    # A wall-clock in a +01:00 zone is an earlier UTC instant: subtract the offset.
    epoch -= _offset_seconds(offset)
    return epoch


def _syslog_to_epoch(m, now_year):
    # type: (re.Match, int) -> float
    mon, day, hh, mm, ss = m.groups()
    month = _MONTHS.get(mon.lower())
    if month is None:
        raise ValueError("unknown month %r" % mon)
    dt = datetime.datetime(now_year, month, int(day), int(hh), int(mm), int(ss))
    return (dt - _EPOCH).total_seconds()


def _epoch_match_to_epoch(m):
    # type: (re.Match) -> float
    digits, frac = m.group(1), m.group(2)
    if len(digits) == 13:  # milliseconds
        value = int(digits) / 1000.0
    else:                  # seconds
        value = float(digits)
    if frac:
        value += _frac_to_seconds(frac)
    return value


class TimestampParser(object):
    """Extract an epoch-seconds float from a raw line (best effort).

    Construction options:

    * ``regex`` — an operator-supplied pattern. Its first capturing group (or the
      whole match when it has none) is the timestamp text, parsed with
      ``strptime_fmt`` when given, else fed through the built-in shape parsers.
    * ``strptime_fmt`` — a :func:`time.strptime` format applied to the regex
      capture (only meaningful with ``regex``). Assumed UTC.

    Without a regex the built-in battery (ISO, syslog, epoch) is scanned.
    """

    def __init__(self, regex=None, strptime_fmt=None):
        # type: (str, str) -> None
        self._user_re = re.compile(regex) if regex else None
        self._strptime_fmt = strptime_fmt
        # UTC "current year" for syslog timestamps (which carry no year). Built
        # timezone-aware then read .year so it is correct on both py3.9 and 3.12
        # without the deprecated datetime.utcnow().
        self._now_year = datetime.datetime.now(datetime.timezone.utc).year

    def parse(self, line):
        # type: (str) -> float
        """Return epoch seconds for ``line``, or ``None`` if not parseable."""
        if self._user_re is not None:
            m = self._user_re.search(line)
            if m is None:
                return None
            text = m.group(1) if m.groups() else m.group(0)
            return self._parse_text(text)
        return self._parse_builtin(line)

    def _parse_text(self, text):
        # type: (str) -> float
        text = text.strip()
        if self._strptime_fmt:
            try:
                struct = time.strptime(text, self._strptime_fmt)
            except ValueError:
                return None
            # calendar.timegm without importing calendar: treat as UTC.
            dt = datetime.datetime(*struct[:6])
            return (dt - _EPOCH).total_seconds()
        # No explicit format: run the captured text through the shape parsers.
        return self._parse_builtin(text)

    def _parse_builtin(self, line):
        # type: (str) -> float
        m = _ISO_RE.search(line)
        if m is not None:
            try:
                return _iso_to_epoch(m)
            except ValueError:
                pass
        m = _SYSLOG_RE.search(line)
        if m is not None:
            try:
                return _syslog_to_epoch(m, self._now_year)
            except ValueError:
                pass
        m = _EPOCH_RE.search(line)
        if m is not None:
            try:
                return _epoch_match_to_epoch(m)
            except ValueError:
                pass
        return None
