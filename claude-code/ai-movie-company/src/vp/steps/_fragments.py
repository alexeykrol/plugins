"""Fragment bookkeeping shared by S3a and S3b (SPEC S3a "Код (бухгалтерия)", S3b).

  * quote matching: normalized token comparison of a model/human quote against subtitles.srt
    (exact token run first, then a tolerant SequenceMatcher fallback, ratio >= FUZZY_MIN);
  * the three boundary forms of a human ("#412" subtitle number, "1:24:20" time, a quote);
  * boundary normalization by mode:
      words     (_cut.json word timings): entry = first word start - 0.3 s, exit = last word end
                + 0.5 s, each snapped to the nearest pause >= 250 ms within 1.5 s; never mid-word;
      srt_only  entry = start of the subtitle holding the first word, exit = end of the subtitle
                holding the last word; fragment marked `boundary: subtitle`;
  * duration ranges per type and overlap (intersection / shorter duration) within a type.

Models never see times; all times here are integer milliseconds.
"""
from __future__ import annotations

import bisect
import difflib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import VPError
from ..core.srt import Cue
from ..core.timefmt import Boundary, parse_boundary

PRE_MS = 300          # entry lead before the first word
POST_MS = 500         # exit tail after the last word
PAUSE_MIN_MS = 250    # a pause to snap to
SNAP_MS = 1500        # search radius for a pause
EDGE_MS = 150         # keep the cut at least this far from the word next to the pause
FUZZY_MIN = 0.85      # SequenceMatcher ratio for a tolerant quote match
ALIGN_K = 5           # quote words used to align against word timings
OVERLAP_MAX = 0.5     # max overlap share inside a type
TYPES = ("reel", "long")
TYPE_PREFIX = {"reel": "r", "long": "l"}

_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
_CUE_PREFIX_RE = re.compile(r"^\s*#\d+\s+")


class BoundaryError(Exception):
    """A boundary cannot be resolved. `reason` is a short code (quote_not_found, cue_not_found,
    bad_boundary, end_before_start, out_of_range)."""

    def __init__(self, reason: str, msg: str):
        super().__init__(msg)
        self.reason = reason
        self.msg = msg


# ---- text normalization ---------------------------------------------------------------------

def norm_tokens(text: str) -> list[str]:
    """Lowercase, ё->е, punctuation removed, split on whitespace."""
    t = (text or "").lower().replace("ё", "е")
    t = _WORD_RE.sub(" ", t).replace("_", " ")
    return t.split()


def norm_text(text: str) -> str:
    return " ".join(norm_tokens(text))


def strip_cue_prefix(quote: str) -> str:
    """A model may copy the "#12 " subtitle marker into a quote; drop it."""
    return _CUE_PREFIX_RE.sub("", quote or "")


def word_count(text: str) -> int:
    return len(norm_tokens(strip_cue_prefix(text)))


# ---- word timings ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Word:
    norm: str
    start_ms: int
    end_ms: int


