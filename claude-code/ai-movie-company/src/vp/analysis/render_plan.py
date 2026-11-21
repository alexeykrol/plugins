"""Render a segment plan with ffmpeg in one pass (shared by montage and takes).

A segment plan is the edit plan of SPEC §0 layer 3 in its simplest form: an ordered list of
source ranges laid end to end, plus optionally one music track under all of them.

  segments  [{source_path, in_ms, duration_ms, ...}] — laid at 0, d0, d0+d1, …
  music     {path, in_ms} | None — cut to the total length, faded out at the end
  canvas    (w, h, fps) — every segment is scaled to fill and centre-cropped (never letterboxed)

Cut points are placed on frames from the cumulative times (frame = round(t·fps)), so a long plan
does not drift away from the beat grid. `keep_audio=True` keeps each segment's own sound (takes
cleaning) with 10 ms fades at the joins against clicks; then every source must have audio.
"""
from __future__ import annotations

import math
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from typing import Any, Sequence

from ..adapters import media
from ..core.errors import VPError
from ..verify_mp4 import _probe, _seconds

JOIN_FADE_S = 0.010
MUSIC_FADE_S = 1.5


def segment_frames(durations_ms: Sequence[int], fps: float) -> list[int]:
    """Frames per segment from cumulative boundaries: no drift, sums to round(total·fps)."""
    out, t = [], 0
    for d in durations_ms:
        a, b = round(t * fps / 1000), round((t + d) * fps / 1000)
        out.append(int(b - a))
        t += d
    return out


def ffmpeg_args(segments: Sequence[dict[str, Any]], canvas: tuple[int, int, float],
                music: dict[str, Any] | None = None, keep_audio: bool = False) -> list[str]:
    if not segments:
        raise VPError("data", "plan has no segments")
    w, h, fps = int(canvas[0]), int(canvas[1]), float(canvas[2])
    w, h = w - w % 2, h - h % 2
    if w <= 0 or h <= 0 or not math.isfinite(fps) or fps <= 0:
        raise VPError("data", "canvas dimensions and frame rate must be finite and positive")
    frames = segment_frames([int(s["duration_ms"]) for s in segments], fps)
    if any(f <= 0 for f in frames):
        raise VPError("data", "plan has a segment shorter than one frame")
    total_s = sum(frames) / fps
    args: list[str] = []
    parts: list[str] = []
    graph: list[str] = []
    for i, (s, f) in enumerate(zip(segments, frames)):
        dur_s = f / fps
        args += ["-ss", f"{int(s['in_ms']) / 1000:.3f}", "-t", f"{dur_s + 0.25:.3f}",
                 "-i", str(s["source_path"])]
        # Start the frame grid at the first decoded frame and preserve the final input frame.
        graph.append(f"[{i}:v:0]setpts=PTS-STARTPTS,fps={fps:g}:eof_action=pass,"
                     f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                     f"crop={w}:{h},setsar=1,trim=end_frame={f}[v{i}]")
        parts.append(f"[v{i}]")
        if keep_audio:
            fo = max(0.0, dur_s - JOIN_FADE_S)
            graph.append(f"[{i}:a:0]atrim=duration={dur_s:.6f},asetpts=PTS-STARTPTS,"
                         f"aresample=48000,afade=t=in:d={JOIN_FADE_S},"
                         f"afade=t=out:st={fo:.6f}:d={JOIN_FADE_S}[a{i}]")
            parts.append(f"[a{i}]")
    n = len(segments)
    graph.append("".join(parts) + f"concat=n={n}:v=1:a={1 if keep_audio else 0}"
                 + ("[v][a]" if keep_audio else "[v]"))
    maps = ["-map", "[v]"]
    if keep_audio:
        maps += ["-map", "[a]"]
    elif music:
        args += ["-ss", f"{int(music.get('in_ms') or 0) / 1000:.3f}", "-t", f"{total_s + 0.5:.3f}",
                 "-i", str(music["path"])]
        fade = min(MUSIC_FADE_S, total_s / 4)
        graph.append(f"[{n}:a:0]atrim=duration={total_s:.6f},asetpts=PTS-STARTPTS,aresample=48000,"
                     f"afade=t=out:st={total_s - fade:.6f}:d={fade:.3f}[a]")
        maps += ["-map", "[a]"]
    return [*args, "-filter_complex", ";".join(graph), *maps, *media.VIDEO_ENC,
            *(media.AUDIO_ENC if (keep_audio or music) else []), "-r", f"{fps:g}",
            "-movflags", "+faststart"]


def render(segments: Sequence[dict[str, Any]], out: str | Path, canvas: tuple[int, int, float],
           music: dict[str, Any] | None = None, keep_audio: bool = False,
           timeout_s: float = 3600) -> Path:
    for s in segments:
        if not Path(s["source_path"]).is_file():
            raise VPError("data", f"plan source not found: {s['source_path']}")
    if music and not Path(music["path"]).is_file():
        raise VPError("data", f"music not found: {music['path']}")
    args = ffmpeg_args(segments, canvas, music, keep_audio)
    fps = float(canvas[2])
    frames = segment_frames([int(s["duration_ms"]) for s in segments], fps)
    expected_s = sum(frames) / fps
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{out.stem}.", dir=out.parent) as tmp:
        rendered = media._render_to(Path(tmp) / out.name, args, timeout_s=timeout_s)
        # Inspect the video stream directly. Audio may extend the container duration.
        try:
            result = _probe(rendered, "-select_streams", "v:0", "-show_entries", "stream=duration")
        except (OSError, TimeoutExpired, ValueError) as exc:
            raise VPError("data", f"cannot verify rendered video duration: {exc}") from exc
        streams = result.get("streams") or []
        video_s = _seconds(streams[0].get("duration")) if streams else None
        if video_s is None or video_s <= 0 or abs(video_s - expected_s) > 1 / fps + 0.001:
            raise VPError("data", f"rendered video stream duration {video_s}s differs from plan "
                          f"({expected_s:.3f}s)")
        rendered.replace(out)
    return out
