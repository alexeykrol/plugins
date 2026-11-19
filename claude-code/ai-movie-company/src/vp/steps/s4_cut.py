"""S4 — cut (SPEC S4). Code, ffmpeg. One instance per item (fragments of fragments.yaml + "full").

  long / full  source frame, uncropped (21:9 is shown by YouTube with bars)
  reel         9:16 band crop at the profile's fixed area `reel_crop: {x_frac, w_frac}` (v1;
               face tracking is v2)
  full         the whole recording: the source file is copied byte for byte (lossless, no re-encode)

clip.srt = subtitles.srt sliced to [in, out) and rebased to 0 (the cut timeline).

Acceptance (code): duration = out − in ± 1 frame; audio present; first frame not black;
clip.srt has no overlaps and no cue after the end of the cut.

Also exports `fragment_record(ctx)` — the item's fragment as a normalised dict, used by S6a/S6b.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..adapters import media
from ..core.io import read_yaml
from ..core.srt import read_srt, slice_cues, write_srt
from ..core.timefmt import parse_time
from .base import FULL, Ctx, StepSpec, VPError, item_kind

FRAME_TOLERANCE = 1  # frames


def _to_ms(value: Any, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise VPError("data", f"fragment {field}: missing")
    if isinstance(value, (int, float)):
        return int(round(float(value) * 1000))
    try:
        return parse_time(str(value))
    except ValueError as e:
        raise VPError("data", f"fragment {field}: not a time: {value!r}") from e


def fragment_record(ctx: Ctx) -> dict[str, Any]:
    """{id, kind, start_ms, end_ms, title, thesis} of ctx.item.

    fragments.yaml fields accepted (first found wins): start_ms/end_ms (int ms) |
    in_ms/out_ms | start/end (seconds as number, or "h:m:s"); title | name; thesis.
    For "full": [0, source duration), title from request.yaml `title` (may be empty).
    """
    if ctx.item == FULL:
        req = ctx.job.request()
        return {"id": FULL, "kind": "full", "start_ms": 0,
                "end_ms": media.duration_ms(ctx.job.source_video),
                "title": str(req.get("title") or ""), "thesis": str(req.get("thesis") or "")}
    kind = item_kind(ctx)
    data = read_yaml(ctx.job.fragments_yaml, default={}) or {}
    frag = next(f for f in data.get("fragments") or [] if str(f.get("id")) == ctx.item)
    if "start_ms" in frag:
        start, end = int(frag["start_ms"]), int(frag["end_ms"])
    elif "in_ms" in frag:
        start, end = int(frag["in_ms"]), int(frag["out_ms"])
    else:
        start, end = _to_ms(frag.get("start"), "start"), _to_ms(frag.get("end"), "end")
    if start < 0 or end <= start:
        raise VPError("data", f"fragment {ctx.item}: bad range {start}..{end}")
    return {"id": ctx.item, "kind": kind, "start_ms": start, "end_ms": end,
            "title": str(frag.get("title") or frag.get("name") or ""),
            "thesis": str(frag.get("thesis") or "")}


def check_cues(cues, duration_ms: int) -> list[str]:
    """Violations of a cut-timeline SRT: overlaps, negative/empty cues, cue past the end."""
    out = []
    for a, b in zip(cues, cues[1:]):
        if b.start_ms < a.end_ms:
            out.append(f"overlap {a.id}/{b.id}")
    for c in cues:
        if c.start_ms < 0 or c.end_ms <= c.start_ms:
            out.append(f"bad cue {c.id}")
        if c.end_ms > duration_ms:
            out.append(f"cue {c.id} after end")
    return out


class S4Cut(StepSpec):
    id, deps, scope, version, gate = "S4", ("S3b",), "item", "1", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.source_video, ctx.job.subtitles_srt]

    def config(self, ctx: Ctx) -> dict:
        rec = fragment_record(ctx)
        cfg: dict[str, Any] = {"fragment": {k: rec[k] for k in ("id", "kind", "start_ms", "end_ms")}}
        if rec["kind"] == "reel":
            crop = ctx.profile.get("reel_crop") or {}
            cfg["reel_crop"] = {"x_frac": float(crop.get("x_frac")), "w_frac": float(crop.get("w_frac"))}
        return cfg

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.cut_mp4(ctx.item), ctx.job.clip_srt(ctx.item)]

    def run(self, ctx: Ctx) -> dict:
        job, item = ctx.job, ctx.item
        rec = fragment_record(ctx)
        start, end = rec["start_ms"], rec["end_ms"]
        src_info = media.probe(job.source_video)
        if end > src_info["duration_ms"] + 1000 / (src_info["fps"] or 25):
            raise VPError("data", f"{item}: fragment end {end} ms beyond the video ({src_info['duration_ms']} ms)")
        end = min(end, src_info["duration_ms"])
        out = job.cut_mp4(item)
        out.parent.mkdir(parents=True, exist_ok=True)
        if rec["kind"] == "full":
            fd, tmp = tempfile.mkstemp(prefix=".cut.", suffix=".mp4", dir=out.parent)
            os.close(fd)
            try:
                shutil.copyfile(job.source_video, tmp)
                os.replace(tmp, out)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        elif rec["kind"] == "reel":
            crop = self.config(ctx)["reel_crop"]
            fd, tmp = tempfile.mkstemp(prefix=".uncropped.", suffix=".mp4", dir=out.parent)
            os.close(fd)
            try:
                media.cut(job.source_video, start, end, tmp)
                media.crop_fraction(tmp, out, crop["x_frac"], crop["w_frac"])
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        else:
            media.cut(job.source_video, start, end, out)

        # ---- acceptance -------------------------------------------------------------------
        info = media.probe(out)
        fps = info["fps"] or src_info["fps"] or 25.0
        frame_ms = 1000.0 / fps
        expected = end - start
        if abs(info["duration_ms"] - expected) > FRAME_TOLERANCE * frame_ms + 0.5:
            raise VPError("data", f"{item}: cut is {info['duration_ms']} ms, expected {expected} ± 1 frame")
        if not info["has_audio"]:
            raise VPError("data", f"{item}: cut has no audio")
        if media.is_black_frame(out, 0):
            raise VPError("data", f"{item}: first frame of the cut is black")
        if rec["kind"] == "reel":
            ratio = info["width"] / info["height"]
            if abs(ratio - 9 / 16) > 0.03:
                raise VPError("data", f"{item}: reel frame {info['width']}x{info['height']} is not 9:16")
        cues = slice_cues(read_srt(job.subtitles_srt), start, end, rebase=True)
        bad = check_cues(cues, expected)
        if bad:
            raise VPError("data", f"{item}: clip.srt invalid: {bad[:5]}")
        write_srt(job.clip_srt(item), cues)
        return {"kind": rec["kind"], "duration_ms": info["duration_ms"], "cues": len(cues),
                "width": info["width"], "height": info["height"]}


STEP = S4Cut()