def load_words(path: str | Path) -> list[Word] | None:
    """Words of a Whisper verbose_json (`segments[].words[] {word,start,end}` in seconds, or a
    top-level `words` list). None if the file is absent or has no word timings."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise VPError("data", f"{p.name}: not valid JSON: {e}") from e
    raw: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for seg in data.get("segments") or []:
            raw.extend((seg or {}).get("words") or [])
        raw.extend(data.get("words") or [])
    out: list[Word] = []
    for w in raw:
        try:
            s, e = int(round(float(w["start"]) * 1000)), int(round(float(w["end"]) * 1000))
        except (KeyError, TypeError, ValueError) as ex:
            raise VPError("data", f"{p.name}: bad word timing {w!r}") from ex
        for tok in norm_tokens(str(w.get("word", ""))):
            out.append(Word(tok, s, max(s, e)))
    out.sort(key=lambda w: (w.start_ms, w.end_ms))
    return out or None


# ---- the timeline -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Point:
    """A resolved boundary."""
    ms: int
    cue: int                  # index into Timeline.cues of the subtitle holding the boundary word
    approx: bool = False      # words mode: quote words not aligned, nearest word used


@dataclass(frozen=True)
class Span:
    tok_first: int
    tok_last: int
    ratio: float


class Timeline:
    """Subtitles (+ optional word timings) of one recording; resolves boundaries.

    mode = "words" when `words` is given, else "srt_only".
    """

    def __init__(self, cues: list[Cue], words: list[Word] | None = None, end_limit_ms: int | None = None):
        if not cues:
            raise VPError("data", "no subtitles")
        self.cues = list(cues)
        self.words = list(words) if words else []
        self.mode = "words" if self.words else "srt_only"
        self.tokens: list[str] = []
        self.tok_cue: list[int] = []
        self.tok_pos: list[int] = []
        self.cue_ntok: list[int] = []
        self.cue_first_tok: list[int] = []
        for ci, c in enumerate(self.cues):
            toks = norm_tokens(c.text)
            self.cue_first_tok.append(len(self.tokens))
            self.cue_ntok.append(len(toks))
            for pi, t in enumerate(toks):
                self.tokens.append(t)
                self.tok_cue.append(ci)
                self.tok_pos.append(pi)
        self._positions: dict[str, list[int]] = {}
        for i, t in enumerate(self.tokens):
            self._positions.setdefault(t, []).append(i)
        last = max(c.end_ms for c in self.cues)
        if self.words:
            last = max(last, self.words[-1].end_ms)
        self.end_limit_ms = end_limit_ms if end_limit_ms else last + POST_MS + SNAP_MS
        self._wstarts = [w.start_ms for w in self.words]
        self._wends = [w.end_ms for w in self.words]
        self.gaps: list[tuple[int, int]] = []
        if self.words:
            if self.words[0].start_ms >= PAUSE_MIN_MS:
                self.gaps.append((0, self.words[0].start_ms))
            run_end = self.words[0].end_ms
            for w in self.words[1:]:
                if w.start_ms - run_end >= PAUSE_MIN_MS:
                    self.gaps.append((run_end, w.start_ms))
                run_end = max(run_end, w.end_ms)
            if self.end_limit_ms - run_end >= PAUSE_MIN_MS:
                self.gaps.append((run_end, self.end_limit_ms))
        self._cue_by_id = {c.id: i for i, c in enumerate(self.cues)}

    @property
    def boundary_kind(self) -> str:
        return "words" if self.mode == "words" else "subtitle"

    # ---- quotes ------------------------------------------------------------------------------
    def find_quote(self, quote: str, min_tok: int = 0) -> Span | None:
        """First occurrence (at or after token `min_tok`) of the quote in the subtitles."""
        q = norm_tokens(strip_cue_prefix(quote))
        if not q:
            return None
        L, n = len(q), len(self.tokens)
        for i in self._positions.get(q[0], []):
            if i >= min_tok and i + L <= n and self.tokens[i:i + L] == q:
                return Span(i, i + L - 1, 1.0)
        # tolerant fallback: candidate starts anchored on any of the first 3 quote words
        cands: set[int] = set()
        for j, t in enumerate(q[:3]):
            for i in self._positions.get(t, []):
                s = i - j
                if s >= min_tok:
                    cands.add(s)
        best: Span | None = None
        for s in sorted(cands):
            for L2 in {L, L - 1, L + 1}:
                if L2 <= 0 or s + L2 > n:
                    continue
                r = difflib.SequenceMatcher(None, q, self.tokens[s:s + L2], autojunk=False).ratio()
                if r >= FUZZY_MIN and (best is None or r > best.ratio):
                    best = Span(s, s + L2 - 1, r)
        return best

    # ---- word alignment ---------------------------------------------------------------------
    def _est_ms(self, tok: int, after: bool) -> int:
        ci = self.tok_cue[tok]
        c = self.cues[ci]
        n = max(1, self.cue_ntok[ci])
        frac = (self.tok_pos[tok] + (1 if after else 0)) / n
        return int(c.start_ms + frac * c.duration_ms)

    def _word_range(self, lo: int, hi: int) -> range:
        a = bisect.bisect_left(self._wstarts, lo)
        b = bisect.bisect_right(self._wstarts, hi)
        return range(a, b)

    def _align(self, q: list[str] | None, est: int, ci: int, forward: bool) -> tuple[int, bool]:
        """Index of the boundary word: for a start, the word matching q[0]; for an end, the word
        matching q[-1]. Searched among words near the subtitle `ci`."""
        c = self.cues[ci]
        cand = self._word_range(c.start_ms - 2000, c.end_ms + 2000)
        if not cand:
            i = min(range(len(self.words)), key=lambda k: abs(self.words[k].start_ms - est))
            return i, True
        best_i, best_score = -1, -1
        if q:
            k = min(ALIGN_K, len(q))
            for i in cand:
                score = 0
                for j in range(k):
                    wi = i + j if forward else i - j
                    if 0 <= wi < len(self.words) and self.words[wi].norm == (q[j] if forward else q[-1 - j]):
                        score += 1
                if score > best_score or (score == best_score and
                                          abs(self._wtime(i, forward) - est) < abs(self._wtime(best_i, forward) - est)):
                    best_i, best_score = i, score
            if best_score >= min(2, k):
                return best_i, False
        i = min(cand, key=lambda k_: abs(self._wtime(k_, forward) - est))
        return i, bool(q)

    def _wtime(self, i: int, forward: bool) -> int:
        return self.words[i].start_ms if forward else self.words[i].end_ms

    # ---- entry / exit in words mode ---------------------------------------------------------
    def inside_word(self, ms: int) -> bool:
        """True if `ms` lies strictly inside a word (mid-word cut)."""
        i = bisect.bisect_right(self._wstarts, ms) - 1
        while i >= 0 and self.words[i].start_ms > ms - 60_000:
            w = self.words[i]
            if w.start_ms < ms < w.end_ms:
                return True
            i -= 1
        return False

    def _word_containing(self, ms: int) -> Word | None:
        i = bisect.bisect_right(self._wstarts, ms) - 1
        while i >= 0 and self.words[i].start_ms > ms - 60_000:
            w = self.words[i]
            if w.start_ms < ms < w.end_ms:
                return w
            i -= 1
        return None

    def entry_ms(self, wi: int) -> int:
        w = self.words[wi]
        t = w.start_ms - PRE_MS
        best = None
        for gs, ge in self.gaps:
            if ge > w.start_ms or ge < t - SNAP_MS:
                continue
            d = 0 if gs <= t <= ge else min(abs(t - gs), abs(t - ge))
            if d <= SNAP_MS and (best is None or d < best[0] or (d == best[0] and ge > best[2])):
                best = (d, gs, ge)
        if best:
            _, gs, ge = best
            margin = min(EDGE_MS, (ge - gs) // 2)
            cut = min(max(t, gs + margin), ge - margin)
        else:
            cut = t
            x = self._word_containing(cut)
            if x is not None:
                cut = x.end_ms
        return max(0, cut)

    def exit_ms(self, wi: int) -> int:
        w = self.words[wi]
        t = w.end_ms + POST_MS
        best = None
        for gs, ge in self.gaps:
            if gs < w.end_ms or gs > t + SNAP_MS:
                continue
            d = 0 if gs <= t <= ge else min(abs(t - gs), abs(t - ge))
            if d <= SNAP_MS and (best is None or d < best[0] or (d == best[0] and gs < best[1])):
                best = (d, gs, ge)
        if best:
            _, gs, ge = best
            margin = min(EDGE_MS, (ge - gs) // 2)
            cut = max(min(t, ge - margin), gs + margin)
        else:
            cut = t
            x = self._word_containing(cut)
            if x is not None:
                cut = x.start_ms
        return min(cut, self.end_limit_ms)

    # ---- boundary resolution ----------------------------------------------------------------
    def _cue_index(self, number: int) -> int:
        ci = self._cue_by_id.get(str(number))
        if ci is None:
            raise BoundaryError("cue_not_found", f"subtitle #{number} not found")
        return ci

    def _tok_after_ms(self, ms: int) -> int:
        for ci, c in enumerate(self.cues):
            if c.end_ms > ms and self.cue_ntok[ci]:
                return self.cue_first_tok[ci]
        return len(self.tokens)

    def resolve(self, value: Any, side: str, *, quote: bool = False, min_ms: int | None = None) -> tuple[Point, Span | None]:
        """Resolve one boundary. side: "start" | "end". quote=True forces the quote form (model
        output); otherwise the form is classified by vp.core.timefmt.parse_boundary.
        For an end quote, `min_ms` restricts the search to subtitles ending after it."""
        if side not in ("start", "end"):
            raise ValueError(side)
        if quote:
            b = Boundary("quote", quote=str(value))
        else:
            try:
                b = parse_boundary(value)
            except ValueError as e:
                raise BoundaryError("bad_boundary", f"bad {side} boundary {value!r}: {e}") from e
        start = side == "start"
        if b.kind == "quote":
            min_tok = self._tok_after_ms(min_ms) if min_ms is not None else 0
            span = self.find_quote(b.quote or "", min_tok)
            if span is None:
                raise BoundaryError("quote_not_found", f"{side} quote not found: {str(b.quote)[:80]!r}")
            tok = span.tok_first if start else span.tok_last
            ci = self.tok_cue[tok]
            if self.mode == "srt_only":
                c = self.cues[ci]
                return Point(c.start_ms if start else c.end_ms, ci), span
            q = norm_tokens(strip_cue_prefix(b.quote or ""))
            q = self.tokens[span.tok_first:span.tok_last + 1] or q
            wi, approx = self._align(q, self._est_ms(tok, after=not start), ci, forward=start)
            return Point(self.entry_ms(wi) if start else self.exit_ms(wi), ci, approx), span
        if b.kind == "cue":
            ci = self._cue_index(int(b.cue or 0))
            c = self.cues[ci]
            if self.mode == "srt_only":
                return Point(c.start_ms if start else c.end_ms, ci), None
            if start:
                rng = [i for i in self._word_range(c.start_ms - 200, c.end_ms)]
                wi = rng[0] if rng else self._nearest_word(c.start_ms)
                return Point(self.entry_ms(wi), ci, not rng), None
            rng = [i for i in self._word_range(c.start_ms, c.end_ms + 200) if self.words[i].end_ms <= c.end_ms + 200]
            wi = rng[-1] if rng else self._nearest_word(c.end_ms)
            return Point(self.exit_ms(wi), ci, not rng), None
        # time
        t = int(b.ms or 0)
        if self.mode == "srt_only":
            if start:
                for ci, c in enumerate(self.cues):
                    if c.end_ms > t:
                        return Point(c.start_ms, ci), None
                raise BoundaryError("out_of_range", f"start {value!r} is after the last subtitle")
            for ci in range(len(self.cues) - 1, -1, -1):
                if self.cues[ci].start_ms < t:
                    return Point(self.cues[ci].end_ms, ci), None
            raise BoundaryError("out_of_range", f"end {value!r} is before the first subtitle")
        if start:
            wi = bisect.bisect_right(self._wends, t)       # first word ending after t
            if wi >= len(self.words):
                raise BoundaryError("out_of_range", f"start {value!r} is after the last word")
            return Point(self.entry_ms(wi), self._cue_at(self.words[wi].start_ms)), None
        wi = bisect.bisect_left(self._wstarts, t) - 1      # last word starting before t
        if wi < 0:
            raise BoundaryError("out_of_range", f"end {value!r} is before the first word")
        return Point(self.exit_ms(wi), self._cue_at(self.words[wi].end_ms, end=True)), None

    def _nearest_word(self, ms: int) -> int:
        return min(range(len(self.words)), key=lambda k: abs(self.words[k].start_ms - ms))

    def _cue_at(self, ms: int, end: bool = False) -> int:
        """Index of the subtitle containing ms (or the nearest one)."""
        best, bd = 0, None
        for ci, c in enumerate(self.cues):
            if c.start_ms <= ms <= c.end_ms:
                return ci
            d = min(abs(c.start_ms - ms), abs(c.end_ms - ms))
            if bd is None or d < bd:
                best, bd = ci, d
        return best

    def fragment(self, start: Any, end: Any, *, quotes: bool = False) -> dict[str, Any]:
        """Resolve a (start, end) pair into {start_ms, end_ms, duration_ms, boundary, first_cue,
        last_cue, text, approx}. Raises BoundaryError."""
        return self.pair(start, end, quotes=quotes)

    def cue_index_of(self, cue_id: Any) -> int:
        ci = self._cue_by_id.get(str(cue_id))
        if ci is None:
            raise BoundaryError("cue_not_found", f"subtitle #{cue_id} not found (subtitles changed?)")
        return ci

    def pair(self, start: Any, end: Any, *, quotes: bool = False,
             base: dict[str, Any] | None = None) -> dict[str, Any]:
        """Like fragment(); a side given as None keeps the boundary of `base` (a fragment dict with
        start_ms, end_ms, first_cue, last_cue) — used by S3b `adjust` with one side changed."""
        if start is None:
            if base is None:
                raise BoundaryError("bad_boundary", "start boundary missing")
            sp = Point(int(base["start_ms"]), self.cue_index_of(base["first_cue"]))
        else:
            sp, _ = self.resolve(start, "start", quote=quotes)
        if end is None:
            if base is None:
                raise BoundaryError("bad_boundary", "end boundary missing")
            ep = Point(int(base["end_ms"]), self.cue_index_of(base["last_cue"]))
        else:
            min_ms = None
            if quotes or (isinstance(end, str) and parse_kind(end) == "quote"):
                min_ms = self.cues[sp.cue].start_ms - 1
            ep, _ = self.resolve(end, "end", quote=quotes, min_ms=min_ms)
        if ep.ms <= sp.ms or ep.cue < sp.cue:
            raise BoundaryError("end_before_start", "end boundary is not after the start boundary")
        text = " ".join(c.text.replace("\n", " ").strip() for c in self.cues[sp.cue:ep.cue + 1]
                        if c.text.strip())
        return {"start_ms": sp.ms, "end_ms": ep.ms, "duration_ms": ep.ms - sp.ms,
                "boundary": self.boundary_kind, "first_cue": self.cues[sp.cue].id,
                "last_cue": self.cues[ep.cue].id, "text": text,
                "approx": bool(sp.approx or ep.approx)}


def parse_kind(value: Any) -> str:
    try:
        return parse_boundary(value).kind
    except ValueError:
        return "bad"


# ---- ranges and overlaps ----------------------------------------------------------------------

def duration_range(profile: dict[str, Any], kind: str, human: bool = False) -> tuple[int, int]:
    """(min_ms, max_ms) of a fragment type from the resolved profile. `min_s` is what the model
    proposes; a person's own choice at the review gate (`human=True`) may go down to
    `human_min_s` when the profile sets it (owner's test drive 2026-11-18: a 25 s reel he picked
    was refused by code)."""
    r = profile.get(kind) or {}
    try:
        lo = float(r["min_s"])
        if human and r.get("human_min_s") is not None:
            lo = min(lo, float(r["human_min_s"]))
        return int(lo * 1000), int(float(r["max_s"]) * 1000)
    except (KeyError, TypeError, ValueError) as e:
        raise VPError("data", f"profile lacks {kind}.min_s/max_s") from e


def in_range(duration_ms: int, rng: tuple[int, int]) -> bool:
    return rng[0] <= duration_ms <= rng[1]


def overlap_share(a: tuple[int, int], b: tuple[int, int]) -> float:
    """Intersection / shorter duration (0..1)."""
    inter = min(a[1], b[1]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    shorter = min(a[1] - a[0], b[1] - b[0])
    return inter / shorter if shorter > 0 else 1.0


def overlapping_pairs(frags: list[dict[str, Any]], limit: float = OVERLAP_MAX) -> list[tuple[str, str, float]]:
    """Pairs of same-type fragments overlapping more than `limit`."""
    out = []
    for i, a in enumerate(frags):
        for b in frags[i + 1:]:
            if a["type"] != b["type"]:
                continue
            s = overlap_share((a["start_ms"], a["end_ms"]), (b["start_ms"], b["end_ms"]))
            if s > limit:
                out.append((str(a.get("id")), str(b.get("id")), round(s, 3)))
    return out


def expected_count(kind: str, recording_ms: int, profile: dict[str, Any]) -> int:
    """Expected number of strong fragments of a type for a recording length (profile
    `selection_expected_per_hour`, defaults reel 8 / long 2); at least 1."""
    per_hour = {"reel": 8.0, "long": 2.0}
    per_hour.update(profile.get("selection_expected_per_hour") or {})
    return max(1, int(round(float(per_hour.get(kind, 1.0)) * recording_ms / 3_600_000)))


def proposal_cap(kind: str, recording_ms: int, profile: dict[str, Any]) -> int:
    """Upper bound on proposals = ceil(expected × selection_margin) (SPEC S3a, margin default 1.5)."""
    margin = float(profile.get("selection_margin", 1.5))
    return max(1, math.ceil(expected_count(kind, recording_ms, profile) * margin))


def clock(ms: int) -> str:
    """hh:mm:ss for human-facing files."""
    s = max(0, int(ms)) // 1000
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def load_timeline(job, words_mode: str | None = None) -> Timeline:
    """Load the subtitles and usable source transcript word times for S3.

    An explicit words mode refers to a proposal whose boundaries already depend on those times.
    """
    from .. import transcribe
    from ..core.io import read_yaml
    from ..core.srt import read_srt
    from .s0_intake import INFERRED_WORD_TIMES, staged_word_times

    cues = read_srt(job.subtitles_srt)
    mode = words_mode
    if mode is None:
        jy = read_yaml(job.job_yaml, default={}) or {}
        mode = jy.get("boundary_mode") if isinstance(jy, dict) else None
    words = None
    if mode != "srt_only":
        timing = staged_word_times(job)
        words = [Word(token, start, end) for text, start, end, origin in timing.words
                 if origin != "inferred" for token in norm_tokens(text)]
        if not words:
            reason = timing.reason or INFERRED_WORD_TIMES
            if words_mode == "words":
                raise VPError("data", reason)
            if reason != transcribe.NO_WORD_TIMES:
                print(f"S3 is using subtitles only because {reason}.", flush=True)
    return Timeline(cues, words, end_limit_ms=_video_end_ms(job))


def _video_end_ms(job) -> int | None:
    from ..core.io import read_yaml
    jy = read_yaml(job.job_yaml, default={}) or {}
    if not isinstance(jy, dict):
        return None
    for node in (jy, jy.get("video") or {}, jy.get("source") or {}):
        if isinstance(node, dict):
            for k in ("duration_ms",):
                if isinstance(node.get(k), (int, float)) and node[k] > 0:
                    return int(node[k])
            if isinstance(node.get("duration_s"), (int, float)) and node["duration_s"] > 0:
                return int(node["duration_s"] * 1000)
    return None
