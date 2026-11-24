"""Local measurements for builder outputs and bounded final-file audio mastering."""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import tempfile
from array import array
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Mapping

from ..builders.contracts import (
    QACheck, QARecord, Rect, RenderReelBody, Timeline, validate_qa_record, validate_timeline,
)
from ..builders.presets import REEL_PRESETS, ReelPreset
from ..core.errors import VPError
from ..core.io import sha256_bytes, sha256_file
from . import media
from .build_render import EffectGeometry, inspect_effect_bounds
from .reel_cut import (
    ScreenBackground, _crop_filter, _medium_background_filter, medium_foreground, source_picture_time,
)


@dataclass(frozen=True)
class ProductionQAPolicy:
    revision: str
    target_lufs: float
    lufs_tolerance: float
    max_dbtp: float
    mastering_retries: int
    limiter_sample_rate: int
    audio_lag_grid_ms: int
    audio_lag_search_ms: int
    max_audio_lag_ms: int
    min_audio_correlation: float
    silence_db: float
    quiet_lead_ms: int
    audio_open_ms: int
    quiet_tail_ms: int
    tail_picture_ms: int
    min_card_ssim: float
    intro_frames: int | None
    outro_frames: int | None
    intro_readable_dwell_frames: int
    outro_readable_dwell_frames: int

    def __post_init__(self):
        if not (0 <= self.audio_open_ms <= self.quiet_lead_ms and self.quiet_tail_ms >= 0
                and self.tail_picture_ms >= 0 and 0 < self.min_audio_correlation <= 1
                and 0 < self.min_card_ssim <= 1 and self.audio_lag_grid_ms > 0
                and self.audio_lag_search_ms > 0 and 0 <= self.max_audio_lag_ms < self.audio_lag_search_ms
                and self.audio_lag_search_ms % self.audio_lag_grid_ms == 0
                and self.silence_db < 0
                and self.mastering_retries >= 0 and self.limiter_sample_rate > 0
                and self.lufs_tolerance >= 0 and -24 <= self.max_dbtp <= 0
                and self.intro_readable_dwell_frames > 0 and self.outro_readable_dwell_frames > 0
                and all(value is None or value > 0 for value in (self.intro_frames, self.outro_frames))):
            raise ValueError("invalid production QA policy")

    @classmethod
    def for_reel(
        cls, preset: ReelPreset, *, revision: str, max_audio_lag_ms: int, min_audio_correlation: float,
        min_card_ssim: float, quiet_tail_ms: int, intro_readable_dwell_frames: int,
        thought_readable_dwell_frames: int,
    ) -> ProductionQAPolicy:
        return cls(
            revision=revision, target_lufs=preset.target_lufs, lufs_tolerance=preset.lufs_tolerance,
            max_dbtp=preset.max_dbtp, mastering_retries=preset.mastering_retries,
            limiter_sample_rate=preset.limiter_sample_rate, audio_lag_grid_ms=preset.audio_lag_grid_ms,
            audio_lag_search_ms=preset.audio_lag_search_ms, max_audio_lag_ms=max_audio_lag_ms,
            min_audio_correlation=min_audio_correlation, silence_db=preset.silence_db,
            quiet_lead_ms=preset.quiet_lead_ms, audio_open_ms=preset.audio_open_ms,
            quiet_tail_ms=quiet_tail_ms, tail_picture_ms=preset.tail_picture_ms, min_card_ssim=min_card_ssim,
            intro_frames=round(Fraction(preset.intro_ms * preset.fps, 1000)),
            outro_frames=round(Fraction(preset.thought_ms * preset.fps, 1000)),
            intro_readable_dwell_frames=intro_readable_dwell_frames,
            outro_readable_dwell_frames=thought_readable_dwell_frames,
        )


def _measured(passed: bool, values: dict, evidence: list[str], reason: str | None = None) -> QACheck:
    check: QACheck = {"status": "passed" if passed else "failed", "values": values, "evidence": evidence}
    if reason and not passed:
        check["reason"] = reason
    return check


def _missing(reason: str, values: dict | None = None, evidence: list[str] | None = None) -> QACheck:
    return {"status": "not_run", "values": values or {}, "evidence": evidence or [], "reason": reason}


def _local_command(exe: str, args: list[str], *, binary: bool = False, timeout: int = 120,
                   stderr_output: bool = False):
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=not binary, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VPError("data", f"local media command unavailable: {exc}") from exc
    if proc.returncode:
        error = proc.stderr.decode(errors="replace") if binary else proc.stderr
        raise VPError("data", f"local media command failed: {error.strip()[-300:]}")
    return proc.stderr if stderr_output else proc.stdout


_LOCAL_PROTOCOLS = ["-protocol_whitelist", "file,pipe"]


def _video_probe(path: Path, *, cadence: bool = False) -> dict | None:
    entries = "stream=width,height,avg_frame_rate,nb_read_frames,time_base"
    flags = ["-show_frames"] if cadence else []
    if cadence:
        entries += ":frame=best_effort_timestamp"
    raw = _local_command(media.FFPROBE, [
        "-v", "error", *_LOCAL_PROTOCOLS, "-select_streams", "v:0", "-count_frames",
        *flags, "-show_entries", entries, "-of", "json", str(path),
    ], timeout=3600)
    data = json.loads(raw)
    stream = next(iter(data.get("streams", [])), None)
    if stream is not None and cadence:
        stream["timestamps"] = [frame.get("best_effort_timestamp") for frame in data.get("frames", [])]
    return stream


def _frame_count(stream: dict | None) -> int | None:
    frames = (stream or {}).get("nb_read_frames", "N/A")
    return int(frames) if str(frames).isdigit() else None


