"""Word times for captions: the recogniser's words aligned to the proofread caption text.

Why (BACKLOG "Caption sync", incident 2026-11-23): the proofread subtitles know phrase times only.
A renderer that spreads the words of a phrase by their letters is right only while the speech is
even; a pause, a hesitation or a slow stretch inside the phrase moves every later word by up to
the length of the pause, so the highlighted word runs away from the voice.

Everything here is counting, so everything here is code (models never see a time):

    remap(words, start_ms, end_ms)        source-timeline words -> the clip's time base
    align(cues, words)                    every caption word gets a time: the recogniser's where the
                                          spelling matches (difflib on normalised words), else
                                          interpolated between its matched neighbours
    paged(words, ...) / page_cues(...)    small caption pages (<= 3 words / 18 chars) and the same
                                          pages as cues, so nothing is left to guess inside a cue
    sync_report(shown, heard, ...)        how far the words on screen are from the words in the
    sync_verdict(report, ...)             sound, and the gate: passed | failed | not_run

    write_clip_words / read_clip_words    `clip.words.json` next to `clip.srt`: the aligned word
                                          times of one clip, bound to the exact text of that
                                          clip.srt by its hash (a stale file is never used)

A recording that came with subtitles only has no word times at all. Then the pauses of the clip's
own sound are the only thing known locally (found by ffmpeg, vp.adapters.media.pauses):

    voiced_intervals(pauses, clip_ms)     where there is sound
    estimate_voiced(cues, voiced)         the words of a phrase placed on voiced time only, a
                                          pause always between two words; a phrase whose words
                                          cannot be placed safely is marked to be shown whole
    whole_phrases(cues)                   every phrase shown whole (the pauses are not known)
    voiced_report / voiced_verdict        the local gate: does a caption appear in a pause, ahead
                                          of the voice — passed | failed | not_run

What the pauses can and cannot do was measured on the 13 usable reels of the incident job
(Whisper word times of the clips as the truth, 247 phrases): see `VOICED_TIGHT_MS`.

A word is `(text, start_ms, end_ms)`, integer milliseconds. Imports only vp.core.
"""
from __future__ import annotations

import difflib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..core.io import read_json, sha256_bytes, write_json
from ..core.srt import Cue

Timed = tuple[str, int, int]

SCHEMA = 1
WORDS_NAME = "clip.words.json"
ESTIMATES = ("estimated_voiced", "estimated")     # `word_times` of a clip without real word times
MAX_WORDS = 3               # words on one caption page
MAX_CHARS = 18              # characters on one caption page (a longer single word stays alone)
MATCH_SLACK_MS = 1000       # a recogniser word further than this from its cue is another word
MIN_MATCH_SHARE = 0.5       # below this share of matched words the recogniser's words are not
                            # those of this text: not used at all
GUESS_MS_PER_CHAR = 80      # speaking pace assumed for a word the recogniser did not give
GUESS_MAX_MS = 600          # ... and the most one such word may take
PAUSE_BREAK_MS = 700        # a pause this long inside a phrase starts a new caption page
PAGE_HOLD_GAP_MS = 600      # a page stays up until the next one when the gap is shorter
PAGE_MIN_MS = 150

# QA gate (BACKLOG): the word on screen against the word in the sound
SYNC_P90_MS = 300
SYNC_MAX_MS = 800
SYNC_MIN_WORDS = 3          # fewer compared words than this is no measurement
SYNC_MIN_SHARE = 0.5        # so is a smaller share of the caption words

# ---- no word times: what the pauses of the sound allow ------------------------------------------
# How these were chosen (2026-11-23): the 13 usable wave-2 reels of the incident job (44–102 s
# each, 247 phrases, one speaker, a clean Zoom recording), Whisper word times of each clip as the
# truth, the caption gate's own measure (distance between a word on screen and the same word in
# the sound; p90 over a reel, mean over the reels):
#     words spread by letters over the phrase (the old estimate)      p90 1259 ms, 0 of 13 pass
#     words spread by letters over voiced time only, every phrase     p90 ~1230 ms, 0 of 13 pass
#     the phrase split at its pauses, each part shown whole           p90 ~400–550 ms, 6–10 pass
#     every phrase shown whole for its own time                       p90 288 ms, 11 of 13 pass
# ("pass" = the p90 limit of 300 ms. What is shipped — 238 phrases whole, 9 word-timed — gives
# p90 293 ms, 11 of 13; with the gate's second limit, no word further than 800 ms, 5 of 13: a
# single word said after its subtitle has gone is common in these subtitles.)
# In real speech most of the drift is not pauses (a phrase had a pause of 0.3 s or more in a
# minority of cases) but an uneven pace and words the proofreading removed, and letters do not
# say which side of a pause a word is on. So words are placed by the pauses only where the
# pauses leave almost nothing to guess; any other phrase is shown whole.
PAUSE_BELOW_MEAN_DB = 16.0  # a pause = the sound stays this far under the clip's mean level.
                            # 13 dB finds ~40% more "pauses" (quiet speech counted as silence);
                            # 20 dB loses a third of them once any room noise is mixed in; at 16
                            # the pauses of a reel stayed the same from -20 dB of gain to pink
                            # noise at -46 dBFS. Louder noise loses pauses: the safe direction
                            # (more phrases shown whole, less for the gate to see).
