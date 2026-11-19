"""Default fake responders for `s3a.reel` / `s3a.long`.

They read the model input `subtitles.txt` (lines `#<number> <text>`) and pick REAL quotes from it,
so the code's quote matching works end to end. Spans are chosen by character count (speech is
~18 characters per second in the test fixture):
  reel: two spans of ~400 characters starting at 10 % and 55 % of the text
  long: one span of ~1300 characters starting at 25 % of the text
With the 3-minute fixture (reel 10–40 s, long 40–120 s) this yields 2 reels + 1 long.
"""
from __future__ import annotations

import re

import yaml

from .fake_responders import register

_LINE_RE = re.compile(r"^#(\S+)\s+(.*)$")
PLAN = {"reel": [(0.10, 400), (0.55, 400)], "long": [(0.25, 1300)]}
QUOTE_WORDS = 12


def parse_transcript(text: str) -> list[tuple[str, str]]:
    out = []
    for line in (text or "").splitlines():
        m = _LINE_RE.match(line.strip())
        if m and m.group(2).strip():
            out.append((m.group(1), m.group(2).strip()))
    return out


def pick_span(lines: list[tuple[str, str]], start_frac: float, target_chars: int) -> tuple[int, int]:
    total = sum(len(t) for _, t in lines)
    pos, i = 0, 0
    while i < len(lines) - 1 and pos + len(lines[i][1]) <= start_frac * total:
        pos += len(lines[i][1])
        i += 1
    j, acc = i, len(lines[i][1])
    while acc < target_chars and j < len(lines) - 1:
        j += 1
        acc += len(lines[j][1])
    return i, j


def quotes(lines: list[tuple[str, str]], i: int, j: int) -> tuple[str, str]:
    words = " ".join(t for _, t in lines[i:j + 1]).split()
    n = min(QUOTE_WORDS, len(words))
    return " ".join(words[:n]), " ".join(words[-n:])


def respond_select(task: str, prompt: str, inputs: dict[str, str]) -> str:
    kind = task.rsplit(".", 1)[-1]          # s3a.reel, also s3c.replay.<rec>.reel
    lines = parse_transcript(inputs.get("subtitles.txt", ""))
    frags = []
    if lines:
        for n, (frac, chars) in enumerate(PLAN.get(kind, []), 1):
            i, j = pick_span(lines, frac, chars)
            sq, eq = quotes(lines, i, j)
            frags.append({"start_quote": sq, "end_quote": eq,
                          "title": f"Фрагмент {kind} {n}", "thesis": "Тезис фрагмента.",
                          "why": "Законченная мысль с сильным выводом.",
                          "applied": ["R004" if kind == "reel" else "R007"], "score": 5 - n})
    return yaml.safe_dump({"fragments": frags}, allow_unicode=True, sort_keys=False)


register("s3a.", respond_select)