def _format_check(video: dict | None, timeline: Timeline, path: Path) -> QACheck:
    expected_rate = Fraction(timeline["frame_rate"]["num"], timeline["frame_rate"]["den"])
    expected = {**timeline["canvas"], "rate": str(expected_rate), "frames": timeline["duration_frames"]}
    evidence = [f"ffprobe decoded frames, presentation timestamps and stream metadata: {path.name}"]
    if video is None:
        return _measured(False, {"expected": expected}, evidence, "no video stream")
    try:
        rate = Fraction(video.get("avg_frame_rate", "0/0"))
    except (ValueError, ZeroDivisionError):
        rate = None
    actual = {"width": video.get("width"), "height": video.get("height"), "rate": str(rate) if rate else None,
              "frames": _frame_count(video)}
    values = {"actual": actual, "expected": expected}
    if actual["frames"] is None:
        return _missing("ffprobe returned no decoded frame count", values, evidence)
    if (any(actual[key] != expected[key] for key in ("width", "height", "frames"))
            or (rate is not None and rate != expected_rate)):
        return _measured(False, values, evidence)
    timestamps = video.get("timestamps", [])
    try:
        ticks_per_frame = 1 / (expected_rate * Fraction(video.get("time_base", "0/0")))
        if len(timestamps) != actual["frames"] or any(pts is None for pts in timestamps):
            return _missing("ffprobe returned incomplete presentation timestamps", values, evidence)
    except (ValueError, ZeroDivisionError):
        return _missing("ffprobe returned no usable video time base", values, evidence)
    # A muxer can round each PTS to its time base. The distance between two rounded PTS
    # can differ from the exact rational distance by up to one tick.
    origin = timestamps[0]
    maximum_error = max(abs(pts - origin - i * ticks_per_frame) for i, pts in enumerate(timestamps))
    ordered = all(a < b for a, b in zip(timestamps, timestamps[1:]))
    values.update({"maximum_cadence_error_ticks": float(maximum_error), "allowed_cadence_error_ticks": 1})
    if rate is None and len(timestamps) < 2:
        return _missing("a single frame cannot establish cadence without a reported frame rate", values, evidence)
    return _measured(ordered and maximum_error <= 1, values, evidence)


_BORDER_TOLERANCE_PX = 4
_BORDER_CHANNEL_RANGE = 6
_BORDER_SAMPLE_COUNT = 7


def _edge_borders(raw: bytes, width: int, height: int) -> dict[str, int]:
    size = width * height
    planes = [raw[i * size:(i + 1) * size] for i in range(3)]

    def count(lines):
        limits = [(255, 0)] * len(planes)
        measured = 0
        for line in lines:
            values = [plane[line] for plane in planes]
            limits = [(min(low, min(value)), max(high, max(value)))
                      for (low, high), value in zip(limits, values)]
            if any(high - low > _BORDER_CHANNEL_RANGE for low, high in limits):
                break
            measured += 1
        return measured

    return {
        "top": count(slice(y * width, (y + 1) * width) for y in range(height)),
        "bottom": count(slice(y * width, (y + 1) * width) for y in range(height - 1, -1, -1)),
        "left": count(slice(x, size, width) for x in range(width)),
        "right": count(slice(x, size, width) for x in range(width - 1, -1, -1)),
    }


def _picture_sample(path: Path, seconds: Fraction, *, filters: str = "", source: bool = False) -> bytes:
    seek = ["-seek_timestamp", "1", "-noautorotate"] if source else []
    return _local_command(media.FFMPEG, [
        "-nostdin", "-v", "error", *_LOCAL_PROTOCOLS, *seek, "-ss", f"{float(seconds):.9f}", "-i", str(path),
        "-map", "0:v:0", "-frames:v", "1", "-filter_threads", "1",
        "-vf", filters + "scale=flags=neighbor", "-pix_fmt", "yuv444p",
        "-threads", "1", "-f", "rawvideo", "-",
    ], binary=True)


def _picture_rect(raw: bytes, width: int, height: int, rect: Rect) -> bytes:
    x, y, w, h = (rect[key] for key in ("x", "y", "width", "height"))
    plane_size = width * height
    return b"".join(raw[offset + row * width + x:offset + row * width + x + w]
                    for offset in (0, plane_size, 2 * plane_size) for row in range(y, y + h))


def _channel_limits(raw: bytes) -> list[tuple[int, int]]:
    size = len(raw) // 3
    planes = (raw[i:i + size] for i in range(0, len(raw), size))
    return [(min(plane), max(plane)) for plane in planes]


def _merge_channel_limits(a, b):
    return [(min(lo, other_lo), max(hi, other_hi)) for (lo, hi), (other_lo, other_hi) in zip(a, b)]


def _fill_bands(fg: Rect, width: int, height: int) -> dict[str, Rect]:
    x, y, w, h = (fg[key] for key in ("x", "y", "width", "height"))
    # Ignore encoder bleed immediately next to the foreground seam.
    bands = {}
    for edge, start, end, full, horizontal in (
        ("top", 0, y, width, True), ("bottom", y + h, height, width, True),
        ("left", 0, x, height, False), ("right", x + w, width, height, False),
    ):
        if start < end:
            guard = min(_BORDER_TOLERANCE_PX, end - start - 1)
            if edge in ("top", "left"):
                end -= guard
            else:
                start += guard
            bands[edge] = ({"x": 0, "y": start, "width": full, "height": end - start} if horizontal
                           else {"x": start, "y": 0, "width": end - start, "height": full})
    return bands


