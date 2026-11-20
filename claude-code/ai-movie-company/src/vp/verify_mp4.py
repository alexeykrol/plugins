# Based on nexu-io/openmotion's scripts/verify-export.ts (51bcb20, Apache-2.0).
"""Technical verification of a finished MP4."""
from __future__ import annotations

import json
import math
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .adapters import media
from .core.errors import VPError

AudioPolicy = Literal["required", "optional", "forbidden"]
Status = Literal["passed", "failed", "blocked"]

NOT_COVERED = (
    "Visual quality and agreement with the brief or storyboard",
    "Playback, seeking and replay in the target browser or player",
    "Audibility, soundtrack content and audio/video synchronization",
    "Expected resolution, frame rate and audio codec",
    "Byte-for-byte integrity or changes that preserve file size and modification time",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    details: str


@dataclass(frozen=True)
class VerificationResult:
    status: Status
    checks: tuple[CheckResult, ...]
    not_covered: tuple[str, ...] = NOT_COVERED


def _seconds(value: object) -> float | None:
    try:
        seconds = float(str(value))
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None


def _probe(path: Path, *args: str) -> dict:
    # The media adapter's probe summary drops stream counts and pixel format.
    process = subprocess.run(
        [media.FFPROBE, "-v", "error", "-protocol_whitelist", "file", "-of", "json",
         *args, str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if process.returncode or process.stderr.strip():
        raise VPError("data", f"ffprobe failed: {process.stderr.strip()[-500:]}")
    return json.loads(process.stdout)


def _decode(path: Path, name: str, at_s: float | None = None) -> CheckResult:
    args = ["-loglevel", "error", "-xerror", "-err_detect", "explode",
            "-protocol_whitelist", "file", "-abort_on", "empty_output_stream"]
    if at_s is not None:
        args.extend(["-ss", f"{at_s:.6f}"])
    args.extend(["-f", "mov", "-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough"])
    if at_s is None:
        args.extend(["-map", "0:a?"])
    else:
        args.extend(["-frames:v", "1", "-an"])
    try:
        process = media.run_ffmpeg([*args, "-f", "null", "-"])
        if process.stderr.strip():
            return CheckResult(name, "failed", process.stderr.strip()[-500:])
    except VPError as exc:
        status = "blocked" if isinstance(exc.__cause__, FileNotFoundError) else "failed"
        return CheckResult(name, status, exc.msg)
    except OSError as exc:
        return CheckResult(name, "blocked", str(exc))
    details = "Decoded video and all present audio streams."
    if at_s is not None:
        details = f"Sought to {at_s:.6f}s and decoded a video frame."
    return CheckResult(name, "passed", details)


def verify_mp4(
    path: str | Path,
    *,
    audio_policy: AudioPolicy = "optional",
    expected_duration_s: float | None = None,
    duration_tolerance_s: float = 0.1,
) -> VerificationResult:
    """Check an exported MP4. The duration and absolute tolerance are in seconds."""
    if audio_policy not in ("required", "optional", "forbidden"):
        raise ValueError(f"Unknown audio policy: {audio_policy!r}")
    if expected_duration_s is not None and (
        not math.isfinite(expected_duration_s) or expected_duration_s <= 0
    ):
        raise ValueError("Expected duration must be finite and positive.")
    if not math.isfinite(duration_tolerance_s) or duration_tolerance_s < 0:
        raise ValueError("Duration tolerance must be finite and nonnegative.")

    if not media.available():
        return VerificationResult("blocked", (
            CheckResult("tools", "blocked", "Both ffmpeg and ffprobe are required."),
        ))

    file = Path(path).absolute()
    try:
        before = file.stat()
    except OSError as exc:
        return VerificationResult("failed", (CheckResult("file", "failed", str(exc)),))
    if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
        return VerificationResult("failed", (
            CheckResult("file", "failed", "Expected a nonempty regular file."),
        ))

    checks = [CheckResult("file", "passed", f"{before.st_size} bytes")]

    def record(name: str, passed: bool, details: str) -> None:
        checks.append(CheckResult(name, "passed" if passed else "failed", details))

    try:
        probe = _probe(file, "-show_format", "-show_streams")
    except OSError as exc:
        checks.append(CheckResult("probe", "blocked", str(exc)))
    except (VPError, subprocess.TimeoutExpired, ValueError) as exc:
        record("probe", False, str(exc))
    else:
        record("probe", True, "Read stream and container metadata.")
        streams = probe.get("streams", [])
        container = probe.get("format", {})
        format_name = container.get("format_name", "")
        brand = container.get("tags", {}).get("major_brand", "").strip()
        record("mp4_container", "mp4" in format_name.split(",") and brand != "qt",
               f"Format: {format_name}. Major brand: {brand or 'unknown'}.")

        videos = [stream for stream in streams if stream.get("codec_type") == "video"]
        video = videos[0] if videos else {}
        codec, pix_fmt = video.get("codec_name"), video.get("pix_fmt")
        record("video_stream", len(videos) == 1 and codec == "h264" and pix_fmt == "yuv420p",
               f"{len(videos)} video stream(s). First codec: {codec}. Pixel format: {pix_fmt}.")

        audio_count = sum(stream.get("codec_type") == "audio" for stream in streams)
        audio_ok = {"required": audio_count > 0, "optional": True, "forbidden": audio_count == 0}
        record("audio_policy", audio_ok[audio_policy],
               f"{audio_count} audio stream(s). Policy: {audio_policy}.")

        container_s = _seconds(container.get("duration"))
        video_s = _seconds(video.get("duration"))
        duration_s = video_s if video_s is not None else container_s
        if expected_duration_s is not None:
            lower = expected_duration_s - duration_tolerance_s
            upper = expected_duration_s + duration_tolerance_s
            in_range = duration_s is not None and lower <= duration_s <= upper
            container_in_range = container_s is None or lower <= container_s <= upper
            record("duration", in_range and container_in_range,
                   f"Video: {video_s}s. Container: {container_s}s. Expected range: [{lower}, {upper}]s.")

        if videos:
            checks.append(_decode(file, "full_decode"))
            video_start_s = _seconds(video.get("start_time")) or 0.0
            container_start_s = _seconds(container.get("start_time")) or 0.0
            offset_s = max(0.0, video_start_s - container_start_s)
            checks.append(_decode(file, "seek_start", offset_s))
            if duration_s is not None and duration_s > 0:
                _check_tail_seeks(file, checks, video_start_s, container_start_s, duration_s)
            else:
                record("seek_middle", False, "A positive video duration is required.")
                record("seek_end", False, "A positive video duration is required.")

    try:
        after = file.stat()
        unchanged = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        )
        record("unchanged_file", unchanged, "Compared file identity, size and modification time.")
    except OSError as exc:
        record("unchanged_file", False, str(exc))

    statuses = {check.status for check in checks}
    if "blocked" in statuses:
        return VerificationResult("blocked", tuple(checks))
    return VerificationResult("failed" if "failed" in statuses else "passed", tuple(checks))


def _check_tail_seeks(
    file: Path,
    checks: list[CheckResult],
    video_start_s: float,
    container_start_s: float,
    duration_s: float,
) -> None:
    midpoint_s = video_start_s + duration_s / 2
    try:
        # FFprobe seeks to a preceding keyframe. Read through EOF to find the last
        # presentation timestamp even with reordered frames or variable frame durations.
        tail_start_s = video_start_s + duration_s * 0.9
        tail = _probe(file, "-select_streams", "v:0", "-read_intervals", f"{tail_start_s:.6f}%",
                      "-show_entries", "packet=pts_time")
        timestamps = (_seconds(packet.get("pts_time")) for packet in tail.get("packets", []))
        last_pts = max((timestamp for timestamp in timestamps if timestamp is not None), default=None)
        if last_pts is None:
            raise ValueError("No video packet timestamp found after seeking into the file.")
    except OSError as exc:
        end = CheckResult("seek_end", "blocked", str(exc))
    except (VPError, subprocess.TimeoutExpired, ValueError) as exc:
        end = CheckResult("seek_end", "failed", str(exc))
    else:
        midpoint_s = min(midpoint_s, last_pts)
        # Allow for rounding between FFprobe's timestamp and FFmpeg's seek argument.
        end = _decode(file, "seek_end", max(0.0, last_pts - container_start_s - 0.000001))
    checks.append(_decode(file, "seek_middle", max(0.0, midpoint_s - container_start_s)))
    checks.append(end)
