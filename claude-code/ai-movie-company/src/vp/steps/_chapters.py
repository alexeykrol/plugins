"""YouTube chapters from start quotes (SPEC S6b): the model names chapters by a verbatim quote of
their first words; code finds each quote in final.srt and takes the time — the model never sees
or writes a time.

Rules (SPEC S6, S9): first chapter at 00:00, chapters ascending, each ≥ 10 s long (the last one
too, up to the end of the video), at least 3 — otherwise no chapters at all (with a reason).

Public API:
  normalize_words(text) -> list[str]
  find_quote(cues, quote, from_cue=0, min_words=4) -> int | None        cue index of the quote start
  compute_chapters(cues, proposals, duration_ms, min_gap_ms=10000, min_count=3)
      -> (chapters [{start_ms, title}], dropped [{title, reason}], drop_all_reason | None)
  format_chapters(chapters) -> "00:00 Title\\n01:23 Title"
  chapter_violations(chapters, duration_ms, min_gap_ms=10000, min_count=3) -> list[str]   (S9)
"""
from __future__ import annotations

import re
from typing import Any, Sequence

from ..core.srt import Cue
from ..core.timefmt import fmt_clock
from ._limits import has_forbidden

_WORD_RE = re.compile(r"[0-9a-zа-я]+", re.I)
CHAPTER_TITLE_MAX = 80


def normalize_words(text: str) -> list[str]:
    """Lower-case words without punctuation; ё -> е (recognizer/proofreader drift)."""
    return _WORD_RE.findall((text or "").lower().replace("ё", "е"))


def _word_stream(cues: Sequence[Cue]) -> tuple[list[str], list[int]]:
    words: list[str] = []
    owner: list[int] = []
    for i, c in enumerate(cues):
        for w in normalize_words(c.text):
            words.append(w)
            owner.append(i)
    return words, owner


def _find_seq(words: list[str], seq: list[str], start: int) -> int | None:
    n = len(seq)
    if n == 0:
        return None
    for k in range(start, len(words) - n + 1):
        if words[k:k + n] == seq:
            return k
    return None


def find_quote(cues: Sequence[Cue], quote: str, from_cue: int = 0, min_words: int = 4) -> int | None:
    """Index of the cue where `quote` starts (searching from cue `from_cue`), or None.

    Normalised word match; the whole quote is tried first, then shorter prefixes down to
    `min_words` words (a model may misquote the tail). A quote shorter than `min_words` words
    must match completely.
    """
    words, owner = _word_stream(cues)
    seq = normalize_words(quote)
    if not seq:
        return None
    start = next((k for k, o in enumerate(owner) if o >= from_cue), len(words))
    lengths = [len(seq)] + list(range(min(len(seq) - 1, 12), min_words - 1, -1))
    for n in lengths:
        if n <= 0:
            continue
        k = _find_seq(words, seq[:n], start)
        if k is not None:
            return owner[k]
    return None


def _clean_title(title: Any) -> str | None:
    if not isinstance(title, str):
        return None
    t = " ".join(title.split())
    if not t or has_forbidden(t) or len(t) > CHAPTER_TITLE_MAX:
        return None
    return t


def compute_chapters(cues: Sequence[Cue], proposals: Sequence[dict[str, Any]], duration_ms: int,
                     min_gap_ms: int = 10_000, min_count: int = 3
                     ) -> tuple[list[dict[str, Any]], list[dict[str, str]], str | None]:
    """Chapters from model proposals [{quote, title}] on the final timeline (cues = final.srt).

    Bookkeeping, all in code: quote -> cue start; out-of-order / duplicate / unknown quotes are
    dropped with a reason; the first chapter is moved to 00:00 (it covers the intro card);
    a chapter starting < min_gap_ms after the previous one or < min_gap_ms before the end is
    dropped. Fewer than `min_count` survivors -> no chapters (drop_all_reason set).
    """
    dropped: list[dict[str, str]] = []
    found: list[dict[str, Any]] = []
    cursor = 0
    for p in proposals:
        title = _clean_title(p.get("title") if isinstance(p, dict) else None)
        quote = p.get("quote") if isinstance(p, dict) else None
        label = title or str((p or {}).get("title") if isinstance(p, dict) else p)[:60]
        if title is None:
            dropped.append({"title": label, "reason": "bad_title"})
            continue
        if not isinstance(quote, str) or not quote.strip():
            dropped.append({"title": title, "reason": "no_quote"})
            continue
        idx = find_quote(cues, quote, from_cue=cursor)
        if idx is None:
            reason = "quote_out_of_order" if find_quote(cues, quote) is not None else "quote_not_found"
            dropped.append({"title": title, "reason": reason})
            continue
        found.append({"start_ms": cues[idx].start_ms, "title": title})
        cursor = idx + 1
    if found:
        found[0]["start_ms"] = 0
    chapters: list[dict[str, Any]] = []
    for ch in found:
        if chapters and ch["start_ms"] - chapters[-1]["start_ms"] < min_gap_ms:
            dropped.append({"title": ch["title"], "reason": "too_close"})
            continue
        if duration_ms - ch["start_ms"] < min_gap_ms:
            dropped.append({"title": ch["title"], "reason": "too_close_to_end"})
            continue
        chapters.append(ch)
    if len(chapters) < min_count:
        return [], dropped, f"fewer_than_{min_count}_chapters"
    return chapters, dropped, None


def format_chapters(chapters: Sequence[dict[str, Any]]) -> str:
    """Description block: one 'MM:SS Title' line per chapter."""
    return "\n".join(f"{fmt_clock(c['start_ms'])} {c['title']}" for c in chapters)


def chapter_violations(chapters: Sequence[dict[str, Any]], duration_ms: int,
                       min_gap_ms: int = 10_000, min_count: int = 3) -> list[str]:
    """Formal check of a chapter list (empty list = no chapters = ok)."""
    if not chapters:
        return []
    out = []
    if len(chapters) < min_count:
        out.append("chapters_too_few")
    if chapters[0].get("start_ms") != 0:
        out.append("chapters_first_not_zero")
    starts = [c.get("start_ms") for c in chapters]
    if any(not isinstance(s, int) for s in starts):
        return out + ["chapters_bad_time"]
    if any(b - a < min_gap_ms for a, b in zip(starts, starts[1:])):
        out.append("chapters_too_close")
    if any(b <= a for a, b in zip(starts, starts[1:])):
        out.append("chapters_not_ascending")
    if duration_ms - starts[-1] < min_gap_ms:
        out.append("chapters_last_too_short")
    return out
