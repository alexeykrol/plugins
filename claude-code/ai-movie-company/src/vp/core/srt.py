"""SRT parsing/formatting and millisecond helpers — the single implementation for all steps.

Times are integer milliseconds everywhere in code. Models never see them.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import VPError
from .io import atomic_write

_TS_RE = re.compile(r"^\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*$")
_ARROW_RE = re.compile(r"^\s*(\S+)\s*-->\s*(\S+)")
_SPEAKER_RE = re.compile(r"^Speaker (\d+):\s*")


@dataclass(frozen=True)
class Cue:
    """One subtitle. `id` is the original number as text; text lines joined with "\\n"."""

    id: str
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def shifted(self, delta_ms: int) -> "Cue":
        return replace(self, start_ms=self.start_ms + delta_ms, end_ms=self.end_ms + delta_ms)


def ts_to_ms(ts: str) -> int:
    """'HH:MM:SS,mmm' (or '.') -> ms. Fractions shorter than 3 digits are right-padded."""
    m = _TS_RE.match(ts)
    if not m:
        raise VPError("data", f"bad SRT timestamp {ts!r}")
    h, mi, s, frac = m.groups()
    if int(mi) > 59 or int(s) > 59:
        raise VPError("data", f"bad SRT timestamp {ts!r}")
    return ((int(h) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0"))


def ms_to_ts(ms: int) -> str:
    """ms -> 'HH:MM:SS,mmm'."""
    if ms < 0:
        raise VPError("data", f"negative time {ms}")
    h, ms = divmod(int(ms), 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, f = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{f:03d}"


def parse_srt(text: str, strip_speaker: bool = True) -> list[Cue]:
    """Parse SRT text. BOM/CRLF tolerated. With strip_speaker, a leading "Speaker N: " label is
    removed from the text and kept in `speaker`. Raises VPError(data) on malformed blocks."""
    raw = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        if not block.strip():
            continue
        lines = block.split("\n")
        if len(lines) < 2:
            raise VPError("data", f"SRT block too short: {block[:60]!r}")
        cid = lines[0].strip()
        m = _ARROW_RE.match(lines[1])
        if not cid or not m:
            raise VPError("data", f"SRT block without id/time line: {block[:60]!r}")
        start, end = ts_to_ms(m.group(1)), ts_to_ms(m.group(2))
        if end < start:
            raise VPError("data", f"SRT cue {cid}: end before start")
        body = "\n".join(lines[2:]).strip()
        speaker = None
        if strip_speaker:
            sm = _SPEAKER_RE.match(body)
            if sm:
                speaker = sm.group(1)
                body = body[sm.end():]
        cues.append(Cue(cid, start, end, body, speaker))
    return cues


def format_srt(cues: list[Cue], renumber: bool = False, with_speaker: bool = False) -> str:
    """Format cues as SRT text (ends with a newline). renumber: ids 1..n."""
    blocks = []
    for i, c in enumerate(cues, 1):
        cid = str(i) if renumber else c.id
        body = c.text
        if with_speaker and c.speaker is not None:
            body = f"Speaker {c.speaker}: {body}"
        blocks.append(f"{cid}\n{ms_to_ts(c.start_ms)} --> {ms_to_ts(c.end_ms)}\n{body}")
    return "\n\n".join(blocks) + "\n" if blocks else ""


def read_srt(path: str | os.PathLike, strip_speaker: bool = True) -> list[Cue]:
    """Read and parse an SRT file (UTF-8, BOM tolerated)."""
    p = Path(path)
    if not p.exists():
        raise VPError("data", f"SRT not found: {p}")
    return parse_srt(p.read_text(encoding="utf-8-sig"), strip_speaker=strip_speaker)


def write_srt(path: str | os.PathLike, cues: list[Cue], renumber: bool = False,
              with_speaker: bool = False) -> Path:
    """Atomically write cues as SRT."""
    return atomic_write(path, format_srt(cues, renumber=renumber, with_speaker=with_speaker))


def slice_cues(cues: list[Cue], start_ms: int, end_ms: int, rebase: bool = True) -> list[Cue]:
    """Cues overlapping [start_ms, end_ms), clipped to the window; rebased to 0 if `rebase`."""
    out = []
    for c in cues:
        if c.end_ms <= start_ms or c.start_ms >= end_ms:
            continue
        s, e = max(c.start_ms, start_ms), min(c.end_ms, end_ms)
        if e <= s:
            continue
        nc = replace(c, start_ms=s, end_ms=e)
        out.append(nc.shifted(-start_ms) if rebase else nc)
    return out


def srt_duration_ms(cues: list[Cue]) -> int:
    """End of the last cue (0 if empty)."""
    return max((c.end_ms for c in cues), default=0)