def _matches_flat_reference(expected, actual) -> bool:
    return all(max(expected_hi, actual_hi) - min(expected_lo, actual_lo) <= _BORDER_CHANNEL_RANGE
               for (expected_lo, expected_hi), (actual_lo, actual_hi) in zip(expected, actual))


def _check_flat_source(timeline, sources, frames, constant, extrema, fg, *, check_foreground, evidence):
    width, height = timeline["canvas"]["width"], timeline["canvas"]["height"]
    speaker = timeline["layout"]["speaker"]
    preset = REEL_PRESETS[timeline["preset"]]
    background = ScreenBackground(preset.medium_background_blur_sigma, preset.medium_background_brightness)
    blur = _medium_background_filter(speaker, width, height, background)
    fit = f"{_crop_filter(speaker)},scale={fg['width']}:{fg['height']}:flags=lanczos"
    for frame, piece, foreground_limits in frames:
        path = sources.get(piece["source"]["source_id"])
        if path is None or "frames" not in piece["source"]:
            raise VPError("data", "original picture is required to verify constant planned fill")
        rate = timeline["inputs"]["sources"][piece["source"]["source_id"]]["frame_rate"]
        # Seek between frames so timestamp rounding cannot skip the intended picture.
        seconds = max(Fraction(0), source_picture_time(piece, timeline, frame)
                      - Fraction(rate["den"], 2 * rate["num"]))
        reference = f"source picture sampled for flat areas: {path.name}"
        if reference not in evidence:
            evidence.append(reference)
        if constant:
            raw = _picture_sample(path, seconds, filters=blur + ",", source=True)
            if len(raw) != width * height * 3:
                raise VPError("data", "could not decode the expected source fill")
            for edge, rect in constant.items():
                expected = _channel_limits(_picture_rect(raw, width, height, rect))
                if not _matches_flat_reference(expected, extrema[edge]):
                    axis = "height" if edge in ("top", "bottom") else "width"
                    return f"{edge} fill is constant across samples ({rect[axis]} px tested) and differs from source"
        if check_foreground:
            raw = _picture_sample(path, seconds, filters=fit + ",", source=True)
            if len(raw) != fg["width"] * fg["height"] * 3:
                raise VPError("data", "could not decode the expected source foreground")
            if not _matches_flat_reference(_channel_limits(raw), foreground_limits):
                return "uniform foreground differs from source"
    return None


def _active_picture(final: Path, timeline: Timeline, sources: Mapping[str, Path] | None = None) -> QACheck:
    values = {"tolerance_px": _BORDER_TOLERANCE_PX, "max_channel_range": _BORDER_CHANNEL_RANGE, "samples": []}
    if timeline["kind"] != "reel":
        return _missing("active-picture sampling applies to reels", values)
    # Sample throughout the body, with cards and intentional picture fades excluded.
    runs = []
    for piece in (row for row in timeline["elements"] if row["kind"] == "piece"):
        frames = piece["output"]["frames"]
        start = frames["start"]
        fades = sorted((event["output"]["frames"] for event in piece["events"]
                        if event["kind"] in ("picture_fade_in", "picture_fade_out")),
                       key=lambda bounds: bounds["start"])
        for fade in [*fades, {"start": frames["end"], "end": frames["end"]}]:
            if start < fade["start"]:
                runs.append((start, fade["start"] - start, piece))
            start = max(start, fade["end"])
    total = sum(length for _, length, _ in runs)
    fps = Fraction(timeline["frame_rate"]["num"], timeline["frame_rate"]["den"])
    width, height = timeline["canvas"]["width"], timeline["canvas"]["height"]
    fills = timeline.get("speaker_background", False)
    try:
        fg = (medium_foreground(timeline["layout"]["speaker"], width, height) if fills
              else {"x": 0, "y": 0, "width": width, "height": height})
    except VPError as exc:
        return _missing(exc.msg, values)
    bands = _fill_bands(fg, width, height) if fills else {}
    extrema = {}
    reference_frames = []
    if bands:
        values.update(foreground=fg, fill_constant_channel_range=0,
                      fill_reference_channel_tolerance=_BORDER_CHANNEL_RANGE,
                      fill_foreground_seam_exclusion_px=_BORDER_TOLERANCE_PX)
    count = min(total, _BORDER_SAMPLE_COUNT)
    try:
        for i in range(count):
            pos = (2 * i + 1) * total // (2 * count)
            for start, length, piece in runs:
                if pos < length:
                    frame = start + pos
                    break
                pos -= length
            raw = _picture_sample(final, frame / fps)
            if len(raw) != width * height * 3:
                return _missing(f"could not decode body frame {frame} at canvas size", values)
            pixels = _picture_rect(raw, width, height, fg) if bands else raw
            borders = _edge_borders(pixels, fg["width"], fg["height"])
            if borders["top"] != fg["height"]:
                values["samples"].append({"frame": frame, "border_px": borders})
            if bands:
                for edge, rect in bands.items():
                    limits = _channel_limits(_picture_rect(raw, width, height, rect))
                    extrema[edge] = _merge_channel_limits(extrema.get(edge, limits), limits)
                reference_frames.append((frame, piece, _channel_limits(pixels)))
    except VPError as exc:
        return _missing(exc.msg, values)
    samples = values["samples"]
    maximum = {edge: max((sample["border_px"][edge] for sample in samples), default=0)
               for edge in ("top", "bottom", "left", "right")}
    values["max_border_px"] = maximum
    evidence = [f"decoded body frames: {final.name}"]
    excessive = ", ".join(f"{edge} {pixels} px" for edge, pixels in maximum.items()
                          if pixels > _BORDER_TOLERANCE_PX)
    if excessive:
        return _measured(False, values, evidence, f"uniform border exceeds {_BORDER_TOLERANCE_PX} px: {excessive}")
    if not reference_frames:
        if not samples:
            return _missing("no body sample has enough picture detail to measure borders", values)
        return _measured(True, values, evidence)

    ranges = {edge: max(high - low for low, high in limits) for edge, limits in extrema.items()}
    values["fill_max_channel_range"] = ranges
    constant = {edge: bands[edge] for edge, spread in ranges.items() if spread == 0}
    if constant or not samples:
        try:
            mismatch = _check_flat_source(timeline, sources or {}, reference_frames, constant, extrema, fg,
                                          check_foreground=not samples, evidence=evidence)
        except VPError as exc:
            return _missing(exc.msg, values, evidence)
        if mismatch:
            return _measured(False, values, evidence, mismatch)
    return _measured(True, values, evidence)


