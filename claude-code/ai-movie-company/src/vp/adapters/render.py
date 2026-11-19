"""Renderer interface (S5 clips, S7 thumbnails).

v1 implementation: `FfmpegRenderer(config: dict)` — written by the render worker in this module.
The core creates it as `FfmpegRenderer(ctx.config)` (see vp.core.ctx). Remotion is a later
adapter with the same interface.
"""
from __future__ import annotations

import abc
import os
from typing import Any

KINDS = ("reel", "long", "full")


class Renderer(abc.ABC):
    """Produces final videos and thumbnails. Implementations must write outputs atomically."""

    @abc.abstractmethod
    def render_clip(self, cut_mp4: str | os.PathLike, clip_srt: str | os.PathLike,
                    design_texts: dict[str, Any], kind: str, out_mp4: str | os.PathLike) -> dict[str, int]:
        """Render the final video from the cut, burned/attached subtitles and design texts
        (intro/outro/announce). kind: reel | long | full.

        Returns {"intro_ms": int, "outro_ms": int, "duration_ms": int}: the shift of the clip
        timeline inside the final video (used to compute final.srt and chapters).
        """

    @abc.abstractmethod
    def render_thumbnail(self, frame_png: str | os.PathLike, text: str,
                         out_png: str | os.PathLike) -> None:
        """Thumbnail 1280×720 PNG < 2 MB from a frame and a short text (≤ 5 words)."""


# =============================================================================================
# FfmpegRenderer — v1 renderer (orchestrator decision 2026-11-17). Remotion = a later adapter
# behind the same interface. Brand = neutral stubs; every brand value is a config parameter.
#
# Config keys read (all optional, defaults below):
#   brand:  {channel_plate, font_file, font_name, text_color, plate_color, card_bg, card_text_color}
#   render: {cards: {reel|long|full: {intro_ms, outro_ms}}, lufs, true_peak, lra,
#            card_font_frac, plate_font_frac, reel_sub_font_size, reel_sub_margin_v,
#            thumb_font_px, thumb_max_bytes}
# =============================================================================================

import json as _json
import shutil as _shutil
import subprocess as _subprocess
import tempfile as _tempfile
from pathlib import Path as _Path

BRAND_DEFAULTS: dict[str, Any] = {
    "channel_plate": None,          # no plate until a channel is known
    "font_file": None,            # None -> first existing of FONT_CANDIDATES
    "font_name": "Arial",         # libass font name for burned reel subtitles
    "text_color": "#FFFFFF",      # text on plates (subtitles, channel plate, thumbnail)
    "plate_color": "#000000",     # opaque plate behind text
    "card_bg": "#14181F",         # intro/outro card background, thumbnail padding
    "card_text_color": "#FFFFFF",
}

RENDER_DEFAULTS: dict[str, Any] = {
    "cards": {
        "reel": {"intro_ms": 1500, "outro_ms": 1500},
        "long": {"intro_ms": 3000, "outro_ms": 4000},
        "full": {"intro_ms": 3000, "outro_ms": 4000},
    },
    "lufs": -14.0,
    "lufs_tolerance": 1.0,
    "true_peak": -1.0,
    "lra": 11.0,
    "card_font_frac": 0.075,      # card text size, fraction of min(width, height)
    "plate_font_frac": 0.045,     # channel plate size
    "reel_sub_font_size": 16,     # libass units (PlayResY 288)
    "reel_sub_margin_v": 40,
    "thumb_font_px": 72,
    "thumb_max_bytes": 2 * 1024 * 1024,
}

FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)

THUMB_W, THUMB_H = 1280, 720
MIN_CONTRAST = 4.5


def _merge(base: dict[str, Any], over: Any) -> dict[str, Any]:
    out = dict(base)
    if isinstance(over, dict):
        for k, v in over.items():
            out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def brand_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Brand values from config `brand` over BRAND_DEFAULTS."""
    return _merge(BRAND_DEFAULTS, (config or {}).get("brand"))


def render_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Render values from config `render` over RENDER_DEFAULTS."""
    return _merge(RENDER_DEFAULTS, (config or {}).get("render"))


