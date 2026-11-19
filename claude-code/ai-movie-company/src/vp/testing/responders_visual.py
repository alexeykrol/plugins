"""Default fake responder for S5v (tests and offline e2e): a clean picture.

Tests that need a failing verdict register their own responder for `s5v.` or pass a FakeJudge
with a custom responder."""
from __future__ import annotations

from .fake_responders import register

CLEAN = ("face_in_frame: yes\npersonal_data: no\nsubtitles_readable: yes\n"
         "subtitles_cover_face: no\nbars_or_empty: no\nproblem_frames: []\nnote: Кадр чистый.\n")


def s5v_visual(task: str, prompt: str, inputs: dict[str, str]) -> str:
    if "attachments" not in inputs:
        raise ValueError("s5v: no contact sheet attached")
    if "none in the picture" in inputs.get("clip", ""):
        return CLEAN.replace("subtitles_readable: yes", "subtitles_readable: no_subtitles")
    return CLEAN


register("s5v.", s5v_visual)
