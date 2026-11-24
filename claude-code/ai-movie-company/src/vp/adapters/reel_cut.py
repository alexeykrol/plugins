"""Render the prepared reel body with a lossless audio clock for downstream QA."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from math import ceil, lcm
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from ..analysis.edit_clock import EditClock
from ..builders.contracts import (
    MediaInput, OutputSpan, PresentationTime, Ratio, Rect, ReelSourceStart, ReelTimeline, Span,
    TimelineElement, TimelineInputs,
    validate_timeline,
)
from ..builders.presets import REEL_PRESETS, RequiredReel810Geometry
from ..core.errors import VPError
from . import media

_PROBE_TIMEOUT_S = 3600  # Match the media adapter's full local render timeout.


@dataclass(frozen=True)
class ReelSource:
    media: MediaInput
    width: int
    height: int
    frames: int
    samples: int
    source_start: ReelSourceStart | None = None

    @property
    def frames_per_ms(self) -> Fraction:
        return _ratio(self.media["frame_rate"]) / 1000

    @property
    def picture_start_ms(self) -> Fraction:
        return _ratio(self.source_start["picture"]) * 1000 if self.source_start else Fraction(0)

    @property
    def aligned_start_ms(self) -> Fraction:
        return _ratio(self.source_start["aligned"]) * 1000 if self.source_start else Fraction(0)

    @property
    def sound_end_ms(self) -> Fraction:
        start = _ratio(self.source_start["sound"]) * 1000 if self.source_start else 0
        return start + Fraction(self.samples * 1000, self.media["sample_rate"])

    @property
    def picture_end_ms(self) -> Fraction:
        return self.picture_start_ms + self.frames / self.frames_per_ms

    @property
    def duration_ms(self) -> int:
        return int(min(self.picture_end_ms, self.sound_end_ms))


@dataclass(frozen=True)
class ScreenBackground:
    blur_sigma: float
    brightness: float

    def __post_init__(self):
        if not (0 < self.blur_sigma < float("inf") and -1 <= self.brightness <= 0):
            raise _data("screen_background", "blur sigma must be positive and brightness must be between -1 and 0")


@dataclass(frozen=True)
class MeasuredPiece:
    """Decoded body partition with its planned source and envelope coordinates."""

    element_id: str
    body: OutputSpan
    source_frames: Span
    source_samples: Span
    speech_samples: Span
    source_audio_samples: Span
    silence_samples: tuple[Span, ...]


@dataclass(frozen=True)
class ReelCutResult:
    path: Path
    frames: int
    samples: int
    pieces: tuple[MeasuredPiece, ...]


def _data(path: str, message: str) -> VPError:
    return VPError("data", f"{path}: {message}", path=path)


def _ratio(value: Ratio | PresentationTime) -> Fraction:
    return Fraction(value["num"], value["den"])


def _probe(path: Path, *options: str) -> dict:
    try:
        proc = subprocess.run(
            [media.FFPROBE, "-v", "error", "-protocol_whitelist", "file,pipe", *options,
             "-of", "json", str(path)], capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise VPError("internal", "ffprobe not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise _data(str(path), "ffprobe timed out") from exc
    if proc.returncode:
        raise _data(str(path), f"ffprobe failed: {proc.stderr.strip()[-300:]}")
    return json.loads(proc.stdout)


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _recording_path(path: str | Path) -> Path:
    try:
        local = Path(path).resolve(strict=True)
    except FileNotFoundError as exc:
        raise _data(str(path), "recording is missing") from exc
    if not local.is_file():
        raise _data(str(path), "expected a local recording file")
    return local


def _recording_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _streams(path: Path) -> tuple[dict, dict]:
    streams = _probe(path, "-show_streams").get("streams", [])
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    if video is None or audio is None:
        raise _data(str(path), "reel source needs picture and sound")
    return video, audio


def _source_streams(path: Path) -> tuple[dict, dict, Fraction, int]:
    video, audio = _streams(path)
    try:
        rate = Fraction(video["avg_frame_rate"])
        sample_rate = int(audio["sample_rate"])
        if rate <= 0 or sample_rate <= 0 or any(
            Fraction(stream["time_base"]) <= 0 for stream in (video, audio)
        ):
            raise ValueError
        if any(int(stream["start_pts"]) < 0 for stream in (video, audio)):
            raise _data(str(path), "picture or sound starts before time zero")
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise _data(str(path), "could not measure the start and rate of both picture and sound") from exc
    return video, audio, rate, sample_rate


def _source_start(video: dict, audio: dict) -> ReelSourceStart | None:
    picture, sound = (int(stream["start_pts"]) * Fraction(stream["time_base"]) for stream in (video, audio))
    if not (picture or sound):
        return None

    def seconds(value: Fraction) -> PresentationTime:
        return {"num": value.numerator, "den": value.denominator}

    return {"picture": seconds(picture), "sound": seconds(sound), "aligned": seconds(max(picture, sound))}


def probe_reel_source(path: str | Path) -> ReelSource:
    """Measure a local CFR recording and bind its native clocks to its bytes."""
    local = _recording_path(path)
    before = _signature(local)
    video, audio, rate, sample_rate = _source_streams(local)
    packets = _probe(local, "-show_packets",
                     "-show_entries", "packet=stream_index,pts,duration").get("packets", [])
    try:
        stamps = sorted(int(p["pts"]) for p in packets if p["stream_index"] == video["index"])
        audio_packets = [(int(p["pts"]), int(p["duration"]))
                         for p in packets if p["stream_index"] == audio["index"]]
        step = 1 / (rate * Fraction(video["time_base"]))
        samples = int(Fraction(audio["time_base"]) * int(audio["duration_ts"]) * sample_rate)
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise _data(str(local), "could not measure the native frame count or audio samples") from exc
    # A coarse container timebase can round each CFR timestamp by half a tick.
    origin = int(video["start_pts"])
    if (not stamps or step < 1 or any(abs(pts - origin - i * step) > Fraction(1, 2)
                                     for i, pts in enumerate(stamps))):
        raise _data(str(local), "variable-frame-rate source needs local CFR normalization")
    if (samples <= 0 or not audio_packets or any(
        stamp + length != following for (stamp, length), (following, _) in zip(audio_packets, audio_packets[1:])
    )):
        raise _data(str(local), "source needs a continuous audio sample clock")
    starts = _source_start(video, audio)
    if starts:
        video_end = _ratio(starts["picture"]) + len(stamps) / rate
        audio_end = _ratio(starts["sound"]) + Fraction(samples, sample_rate)
        if _ratio(starts["aligned"]) >= min(video_end, audio_end):
            raise _data(str(local), "picture and sound do not overlap")
    digest = _recording_digest(local)
    if _signature(local) != before:
        raise _data(str(local), "recording changed while it was being measured")
    return ReelSource({"sha256": digest, "frame_rate": {"num": rate.numerator, "den": rate.denominator},
                       "sample_rate": sample_rate},
                      int(video["width"]), int(video["height"]), len(stamps), samples, starts)


def _count(path: Path, rate: Ratio, sample_rate: int, canvas: dict) -> tuple[int, int]:
    result = _probe(path, "-show_frames", "-show_streams", "-show_entries",
                    "stream=codec_type,time_base,width,height,sample_rate:"
                    "frame=media_type,best_effort_timestamp,nb_samples")
    streams = {stream["codec_type"]: stream for stream in result["streams"]}
    video, audio = streams["video"], streams["audio"]
    if (video["width"], video["height"], int(audio["sample_rate"])) != (
        canvas["width"], canvas["height"], sample_rate
    ):
        raise _data(str(path), "decoded canvas or sample rate disagrees with the timeline")
    video_tick = Fraction(video["time_base"])
    audio_tick = Fraction(audio["time_base"])
    fps = _ratio(rate)
    frames = samples = 0
    for frame in result["frames"]:
        stamp = int(frame["best_effort_timestamp"])
        if frame["media_type"] == "video":
            if stamp * video_tick * fps != frames:
                raise _data(str(path), f"unexpected picture timestamp at frame {frames}")
            frames += 1
        elif frame["media_type"] == "audio":
            if stamp * audio_tick * sample_rate != samples:
                raise _data(str(path), f"unexpected audio timestamp at sample {samples}")
            samples += int(frame["nb_samples"])
    return frames, samples


def _bounds(span: Span, origin: int = 0) -> tuple[int, int]:
    return span["start"] - origin, span["end"] - origin


def _size(span: Span) -> int:
    return span["end"] - span["start"]


def _event_spans(piece: TimelineElement, kind: str, axis: str) -> list[Span]:
    return [event["output"][axis] for event in piece["events"] if event["kind"] == kind]


def _crop_filter(rect: Rect) -> str:
    return f"crop={rect['width']}:{rect['height']}:{rect['x']}:{rect['y']}:exact=1"


def _fill(width: int, height: int) -> str:
    return (f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop={width}:{height},setsar=1")


def medium_foreground(speaker: Rect, width: int, height: int) -> Rect:
    """Fit the constraining axis and center the even-sized foreground."""
    scale = min(Fraction(width, speaker["width"]), Fraction(height, speaker["height"]))
    w, h = (round(speaker[axis] * scale) // 2 * 2 for axis in ("width", "height"))
    if min(w, h) < 2:
        raise _data("layout.speaker", "the fitted speaker rectangle is smaller than two pixels")
    # Match the YUV420 overlay's chroma-aligned coordinates.
    return {"x": (width - w) // 4 * 2, "y": (height - h) // 4 * 2, "width": w, "height": h}


def _medium_background_filter(speaker: Rect, width: int, height: int, background: ScreenBackground) -> str:
    scale = min(Fraction(speaker["width"], width), Fraction(speaker["height"], height))
    w, h = (max(2, round(axis * scale) // 2 * 2) for axis in (width, height))
    rect = {"x": speaker["x"] + (speaker["width"] - w) // 2,
            "y": speaker["y"] + (speaker["height"] - h) // 2, "width": w, "height": h}
    return (f"{_crop_filter(rect)},scale={width}:{height},setsar=1,"
            f"gblur=sigma={background.blur_sigma},eq=brightness={background.brightness}")


def _composite(graph: list[str], clip: str, output: str, speaker: Rect, screen: Rect | None,
               width: int, height: int, speaker_height: int, tolerance: int,
               divider: int, background: ScreenBackground | None, fill_speaker: bool) -> None:
    if screen is None:
        crop = _crop_filter(speaker)
        if fill_speaker:
            if background is None:
                raise _data("screen_background", "the planned speaker background needs blur and brightness settings")
            fit = medium_foreground(speaker, width, height)
            blur = _medium_background_filter(speaker, width, height, background)
            graph.extend((
                f"[{clip}]split=2[{output}bg][{output}speaker]",
                f"[{output}bg]{blur}[{output}blur]",
                f"[{output}speaker]{crop},scale={fit['width']}:{fit['height']}:flags=lanczos,setsar=1[{output}fg]",
                f"[{output}blur][{output}fg]overlay={fit['x']}:{fit['y']}[{output}]",
            ))
        else:
            graph.append(f"[{clip}]{crop},{_fill(width, height)}[{output}]")
        return
    band = height - speaker_height - divider
    graph.append(f"[{clip}]split=2[{output}speaker][{output}screen]")
    graph.append(f"[{output}speaker]{_crop_filter(speaker)},{_fill(width, speaker_height)},"
                 f"pad={width}:{speaker_height + divider}:0:0:color=yellow[{output}top]")
    crop = f"[{output}screen]{_crop_filter(screen)}"
    fitted_height = Fraction(screen["height"] * width, screen["width"])
    if abs(fitted_height - band) <= tolerance:
        graph.append(f"{crop},scale={width}:{band},setsar=1[{output}bottom]")
    else:
        if background is None or background.brightness == 0:
            raise _data("screen_background", "supply blur sigma and negative brightness for a fitted screen")
        graph.extend((
            f"{crop},split=2[{output}bg][{output}fg]",
            f"[{output}bg]{_fill(width, band)},gblur=sigma={background.blur_sigma},"
            f"eq=brightness={background.brightness}[{output}blur]",
            f"[{output}fg]scale={width}:{band}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2,setsar=1[{output}fit]",
            f"[{output}blur][{output}fit]overlay=(W-w)/2:(H-h)/2[{output}bottom]",
        ))
    graph.append(f"[{output}top][{output}bottom]vstack[{output}]")


def _join_video(graph: list[str], labels: Sequence[str], rate: Ratio, destination: str) -> None:
    stack = "".join(f"[{label}]" for label in labels)
    join = f"concat=n={len(labels)}:v=1:a=0," if len(labels) > 1 else ""
    graph.append(f"{stack}{join}settb=1/{rate['num']},setpts=N*{rate['den']}[{destination}]")


def _first_live_time(piece: TimelineElement, timeline: ReelTimeline, frame: int, fps: Fraction) -> Fraction:
    native_rate = timeline["inputs"]["sources"][piece["source"]["source_id"]]["sample_rate"]
    first_sound = (_event_spans(piece, "audio_fade_in", "samples")
                   or _event_spans(piece, "speech", "samples"))[0]["start"]
    return (Fraction(piece["source"]["samples"]["start"], native_rate)
            + Fraction(frame, fps) - Fraction(first_sound, timeline["sample_rate"]))


def source_picture_time(piece: TimelineElement, timeline: ReelTimeline, frame: int) -> Fraction:
    """Presentation time of the source picture picked for an output frame."""
    src_fps = _ratio(timeline["inputs"]["sources"][piece["source"]["source_id"]]["frame_rate"])
    origin = _ratio(timeline["source_start"]["picture"]) if "source_start" in timeline else Fraction(0)
    start, end = _bounds(piece["source"]["frames"])
    first, last = _bounds(piece["output"]["frames"])
    for hold in (event for event in piece["events"] if event["kind"] == "freeze"):
        a, b = _bounds(hold["output"]["frames"])
        if a <= frame < b:
            return origin + Fraction(hold["source_frame"], src_fps)
        if a == first:
            first = b
        else:
            last = a
    if "source_start" in timeline:
        presentation = _first_live_time(piece, timeline, frame, _ratio(timeline["frame_rate"]))
        native = int((presentation - origin) * src_fps // 1)
    else:
        # The legacy conformer rounds native timestamps to the nearest output tick.
        native = start + ceil(Fraction(2 * (frame - first) + 1, 2) * (end - start) / (last - first)) - 1
    return origin + Fraction(min(end - 1, max(start, native)), src_fps)


def _video_graph(graph: list[str], piece: TimelineElement, timeline: ReelTimeline,
                 source_rate: Ratio, split_810: RequiredReel810Geometry | None,
                 background: ScreenBackground | None, video: dict) -> None:
    rate = timeline["frame_rate"]
    fps = f"{rate['num']}/{rate['den']}"
    window = piece["output"]["frames"]
    length = _size(window)
    native = piece["source"].get("frames")
    if native is None:
        raise _data(piece["id"], "cutting requires native picture frame bounds")
    start, end = _bounds(native)
    holds = [event for event in piece["events"] if event["kind"] == "freeze"]
    pre = post = 0
    for event in holds:
        a, b = _bounds(event["output"]["frames"], window["start"])
        if a == 0 and b < length and event["source_frame"] == start and not pre:
            pre = b
        elif 0 < a and b == length and event["source_frame"] == end - 1 and not post:
            post = b - a
        else:
            raise _data(piece["id"], "freeze must hold the matching edge frame at a piece boundary")
    live = length - pre - post
    if live <= 0:
        raise _data(piece["id"], "freeze events leave no source picture")

    # Conform the native picture range to the realized frame budget. The difference
    # is bounded by snapping the two edges to source and output frame grids.
    src_fps, out_fps = _ratio(source_rate), _ratio(rate)
    if abs(Fraction(end - start, src_fps) - Fraction(live, out_fps)) > 2 / src_fps + 2 / out_fps:
        raise _data(piece["id"], "picture frame bounds disagree with the planned live duration")
    count = 1 + bool(pre) + bool(post)
    inputs = [f"native{i}" for i in range(count)] if count > 1 else ["0:v:0"]
    if count > 1:
        graph.append(f"[0:v:0]split={count}" + "".join(f"[{label}]" for label in inputs))
    source_start = timeline.get("source_start")

    def trim(a: int, b: int) -> str:
        if source_start is None:
            return f"trim=start_frame={a}:end_frame={b}"
        step = 1 / (src_fps * Fraction(video["time_base"]))
        origin = int(video["start_pts"])
        left, right = (ceil(origin + (i - Fraction(1, 2)) * step) for i in (a, b))
        return f"trim=start_pts={left}:end_pts={right}"

    labels = []
    if pre:
        labels.append("pre")
        graph.append(f"[{inputs.pop(0)}]{trim(start, start + 1)},"
                     f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop={pre - 1},"
                     f"settb=1/{rate['num']},setpts=N*{rate['den']}[pre]")
    labels.append("live")
    if source_start is None:
        speed = Fraction(live, out_fps) / Fraction(end - start, src_fps)
        conformed = (f"settb=AVTB,setpts=(PTS-STARTPTS)*{speed.numerator}/{speed.denominator},"
                     f"fps={fps}:start_time=0,tpad=stop_mode=clone:stop=1")
    else:
        first_live = _first_live_time(piece, timeline, window["start"] + pre, out_fps)
        phase = _ratio(source_start["picture"]) + Fraction(start, src_fps) - first_live
        ticks = lcm(phase.denominator, src_fps.numerator, out_fps.numerator)
        # The output tick selects the most recent native frame, including a frame
        # presented on that exact tick. The final clone covers frame rounding only.
        conformed = (f"tpad=stop_mode=clone:stop=2,settb=1/{ticks},"
                     f"setpts=N*{int(ticks / src_fps)}+({int(phase * ticks)}),"
                     f"fps={fps}:start_time=0:round=up")
    graph.append(f"[{inputs.pop(0)}]{trim(start, end)},{conformed},trim=end_frame={live}[live]")
    if post:
        labels.append("post")
        graph.append(f"[{inputs[0]}]{trim(end - 1, end)},"
                     f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop={post - 1},"
                     f"settb=1/{rate['num']},setpts=N*{rate['den']}[post]")
    _join_video(graph, labels, rate, "picture")

    layout = timeline["layout"]
    preset = REEL_PRESETS[timeline["preset"]]
    width, height = timeline["canvas"]["width"], timeline["canvas"]["height"]
    geometry = split_810 or {}
    speaker_height = preset.split_speaker_height_px or geometry.get("split_speaker_height_px")
    tolerance = preset.screen_fit_tolerance_px
    if tolerance is None:
        tolerance = geometry.get("screen_fit_tolerance_px")
    if layout["mode"] == "split" and (speaker_height is None or tolerance is None):
        raise _data("split_810", "split speaker band and screen fit tolerance are required")

    # A screen choice remains active through subsequent pieces until another switch.
    runs = [(0, layout.get("screen"))]
    for switch in sorted(timeline["crop_switches"], key=lambda row: row["output_frame"]):
        at = switch["output_frame"] - window["start"]
        if at <= 0:
            runs[0] = (0, switch["screen"])
        elif at < length:
            runs.append((at, switch["screen"]))
    labels = []
    if len(runs) > 1:
        graph.append(f"[picture]split={len(runs)}" + "".join(f"[run{i}]" for i in range(len(runs))))
    for i, (at, screen) in enumerate(runs):
        clip = "picture"
        if len(runs) > 1:
            stop = runs[i + 1][0] if i + 1 < len(runs) else length
            clip = f"trim{i}"
            graph.append(f"[run{i}]trim=start_frame={at}:end_frame={stop},setpts=PTS-STARTPTS[{clip}]")
        label = f"crop{i}"
        _composite(graph, clip, label, layout["speaker"], screen, width, height,
                   speaker_height, tolerance, preset.divider_px, background,
                   timeline.get("speaker_background", False))
        labels.append(label)
    _join_video(graph, labels, rate, "cropped")
    filters = []
    for kind, direction in (("picture_fade_in", "in"), ("picture_fade_out", "out")):
        for event in _event_spans(piece, kind, "frames"):
            at, stop = _bounds(event, window["start"])
            if stop - at == 1:
                filters.append(f"drawbox=color=black:t=fill:enable='eq(n,{at})'")
            elif stop > at:
                filters.append(f"fade=t={direction}:s={at}:n={stop - at - 1}")
    graph.append(f"[cropped]{','.join([*filters, 'format=yuv420p'])}[v]")


def _audio_graph(graph: list[str], piece: TimelineElement, sample_rate: int,
                 clock_start: ReelSourceStart | None, audio: dict) -> tuple[Span, Span]:
    window = piece["output"]["samples"]
    speech_events = _event_spans(piece, "speech", "samples")
    if len(speech_events) != 1:
        raise _data(piece["id"], "cutting requires one speech window per piece")
    speech, = speech_events
    opens = _event_spans(piece, "audio_fade_in", "samples")
    closes = _event_spans(piece, "audio_fade_out", "samples")
    start = opens[0]["start"] if opens else speech["start"]
    end = closes[0]["end"] if closes else speech["end"]
    if (len(opens) > 1 or len(closes) > 1 or (opens and (start > speech["start"] or opens[0]["end"] >= speech["end"]))
            or (closes and closes[0]["start"] < speech["end"])):
        raise _data(piece["id"], "audio fades must preserve the end of speech")
    source_start, source_end = _bounds(piece["source"]["samples"])
    if clock_start is None:
        trim = f"atrim=start_sample={source_start}:end_sample={source_end}"
    else:
        trim = f"asettb=1/{audio['sample_rate']},atrim=start_pts={source_start}:end_pts={source_end}"
    filters = [trim, "asetpts=N/SR/TB",
               f"aresample={sample_rate}", f"atrim=end_sample={end - start}"]
    if opens:
        filters.append(f"afade=t=in:ss=0:ns={_size(opens[0])}")
    if closes:
        filters.append(f"afade=t=out:ss={closes[0]['start'] - start}:ns={_size(closes[0])}")
    filters.extend((f"adelay={start - window['start']}S:all=1",
                    f"apad=pad_len={window['end'] - end}", "asetpts=N/SR/TB"))
    graph.append(f"[0:a:0]{','.join(filters)}[a]")
    return speech, {"start": start, "end": end}


def _encode(out: Path, inputs: Sequence[str], graph: Sequence[str], rate: Ratio) -> None:
    media.run_ffmpeg(["-xerror", *inputs, "-filter_complex_threads", "1", "-filter_complex", ";".join(graph),
                      "-map", "[v]", "-map", "[a]", *media.VIDEO_ENC, "-c:a", "pcm_s24le",
                      "-fps_mode", "passthrough", "-enc_time_base:v", f"1/{rate['num']}",
                      "-video_track_timescale", str(rate["num"]), "-f", "mov", str(out)])


def _verify(path: Path, timeline: ReelTimeline, expected: tuple[int, int]) -> tuple[int, int]:
    measured = _count(path, timeline["frame_rate"], timeline["sample_rate"], timeline["canvas"])
    if measured != expected:
        raise _data(str(path), f"decoded {measured} frames/samples, expected {expected}")
    return measured


def cut_reel(
    timeline: ReelTimeline, sources: Mapping[str, str | Path], out: str | Path, *,
    expected_inputs: TimelineInputs, split_810: RequiredReel810Geometry | None = None,
    screen_background: ScreenBackground | None = None,
) -> ReelCutResult:
    """Write a body-only MOV with PCM, after checking fresh inputs and decoded counts.

    expected_inputs must come from current build inputs supplied by the integrator.
    Measured result windows are body-relative, excluding generated cards.
    """
    errors = validate_timeline(timeline)
    if errors or timeline.get("kind") != "reel":
        raise _data("timeline", errors[0] if errors else "expected a reel timeline")
    if timeline["inputs"] != expected_inputs:
        raise _data("timeline.inputs", "stale reel timeline: current build inputs differ")
    pieces = [e for e in timeline["elements"] if e["kind"] == "piece"]
    source_ids = {piece["source"]["source_id"] for piece in pieces}
    if len(source_ids) != 1:
        raise _data("timeline.inputs.sources", "a reel body requires one recording")
    source_id, = source_ids
    if source_id not in sources:
        raise _data(f"sources.{source_id}", "recording path is required")
    source = _recording_path(sources[source_id])
    output = Path(out)
    if output.suffix.lower() != ".mov" or output.resolve() == source:
        raise _data("out", "a distinct .mov path is required for the PCM body")
    before = _signature(source)
    source_input = timeline["inputs"]["sources"][source_id]
    if _recording_digest(source) != source_input["sha256"]:
        raise _data(f"sources.{source_id}", "stale reel timeline: recording bytes changed")
    video, audio, source_rate, source_sample_rate = _source_streams(source)
    if source_rate != _ratio(source_input["frame_rate"]) or source_sample_rate != source_input["sample_rate"]:
        raise _data(f"sources.{source_id}", "native recording clocks changed")
    source_start = _source_start(video, audio)
    if source_start != timeline.get("source_start"):
        raise _data(f"sources.{source_id}", "recording stream starts differ from the compiled timeline")
    rate, sample_rate = timeline["frame_rate"], timeline["sample_rate"]
    clock = EditClock([], frame_rate=rate, sample_rate=sample_rate)
    frame_origin = pieces[0]["output"]["frames"]["start"]
    sample_origin = pieces[0]["output"]["samples"]["start"]
    output.parent.mkdir(parents=True, exist_ok=True)
    measured_rows, files = [], []
    frames = samples = 0

    def shifted(span: Span) -> Span:
        return {"start": span["start"] - sample_origin, "end": span["end"] - sample_origin}

    with TemporaryDirectory(prefix=".reel-cut-", dir=output.parent) as tmp:
        for piece in pieces:
            if _ratio(piece["rate"]) != 1 or piece["audio"] != "source":
                raise _data(piece["id"], "cutting requires unit playback rate and source audio")
            graph = []
            speech, audible = _audio_graph(graph, piece, sample_rate, source_start, audio)
            _video_graph(graph, piece, timeline, source_input["frame_rate"], split_810, screen_background, video)
            path = Path(tmp) / f"{piece['id']}.mov"
            inputs = ["-protocol_whitelist", "file,pipe", "-noautorotate"]
            if source_start:
                start = Fraction(piece["source"]["samples"]["start"], source_sample_rate)
                aligned = _ratio(source_start["aligned"])
                if start < aligned:
                    raise _data(piece["id"], "sound is scheduled before picture and sound overlap")
                picture = _ratio(source_start["picture"]) + Fraction(piece["source"]["frames"]["start"], source_rate)
                # Leave decoder preroll before the earliest sample or held frame.
                seek = max(0, int(min(start, picture)) - 2)
                inputs.extend(("-copyts", "-seek_timestamp", "1", "-ss", str(seek)))
            _encode(path, [*inputs, "-i", str(source)], graph, rate)
            planned = tuple(_size(piece["output"][axis]) for axis in ("frames", "samples"))
            frame_count, sample_count = _verify(path, timeline, planned)
            measured_rows.append(MeasuredPiece(
                element_id=piece["id"],
                body={"frames": {"start": frames, "end": frames + frame_count},
                      "samples": {"start": samples, "end": samples + sample_count}},
                source_frames=piece["source"]["frames"].copy(), source_samples=piece["source"]["samples"].copy(),
                speech_samples=shifted(speech), source_audio_samples=shifted(audible),
                silence_samples=tuple(shifted(span) for span in _event_spans(piece, "silence", "samples")),
            ))
            frames += frame_count
            samples += sample_count
            if samples + sample_origin != clock.sample_at_frame(frames + frame_origin):
                raise _data(piece["id"], "decoded join disagrees with the EditClock seam")
            files.append(path)
        final = files[0]
        if len(files) > 1:
            final = Path(tmp) / "body.mov"
            inputs = [arg for file in files for arg in ("-i", str(file))]
            graph = []
            _join_video(graph, [f"{i}:v:0" for i in range(len(files))], rate, "v")
            audio = "".join(f"[{i}:a:0]" for i in range(len(files)))
            graph.append(f"{audio}concat=n={len(files)}:v=0:a=1,asetpts=N/SR/TB[a]")
            _encode(final, inputs, graph, rate)
            _verify(final, timeline, (frames, samples))
        if _signature(source) != before:
            raise _data(f"sources.{source_id}", "recording changed while the body was being rendered")
        final.replace(output)
    return ReelCutResult(output, frames, samples, tuple(measured_rows))
