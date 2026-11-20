"""S5v — verify the finished MP4, then check the picture by model judgment.

Code (S4/S5) already proved frame size, no black bars, no black or frozen picture, no long silence,
loudness. This step answers the rest with one look:

  code     N frames evenly over the clip part of render.mp4 (title/end cards excluded) -> one
           contact sheet clips/<item>/visual.sheet.png (reel: 360x640 cells, 3 per row)
  judge    task `s5v.<item>` (prompt s5v_visual.md) opens the sheet and answers fixed questions:
           face_in_frame yes|no|no_face, personal_data yes|no, subtitles_readable yes|no|no_subtitles,
           subtitles_cover_face yes|no, bars_or_empty yes|no, problem_frames [n], note
  code     decides: pass iff face_in_frame != no, personal_data == no, subtitles_readable != no,
           subtitles_cover_face == no, bars_or_empty == no. Writes clips/<item>/visual.yaml
           {ok, answers, failures, waived}. A failure stops the item before metadata and upload
           (VPError data) with the model's own words.

The owner can accept a failed visual verdict for one clip: clips/<item>/picture.accept.yaml
`visual: true` with a `reason` — the verdict is kept and shown as waived. The editing director
cites visual.yaml instead of "I have not seen the picture".
"""
from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from ..adapters import media
from ..core.io import read_yaml, write_yaml
from ..verify_mp4 import verify_mp4
from .base import Ctx, StepSpec, VPError, item_kind
from .s4_cut import picture_accept
from .s6a_design_texts import load_prompt, prompt_hash

PROMPT = "s5v_visual.md"
DEFAULTS = {"frames": 6}
COLS = 3
FIELDS = {
    "face_in_frame": ("yes", "no", "no_face"),
    "personal_data": ("yes", "no"),
    "subtitles_readable": ("yes", "no", "no_subtitles"),
    "subtitles_cover_face": ("yes", "no"),
    "bars_or_empty": ("yes", "no"),
}
FAIL_WHEN = {
    "face_in_frame": ("no", "the speaker's face is not fully in the frame"),
    "personal_data": ("yes", "personal data is readable on screen"),
    "subtitles_readable": ("no", "the subtitles are hard to read"),
    "subtitles_cover_face": ("yes", "subtitles or the plate cover the face"),
    "bars_or_empty": ("yes", "bars or an empty band in the frame"),
}
# A timecode (0:12, 01:24:20, 1:05.5, an SRT arrow) — not an aspect ratio: "9:16", "16:9", "4:3"
# and "21:9" are words about the frame, not times.
_TIME_RE = re.compile(r"(?<![\d:])(?!9:16(?![\d:]))\d{1,2}:[0-5]\d(?![\d])|-->|\d+\s*ms\b")


def visual_sheet(job, item: str) -> Path:
    return job.item_dir(item) / "visual.sheet.png"


def visual_yaml(job, item: str) -> Path:
    return job.item_dir(item) / "visual.yaml"


def planned_render_duration_s(manifest: Any) -> float | None:
    """Use the planned clip and card lengths, independent of measured duration_ms."""
    if not isinstance(manifest, dict):
        return None
    parts = [manifest.get(key) for key in ("clip_ms", "intro_ms", "outro_ms")]
    if any(type(ms) is not int or ms < 0 for ms in parts):
        return None
    total_ms = sum(parts)
    return total_ms / 1000 if total_ms else None


def s5v_settings(config: dict[str, Any]) -> dict[str, int]:
    s = dict(DEFAULTS)
    s.update({k: int(v) for k, v in (config.get("s5v") or {}).items() if k in DEFAULTS})
    return s


def visual_waiver(ctx: Ctx) -> str | None:
    """The owner's reason when picture.accept.yaml has `visual: true` and a reason, else None."""
    doc = read_yaml(picture_accept(ctx.job, ctx.item), default=None)
    if isinstance(doc, dict) and doc.get("visual") is True and str(doc.get("reason") or "").strip():
        return str(doc["reason"]).strip()
    return None


def sheet_times(start_ms: int, end_ms: int, n: int) -> list[int]:
    span = max(end_ms - start_ms, 1)
    return [int(start_ms + span * (k + 0.5) / n) for k in range(n)]