PAUSE_MIN_MS = 300          # shorter gaps are stops inside words and between fluent words; the
                            # results above did not change between 200 and 400 ms
VOICED_MIN_MS = 100         # a shorter sound between two pauses is a click or a breath
VOICED_TIGHT_MS = 1000      # a stretch of sound that holds several words of a phrase may be this
                            # long for its words to be timed one by one: then a word is at most
                            # about half a second from its place. With it 10 of the 247 phrases
                            # were word-timed and the reels' p90 stayed 295 ms; at 1500 ms it was
                            # 487 ms, at 2000 ms 658 ms.
SNAP_MARGIN_MS = 150        # the pause must fall between two words clearly: the next best place
                            # for it has to be this much (of voiced time) worse
CHAR_MS_MIN = 25            # less voiced time per letter than this is not the sound of these
                            # words (the incident phrases: 46–110 ms per letter)

# the local gate (`caption_voiced`): it sees pauses, not words
LEAD_MAX_MS = 1500          # a caption may appear in a pause this long before the sound comes
                            # back. Whole phrases of the 13 reels (11 of them in time by the
                            # recogniser): at most 777 ms, because a subtitle often starts in the
                            # breath before its phrase.
ON_SILENCE_MAX = 0.5        # share of caption-on-screen time that may fall on pauses; the 13
                            # reels: 0.03–0.14 whatever the timing, so this only catches
                            # captions that mostly sit on silence (subtitles of another sound)


# ---- words ------------------------------------------------------------------------------------

def norm(word: str) -> str:
    """Spelling used for matching: lower case, ё = е, letters and digits only."""
    return re.sub(r"[\W_]+", "", str(word).lower().replace("ё", "е"), flags=re.UNICODE)


def timed_words(words: Iterable[Any]) -> list[Timed]:
    """Recogniser words in any of the engine's shapes -> [(text, start_ms, end_ms)], sorted.
    Accepts (text, start, end) tuples, {"w"|"text"|"word", "start", "end"} dicts (ms) and objects
    with `.text/.start_ms/.end_ms`. A token holding several words shares its time among them."""
    out: list[Timed] = []
    for w in words or ():
        if isinstance(w, dict):
            text, s, e = w.get("w", w.get("text", w.get("word", ""))), w.get("start"), w.get("end")
        elif isinstance(w, (tuple, list)):
            text, s, e = w[0], w[1], w[2]
        else:
            text, s, e = getattr(w, "text", ""), getattr(w, "start_ms", None), getattr(w, "end_ms", None)
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)) or isinstance(s, bool):
            continue
        s, e = int(round(s)), int(round(e))
        for tok in str(text or "").split():
            out.append((tok, s, max(s, e)))
    out.sort(key=lambda x: (x[1], x[2]))
    return out


def remap(words: Iterable[Any], start_ms: int, end_ms: int) -> list[Timed]:
    """Words of the source recording that sound inside [start_ms, end_ms), on the clip's time base
    (0 = the clip start), clamped to the clip."""
    out: list[Timed] = []
    span = max(0, end_ms - start_ms)
    for text, s, e in timed_words(words):
        if e <= start_ms or s >= end_ms:
            continue
        out.append((text, max(0, s - start_ms), min(span, e - start_ms)))
    return out


def _keys(words: Sequence[str], tag: str) -> list[str]:
    """Normalised spellings; a token with no letters or digits gets a key nothing else has, so
    punctuation never anchors an alignment."""
    return [norm(w) or f"\0{tag}{i}" for i, w in enumerate(words)]


def _blocks(a: Sequence[str], b: Sequence[str]) -> list[tuple[int, int, int]]:
    sm = difflib.SequenceMatcher(None, _keys(a, "a"), _keys(b, "b"), autojunk=False)
    return [(m.a, m.b, m.size) for m in sm.get_matching_blocks() if m.size]