def card_durations(config: dict[str, Any] | None, kind: str) -> tuple[int, int]:
    """(intro_ms, outro_ms) requested for a kind (before frame rounding)."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    c = render_settings(config)["cards"].get(kind) or {}
    return int(c.get("intro_ms", 0)), int(c.get("outro_ms", 0))


def parse_color(color: str) -> tuple[int, int, int]:
    """'#RRGGBB' | 'RRGGBB' | '0xRRGGBB' -> (r, g, b)."""
    s = str(color).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    s = s.lstrip("#")
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise ValueError(f"bad colour {color!r}; expected #RRGGBB")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def relative_luminance(color: str) -> float:
    """WCAG 2.x relative luminance of an sRGB colour."""
    def lin(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = parse_color(color)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio (1..21) between two colours."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def readable_text_color(text: str, plate: str, minimum: float = MIN_CONTRAST) -> str:
    """`text` if it reaches `minimum` contrast on `plate`, else black or white (the better one)."""
    if contrast_ratio(text, plate) >= minimum:
        return text
    return "#000000" if contrast_ratio("#000000", plate) >= contrast_ratio("#FFFFFF", plate) else "#FFFFFF"


def _ff_color(color: str) -> str:
    r, g, b = parse_color(color)
    return f"0x{r:02X}{g:02X}{b:02X}"


def _ass_color(color: str, alpha: int = 0) -> str:
    """ASS colour &HAABBGGRR."""
    r, g, b = parse_color(color)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def find_font(configured: str | None = None) -> str:
    """Font file for drawtext: the configured one, else the first existing candidate."""
    for p in ([configured] if configured else []) + list(FONT_CANDIDATES):
        if p and _Path(p).expanduser().exists():
            return str(_Path(p).expanduser())
    from ..core.errors import VPError
    raise VPError("data", "no font file found for drawtext; set config brand.font_file")


def wrap_text(text: str, max_chars: int, max_lines: int | None = None) -> list[str]:
    """Greedy word wrap; a word longer than max_chars stays on its own line."""
    words = " ".join((text or "").split()).split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        if not w:
            continue
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return lines


def _filter_quote(value: str) -> str:
    """Quote a value for a filtergraph option ('...'); single quotes are not allowed."""
    if "'" in value:
        from ..core.errors import VPError
        raise VPError("data", f"path with a quote is not supported in ffmpeg filters: {value}")
    return "'" + value.replace("\\", "/") + "'"


def video_info(path: str | os.PathLike) -> dict[str, Any]:
    """{width, height, fps (float), rate ('num/den'), video_ms, audio_ms (or None)} via ffprobe."""
    from . import media
    from ..core.errors import VPError
    cmd = [media.FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", str(path)]
    try:
        proc = _subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as e:
        raise VPError("internal", "ffprobe not found on PATH") from e
    if proc.returncode != 0:
        raise VPError("data", f"ffprobe failed on {_Path(path).name}: {proc.stderr.strip()[-300:]}")
    streams = (_json.loads(proc.stdout or "{}").get("streams")) or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise VPError("data", f"{_Path(path).name}: no video stream")
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "25/1"
    if rate in ("0/0", ""):
        rate = v.get("r_frame_rate") or "25/1"
    num, _, den = rate.partition("/")
    fps = float(num) / float(den or 1)

    def ms(s: dict | None) -> int | None:
        if not s or s.get("duration") in (None, "N/A"):
            return None
        return int(round(float(s["duration"]) * 1000))
    return {"width": int(v["width"]), "height": int(v["height"]), "fps": fps, "rate": rate,
            "video_ms": ms(v), "audio_ms": ms(a)}


def measure_lufs(path: str | os.PathLike) -> float:
    """Integrated loudness (LUFS) of a file, measured by ffmpeg loudnorm (first pass)."""
    from . import media
    return float(media.loudnorm_measure(path)["input_i"])


class FfmpegRenderer(Renderer):
    """Renderer on plain ffmpeg (drawtext for cards and plates, libass for burned subtitles,
    two-pass loudnorm). All outputs written atomically (temp file in the target dir + rename)."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.brand = brand_settings(self.config)
        self.settings = render_settings(self.config)
        self._font: str | None = None

    # ---- helpers -------------------------------------------------------------------------
    @property
    def font(self) -> str:
        if self._font is None:
            self._font = find_font(self.brand.get("font_file"))
        return self._font

    def thumbnail_colors(self) -> tuple[str, str]:
        """(text colour, plate colour) actually used on thumbnails (contrast-corrected)."""
        plate = self.brand["plate_color"]
        return readable_text_color(self.brand["text_color"], plate), plate

    def _drawtext(self, tmp: _Path, text: str, size: int, color: str, x: str, y: str,
                  box: str | None = None, boxborder: int = 0) -> str:
        tmp.mkdir(parents=True, exist_ok=True)
        fd, tf = _tempfile.mkstemp(prefix="t", suffix=".txt", dir=tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        parts = [f"fontfile={_filter_quote(self.font)}", f"textfile={_filter_quote(tf)}",
                 "expansion=none", f"fontsize={size}", f"fontcolor={_ff_color(color)}",
                 f"x={x}", f"y={y}"]
        if box:
            parts += ["box=1", f"boxcolor={_ff_color(box)}", f"boxborderw={boxborder}"]
        return "drawtext=" + ":".join(parts)

    def _centered_lines(self, tmp: _Path, text: str, w: int, h: int, size: int, color: str,
                        box: str | None = None) -> list[str]:
        max_chars = max(6, int(w * 0.86 / (size * 0.56)))
        lines = wrap_text(text, max_chars, max_lines=5)
        lh = int(size * 1.35)
        top = (h - lh * len(lines)) // 2
        return [self._drawtext(tmp, line, size, color, "(w-text_w)/2", str(top + i * lh), box,
                               max(2, size // 5) if box else 0)
                for i, line in enumerate(lines)]

    def _plate(self, tmp: _Path, w: int, h: int) -> str | None:
        text = self.brand.get("channel_plate")
        if not text:
            return None
        size = max(10, int(min(w, h) * float(self.settings["plate_font_frac"])))
        pad = max(4, size // 2)
        fg = readable_text_color(self.brand["text_color"], self.brand["plate_color"])
        return self._drawtext(tmp, text, size, fg, f"w-text_w-{pad}", str(pad),
                              self.brand["plate_color"], max(2, size // 4))

    @staticmethod
    def _to(out: _Path, run) -> _Path:
        """Run `run(tmp_path)` and atomically rename the temp file to `out`."""
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = _tempfile.mkstemp(prefix=f".{out.stem}.", suffix=out.suffix, dir=out.parent)
        os.close(fd)
        try:
            run(tmp)
            os.replace(tmp, out)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return out

    # ---- interface -----------------------------------------------------------------------
    def render_clip(self, cut_mp4, clip_srt, design_texts, kind, out_mp4) -> dict[str, int]:
        from . import media
        from ..core.errors import VPError
        if kind not in KINDS:
            raise VPError("data", f"render: unknown kind {kind!r}")
        info = video_info(cut_mp4)
        if info["audio_ms"] is None:
            raise VPError("data", "render: cut has no audio")
        w, h, fps, rate = info["width"], info["height"], info["fps"], info["rate"]
        want_in, want_out = card_durations(self.config, kind)
        in_frames, out_frames = round(want_in * fps / 1000), round(want_out * fps / 1000)
        intro_ms, outro_ms = round(in_frames * 1000 / fps), round(out_frames * 1000 / fps)
        clip_frames = round((info["video_ms"] or 0) * fps / 1000)
        total_s = (in_frames + clip_frames + out_frames) / fps
        bg = self.brand["card_bg"]
        card_fg = readable_text_color(self.brand["card_text_color"], bg)
        card_size = max(12, int(min(w, h) * float(self.settings["card_font_frac"])))
        texts = design_texts or {}
        tmpdir = _Path(_tempfile.mkdtemp(prefix="vp-render-"))
        try:
            inputs = ["-i", str(cut_mp4)]
            chains, segs = [], []
            afmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
            n_in = 1

            def card(frames: int, text: str, with_plate: bool, label: str) -> None:
                nonlocal n_in
                d = frames / fps
                inputs.extend(["-f", "lavfi", "-i", f"color=c={_ff_color(bg)}:s={w}x{h}:r={rate}:d={d:.6f}",
                               "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
                vi, ai = n_in, n_in + 1
                n_in += 2
                filters = self._centered_lines(tmpdir, text, w, h, card_size, card_fg) if text else []
                if with_plate:
                    p = self._plate(tmpdir, w, h)
                    if p:
                        filters.append(p)
                filters += ["format=yuv420p", "setsar=1"]
                chains.append(f"[{vi}:v]" + ",".join(filters) + f"[v{label}]")
                chains.append(f"[{ai}:a]atrim=duration={d:.6f},asetpts=N/SR/TB,{afmt}[a{label}]")
                segs.append(f"[v{label}][a{label}]")

            if in_frames > 0:
                card(in_frames, str(texts.get("intro_text") or ""), False, "i")
            main = []
            if kind == "reel":
                srt_copy = tmpdir / "clip.srt"
                _shutil.copyfile(clip_srt, srt_copy)
                sub_fg = readable_text_color(self.brand["text_color"], self.brand["plate_color"])
                style = ",".join([
                    f"Fontname={self.brand['font_name']}",
                    f"Fontsize={int(self.settings['reel_sub_font_size'])}",
                    f"PrimaryColour={_ass_color(sub_fg)}",
                    f"OutlineColour={_ass_color(self.brand['plate_color'])}",
                    f"BackColour={_ass_color(self.brand['plate_color'])}",
                    "BorderStyle=3", "Outline=2", "Shadow=0", "Alignment=2",
                    f"MarginV={int(self.settings['reel_sub_margin_v'])}",
                ])
                fontsdir = str(_Path(self.font).parent)
                main.append(f"subtitles=filename={_filter_quote(str(srt_copy))}:charenc=UTF-8:"
                            f"fontsdir={_filter_quote(fontsdir)}:force_style='{style}'")
                p = self._plate(tmpdir, w, h)
                if p:
                    main.append(p)
            main += ["format=yuv420p", "setsar=1"]
            chains.append("[0:v]" + ",".join(main) + "[vm]")
            chains.append(f"[0:a]{afmt}[am]")
            segs.append("[vm][am]")
            if out_frames > 0:
                card(out_frames, str(texts.get("announce_text") or texts.get("outro_text") or ""), True, "o")
            chains.append("".join(segs) + f"concat=n={len(segs)}:v=1:a=1[v][a]")
            joined = tmpdir / "joined.mp4"
            media.run_ffmpeg([*inputs, "-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]",
                              *media.VIDEO_ENC, *media.AUDIO_ENC, str(joined)])
            st = self.settings
            measured = media.loudnorm_measure(joined, st["lufs"], st["true_peak"], st["lra"])
            af = (f"loudnorm=I={st['lufs']}:TP={st['true_peak']}:LRA={st['lra']}:"
                  f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
                  f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
                  f"offset={measured['target_offset']}:linear=true,"
                  f"aresample=48000,atrim=duration={total_s:.6f}")
            self._to(_Path(out_mp4), lambda tmp: media.run_ffmpeg(
                ["-i", str(joined), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-af", af,
                 "-ar", "48000", *media.AUDIO_ENC, "-movflags", "+faststart", tmp]))
        finally:
            _shutil.rmtree(tmpdir, ignore_errors=True)
        return {"intro_ms": intro_ms, "outro_ms": outro_ms,
                "duration_ms": media.duration_ms(out_mp4)}

    def render_thumbnail(self, frame_png, text, out_png) -> None:
        from . import media
        from ..core.errors import VPError
        fg, plate = self.thumbnail_colors()
        size = int(self.settings["thumb_font_px"])
        max_chars = max(6, int(THUMB_W * 0.88 / (size * 0.56)))
        lines = wrap_text(text or "", max_chars, max_lines=3)
        lh = int(size * 1.4)
        tmpdir = _Path(_tempfile.mkdtemp(prefix="vp-thumb-"))
        try:
            vf = [f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease",
                  f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2:color={_ff_color(self.brand['card_bg'])}",
                  "setsar=1"]
            top = THUMB_H - 48 - lh * len(lines)
            for i, line in enumerate(lines):
                vf.append(self._drawtext(tmpdir, line, size, fg, "60", str(top + i * lh), plate, size // 5))
            p = self._plate(tmpdir, THUMB_W, THUMB_H)
            if p:
                vf.append(p)
            limit = int(self.settings["thumb_max_bytes"])

            def render(tmp: str, palette: bool) -> None:
                chain = ",".join(vf)
                if palette:
                    chain = chain + ",split[a][b];[a]palettegen=max_colors=256[p];[b][p]paletteuse"
                    media.run_ffmpeg(["-i", str(frame_png), "-filter_complex", chain, "-frames:v", "1",
                                      "-update", "1", "-f", "image2", "-c:v", "png", tmp])
                else:
                    media.run_ffmpeg(["-i", str(frame_png), "-vf", chain, "-frames:v", "1",
                                      "-update", "1", "-f", "image2", "-c:v", "png",
                                      "-pix_fmt", "rgb24", "-compression_level", "9", tmp])

            def run(tmp: str) -> None:
                render(tmp, False)
                if os.path.getsize(tmp) >= limit:
                    render(tmp, True)
                if os.path.getsize(tmp) >= limit:
                    raise VPError("data", f"thumbnail is {os.path.getsize(tmp)} bytes, limit {limit}")
            self._to(_Path(out_png), run)
        finally:
            _shutil.rmtree(tmpdir, ignore_errors=True)
