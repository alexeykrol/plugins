"""Local builder composition entry and conservative animation geometry."""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from ..builders.contracts import RenderBox, RendererProps, RenderReelBody, validate_renderer_props
from ..builders.presets import REEL_PRESETS
from ..core.errors import VPError
from .render_remotion import (
    DEFAULT_GL_BACKEND, REMOTION_SOURCE, RemotionRenderer, RemotionWorkspace, _link, render_gl,
)

COMPOSITIONS = {
    "reel_body": "BuildReelBody",
    "reel_card": "BuildReelCard",
    "long_card": "BuildLongCard",
    "long_overlay": "BuildLongOverlay",
}


@dataclass(frozen=True)
class EffectGeometry:
    """Optional output-pixel exclusions and overrides for canvases without preset geometry."""

    face: RenderBox | None = None
    cards: tuple[RenderBox, ...] = ()
    caption_clearance_px: float | None = None
    caption_half_extra_px: float | None = None
    split_speaker_height_px: int | None = None


@dataclass(frozen=True)
class EffectBounds:
    errors: tuple[str, ...]
    minimum_caption_gap_px: float | None
    required_caption_gap_px: float | None


@dataclass(frozen=True)
class _Bounds:
    left: float
    top: float
    right: float
    bottom: float

    @classmethod
    def box(cls, box: RenderBox) -> _Bounds:
        return cls(box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])

    def inside(self, outer: _Bounds) -> bool:
        return (outer.left <= self.left <= self.right <= outer.right
                and outer.top <= self.top <= self.bottom <= outer.bottom)

    def overlaps(self, other: _Bounds) -> bool:
        return (max(self.left, other.left) < min(self.right, other.right)
                and max(self.top, other.top) < min(self.bottom, other.bottom))


def _rotated_extent(primary: float, cross: float, degrees: float) -> float:
    angle = min(math.radians(degrees), math.atan2(cross, primary))
    return primary * math.cos(angle) + cross * math.sin(angle)