def _share(words: Sequence[str], lo: int, hi: int) -> list[tuple[int, int]]:
    """[lo, hi) split among `words` by letters + 1 (a stand-in for syllables): the guess used
    when a phrase has no word times at all."""
    weights = [len(w) + 1 for w in words]
    total = sum(weights) or 1
    span = max(0, hi - lo)
    out, acc = [], 0
    for i, wt in enumerate(weights):
        s = lo + round(span * acc / total)
        acc += wt
        e = hi if i == len(weights) - 1 else lo + round(span * acc / total)
        out.append((s, max(s, e)))
    return out


def _between(words: Sequence[str], lo: int, hi: int, lean_left: bool) -> list[tuple[int, int]]:
    """Times for a run of unmatched words between two known times. Each word gets the room its
    letters need at a speaking pace, never more: when the gap is longer than that it is a pause,
    and the words stay next to the word they follow (`lean_left`) or the word they precede —
    they are not stretched over the silence. A token without letters or digits (a dash) takes
    no time of its own."""
    room = [min(GUESS_MAX_MS, (len(norm(w)) + 1) * GUESS_MS_PER_CHAR) if norm(w) else 0 for w in words]
    total = sum(room)
    span = min(max(0, hi - lo), total)
    start = lo if lean_left else hi - span
    out, acc = [], 0
    for r in room:
        s = start + (round(span * acc / total) if total else 0)
        acc += r
        e = start + (round(span * acc / total) if total else 0)
        out.append((s, max(s, e)))
    return out


def estimate(cue: Cue) -> list[Timed]:
    """Words of a cue with times guessed from the phrase time alone (letters share). Integer ms,
    contiguous, the last word ends exactly at the cue end. The fallback when no word times exist."""
    words = (cue.text or "").split()
    return [(w, s, e) for w, (s, e) in zip(words, _share(words, cue.start_ms, cue.end_ms))]


@dataclass(frozen=True)
class Alignment:
    """Caption words with times, one list per input cue (same order, words as written)."""

    cues: list[list[Timed]]
    matched: int            # words whose time is the recogniser's
    total: int

    @property
    def share(self) -> float:
        return self.matched / self.total if self.total else 0.0

    @property
    def usable(self) -> bool:
        return self.total > 0 and self.share >= MIN_MATCH_SHARE

    def words(self) -> list[Timed]:
        return [w for c in self.cues for w in c]


def align(cues: Sequence[Cue], words: Iterable[Any], clip_ms: int | None = None) -> Alignment:
    """Give every word of the caption text a time.

    Matched words (same normalised spelling, in order, and sounding within MATCH_SLACK_MS of
    their cue) take the recogniser's time. The others are placed between their matched
    neighbours inside the cue (or the cue edges) at a speaking pace (`_between`); a cue with no
    matched word keeps the estimate. The result is monotonic and never overlaps; with `clip_ms`
    nothing ends after the clip."""
    heard = timed_words(words)
    toks: list[tuple[int, str]] = [(ci, w) for ci, c in enumerate(cues) for w in (c.text or "").split()]
    times: list[tuple[int, int] | None] = [None] * len(toks)
    for ia, ib, size in _blocks([w for _, w in toks], [w for w, _, _ in heard]):
        for k in range(size):
            cue = cues[toks[ia + k][0]]
            _, s, e = heard[ib + k]
            if cue.start_ms - MATCH_SLACK_MS <= s <= cue.end_ms + MATCH_SLACK_MS:
                times[ia + k] = (s, e)
    matched = sum(1 for t in times if t)

    flat: list[list[Any]] = []          # [cue index, word, start, end]
    pos = 0
    for ci, cue in enumerate(cues):
        n = len((cue.text or "").split())
        idx = list(range(pos, pos + n))
        pos += n
        if not any(times[i] for i in idx):
            flat += [[ci, w, s, e] for w, s, e in estimate(cue)]
            continue
        k = 0
        while k < n:
            i = idx[k]
            if times[i]:
                flat.append([ci, toks[i][1], *times[i]])
                k += 1
                continue
            run = k                                     # a run of unmatched words [run, k)
            while k < n and not times[idx[k]]:
                k += 1
            hi = times[idx[k]][0] if k < n else None
            lo = times[idx[run - 1]][1] if run else min(cue.start_ms, hi)
            hi = max(cue.end_ms, lo) if hi is None else max(hi, lo)
            gap_words = [toks[idx[j]][1] for j in range(run, k)]
            for w, (s, e) in zip(gap_words, _between(gap_words, lo, hi, lean_left=bool(run))):
                flat.append([ci, w, s, e])

    # monotonic starts, no overlaps, inside the clip
    last = 0
    for f in flat:
        f[2] = max(0, f[2], last)
        if clip_ms is not None:
            f[2] = min(f[2], clip_ms)
        last = f[2]
    for i, f in enumerate(flat):
        end = max(f[3], f[2])
        if i + 1 < len(flat):
            end = min(end, flat[i + 1][2])
        if clip_ms is not None:
            end = min(end, clip_ms)
        f[3] = max(end, f[2])
    out: list[list[Timed]] = [[] for _ in cues]
    for ci, w, s, e in flat:
        out[ci].append((w, int(s), int(e)))
    return Alignment(out, matched, len(toks))