def build_sheet(video: Path, start_ms: int, end_ms: int, n: int, out_png: Path) -> Path:
    """Contact sheet of n frames of [start, end), COLS per row, cells 360x640 (vertical) or
    640x360-ish (horizontal); written atomically."""
    info = media.probe(video)
    W, H = info["width"], info["height"]
    if H > W:
        cw, ch = 360, 640
    else:
        cw = 640
        ch = int(640 * H / W) // 2 * 2
    args: list[str] = []
    for t in sheet_times(start_ms, end_ms, n):
        args += ["-ss", f"{t / 1000:.3f}", "-i", str(video)]
    cells = "".join(f"[{k}:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
                    f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2,setsar=1[c{k}];" for k in range(n))
    layout = "|".join(f"{(k % COLS) * cw}_{(k // COLS) * ch}" for k in range(n))
    graph = cells + "".join(f"[c{k}]" for k in range(n)) + \
        f"xstack=inputs={n}:layout={layout}:fill=black[out]"
    if n == 1:
        graph = cells.rstrip(";").replace("[c0]", "[out]")
    return media._render_to(out_png, [*args, "-filter_complex", graph, "-map", "[out]",
                                      "-frames:v", "1", "-update", "1", "-f", "image2", "-c:v", "png"])


def _norm(v: Any) -> str:
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return str(v).strip().lower() if v is not None else ""


def parse_answer(raw: str, n_frames: int, task: str) -> dict[str, Any]:
    """Validate the judge's YAML -> {field: value, problem_frames: [int], note: str}."""
    text = raw.strip()
    m = re.match(r"^```(?:yaml)?\s*\n(.*?)\n\s*```$", text, re.S)
    if m:
        text = m.group(1)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise VPError("contract", f"{task}: answer is not YAML: {e}") from e
    if not isinstance(doc, dict):
        raise VPError("contract", f"{task}: answer is not a YAML mapping")
    out: dict[str, Any] = {}
    for field, allowed in FIELDS.items():
        v = _norm(doc.get(field))
        if v not in allowed:
            raise VPError("contract", f"{task}: {field}={doc.get(field)!r}, want one of {allowed}")
        out[field] = v
    frames = doc.get("problem_frames") or []
    if not isinstance(frames, list) or not all(isinstance(f, int) and not isinstance(f, bool) and 1 <= f <= n_frames for f in frames):
        raise VPError("contract", f"{task}: problem_frames must be frame numbers 1..{n_frames}")
    note = str(doc.get("note") or "").strip()
    if _TIME_RE.search(note) or "http" in note:
        raise VPError("contract", f"{task}: note must not contain times or links")
    out["problem_frames"] = sorted(set(frames))
    out["note"] = note
    return out


def verdict(answers: dict[str, Any]) -> list[str]:
    """Failures decided by code from the structured answer."""
    return [text for field, (bad, text) in FAIL_WHEN.items() if answers.get(field) == bad]


class S5vVisual(StepSpec):
    id, deps, scope, version, gate = "S5v", ("S5",), "item", "2", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.render_mp4(ctx.item), ctx.job.render_manifest(ctx.item)]

    def config(self, ctx: Ctx) -> dict:
        return {"settings": s5v_settings(ctx.config), "prompt": prompt_hash(PROMPT),
                "waiver": visual_waiver(ctx)}

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [visual_sheet(ctx.job, ctx.item), visual_yaml(ctx.job, ctx.item)]

    def run(self, ctx: Ctx) -> dict:
        j, item = ctx.job, ctx.item
        kind = item_kind(ctx)
        man = read_yaml(j.render_manifest(item), default=None)
        if not isinstance(man, dict):
            raise VPError("data", f"{item}: render.manifest.yaml missing")
        render = j.render_mp4(item)
        # S5 requires audio for every current output, including the full video.
        verification = verify_mp4(render, audio_policy="required",
                                  expected_duration_s=planned_render_duration_s(man),
                                  duration_tolerance_s=0.5)
        mp4 = {"status": verification.status, "checks": [asdict(check) for check in verification.checks]}
        report = {"item": item, "kind": kind, "mp4_verification": mp4}
        if verification.status != "passed":
            if verification.status == "blocked":
                failure = "MP4 unmeasured (ffmpeg and ffprobe required)"
            else:
                names = ", ".join(check.name for check in verification.checks if check.status == "failed")
                failure = f"MP4 verification failed ({names})"
            write_yaml(visual_yaml(j, item), {
                **report, "ok": False, "answers": {}, "failures": [failure], "waived": False})
            raise VPError("data", f"{item}: {render.name}: {failure}", mp4_verification=mp4)

        start = int(man["intro_ms"])
        end = int(man["duration_ms"]) - int(man["outro_ms"])
        n = s5v_settings(ctx.config)["frames"]
        sheet = build_sheet(render, start, end, n, visual_sheet(j, item))
        clip = (f"kind: {'vertical reel' if kind == 'reel' else 'horizontal video'}\n"
                f"subtitles: {'burned into the picture' if kind == 'reel' else 'none in the picture'}\n"
                f"frames: {n}\n")
        task = f"s5v.{item}"
        raw = ctx.judge.run(task, load_prompt(PROMPT), {"clip": clip}, "output.yaml", ctx,
                            attachments={"sheet.png": sheet})
        answers = parse_answer(raw, n, task)
        failures = verdict(answers)
        waiver = visual_waiver(ctx)
        ok = not failures or waiver is not None
        write_yaml(visual_yaml(j, item), {
            **report, "ok": ok, "answers": answers, "failures": failures,
            "waived": bool(failures and waiver), "waiver_reason": waiver if failures else None,
            "checked_by": "model (S5v) from the contact sheet visual.sheet.png"})
        if not ok:
            frames = answers["problem_frames"]
            raise VPError("data", f"{item}: visual check failed — " + "; ".join(failures)
                          + (f" (frames {frames} of {n})" if frames else "")
                          + (f". Model: {answers['note']}" if answers["note"] else ""))
        return {"kind": kind, "ok": ok, "failures": failures, "waived": bool(failures and waiver),
                "mp4_verification": mp4}


STEP = S5vVisual()
