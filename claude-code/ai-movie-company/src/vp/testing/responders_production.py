"""Default fake-judge responders for the production steps S6a, S6b, S7 (valid outputs built from
the inputs only — deterministic, no model)."""
from __future__ import annotations

import json
import re

from .fake_responders import register

_PUNCT_END = re.compile(r"[\s.,;:!?…—-]+$")


def _cut_words(text: str, max_chars: int, max_words: int | None = None) -> str:
    words = text.split()
    if max_words:
        words = words[:max_words]
    out = ""
    for w in words:
        nxt = f"{out} {w}" if out else w
        if len(nxt) > max_chars:
            break
        out = nxt
    return _PUNCT_END.sub("", out.replace("<", "").replace(">", "")) or "Фрагмент"


def _ctx(inputs: dict[str, str]) -> tuple[dict, list[str], dict]:
    frag = json.loads(inputs.get("fragment") or "{}")
    lines = [ln for ln in (inputs.get("text") or "").splitlines() if ln.strip()]
    author = json.loads(inputs.get("author") or "{}")
    return frag, lines, author


def _base(frag: dict, lines: list[str]) -> str:
    return frag.get("title") or frag.get("thesis") or (lines[0] if lines else "Фрагмент лекции")


def respond_s6a(task: str, prompt: str, inputs: dict[str, str]) -> str:
    frag, lines, author = _ctx(inputs)
    base = _base(frag, lines)
    out = {"intro_text": _cut_words(base, 80),
           "announce_text": "Полная лекция и другие фрагменты — на канале",
           "thumbnail_text": _cut_words(base, 40, max_words=4)}
    for name, spec in author.items():
        if spec.get("status") in ("locked", "extend") and name in out:
            out[name] = ""
    return json.dumps(out, ensure_ascii=False)


def respond_s6b(task: str, prompt: str, inputs: dict[str, str]) -> str:
    frag, lines, author = _ctx(inputs)
    base = _cut_words(_base(frag, lines), 80)
    titles = [base, _cut_words(f"Почему {base[:1].lower()}{base[1:]}", 99) + "?",
              _cut_words(f"{base}: разбор", 100)]
    desc_lines = [frag.get("thesis") or base] + lines[:2]
    description = "\n".join(d for d in desc_lines if d)
    tags = list(dict.fromkeys(w.strip(".,!?:;«»—").lower() for w in base.split() if len(w) > 3))[:10]
    tags = [t for t in tags if t] + ["лекция", "ИИ"]
    chapters = []
    if frag.get("type") in ("long", "full") and lines:
        n = len(lines)
        for k, idx in enumerate(sorted({0, n // 4, n // 2, (3 * n) // 4})):
            words = lines[idx].split()
            chapters.append({"title": _cut_words(" ".join(words[:5]), 60) or f"Часть {k + 1}",
                             "quote": " ".join(words[:8])})
    out = {"titles": titles, "description": description, "tags": tags, "hashtags": ["#ИИ"],
           "chapters": chapters}
    for name, spec in author.items():
        if spec.get("status") in ("locked", "extend") and name in ("description",):
            out[name] = ""
        if spec.get("status") in ("locked", "extend") and name in ("tags", "hashtags"):
            out[name] = []
        if spec.get("status") in ("locked", "extend") and name == "title":
            out["titles"] = ["", "", ""] if spec.get("status") == "locked" else ["(разбор)", "(лекция)", "(фрагмент)"]
    return json.dumps(out, ensure_ascii=False)


def respond_s7(task: str, prompt: str, inputs: dict[str, str]) -> str:
    return json.dumps({"candidate": 1, "why": "the sharpest frame"})


register("s6a.", respond_s6a)
register("s6b.", respond_s6b)
register("s7.", respond_s7)