# ---- no word times: the pauses of the sound -----------------------------------------------------

Span = tuple[int, int]


def voiced_intervals(pauses: Iterable[Span], clip_ms: int, min_ms: int = VOICED_MIN_MS) -> list[Span]:
    """Where there is sound in [0, clip_ms): everything outside the pauses. A stretch shorter
    than `min_ms` between two pauses is a click, not speech, and is left out."""
    out: list[Span] = []
    t = 0
    for a, b in sorted((int(a), int(b)) for a, b in pauses):
        a, b = max(0, a), min(int(clip_ms), b)
        if b <= a:
            continue
        if a > t:
            out.append((t, a))
        t = max(t, b)
    if clip_ms > t:
        out.append((t, int(clip_ms)))
    return [(a, b) for a, b in out if b - a >= min_ms]


@dataclass(frozen=True)
class Estimate:
    """Caption words of a clip that has no word times, one list per cue (words as written).

    whole   indices of the cues to show as a whole phrase for their own time: their words could
            not be placed safely, so each of them carries the cue's time
    why     how many cues are whole, by reason: no_sound | too_little_sound | unclear_pause |
            long_stretch | pauses_unknown"""

    cues: list[list[Timed]]
    whole: list[int]
    why: dict[str, int]

    @property
    def timed(self) -> int:
        """Cues whose words are timed one by one."""
        return len(self.cues) - len(self.whole)


def _weight(word: str) -> int:
    """Share of a word in its stretch of sound: letters + 1 (as `_share`); a token without
    letters or digits (a dash) takes no time."""
    n = len(norm(word))
    return n + 1 if n else 0


def _snap(weights: Sequence[int], segs: Sequence[Span]) -> list[list[int]] | None:
    """Which words sound in which stretch: every pause between two stretches is put between
    two words, at the word boundary nearest to it by letters. None when a pause has no clear
    place (the next best boundary is less than SNAP_MARGIN_MS worse): then the letters do not
    say which side of the pause a word is on."""
    n, weight_sum = len(weights), sum(weights)
    voiced = sum(b - a for a, b in segs)
    bounds, acc = [0.0], 0
    for wt in weights:
        acc += wt
        bounds.append(voiced * acc / weight_sum)
    groups: list[list[int]] = []
    i0, heard = 0, 0
    for a, b in segs[:-1]:
        heard += b - a
        places: dict[float, int] = {}                # distinct boundary -> its first word index
        for i in range(i0, n + 1):                   # (a dash adds no boundary of its own)
            places.setdefault(round(bounds[i], 3), i)
        ranked = sorted((abs(at - heard), i) for at, i in places.items())
        if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < SNAP_MARGIN_MS:
            return None
        i1 = ranked[0][1]
        groups.append(list(range(i0, i1)))
        i0 = i1
    groups.append(list(range(i0, n)))
    return groups


def _place(cue: Cue, words: Sequence[str], voiced: Sequence[Span]) -> tuple[list[Timed] | None, str | None]:
    """The words of one cue on the voiced time inside it, or (None, reason) when they cannot be
    placed safely."""
    weights = [_weight(w) for w in words]
    letters = sum(len(norm(w)) for w in words)
    segs = [(max(a, cue.start_ms), min(b, cue.end_ms)) for a, b in voiced
            if b > cue.start_ms and a < cue.end_ms]
    segs = [(a, b) for a, b in segs if b - a >= VOICED_MIN_MS]
    if not segs or not letters:
        return None, "no_sound"
    if sum(b - a for a, b in segs) < letters * CHAR_MS_MIN:
        return None, "too_little_sound"
    groups = _snap(weights, segs)
    if groups is None:
        return None, "unclear_pause"
    out: list[Timed] = []
    for (a, b), idx in zip(segs, groups):
        if sum(1 for i in idx if weights[i]) > 1 and b - a > VOICED_TIGHT_MS:
            return None, "long_stretch"
        total, acc = sum(weights[i] for i in idx), 0
        for i in idx:
            s = a + (round((b - a) * acc / total) if total else 0)
            acc += weights[i]
            e = a + (round((b - a) * acc / total) if total else 0)
            out.append((words[i], s, max(s, e)))
    return out, None


