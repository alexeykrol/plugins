"""Local, measured long-video assembly. Drawing is an injected operation."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from math import lcm
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from typing import TypedDict
import wave

from . import media
from ..builders.contracts import (
    LongTimeline, RenderBox, RenderBrand, RenderLongCard, RenderLongOverlay,
    Span, validate_renderer_props, validate_timeline,
)
from ..core.errors import VPError
from ..core.io import sha256_file, sha256_obj
from ..steps.long_plan import COMPILER_REVISION, PRESET_REVISION

LongDraw = Callable[[RenderLongCard | RenderLongOverlay, Path], object]
_PCM_WIDTH = 2
_COPY_BUFFER = 1 << 20


@dataclass(frozen=True)
class LongRenderPolicy:
    brand: RenderBrand
    chapter_box: RenderBox
    subscribe_box: RenderBox
    audio_channels: int
    timeout_s: float


class UnitMeasurement(TypedDict):
    id: str
    start_frame: int
    end_frame: int
    start_sample: int
    end_sample: int
    frames: int
    samples: int
    first_frame_sha256: str
    last_frame_sha256: str
    pcm_sha256: str
    silent: bool
    verified_silent_spans: list[Span]


class SeamMeasurement(TypedDict):
    left_unit: str
    right_unit: str
    frame: int
    sample: int
    expected_picture_sha256: list[str]
    observed_picture_sha256: list[str]
    expected_audio_s16: list[list[int]]
    observed_audio_s16: list[list[int]]
    audio_step_s16: int


@dataclass(frozen=True)
class LongRenderEvidence:
    timeline_sha256: str
    media_sha256: str
    join: str
    fallback_reason: str | None
    frame_count: int
    sample_count: int
    units: list[UnitMeasurement]
    seams: list[SeamMeasurement]


@dataclass(frozen=True)
class _Unit:
    video: Path
    audio: Path
    hashes: list[str]
    measurement: UnitMeasurement


def _ffmpeg(policy: LongRenderPolicy, *args) -> str:
    local_args = []
    for arg in args:
        if arg == "-i":
            local_args.extend(["-protocol_whitelist", "file,pipe"])
        local_args.append(arg)
    return media.run_ffmpeg(
        ["-loglevel", "error", "-xerror", *local_args], timeout_s=policy.timeout_s,
    ).stdout


def _probe(path: Path, selector: str, entries: str, policy: LongRenderPolicy, *options) -> dict:
    try:
        proc = subprocess.run(
            [media.FFPROBE, "-v", "error", "-select_streams", selector,
             "-show_entries", entries, "-of", "json", *options,
             "-protocol_whitelist", "file,pipe", str(path)],
            capture_output=True, text=True, check=True, timeout=policy.timeout_s,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VPError("data", f"ffprobe failed for {path.name}: {exc}") from exc
    if proc.stderr.strip():
        raise VPError("data", f"ffprobe reported a decode error for {path.name}: {proc.stderr.strip()[-300:]}")
    return json.loads(proc.stdout)


def _video_clock(path, fps, policy, *, canvas=None):
    picture = _probe(path, "v:0", "stream=time_base,width,height:frame=pts", policy)
    if not picture.get("streams"):
        raise VPError("data", f"{path.name}: expected a local video track")
    stream = picture["streams"][0]
    if canvas and (stream["width"], stream["height"]) != (canvas["width"], canvas["height"]):
        raise VPError("data", f"{path.name}: drawn dimensions differ from the composition")
    video_base = Fraction(stream["time_base"])
    video_frames = picture.get("frames", [])
    # A coarse container time base may round the nominal CFR grid to its nearest tick.
    for index, frame in enumerate(video_frames):
        tick = Fraction(index, fps) / video_base
        if "pts" not in frame or abs(frame["pts"] - tick) > Fraction(1, 2):
            raise VPError("data", f"{path.name}: video is off its zero-origin CFR grid at frame {index}")
    return len(video_frames)


def _audio_clock(path, sample_rate, policy):
    sound = _probe(path, "a:0", "stream=sample_rate,time_base,duration_ts:frame=pts,nb_samples", policy)
    if not sound.get("streams"):
        raise VPError("data", f"{path.name}: expected a local audio track")
    audio_stream = sound["streams"][0]
    if int(audio_stream["sample_rate"]) != sample_rate:
        raise VPError("data", f"{path.name}: source sample rate differs from the timeline")
    audio_base = Fraction(audio_stream["time_base"])
    samples = 0
    for packet in sound.get("frames", []):
        tick = Fraction(samples, sample_rate) / audio_base
        if "pts" not in packet or abs(packet["pts"] - tick) > Fraction(1, 2):
            raise VPError("data", f"{path.name}: audio is not continuous at sample {samples}")
        samples += packet["nb_samples"]
    duration = audio_stream.get("duration_ts")
    return samples, None if duration is None else duration * audio_base


def _check_source(path, source, elements, policy):
    fps = Fraction(source["frame_rate"]["num"], source["frame_rate"]["den"])
    video_frames = _video_clock(path, fps, policy)
    samples, _ = _audio_clock(path, source["sample_rate"], policy)
    for element in elements:
        span = element["source"]
        if "frames" not in span or span["frames"]["end"] > video_frames:
            raise VPError("data", f"{element['id']}: source frame bounds exceed the decoded source")
        if element["audio"] == "source" and span["samples"]["end"] > samples:
            raise VPError("data", f"{element['id']}: source sample bounds exceed the decoded source")


def _frame_hashes(path: Path, fps: Fraction, count: int, policy: LongRenderPolicy) -> list[str]:
    output = _ffmpeg(
        policy, "-copyts", "-i", path, "-map", "0:v:0", "-an", "-fps_mode", "passthrough",
        "-enc_time_base", "demux", "-f", "framehash", "-hash", "sha256", "-",
    )
    base = None
    hashes = []
    for line in output.splitlines():
        if line.startswith("#tb 0: "):
            base = Fraction(line.split(": ", 1)[1])
        if not line or line.startswith("#"):
            continue
        _, _, pts, duration, _, digest = (value.strip() for value in line.split(","))
        if base is None or int(pts) * base != Fraction(len(hashes), fps) or int(duration) * base != 1 / fps:
            raise VPError("data", f"{path.name}: video PTS or duration differs at frame {len(hashes)}")
        hashes.append(digest)
    if len(hashes) != count:
        raise VPError("data", f"{path.name}: expected {count} decoded frames, got {len(hashes)}")
    return hashes


def _encode_video(policy, source, filters, out, fps, count):
    filters = [*filters, f"settb={1 / fps}", "setpts=N",
               f"fps={fps}:eof_action=pass", f"trim=end_frame={count}"]
    _ffmpeg(policy, *source, "-map", "0:v:0", "-an", "-vf", ",".join(filters),
            *media.VIDEO_ENC, "-bf", "0", "-fps_mode", "passthrough",
            "-enc_time_base", f"{fps.denominator}/{fps.numerator}",
            "-video_track_timescale", fps.numerator, out)


def _picture(element, source_path, out, fps, canvas, policy):
    output = element["output"]["frames"]
    freeze = next((e for e in element["events"] if e["kind"] == "freeze"), None)
    hold = 0 if freeze is None else output["end"] - freeze["output"]["frames"]["start"]
    content = output["end"] - output["start"] - hold
    source = element["source"]["frames"]
    count = source["end"] - source["start"]
    if freeze is not None and (
        freeze["source_frame"] != source["end"] - 1
        or freeze["output"]["frames"]["end"] != output["end"] or content <= 0
    ):
        raise VPError("data", f"{element['id']}: unsupported hold outside the trailing source frame")
    filters = [f"trim=start_frame={source['start']}:end_frame={source['end']}"]
    if count != content:
        # The extra source frame is a boundary marker for fps. Trim it away
        # before adding any explicitly planned hold.
        frame_time = Fraction(content, count) / fps
        filters += ["tpad=stop=1:stop_mode=clone", "settb=AVTB",
                    f"setpts=N*{frame_time.numerator}/{frame_time.denominator}/TB",
                    f"fps={fps}:round=near", f"trim=end_frame={content}"]
    if hold:
        filters.append(f"tpad=stop={hold}:stop_mode=clone")
    width, height = canvas["width"], canvas["height"]
    filters += [f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]
    if transition := element.get("transition"):
        span = transition["output"]["frames"]
        color = "white" if transition["kind"] == "flash" else "black"
        filters.append(f"fade=t=in:s={span['start'] - output['start']}:n={span['end'] - span['start']}:color={color}")
    _encode_video(policy, ["-i", source_path], filters, out, fps, content + hold)


def _audio(element, source_path, out, sample_rate, policy):
    output = element["output"]["samples"]
    count = output["end"] - output["start"]
    if element["audio"] == "silent":
        source = ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={'mono' if policy.audio_channels == 1 else 'stereo'}"]
        filters = []
    else:
        source = ["-i", source_path]
        bounds = element["source"]["samples"]
        source_count = bounds["end"] - bounds["start"]
        hold = next((e["output"]["samples"] for e in element["events"] if e["kind"] == "freeze"), None)
        content = count if hold is None else hold["start"] - output["start"]
        # Resample at the exact ratio of realized sample lengths, then restore
        # the output clock. There is no per-piece AAC priming or padding.
        ratio = Fraction(content, source_count)
        scale = max(1, sample_rate // max(ratio.denominator, ratio.numerator))
        virtual_in = ratio.denominator * scale
        virtual_out = ratio.numerator * scale
        filters = [
            f"atrim=start_sample={bounds['start']}:end_sample={bounds['end']}",
            "asetpts=N/SR/TB",
            f"asetrate={virtual_in}", f"aresample={virtual_out}", f"asetrate={sample_rate}",
            f"atrim=end_sample={content}",
        ]
        for event in element["events"]:
            if event["kind"] in ("audio_fade_in", "audio_fade_out"):
                span = event["output"]["samples"]
                direction = "in" if event["kind"] == "audio_fade_in" else "out"
                filters.append(f"afade=t={direction}:ss={span['start'] - output['start']}:ns={span['end'] - span['start']}")
        if hold is not None:
            filters += [f"apad=pad_len={hold['end'] - hold['start']}"]
    filters += [f"atrim=end_sample={count}", "asetpts=N/SR/TB"]
    _ffmpeg(policy, *source, "-map", "0:a:0", "-vn", "-af", ",".join(filters),
            "-ac", policy.audio_channels, "-c:a", "pcm_s16le", out)


def _draw(draw, props, path):
    errors = validate_renderer_props(props)
    if errors:
        raise VPError("data", "\n".join(errors))
    draw(props, path)
    if not path.is_file() or not path.stat().st_size:
        raise VPError("data", f"draw did not produce {path.name}")


def _overlay_boxes(timeline, policy):
    boxes = {"chapter": policy.chapter_box, "subscribe": policy.subscribe_box}
    overlays = timeline["overlays"]
    for i, left in enumerate(overlays):
        a = boxes[left["kind"]]
        for right in overlays[i + 1:]:
            if left["output"]["frames"]["end"] <= right["output"]["frames"]["start"]:
                continue
            b = boxes[right["kind"]]
            shared_area = (
                a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
                and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]
            )
            if shared_area:
                raise VPError("data", f"overlay boxes collide: {left['id']} and {right['id']}")
    return boxes


def _conform_drawn(path, out, count, timing, fps, policy):
    if _video_clock(path, fps, policy, canvas=timing) != count:
        raise VPError("data", f"{path.name}: drawn frame count differs from the composition")
    _encode_video(policy, ["-i", path], ["setsar=1"], out, fps, count)


def _render_unit(timeline, element, unit, index, sources, work, public, draw, policy, fps, boxes):
    key = unit["id"]
    stem = f"unit-{index}"
    frames = unit["output"]["frames"]
    samples = unit["output"]["samples"]
    count = frames["end"] - frames["start"]
    sample_count = samples["end"] - samples["start"]
    video = None
    source = None
    if "source" in element:
        source = sources[element["source"]["source_id"]]
        video = work / f"{stem}-source.mp4"
        _picture(element, source, video, fps, timeline["canvas"], policy)
        _frame_hashes(video, fps, count, policy)
    timing = {**timeline["canvas"], "fps": float(fps), "durationInFrames": count}
    if card := element.get("card"):
        props: RenderLongCard = {
            **timing, "kind": "long_card", "role": element["kind"], "title": card["text"],
            "video": video.relative_to(public).as_posix() if video else None,
            "brand": policy.brand, "points": card.get("points", []),
        }
        for field, prop in (("subtitle", "subtitle"), ("subline", "subline"), ("end_text", "endText")):
            if field in card:
                props[prop] = card[field]
        drawn = work / f"{stem}-card.mp4"
        _draw(draw, props, drawn)
        video = work / f"{stem}-card-conformed.mp4"
        _conform_drawn(drawn, video, count, timing, fps, policy)
    overlays = []
    for overlay in timeline["overlays"]:
        bounds = overlay["output"]["frames"]
        if bounds["start"] < frames["end"] and frames["start"] < bounds["end"]:
            kind = overlay["kind"]
            overlays.append({
                "id": overlay["id"], "kind": kind, "text": overlay["text"],
                "startFrame": bounds["start"] - frames["start"], "endFrame": bounds["end"] - frames["start"],
                "box": boxes[kind],
            })
    if overlays:
        props: RenderLongOverlay = {
            **timing, "kind": "long_overlay", "video": video.relative_to(public).as_posix(),
            "brand": policy.brand, "overlays": overlays,
        }
        drawn = work / f"{stem}-overlay.mp4"
        _draw(draw, props, drawn)
        video = work / f"{stem}-overlay-conformed.mp4"
        _conform_drawn(drawn, video, count, timing, fps, policy)
    hashes = _frame_hashes(video, fps, count, policy)
    audio = work / f"{stem}.wav"
    _audio(element, source, audio, timeline["sample_rate"], policy)
    if not audio.is_file():
        raise VPError("data", f"{key}: no unit audio was produced")
    verified_silence = []
    with wave.open(str(audio), "rb") as pcm:
        if _pcm_shape(pcm) != (policy.audio_channels, _PCM_WIDTH, timeline["sample_rate"], sample_count):
            raise VPError("data", f"{key}: PCM clock differs from the unit's sample partition")
        pcm_digest, silent = _pcm_digest(pcm, sample_count)
        if element["audio"] == "silent" and not silent:
            raise VPError("data", f"{key}: planned silent unit contains sound")
        for event in element["events"]:
            if event["kind"] == "silence":
                bounds = event["output"]["samples"]
                pcm.setpos(bounds["start"] - samples["start"])
                _, quiet = _pcm_digest(pcm, bounds["end"] - bounds["start"])
                if not quiet:
                    raise VPError("data", f"{key}: planned silent interval contains sound")
                verified_silence.append(bounds.copy())
    measurement: UnitMeasurement = {
        "id": key, "start_frame": frames["start"], "end_frame": frames["end"],
        "start_sample": samples["start"], "end_sample": samples["end"],
        "frames": count, "samples": sample_count, "pcm_sha256": pcm_digest,
        "first_frame_sha256": hashes[0], "last_frame_sha256": hashes[-1], "silent": silent,
        "verified_silent_spans": verified_silence,
    }
    return _Unit(video, audio, hashes, measurement)


def _pcm_shape(pcm):
    return pcm.getnchannels(), pcm.getsampwidth(), pcm.getframerate(), pcm.getnframes()


def _pcm_digest(pcm, frames):
    digest = hashlib.sha256()
    silent = True
    chunk = _COPY_BUFFER // (pcm.getnchannels() * pcm.getsampwidth())
    while frames:
        count = min(chunk, frames)
        block = pcm.readframes(count)
        if len(block) != count * pcm.getnchannels() * pcm.getsampwidth():
            raise VPError("data", "decoded PCM ended before its expected sample boundary")
        digest.update(block)
        silent = silent and block.count(0) == len(block)
        frames -= count
    return digest.hexdigest(), silent


def _pcm_edge(path, *, last):
    with wave.open(str(path), "rb") as pcm:
        if last:
            pcm.setpos(pcm.getnframes() - 1)
        return _unpack_sample(pcm)


def _unpack_sample(pcm):
    return list(struct.unpack("<" + "h" * pcm.getnchannels(), pcm.readframes(1)))


def _master_audio(units, out, sample_rate, channels):
    with wave.open(str(out), "wb") as master:
        master.setparams((channels, _PCM_WIDTH, sample_rate, 0, "NONE", "not compressed"))
        for unit in units:
            with wave.open(str(unit.audio), "rb") as pcm:
                while block := pcm.readframes(_COPY_BUFFER // (channels * _PCM_WIDTH)):
                    master.writeframesraw(block)


def _join_video(units, work, fps, policy, copy):
    out = work / "joined.mp4"
    if copy:
        settings = [
            _probe(unit.video, "v:0",
                   "stream=extradata_hash,codec_name,pix_fmt,width,height,time_base:"
                   "packet=flags", policy, "-show_data_hash", "sha256", "-read_intervals", "%+#1")
            for unit in units
        ]
        first_stream = settings[0].get("streams")
        if (not first_stream or not first_stream[0].get("extradata_hash")
                or any(row.get("streams") != first_stream or not row.get("packets")
                       or "K" not in row["packets"][0]["flags"]
                       for row in settings)):
            raise VPError("data", "stream copy requires matching video configuration and unit keyframes")
        playlist = work / "video.ffconcat"
        playlist.write_text("ffconcat version 1.0\n" + "".join(
            f"file {unit.video.name}\n" for unit in units
        ), encoding="utf-8")
        _ffmpeg(policy, "-f", "concat", "-safe", "1", "-i", playlist, "-map", "0:v:0", "-an", "-c:v", "copy",
                "-video_track_timescale", fps.numerator, out)
    else:
        # Decode each unit independently so incompatible stream parameters or
        # overlapping container timestamps in the copy candidate cannot
        # carry over. The video-only concat filter emits each decoded frame once.
        inputs = [argument for unit in units for argument in ("-threads", "1", "-i", unit.video)]
        clips = [f"[{i}:v:0]setpts=PTS-STARTPTS[v{i}]" for i in range(len(units))]
        labels = "".join(f"[v{i}]" for i in range(len(units)))
        graph = ";".join([*clips, f"{labels}concat=n={len(units)}:v=1:a=0,settb={1 / fps},setpts=N[v]"])
        _ffmpeg(policy, *inputs, "-filter_complex", graph, "-map", "[v]", "-an", *media.VIDEO_ENC,
                "-crf", "0", "-bf", "0", "-fps_mode", "passthrough",
                "-enc_time_base", f"{fps.denominator}/{fps.numerator}",
                "-video_track_timescale", fps.numerator, out)
    return out


def _mux_and_measure(units, video, audio, candidate, work, fps, timeline, policy):
    sample_rate = timeline["sample_rate"]
    _ffmpeg(policy, "-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "alac", "-movflags", "+faststart",
            "-video_track_timescale", fps.numerator, "-movie_timescale", lcm(fps.numerator, sample_rate),
            candidate)
    expected = [digest for unit in units for digest in unit.hashes]
    observed = _frame_hashes(candidate, fps, timeline["duration_frames"], policy)
    if observed != expected:
        mismatch = next(i for i, (a, b) in enumerate(zip(expected, observed)) if a != b)
        raise VPError("data", f"joined picture content differs at frame {mismatch}")
    decoded = work / "master-decoded.wav"
    _ffmpeg(policy, "-i", candidate, "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", decoded)
    measured_samples, duration = _audio_clock(candidate, sample_rate, policy)
    if (measured_samples != timeline["duration_samples"]
            or duration != Fraction(timeline["duration_samples"], sample_rate)):
        raise VPError("data", "joined audio timestamp bounds differ from the sample clock")
    seams = []
    with wave.open(str(decoded), "rb") as pcm:
        if _pcm_shape(pcm) != (policy.audio_channels, _PCM_WIDTH, sample_rate, timeline["duration_samples"]):
            raise VPError("data", "joined PCM shape differs from the sample clock")
        for unit in units:
            digest, _ = _pcm_digest(pcm, unit.measurement["samples"])
            if digest != unit.measurement["pcm_sha256"]:
                raise VPError("data", f"joined PCM differs in {unit.measurement['id']}")
        for left, right in zip(units, units[1:]):
            frame = right.measurement["start_frame"]
            sample = right.measurement["start_sample"]
            pcm.setpos(sample - 1)
            before, after = _unpack_sample(pcm), _unpack_sample(pcm)
            seams.append({
                "left_unit": left.measurement["id"], "right_unit": right.measurement["id"],
                "frame": frame, "sample": sample,
                "expected_picture_sha256": [left.hashes[-1], right.hashes[0]],
                "observed_picture_sha256": observed[frame - 1:frame + 1],
                "expected_audio_s16": [_pcm_edge(left.audio, last=True), _pcm_edge(right.audio, last=False)],
                "observed_audio_s16": [before, after],
                "audio_step_s16": max(abs(a - b) for a, b in zip(before, after)),
            })
    return len(observed), timeline["duration_samples"], seams


def render_long(
    timeline: LongTimeline, sources: Mapping[str, Path], out: Path, *,
    current_timeline: LongTimeline, public_dir: Path, draw: LongDraw,
    policy: LongRenderPolicy, try_stream_copy: bool = True,
) -> LongRenderEvidence:
    """Render a current compiled candidate and atomically expose its verified MP4.

    Pass a timeline freshly compiled from the current artifacts as
    `current_timeline`. The injected draw uses paths relative to `public_dir`
    and must emit the requested composition with zero-origin CFR video.
    """
    errors = validate_timeline(timeline)
    if errors or timeline["kind"] != "long":
        raise VPError("data", "\n".join(errors) or "expected a long timeline")
    if (timeline != current_timeline or timeline["compiler_revision"] != COMPILER_REVISION
            or timeline["preset_revision"] != PRESET_REVISION):
        raise VPError("data", "stale long timeline: rebuild from the current compilation inputs")
    out, public = Path(out).resolve(), Path(public_dir).resolve()
    if out.suffix.lower() != ".mp4" or not public.is_dir():
        raise VPError("data", "expected an MP4 destination and an existing renderer public directory")
    if (type(policy.audio_channels) is not int or policy.audio_channels not in (1, 2)
            or not 0 < policy.timeout_s < float("inf")
            or any(size % 2 for size in timeline["canvas"].values())):
        raise VPError("data", "invalid render policy: PCM channels, timeout or H.264 canvas")
    paths = {key: Path(path).resolve() for key, path in sources.items()}
    if out in paths.values():
        raise VPError("data", "render destination would overwrite a source")
    elements = timeline["elements"]
    video_units = timeline["video_units"]
    if len(video_units) != len(elements) or any(
        unit["element_ids"] != [element["id"]] or unit["output"] != element["output"]
        for element, unit in zip(elements, video_units)
    ):
        raise VPError("data", "expected the long compiler's one-element video units")
    boxes = _overlay_boxes(timeline, policy)
    for sid, fingerprint in timeline["inputs"]["sources"].items():
        if sid not in paths or not paths[sid].is_file() or sha256_file(paths[sid]) != fingerprint["sha256"]:
            raise VPError("data", f"stale long timeline: source {sid} fingerprint differs")
        used = [element for element in elements if element.get("source", {}).get("source_id") == sid]
        if used:
            _check_source(paths[sid], fingerprint, used, policy)
    rate = timeline["frame_rate"]
    fps = Fraction(rate["num"], rate["den"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with (tempfile.TemporaryDirectory(prefix="long-render-", dir=public) as workspace,
          tempfile.TemporaryDirectory(prefix=".long-candidate-", dir=out.parent) as staging):
        work, candidate = Path(workspace), Path(staging) / "master.mp4"
        units = [
            _render_unit(timeline, element, unit, i, paths, work, public, draw, policy, fps, boxes)
            for i, (element, unit) in enumerate(zip(elements, video_units))
        ]
        audio = work / "master.wav"
        _master_audio(units, audio, timeline["sample_rate"], policy.audio_channels)
        failure = None
        if try_stream_copy:
            try:
                video = _join_video(units, work, fps, policy, copy=True)
                measured = _mux_and_measure(units, video, audio, candidate, work, fps, timeline, policy)
            except VPError as exc:
                failure = exc.msg
        if not try_stream_copy or failure is not None:
            video = _join_video(units, work, fps, policy, copy=False)
            measured = _mux_and_measure(units, video, audio, candidate, work, fps, timeline, policy)
        if any(sha256_file(paths[sid]) != fingerprint["sha256"]
               for sid, fingerprint in timeline["inputs"]["sources"].items()):
            raise VPError("data", "long source changed during render")
        frame_count, sample_count, seams = measured
        evidence = LongRenderEvidence(
            sha256_obj(timeline), sha256_file(candidate),
            "stream_copy" if try_stream_copy and failure is None else "reencoded", failure,
            frame_count, sample_count, [unit.measurement for unit in units], seams,
        )
        os.replace(candidate, out)
    return evidence
