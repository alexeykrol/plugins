"""One CapCut project per job; every version is a new TIMELINE in it, laid out layer by layer.

Owner decisions (2026-11-20/21, test signals 2026-11-21 B1, M2, M3, M4):
  * a CapCut project never holds the finished render: every asset is its own segment on its own
    track (background, overlays, texts, subtitles, voice, music);
  * one project per job; a new version adds a timeline («v2», «v3» …) to it and never touches the
    timelines already there (the person's hand edits live in them);
  * cards keep their shape: every picture segment is scaled uniformly
    (`uniform_scale = {on: true, value: 1.0}` — `value` is the locked x/y RATIO, not the scale;
    writing the scale there drew the cards ~16x taller than wide);
  * library items are written by id in CapCut 9.8's own form (vp.adapters.capcut_library).

Ported from the hand-built, CapCut-accepted builders of the Elsa promo job
(`capcut/project/build_project.py` — multi-timeline project, accepted by CapCut 9.8 on
2026-11-21; `capcut/v3/build_v3.py` — stock on tracks with transform/crop; `capcut/v4/build_v4.py`
— library transitions/filters/text animations, timeline added in place) and the corrected rules of
`~/Movies/AI Movie Company/_capcut-kb` (README: K1, K2, L1–L3, «Эталон s02→s03»).

Project anatomy (docs/research/capcut-multitimeline-anatomy-2026-11-20.md):
  <project>/Timelines/project.json       timelines[] + main_timeline_id
  <project>/Timelines/<id>/draft_info.json  one complete edit per timeline (own material ids)
  <project>/draft_info.json              copy of the MAIN timeline (older readers)
  <project>/timeline_layout.json         UI: timelines docked side by side
  <project>/draft_biz_config.json        per-timeline settings, linkage_enabled false
  <project>/draft_meta_info.json         project media pool
  <project>/Resources/media/             every file the timelines use (CapCut is sandboxed)

Layers of a text job come from `<job>/capcut/layers.json` (written in the dialog; NO times —
items are anchored to scene ids and cue numbers, code computes every time from timeline.json):

  {"name": "сток по слоям",                       optional label of the timeline
   "canvas": [1920, 1080],                         optional (default: the render's frame, else 1920x1080)
   "tracks": [
     {"name": "Фон", "items": [                     video track (the first one is the main track)
        {"scene": "S1", "media": "stock/123.mp4",   one scene; or "scenes": ["S2", "S3"]; or
                                                    "scene": "S2", "cue": 3 (the cue's span)
         "fit": "cover",                            cover | contain (default: cover on the main
                                                    track, contain elsewhere); never stretched
         "box": [1320, 80, 520, 292],               optional target rect on the canvas, px
         "crop": [0, 0, 425, 480],                  optional source rect, px (see CROP_MODEL)
         "sound": false,                            keep the clip's own sound (default off)
         "filter": "Dim Light", "filter_value": 0.6,
         "effect": "Sparkle Flare",
         "animations": ["Fade In"],                 clip animations
         "transition": "Black Fade"}]},             into the next clip of the same track
     {"name": "Надписи", "type": "text", "items": [
        {"scene": "S2", "text": "Эльза — продюсер", "pos": [560, 830], "size": 7,
         "color": "#1d1b19", "background": "#f4efe4", "animations": ["Fade In", "Pop In"]}]},
     {"name": "Стикеры", "type": "sticker", "items": [
        {"scene": "S1", "sticker": "Clapper", "pos": [960, 470], "scale": 1.0}]}]}

Several items of one track on the same span share it evenly, in order. Without layers.json a
text job gets its storyboard sketches (sketches/<scene id>.png) on the main track; with neither,
the rendered picture is used and the export says so (a flattened fallback).
"""
from __future__ import annotations

import copy
import dataclasses
import filecmp
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..core.errors import VPError
from ..core.io import atomic_write, now_iso, read_json
from ..core.paths import vp_home
from . import capcut_library as lib

PROJECT_LINK = "project.json"          # <job>/capcut/project.json: which CapCut project is the job's
PROJECT_CLIP_ID = "job-project"        # marker clip_id of a per-job multi-timeline project
LAYERS_FILE = "layers.json"
WRITTEN_DIR = "written"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
CROP_MODEL = "A"   # cropped region fitted to the canvas, then scaled (capcut-format-reference
                   # §1.3/1.5). Unverified by a CapCut open (probe 4.1); without a crop both models
                   # agree, so only explicit crops depend on it.
UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
SMALL_FILES = ("Timelines/project.json", "timeline_layout.json", "draft_biz_config.json",
               "draft_meta_info.json", "draft_info.json")


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---- placement: aspect kept, never stretched -------------------------------------------------

def place(src_w: int, src_h: int, canvas_w: int, canvas_h: int, *, fit: str = "contain",
          box: tuple[float, float, float, float] | None = None,
          crop: tuple[float, float, float, float] | None = None) -> dict[str, Any]:
    """CapCut clip fields that show the (cropped) picture inside `box` keeping its shape (pure).

    scale = 1.0 means "fitted inside the canvas" (CapCut's default placement); the scale is
    uniform, so x = y and the picture can never be stretched. `fit` contain: the whole picture
    inside the box; cover: the box filled, overflow cut by the canvas. `box` = (x, y, w, h) px from
    the canvas top-left (default the whole canvas). `crop` = (x, y, w, h) source px.
    Returns {scale, transform, crop (normalised quad or None), size_px (w, h on the canvas)}."""
    if not src_w or not src_h:
        raise VPError("data", "a picture layer needs its width and height")
    if fit not in ("contain", "cover"):
        raise VPError("data", f"fit must be contain or cover, not {fit!r}")
    cx, cy, cw, ch = crop if crop else (0.0, 0.0, float(src_w), float(src_h))
    if cw <= 0 or ch <= 0 or cx < 0 or cy < 0 or cx + cw > src_w + 0.5 or cy + ch > src_h + 0.5:
        raise VPError("data", f"crop {crop} is outside the {src_w}x{src_h} picture")
    bx, by, bw, bh = box if box else (0.0, 0.0, float(canvas_w), float(canvas_h))
    if bw <= 0 or bh <= 0:
        raise VPError("data", f"box {box} has no size")
    a = cw / ch
    wfit = min(canvas_w, canvas_h * a)            # the (cropped) picture fitted to the canvas
    hfit = wfit / a
    k = (min if fit == "contain" else max)(bw / wfit, bh / hfit)
    quad = None
    if crop:
        l, t, r, b = cx / src_w, cy / src_h, (cx + cw) / src_w, (cy + ch) / src_h
        quad = {"upper_left_x": l, "upper_left_y": t, "upper_right_x": r, "upper_right_y": t,
                "lower_left_x": l, "lower_left_y": b, "lower_right_x": r, "lower_right_y": b}
    X, Y = bx + bw / 2, by + bh / 2
    return {"scale": {"x": float(k), "y": float(k)},
            "transform": {"x": (X - canvas_w / 2) / (canvas_w / 2),
                          "y": (canvas_h / 2 - Y) / (canvas_h / 2)},
            "crop": quad, "size_px": (wfit * k, hfit * k)}