def estimate_voiced(cues: Sequence[Cue], voiced: Sequence[Span]) -> Estimate:
    """Caption words placed by the pauses of the sound, for a clip without word times.

    Inside a cue the words go on voiced time only, in proportion to their letters; a pause is
    always between two words, so no word (and no caption page) starts inside a pause. That is
    safe only where the pauses leave little to guess (see `VOICED_TIGHT_MS`); a cue is marked
    whole — to be shown as one phrase for its own time, where nothing can drift inside it — when
    it has no sound, too little sound for its letters, a pause without a clear place between two
    words, or a stretch of sound longer than VOICED_TIGHT_MS holding several words."""
    out: list[list[Timed]] = []
    whole: list[int] = []
    why: dict[str, int] = {}
    for ci, cue in enumerate(cues):
        words = (cue.text or "").split()
        placed, reason = _place(cue, words, voiced)
        if placed is None:
            whole.append(ci)
            why[str(reason)] = why.get(str(reason), 0) + 1
            placed = [(w, cue.start_ms, cue.end_ms) for w in words]
        out.append(placed)
    return Estimate(out, whole, why)


def whole_phrases(cues: Sequence[Cue]) -> Estimate:
    """Every cue shown whole for its own time: what is left when not even the pauses are known."""
    return Estimate([[(w, c.start_ms, c.end_ms) for w in (c.text or "").split()] for c in cues],
                    list(range(len(cues))), {"pauses_unknown": len(cues)} if cues else {})


# ---- pages ------------------------------------------------------------------------------------

# Short function words that must not end a caption page when the phrase goes on ("в", "у",
# "и", "the"): a page ending on a preposition reads as a broken sentence.
_CLINGING = re.compile(r"^[\w\-]{1,2}$|^(?:без|для|про|над|под|при|что|как|the|and|for|with|from)$",
                       re.I | re.UNICODE)


def clings(word: str) -> bool:
    return bool(_CLINGING.match(word)) and not re.search(r"[.,!?;:…»\")]$", word)


def pages(words: Sequence[str], max_words: int = MAX_WORDS, max_chars: int = MAX_CHARS) -> list[list[int]]:
    """Word indices split into pages (greedy by words/characters), then a clinging last word
    moves to the next page."""
    out: list[list[int]] = []
    page: list[int] = []
    chars = 0
    for i, w in enumerate(words):
        if page and (len(page) >= max_words or chars + 1 + len(w) > max_chars):
            out.append(page)
            page, chars = [], 0
        chars += len(w) + (1 if page else 0)
        page.append(i)
    if page:
        out.append(page)
    for n in range(len(out) - 1):
        while len(out[n]) > 1 and clings(words[out[n][-1]]):
            out[n + 1].insert(0, out[n].pop())
    return out


def paged(words: Sequence[Timed], max_words: int = MAX_WORDS, max_chars: int = MAX_CHARS,
          pause_break_ms: int | None = PAUSE_BREAK_MS) -> list[list[int]]:
    """Pages of one cue's timed words. With real word times a pause of `pause_break_ms` inside
    the phrase always starts a new page: the words after the pause appear when they are said,
    not before it. `pause_break_ms=None` = pages by size only (estimated times know no pauses)."""
    runs: list[list[int]] = []
    for i, (_, s, _e) in enumerate(words):
        if runs and (pause_break_ms is None or s - words[i - 1][2] < pause_break_ms):
            runs[-1].append(i)
        else:
            runs.append([i])
    out: list[list[int]] = []
    for run in runs:
        out += [[run[j] for j in p] for p in pages([words[i][0] for i in run], max_words, max_chars)]
    return out


def page_cues(aligned: Alignment | Sequence[Sequence[Timed]], max_words: int = MAX_WORDS,
              max_chars: int = MAX_CHARS, clip_ms: int | None = None) -> list[Cue]:
    """One small cue per caption page (<= max_words words / max_chars characters), timed by its
    own words: whatever draws such a cue has nothing left to guess inside it. A page stays up
    until the next one when the gap is short; cues never overlap; numbered 1..n."""
    per_cue = aligned.cues if isinstance(aligned, Alignment) else aligned
    items: list[list[Any]] = []
    for words in per_cue:
        for pg in paged(words, max_words, max_chars):
            items.append([words[pg[0]][1], words[pg[-1]][2], " ".join(words[i][0] for i in pg)])
    for i, it in enumerate(items):
        if i:
            it[0] = max(it[0], items[i - 1][1])
        nxt = items[i + 1][0] if i + 1 < len(items) else clip_ms
        if nxt is not None and nxt - it[1] < PAGE_HOLD_GAP_MS:
            it[1] = max(nxt, it[0])
        it[1] = max(it[1], it[0] + PAGE_MIN_MS)
        if clip_ms is not None:
            it[1] = min(it[1], clip_ms)
    kept = [it for it in items if it[1] > it[0]]
    return [Cue(str(n), int(s), int(e), text) for n, (s, e, text) in enumerate(kept, 1)]


