"""S7 — thumbnail (SPEC S7).

Code proposes candidate frames: N stills evenly spread over the cut (skipping the first and last
5 %), black frames dropped, ranked by sharpness (ffmpeg `blurdetect`, lower blur = sharper);
the best K become candidates 1..K. Face detection is OUT OF SCOPE in v1 (no face model in the
stdlib/ffmpeg toolset) — candidates are ranked by sharpness only.

Judgment: task `s7.<item>` gets, per candidate, its number, its sharpness rank and what is said at
that moment (subtitle text, no time) plus the thumbnail text from S6a; it answers
{"candidate": n}. The thumbnail text itself comes from design_texts.yaml (S6a), never from S7.

Code renders via ctx.renderer.render_thumbnail -> thumb.png and checks: 1280×720, < 2 MB,
contrast between text colour and its opaque plate ≥ 4.5:1 (WCAG, computed by code from the
colours the renderer uses).

Config keys: s7: {candidates: 8, top: 4}.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..adapters import media
from ..adapters.render import MIN_CONTRAST, THUMB_H, THUMB_W, brand_settings, contrast_ratio, readable_text_color
from ..core.io import read_yaml
from ..core.srt import Cue, read_srt
from .base import Ctx, StepSpec, VPError
from .s4_cut import fragment_record
from .s6a_design_texts import fragment_input, load_prompt, parse_json_object, prompt_hash

PROMPT = "s7_thumbnail.md"
DEFAULTS = {"candidates": 8, "top": 4}
THUMB_MAX_BYTES = 2 * 1024 * 1024
_BLUR_RE = re.compile(r"lavfi\.blur=([\d.]+)")


def s7_settings(config: dict[str, Any]) -> dict[str, int]:
    s = dict(DEFAULTS)
    s.update({k: int(v) for k, v in (config.get("s7") or {}).items() if k in DEFAULTS})
    return s


def sample_times(duration_ms: int, n: int) -> list[int]:
    """n times evenly spread over [5 %, 95 %] of the duration."""
    if n <= 0 or duration_ms <= 0:
        return []
    lo, hi = duration_ms * 0.05, duration_ms * 0.95
    if n == 1:
        return [int((lo + hi) / 2)]
    return [int(lo + (hi - lo) * k / (n - 1)) for k in range(n)]


def blur_of(png: str | os.PathLike) -> float:
    """ffmpeg blurdetect value of an image (lower = sharper)."""
    proc = media.run_ffmpeg(["-i", str(png), "-vf", "blurdetect,metadata=print:key=lavfi.blur",
                             "-f", "null", "-"])
    m = _BLUR_RE.search(proc.stderr)
    if not m:
        raise VPError("data", "blurdetect: no value")
    return float(m.group(1))


def speech_at(cues: list[Cue], t_ms: int) -> str:
    """Subtitle text at t (or the last one before it); '' if nothing was said yet."""
    best = ""
    for c in cues:
        if c.start_ms <= t_ms:
            best = c.text
        if c.start_ms <= t_ms < c.end_ms:
            return " ".join(c.text.split())
    return " ".join(best.split())


def rank_candidates(samples: list[dict[str, Any]], top: int) -> list[dict[str, Any]]:
    """Drop black frames, sort by blur ascending (ties: earlier first), keep `top`, number 1..K."""
    usable = [s for s in samples if not s.get("black")]
    usable.sort(key=lambda s: (s["blur"], s["t_ms"]))
    out = []
    for k, s in enumerate(usable[:top], 1):
        out.append({**s, "n": k})
    return out


def candidates_input(cands: list[dict[str, Any]]) -> str:
    """What the model sees: number, sharpness rank, speech. No times."""
    lines = []
    for c in cands:
        said = c["speech"] or "(no speech)"
        lines.append(f"{c['n']}: sharpness rank {c['n']}; said at this moment: «{said}»")
    return "\n".join(lines)


def parse_choice(text: str, cands: list[dict[str, Any]], task: str) -> int:
    data = parse_json_object(text, task)
    n = data.get("candidate")
    if isinstance(n, bool) or not isinstance(n, int) or n not in {c["n"] for c in cands}:
        raise VPError("contract", f"{task}: candidate must be one of {[c['n'] for c in cands]}, got {n!r}")
    return n


class S7Thumbnail(StepSpec):
    id, deps, scope, version, gate = "S7", ("S6a",), "item", "1", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        j, i = ctx.job, ctx.item
        return [j.cut_mp4(i), j.clip_srt(i), j.design_texts(i)]

    def config(self, ctx: Ctx) -> dict:
        rec = fragment_record(ctx)
        return {"fragment": {k: rec[k] for k in ("id", "kind", "title", "thesis")},
                "s7": s7_settings(ctx.config), "brand": brand_settings(ctx.config),
                "prompt": prompt_hash(PROMPT)}

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.thumb_png(ctx.item)]

    def _colors(self, ctx: Ctx) -> tuple[str, str]:
        fn = getattr(ctx.renderer, "thumbnail_colors", None)
        if callable(fn):
            return fn()
        b = brand_settings(ctx.config)
        return readable_text_color(b["text_color"], b["plate_color"]), b["plate_color"]

    def run(self, ctx: Ctx) -> dict:
        j, item = ctx.job, ctx.item
        rec = fragment_record(ctx)
        texts = read_yaml(j.design_texts(item), default=None)
        if not isinstance(texts, dict) or not texts.get("thumbnail_text"):
            raise VPError("data", f"{item}: design_texts.yaml has no thumbnail_text")
        text = str(texts["thumbnail_text"])
        fg, plate = self._colors(ctx)
        contrast = contrast_ratio(fg, plate)
        if contrast < MIN_CONTRAST:
            raise VPError("data", f"{item}: thumbnail text contrast {contrast:.2f}:1 < {MIN_CONTRAST}:1")
        st = s7_settings(ctx.config)
        cues = read_srt(j.clip_srt(item))
        dur = media.duration_ms(j.cut_mp4(item))
        tmp = Path(tempfile.mkdtemp(prefix="vp-s7-"))
        try:
            samples = []
            for k, t in enumerate(sample_times(dur, st["candidates"])):
                png = tmp / f"f{k:02d}.png"
                media.extract_still(j.cut_mp4(item), t, png)
                samples.append({"t_ms": t, "png": str(png), "black": media.is_black_frame(png),
                                "blur": blur_of(png), "speech": speech_at(cues, t)})
            cands = rank_candidates(samples, st["top"])
            if not cands:
                raise VPError("data", f"{item}: no usable thumbnail frame (all black)")
            task = f"s7.{item}"
            inputs = {"fragment": fragment_input(rec), "thumbnail_text": text,
                      "candidates": candidates_input(cands)}
            raw = ctx.judge.run(task, load_prompt(PROMPT), inputs, "output.json", ctx)
            n = parse_choice(raw, cands, task)
            chosen = next(c for c in cands if c["n"] == n)
            ctx.renderer.render_thumbnail(chosen["png"], text, j.thumb_png(item))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        # ---- acceptance -------------------------------------------------------------------
        out = j.thumb_png(item)
        info = media.probe(out)
        size = out.stat().st_size
        if (info["width"], info["height"]) != (THUMB_W, THUMB_H):
            raise VPError("data", f"{item}: thumbnail {info['width']}x{info['height']}, want {THUMB_W}x{THUMB_H}")
        if size >= THUMB_MAX_BYTES:
            raise VPError("data", f"{item}: thumbnail {size} bytes ≥ 2 MB")
        return {"candidate": n, "candidates": len(cands), "bytes": size,
                "contrast": round(contrast, 2), "blur": round(chosen["blur"], 3)}


STEP = S7Thumbnail()
