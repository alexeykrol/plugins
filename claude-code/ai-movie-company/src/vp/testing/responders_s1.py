"""Fake judge responder for `s1.proofread.<NN>`: a light, VALID proofread.

Keeps every ID in order; removes a few filler words; capitalises the first letter of a cue that
starts a sentence (first cue of the chunk or after sentence-final punctuation). Never empties a cue
unless it was empty, never adds words.
"""
from __future__ import annotations

import re

from .fake_responders import register

_FILLER = re.compile(r"(?i)(?<![\w-])(?:ну|вот|как бы|типа|значит|короче|э+|м+)(?![\w-])[,]?\s*")
_SENT_END = re.compile(r'[.!?…]["»]?$')


def light_proofread(text: str, sentence_start: bool) -> str:
    t = _FILLER.sub("", text).strip()
    t = re.sub(r"\s+", " ", t)
    if not t:
        t = text.strip()  # never empty a cue that had words
    if sentence_start and t[:1].islower():
        t = t[:1].upper() + t[1:]
    return t


def respond_proofread(task: str, prompt: str, inputs: dict[str, str]) -> str:
    out = []
    sentence_start = True
    for line in inputs.get("chunk.txt", "").splitlines():
        if not line.strip():
            continue
        cid, _, text = line.partition("|")
        new = light_proofread(text, sentence_start) if text.strip() else ""
        out.append(f"{cid}|{new}")
        if new:
            sentence_start = bool(_SENT_END.search(new))
    return "\n".join(out) + "\n"


register("s1.proofread.", respond_proofread)
