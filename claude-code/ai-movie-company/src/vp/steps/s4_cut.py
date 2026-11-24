"""S4 — cut (SPEC S4). Code, ffmpeg. One instance per item (fragments of fragments.yaml + "full").

  Every kind first finds the source's ACTIVE PICTURE AREA (vp.adapters.picture.active_area:
  cropdetect at several points of the fragment, widest rectangle seen) — a Zoom recording is often
  a 16:9 picture inside a 21:9 canvas, with its own black bars (incident 2026-11-18).

  reel         the largest 9:16 rectangle inside the active area, centred on the profile band
               `reel_crop: {x_frac, w_frac}`, scaled to fill <source height>x<9:16 width>. A reel is
               never letterboxed or pillarboxed. (Face tracking is v2.)
  long         the active area when the source has bars (cropped, not scaled), else the frame
  full         the whole recording: copied byte for byte when the source has no bars; cropped to
               the active area (re-encode) when it has them

clip.srt = subtitles.srt sliced to [in, out) and rebased to 0 (the cut timeline).

Acceptance (code): duration = out − in ± 1 frame; audio present; first frame not black;
clip.srt has no overlaps and no cue after the end of the cut; the picture gate
(vp.adapters.picture.inspect: size/aspect, black bars, black stretches, frozen picture, silence)
passes — its report goes to cut.qa.yaml, also when it fails. Checks the owner waived for this clip
in picture.accept.yaml (`waive: [bars, ...]`, with a reason) are reported, not enforced.
The cut is the media editor exports (CapCut, FCPXML) take, so this gate covers them too.

Also exports `fragment_record(ctx)` — the item's fragment as a normalised dict, used by S6a/S6b.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..adapters import media, picture
from ..core.config import DEFAULTS, config_path, deep_merge
from ..core.io import read_yaml, write_yaml
from ..core.manifest import read_manifest
from ..core.srt import read_srt, slice_cues, write_srt
from ..core.timefmt import parse_time
from .base import FULL, Ctx, StepSpec, VPError, item_kind

FRAME_TOLERANCE = 1  # frames

# The 0.8.8 key included the whole picture_qa overlay. Keep its defaults independent
# of the defaults used by a new cut or render.
PICTURE_KEY_DEFAULTS = {
    "samples": 7, "sample_frames": 10, "crop_limit": 24, "bar_min_frac": 0.01,
    "bar_max_luma": 20, "bar_confirm_points": 3,
    "aspect_tolerance": 0.01, "black_min_s": 1.0, "black_pixel_threshold": 0.03,
    "freeze_noise_db": -60, "freeze_min_s": 2.0,
    "frozen_share_max": {"reel": 0.99, "long": None, "full": None},
    "silence_db": -40.0, "silence_max_s": {"reel": 10.0, "long": None, "full": None},
}


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


def cut_qa(job, item: str) -> Path:
    """Picture-gate report of the cut (S4 output)."""
    return job.item_dir(item) / "cut.qa.yaml"


def picture_accept(job, item: str) -> Path:
    """Owner's waiver for one clip: {waive: [check names], visual: bool, reason: text}."""
    return job.item_dir(item) / "picture.accept.yaml"


def waived_checks(ctx: Ctx) -> list[str]:
    """Code checks the owner accepted for this clip (picture.accept.yaml `waive`); a waiver
    without a reason is ignored."""
    doc = read_yaml(picture_accept(ctx.job, ctx.item), default=None)
    if not isinstance(doc, dict) or not str(doc.get("reason") or "").strip():
        return []
    return sorted(str(x) for x in doc.get("waive") or [])


def written_key_config(config: dict[str, Any]) -> dict[str, Any] | None:
    """Recover which values were written before load_config filled in today's defaults."""
    home = config.get("vp_home")
    if not home:
        return None
    path = config_path(Path(home))
    written = read_yaml(path, default={}) if path.is_file() else None
    return written if isinstance(written, dict) else None


