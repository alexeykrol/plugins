"""S5 — render (SPEC S5). Code: ctx.renderer (FfmpegRenderer in v1).

  reel       intro/outro cards (texts from design_texts.yaml), burned subtitles from clip.srt,
             channel plate
  long/full  intro/outro cards; subtitles NOT burned (final.srt goes to the platform, R7)
  all        loudness −14 LUFS ± 1 (two-pass loudnorm inside the renderer)

Outputs: render.mp4; render.manifest.yaml {kind, intro_ms, outro_ms, clip_ms, duration_ms,
lufs, renderer}; final.srt = clip.srt shifted by intro_ms — the only timeline anything outside
the clip refers to (t_final = t_clip + intro_ms).

Acceptance (code): duration = clip + cards ± 1 frame and equals the manifest; LUFS measured on
render.mp4 within tolerance; final.srt has no overlaps, its last cue ends no later than the end
of speech (intro_ms + clip duration) — cues are clipped there, never moved.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..adapters import media
from ..adapters.render import brand_settings, card_durations, measure_lufs, render_settings, video_info
from ..core.io import read_yaml, write_yaml
from ..core.srt import Cue, read_srt, write_srt
from .base import Ctx, StepSpec, VPError
from .s4_cut import fragment_record


def final_cues(clip_cues: list[Cue], intro_ms: int, clip_ms: int) -> list[Cue]:
    """clip.srt -> final timeline: shift by intro_ms, clip at the end of speech
    (intro_ms + clip_ms), drop cues that end up empty; renumbered 1..n by the writer."""
    speech_end = intro_ms + clip_ms
    out = []
    for c in clip_cues:
        s = c.shifted(intro_ms)
        if s.start_ms >= speech_end:
            continue
        if s.end_ms > speech_end:
            s = replace(s, end_ms=speech_end)
        if s.end_ms > s.start_ms:
            out.append(s)
    return out


def final_srt_violations(cues: list[Cue], intro_ms: int, clip_ms: int) -> list[str]:
    """Acceptance of final.srt: no overlaps, nothing before the intro end, last cue not after speech."""
    out = []
    for a, b in zip(cues, cues[1:]):
        if b.start_ms < a.end_ms:
            out.append(f"overlap {a.id}/{b.id}")
    if cues and cues[0].start_ms < intro_ms:
        out.append("cue_in_intro")
    if cues and cues[-1].end_ms > intro_ms + clip_ms:
        out.append("last_cue_after_speech")
    return out


class S5Render(StepSpec):
    id, deps, scope, version, gate = "S5", ("S6a",), "item", "1", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        j, i = ctx.job, ctx.item
        return [j.cut_mp4(i), j.clip_srt(i), j.design_texts(i)]

    def config(self, ctx: Ctx) -> dict:
        kind = fragment_record(ctx)["kind"]
        return {"kind": kind, "renderer": ctx.config.get("renderer", "ffmpeg"),
                "brand": brand_settings(ctx.config), "render": render_settings(ctx.config),
                "cards": list(card_durations(ctx.config, kind))}

    def outputs(self, ctx: Ctx) -> list[Path]:
        j, i = ctx.job, ctx.item
        return [j.render_mp4(i), j.render_manifest(i), j.final_srt(i)]

    def run(self, ctx: Ctx) -> dict:
        j, item = ctx.job, ctx.item
        kind = fragment_record(ctx)["kind"]
        texts = read_yaml(j.design_texts(item), default=None)
        if not isinstance(texts, dict):
            raise VPError("data", f"{item}: design_texts.yaml missing or invalid")
        clip_info = video_info(j.cut_mp4(item))
        clip_ms = clip_info["video_ms"] or media.duration_ms(j.cut_mp4(item))
        res = ctx.renderer.render_clip(j.cut_mp4(item), j.clip_srt(item), texts, kind, j.render_mp4(item))
        try:
            intro_ms, outro_ms = int(res["intro_ms"]), int(res["outro_ms"])
        except (KeyError, TypeError, ValueError) as e:
            raise VPError("contract", f"{item}: renderer returned {res!r}") from e

        # ---- acceptance -------------------------------------------------------------------
        frame_ms = 1000.0 / (clip_info["fps"] or 25.0)
        duration_ms = media.duration_ms(j.render_mp4(item))
        expected = clip_ms + intro_ms + outro_ms
        if abs(duration_ms - expected) > frame_ms + 0.5:
            raise VPError("data", f"{item}: render is {duration_ms} ms, expected clip+cards {expected} ± 1 frame")
        if res.get("duration_ms") is not None and abs(int(res["duration_ms"]) - duration_ms) > frame_ms + 0.5:
            raise VPError("contract", f"{item}: renderer reported {res['duration_ms']} ms, file is {duration_ms} ms")
        if not media.has_audio(j.render_mp4(item)):
            raise VPError("data", f"{item}: render has no audio")
        rs = render_settings(ctx.config)
        lufs = measure_lufs(j.render_mp4(item))
        if abs(lufs - float(rs["lufs"])) > float(rs["lufs_tolerance"]):
            raise VPError("data", f"{item}: loudness {lufs:.2f} LUFS, want {rs['lufs']} ± {rs['lufs_tolerance']}")
        cues = final_cues(read_srt(j.clip_srt(item)), intro_ms, clip_ms)
        bad = final_srt_violations(cues, intro_ms, clip_ms)
        if bad:
            raise VPError("data", f"{item}: final.srt invalid: {bad[:5]}")

        write_srt(j.final_srt(item), cues, renumber=True)
        manifest: dict[str, Any] = {"item": item, "kind": kind, "intro_ms": intro_ms, "outro_ms": outro_ms,
                                    "clip_ms": clip_ms, "duration_ms": duration_ms,
                                    "lufs": round(lufs, 2), "renderer": type(ctx.renderer).__name__}
        write_yaml(j.render_manifest(item), manifest)
        return {"kind": kind, "duration_ms": duration_ms, "intro_ms": intro_ms, "outro_ms": outro_ms,
                "lufs": round(lufs, 2)}


STEP = S5Render()