def _card_similarity(final: Path, reference: Path, start: int, count: int, before: int, after: int) -> list[float]:
    # Match by decoded frame ordinal. Reset PTS to consecutive ticks so offsets and container
    # rounding cannot influence the comparison. Dash sends per-frame stats to stdout.
    filt = (f"[0:v:0]trim=start_frame={start - before}:end_frame={start + count + after},settb=1/1000,setpts=N,"
            "format=yuv420p[final];"
            f"[1:v:0]trim=end_frame={count},"
            f"tpad=start={before}:stop={after}:start_mode=clone:stop_mode=clone,"
            "settb=1/1000,setpts=N,format=yuv420p[ref];"
            "[final][ref]ssim=stats_file=-:shortest=1")
    stats = _local_command(media.FFMPEG, [
        "-hide_banner", "-nostdin", "-v", "error",
        *_LOCAL_PROTOCOLS, "-i", str(final), *_LOCAL_PROTOCOLS, "-i", str(reference),
        "-filter_complex", filt, "-an", "-f", "null", "-",
    ], timeout=3600)
    return [float(value) for value in re.findall(r"\bAll:([0-9.]+)", stats)]


def _card_check(final: Path, reference: Path | None, element: dict | None, available: int | None,
                policy: ProductionQAPolicy, role: str) -> tuple[QACheck, QACheck]:
    if element is None:
        return _missing(f"the timeline has no {role} card"), _missing(f"the timeline has no {role} card")
    if reference is None or not reference.is_file():
        reason = f"no rendered reference for card {element['id']}"
        return _missing(reason), _missing(reason)
    if available is None:
        return _missing("no decoded final frame count"), _missing("no decoded final frame count")
    span = element["output"]["frames"]
    count = span["end"] - span["start"]
    expected = (policy.intro_frames if role == "intro" else policy.outro_frames) or count
    dwell_min = (policy.intro_readable_dwell_frames if role == "intro"
                 else policy.outro_readable_dwell_frames)
    try:
        reference_stream = _video_probe(reference)
        reference_count = _frame_count(reference_stream)
        if reference_count is None:
            return _missing("no decoded reference frame count"), _missing("no decoded reference frame count")
        compared = min(count, reference_count, max(0, available - span["start"]))
        full = compared == count == reference_count
        before = int(full and span["start"] > 0)
        after = int(full and span["end"] < available)
        scores = _card_similarity(final, reference, span["start"], compared, before, after) if compared else []
        if len(scores) != compared + before + after:
            return _missing("FFmpeg returned incomplete card comparisons"), _missing("FFmpeg returned incomplete card comparisons")
    except VPError as exc:
        return _missing(exc.msg), _missing(exc.msg)

    evidence = [f"decoded per-frame SSIM: {final.name} frames {span['start']}..{span['end']} "
                f"(exclusive) against {reference.name} from frame 0"]
    matching = [score >= policy.min_card_ssim for score in scores[before:before + compared]]
    neighbors = {}
    if before:
        neighbors["previous_frame_ssim"] = scores[0]
    if after:
        neighbors["next_frame_ssim"] = scores[-1]
    isolated = all(score < policy.min_card_ssim for score in neighbors.values())
    if neighbors:
        evidence.append("adjacent final frames compared against the closest reference-card frame")
    check = _measured(isolated and sum(matching) == count == expected == reference_count, {
        "matching_frames": sum(matching), "reference_frames": reference_count, "planned_frames": count,
        "expected_frames": expected, "minimum_ssim": policy.min_card_ssim, **neighbors,
    }, evidence)
    card = element["card"]
    reveals = list(card.get("word_reveal_frames", []))
    reveals.extend(point["revealFrame"] for point in card.get("points", []))
    reveals.extend(card[key]["revealFrame"] for key in ("subline", "end_text") if key in card)
    reveal_end = max(reveals, default=0)
    # Count the continuous verified hold ending at the planned card boundary.
    dwell = 0
    if len(matching) == count:
        for matches in reversed(matching[reveal_end:]):
            if not matches:
                break
            dwell += 1
    return check, _measured(dwell >= dwell_min, {
        "dwell_frames": dwell, "minimum_frames": dwell_min, "last_reveal_frame": reveal_end,
    }, [*evidence, "last reveal frame from resolved card copy in the timeline"])


# Analysis resolution for waveform comparisons. Keep all grid offsets exact at this rate.
_PCM_RATE = 4000
_ALIGNMENT_SAMPLES = _PCM_RATE


