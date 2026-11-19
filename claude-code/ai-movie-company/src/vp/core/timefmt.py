"""Boundary forms a human may write (S3b review): parse and classify only.

  "#412"                    subtitle number           -> Boundary(kind="cue", cue=412)
  "1:24:20", "84:20", "5020.5", "412"
                            time (h:m:s, m:s, seconds) -> Boundary(kind="time", ms=...)
  anything else             a quote from the speech    -> Boundary(kind="quote", quote=...)

Matching quotes or cue numbers to subtitles is NOT done here (that belongs to the steps).
Also: clock formatting for YouTube chapters.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CUE_RE = re.compile(r"^#\s*(\d+)$")
_TIME_RE = re.compile(r"^(\d+)(?::(\d{1,2}))?(?::(\d{1,2}))?(?:[.,](\d+))?$")
_QUOTES = ("«»", '""', "''", "“”", "„“", "‘’")


@dataclass(frozen=True)
class Boundary:
    kind: str                 # "cue" | "time" | "quote"
    cue: int | None = None
    ms: int | None = None
    quote: str | None = None


def parse_time(text: str) -> int:
    """'1:24:20' | '84:20' | '5020.5' | '412' -> ms. Raises ValueError.

    In h:m:s, minutes and seconds must be < 60; in m:s, minutes may exceed 59 ('84:20');
    seconds < 60 whenever a colon is present.
    """
    s = text.strip()
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError(f"not a time: {text!r}")
    a, b, c, frac = m.groups()
    frac_ms = int((frac or "0")[:3].ljust(3, "0"))
    if b is None:
        total_s = int(a)
    elif c is None:
        if int(b) > 59:
            raise ValueError(f"seconds out of range: {text!r}")
        total_s = int(a) * 60 + int(b)
    else:
        if int(b) > 59 or int(c) > 59:
            raise ValueError(f"minutes/seconds out of range: {text!r}")
        total_s = int(a) * 3600 + int(b) * 60 + int(c)
    return total_s * 1000 + frac_ms


def _unquote(s: str) -> str:
    s = s.strip()
    for pair in _QUOTES:
        if len(s) >= 2 and s[0] == pair[0] and s[-1] == pair[1]:
            return s[1:-1].strip()
    return s


def parse_boundary(value: str | int | float) -> Boundary:
    """Classify a boundary written by a human. Raises ValueError for an empty value.

    Numbers from YAML (int/float) are seconds.
    """
    if isinstance(value, bool):
        raise ValueError("boundary must not be a boolean")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("negative time")
        return Boundary("time", ms=int(round(float(value) * 1000)))
    s = str(value).strip()
    if not s:
        raise ValueError("empty boundary")
    m = _CUE_RE.match(s)
    if m:
        return Boundary("cue", cue=int(m.group(1)))
    try:
        return Boundary("time", ms=parse_time(s))
    except ValueError:
        pass
    q = _unquote(s)
    if not q:
        raise ValueError("empty quote")
    return Boundary("quote", quote=q)


def fmt_clock(ms: int) -> str:
    """YouTube chapter clock: 'MM:SS' under an hour, 'H:MM:SS' otherwise (0 -> '00:00')."""
    total = max(0, int(ms)) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
