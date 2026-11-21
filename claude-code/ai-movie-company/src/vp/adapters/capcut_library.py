"""CapCut library items (transitions, filters, effects, animations, stickers) referenced by id.

CapCut does not need the package inside the project: it downloads a library item by id when it
opens the draft (rule L1 of the CapCut knowledge base, 16 of 16 kinds resolved). It does so ONLY
when the item is written the way CapCut 9.8 itself writes it. The first plugin-written projects
broke that in several ways (test signals 2026-11-21, M2), all fixed here from CapCut-saved JSON
(reference project «Эталон элементов CapCut», snapshot s03-owner-fixed):

  * resource_id = effect_id on every catalogue kind; the legacy id lives only in
    third_resource_id ("0" when there is none). A split id left 14/14 video effects, 28/28 text
    templates and the stickers as placeholders;
  * source_platform 1 (without it CapCut takes the legacy path: the new-only transitions never
    loaded and the sound effects resolved to old packages);
  * transitions: is_overlap from the package (false for Black Fade / White Flash), duration =
    the whole frame just below the wanted length, never more than half the shorter neighbour;
  * filters on a clip or on a filter track: apply_target_type 0 (CapCut rewrote our 2 to 0);
    video effects on their own track keep 2;
  * no `path`: CapCut fills it when it downloads the package.

The catalogue (capcut_library.json) lists only items CapCut resolved by id (status `verified`)
and a few written by the corrected recipe but not yet round-tripped (status `recipe`). Pure code:
no file system access apart from reading the catalogue; ids come from `uid`.
"""
from __future__ import annotations

import json
import math
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from ..core.errors import VPError

LIBRARY_PATH = Path(__file__).with_name("capcut_library.json")
KINDS = ("transition", "filter", "video_effect", "text_animation", "clip_animation", "sticker")


@lru_cache(maxsize=1)
def _catalogue() -> dict[str, Any]:
    return json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))


def catalogue() -> dict[str, Any]:
    """A copy of the whole catalogue (source notes + items by kind)."""
    return json.loads(json.dumps(_catalogue()))


def find(kind: str, name: str) -> dict[str, Any]:
    """The catalogue entry of `name` (case-insensitive) with its `name` and `kind` filled in."""
    if kind not in KINDS:
        raise VPError("data", f"unknown CapCut library kind {kind!r}; known: {', '.join(KINDS)}")
    items = _catalogue()["items"].get(kind, {})
    for title, it in items.items():
        if title.lower() == str(name).strip().lower():
            return dict(it, name=title, kind=kind)
    raise VPError("data", f"CapCut library has no {kind.replace('_', ' ')} {name!r}; available: "
                          f"{', '.join(items) or 'none'} (see `vp capcut library`)")