def px_to_transform(x: float, y: float, canvas_w: int, canvas_h: int) -> dict[str, float]:
    """Canvas pixel centre -> CapCut clip.transform (x right, y up, ±1 = canvas edge)."""
    return {"x": (x - canvas_w / 2) / (canvas_w / 2), "y": (canvas_h / 2 - y) / (canvas_h / 2)}


# ---- the layer plan ----------------------------------------------------------------------------

@dataclasses.dataclass
class Item:
    """One resolved segment of a layer: times in frames, media by source path (copied later)."""
    track: str
    kind: str                      # video | text | sticker
    start: int                     # frame on the timeline
    frames: int
    spec: dict[str, Any]
    src: Path | None = None        # media file (video/still) before it is copied into the project
    still: bool = False            # an image made into a still clip
    width: int = 0
    height: int = 0
    media_frames: int = 0          # length of the media file in frames (video)
    has_audio: bool = False


@dataclasses.dataclass
class Plan:
    name: str
    canvas_w: int
    canvas_h: int
    fps: float
    total_frames: int
    tracks: list[tuple[str, str]]          # (name, kind) in order
    items: list[Item]
    warnings: list[str]
    source: str                            # layers.json | sketches | render | video


def _scene_spans(tl_json: dict[str, Any]) -> dict[str, tuple[int, int, dict[str, Any]]]:
    out = {}
    for s in tl_json["scenes"]:
        out[str(s["id"])] = (int(s["from"]), int(s["from"]) + int(s["durationInFrames"]), s)
    return out


def item_span(spec: dict[str, Any], spans: dict[str, tuple[int, int, dict]],
              order: list[str]) -> tuple[int, int]:
    """[start, end) frames of a plan item from its scene / scenes / cue anchors (pure)."""
    if "scenes" in spec:
        ids = [str(i) for i in spec["scenes"]]
        if not ids or any(i not in spans for i in ids):
            raise VPError("data", f"layers.json: unknown scene in {ids}; scenes: {', '.join(order)}")
        if "cue" in spec:
            raise VPError("data", "layers.json: `cue` goes with one `scene`, not with `scenes`")
        idx = sorted(order.index(i) for i in ids)
        if idx != list(range(idx[0], idx[-1] + 1)):
            raise VPError("data", f"layers.json: scenes {ids} are not consecutive")
        return spans[order[idx[0]]][0], spans[order[idx[-1]]][1]
    sid = str(spec.get("scene", ""))
    if sid not in spans:
        raise VPError("data", f"layers.json: item needs a known `scene` (got {sid!r}); scenes: "
                              f"{', '.join(order)}")
    a, b, scene = spans[sid]
    if "cue" in spec:
        cues = scene.get("cues") or []
        n = int(spec["cue"])
        if not 1 <= n <= len(cues):
            raise VPError("data", f"layers.json: scene {sid} has {len(cues)} cues, not cue {n}")
        c = cues[n - 1]
        return a + int(c["from"]), a + int(c["to"])
    return a, b


def resolve_plan(raw: dict[str, Any], tl_json: dict[str, Any], job_root: Path, *,
                 canvas: tuple[int, int], probe: Callable[[Path], dict]) -> Plan:
    """layers.json -> Plan with every time computed from timeline.json (pure apart from probe)."""
    fps = float(tl_json.get("fps") or 30)
    total = int(tl_json["durationInFrames"])
    spans = _scene_spans(tl_json)
    order = [str(s["id"]) for s in tl_json["scenes"]]
    if isinstance(raw.get("canvas"), list) and len(raw["canvas"]) == 2:
        canvas = (int(raw["canvas"][0]), int(raw["canvas"][1]))
    tracks_raw = raw.get("tracks")
    if not isinstance(tracks_raw, list) or not tracks_raw:
        raise VPError("data", "layers.json: needs {\"tracks\": [{\"name\": ..., \"items\": [...]}]}")
    tracks, items, warnings = [], [], []
    probed: dict[Path, dict] = {}
    for ti, tr in enumerate(tracks_raw):
        name = str(tr.get("name") or f"Слой {ti + 1}")
        kind = str(tr.get("type") or "video")
        if kind not in ("video", "text", "sticker"):
            raise VPError("data", f"layers.json: track {name!r} type must be video, text or sticker")
        if any(n == name for n, _k in tracks):
            raise VPError("data", f"layers.json: two tracks are called {name!r}")
        tracks.append((name, kind))
        groups: dict[tuple[int, int], list[dict]] = {}
        for spec in tr.get("items") or []:
            if not isinstance(spec, dict):
                raise VPError("data", f"layers.json: track {name!r} has an item that is not an object")
            groups.setdefault(item_span(spec, spans, order), []).append(spec)
        for (a, b), specs in sorted(groups.items()):
            n = len(specs)
            for k, spec in enumerate(specs):       # several items on one span share it evenly
                s0, s1 = a + (b - a) * k // n, a + (b - a) * (k + 1) // n
                if s1 <= s0:
                    raise VPError("data", f"layers.json: no room for {n} items on track {name!r}")
                it = Item(track=name, kind=kind, start=s0, frames=s1 - s0, spec=spec)
                if kind == "video":
                    _resolve_media(it, job_root, fps, probe, probed, warnings)
                elif kind == "text" and not str(spec.get("text") or "").strip():
                    raise VPError("data", f"layers.json: text item on {name!r} has no text")
                elif kind == "sticker":
                    lib.find("sticker", spec.get("sticker", ""))
                items.append(it)
        ends: list[tuple[int, int]] = sorted((i.start, i.start + i.frames) for i in items
                                             if i.track == name)
        for (a0, a1), (b0, _b1) in zip(ends, ends[1:]):
            if b0 < a1:
                raise VPError("data", f"layers.json: items overlap on track {name!r} at frame {b0}"
                                      f" (put one of them on another track)")
    if not any(k == "video" for _n, k in tracks):
        raise VPError("data", "layers.json: needs at least one video track")
    return Plan(name=str(raw.get("name") or ""), canvas_w=canvas[0], canvas_h=canvas[1], fps=fps,
                total_frames=total, tracks=tracks, items=items, warnings=warnings,
                source=LAYERS_FILE)