# ---- the gate: words on screen against words in the sound --------------------------------------

def _percentile(sorted_vals: Sequence[int], q: float) -> int:
    """Nearest-rank percentile of a sorted list."""
    return sorted_vals[max(0, math.ceil(q * len(sorted_vals)) - 1)]


def sync_report(shown: Sequence[Timed], heard: Iterable[Any], *, mode: str = "word",
                offset_ms: int = 0, phrase: Sequence[bool] | None = None) -> dict[str, Any]:
    """Distance between each caption word on screen and the same word in the sound.

    shown   caption words with the times they are shown at (clip time base)
    heard   recogniser words of the finished video; `offset_ms` = where the clip starts in it
    mode    "word": a word is highlighted on its own -> |shown start - heard start|;
            "phrase": the whole phrase is on screen -> 0 while the word sounds inside the phrase's
            time, else the distance to it;
            "mixed": some phrases are shown whole and the rest word by word — `phrase` says
            which, one flag per word of `shown`.
    Only words inside a matched run of two or more are compared: a lone matched word may be the
    same word said elsewhere. Returns {mode, words, compared, median_ms, p90_ms, max_ms, worst}."""
    if mode not in ("word", "phrase", "mixed"):
        raise ValueError(mode)
    if mode == "mixed" and (phrase is None or len(phrase) != len(shown)):
        raise ValueError("mixed mode needs one phrase flag per shown word")
    whole = [mode == "phrase"] * len(shown) if mode != "mixed" else [bool(x) for x in phrase or ()]
    heard_t = timed_words(heard)
    errors: list[tuple[int, int]] = []          # (error, index in shown)
    for ia, ib, size in _blocks([w for w, _, _ in shown], [w for w, _, _ in heard_t]):
        if size < 2:
            continue
        for k in range(size):
            _, s, e = shown[ia + k]
            voice = heard_t[ib + k][1] - offset_ms
            err = max(s - voice, voice - e, 0) if whole[ia + k] else abs(s - voice)
            errors.append((int(err), ia + k))
    vals = sorted(v for v, _ in errors)
    rep: dict[str, Any] = {"mode": mode, "words": len(shown), "compared": len(vals),
                           "median_ms": None, "p90_ms": None, "max_ms": None, "worst": None}
    if vals:
        worst = max(errors)
        rep.update(median_ms=_percentile(vals, 0.5), p90_ms=_percentile(vals, 0.9), max_ms=vals[-1],
                   worst={"at_ms": int(shown[worst[1]][1]), "off_ms": worst[0]})
    return rep


def _s(ms: int) -> str:
    return f"{ms / 1000:.1f} s"


def sync_verdict(report: dict[str, Any], *, p90_ms: int = SYNC_P90_MS, max_ms: int = SYNC_MAX_MS,
                 min_words: int = SYNC_MIN_WORDS, min_share: float = SYNC_MIN_SHARE) -> dict[str, Any]:
    """The gate over a sync_report: {status: passed|failed|not_run, reason, ...report, limits}.
    Too few comparable words is `not_run` (no measurement), never `passed`."""
    out = {**report, "limits": {"p90_ms": int(p90_ms), "max_ms": int(max_ms)}}
    n, total = int(report.get("compared") or 0), int(report.get("words") or 0)
    if n < min_words or (total and n / total < min_share):
        return {**out, "status": "not_run",
                "reason": f"the recognised speech matched too few caption words to measure "
                          f"({n} of {total})"}
    if report["p90_ms"] > p90_ms or report["max_ms"] > max_ms:
        return {**out, "status": "failed",
                "reason": f"the captions are out of time with the voice: a word on screen is up to "
                          f"{_s(report['max_ms'])} away from the same word in the sound, and nine "
                          f"words in ten are within {_s(report['p90_ms'])} (allowed: "
                          f"{_s(max_ms)} and {_s(p90_ms)})"}
    return {**out, "status": "passed",
            "reason": f"captions follow the voice: nine words in ten within {_s(report['p90_ms'])}, "
                      f"the furthest {_s(report['max_ms'])}"}