def _pcm(path: Path, start: Fraction, count: int, rate: int, *, mono: bool, absolute: bool = False) -> array:
    clock = ["-seek_timestamp", "1"] if absolute else []
    args = ["-hide_banner", "-nostdin", "-v", "error", *_LOCAL_PROTOCOLS,
            *clock, "-ss", f"{float(start):.9f}", "-i", str(path),
            "-map", "0:a:0", "-t", f"{count / rate:.9f}", "-ar", str(rate)]
    if mono:
        args += ["-ac", "1"]
    raw = _local_command(media.FFMPEG, [*args, "-f", "f32le", "-c:a", "pcm_f32le", "-"], binary=True)
    data = array("f")
    data.frombytes(raw)
    if sys.byteorder != "little":
        data.byteswap()
    return data


def _clock(samples: int, rate: int) -> Fraction:
    return Fraction(samples, rate)


def _quiet(final: Path, span: tuple[int, int], rate: int, policy: ProductionQAPolicy,
           *, tail: bool) -> QACheck:
    start, end = span
    minimum_ms = policy.quiet_tail_ms if tail else policy.quiet_lead_ms - policy.audio_open_ms
    if minimum_ms == 0:
        return _missing("the policy requires no quiet audio window here")
    count = round(Fraction(minimum_ms * rate, 1000))
    window_start = end - count if tail else start
    values = {"required_quiet_ms": minimum_ms, "peak_limit_dbfs": policy.silence_db}
    if window_start < start or window_start + count > end:
        return _measured(False, values, ["timeline picture bounds"], "body is shorter than the quiet window")
    if count <= 0:
        return _missing("quiet window rounds to zero audio samples", values)
    try:
        probe = _local_command(media.FFPROBE, [
            "-v", "error", *_LOCAL_PROTOCOLS, "-select_streams", "a:0", "-show_entries", "stream=channels",
            "-of", "json", str(final),
        ])
        stream = next(iter(json.loads(probe).get("streams", [])), {})
        channels = stream.get("channels")
        samples = _pcm(final, _clock(window_start, rate), count, rate, mono=False)
        if not channels or len(samples) < count * channels:
            return _missing("could not decode the complete quiet audio window", values)
    except VPError as exc:
        return _missing(exc.msg, values)
    peak = max(map(abs, samples[:count * channels]))
    if not math.isfinite(peak):
        return _missing("decoded PCM peak is not finite", values)
    peak_db = 20 * math.log10(peak) if peak else None
    values.update({"window_start_sample": window_start, "window_end_sample": window_start + count,
                   "peak_dbfs": peak_db})
    return _measured(peak_db is None or peak_db <= policy.silence_db, values,
                     [f"decoded PCM peak across all channels: {final.name}"])


def _correlation(source: array, render: array) -> float | None:
    # Pearson correlation verifies the waveform while allowing a common mastering gain.
    n = len(source)
    if len(render) != n or n < 2:
        return None
    a, b = sum(source), sum(render)
    aa, bb = sum(x * x for x in source) - a * a / n, sum(y * y for y in render) - b * b / n
    if min(aa, bb) <= 0:
        return None
    ab = sum(x * y for x, y in zip(source, render)) - a * b / n
    return max(-1.0, min(1.0, ab / math.sqrt(aa * bb)))


def _word_correlation(source: array, render: array) -> float | None:
    score = _correlation(source, render)
    if score is None and min(render) == max(render) and _correlation(source, source) is not None:
        return 0.0
    return score


def _find_lag(source: Path, final: Path, source_at: Fraction, final_at: Fraction, length: int,
              policy: ProductionQAPolicy, *, absolute: bool = False) -> tuple[int | None, float | None]:
    window = _clock(policy.audio_lag_search_ms, 1000)
    base = max(Fraction(0), final_at - window)
    count = round((final_at + window - base) * _PCM_RATE) + length
    ref = _pcm(source, source_at, length, _PCM_RATE, mono=True, absolute=absolute)
    candidate = _pcm(final, base, count, _PCM_RATE, mono=True)
    scores = []
    for lag in range(-policy.audio_lag_search_ms, policy.audio_lag_search_ms + 1, policy.audio_lag_grid_ms):
        offset = round((final_at - base + _clock(lag, 1000)) * _PCM_RATE)
        if offset >= 0:
            score = _correlation(ref, candidate[offset:offset + length])
            if score is not None:
                scores.append((score, lag))
    if not scores:
        # A fully decoded flat render window contains evidence of lost source audio.
        flat = len(candidate) >= count and candidate and min(candidate) == max(candidate)
        return None, 0.0 if flat and _correlation(ref, ref) is not None else None
    scores.sort(reverse=True)
    best, lag = scores[0]
    if len(scores) > 1 and math.isclose(best, scores[1][0], abs_tol=1e-9):
        return None, best
    return lag, best