def _sprite_bounds(effect, rules) -> _Bounds | None:
    kind = effect["type"]
    if kind not in ("tag", "sticker", "flyby") or "box" not in effect:
        return None
    box = effect["box"]
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    scale = rules["stickerScale"] if kind == "sticker" else 1
    w, h = box["width"] * scale / 2, box["height"] * scale / 2
    half_w = _rotated_extent(w, h, rules[f"{kind}Degrees"])
    half_h = _rotated_extent(h, w, rules[f"{kind}Degrees"])
    if kind == "flyby":
        half_w += box["width"] * rules["flybyTravelWidths"]
    return _Bounds(cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def _moved_face(face: _Bounds, cx: float, cy: float, scale: float, travel: float) -> _Bounds:
    return _Bounds(min(face.left, cx + (face.left - cx) * scale) - travel,
                   min(face.top, cy + (face.top - cy) * scale) - travel,
                   max(face.right, cx + (face.right - cx) * scale) + travel,
                   max(face.bottom, cy + (face.bottom - cy) * scale) + travel)


def inspect_effect_bounds(props: RenderReelBody, geometry: EffectGeometry = EffectGeometry()) -> EffectBounds:
    """Check continuous envelopes, including positions between sampled video frames."""
    errors = validate_renderer_props(props)
    if errors:
        return EffectBounds(tuple(errors), None, None)
    if props["kind"] != "reel_body":
        return EffectBounds(("kind: expected reel_body",), None, None)
    if not props["effects"]:
        return EffectBounds((), None, None)

    rules = json.loads((REMOTION_SOURCE / "src/build/animation.json").read_text(encoding="utf-8"))
    width, height = props["width"], props["height"]
    canvas = _Bounds(0, 0, width, height)
    cx, cy = width / 2, height / 2
    preset = next((p for p in REEL_PRESETS.values() if (p.width, p.height) == (width, height)), None)
    required = geometry.caption_clearance_px
    extra = geometry.caption_half_extra_px
    if preset is not None:
        if required is None:
            required = preset.caption_clearance_px
        if extra is None:
            extra = preset.caption_half_extra_px
    caption = None
    if props["captions"]:
        for name, value in (("caption_clearance_px", required), ("caption_half_extra_px", extra)):
            if value is None or not math.isfinite(value) or value < 0:
                errors.append(f"geometry.{name}: supply a nonnegative pixel setting")
        if errors:
            return EffectBounds(tuple(errors), None, None)
        center = props["captionStyle"]["yFrac"] * height
        half = props["captionStyle"]["fontFrac"] * width * rules["captionLineHeight"] + extra
        caption = _Bounds(0, center - half, width, center + half)

    protected = {f"card[{i}]": _Bounds.box(box) for i, box in enumerate(geometry.cards)}
    face = _Bounds.box(geometry.face) if geometry.face is not None else None
    if face is not None:
        protected["speaker face"] = face
    for name, bounds in protected.items():
        if not bounds.inside(canvas) or bounds.left == bounds.right or bounds.top == bounds.bottom:
            errors.append(f"geometry.{name}: expected a positive box inside the canvas")
    if props["showPlate"] and props["brand"]["channelPlate"]:
        margin = min(width, height) * rules["plateMarginFraction"]
        protected["channel plate"] = _Bounds(width * (1 - rules["plateWidthFraction"]) - margin,
                                             margin, width - margin,
                                             margin + height * rules["plateHeightFraction"])
    if props["layout"] == "split" and preset is not None:
        speaker_height = geometry.split_speaker_height_px
        if speaker_height is None:
            speaker_height = preset.split_speaker_height_px
        if speaker_height is None or not 0 < speaker_height < height - preset.divider_px:
            errors.append("geometry.split_speaker_height_px: supply a speaker band height inside the canvas")
        else:
            protected["split divider"] = _Bounds(0, speaker_height, width, speaker_height + preset.divider_px)

    minimum = None
    checked = []
    for i, effect in enumerate(props["effects"]):
        path, kind = f"effects[{i}]", effect["type"]
        picture = kind in ("zoom", "shake")
        envelope = _sprite_bounds(effect, rules)
        visible = envelope
        if picture:
            travel = effect["amplitudePx"] if kind == "shake" else 0
            # Shake overscans the picture by at least its full travel on each axis.
            scale = 1 + 2 * travel / min(width, height) if kind == "shake" else effect["scale"]
            if not math.isfinite(scale) or min(cx, cy) * (scale - 1) + 1e-9 < travel:
                errors.append(f"{path}: picture transform can uncover a frame edge")
                continue
            envelope = canvas
            visible = _moved_face(face, cx, cy, scale, travel) if face is not None else None
        if envelope is None:
            errors.append(f"{path}: missing animation envelope")
            continue
        if visible is not None:
            subject = "transformed speaker face" if picture else "animation envelope"
            if not visible.inside(canvas):
                errors.append(f"{path}: {subject} leaves the frame")
            for name, region in protected.items():
                if picture and name == "speaker face":
                    continue
                if visible.overlaps(region):
                    errors.append(f"{path}: {subject} crosses {name}")
            if caption is not None:
                gap = max(caption.top - visible.bottom, visible.top - caption.bottom)
                minimum = gap if minimum is None else min(minimum, gap)
                if gap < required:
                    errors.append(f"{path}: {subject} has {gap:.2f}px caption clearance; requires {required}px")
        checked.append((i, effect, envelope))

    for (i, first, a), (j, second, b) in combinations(checked, 2):
        if max(first["startFrame"], second["startFrame"]) < min(first["endFrame"], second["endFrame"]):
            if a.overlaps(b):
                errors.append(f"effects[{i}] and effects[{j}]: overlapping animation envelopes")
    return EffectBounds(tuple(errors), minimum, required if caption is not None else None)


class _PreparedWorkspace(RemotionWorkspace):
    def ensure(self, log=print) -> Path:
        # The legacy method can install dependencies. Local builds require prepared code and tools.
        if not self.current():
            raise VPError("data", "local build needs a prepared Remotion workspace matching the current code")
        return self.dir


def _public_assets(props: RendererProps, root: Path, out: Path) -> dict[str, Path]:
    video = props.get("video")
    names = {video} if video is not None else set()
    if props["kind"] == "reel_body":
        names.update(effect["asset"] for effect in props["effects"] if "asset" in effect)
    resolved = {}
    for name in sorted(names):
        try:
            source = (root / name).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise VPError("data", f"missing or unreadable Remotion asset: {name}") from exc
        if not source.is_relative_to(root) or not source.is_file():
            raise VPError("data", f"Remotion asset must be a file inside the public directory: {name}")
        if source == out.resolve():
            raise VPError("data", f"render destination is also a public asset: {name}")
        resolved[name] = source
    return resolved


@dataclass(frozen=True)
class BuildRenderResult:
    path: Path
    composition: str
    gl_effective: str
    gl_fallback: bool


def render_build(
    props: RendererProps, out: Path, *, public_dir: Path, config: dict | None = None,
    geometry: EffectGeometry = EffectGeometry(),
) -> BuildRenderResult:
    """Render one validated chunk. Use MP4 for picture and MOV for a transparent long overlay."""
    errors = (list(inspect_effect_bounds(props, geometry).errors)
              if isinstance(props, dict) and props.get("kind") == "reel_body"
              else validate_renderer_props(props))
    if errors:
        raise VPError("contract", "\n".join(errors), errors=errors)

    out = Path(out)
    alpha = props["kind"] == "long_overlay" and props["video"] is None
    suffix = ".mov" if alpha else ".mp4"
    if out.suffix.lower() != suffix:
        raise VPError("data", f"{props['kind']} requires a {suffix} destination")
    assets = _public_assets(props, Path(public_dir).resolve(), out)
    comp = COMPOSITIONS[props["kind"]]
    effective_config = {**(config or {}), "remotion": {**((config or {}).get("remotion") or {}),
                                                       "allow_browser_download": False}}
    renderer = RemotionRenderer(effective_config, workspace=_PreparedWorkspace(effective_config))
    _, requested_gl = render_gl(effective_config)
    attempted_gl = []

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".vp-build-", dir=out.parent) as directory:
        tmp = Path(directory)
        public = tmp / "public"
        public.mkdir()
        for name, source in assets.items():
            dest = public / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _link(source, dest)

        staged = tmp / f"output{suffix}"

        def draw() -> None:
            attempted_gl.append(renderer._gl_attempt or DEFAULT_GL_BACKEND)
            flags = (["--codec=prores", "--prores-profile=4444", "--image-format=png",
                      "--pixel-format=yuva444p10le"] if alpha else None)
            renderer._render(comp, props, staged, tmp, public, props["durationInFrames"], flags)

        renderer._draw_with_gl(draw)
        os.replace(staged, out)
    return BuildRenderResult(out, comp, attempted_gl[-1], bool(requested_gl and len(attempted_gl) > 1))