def not_run(reason: str) -> dict[str, Any]:
    """A check that did not happen, with the reason in plain words."""
    return {"status": "not_run", "reason": reason}


# ---- the local gate: captions against the pauses of the sound -----------------------------------

def voiced_report(onsets: Iterable[int], shows: Iterable[Span], pauses: Iterable[Span]) -> dict[str, Any]:
    """What can be said about captions from the pauses of the sound alone (no recognition).

    onsets  the moments something new appears: a word lighting up, or a whole phrase
    shows   the stretches a caption is on screen
    pauses  the pauses of the same sound, same time base
    Returns {captions, pauses, max_lead_ms, on_silence_share, worst}: `max_lead_ms` = the longest
    time a caption appears before the sound comes back (it started inside a pause);
    `on_silence_share` = the share of caption-on-screen time that falls on pauses."""
    stops = sorted((int(a), int(b)) for a, b in pauses if b > a)
    worst: tuple[int, int] | None = None
    n = 0
    for t in onsets:
        n += 1
        lead = next((b - t for a, b in stops if a <= t < b), 0)
        if worst is None or lead > worst[0]:
            worst = (int(lead), int(t))
    shown = on = 0
    for s, e in shows:
        shown += max(0, e - s)
        on += sum(max(0, min(e, b) - max(s, a)) for a, b in stops)
    return {"captions": n, "pauses": len(stops), "max_lead_ms": worst[0] if worst else None,
            "on_silence_share": round(on / shown, 3) if shown else None,
            "worst": {"at_ms": worst[1], "lead_ms": worst[0]} if worst and worst[0] else None}


def voiced_verdict(report: dict[str, Any], *, lead_max_ms: int = LEAD_MAX_MS,
                   on_silence_max: float = ON_SILENCE_MAX) -> dict[str, Any]:
    """The gate over a voiced_report: {status: passed|failed|not_run, reason, ...report, limits}.

    It sees pauses, not words: `passed` says only that no caption sits in a pause ahead of the
    voice — captions out of time while the voice keeps going are invisible to it (that is
    `caption_sync`). No captions or no pauses is `not_run`: there was nothing to compare."""
    out = {**report, "limits": {"lead_max_ms": int(lead_max_ms), "on_silence_max": float(on_silence_max)}}
    if not report.get("captions"):
        return {**out, "status": "not_run", "reason": "there are no captions to compare with the sound"}
    if not report.get("pauses"):
        return {**out, "status": "not_run",
                "reason": "no pauses were found in the sound, so there was nothing to compare the "
                          "captions with"}
    lead, share = int(report["max_lead_ms"] or 0), float(report["on_silence_share"] or 0.0)
    if lead > lead_max_ms:
        return {**out, "status": "failed",
                "reason": f"a caption appears in a pause {_s(lead)} before the sound comes back, at "
                          f"{_s(report['worst']['at_ms'])} of the clip (allowed: {_s(lead_max_ms)})"}
    if share > on_silence_max:
        return {**out, "status": "failed",
                "reason": f"{round(share * 100)}% of the time the captions are on screen there is no "
                          f"sound (allowed: {round(on_silence_max * 100)}%)"}
    return {**out, "status": "passed",
            "reason": f"no caption sits in a pause ahead of the sound (the earliest is {_s(lead)} "
                      f"ahead, allowed {_s(lead_max_ms)}); this check sees pauses only, it does not "
                      f"hear the words"}


# ---- clip.words.json ---------------------------------------------------------------------------

def words_path(clip_srt: str | os.PathLike) -> Path:
    """`clip.words.json` next to a clip.srt."""
    return Path(clip_srt).with_name(WORDS_NAME)


def _srt_hash(clip_srt: str | os.PathLike) -> str | None:
    try:
        return sha256_bytes(Path(clip_srt).read_bytes())
    except OSError:
        return None


def write_clip_words(clip_srt: str | os.PathLike, alignment: Alignment | None, source: str | None,
                     reason: str | None = None) -> Path:
    """Write the word times of a clip next to its clip.srt, bound to that file's bytes.
    `alignment=None` records that there are none, with the reason (every phrase is then shown
    whole, and the file says why)."""
    doc: dict[str, Any] = {"schema": SCHEMA, "clip_srt_sha256": _srt_hash(clip_srt), "source": source}
    if alignment is None:
        doc.update(word_times="estimated", cues=None, reason=reason or "no word times for this clip")
    else:
        doc.update(word_times="aligned", matched=alignment.matched, total=alignment.total,
                   cues=[[[w, s, e] for w, s, e in cue] for cue in alignment.cues])
    return write_json(words_path(clip_srt), doc)


