"""S1 bookkeeping in code: recognizer-hallucination filter, chunking, meaning-risk signal.

All of this runs without a model (SPEC S1: the filter runs BEFORE the model; meaning_risk is a code
signal, not a verdict).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

# ---- hallucination filter ----------------------------------------------------------------------

# Prompt echo of the transcription prompt (session 11-16: 83 of 1311 cues «Сохраняй все повторы…»).
# Each pattern matches the echoed span; a cue whose remainder has no letters is dropped entirely,
# otherwise the echoed span is cut out before the model sees the cue.
ECHO_PATTERNS: tuple[str, ...] = (
    r"сохраняй\s+все\s+(?:повторы|паузы|слова|паразит\w*)[^.!?…]*[.!?…]*",
    r"сохраняй\s+(?:слова-)?паразит\w*[^.!?…]*[.!?…]*",
)
# Subtitle-editor credits and similar recognizer inventions — match to the end of the cue.
CREDIT_PATTERNS: tuple[str, ...] = (
    r"редактор\s+субтитров.*$",
    r"корректор\s+[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]?\.?\s*[А-ЯЁA-Z][а-яёa-z]+.*$",
    r"субтитры\s+(?:сделал|создавал|делал|подготовил|создал)\w*.*$",
)

_LETTER = re.compile(r"[^\W\d_]")


def compile_patterns(extra: Iterable[str] = ()) -> dict[str, list[re.Pattern]]:
    """{"echo": [...], "credit": [...]} compiled case-insensitive; `extra` adds echo patterns."""
    return {"echo": [re.compile(p, re.I) for p in (*ECHO_PATTERNS, *extra)],
            "credit": [re.compile(p, re.I | re.S) for p in CREDIT_PATTERNS]}


def strip_garbage(text: str, pats: dict[str, list[re.Pattern]]) -> tuple[str, str | None]:
    """Remove echo/credit spans. Returns (remaining text, reason) where reason is "echo"/"credit"
    if something was removed (remaining "" = whole cue is garbage) or None."""
    reason = None
    out = text
    for kind in ("echo", "credit"):
        for p in pats[kind]:
            new = p.sub(" ", out)
            if new != out:
                reason = reason or kind
                out = new
    if reason is None:
        return text, None
    out = re.sub(r"\s+", " ", out).strip()
    if not _LETTER.search(out):
        out = ""
    return out, reason


def contains_garbage(text: str, pats: dict[str, list[re.Pattern]]) -> bool:
    """True if any echo/credit pattern occurs in `text` (acceptance: echo patterns = 0 after)."""
    return any(p.search(text) for kind in ("echo", "credit") for p in pats[kind])


def speech_end_ms(silences: list[tuple[int, int]], duration_ms: int, tol_ms: int = 250) -> int | None:
    """Start of the trailing silence (silence reaching the end of the media), else None."""
    if not silences or duration_ms <= 0:
        return None
    s, e = max(silences, key=lambda x: x[1])
    if e >= duration_ms - tol_ms:
        return s
    return None


def filter_cues(cues: list[dict], pats: dict[str, list[re.Pattern]],
                speech_end: int | None) -> list[dict]:
    """Mark cues [{id,start_ms,end_ms,speaker,text}] in place: `filtered` = None | "echo" | "credit" |
    "after_speech"; `text` becomes the cleaned text (echo spans removed). Returns the list."""
    for c in cues:
        text, reason = strip_garbage(c["text"], pats)
        c["filtered"] = None
        if reason and not text:
            c["filtered"] = reason
        elif reason:
            c["cleaned"] = reason
            c["text"] = text
        if c["filtered"] is None and speech_end is not None and c["start_ms"] >= speech_end:
            c["filtered"] = "after_speech"
    return cues


def sanitize(text: str) -> str:
    """Cue text for the `ID|text` contract: one line, no '|'."""
    return re.sub(r"\s+", " ", text.replace("|", "/")).strip()


# ---- chunking ----------------------------------------------------------------------------------

def chunk(items: list[Any], size: int) -> list[list[Any]]:
    """Split into ceil(n/size) chunks of nearly equal length (each <= size)."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    n = len(items)
    if n == 0:
        return []
    k = -(-n // size)
    base, rem = divmod(n, k)
    out, i = [], 0
    for j in range(k):
        m = base + (1 if j < rem else 0)
        out.append(items[i:i + m])
        i += m
    return out


# ---- meaning risk --------------------------------------------------------------------------------

FILLERS: tuple[str, ...] = (
    "как бы", "так сказать", "в общем", "в общем-то", "то есть", "это самое", "короче говоря",
    "ну", "вот", "типа", "значит", "короче", "собственно", "блин", "э", "ээ", "эээ", "эм", "м", "мм",
    "ммм", "а", "ага", "угу", "слушай", "смотри", "получается", "просто", "так", "да",
)
NEGATIONS: frozenset[str] = frozenset({
    "не", "нет", "ни", "никогда", "нельзя", "ничего", "ничто", "никто", "нигде", "никуда", "никак",
    "никакой", "никакая", "никакое", "никакие", "ниоткуда", "нисколько", "незачем", "некогда",
})

_TOKEN = re.compile(r"[^\W_]+(?:-[^\W_]+)*")


def tokens(text: str, fillers: Iterable[str] = FILLERS) -> list[str]:
    """Lower-case word tokens (ё→е), punctuation dropped, filler words/phrases removed."""
    t = " " + text.lower().replace("ё", "е") + " "
    toks = _TOKEN.findall(t)
    multi = sorted((f.replace("ё", "е").split() for f in fillers if " " in f), key=len, reverse=True)
    single = {f.replace("ё", "е") for f in fillers if " " not in f}
    out: list[str] = []
    i = 0
    while i < len(toks):
        for m in multi:
            if toks[i:i + len(m)] == m:
                i += len(m)
                break
        else:
            if toks[i] not in single:
                out.append(toks[i])
            i += 1
    return out


def _known(word: str, pool: set[str]) -> bool:
    """Same word or the same stem (shared prefix >= 4 for words of 4+ letters: inflection fixes)."""
    if word in pool:
        return True
    if len(word) >= 4:
        stem = word[:4]
        return any(len(p) >= 4 and p[:4] == stem for p in pool)
    return False


def meaning_risk(before: str, after: str, names: Iterable[str] = (),
                 fillers: Iterable[str] = FILLERS) -> dict[str, Any] | None:
    """Code signal for SPEC S1 «сохранение смысла»: words ADDED by the model that are absent from
    the source cue and from the names dictionary, and changes in negations. None = no risk.
    An emptied cue is not a meaning risk (it is counted as emptied)."""
    if not after.strip():
        return None
    a = tokens(before, fillers)
    b = tokens(after, fillers)
    pool = set(a)
    name_pool = {w for n in names for w in tokens(str(n), ())}
    added = [w for w in b if not _known(w, pool) and not _known(w, name_pool)]
    na = Counter(w for w in a if w in NEGATIONS)
    nb = Counter(w for w in b if w in NEGATIONS)
    neg_changed = na != nb
    if not added and not neg_changed:
        return None
    out: dict[str, Any] = {}
    if added:
        out["added"] = added
    if neg_changed:
        out["negation"] = {"before": sum(na.values()), "after": sum(nb.values())}
    return out