def _audio_checks(final: Path, timeline: Timeline, sources: Mapping[str, Path],
                  policy: ProductionQAPolicy) -> tuple[QACheck, QACheck]:
    lag_rows, word_rows = {}, {}
    lag_missing, word_missing, lag_evidence, word_evidence = [], [], [], []
    lag_ok, word_ok = True, True
    rate = timeline["sample_rate"]
    absolute = timeline.get("source_start") is not None
    for element in timeline["elements"]:
        if element["audio"] != "source":
            continue
        eid = element["id"]
        available_words = [word for word in timeline["words"] if word["element_id"] == eid]
        words = sorted(available_words, key=lambda word: word["output"]["samples"]["end"])
        anchors = [word for word in words if word["provenance"] == "matched"]
        src = element["source"]
        source_id = src["source_id"]
        path = sources.get(source_id)
        source_rate = timeline["inputs"]["sources"][source_id]["sample_rate"]
        if path is None or not path.is_file():
            lag_missing.append(f"{eid}: no local source file for {source_id}")
            word_missing.append(f"{eid}: no local source file for {source_id}")
            continue
        if sha256_file(path) != timeline["inputs"]["sources"][source_id]["sha256"]:
            lag_missing.append(f"{eid}: local source digest differs from the compiled input")
            word_missing.append(f"{eid}: local source digest differs from the compiled input")
            continue
        if element["rate"]["num"] != element["rate"]["den"]:
            lag_missing.append(f"{eid}: PCM alignment requires a normal-speed source occurrence")
            word_missing.append(f"{eid}: PCM alignment requires a normal-speed source occurrence")
            continue
        out = element["output"]["samples"]
        if anchors:
            anchor = min(anchors, key=lambda word: word["output"]["samples"]["start"])
            src_start, out_start = anchor["source_samples"]["start"], anchor["output"]["samples"]["start"]
        elif any(event["kind"] == "freeze" for event in element["events"]):
            lag_missing.append(f"{eid}: the held picture has no matched audio anchor")
            word_missing.append(f"{eid}: no matched final word")
            continue
        else:
            src_start, out_start = src["samples"]["start"], out["start"]
        seconds = min(_clock(src["samples"]["end"] - src_start, source_rate), _clock(out["end"] - out_start, rate))
        length = min(_ALIGNMENT_SAMPLES, round(seconds * _PCM_RATE))
        try:
            lag, score = _find_lag(path, final, _clock(src_start, source_rate), _clock(out_start, rate),
                                   length, policy, absolute=absolute) if length >= 2 else (None, None)
        except VPError as exc:
            lag_missing.append(f"{eid}: {exc.msg}")
            word_missing.append(f"{eid}: {exc.msg}")
            continue
        lag_rows[eid] = {"lag_ms": lag, "correlation": score}
        evidence = f"normalized decoded waveform correlation: {path.name} → {final.name}, occurrence {eid}"
        if lag is None and score == 0:
            lag_ok = False
            lag_evidence.append(evidence)
            word_missing.append(f"{eid}: rendered audio is flat across the searched alignment window")
            continue
        if lag is None or score is None:
            lag_missing.append(f"{eid}: no unambiguous PCM alignment")
            word_missing.append(f"{eid}: no unambiguous PCM alignment")
            continue
        lag_evidence.append(evidence)
        trustworthy = score >= policy.min_audio_correlation
        lag_ok &= (trustworthy and abs(lag) <= policy.max_audio_lag_ms)
        if not words or words[-1]["provenance"] != "matched":
            word_missing.append(f"{eid}: no matched final word")
            continue
        if not trustworthy:
            word_missing.append(f"{eid}: the PCM alignment is below the required correlation")
            continue

        word = words[-1]
        sw, ow = word["source_samples"], word["output"]["samples"]
        start = _clock(ow["start"], rate) + _clock(lag, 1000)
        end = _clock(ow["end"], rate) + _clock(lag, 1000)
        planned_length = round(_clock(ow["end"] - ow["start"], rate) * _PCM_RATE)
        source_length = round(_clock(sw["end"] - sw["start"], source_rate) * _PCM_RATE)
        # Compare the terminal half separately so missing final phonemes cannot hide in a good
        # full-word score. The requested and measured span anchors remain visible in the record.
        bounded = _clock(out["start"], rate) <= start < end <= _clock(out["end"], rate)
        fade_windows = [(event["kind"], event["output"]["samples"]) for event in element["events"]
                        if event["kind"] in ("audio_fade_in", "audio_fade_out", "silence")]

        def overlaps(fade, left):
            return max(_clock(fade["start"], rate), left) < min(_clock(fade["end"], rate), end)

        unfaded = bounded and not any(overlaps(fade, start) for _, fade in fade_windows)
        # An opening ramp can overlap the onset of a one-word piece. Its final
        # phonemes must still sit in full-gain audio and correlate independently.
        safe = bounded and not any(overlaps(fade, (start + end) / 2 if kind == "audio_fade_in" and
                                            timeline["kind"] == "reel" else start)
                                   for kind, fade in fade_windows)
        whole = terminal = None
        if safe and min(planned_length, source_length) >= 4:
            try:
                ref = _pcm(path, _clock(sw["start"], source_rate), source_length, _PCM_RATE, mono=True, absolute=absolute)
                take = _pcm(final, start, planned_length, _PCM_RATE, mono=True)
            except VPError as exc:
                word_missing.append(f"{eid}: {exc.msg}")
                continue
            # Independent quantization of source and output clocks can differ by one analysis sample.
            if abs(planned_length - source_length) <= 1:
                common = min(planned_length, source_length)
                if len(ref) >= common and len(take) >= common:
                    whole = _word_correlation(ref[:common], take[:common])
                    terminal = _word_correlation(ref[common // 2:common], take[common // 2:common])
        word_rows[eid] = {"word_id": word["word_id"], "lag_ms": lag, "within_unfaded_body": unfaded,
                          "terminal_unfaded": safe,
                          "source_samples": sw, "planned_output_samples": ow,
                          "correlation": whole, "terminal_correlation": terminal}
        if safe and (whole is None or terminal is None):
            word_missing.append(f"{eid}: incomplete or unusable last-word PCM")
            continue
        if not safe or min(whole, terminal) < policy.min_audio_correlation:
            word_ok = False
        word_evidence.append(f"matched word, fade spans and decoded PCM: {path.name} → {final.name}, occurrence {eid}")

    def combine(rows, missing, evidence, passed, values):
        values = {"occurrences": rows, **values}
        if evidence and not passed:
            return _measured(False, values, evidence, "; ".join(missing) or None)
        if missing or not evidence:
            return _missing("; ".join(missing) or "no source audio occurrences to compare", values, evidence)
        return _measured(True, values, evidence)

    return (
        combine(lag_rows, lag_missing, lag_evidence, lag_ok,
                {"maximum_lag_ms": policy.max_audio_lag_ms, "search_ms": policy.audio_lag_search_ms,
                 "grid_ms": policy.audio_lag_grid_ms, "minimum_correlation": policy.min_audio_correlation}),
        combine(word_rows, word_missing, word_evidence, word_ok,
                {"minimum_correlation": policy.min_audio_correlation}),
    )


def _audio_levels(path: Path, policy: ProductionQAPolicy) -> tuple[QACheck, QACheck]:
    try:
        # loudnorm reports its input measurements on stderr at info level.
        output = _local_command(media.FFMPEG, [
            "-hide_banner", "-nostdin", "-v", "info", *_LOCAL_PROTOCOLS, "-i", str(path),
            "-map", "0:a:0", "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-",
        ], timeout=3600, stderr_output=True)
        match = media._LOUDNORM_JSON.search(output)
        if match is None:
            raise VPError("data", "FFmpeg returned no loudnorm measurements")
        measured = json.loads(match.group(0))
        integrated, peak = float(measured["input_i"]), float(measured["input_tp"])
    except (VPError, KeyError, ValueError) as exc:
        return _missing(f"loudness measurement unavailable: {exc}"), _missing(f"true peak measurement unavailable: {exc}")
    evidence = [f"FFmpeg loudnorm input measurements on the encoded file: {path.name}"]
    loudness_min = Fraction(str(policy.target_lufs)) - Fraction(str(policy.lufs_tolerance))
    loudness_max = Fraction(str(policy.target_lufs)) + Fraction(str(policy.lufs_tolerance))
    loudness = _measured(math.isfinite(integrated) and loudness_min <= Fraction(str(integrated)) <= loudness_max, {
        "lufs": integrated if math.isfinite(integrated) else None, "target_lufs": policy.target_lufs,
        "tolerance_lu": policy.lufs_tolerance,
    }, evidence)
    true_peak = _measured(peak <= policy.max_dbtp, {
        "dbtp": peak if math.isfinite(peak) else None, "maximum_dbtp": policy.max_dbtp,
        "digital_silence": peak == -math.inf,
    }, evidence)
    return loudness, true_peak


_CHECKS = (
    "picture_format", "intro_frames", "outro_frames", "intro_text_dwell", "outro_text_dwell",
    "quiet_lead_in", "quiet_tail", "last_word_protection", "audio_offset",
    "loudness", "true_peak", "effect_caption_clearance", "independent_speech_recognition",
)


def measure_production(
    final: Path, timeline_path: Path, policy: ProductionQAPolicy, *,
    sources: Mapping[str, Path] | None = None, card_references: Mapping[str, Path] | None = None,
    reel_body: RenderReelBody | None = None, effect_geometry: EffectGeometry = EffectGeometry(),
) -> QARecord:
    """Measure final media against its timeline; source/card evidence is keyed by source/element ID."""
    final = Path(final)
    raw = Path(timeline_path).read_bytes()
    try:
        timeline: Timeline = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise VPError("contract", f"timeline is not valid JSON: {exc}") from exc
    if errors := validate_timeline(timeline):
        raise VPError("contract", "\n".join(errors), errors=errors)
    checks = {name: _missing("no finished local media file") for name in _CHECKS}
    if timeline["kind"] == "reel":
        checks["active_picture"] = _missing("no finished local media file")
    record: QARecord = {
        "version": 1, "item": timeline["item"], "kind": timeline["kind"], "policy_revision": policy.revision,
        "timeline_sha256": sha256_bytes(raw), "media_sha256": None, "checks": checks,
    }
    if final.is_file():
        record["media_sha256"] = sha256_file(final)
        _measure_existing(final, timeline, policy, sources or {}, card_references or {}, reel_body,
                          effect_geometry, checks)
    if errors := validate_qa_record(record):
        raise VPError("contract", "\n".join(errors), errors=errors)
    return record


def _measure_existing(final, timeline, policy, sources, cards, reel_body, geometry, checks):
    try:
        video = _video_probe(final, cadence=True)
        checks["picture_format"] = _format_check(video, timeline, final)
    except VPError as exc:
        video = None
        checks["picture_format"] = _missing(exc.msg)
    by_kind = {element["kind"]: element for element in timeline["elements"]}
    reel = timeline["kind"] == "reel"
    if reel:
        checks["active_picture"] = _active_picture(final, timeline, sources)
    for role, kind in (("intro", "intro" if reel else "hook"), ("outro", "thought" if reel else "outro")):
        element = by_kind.get(kind)
        reference = cards.get(element["id"]) if element else None
        checks[f"{role}_frames"], checks[f"{role}_text_dwell"] = _card_check(
            final, reference, element, _frame_count(video), policy, role)
    speech = [element for element in timeline["elements"] if element["audio"] == "source"]
    if speech:
        rate = timeline["sample_rate"]
        first, last = speech[0], speech[-1]
        start, end = first["output"]["samples"]["start"], last["output"]["samples"]["end"]
        speech_spans = [event["output"]["samples"] for element in speech for event in element["events"]
                        if event["kind"] == "speech"]
        speech_spans.extend(word["output"]["samples"] for word in timeline["words"]
                            if word["provenance"] == "matched")
        for key, tail in (("quiet_lead_in", False), ("quiet_tail", True)):
            if not speech_spans:
                checks[key] = _missing("timeline has no measured speech bounds for the quiet handle")
                continue
            actual = (end - max(s["end"] for s in speech_spans) if tail
                      else min(s["start"] for s in speech_spans) - start)
            minimum = policy.tail_picture_ms if tail else policy.quiet_lead_ms
            fps = Fraction(timeline["frame_rate"]["num"], timeline["frame_rate"]["den"])
            frames = round(Fraction(actual, rate) * fps)
            required = round(Fraction(minimum, 1000) * fps)
            observed = _quiet(final, (start, end), rate, policy, tail=tail)
            values = {**observed["values"], "planned_picture_handle_frames": frames,
                      "minimum_picture_handle_frames": required}
            if frames < required:
                checks[key] = _measured(False, values, [*observed["evidence"], "timeline speech and picture bounds"],
                                        "picture handle is shorter than required")
            else:
                observed["values"] = values
                checks[key] = observed
    else:
        for key in ("quiet_lead_in", "quiet_tail"):
            checks[key] = _missing("timeline has no source audio occurrence")
    checks["audio_offset"], checks["last_word_protection"] = _audio_checks(final, timeline, sources, policy)
    checks["loudness"], checks["true_peak"] = _audio_levels(final, policy)
    checks["effect_caption_clearance"] = _effect_clearance(timeline, reel_body, geometry)
    checks["independent_speech_recognition"] = _missing("no independent local render speech recognition was supplied")


def _effect_clearance(timeline, reel_body, geometry):
    if timeline["kind"] != "reel" or not timeline["effects"]:
        return _missing("timeline has no reel effects to measure")
    if reel_body is None:
        return _missing("no delivered reel-body props for measuring animation clearance")
    bounds = inspect_effect_bounds(reel_body, geometry)
    evidence = ["continuous animation geometry from delivered reel-body props and protected areas"]
    values = {"minimum_gap_px": bounds.minimum_caption_gap_px, "required_gap_px": bounds.required_caption_gap_px}
    if bounds.errors:
        values["errors"] = list(bounds.errors)
        # A missing geometry setting is absent evidence. A computed violation is evidence of failure.
        if all(error.startswith("geometry.") and "supply a " in error for error in bounds.errors):
            return _missing("required animation geometry is missing", values, evidence)
        return _measured(False, values, evidence)
    pieces = [element for element in timeline["elements"] if element["kind"] == "piece"]
    start = pieces[0]["output"]["frames"]["start"]
    end = pieces[-1]["output"]["frames"]["end"]
    rebased = [{**row["props"], "startFrame": row["props"]["startFrame"] - start,
                "endFrame": row["props"]["endFrame"] - start} for row in timeline["effects"]]
    actual = sorted(reel_body["effects"], key=lambda effect: effect["type"])
    expected = sorted(rebased, key=lambda effect: effect["type"])
    fps = timeline["frame_rate"]["num"] / timeline["frame_rate"]["den"]
    if ((reel_body["width"], reel_body["height"]) != (timeline["canvas"]["width"], timeline["canvas"]["height"])
            or reel_body["durationInFrames"] != end - start or actual != expected
            or reel_body["fps"] != fps or reel_body["layout"] != timeline["layout"]["mode"]
            or reel_body["captionStyle"]["yFrac"] != timeline["caption_y_frac"]
            or bool(reel_body["captions"]) != bool(timeline["captions"])):
        return _measured(False, {}, [*evidence, "compiled reel timeline"],
                         "reel-body timing or geometry differs from the timeline")
    if bounds.minimum_caption_gap_px is None:
        return _missing("no caption band and visible effect envelope to compare", evidence=evidence)
    return _measured(True, values, evidence)


def master_audio(pre_master: Path, out: Path, policy: ProductionQAPolicy, *, sample_rate: int) -> Path:
    """Copy video, master audio from the same input on every attempt, and publish locally on pass."""
    pre_master, out = Path(pre_master), Path(out)
    if pre_master.resolve() == out.resolve() or sample_rate <= 0:
        raise VPError("data", "mastering requires a separate destination and a positive audio sample rate")
    initial, _ = _audio_levels(pre_master, policy)
    measured = initial["values"].get("lufs")
    if measured is None:
        raise VPError("data", "cannot master audio without a finite pre-master loudness", check=initial)
    gain = policy.target_lufs - measured
    ceiling = policy.max_dbtp
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".vp-master-", dir=out.parent) as directory:
        candidate = Path(directory) / "candidate.mp4"
        for attempt in range(1, policy.mastering_retries + 2):
            limit = max(0.0625, 10 ** (ceiling / 20))  # FFmpeg alimiter's minimum supported limit.
            filters = (f"volume={gain:.8f}dB,aresample={policy.limiter_sample_rate},"
                       f"alimiter=limit={limit:.8f}:level=false:latency=true,aresample={sample_rate}")
            _local_command(media.FFMPEG, [
                "-hide_banner", "-nostdin", "-v", "error", "-y", *_LOCAL_PROTOCOLS, "-i", str(pre_master),
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                "-af", filters, *media.AUDIO_ENC, "-movflags", "+faststart", str(candidate),
            ], timeout=3600)
            loudness, peak = _audio_levels(candidate, policy)
            if loudness["status"] == peak["status"] == "passed":
                candidate.replace(out)
                return out
            measured, dbtp = loudness["values"].get("lufs"), peak["values"].get("dbtp")
            if measured is None or dbtp is None:
                raise VPError("data", "encoded master has no finite loudness or true peak",
                              loudness=loudness, true_peak=peak, attempts=attempt)
            gain += policy.target_lufs - measured
            ceiling -= max(0, dbtp - policy.max_dbtp)
        raise VPError("data", "audio mastering exhausted its retries without passing final-file QA",
                      loudness=loudness, true_peak=peak, attempts=attempt)