def write_clip_estimate(clip_srt: str | os.PathLike, estimate: Estimate, word_times: str,
                        reason: str, sound: dict[str, Any] | None = None) -> Path:
    """Write what stands in for word times when the recording has none: `word_times` =
    "estimated_voiced" (placed by the pauses of the clip's sound, `estimate_voiced`) or
    "estimated" (the pauses are not known either, `whole_phrases`), which cues are shown whole,
    why, and what the sound measurement gave."""
    if word_times not in ESTIMATES:
        raise ValueError(word_times)
    doc: dict[str, Any] = {"schema": SCHEMA, "clip_srt_sha256": _srt_hash(clip_srt), "source": None,
                           "word_times": word_times, "reason": reason, "whole": list(estimate.whole),
                           "why": dict(estimate.why), "sound": sound,
                           "cues": [[[w, s, e] for w, s, e in cue] for cue in estimate.cues]}
    return write_json(words_path(clip_srt), doc)


def read_clip_words(clip_srt: str | os.PathLike, cues: Sequence[Cue]) -> tuple[list[list[Timed]] | None, dict[str, Any]]:
    """(word times per cue | None, info) for a clip.srt.

    info["word_times"]: "aligned" — the recording's word times; "estimated_voiced" — placed by
    the pauses of the sound, with info["whole"] = the cues to show as whole phrases;
    "estimated" — nothing is known inside a phrase: every cue is shown whole. None — with the
    reason in info["reason"] — when there is no file, it records that the clip has no word
    times, it belongs to another text of clip.srt, or it does not hold exactly the words of
    `cues`: the caller then shows every phrase whole, and says so."""
    path = words_path(clip_srt)
    try:
        data = read_json(path, default=None) if path.is_file() else None
    except ValueError:
        data = "unreadable"
    if data is None:
        return None, {"word_times": "estimated", "reason": "no word times for this clip"}
    bad = {"word_times": "estimated"}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None, {**bad, "reason": f"{WORDS_NAME} is not readable"}
    if data.get("clip_srt_sha256") != _srt_hash(clip_srt):
        return None, {**bad, "reason": f"{WORDS_NAME} was made for another text of the subtitles"}
    if data.get("cues") is None:
        return None, {**bad, "reason": str(data.get("reason") or "no word times for this clip")}
    if not isinstance(data["cues"], list):
        return None, {**bad, "reason": f"{WORDS_NAME} is not readable"}
    out: list[list[Timed]] = []
    try:
        if len(data["cues"]) != len(cues):
            raise ValueError("cue count")
        for cue, row in zip(cues, data["cues"]):
            timed = [(str(w), int(s), int(e)) for w, s, e in row]
            if [w for w, _, _ in timed] != (cue.text or "").split():
                raise ValueError("words")
            out.append(timed)
        kind = str(data.get("word_times") or "aligned")
        whole = sorted({int(i) for i in data.get("whole") or ()})
        if kind not in ("aligned", *ESTIMATES) or any(not 0 <= i < len(cues) for i in whole):
            raise ValueError("kind")
    except (TypeError, ValueError):
        return None, {**bad, "reason": f"{WORDS_NAME} does not hold the words of the subtitles"}
    if kind == "aligned":
        return out, {"word_times": "aligned", "source": data.get("source"),
                     "matched": data.get("matched"), "total": data.get("total")}
    if kind == "estimated":                       # written without pauses: every phrase whole
        whole = list(range(len(cues)))
    return out, {"word_times": kind, "reason": str(data.get("reason") or "no word times for this clip"),
                 "whole": whole}


def caption_info(info: dict[str, Any], n_cues: int) -> dict[str, Any]:
    """What a QA report says about how the captions of a clip were timed, from the info of
    `read_clip_words`: without real word times it counts the phrases and how many of them are
    shown whole (all of them when nothing could be read)."""
    out = {k: v for k, v in info.items() if k != "whole"}
    if out.get("word_times") in ESTIMATES:
        whole = info.get("whole")
        out.update(cues=int(n_cues), phrase_cues=int(n_cues) if whole is None else len(whole))
    return out


def clip_words_current(clip_srt: str | os.PathLike) -> bool:
    """True when `clip.words.json` exists and was made for the present text of this clip.srt."""
    path = words_path(clip_srt)
    try:
        data = read_json(path, default=None) if path.is_file() else None
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("schema") == SCHEMA \
        and data.get("clip_srt_sha256") == _srt_hash(clip_srt)