def key_overlay(live: Any, defaults: Any, written: Any) -> Any:
    """Keep written settings and overrides, dropping values supplied only by current defaults."""
    if not isinstance(live, dict) or not isinstance(defaults, dict):
        return live
    written = written if isinstance(written, dict) else {}
    return {
        name: key_overlay(value, defaults.get(name), written.get(name))
        for name, value in live.items()
        if name in written or name not in defaults or value != defaults[name]
    }


def picture_key_settings(ctx: Ctx) -> dict[str, Any]:
    live = ctx.config.get("picture_qa") or {}
    defaults = DEFAULTS["picture_qa"]
    if defaults != PICTURE_KEY_DEFAULTS:
        written = written_key_config(ctx.config)
        if written is not None:
            live = key_overlay(live, defaults, written.get("picture_qa"))
    return deep_merge(PICTURE_KEY_DEFAULTS, live)


def key_waivers(live: list[str], previous: dict[str, Any]) -> list[str]:
    """Caption sync is independent of the draw, unless already keyed by the saved stage."""
    include_caption_sync = "caption_sync" in (previous.get("waive") or [])
    return [name for name in live if include_caption_sync or name != "caption_sync"]


class S4Cut(StepSpec):
    id, deps, scope, version, gate = "S4", ("S3b",), "item", "2", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.source_video, ctx.job.subtitles_srt]

    def config(self, ctx: Ctx) -> dict:
        rec = fragment_record(ctx)
        cfg: dict[str, Any] = {"fragment": {k: rec[k] for k in ("id", "kind", "start_ms", "end_ms")}}
        if rec["kind"] == "reel":
            crop = ctx.profile.get("reel_crop") or {}
            cfg["reel_crop"] = {"x_frac": float(crop.get("x_frac")), "w_frac": float(crop.get("w_frac"))}
        previous = (read_manifest(ctx.job, self.id, ctx.item) or {}).get("config") or {}
        cfg["picture_qa"] = picture_key_settings(ctx)
        cfg["waive"] = key_waivers(waived_checks(ctx), previous)
        return cfg

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.cut_mp4(ctx.item), ctx.job.clip_srt(ctx.item), cut_qa(ctx.job, ctx.item)]

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
        st = picture.settings(ctx.config)
        src_area = picture.active_area(job.source_video, start, end, policy="union", st=st)
        W, H = src_info["width"], src_info["height"]
        crop_info: dict[str, Any] = {"source_area": src_area.as_dict(),
                                     "source_bars": picture.bars(W, H, src_area, st)}
        if rec["kind"] == "full" and not crop_info["source_bars"]:
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
            rect, out_w, out_h = picture.reel_crop_rect(W, H, src_area, crop["x_frac"], crop["w_frac"])
            crop_info.update(crop=rect.as_dict(), out_size=[out_w, out_h])
            media.cut(job.source_video, start, end, out,
                      vf=media.crop_scale_filter(rect.x, rect.y, rect.w, rect.h, out_w, out_h))
        else:
            rect = picture.long_crop_rect(W, H, src_area, st)
            vf = media.crop_scale_filter(rect.x, rect.y, rect.w, rect.h) if rect else None
            if rect:
                crop_info["crop"] = rect.as_dict()
            media.cut(job.source_video, start, end, out, vf=vf)

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
        report = picture.apply_waivers(picture.inspect(out, rec["kind"], config=ctx.config),
                                       waived_checks(ctx))
        write_yaml(cut_qa(job, item), {**report, **crop_info})
        picture.require_clean(report, f"{item} cut")
        cues = slice_cues(read_srt(job.subtitles_srt), start, end, rebase=True)
        bad = check_cues(cues, expected)
        if bad:
            raise VPError("data", f"{item}: clip.srt invalid: {bad[:5]}")
        write_srt(job.clip_srt(item), cues)
        return {"kind": rec["kind"], "duration_ms": info["duration_ms"], "cues": len(cues),
                "width": info["width"], "height": info["height"]}


STEP = S4Cut()
