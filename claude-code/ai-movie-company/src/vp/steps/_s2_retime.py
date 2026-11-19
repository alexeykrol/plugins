"""S2 retiming core — a port of the validated prototype `src/prototype/retime.py` (2026-11-16).

Pure functions over fragment dicts; no I/O. A fragment is
    {"id": str, "start": ms, "end": ms, "spk": str, "text": str}
(text = proofread text, "" for emptied or filtered cues).

In `srt_only` mode the behaviour is byte-identical to the prototype (a regression test checks it on
the 16.11 etalons). In `words` mode (v1 improvement, SPEC S2) a fragment that is long by text or by
time is split using the recognizer's word timestamps instead of the character share, so no piece is
`estimated`. Constants and the gate (HARD / RATE) are those of the prototype.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

MAX_CHARS = 80        # 2 lines x 42 with slack for word wrap
LINE = 42
MAX_SPAN = 6500       # ms, max title duration when merging
MIN_DUR = 1000        # ms, desired minimum title duration
GAP = 80              # ms, gap kept between consecutive titles (~2 frames)
CHAIN = 400           # ms, gaps shorter than this are closed (no flicker)
BREAK_GAP = 1000      # ms, pause that always ends a title
SENT_MIN = 15         # chars: sentence end closes a title if it has at least this
ACCEPT_CHARS = MAX_CHARS + 4   # SPEC S2 acceptance «> 84 знаков ≤ 0,5 %» == merge limit of merge_short

# Guarantees of SPEC S2. HARD: any occurrence blocks the output. RATE: share of titles must stay <= RATE_MAX.
HARD = ('nonpositive', 'overlap', 'nonmonotonic', 'long>7s', 'ids_mismatch', 'pause_inside', 'drift', 'empty')
RATE = ('short<0.7s', 'chars>84')
RATE_MAX = 0.005

CONSTANTS = {"MAX_CHARS": MAX_CHARS, "LINE": LINE, "MAX_SPAN": MAX_SPAN, "MIN_DUR": MIN_DUR, "GAP": GAP,
             "CHAIN": CHAIN, "BREAK_GAP": BREAK_GAP, "SENT_MIN": SENT_MIN, "ACCEPT_CHARS": ACCEPT_CHARS, "HARD": list(HARD),
             "RATE": list(RATE), "RATE_MAX": RATE_MAX}

_SENT_END = re.compile(r'[.!?…]["»]?$')


def group(frags: list[dict]) -> list[list[dict]]:
    """Greedy grouping of consecutive fragments into titles."""
    groups, cur = [], []

    def text_of(g): return ' '.join(f['text'] for f in g if f['text'])
    for fr in frags:
        if not fr['text']:              # emptied fragment: absorbed into current title span only
            continue
        if cur:
            prev = cur[-1]
            joined = text_of(cur + [fr])
            brk = (fr['spk'] != prev['spk'] and not fr['text'][:1].islower()
                   or fr['start'] - prev['end'] > BREAK_GAP
                   or len(joined) > MAX_CHARS
                   or fr['end'] - cur[0]['start'] > MAX_SPAN
                   or (_SENT_END.search(prev['text']) and len(text_of(cur)) >= SENT_MIN))
            if brk:
                groups.append(cur)
                cur = []
        cur.append(fr)
    if cur:
        groups.append(cur)
    return groups


def split_text(text: str, limit: int) -> list[str]:
    """Split text at word boundaries into parts <= limit, preferring punctuation (prototype rule)."""
    if len(text) <= limit:
        return [text]
    words = text.split(' ')
    parts, cur = [], ''
    for w in words:
        cand = (cur + ' ' + w).strip()
        if len(cand) > limit and cur:
            parts.append(cur)
            cur = w
        else:
            cur = cand
            if re.search(r'[.!?…,;:]$', w) and len(cur) > limit * 0.6:
                parts.append(cur)
                cur = ''
    if cur:
        parts.append(cur)
    return parts


def split_long(title: dict) -> list[dict]:
    """A single fragment longer than MAX_CHARS: split at punctuation, time by char share (estimate)."""
    text = title['text']
    span0 = title['end'] - title['start']
    limit = MAX_CHARS
    if span0 > MAX_SPAN:                      # long in time: aim for pieces under MAX_SPAN
        n = -(-span0 // MAX_SPAN)
        limit = min(MAX_CHARS, max(25, -(-len(text) // n) + 6))
    if len(text) <= limit:
        return [title]
    parts = split_text(text, limit)
    total = sum(len(p) for p in parts)
    t = title['start']
    span = title['end'] - title['start']
    out = []
    for i, p in enumerate(parts):
        e = title['end'] if i == len(parts) - 1 else t + round(span * len(p) / total)
        out.append({**title, 'start': t, 'end': e, 'text': p, 'estimated': True})
        t = e
    return out


# ---- words mode ------------------------------------------------------------------------------

def _norm_token(w: str) -> str:
    return re.sub(r'[^\w]+', '', w.lower().replace('ё', 'е'))


def word_times(text: str, words: list[dict]) -> list[tuple[int, int]] | None:
    """Time (start_ms, end_ms) for every space-separated token of the proofread `text`, from the
    recognizer `words` [{"w","start","end"}] of the same cue. Tokens are aligned on normalised
    spelling (difflib); unaligned tokens take times interpolated between aligned neighbours.
    None if there are no words."""
    if not words:
        return None
    toks = text.split(' ')
    a = [_norm_token(t) for t in toks]
    b = [_norm_token(w['w']) for w in words]
    m: dict[int, int] = {}
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            m[blk.a + k] = blk.b + k
    if not m:  # nothing aligned: map by relative position
        n, w = len(toks), len(words)
        m = {i: min(w - 1, (i * w) // n) for i in range(n)}
    known = sorted(m)
    out: list[tuple[int, int]] = []
    for i in range(len(toks)):
        if i in m:
            wd = words[m[i]]
            out.append((wd['start'], wd['end']))
            continue
        lo = max((k for k in known if k < i), default=None)
        hi = min((k for k in known if k > i), default=None)
        if lo is None:
            t = words[m[hi]]['start']
            out.append((t, t))
        elif hi is None:
            t = words[m[lo]]['end']
            out.append((t, t))
        else:
            t0, t1 = words[m[lo]]['end'], words[m[hi]]['start']
            frac = (i - lo) / (hi - lo)
            t = int(round(t0 + (t1 - t0) * frac))
            out.append((t, t))
    # monotonic starts
    fixed, last = [], None
    for s, e in out:
        if last is not None and s < last:
            s = last
        e = max(e, s)
        fixed.append((s, e))
        last = s
    return fixed


def split_by_words(fr: dict, words: list[dict]) -> list[dict] | None:
    """Split a long fragment using word timestamps (no `estimated` pieces).

    Text is split at punctuation (<= MAX_CHARS, prototype rule); a part still longer than MAX_SPAN
    in time is split further at word boundaries by time. Piece i starts at its first word (piece 0
    at the fragment start: start drift stays 0) and ends at its last word's end (the last piece
    no later than the fragment end). Returns None when no word timings cover the fragment.
    """
    text = fr['text']
    toks = text.split(' ')
    times = word_times(text, words)
    if times is None:
        return None
    parts = split_text(text, MAX_CHARS)
    # token index ranges of the parts
    ranges, k = [], 0
    for p in parts:
        n = len(p.split(' '))
        ranges.append((k, k + n))
        k += n
    if k != len(toks):  # split_text drops nothing, but guard against odd spacing
        ranges = [(0, len(toks))]
    # time split of each range
    pieces: list[tuple[int, int]] = []
    for lo, hi in ranges:
        cur = lo
        for i in range(lo + 1, hi):
            if times[i][1] - times[cur][0] > MAX_SPAN:
                pieces.append((cur, i))
                cur = i
        pieces.append((cur, hi))
    if len(pieces) == 1:
        p0 = {**fr, 'end': min(fr['end'], max(times[-1][1], fr['start'] + 1)), 'timing': 'words'}
        return [p0]
    out = []
    for j, (lo, hi) in enumerate(pieces):
        start = fr['start'] if j == 0 else max(times[lo][0], out[-1]['start'] + 1)
        end = max(times[hi - 1][1], start + 1)
        if j == len(pieces) - 1:
            end = min(max(end, start + 1), max(fr['end'], start + 1))
        out.append({**fr, 'start': start, 'end': end, 'text': ' '.join(toks[lo:hi]),
                    'timing': 'words', 'inner': j > 0})
    return out


def merge_short(titles: list[dict]) -> list[dict]:
    """Titles whose own span is under MIN_DUR*0.7 are merged into a neighbour when limits allow."""
    out = []
    i = 0
    while i < len(titles):
        t = titles[i]
        short = t['end'] - t['start'] < 700
        if short:
            prev = out[-1] if out else None
            nxt = titles[i + 1] if i + 1 < len(titles) else None

            def ok(a, b):
                # same break rules as group(): a long pause or a speaker change never merges
                if b['start'] - a['end'] > BREAK_GAP:
                    return False
                if b.get('spk') != a.get('spk') and not b['text'][:1].islower():
                    return False
                return (len(a['text']) + 1 + len(b['text']) <= MAX_CHARS + 4
                        and max(a['end'], b['end']) - a['start'] <= 7000)
            prefer_prev = prev is not None and not _SENT_END.search(prev['text'])
            order = [('p', prev), ('n', nxt)] if prefer_prev else [('n', nxt), ('p', prev)]
            for side, nb in order:
                if nb is None:
                    continue
                if side == 'p' and ok(prev, t):
                    prev.update(text=prev['text'] + ' ' + t['text'], end=max(prev['end'], t['end']),
                                ids=prev['ids'] + t['ids'],
                                estimated=prev.get('estimated') or t.get('estimated'))
                    break
                if side == 'n' and ok(t, nxt):
                    merged = {**nxt, 'start': t['start'], 'text': t['text'] + ' ' + nxt['text'],
                              'ids': t['ids'] + nxt['ids'],
                              'estimated': t.get('estimated') or nxt.get('estimated')}
                    if 'inner' in t or 'inner' in nxt:  # words mode only: start is now t's
                        merged['inner'] = bool(t.get('inner'))
                    titles[i + 1] = merged
                    break
            else:
                out.append(t)
            i += 1
            continue
        out.append(t)
        i += 1
    return out


def wrap(text: str) -> str:
    """Break a title into at most two lines <= LINE where possible (prototype rule)."""
    if len(text) <= LINE:
        return text
    best, score = None, None
    for m in re.finditer(' ', text):
        i = m.start()
        a, b = text[:i], text[i + 1:]
        if len(a) > LINE or len(b) > LINE:
            continue
        s = abs(len(a) - len(b)) - (15 if re.search(r'[.,!?;:…—]$', a) else 0)
        if score is None or s < score:
            best, score = (a, b), s
    if not best:
        sp = [m.start() for m in re.finditer(' ', text)]
        if sp:
            i = min(sp, key=lambda i: max(i, len(text) - i - 1))
            best = (text[:i], text[i + 1:])
    return '\n'.join(best) if best else text


def build(frags: list[dict], words_by_id: dict[str, list[dict]] | None = None) -> list[dict]:
    """Fragments -> titles. `words_by_id` (words mode): recognizer words per fragment id."""
    pieces = []
    for fr in frags:
        if fr['text'] and (len(fr['text']) > MAX_CHARS or fr['end'] - fr['start'] > MAX_SPAN):
            split = None
            if words_by_id is not None:
                split = split_by_words({**fr}, words_by_id.get(fr['id']) or [])
            if split is None:
                split = split_long({**fr})
            for p in split:
                p['ids'] = [fr['id']]
                pieces.append(p)
        else:
            pieces.append(fr)
    titles = []
    for g in group(pieces):
        t = {'start': g[0]['start'], 'end': max(f['end'] for f in g),
             'text': ' '.join(f['text'] for f in g), 'ids': [x for f in g for x in f.get('ids', [f['id']])],
             'spk': g[0]['spk'], 'estimated': any(f.get('estimated') for f in g)}
        if g[0].get('inner'):
            t['inner'] = True
        if any(f.get('timing') == 'words' for f in g):
            t['timing'] = 'words'
        titles.append(t)
    titles = merge_short(titles)
    # starts must strictly increase
    for i in range(1, len(titles)):
        if titles[i]['start'] <= titles[i - 1]['start']:
            titles[i]['start'] = titles[i - 1]['start'] + 200
            titles[i]['moved_start'] = True
    # resolve overlaps, close short gaps, extend too-short titles
    for i, t in enumerate(titles):
        nxt = titles[i + 1]['start'] if i + 1 < len(titles) else None
        if nxt is None:
            continue
        limit = nxt - GAP
        if t['end'] > limit:
            t['end'] = limit
            t['trimmed'] = True
        elif nxt - t['end'] < CHAIN:
            t['end'] = limit
        if t['end'] - t['start'] < MIN_DUR:
            t['end'] = min(t['start'] + MIN_DUR, limit)
    return titles


def validate(frags: list[dict], titles: list[dict]) -> tuple[dict[str, list], list[int]]:
    """Issues {kind: [title numbers]} and start drift per checked title (prototype rules)."""
    issues: dict[str, list] = {}

    def add(k, v): issues.setdefault(k, []).append(v)
    for i, t in enumerate(titles):
        if t['end'] <= t['start']:
            add('nonpositive', i + 1)
        if i and t['start'] < titles[i - 1]['end']:
            add('overlap', i + 1)
        if i and t['start'] <= titles[i - 1]['start']:
            add('nonmonotonic', i + 1)
        d = t['end'] - t['start']
        if d < 700:
            add('short<0.7s', i + 1)
        if d > 7000:
            add('long>7s', i + 1)
        if len(t['text']) > ACCEPT_CHARS:  # prototype checked > MAX_CHARS (80); SPEC S2 says > 84
            add('chars>84', i + 1)
        if d and len(t['text']) / (d / 1000) > 21:
            add('cps>21', i + 1)
        for ln in wrap(t['text']).split('\n'):
            if len(ln) > LINE:
                add('line>42', i + 1)
    srcmap = {f['id']: f for f in frags}
    for i, t in enumerate(titles):
        for a, b in zip(t['ids'], t['ids'][1:]):
            if a != b and srcmap[b]['start'] - srcmap[a]['end'] > BREAK_GAP:
                add('pause_inside', i + 1)
    used = [x for t in titles for x in t['ids']]
    expected = [f['id'] for f in frags if f['text']]
    seen = list(dict.fromkeys(used))
    if seen != expected:
        add('ids_mismatch', 'order/coverage')
    # drift: title start vs its first source fragment start
    drift = []
    for t in titles:
        if t.get('estimated') and t['start'] != srcmap[t['ids'][0]]['start']:
            continue  # inner piece of a split fragment: estimated by char share
        if t.get('inner'):
            continue  # inner piece of a fragment split by word timestamps: starts at its word
        drift.append(t['start'] - srcmap[t['ids'][0]]['start'])
    for i, d in enumerate(drift):
        if d:
            add('drift', i + 1)
    if not titles:
        add('empty', 'no titles')
    return issues, drift


def violations(issues: dict[str, list], n_titles: int) -> dict[str, int]:
    """Gate: HARD kinds with any occurrence, RATE kinds above RATE_MAX share."""
    bad = {k: len(issues[k]) for k in HARD if k in issues}
    bad.update({k: len(issues[k]) for k in RATE
                if k in issues and len(issues[k]) / max(n_titles, 1) > RATE_MAX})
    return bad


def stats(titles: list[dict], drift: list[int]) -> dict[str, Any]:
    durs = [t['end'] - t['start'] for t in titles]
    return {
        "titles": len(titles),
        "trimmed": sum(1 for t in titles if t.get('trimmed')),
        "moved_start": sum(1 for t in titles if t.get('moved_start')),
        "estimated": sum(1 for t in titles if t.get('estimated')),
        "words_timed": sum(1 for t in titles if t.get('timing') == 'words'),
        "duration_ms": ({"min": min(durs), "median": sorted(durs)[len(durs) // 2], "max": max(durs)}
                        if durs else None),
        "drift_nonzero": sum(1 for d in drift if d),
    }


def words_for_cues(frags: list[dict], words: list[dict]) -> dict[str, list[dict]]:
    """Assign recognizer words [{"w","start","end"}] (ms, sorted) to fragments by word midpoint
    inside [start, end] of the fragment (first match wins)."""
    out: dict[str, list[dict]] = {f['id']: [] for f in frags}
    spans = sorted(((f['start'], f['end'], f['id']) for f in frags), key=lambda x: (x[0], x[1]))
    j0 = 0
    for w in words:
        mid = (w['start'] + w['end']) / 2
        while j0 < len(spans) and spans[j0][1] < mid:
            j0 += 1
        for j in range(j0, len(spans)):
            s, e, fid = spans[j]
            if s > mid:
                break
            if s <= mid <= e:
                out[fid].append(w)
                break
    return out


def load_words(data: Any) -> list[dict]:
    """Words from a Whisper verbose_json-like object -> [{"w","start","end"}] in ms, sorted.
    Accepts {"segments":[{"words":[...]}]} or a top-level {"words":[...]}. Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("words json must be an object")
    raw: list = []
    if isinstance(data.get("words"), list):
        raw.extend(data["words"])
    for seg in data.get("segments") or []:
        if isinstance(seg, dict) and isinstance(seg.get("words"), list):
            raw.extend(seg["words"])
    out = []
    for w in raw:
        if not isinstance(w, dict):
            raise ValueError("word entry must be an object")
        text = str(w.get("word", w.get("text", ""))).strip()
        s, e = w.get("start"), w.get("end")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)) or e < s:
            raise ValueError(f"bad word timing {w!r}")
        if text:
            out.append({"w": text, "start": int(round(s * 1000)), "end": int(round(e * 1000))})
    out.sort(key=lambda x: (x["start"], x["end"]))
    return out