def _resolve_media(it: Item, job_root: Path, fps: float, probe: Callable[[Path], dict],
                   probed: dict[Path, dict], warnings: list[str]) -> None:
    media = it.spec.get("media")
    if not media:
        raise VPError("data", f"layers.json: video item on {it.track!r} needs `media`")
    p = Path(str(media)).expanduser()
    p = p if p.is_absolute() else job_root / p
    if not p.is_file():
        raise VPError("data", f"layers.json: media not found: {p}")
    if p not in probed:
        probed[p] = probe(p)
    info = probed[p]
    if not info.get("width") or not info.get("height"):
        raise VPError("data", f"layers.json: {p.name} has no picture")
    it.src, it.width, it.height = p, int(info["width"]), int(info["height"])
    it.still = p.suffix.lower() in IMAGE_EXT
    it.has_audio = bool(info.get("has_audio")) and not it.still
    if it.still:
        it.media_frames = it.frames
        return
    it.media_frames = int(int(info.get("duration_ms") or 0) * fps // 1000)
    if it.media_frames <= 0:
        raise VPError("data", f"layers.json: {p.name} has no length")
    if it.media_frames < it.frames:
        warnings.append(f"{p.name} on {it.track!r} is {it.media_frames} frames, shorter than its "
                        f"{it.frames}-frame span: the segment ends with the clip")
        it.frames = it.media_frames


def default_plan(tl_json: dict[str, Any], job_root: Path, *, canvas: tuple[int, int],
                 probe: Callable[[Path], dict], render: Path | None,
                 explicit_video: bool = False) -> Plan:
    """No layers.json: the storyboard sketches on the main track (one per scene), else the render."""
    order = [str(s["id"]) for s in tl_json["scenes"]]
    sketches = {}
    sk = job_root / "sketches"
    if sk.is_dir() and not explicit_video:
        files = {f.stem.lower(): f for f in sorted(sk.iterdir())
                 if f.is_file() and f.suffix.lower() in IMAGE_EXT}
        for n, sid in enumerate(order):
            for want in (sid.lower(), f"s{n + 1:02d}", f"s{n + 1}"):
                if want in files:
                    sketches[sid] = files[want]
                    break
    if sketches:
        raw = {"tracks": [{"name": "Эскизы", "items": [
            {"scene": sid, "media": str(sketches[sid]), "fit": "contain"} for sid in order
            if sid in sketches]}]}
        plan = resolve_plan(raw, tl_json, job_root, canvas=canvas, probe=probe)
        missing = [sid for sid in order if sid not in sketches]
        if missing:
            plan.warnings.append("no sketch for scene(s) " + ", ".join(missing))
        plan.source, plan.name = "sketches", "эскизы"
        return plan
    if render is None:
        raise VPError("data", f"nothing to put on the picture track: no {LAYERS_FILE}, no "
                              f"sketches/<scene>.png and no render in {job_root / 'renders'}")
    raw = {"tracks": [{"name": "Картинка", "items": [
        {"scenes": order, "media": str(render), "fit": "contain"}]}]}
    plan = resolve_plan(raw, tl_json, job_root, canvas=canvas, probe=probe)
    plan.source = "video" if explicit_video else "render"
    plan.name = "картинка"
    if not explicit_video:
        plan.warnings.append(f"no {LAYERS_FILE} and no sketches: the picture track holds the "
                             f"flattened render {render.name} (layers are not separate)")
    return plan


def load_layers(job_root: Path) -> dict[str, Any] | None:
    p = Path(job_root) / "capcut" / LAYERS_FILE
    if not p.is_file():
        return None
    try:
        data = read_json(p)
    except ValueError as e:
        raise VPError("data", f"{p}: not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise VPError("data", f"{p}: must be a JSON object")
    return data


# ---- draft_info of a layered timeline ----------------------------------------------------------

def _hex_rgb(c: str) -> list[float]:
    c = str(c).lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", c):
        raise VPError("data", f"colour must be #RRGGBB, not {c!r}")
    return [round(int(c[i:i + 2], 16) / 255, 6) for i in (0, 2, 4)]


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def build_layered_info(plan: Plan, media_paths: dict[int, Path], tpl: dict, atpl: dict,
                       timeline_id: str, *, cues: list | None = None, audio: list | None = None,
                       sub_kind: str = "long", uid: Callable[[], str] = _uid) -> dict[str, Any]:
    """draft_info.json of one layered timeline (pure).

    `media_paths[i]` = where plan.items[i]'s media sits inside the project (final path). Tracks
    in order: the plan's video/text/sticker tracks, the subtitles, then the audio tracks. Every
    time is a whole frame floored to µs (rule K2); track_render_index = the track's position."""
    from .capcut import SUB_SIZE, SUB_Y, _extras, audio_tracks
    fps = plan.fps or 30.0
    fus = lambda f: lib.frame_us(f, fps)  # noqa: E731
    p = tpl["proto"]
    info = copy.deepcopy(tpl["draft_info"])
    mats = info["materials"]
    info.update(id=timeline_id, duration=fus(plan.total_frames), fps=float(fps))
    info["canvas_config"] = dict(info.get("canvas_config") or {}, ratio="original",
                                 width=plan.canvas_w, height=plan.canvas_h, background=None)
    tracks: dict[str, dict[str, Any]] = {}
    for n, (name, kind) in enumerate(plan.tracks):
        proto = p["video_track"] if kind == "video" else p["text_track"]
        tr = dict(copy.deepcopy(proto), id=uid(), segments=[], name=name, is_default_name=False)
        if kind == "video":
            tr["flag"] = 0 if not any(k == "video" for _n, k in plan.tracks[:n]) else 2
        tracks[name] = tr
    text_ri = int(p["text_segment"].get("render_index", 14000))
    video_order = [n for n, k in plan.tracks if k == "video"]
    by_track: dict[str, list[int]] = {}
    for i, it in enumerate(plan.items):
        by_track.setdefault(it.track, []).append(i)
    for name, idxs in by_track.items():
        idxs.sort(key=lambda i: plan.items[i].start)
        for pos, i in enumerate(idxs):
            it = plan.items[i]
            start_us, dur_us = fus(it.start), fus(it.frames)
            if it.kind == "video":
                nxt = plan.items[idxs[pos + 1]] if pos + 1 < len(idxs) else None
                seg = _video_segment(it, nxt, media_paths[i], plan, p, mats, uid, fps,
                                     start_us, dur_us, video_order.index(name))
            elif it.kind == "text":
                seg = _text_segment(it, plan, p, mats, uid, start_us, dur_us, text_ri)
                text_ri += 1
            else:
                seg = _sticker_segment(it, plan, p, mats, uid, start_us, dur_us, text_ri)
                text_ri += 1
            tracks[name]["segments"].append(seg)
    out = [tracks[n] for n, _k in plan.tracks if tracks[n]["segments"]]
    if cues:
        out.append(_subtitle_track(cues, sub_kind, p, mats, uid, fps, SUB_SIZE, SUB_Y, _extras))
    info["tracks"] = out
    if audio:
        info["tracks"] += audio_tracks(list(audio), fps, mats, uid, atpl,
                                       start_us=lambda ms: fus(int(round(ms * fps / 1000))))
        # audio lengths round UP to a whole frame, the timeline length is floored (K2): trim the
        # sub-frame overshoot so nothing ends past the timeline
        end = info["duration"]
        for tr in info["tracks"]:
            if tr["type"] != "audio":
                continue
            for s in tr["segments"]:
                tt, st = s["target_timerange"], s.get("source_timerange")
                over = tt["start"] + tt["duration"] - end
                if over > 0:
                    tt["duration"] -= over
                    if st:
                        st["duration"] = max(0, st["duration"] - over)
            tr["segments"] = [s for s in tr["segments"] if s["target_timerange"]["duration"] > 0]
    for n, tr in enumerate(info["tracks"]):
        for s in tr["segments"]:
            s["track_render_index"] = n
    return info


def _video_segment(it: Item, nxt: Item | None, path: Path, plan: Plan, p: dict, mats: dict,
                   uid: Callable[[], str], fps: float, start_us: int, dur_us: int,
                   layer: int) -> dict[str, Any]:
    from .capcut import _extras
    spec = it.spec
    main = layer == 0
    fit = str(spec.get("fit") or ("cover" if main else "contain"))
    box = tuple(float(v) for v in spec["box"]) if spec.get("box") else None
    crop = tuple(float(v) for v in spec["crop"]) if spec.get("crop") else None
    pl = place(it.width, it.height, plan.canvas_w, plan.canvas_h, fit=fit, box=box, crop=crop)
    vmat = copy.deepcopy(p["video_material"])
    media_us = lib.frame_us(it.media_frames, fps) if not it.still else dur_us
    vmat.update(id=uid(), path=str(path), material_name=path.name, duration=media_us,
                width=it.width, height=it.height, has_audio=it.has_audio)
    if pl["crop"]:
        vmat.update(crop=pl["crop"], crop_ratio="free", crop_scale=1.0)
    mats["videos"].append(vmat)
    refs = _extras(p["video_extra"], mats, uid)
    if spec.get("animations"):
        names = [spec["animations"]] if isinstance(spec["animations"], str) else spec["animations"]
        c = lib.animation_container(uid, lib.animation_entries(
            "clip_animation", [(n, None) for n in names], dur_us))
        mats.setdefault("material_animations", []).append(c)
        refs.append(c["id"])
    if spec.get("filter"):
        f = lib.filter_material(spec["filter"], uid, float(spec.get("filter_value", 1.0)))
        mats.setdefault("effects", []).append(f)
        refs.append(f["id"])
    if spec.get("effect"):
        e = lib.video_effect(spec["effect"], uid)
        mats.setdefault("video_effects", []).append(e)
        refs.append(e["id"])
    if spec.get("transition"):
        if nxt is None or nxt.start != it.start + it.frames:
            raise VPError("data", f"layers.json: transition {spec['transition']!r} on {it.track!r} "
                                  f"needs a next clip that starts where this one ends")
        cap = min(dur_us, lib.frame_us(nxt.frames, fps)) // 2
        tr = lib.transition(spec["transition"], uid, fps, want_s=spec.get("transition_s"),
                            cap_us=cap)
        mats.setdefault("transitions", []).append(tr)
        refs.insert(min(2, len(refs)), tr["id"])     # where CapCut itself puts library refs (L3)
    seg = copy.deepcopy(p["video_segment"])
    clip = copy.deepcopy(seg["clip"])
    clip.update(scale=pl["scale"], transform=pl["transform"], rotation=float(spec.get("rotation", 0.0)),
                alpha=float(spec.get("alpha", 1.0)))
    seg.update(id=uid(), material_id=vmat["id"], clip=clip,
               source_timerange={"start": 0, "duration": dur_us},
               target_timerange={"start": start_us, "duration": dur_us},
               uniform_scale={"on": True, "value": 1.0}, extra_material_refs=refs,
               volume=1.0 if spec.get("sound") and it.has_audio else 0.0,
               last_nonzero_volume=1.0, render_index=layer, common_keyframes=[])
    return seg


def _text_style(text: str, size: float, color: str, style_tpl: dict) -> dict[str, Any]:
    style = copy.deepcopy(style_tpl)
    style["text"] = text
    for s in style.get("styles", []):
        s["range"] = [0, _utf16_len(text)]
        s["size"] = float(size)
        fill = s.setdefault("fill", {})
        fill["content"] = {"render_type": "solid", "solid": {"alpha": 1.0, "color": _hex_rgb(color)}}
    return style


def _text_segment(it: Item, plan: Plan, p: dict, mats: dict, uid: Callable[[], str],
                  start_us: int, dur_us: int, render_index: int) -> dict[str, Any]:
    spec = it.spec
    text = str(spec["text"])
    size = float(spec.get("size", 6.0))
    color = str(spec.get("color", "#FFFFFF"))
    style = _text_style(text, size, color, json.loads(p["text_material"]["content"]))
    tmat = copy.deepcopy(p["text_material"])
    tmat.update(id=uid(), content=json.dumps(style, ensure_ascii=False), font_size=size,
                text_color=color, group_id="")
    if spec.get("background"):
        tmat.update(background_style=1, background_color=str(spec["background"]),
                    background_alpha=float(spec.get("background_alpha", 1.0)))
    mats["texts"].append(tmat)
    names = spec.get("animations") or []
    names = [names] if isinstance(names, str) else names
    c = lib.animation_container(uid, lib.animation_entries(
        "text_animation", [(n, None) for n in names], dur_us))
    mats.setdefault("material_animations", []).append(c)
    seg = copy.deepcopy(p["text_segment"])
    x, y = spec.get("pos") or (plan.canvas_w / 2, plan.canvas_h * 0.85)
    clip = copy.deepcopy(seg["clip"])
    clip.update(transform=px_to_transform(float(x), float(y), plan.canvas_w, plan.canvas_h),
                rotation=float(spec.get("rotation", 0.0)), scale={"x": 1.0, "y": 1.0})
    seg.update(id=uid(), material_id=tmat["id"], clip=clip, render_index=render_index,
               target_timerange={"start": start_us, "duration": dur_us},
               uniform_scale={"on": True, "value": 1.0}, extra_material_refs=[c["id"]],
               common_keyframes=[])
    return seg


def _sticker_segment(it: Item, plan: Plan, p: dict, mats: dict, uid: Callable[[], str],
                     start_us: int, dur_us: int, render_index: int) -> dict[str, Any]:
    spec = it.spec
    m = lib.sticker_material(spec["sticker"], uid)
    mats.setdefault("stickers", []).append(m)
    c = lib.animation_container(uid, [])
    mats.setdefault("material_animations", []).append(c)
    seg = copy.deepcopy(p["text_segment"])
    x, y = spec.get("pos") or (plan.canvas_w / 2, plan.canvas_h / 2)
    k = float(spec.get("scale", 1.0))
    clip = copy.deepcopy(seg["clip"])
    clip.update(transform=px_to_transform(float(x), float(y), plan.canvas_w, plan.canvas_h),
                scale={"x": k, "y": k}, rotation=float(spec.get("rotation", 0.0)))
    seg.update(id=uid(), material_id=m["id"], clip=clip, render_index=render_index,
               source_timerange=None, target_timerange={"start": start_us, "duration": dur_us},
               uniform_scale={"on": True, "value": 1.0}, extra_material_refs=[c["id"]],
               common_keyframes=[])
    return seg


def _subtitle_track(cues: list, kind: str, p: dict, mats: dict, uid: Callable[[], str],
                    fps: float, sub_size: dict, sub_y: dict, extras: Callable) -> dict[str, Any]:
    style_tpl = json.loads(p["text_material"]["content"])
    group = f"import_{int(time.time() * 1000)}"
    base = int(p["text_segment"].get("render_index", 14000))
    segs = []
    for n, cue in enumerate(cues):
        a = int(round(cue.start_ms * fps / 1000))
        b = int(round(cue.end_ms * fps / 1000))
        if b <= a:
            continue
        style = copy.deepcopy(style_tpl)
        style["text"] = cue.text
        for s in style.get("styles", []):
            s["range"] = [0, _utf16_len(cue.text)]
            s["size"] = sub_size.get(kind, 5.0)
        tmat = copy.deepcopy(p["text_material"])
        tmat.update(id=uid(), content=json.dumps(style, ensure_ascii=False), group_id=group,
                    font_size=sub_size.get(kind, 5.0))
        mats["texts"].append(tmat)
        seg = copy.deepcopy(p["text_segment"])
        seg.update(id=uid(), material_id=tmat["id"], render_index=base + 1000 + n,
                   target_timerange={"start": lib.frame_us(a, fps),
                                     "duration": lib.frame_us(b - a, fps)},
                   extra_material_refs=extras(p["text_extra"], mats, uid))
        seg["clip"] = dict(copy.deepcopy(seg["clip"]), transform={"x": 0.0, "y": sub_y.get(kind, -0.8)})
        segs.append(seg)
    return dict(copy.deepcopy(p["text_track"]), id=uid(), segments=segs, name="Субтитры",
                is_default_name=False)


# ---- checks ------------------------------------------------------------------------------------

def _material_ids(info: dict[str, Any]) -> set[str]:
    return {m["id"] for lst in info["materials"].values() if isinstance(lst, list)
            for m in lst if isinstance(m, dict) and "id" in m}


def validate_timeline(info: dict[str, Any], folder: Path | None = None,
                      final: Path | None = None) -> list[str]:
    """Structural checks of one timeline; empty list = fine. `folder` (where the project is on
    disk now) + `final` (where its paths point) also check that every media file is there."""
    errs = []
    ids = _material_ids(info)
    seg_ids = [s["id"] for t in info["tracks"] for s in t["segments"]]
    if len(seg_ids) != len(set(seg_ids)):
        errs.append("duplicate segment id")
    dur = info["duration"]
    vids = {m["id"]: m for m in info["materials"].get("videos", [])}
    for n, t in enumerate(info["tracks"]):
        segs = sorted(t["segments"], key=lambda s: s["target_timerange"]["start"])
        for a, b in zip(segs, segs[1:]):
            if a["target_timerange"]["start"] + a["target_timerange"]["duration"] > \
                    b["target_timerange"]["start"]:
                errs.append(f"overlap on track {t.get('name')!r} at {b['target_timerange']['start']}")
        for s in segs:
            tr = s["target_timerange"]
            if tr["start"] < 0 or tr["start"] + tr["duration"] > dur + 1:
                errs.append(f"segment past the end on {t.get('name')!r}")
            if s.get("track_render_index") != n:
                errs.append(f"track_render_index {s.get('track_render_index')} != position {n}")
            for r in [s["material_id"]] + list(s.get("extra_material_refs") or []):
                if r not in ids:
                    errs.append(f"dangling ref {r} on {t.get('name')!r}")
            if t["type"] == "video":
                us = s.get("uniform_scale") or {}
                sc = (s.get("clip") or {}).get("scale") or {}
                if us.get("value") != 1.0:
                    errs.append(f"uniform_scale.value {us.get('value')} (must be 1.0) on "
                                f"{t.get('name')!r}")
                if us.get("on") and abs(sc.get("x", 1) - sc.get("y", 1)) > 1e-9:
                    errs.append(f"stretched: scale x != y on {t.get('name')!r}")
                m = vids.get(s["material_id"])
                st = s.get("source_timerange") or {}
                if m and st.get("start", 0) + st.get("duration", 0) > m["duration"] + 1:
                    errs.append(f"source past the end of {m['material_name']}")
    for kind, lst in info["materials"].items():
        if isinstance(lst, list):
            for m in lst:
                if isinstance(m, dict):
                    errs += lib.check_material(m)
    if folder is not None and final is not None:
        for kind in ("videos", "audios"):
            for m in info["materials"].get(kind, []):
                pth = Path(m["path"])
                local = folder / pth.relative_to(final) if pth.is_relative_to(final) else pth
                if not local.is_file():
                    errs.append(f"missing media {m['path']}")
    return errs


# ---- the job's project on disk -----------------------------------------------------------------

def read_link(job_root: Path) -> dict[str, Any] | None:
    p = Path(job_root) / "capcut" / PROJECT_LINK
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def find_project(job_root: Path, job_id: str, root: Path) -> Path | None:
    """The job's CapCut project in `root`: from <job>/capcut/project.json, else by the marker."""
    from .capcut import _read_made_marker
    link = read_link(job_root)
    cands = []
    if link and link.get("folder"):
        cands.append(Path(link["folder"]))
    if root.is_dir():
        cands += sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    for c in cands:
        if c.parent != root or not (c / "Timelines" / "project.json").is_file():
            continue
        m = _read_made_marker(c)
        if m and m.get("job_id") == job_id and m.get("clip_id") == PROJECT_CLIP_ID:
            return c
    return None


def place_media(folder: Path, wanted: list[tuple[Path, str]]) -> tuple[list[Path], list[tuple[Path, Path]]]:
    """Names inside <project>/Resources/media for `wanted` [(source, preferred name)].

    A file already there with the same bytes is reused; a different file never overwritten (a
    numbered name is taken instead). Returns (relative path per wanted, [(source, rel) to copy])."""
    from .capcut import MEDIA_DIR, safe_name
    rels, copies, taken = [], [], {}
    media_dir = folder / MEDIA_DIR
    on_disk = sorted(p for p in media_dir.iterdir() if p.is_file()) if media_dir.is_dir() else []

    def same_bytes(src: Path) -> Path | None:
        """A file an earlier version already placed with these exact bytes (under any name)."""
        size = src.stat().st_size
        for p in on_disk:
            if p.stat().st_size == size and filecmp.cmp(src, p, shallow=False):
                return Path(MEDIA_DIR) / p.name
        return None

    for src, pref in wanted:
        pref = safe_name(pref) or src.name
        stem, suf = Path(pref).stem, Path(pref).suffix
        dst = folder / MEDIA_DIR / pref
        if not (dst.exists() and filecmp.cmp(src, dst, shallow=False)) and \
                (Path(MEDIA_DIR) / pref) not in taken:
            prev = same_bytes(src)
            if prev is not None:
                taken.setdefault(prev, src)
                rels.append(prev)
                continue
        n = 1
        while True:
            name = pref if n == 1 else f"{stem} {n}{suf}"
            rel = Path(MEDIA_DIR) / name
            dst = folder / rel
            if rel in taken:
                if taken[rel] == src:
                    break
            elif not dst.exists():
                taken[rel] = src
                copies.append((src, rel))
                break
            elif filecmp.cmp(src, dst, shallow=False):
                taken[rel] = src
                break
            n += 1
        rels.append(rel)
    return rels, copies


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # a real copy, not a hard link (the hand builders linked): a source re-rendered in place would
    # otherwise silently change the media of every earlier timeline
    shutil.copy2(src, dst)


def _meta_entries(tpl: dict, atpl: dict, info: dict, now_us: int) -> list[tuple[int, dict]]:
    out = []
    for m in info["materials"].get("videos", []):
        e = copy.deepcopy(tpl["meta_video_entry"])
        e.update(id=str(uuid.uuid4()), file_Path=m["path"], extra_info=Path(m["path"]).name,
                 duration=m["duration"], width=m["width"], height=m["height"],
                 create_time=now_us // 1_000_000, import_time=now_us // 1_000_000,
                 import_time_ms=now_us, roughcut_time_range={"duration": m["duration"], "start": 0})
        out.append((0, e))
    for m in info["materials"].get("audios", []):
        e = copy.deepcopy(atpl["meta_audio_entry"])
        e.update(id=str(uuid.uuid4()), file_Path=m["path"], extra_info=Path(m["path"]).stem,
                 duration=m["duration"], import_time=now_us // 1_000_000)
        out.append((1, e))
    return out


def _add_to_pool(meta: dict, entries: list[tuple[int, dict]]) -> None:
    groups = meta.setdefault("draft_materials", [])
    known = {e.get("file_Path") for g in groups for e in g.get("value", []) if isinstance(e, dict)}
    for typ, e in entries:
        if e["file_Path"] in known:
            continue
        known.add(e["file_Path"])
        g = next((g for g in groups if g.get("type") == typ), None)
        if g is None:
            groups.append({"type": typ, "value": [e]})
        else:
            g.setdefault("value", []).append(e)


@dataclasses.dataclass
class TimelineBuild:
    """What a version contributes: the media it needs and a builder of its draft_info.

    `media` = [(source file, preferred name)]; `build(rels)` gets the project-relative path of
    each media file (same order) and returns the draft_info (paths must be absolute:
    <final project>/<rel>; use `final / rel`)."""
    name: str
    media: list[tuple[Path, str]]
    build: Callable[[Path, list[Path]], dict[str, Any]]
    cover_from: Path | None = None


def write_version(builds: list[TimelineBuild], *, job_root: Path, job_id: str, title: str,
                  root: Path, running: Callable[[], bool], backup_dir: Path | None = None,
                  cover: Callable[[Path, Path], None] | None = None,
                  make_main: bool = True, baseline_dir: Path | None = None) -> dict[str, Any]:
    """Add one timeline per build to the job's CapCut project (created on first use).

    Existing timelines are never touched; nothing is written while CapCut runs or has the project
    open (`.locked`). Small project files are backed up first; on any failure everything this call
    added is removed and the backed-up files are restored."""
    from .capcut import MADE_MARKER, MADE_TOOL, STAGING_PREFIX, build_root_entry, load_audio_template, \
        load_template, register, safe_name, unique_folder
    if running():
        raise VPError("data", "CapCut is running. Quit CapCut (Cmd+Q) and run the export again: "
                              "CapCut rewrites its project files on exit and would drop the new timeline.")
    if not root.is_dir():
        raise VPError("data", f"CapCut drafts folder not found: {root}. Is CapCut desktop installed "
                              "and opened at least once? (or set VP_CAPCUT_DRAFTS)")
    tpl, atpl = load_template(), load_audio_template()
    backup_dir = backup_dir or vp_home() / "capcut" / "backups"
    now_us = int(time.time() * 1_000_000)
    existing = find_project(job_root, job_id, root)
    if existing is not None and (existing / ".locked").exists():
        raise VPError("data", f"the project «{existing.name}» is open in CapCut (.locked): quit "
                              "CapCut and run the export again")
    folder = existing or unique_folder(root, safe_name(title))
    work = folder if existing else root / f"{STAGING_PREFIX}{uuid.uuid4().hex[:8]}"
    created: list[Path] = []
    backups: dict[str, bytes] = {}
    if existing:
        for f in SMALL_FILES:
            if (folder / f).is_file():
                backups[f] = (folder / f).read_bytes()
        bdir = backup_dir / f"{safe_name(folder.name)}.{time.strftime('%Y%m%d-%H%M%S')}"
        n = 2
        while bdir.exists():
            bdir = backup_dir / f"{safe_name(folder.name)}.{time.strftime('%Y%m%d-%H%M%S')}-{n}"
            n += 1
        for f, raw in backups.items():
            atomic_write(bdir / f, raw)
    else:
        work.mkdir()
    try:
        pj = json.loads((folder / "Timelines" / "project.json").read_text(encoding="utf-8")) \
            if existing else None
        taken_ids: set[str] = set()
        if existing:
            for t in pj.get("timelines", []):
                f = folder / "Timelines" / t["id"] / "draft_info.json"
                if f.is_file():
                    taken_ids |= set(UUID_RE.findall(f.read_text(encoding="utf-8")))
        added = []
        for b in builds:
            rels, copies = place_media(work, b.media)
            for src, rel in copies:
                if not (work / rel).exists():
                    _copy(src, work / rel)
                    created.append(work / rel)
            info = b.build(folder, rels)
            if set(UUID_RE.findall(_dump(info))) & taken_ids:
                raise VPError("internal", "new timeline shares an id with an existing one")
            errs = validate_timeline(info, work, folder)
            if errs:
                raise VPError("data", "the CapCut timeline failed its checks: " + "; ".join(errs[:8]))
            taken_ids |= set(UUID_RE.findall(_dump(info)))
            tdir = work / "Timelines" / info["id"]
            tmp = work / "Timelines" / f".{info['id']}.tmp"
            tmp.mkdir(parents=True)
            created.append(tmp)
            written = _dump(info)
            (tmp / "draft_info.json").write_text(written, encoding="utf-8")
            if baseline_dir is not None:
                atomic_write(baseline_dir / f"{info['id']}.json", written)
            if cover and b.cover_from and not (work / "draft_cover.jpg").exists():
                cover(b.cover_from, work / "draft_cover.jpg")
                if (work / "draft_cover.jpg").exists():
                    created.append(work / "draft_cover.jpg")
            if (work / "draft_cover.jpg").exists():
                shutil.copy2(work / "draft_cover.jpg", tmp / "draft_cover.jpg")
            os.rename(tmp, tdir)
            created.remove(tmp)
            created.append(tdir)
            added.append((info, b.name))
        # ---- project files
        names = [n for _i, n in added]
        if pj is None:
            pj = copy.deepcopy(tpl["timelines_project"])
            proto_tl = pj["timelines"][0]
            pj.update(id=_uid(), create_time=now_us, update_time=now_us, timelines=[])
        else:
            proto_tl = (pj.get("timelines") or [copy.deepcopy(tpl["timelines_project"])["timelines"][0]])[0]
        old_main = pj.get("main_timeline_id")
        for k, (info, name) in enumerate(added):
            pj["timelines"].append(dict(proto_tl, id=info["id"], name=name, create_time=now_us + k,
                                        update_time=now_us + k, is_marked_delete=False))
        pj["update_time"] = now_us
        main_note = None
        new_main = added[-1][0]
        switch = make_main or not old_main
        if old_main and switch:
            # the root draft_info.json mirrors the main timeline; switch only when it really does
            # (otherwise CapCut's newest copy could be the root file — never lose a hand edit)
            try:
                root_info = json.loads((folder / "draft_info.json").read_text(encoding="utf-8"))
                main_info = json.loads((folder / "Timelines" / old_main / "draft_info.json")
                                       .read_text(encoding="utf-8"))
                switch = root_info == main_info
            except (OSError, ValueError):
                switch = False
            if not switch:
                main_note = ("the project's root draft_info.json differs from its main timeline: "
                             "the main timeline was left as it was; open the new timeline from "
                             "the timeline list")
        if switch:
            pj["main_timeline_id"] = new_main["id"]
            atomic_write(work / "draft_info.json", _dump(new_main))
        main_id = pj["main_timeline_id"]
        atomic_write(work / "Timelines" / "project.json", _dump(pj))
        lay_p = work / "timeline_layout.json"
        lay = json.loads(lay_p.read_text(encoding="utf-8")) if lay_p.is_file() else \
            {"dockItems": [{"dockIndex": 0, "ratio": 1, "timelineIds": [], "timelineNames": []}],
             "layoutOrientation": 1}
        dock = (lay.get("dockItems") or [{}])[0]
        dock.setdefault("timelineIds", []).extend(i["id"] for i, _n in added)
        dock.setdefault("timelineNames", []).extend(names)
        atomic_write(lay_p, _dump(lay))
        biz_p = work / "draft_biz_config.json"
        biz = json.loads(biz_p.read_text(encoding="utf-8")) if biz_p.is_file() else {}
        biz.setdefault("timeline_settings", {}).update(
            {i["id"]: {"linkage_enabled": False} for i, _n in added})
        atomic_write(biz_p, json.dumps(biz, indent=4))
        meta_p = work / "draft_meta_info.json"
        if meta_p.is_file():
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        else:
            meta = copy.deepcopy(tpl["draft_meta_info"])
            for g in meta.get("draft_materials", []):
                if g.get("type") in (0, 1):
                    g["value"] = []
            meta.update(draft_cover="draft_cover.jpg", draft_fold_path=str(folder),
                        draft_root_path=str(folder.parent), draft_id=_uid(),
                        draft_name=folder.name, tm_draft_create=now_us)
        for info, _n in added:
            _add_to_pool(meta, _meta_entries(tpl, atpl, info, now_us))
        main_info = next((i for i, _n in added if i["id"] == main_id), None)
        if main_info is not None:
            meta["tm_duration"] = main_info["duration"]
        mdir = work / "Resources" / "media"
        meta.update(tm_draft_modified=now_us,
                    draft_timeline_materials_size_=sum(f.stat().st_size for f in mdir.iterdir()
                                                       if f.is_file()) if mdir.is_dir() else 0)
        atomic_write(meta_p, _dump(meta))
        if not existing:
            (work / MADE_MARKER).write_text(_dump(
                {"tool": MADE_TOOL, "job_id": job_id, "clip_id": PROJECT_CLIP_ID, "version": None,
                 "created_at": now_iso()}), encoding="utf-8")
            if running():
                raise VPError("data", "CapCut started meanwhile: nothing was installed")
            if folder.exists():
                raise VPError("data", f"folder appeared while exporting: {folder}")
            os.rename(work, folder)
    except BaseException:
        if existing:
            for p in reversed(created):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            for f, raw in backups.items():
                atomic_write(folder / f, raw)
        else:
            shutil.rmtree(work, ignore_errors=True)
        raise
    reg_backup = None
    if not existing:
        reg_backup = register(root, build_root_entry(tpl, meta), backup_dir)
    timelines = [{"id": i["id"], "name": n, "duration_us": i["duration"],
                  "tracks": [{"name": t.get("name"), "type": t["type"], "segments": len(t["segments"])}
                             for t in i["tracks"]]} for i, n in added]
    link = read_link(job_root) or {}
    if link.get("folder") != str(folder):
        link = {"folder": str(folder), "name": folder.name, "created_at": now_iso(), "timelines": []}
    link.setdefault("timelines", []).extend({"id": t["id"], "name": t["name"],
                                             "added_at": now_iso()} for t in timelines)
    atomic_write(Path(job_root) / "capcut" / PROJECT_LINK,
                 json.dumps(link, ensure_ascii=False, indent=1) + "\n")
    return {"folder": str(folder), "name": folder.name, "created": not existing,
            "main_timeline_id": main_id, "timelines": timelines, "note": main_note,
            "registry_backup": str(reg_backup) if reg_backup else None}


# ---- a text job's version as a layered timeline -----------------------------------------------

def text_version(job_root: Path, version: str, vdir: Path, *, video: Path | None = None,
                 music: Path | None = None, probe: Callable[[Path], dict] | None = None,
                 make_bed: Callable | None = None, make_still: Callable | None = None,
                 label: str | None = None, **volumes: float) -> tuple[TimelineBuild, dict[str, Any]]:
    """The layered timeline of one version of a text job + what went in (no project writes)."""
    from .. import text as textjob
    from . import media as mediamod
    from .capcut import load_audio_template, load_template
    probe = probe or mediamod.probe
    make_still = make_still or _make_still
    tl_json = textjob.load_timeline(job_root)
    fps = float(tl_json.get("fps") or 30)
    cues, audio, inputs = textjob.capcut_audio_cues(job_root, vdir, music=music, probe=probe,
                                                    make_bed=make_bed, **volumes)
    render = None
    if video is not None:
        render = Path(video).expanduser().resolve()
        if not render.is_file():
            raise VPError("data", f"video not found: {render}")
    else:
        try:
            render = textjob.pick_render(job_root, version)
        except VPError:
            render = None
    canvas = (1920, 1080)
    if render is not None:
        rinfo = probe(render)
        if rinfo.get("width") and rinfo.get("height"):
            canvas = (int(rinfo["width"]), int(rinfo["height"]))
    raw = None if video is not None else load_layers(job_root)
    if raw is not None:
        plan = resolve_plan(raw, tl_json, job_root, canvas=canvas, probe=probe)
    else:
        plan = default_plan(tl_json, job_root, canvas=canvas, probe=probe, render=render,
                            explicit_video=video is not None)
    # stills: a picture becomes a still clip of its span (a verified video material; a PNG photo
    # material has no CapCut-saved prototype yet)
    media: list[tuple[Path, str]] = []
    for n, it in enumerate(plan.items):
        if it.kind != "video":
            continue
        src = it.src
        if it.still:
            out = vdir / "stills" / f"{n + 1:03d}_{src.stem}_{it.frames}f.mp4"
            if not out.is_file():
                make_still(src, out, it.frames, fps)
            src = out
        media.append((src, src.name))
    for c in audio:
        media.append((Path(c.path), Path(c.path).name))
    vidx = [n for n, it in enumerate(plan.items) if it.kind == "video"]
    kind = "reel" if plan.canvas_h > plan.canvas_w else "long"
    tpl, atpl = load_template(), load_audio_template()
    name = version + (f" — {label or plan.name}" if (label or plan.name) else "")

    def build(final: Path, rels: list[Path]) -> dict[str, Any]:
        paths = {i: final / rels[k] for k, i in enumerate(vidx)}
        arels = rels[len(vidx):]
        aud = [dataclasses.replace(c, path=final / r) for c, r in zip(audio, arels)]
        return build_layered_info(plan, paths, tpl, atpl, _uid(), cues=cues, audio=aud,
                                  sub_kind=kind)

    first = next((plan.items[i].src for i in vidx if not plan.items[i].still), None)
    inputs = dict(inputs, layers=plan.source, plan_warnings=list(plan.warnings),
                  tracks=[{"name": n, "type": k,
                           "items": sum(1 for it in plan.items if it.track == n)}
                          for n, k in plan.tracks],
                  canvas=f"{plan.canvas_w}x{plan.canvas_h}",
                  library=sorted({f"{k}:{it.spec[k]}" for it in plan.items
                                  for k in ("filter", "effect", "transition", "sticker")
                                  if it.spec.get(k)}),
                  crops=sum(1 for it in plan.items if it.spec.get("crop")))
    return TimelineBuild(name=name, media=media, build=build, cover_from=first), inputs


def _make_still(src: Path, out: Path, frames: int, fps: float) -> Path:
    """A picture as a still H.264 clip of `frames` frames, its own size (even), shape kept."""
    from . import media as mediamod
    return mediamod._render_to(out, ["-loop", "1", "-framerate", f"{fps:g}", "-i", str(src),
                                     "-frames:v", str(frames + 2), "-vf",
                                     "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                                     "-r", f"{fps:g}"], timeout_s=600)
