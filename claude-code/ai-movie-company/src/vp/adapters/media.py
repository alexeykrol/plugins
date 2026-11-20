"""ffprobe/ffmpeg wrappers. All times are integer milliseconds.

Every function writes its output atomically: ffmpeg renders into a temp file in the target
directory, which is then renamed. Failures raise VPError("data") with the tail of stderr.
Encoding defaults: H.264 (libx264, yuv420p, CRF 18), AAC 192k, +faststart.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from ..core import progress
from ..core.errors import VPError

FFMPEG = os.environ.get("VP_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("VP_FFPROBE", "ffprobe")
VIDEO_ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
AUDIO_ENC = ["-c:a", "aac", "-b:a", "192k"]


def _sec(ms: int | float) -> str:
    return f"{ms / 1000:.3f}"


def run_ffmpeg(args: Sequence[str], timeout_s: float = 3600,
               progress_ms: int | None = None) -> subprocess.CompletedProcess:
    """Run ffmpeg with -hide_banner -nostdin -y; raises VPError(data) on failure.

    With an active progress tracker (vp.core.progress) the call reports how much of the media is
    processed (`-progress pipe:1`). `progress_ms` = expected output duration; when None it is
    taken from `-t` or from the first input's duration (single-frame calls report nothing).
    """
    args = [str(a) for a in args]
    total = _progress_total(args, progress_ms) if progress.current() is not None else None
    if total:
        return _run_with_progress(args, timeout_s, total)
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError as e:
        raise VPError("internal", "ffmpeg not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise VPError("data", f"ffmpeg timed out after {timeout_s}s") from e
    if proc.returncode != 0:
        raise VPError("data", f"ffmpeg failed: {proc.stderr.strip()[-500:]}")
    return proc


def _progress_total(args: list[str], progress_ms: int | None) -> int | None:
    """Expected duration (ms) of the output of an ffmpeg call, or None (no progress)."""
    if progress_ms:
        return int(progress_ms)
    if "-frames:v" in args or "-vframes" in args:
        return None
    if "-t" in args:
        try:
            return int(float(args[args.index("-t") + 1]) * 1000)
        except (IndexError, ValueError):
            return None
    if "-i" in args and "-f" not in args[:args.index("-i")]:
        try:
            ms = duration_ms(args[args.index("-i") + 1])
        except Exception:  # noqa: BLE001 — progress is best effort
            return None
        return ms or None
    return None


def parse_progress_line(line: str) -> int | None:
    """Milliseconds of output written, from one `-progress` line (out_time_us / out_time_ms are
    both microseconds in ffmpeg; out_time=HH:MM:SS.ffffff as a fallback); None otherwise."""
    key, _, val = line.strip().partition("=")
    val = val.strip()
    if key in ("out_time_us", "out_time_ms"):
        try:
            us = int(val)
        except ValueError:
            return None
        return max(us // 1000, 0)
    if key == "out_time":
        m = re.fullmatch(r"(-?)(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", val)
        if not m or m.group(1):
            return None
        return int((int(m.group(2)) * 3600 + int(m.group(3)) * 60 + float(m.group(4))) * 1000)
    return None


def _run_with_progress(args: list[str], timeout_s: float, total_ms: int) -> subprocess.CompletedProcess:
    import threading
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                bufsize=1)
    except FileNotFoundError as e:
        raise VPError("internal", "ffmpeg not found on PATH") from e
    err: list[str] = []
    reader = threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True)
    reader.start()
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        proc.kill()
    timer = threading.Timer(timeout_s, _kill)
    timer.start()
    since = progress.clock()
    out_lines: list[str] = []
    try:
        for line in proc.stdout:
            ms = parse_progress_line(line)
            if ms is not None:
                progress.report(min(ms, total_ms) / 1000, total_ms / 1000, "s", since=since)
            elif not line.startswith(("progress=", "frame=", "fps=", "stream_", "bitrate=",
                                      "total_size=", "dup_frames=", "drop_frames=", "speed=",
                                      "out_time")):
                out_lines.append(line)
        rc = proc.wait()
    finally:
        timer.cancel()
        reader.join(timeout=5)
        for f in (proc.stdout, proc.stderr):
            f.close()
    stderr = "".join(err)
    if timed_out.is_set():
        raise VPError("data", f"ffmpeg timed out after {timeout_s}s")
    if rc != 0:
        raise VPError("data", f"ffmpeg failed: {stderr.strip()[-500:]}")
    progress.report(total_ms / 1000, total_ms / 1000, "s", since=since, force=True)
    return subprocess.CompletedProcess(cmd, rc, "".join(out_lines), stderr)


def _render_to(out: str | os.PathLike, args_before_out: Sequence[str], timeout_s: float = 3600) -> Path:
    """Run ffmpeg writing to a temp file next to `out`, then rename atomically."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.stem}.", suffix=out.suffix, dir=out.parent)
    os.close(fd)
    try:
        run_ffmpeg([*args_before_out, tmp], timeout_s=timeout_s)
        os.replace(tmp, out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return out


def _probe_int(value: int | str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def probe(path: str | os.PathLike) -> dict[str, Any]:
    """{duration_ms, width, height, fps, has_audio, video_codec, audio_codec, sample_rate, channels}."""
    p = Path(path)
    if not p.exists():
        raise VPError("data", f"media not found: {p}")
    cmd = [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as e:
        raise VPError("internal", "ffprobe not found on PATH") from e
    if proc.returncode != 0:
        raise VPError("data", f"ffprobe failed on {p.name}: {proc.stderr.strip()[-300:]}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = None
    if v and v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
        num, _, den = v["avg_frame_rate"].partition("/")
        fps = round(float(num) / float(den or 1), 3)
    dur = (data.get("format") or {}).get("duration") or (v or {}).get("duration") or 0
    return {"duration_ms": int(round(float(dur) * 1000)),
            "width": int(v["width"]) if v else None, "height": int(v["height"]) if v else None,
            "fps": fps, "has_audio": a is not None,
            "video_codec": v.get("codec_name") if v else None,
            "audio_codec": a.get("codec_name") if a else None,
            "sample_rate": _probe_int(a.get("sample_rate")) if a else None,
            "channels": _probe_int(a.get("channels")) if a else None}


def duration_ms(path: str | os.PathLike) -> int:
    return probe(path)["duration_ms"]


def has_audio(path: str | os.PathLike) -> bool:
    return probe(path)["has_audio"]


def cut(src: str | os.PathLike, start_ms: int, end_ms: int, out: str | os.PathLike,
        vf: str | None = None) -> Path:
    """Frame-accurate cut [start_ms, end_ms) with re-encode; `vf` = optional video filter
    (e.g. crop_scale_filter), applied in the same encode."""
    if end_ms <= start_ms or start_ms < 0:
        raise VPError("data", f"bad cut range {start_ms}..{end_ms}")
    return _render_to(out, ["-ss", _sec(start_ms), "-i", str(src), "-t", _sec(end_ms - start_ms),
                            "-map", "0:v:0", "-map", "0:a:0?", *(["-vf", vf] if vf else []),
                            *VIDEO_ENC, *AUDIO_ENC, "-movflags", "+faststart"])


def crop_scale_filter(x: int, y: int, w: int, h: int, out_w: int | None = None,
                      out_h: int | None = None) -> str:
    """ffmpeg filter: crop the rectangle (even sizes), then scale to out_w×out_h when given
    (fill, no padding — the rectangle already has the target aspect)."""
    w, h = w - w % 2, h - h % 2
    vf = f"crop={w}:{h}:{x}:{y}"
    if out_w and out_h and (out_w, out_h) != (w, h):
        vf += f",scale={out_w}:{out_h}:flags=lanczos"
    return vf + ",setsar=1"


def crop(src: str | os.PathLike, out: str | os.PathLike, x: int, y: int, w: int, h: int) -> Path:
    """Crop a rectangle (even sizes enforced)."""
    w, h = w - w % 2, h - h % 2
    return _render_to(out, ["-i", str(src), "-vf", f"crop={w}:{h}:{x}:{y}", "-map", "0:v:0",
                            "-map", "0:a:0?", *VIDEO_ENC, "-c:a", "copy", "-movflags", "+faststart"])


def crop_fraction(src: str | os.PathLike, out: str | os.PathLike, x_frac: float, w_frac: float) -> Path:
    """Vertical-band crop by fractions of the width (profile reel_crop), full height."""
    info = probe(src)
    w = int(info["width"] * w_frac)
    x = int(info["width"] * x_frac)
    return crop(src, out, x, 0, w, info["height"])


def scale_pad(src: str | os.PathLike, out: str | os.PathLike, width: int, height: int,
              color: str = "black") -> Path:
    """Scale to fit width×height keeping aspect, pad the rest."""
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={color},setsar=1")
    return _render_to(out, ["-i", str(src), "-vf", vf, "-map", "0:v:0", "-map", "0:a:0?",
                            *VIDEO_ENC, "-c:a", "copy", "-movflags", "+faststart"])


def concat(parts: Sequence[str | os.PathLike], out: str | os.PathLike) -> Path:
    """Concatenate clips (same resolution expected) with re-encode via the concat demuxer."""
    if not parts:
        raise VPError("data", "concat: no parts")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, lst = tempfile.mkstemp(prefix=".concat.", suffix=".txt", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for p in parts:
                f.write("file '" + str(Path(p).resolve()).replace("'", "'\\''") + "'\n")
        return _render_to(out, ["-f", "concat", "-safe", "0", "-i", lst, *VIDEO_ENC, *AUDIO_ENC,
                                "-movflags", "+faststart"])
    finally:
        os.unlink(lst)


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def loudnorm_measure(src: str | os.PathLike, i: float = -14.0, tp: float = -1.0,
                     lra: float = 11.0) -> dict[str, str]:
    """First loudnorm pass: returns the measured values (input_i, input_tp, input_lra,
    input_thresh, target_offset, ...) as strings, as printed by ffmpeg."""
    proc = run_ffmpeg(["-i", str(src), "-vn", "-af", f"loudnorm=I={i}:TP={tp}:LRA={lra}:print_format=json",
                       "-f", "null", "-"])
    m = _LOUDNORM_JSON.search(proc.stderr)
    if not m:
        raise VPError("data", "loudnorm: no measurement in ffmpeg output")
    return json.loads(m.group(0))


def loudnorm_apply(src: str | os.PathLike, out: str | os.PathLike, measured: dict[str, str],
                   i: float = -14.0, tp: float = -1.0, lra: float = 11.0) -> Path:
    """Second loudnorm pass (linear) with measured values; video stream copied."""
    af = (f"loudnorm=I={i}:TP={tp}:LRA={lra}:measured_I={measured['input_i']}:"
          f"measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:"
          f"measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:linear=true")
    return _render_to(out, ["-i", str(src), "-map", "0:v:0?", "-map", "0:a:0", "-c:v", "copy",
                            "-af", af, "-ar", "48000", *AUDIO_ENC, "-movflags", "+faststart"])


def extract_still(src: str | os.PathLike, at_ms: int, out_png: str | os.PathLike) -> Path:
    """One frame at `at_ms` as PNG."""
    return _render_to(out_png, ["-ss", _sec(at_ms), "-i", str(src), "-frames:v", "1",
                                "-update", "1", "-f", "image2", "-c:v", "png"])


_BLACK_RE = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")


def black_frames(src: str | os.PathLike, min_duration_s: float = 0.1,
                 pixel_threshold: float = 0.10) -> list[tuple[int, int]]:
    """Black intervals [(start_ms, end_ms)] found by ffmpeg blackdetect."""
    proc = run_ffmpeg(["-i", str(src), "-an", "-vf",
                       f"blackdetect=d={min_duration_s}:pix_th={pixel_threshold}", "-f", "null", "-"])
    return [(int(float(a) * 1000), int(float(b) * 1000)) for a, b in _BLACK_RE.findall(proc.stderr)]


def is_black_frame(png_or_video: str | os.PathLike, at_ms: int = 0) -> bool:
    """True if the frame at `at_ms` is (almost) black."""
    proc = run_ffmpeg(["-ss", _sec(at_ms), "-i", str(png_or_video), "-frames:v", "1", "-vf",
                       "signalstats,metadata=print:key=lavfi.signalstats.YAVG", "-f", "null", "-"])
    m = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", proc.stderr)
    return bool(m and float(m.group(1)) < 20.0)


_SILENCE_START = re.compile(r"silence_start:\s*([\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([\d.]+)")


def silences(src: str | os.PathLike, noise_db: float = -40.0, min_s: float = 0.5) -> list[tuple[int, int]]:
    """Silent intervals [(start_ms, end_ms)] via silencedetect (open end -> media duration)."""
    proc = run_ffmpeg(["-i", str(src), "-vn", "-af", f"silencedetect=n={noise_db}dB:d={min_s}",
                       "-f", "null", "-"])
    starts = [float(x) for x in _SILENCE_START.findall(proc.stderr)]
    ends = [float(x) for x in _SILENCE_END.findall(proc.stderr)]
    out = []
    for k, s in enumerate(starts):
        e = ends[k] if k < len(ends) else probe(src)["duration_ms"] / 1000
        out.append((int(s * 1000), int(e * 1000)))
    return out


def available() -> bool:
    """True if ffmpeg and ffprobe are on PATH."""
    return shutil.which(FFMPEG) is not None and shutil.which(FFPROBE) is not None