def frame_us(frames: int, fps: float) -> int:
    """Whole frames -> µs, rounded DOWN the way CapCut re-snaps every timerange (rule K2)."""
    return int(int(frames) * 1_000_000 // (fps or 30.0))


def transition_us(want_s: float, fps: float, cap_us: int | None = None) -> int:
    """Transition length as CapCut 9.8 writes it: the whole frame just below the wanted length
    (0.5 s -> 466666 at 30 fps, 0.9 s -> 866666), never more than `cap_us` (half the shorter
    neighbouring clip)."""
    fps = fps or 30.0
    frames = max(math.ceil(round(want_s * fps, 6)) - 1, 1)
    d = frame_us(frames, fps)
    return min(d, cap_us) if cap_us else d


def transition(name: str, uid: Callable[[], str], fps: float, *, want_s: float | None = None,
               cap_us: int | None = None) -> dict[str, Any]:
    """materials.transitions[] entry, referenced from the EARLIER clip's extra_material_refs."""
    it = find("transition", name)
    return {"id": uid(), "type": "transition", "name": it["name"], "effect_id": it["effect_id"],
            "resource_id": it["effect_id"], "third_resource_id": it.get("third_resource_id") or "0",
            "source_platform": 1, "sub_type": 0, "adjust_params": [],
            "duration": transition_us(want_s or it.get("default_s") or 0.5, fps, cap_us),
            "is_overlap": bool(it.get("is_overlap", True)), "platform": "all",
            "category_id": it.get("category_id", ""), "category_name": "", "request_id": "",
            "is_ai_transition": False, "video_path": "", "task_id": ""}


def filter_material(name: str, uid: Callable[[], str], value: float = 1.0) -> dict[str, Any]:
    """materials.effects[] filter (on a clip through extra_material_refs, or on a filter track)."""
    it = find("filter", name)
    return {"id": uid(), "type": "filter", "name": it["name"], "effect_id": it["effect_id"],
            "resource_id": it["effect_id"], "third_resource_id": it.get("third_resource_id") or "0",
            "source_platform": 1, "apply_target_type": 0, "value": float(value), "sub_type": "none",
            "platform": "all", "category_id": it.get("category_id", ""), "category_name": "",
            "adjust_params": [], "time_range": None, "formula_id": "", "version": "",
            "item_effect_type": 0, "visible": True, "intensity_key": "", "panel_id": "",
            "enable_skin_tone_correction": False, "exclusion_group": [], "face_adjust_params": [],
            "bloom_params": None, "request_id": "", "algorithm_artifact_path": "",
            "multi_language_current": "",
            "color_match_info": {"source_feature_path": "", "target_feature_path": "",
                                 "target_image_path": ""}}


def video_effect(name: str, uid: Callable[[], str], *, on_track: bool = False,
                 track_index: int = 0) -> dict[str, Any]:
    """materials.video_effects[] entry: on a clip (apply_target_type 0) or on its own track (2)."""
    it = find("video_effect", name)
    return {"id": uid(), "type": "video_effect", "name": it["name"], "effect_id": it["effect_id"],
            "resource_id": it["effect_id"], "source_platform": 1, "sub_type": 0,
            "apply_target_type": 2 if on_track else 0, "value": 1.0, "platform": "all",
            "category_id": it.get("category_id", ""), "category_name": "", "version": "",
            "item_effect_type": 0, "bind_segment_id": "", "transparent_params": "",
            "adjust_params": [{"name": n, "value": v, "default_value": v}
                              for n, v in it.get("adjust", [])],
            "time_range": None, "formula_id": "", "apply_time_range": None,
            "render_index": 10000 if on_track else 11000, "track_render_index": track_index,
            "common_keyframes": [], "request_id": "", "algorithm_artifact_path": "",
            "disable_effect_faces": [], "covering_relation_change": 0, "enable_mask": True,
            "effect_mask": [], "sdk_extra": ""}


def animation_entries(kind: str, wanted: list[tuple[str, float | None]],
                      seg_us: int) -> list[dict[str, Any]]:
    """animations[] of one container. `kind` is text_animation or clip_animation; `wanted` =
    [(name, seconds or None for the default)]. In and out keep their default length (at most
    half the segment), a loop fills what they leave, a group spans the whole segment."""
    items = [(find(kind, n), s) for n, s in wanted]
    lengths = {}
    for it, s in items:
        if it["type"] in ("in", "out"):
            want = int(round((s or it.get("default_s") or 0.5) * 1_000_000))
            lengths[it["name"], it["type"]] = min(want, seg_us // 2)
    in_us = sum(v for (n, t), v in lengths.items() if t == "in")
    out_us = sum(v for (n, t), v in lengths.items() if t == "out")
    video = kind == "clip_animation"
    out = []
    for it, _s in items:
        t = it["type"]
        if t == "in":
            start, dur = 0, lengths[it["name"], t]
        elif t == "out":
            dur = lengths[it["name"], t]
            start = seg_us - dur
        elif t == "group":
            start, dur = 0, seg_us
        else:
            start, dur = in_us, max(0, seg_us - in_us - out_us)
        out.append({"id": it["effect_id"], "type": t, "start": int(start), "duration": int(dur),
                    "platform": "all", "resource_id": it["effect_id"],
                    "third_resource_id": it["effect_id"], "source_platform": 1,
                    "name": it["name"], "category_id": "", "category_name": "",
                    "panel": "video" if video else "",
                    "material_type": "video" if video else "sticker",
                    "anim_adjust_params": None, "request_id": ""})
    return out


def animation_container(uid: Callable[[], str], entries: list[dict[str, Any]]) -> dict[str, Any]:
    """materials.material_animations[] container (CapCut uses type sticker_animation for clips,
    texts and stickers alike)."""
    return {"id": uid(), "type": "sticker_animation", "multi_language_current": "none",
            "animations": list(entries)}


def sticker_material(name: str, uid: Callable[[], str]) -> dict[str, Any]:
    """materials.stickers[] entry by the s03 rule (sticker_id = resource_id = effect_id).
    Status `recipe`: the corrected form, not yet confirmed by a CapCut open."""
    it = find("sticker", name)
    return {"id": uid(), "type": "sticker", "name": it["name"], "resource_id": it["effect_id"],
            "sticker_id": it["effect_id"], "effect_id": it["effect_id"],
            "third_resource_id": it.get("third_resource_id") or "0", "source_platform": 1}


def check_material(m: dict[str, Any]) -> list[str]:
    """Problems that make CapCut leave a library item as a placeholder (empty = fine)."""
    errs = []
    eff = m.get("effect_id")
    if not eff:
        return errs
    if m.get("resource_id") != eff:
        errs.append(f"{m.get('type')} {m.get('name')!r}: resource_id != effect_id")
    if m.get("source_platform") != 1:
        errs.append(f"{m.get('type')} {m.get('name')!r}: source_platform is not 1")
    if m.get("type") == "filter" and m.get("apply_target_type") != 0:
        errs.append(f"filter {m.get('name')!r}: apply_target_type is not 0")
    return errs


def library_listing() -> list[dict[str, Any]]:
    """Every catalogue item as {kind, name, status, effect_id} (for `vp capcut library`)."""
    out = []
    for kind in KINDS:
        for name, it in _catalogue()["items"].get(kind, {}).items():
            out.append({"kind": kind, "name": name, "status": it.get("status", "recipe"),
                        "effect_id": it["effect_id"], "default_s": it.get("default_s")})
    return out


def _uid() -> str:
    return str(uuid.uuid4()).upper()
