"""Offline command branch for approved editorial plans and immutable local builds."""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, replace
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

import yaml

from .. import transcribe
from ..adapters import media
from ..adapters.build_render import EffectGeometry, render_build as draw_prepared_build
from ..adapters.long_render import LongRenderEvidence, LongRenderPolicy, render_long
from ..adapters.production_qa import ProductionQAPolicy, master_audio, measure_production
from ..adapters.reel_cut import ReelSource, ScreenBackground, _probe, cut_reel, probe_reel_source
from ..adapters.render_remotion import (
    RemotionRenderer, RemotionWorkspace, code_hash, find_browser, find_node, missing_tools, render_gl, source_hash,
)
from ..analysis import align_words
from ..analysis.edit_anchors import WordTiming
from ..builders.contracts import (
    normalize_long_plan, normalize_reel_item, plan_digest, validate_long_plan,
    validate_qa_record, validate_reels, validate_renderer_props, validate_timeline,
)
from ..builders.presets import REEL_PRESETS
from ..core.errors import VPError
from ..core.io import MISSING, atomic_write, canonical_json, now_iso, sha256_bytes, sha256_obj, write_json
from ..core.lock import job_lock
from ..core.paths import BuildPaths, FULL, Job, vp_home
from ..core.srt import format_srt, parse_srt
from . import long_plan, reel_plan
from ._author_fields import author_fields, check_author_fields
from .long_qa import build_long_qa, format_long_report
from .s0_intake import STEP as S0
from .s3b_review import STEP as S3B
from .s4_cut import STEP as S4
from .s6a_design_texts import DESIGN_FIELDS, STEP as S6A, text_violation

_REVISION = "local-build-1"
_SOURCE_ID = "recording"
_MANIFEST = "manifest.json"
_FILE_PROTOCOLS = ["-protocol_whitelist", "file,pipe"]
_REEL_SETTINGS = (
    "sample_rate", "audio_close_ms", "intro_readable_dwell_frames", "thought_readable_dwell_frames",
    "max_audio_lag_ms", "min_audio_correlation", "min_card_ssim", "quiet_tail_ms", "caption_plate",
    "show_body_plate",
)
_LONG_COMPILE_SETTINGS = ("width", "height", "fps_num", "fps_den", "sample_rate", "max_cut_shift_ms",
                          "hook_end_text_lead_ms")
_LONG_RENDER_SETTINGS = ("audio_channels", "timeout_s", "chapter_box", "subscribe_box",
                         "max_audio_step_s16", "target_lufs", "lufs_tolerance", "max_dbtp",
                         "max_audio_lag_ms", "min_audio_correlation", "min_card_ssim",
                         "intro_readable_dwell_frames", "outro_readable_dwell_frames",
                         "audio_lag_grid_ms", "audio_lag_search_ms")
_SPLIT_810_SETTINGS = ("split_speaker_height_px", "split_screen_height_px", "screen_fit_tolerance_px",
                       "caption_clearance_px", "caption_half_extra_px")


def _data(message: str) -> VPError:
    return VPError("data", message)


def _file_digest(path: Path) -> str:
    """Hash current bytes, including edits that preserve size and filesystem timestamps."""
    try:
        if not path.is_file():
            return MISSING
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return MISSING


def _read_mapping(raw: bytes, path: Path) -> dict:
    try:
        value = yaml.safe_load(raw)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise _data(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise _data(f"expected a YAML mapping in {path}")
    return value


def _read_json(path: Path):
    return json.loads(path.read_bytes())


def _validate(validator, value) -> None:
    if errors := validator(value):
        raise _data("\n".join(errors))


def _lookup(settings: dict, dotted: str):
    value: Any = settings
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _require(settings: dict, keys: list[str]) -> None:
    missing = sorted({key for key in keys if _lookup(settings, key) is None})
    if missing:
        raise _data("missing builders settings: " + ", ".join(f"builders.{key}" for key in missing))


def _validate_settings(settings: dict, section: str, *, ints=(), nums=(), bools=()) -> None:
    bad = []
    for name in ints:
        if type(settings[name]) is not int:
            bad.append(name)
    for name in nums:
        if type(settings[name]) not in (int, float) or not math.isfinite(settings[name]):
            bad.append(name)
    for name in bools:
        if type(settings[name]) is not bool:
            bad.append(name)
    if bad:
        raise _data("invalid builders settings: " + ", ".join(f"builders.{section}.{name}" for name in bad))


class Inputs:
    """Capture the exact bytes used by a command and check them at readback."""

    def __init__(self, job: Job):
        self.job = job
        self.hashes: dict[Path, str] = {}

    def read(self, path: Path, *, optional: bool = False) -> bytes | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            if not optional:
                raise _data(f"missing local build input: {path}") from None
            raw = None
        self.remember(path, sha256_bytes(raw) if raw is not None else MISSING)
        return raw

    def remember(self, path: Path, digest: str) -> None:
        if self.hashes.setdefault(path, digest) != digest:
            raise _data(f"local build input changed during the command: {path}")

    def mapping(self, path: Path, *, optional: bool = False) -> dict | None:
        raw = self.read(path, optional=optional)
        return _read_mapping(raw, path) if raw is not None else None

    def verify(self) -> None:
        if os.path.lexists(self.job.lock_file):
            raise _data("the pipeline took the recording job lock during the local build")
        for path, digest in self.hashes.items():
            if _file_digest(path) != digest:
                raise _data(f"local build input changed during the command: {path}")


class Store:
    """Content-addressed entries with manifest-last publication under one work lock."""

    def __init__(self, paths: BuildPaths):
        self.paths = paths

    def path(self, *parts: str) -> Path:
        return self.paths.destination(self.paths.root.joinpath(*parts))

    def output(self, directory: Path, name: str) -> Path:
        return self.paths.destination(directory / name)

    def valid(self, address: Path, stage: str, deps: dict, names: tuple[str, ...]) -> bool:
        if not address.is_dir():
            return False
        try:
            manifest = _read_json(self.output(address, _MANIFEST))
            if not isinstance(manifest, dict):
                return False
            outputs = manifest.get("outputs")
            if (manifest.get("stage") != stage or manifest.get("version") != _REVISION
                    or manifest.get("config_hash") != sha256_obj(deps)
                    or manifest.get("inputs") != deps or not isinstance(outputs, dict)
                    or set(outputs) != set(names)):
                return False
            return all(digest != MISSING and _file_digest(self.output(address, name)) == digest
                       for name, digest in outputs.items())
        except (ValueError, UnicodeError, OSError):
            return False

    def address(self, stage: str, deps: dict, *, finished: bool = False) -> Path:
        version_key = sha256_obj([_REVISION, stage, deps])
        return self.path("builds", version_key) if finished else self.path("cache", stage, version_key)

    def failed_directory(self, prefix: str) -> Path:
        failed = self.path("failed")
        failed.mkdir(exist_ok=True)
        return Path(tempfile.mkdtemp(dir=failed, prefix=prefix + "-"))

    def materialize(self, stage: str, deps: dict, names: tuple[str, ...], produce: Callable,
                    *, item: str | None = None, finished: bool = False) -> tuple[Path, bool]:
        address = self.address(stage, deps, finished=finished)
        if self.valid(address, stage, deps, names):
            return address, True
        if address.exists():
            invalid = self.failed_directory("invalid") / address.name
            os.replace(address, self.paths.destination(invalid))
        address.parent.mkdir(parents=True, exist_ok=True)
        started = now_iso()
        with tempfile.TemporaryDirectory(dir=self.paths.root, prefix=".staging-") as temp:
            stage_dir = Path(temp)
            try:
                produce(stage_dir)
                outputs = {name: _file_digest(self.output(stage_dir, name)) for name in names}
                if MISSING in outputs.values():
                    raise _data(f"{stage} did not produce every declared local artifact")
                write_json(self.output(stage_dir, _MANIFEST), {
                    "stage": stage, "item": item, "version": _REVISION, "inputs": deps,
                    "config_hash": sha256_obj(deps), "outputs": outputs,
                    "started_at": started, "finished_at": now_iso(),
                })
                os.replace(stage_dir, self.paths.destination(address))
            except BaseException as exc:
                diagnostics = [name for name in ("qa.json", "report.md") if (stage_dir / name).is_file()]
                if diagnostics:
                    failed = self.failed_directory(stage)
                    for name in diagnostics:
                        os.replace(self.output(stage_dir, name), self.output(failed, name))
                    if isinstance(exc, VPError):
                        exc.details["diagnostics"] = str(failed)
                raise
        return address, False

    def json_cache(self, stage: str, deps: dict, make: Callable):
        directory, _ = self.materialize(stage, deps, ("data.json",),
                                        lambda temp: write_json(temp / "data.json", make()))
        return _read_json(directory / "data.json")


def _stage_manifest(inputs: Inputs, stage, item: str | None = None) -> dict:
    path = inputs.job.manifest_path(stage.id, item)
    manifest = inputs.mapping(path)
    if (manifest.get("stage") != stage.id or manifest.get("item") != item
            or str(manifest.get("version")) != str(stage.version)
            or not all(isinstance(manifest.get(field), dict) for field in ("inputs", "outputs"))):
        raise _data(f"invalid {stage.id} acceptance manifest: {path}")
    return manifest


def _check_stage_file(inputs: Inputs, manifest: dict, side: str, path: Path) -> None:
    digest = inputs.hashes.get(path)
    if digest is None:
        digest = _file_digest(path)
        inputs.remember(path, digest)
    if manifest[side].get(inputs.job.rel(path)) != digest:
        raise _data(f"stale {manifest['stage']} acceptance evidence: {path.name}")


def _source_and_selection(inputs: Inputs) -> tuple[Path, str, dict, list, transcribe.JobWordTimes]:
    job = inputs.job
    source = job.source_video
    if (source_digest := _file_digest(source)) == MISSING:
        raise _data(f"missing local recording: {source}")
    inputs.remember(source, source_digest)
    timing = _read_word_times(inputs, source)
    accepted = inputs.mapping(job.fragments_yaml)
    cues = parse_srt(inputs.read(job.subtitles_srt).decode("utf-8-sig"))
    selection = _stage_manifest(inputs, S3B)
    intake = _stage_manifest(inputs, S0)
    _check_stage_file(inputs, intake, "inputs", source)
    _check_stage_file(inputs, intake, "outputs", job.job_yaml)
    _check_stage_file(inputs, selection, "inputs", job.job_yaml)
    _check_stage_file(inputs, selection, "inputs", job.subtitles_srt)
    _check_stage_file(inputs, selection, "outputs", job.fragments_yaml)
    # Older manifests already record an explicit MISSING source-words input.
    if job.rel(job.source_words) in selection["inputs"] or job.source_words.exists():
        _check_stage_file(inputs, selection, "inputs", job.source_words)
    whole = accepted.get("whole_recording")
    if isinstance(whole, dict) and whole.get("kind") in ("reel", "full"):
        if selection.get("config", {}).get("whole_recording") != whole["kind"]:
            raise _data("stale S3b acceptance evidence: whole-recording selection")
    else:
        for name, path in (("review_sha256", job.fragments_review),
                           ("proposed_sha256", job.fragments_proposed)):
            _check_stage_file(inputs, selection, "inputs", path)
            digest = inputs.hashes[path]
            if digest == MISSING or accepted.get(name) != digest:
                raise _data(f"missing accepted S3b review evidence: {path.name}")
    return source.resolve(), source_digest, accepted, cues, timing


def _fragments(accepted: dict) -> dict[str, dict]:
    rows = accepted.get("fragments")
    if not isinstance(rows, list):
        raise _data("fragments.yaml must contain accepted fragments")
    records = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise _data("accepted fragment is missing an item ID")
        item = row["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", item) or item == FULL or item in records:
            raise _data(f"invalid or duplicate accepted fragment ID: {item!r}")
        if (not all(type(row.get(key)) is int for key in ("start_ms", "end_ms"))
                or not 0 <= row["start_ms"] < row["end_ms"] or row.get("type") not in ("reel", "long")):
            raise _data(f"invalid accepted fragment bounds or kind: {item}")
        records[item] = row
    return records


def _tool_fingerprint() -> str:
    paths = [shutil.which(binary) for binary in (media.FFMPEG, media.FFPROBE)]
    if not all(paths):
        missing = [binary for binary, path in zip((media.FFMPEG, media.FFPROBE), paths) if not path]
        raise _data("missing local media tools: " + ", ".join(missing))
    return sha256_obj([(path, _file_digest(Path(path))) for path in paths])


def _source_info(store: Store, path: Path, digest: str, tools: str) -> ReelSource:
    def probe():
        result = probe_reel_source(path)
        if result.media["sha256"] != digest:
            raise _data("recording changed during source measurement")
        return asdict(result)

    return ReelSource(**store.json_cache("source", {"recording": digest, "tools": tools}, probe))


def _silences(store: Store, source: Path, info: ReelSource, tools: str, db: float, minimum_ms: int):
    if not math.isfinite(db) or db >= 0 or minimum_ms <= 0:
        raise _data("silence detector requires a negative threshold and positive minimum duration")

    def measure():
        # The legacy silencedetect helper can follow network protocol references.
        timestamps = ["-copyts"] if info.source_start else []
        proc = media.run_ffmpeg([*_FILE_PROTOCOLS, *timestamps, "-i", source, "-vn", "-af",
                                 f"silencedetect=n={db}dB:d={minimum_ms / 1000}", "-f", "null", "-"])
        starts = media._SILENCE_START.findall(proc.stderr)
        ends = media._SILENCE_END.findall(proc.stderr)
        spans = []
        for index, value in enumerate(starts):
            start = max(0, math.ceil(Fraction(value) * 1000))
            end = min(info.duration_ms, math.floor(Fraction(ends[index]) * 1000)
                      if index < len(ends) else info.duration_ms)
            if start < end:
                spans.append((start, end))
        return spans

    return store.json_cache("silence", {"recording": info.media["sha256"], "tools": tools,
                                        "db": db, "minimum_ms": minimum_ms,
                                        "source_start": info.source_start}, measure)


def _align(cues: list, words: list[tuple[str, int, int, str]]) -> dict:
    # Replay the legacy match selection so that an interpolated or clipped edge
    # can never accidentally certify a speech cut. The aligner only exposes a total.
    # The reader splits a multiword entry into tokens that share the same bounds.
    bounds = Counter((start, end) for _, start, end, _ in words)
    heard = align_words.timed_words(words)
    if not heard:
        return {}
    tokens = [(cue, token) for cue in cues for token in cue.text.split()]
    evidence = {}
    for authored, recognized, size in align_words._blocks([token for _, token in tokens],
                                                         [text for text, _, _ in heard]):
        for offset in range(size):
            cue, _ = tokens[authored + offset]
            index = recognized + offset
            _, start, end = heard[index]
            if (bounds[start, end] == 1
                    and cue.start_ms - align_words.MATCH_SLACK_MS <= start <= cue.end_ms + align_words.MATCH_SLACK_MS):
                evidence[authored + offset] = (start, end, f"source:w{index + 1}")
    if not evidence:
        return {}
    result = align_words.align(cues, heard)
    index = 0
    output = {}
    for cue, aligned in zip(cues, result.cues):
        rows = []
        for text, start, end in aligned:
            match = evidence.get(index)
            index += 1
            measured_start = match is not None and start == match[0] and start < end
            measured_end = match is not None and end == match[1] and start < end
            rows.append(asdict(WordTiming(
                text, start, end, "measured" if measured_start else "interpolated",
                "measured" if measured_end else "interpolated",
                match[2] if measured_start or measured_end else None,
            )))
        if any(row["start_provenance"] == "measured" or row["end_provenance"] == "measured" for row in rows):
            output[cue.id] = rows
    return output


def _read_word_times(inputs: Inputs, source: Path) -> transcribe.JobWordTimes:
    job = inputs.job
    for path in (job.source_words, job.caption_words, job.caption_choice,
                 job.root / transcribe.META_NAME, job.root / transcribe.RAW_NAME):
        inputs.remember(path, _file_digest(path))

    def probe(path):
        duration = _probe(path, "-show_entries", "format=duration")["format"]["duration"]
        return {"duration_ms": round(float(duration) * 1000)}

    timing = transcribe.read_job_word_times(job, video=source, probe=probe)
    if timing.provenance == transcribe.LEGACY_UNVERIFIED:
        raise _data("The word times of this job were saved before recordings were identified, "
                    "so they cannot be confirmed for this recording. The local builder needs "
                    "confirmed times from `vp transcribe run <job> --quote <quote_id>`.")
    if timing.provenance != transcribe.VERIFIED or not timing.words:
        raise _data(timing.reason or transcribe.LOCAL_UNVERIFIED)
    return timing


def _word_times(timing: transcribe.JobWordTimes, store: Store, cues: list, transcript: str,
                recording: str) -> tuple[dict | None, str]:
    digest = sha256_obj(timing.words)
    aligned = store.json_cache("alignment", {"transcript": transcript, "recording": recording, "words": digest},
                               lambda: _align(cues, timing.words))
    return ({key: [WordTiming(**row) for row in rows] for key, rows in aligned.items()} or None), digest


def _compile_with_binding(store: Store, kind: str, plan: str, transcript: str, compile_current: Callable,
                          *, authored_plan: str | None = None, accept: bool = True):
    keys = sorted({plan, authored_plan or plan})
    bindings = [store.path("bindings", kind, key + ".json") for key in keys]
    for path in bindings:
        if path.exists() and _read_json(path) != {"plan": path.stem, "transcript": transcript}:
            # Resolve against current evidence for a specific anchor error, but require
            # a new authored revision even if the old references happen to resolve.
            try:
                compile_current()
            except VPError as exc:
                raise _data(f"stale {kind} plan transcript revision: {exc.msg}") from exc
            raise _data(f"stale {kind} plan transcript revision; review and revise the plan against subtitles.srt")
        if not path.exists() and not accept:
            raise _data("stale long timeline: run vp long-plan for the current inputs")
    result = compile_current()
    if accept:
        for path in bindings:
            if not path.exists():
                write_json(path, {"plan": path.stem, "transcript": transcript})
    return result


def _reel_required(plan: dict) -> list[str]:
    _, item = next(iter(plan["items"].items()))
    required = list(_REEL_SETTINGS)
    split = item["layout"] == "split"
    if plan["preset"] == "owner_reel_810":
        if split:
            required.extend(f"split_810.{name}" for name in _SPLIT_810_SETTINGS)
        elif item["effects"]:
            required.extend(("split_810.caption_clearance_px", "split_810.caption_half_extra_px"))
    if split:
        required.extend(("screen_background.blur_sigma", "screen_background.brightness"))
    elif "speaker" not in plan["crops"].get(item.get("crop"), {}):
        required.append("medium_crop")
    if item["captions"]["mode"] == "local_phrase":
        required.append("allow_local_phrase")
    if "subline" in item["cards"]:
        required.append("subline_max_chars")
    if item["effects"]:
        required.extend(("face_box", "protected_boxes"))
    for effect in item["effects"]:
        kind = effect["type"]
        style = f"effect_styles.{kind}"
        if "duration_ms" not in effect:
            required.append(f"default_effect_duration_ms.{kind}")
        if kind == "zoom":
            if "scale" not in effect:
                required.append(style + ".scale")
        elif kind == "shake":
            required.append(style + ".amplitudePx")
        else:
            required.append(style + ".box")
            if "slot" not in effect:
                required.append(style + ".slot")
        if "asset" in effect:
            required.extend(("asset_root", f"effect_assets.{effect['asset']}"))
    return ["reel." + name for name in required]


def _copy_for_reel(inputs: Inputs, plan: dict, item: str, request: dict, fragment: dict) -> str | None:
    job = inputs.job
    cards = plan["items"][item]["cards"]
    design = None
    if "intro" not in cards or "thumbnail" not in cards:
        design = inputs.mapping(job.design_texts(item), optional=True)
    if design is not None:
        manifest = _stage_manifest(inputs, S6A, item)
        _check_stage_file(inputs, manifest, "outputs", job.design_texts(item))
        _check_stage_file(inputs, manifest, "inputs", job.clip_srt(item))
        if inputs.hashes[job.clip_srt(item)] == MISSING:
            raise _data(f"missing accepted S6a clip subtitles for {item}")
        cut = _stage_manifest(inputs, S4, item)
        _check_stage_file(inputs, cut, "outputs", job.clip_srt(item))
        _check_stage_file(inputs, cut, "inputs", job.source_video)
        _check_stage_file(inputs, cut, "inputs", job.subtitles_srt)
        accepted_cut = {key: fragment[key] for key in ("id", "start_ms", "end_ms")} | {"kind": "reel"}
        if cut.get("config", {}).get("fragment") != accepted_cut:
            raise _data(f"stale S6a clip range for {item}")
        if design.get("item") != item or design.get("kind") != "reel":
            raise _data(f"invalid accepted S6a copy for {item}")
    authors = author_fields(request, item)
    inherited = {}
    values = {}
    for role, name in zip(("intro", "thought", "thumbnail"), DESIGN_FIELDS):
        if role not in cards:
            if name in (design or {}):
                cards[role] = inherited[name] = design[name]
            elif name in authors:
                cards[role] = authors[name].value
        if role in cards:
            value = cards[role]
            if reason := text_violation(name, value):
                raise _data(f"{item} cards.{role}: invalid copy ({reason})")
            values[name] = value
    if violations := check_author_fields(values, request, item, ignore_link_block=False):
        raise _data(f"{item} cards: {violations[0]['field']} violates author protection")
    return sha256_obj(inherited) if inherited else None


def _reel_policy(plan: dict, item: str, settings: dict, assets: dict):
    row = plan["items"][item]
    _validate_settings(settings, "reel", ints=("sample_rate", "audio_close_ms", "intro_readable_dwell_frames",
                                               "thought_readable_dwell_frames", "max_audio_lag_ms", "quiet_tail_ms"),
                       nums=("min_audio_correlation", "min_card_ssim"), bools=("caption_plate", "show_body_plate"))
    if row["captions"]["mode"] == "local_phrase" and settings["allow_local_phrase"] is not True:
        raise _data("builders.reel.allow_local_phrase must be true for a whole-phrase reel")
    if "subline" in row["cards"]:
        limit = settings["subline_max_chars"]
        if type(limit) is not int or not 0 < len(row["cards"]["subline"]) <= limit:
            raise _data(f"{item} cards.subline exceeds builders.reel.subline_max_chars")
    preset = REEL_PRESETS[plan["preset"]]
    kinds = {effect["type"] for effect in row["effects"]}
    default_durations = settings.get("default_effect_duration_ms", {})
    effect_styles = settings.get("effect_styles", {})
    layout = settings.get("split_810", {}) if plan["preset"] == "owner_reel_810" else {}
    split = row["layout"] == "split"
    if layout:
        used = _SPLIT_810_SETTINGS if split else ("caption_clearance_px", "caption_half_extra_px")
        layout = {key: layout[key] for key in used if key in layout}
        _validate_settings(layout, "reel.split_810", ints=tuple(layout))
    if split:
        params = settings["screen_background"]
        _validate_settings(params, "reel.screen_background", nums=("blur_sigma", "brightness"))
        background = ScreenBackground(params["blur_sigma"], params["brightness"])
    else:
        background = ScreenBackground(preset.medium_background_blur_sigma, preset.medium_background_brightness)
    compile_policy = reel_plan.ReelCompileSettings(
        settings["sample_rate"], settings["audio_close_ms"], settings["intro_readable_dwell_frames"],
        settings["thought_readable_dwell_frames"],
        {kind: default_durations[kind] for kind in kinds if kind in default_durations},
        split_810=layout if split and layout else None,
        effect_styles={kind: effect_styles[kind] for kind in kinds if kind in effect_styles},
        effect_assets={name: str(path[0]) for name, path in assets.items()},
        speaker_background=not split,
    )
    geometry = EffectGeometry(
        face=settings["face_box"] if kinds else None,
        cards=tuple(settings["protected_boxes"]) if kinds else (),
        caption_clearance_px=layout.get("caption_clearance_px"),
        caption_half_extra_px=layout.get("caption_half_extra_px"),
        split_speaker_height_px=layout.get("split_speaker_height_px"),
    )
    qa = ProductionQAPolicy.for_reel(preset, revision=_REVISION,
        **{name: settings[name] for name in ("max_audio_lag_ms", "min_audio_correlation",
                                             "min_card_ssim", "quiet_tail_ms",
                                             "intro_readable_dwell_frames", "thought_readable_dwell_frames")})
    return compile_policy, geometry, background, qa


def _assets(inputs: Inputs, plan: dict, item: str, settings: dict) -> dict:
    ids = {effect["asset"] for effect in plan["items"][item]["effects"] if "asset" in effect}
    if not ids:
        return {}
    root = Path(settings["asset_root"]).expanduser().resolve()
    out = {}
    for asset_id in sorted(ids):
        name = settings["effect_assets"][asset_id]
        if not isinstance(name, str) or not re.fullmatch(r"(?:[\w.-]+/)*[\w.-]+", name):
            raise _data(f"invalid relative Remotion asset path: {asset_id}")
        rel = Path(name)
        src = (root / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or not src.is_relative_to(root) or not src.is_file():
            raise _data(f"missing or unsafe local Remotion asset: {asset_id} ({name})")
        # Namespace generated video and externally supplied assets independently.
        out[asset_id] = (Path("effects") / rel, src)
        inputs.remember(src, _file_digest(src))
    return out


def _compile_reel(store, inputs, plan, item, source, info, tools, records, cues, words, words_digest,
                  transcript, settings, compile_policy, inherited, authored_digest):
    item_plan = plan["items"][item]
    preset = REEL_PRESETS[plan["preset"]]
    pauses = _silences(store, source, info, tools, preset.silence_db, min(preset.pause_lengths_ms))
    ids = {item} | {ref["fragment"] for ref in item_plan.get("pieces", []) if "fragment" in ref}
    if missing := ids - records.keys():
        raise _data(f"{item}: unknown accepted fragments: {', '.join(sorted(missing))}")
    ranges = {key: (records[key]["start_ms"], records[key]["end_ms"]) for key in sorted(ids)}
    approved = {key: records[key]["type"] for key in sorted(ids)}
    accepted_digest = sha256_obj([ranges, approved])
    effective = plan_digest(plan)
    crop = settings.get("medium_crop") if "speaker" not in plan["crops"].get(item_plan.get("crop"), {}) else None
    deps = {"plan": effective, "accepted": accepted_digest, "recording": info.media["sha256"], "tools": tools,
            "transcript": transcript, "words": words_digest, "inherited": inherited,
            "policy": sha256_obj([asdict(compile_policy), crop]), "preset": sha256_obj(asdict(preset)),
            "compiler": reel_plan.COMPILER_REVISION}

    def compile_current():
        timeline = reel_plan.compile_reel(
            plan, item, accepted=ranges, cues=cues, word_times=words, source_id=_SOURCE_ID,
            source=info, pauses=pauses, settings=compile_policy, accepted_sha256=accepted_digest,
            transcript_sha256=transcript, plan_transcript_sha256=transcript, source_words_sha256=words_digest,
            word_times_transcript_sha256=transcript if words else None, medium_crop=crop,
        )
        if inherited is not None:
            timeline["inputs"]["design_texts"] = inherited
        inputs.verify()
        return timeline

    return _compile_with_binding(store, "reel", effective, transcript,
                                 lambda: store.json_cache("reel-compile", deps, compile_current),
                                 authored_plan=authored_digest)


def _frame_count(span: dict) -> int:
    return span["end"] - span["start"]


def _timing(timeline: dict, count: int) -> dict:
    rate = timeline["frame_rate"]
    return {**timeline["canvas"], "fps": rate["num"] / rate["den"], "durationInFrames": count}


def _reel_props(timeline: dict, brand: dict, settings: dict) -> tuple[dict, dict]:
    pieces = [row for row in timeline["elements"] if row["kind"] == "piece"]
    origin = pieces[0]["output"]["frames"]["start"]
    count = pieces[-1]["output"]["frames"]["end"] - origin
    words = {(row["element_id"], row["word_id"]): row for row in timeline["words"]}
    pages = []
    for row in timeline["captions"]:
        span = row["output"]["frames"]
        start, end = span["start"] - origin, span["end"] - origin
        if end <= start:
            continue
        showing = []
        for word_id in row["word_ids"]:
            word = words[row["element_id"], word_id]
            bounds = word["output"]["frames"]
            first = min(end, max(start, bounds["start"] - origin))
            last = min(end, max(first, bounds["end"] - origin))
            showing.append({"text": word["text"], "startFrame": first, "endFrame": last})
        pages.append({"text": row["text"], "mode": row["mode"], "startFrame": start,
                      "endFrame": end, "words": showing})
    effects = []
    for row in timeline["effects"]:
        props = dict(row["props"])
        props["startFrame"] -= origin
        props["endFrame"] -= origin
        effects.append(props)
    preset = REEL_PRESETS[timeline["preset"]]
    body = {**_timing(timeline, count), "kind": "reel_body", "video": "input.mp4",
            "brand": brand, "showPlate": settings["show_body_plate"], "layout": timeline["layout"]["mode"],
            "captions": pages, "effects": effects, "captionStyle": {
                "fontFrac": preset.caption_font_frac, "yFrac": timeline["caption_y_frac"],
                "plate": settings["caption_plate"],
            }}
    cards = {}
    for row in timeline["elements"]:
        if row["kind"] == "piece":
            continue
        card = row["card"]
        props = {**_timing(timeline, _frame_count(row["output"]["frames"])), "kind": "reel_card",
                 "role": row["kind"], "text": card["text"],
                 "wordRevealFrames": card.get("word_reveal_frames", []), "brand": brand,
                 # Design §1, Final card: "The job adds the channel plate".
                 "showPlate": row["kind"] == "thought"}
        if "subline" in card:
            props["subline"] = card["subline"]
        cards[row["id"]] = props
    return body, cards


class Draw:
    """Cache picture by local props, input bytes and the prepared renderer identity."""

    def __init__(self, store: Store, config: dict, renderer_revision: str, tools: str):
        self.store = store
        self.config = config
        self.brand = RemotionRenderer(config).brand_props()
        workspace = RemotionWorkspace(config)
        # The prepared renderer opens a shared workspace lock even though it never installs.
        store.paths.require_unwatched(workspace.base)
        store.paths.require_unwatched(workspace.dir)
        store.paths.require_unwatched(workspace.base / ".lock")
        runtime = {}
        if workspace.current():
            for name, finder in (("node_bin", find_node), ("browser_executable", find_browser)):
                if executable := finder(workspace.settings.get(name), workspace.home):
                    path = Path(executable).resolve()
                    stat = path.stat()
                    runtime[name] = [str(path), stat.st_size, stat.st_mtime_ns]
        self.identity = sha256_obj({
            "renderer_revision": renderer_revision, "source": source_hash(), "code": code_hash(),
            "remotion": config["remotion"], "runtime": runtime, "gl": render_gl(config), "tools": tools,
        })

    def cached(self, props: dict, *, video: Path | None = None, assets: dict | None = None,
               geometry: EffectGeometry = EffectGeometry()) -> Path:
        _validate(validate_renderer_props, props)
        assets = assets or {}
        deps = {"props": sha256_obj(props), "renderer": self.identity, "geometry": sha256_obj(asdict(geometry)),
                "video": _file_digest(video) if video is not None else None,
                "assets": {str(name): _file_digest(path) for name, path in assets.values()}}

        def produce(temp):
            public = temp / "public"
            public.mkdir()
            if video is not None:
                shutil.copyfile(video, public / props["video"])
            for name, src in assets.values():
                target = self.store.output(public, str(name))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)
            result = render_build(props, temp / "picture.mp4", public_dir=public,
                                  config=self.config, geometry=geometry)
            write_json(temp / "backend.json", {"gl_effective": result.gl_effective,
                                                "gl_fallback": result.gl_fallback})
            shutil.rmtree(public)

        result, _ = self.store.materialize("draw", deps, ("picture.mp4", "backend.json"), produce)
        return result


def render_build(props, out, *, public_dir, config, geometry):
    """Report absent dependencies before invoking the download-disabled adapter."""
    workspace = RemotionWorkspace(config)
    missing = missing_tools(config)
    if not workspace.current():
        missing.append(f"prepared Remotion CLI and current code in {workspace.dir}")
    if missing:
        raise _data("missing local render dependencies: " + ", ".join(missing))
    return draw_prepared_build(props, out, public_dir=public_dir, config=config, geometry=geometry)


def _reel_cut(store, timeline, source, tools, compile_policy, background):
    pieces = [row for row in timeline["elements"] if row["kind"] == "piece"]
    frames = pieces[0]["output"]["frames"]["start"]
    samples = pieces[0]["output"]["samples"]["start"]
    # Shift absolute positions out of the cut key. Card text, caption pages and
    # effects do not change the PCM or prepared camera/screen picture.
    def relative(element):
        return {axis: {edge: value - shift for edge, value in element[axis].items()}
                for axis, shift in (("frames", frames), ("samples", samples))}

    windows = [{**piece, "output": relative(piece["output"]),
                "events": [{**e, "output": relative(e["output"])} for e in piece["events"]]} for piece in pieces]
    switches = [{key: value for key, value in switch.items() if key != "word_id"} | {
        "output_frame": switch["output_frame"] - frames} for switch in timeline["crop_switches"]]
    filled_medium = timeline.get("speaker_background", False)
    if timeline["layout"]["mode"] == "medium" and not filled_medium:
        background = None
    cut_inputs = [
        timeline["inputs"]["sources"], timeline["canvas"], timeline["frame_rate"], timeline["sample_rate"],
        timeline["preset"], timeline["preset_revision"], timeline["layout"], switches, windows,
        timeline.get("source_start"), timeline.get("speaker_background"),
        compile_policy.split_810, asdict(background) if background else None,
    ]
    if filled_medium:
        cut_inputs.append({"medium_fill": 2})
    deps = {"tools": tools, "cut": sha256_obj(cut_inputs)}

    def produce(temp):
        pcm = temp / "body.mov"
        cut_reel(timeline, {_SOURCE_ID: source}, pcm, expected_inputs=timeline["inputs"],
                 split_810=compile_policy.split_810, screen_background=background)
        # A browser-ready picture-only view of the exact cut. Assembly uses the
        # original PCM, independent of Remotion and AAC priming.
        media.run_ffmpeg([*_FILE_PROTOCOLS, "-i", pcm, "-map", "0:v:0", "-an", "-c:v", "copy",
                          temp / "body.mp4"])

    directory, _ = store.materialize("reel-cut", deps, ("body.mov", "body.mp4"), produce)
    return directory


def _reel_assembly(store, timeline, cut, rendered_body, cards, tools):
    visual = [cards["intro"] / "picture.mp4"] if "intro" in cards else []
    visual += [rendered_body / "picture.mp4", cards["thought"] / "picture.mp4"]
    deps = {"tools": tools, "picture": [_file_digest(path) for path in visual],
            "pcm": _file_digest(cut / "body.mov"),
            "clock": sha256_obj([timeline["frame_rate"], timeline["sample_rate"],
                                 [(row["kind"], row["output"]) for row in timeline["elements"]]])}

    def produce(temp):
        args = [arg for path in [*visual, cut / "body.mov"] for arg in (*_FILE_PROTOCOLS, "-i", path)]
        # MP4/MOV PCM assembly needs no FFmpeg-generated time-based silence. Each
        # card contributes exactly its compiled number of samples at this clock.
        graph = []
        video = []
        audio = []
        rate = timeline["frame_rate"]
        fps = f"{rate['num']}/{rate['den']}"
        for i in range(len(visual)):
            graph.append(f"[{i}:v:0]setsar=1,setpts=PTS-STARTPTS[v{i}]")
            video.append(f"[v{i}]")
        graph.append("".join(video) + f"concat=n={len(video)}:v=1:a=0,setpts=N/({fps}*TB)[v]")
        pcm_index = len(visual)
        graph.append(f"[{pcm_index}:a:0]asetpts=N/SR/TB[body]")
        for i, row in enumerate(timeline["elements"]):
            if row["kind"] == "piece":
                if "[body]" not in audio:
                    audio.append("[body]")
                continue
            size = _frame_count(row["output"]["samples"])
            graph.append(f"[{pcm_index}:a:0]atrim=end_sample={size},volume=0,apad=whole_len={size},"
                         f"asetpts=N/SR/TB[a{i}]")
            audio.append(f"[a{i}]")
        graph.append("".join(audio) + f"concat=n={len(audio)}:v=0:a=1,asetpts=N/SR/TB[a]")
        media.run_ffmpeg(["-xerror", *args, "-filter_complex_threads", "1", "-filter_complex", ";".join(graph),
                          "-map", "[v]", "-map", "[a]", *media.VIDEO_ENC, "-c:a", "pcm_s24le",
                          "-fps_mode", "passthrough", "-video_track_timescale", str(rate["num"]),
                          temp / "premaster.mov"])

    result, _ = store.materialize("reel-assembly", deps, ("premaster.mov",), produce)
    return result / "premaster.mov"


def _master_reel(store, timeline, cut, premaster, policy, tools):
    names = ("target_lufs", "lufs_tolerance", "max_dbtp", "mastering_retries", "limiter_sample_rate")
    silent_samples = [_frame_count(row["output"]["samples"]) if row["kind"] != "piece" else 0
                      for row in timeline["elements"]]
    deps = {"tools": tools, "body": _file_digest(cut / "body.mov"), "clock": silent_samples,
            "sample_rate": timeline["sample_rate"],
            **{name: getattr(policy, name) for name in names}}

    def encode_audio(temp):
        mixed = temp / "master.mp4"
        master_audio(premaster, mixed, policy, sample_rate=timeline["sample_rate"])
        media.run_ffmpeg([*_FILE_PROTOCOLS, "-i", mixed, "-map", "0:a:0", "-c:a", "copy", temp / "audio.m4a"])
        mixed.unlink()

    encoded, _ = store.materialize("reel-audio", deps, ("audio.m4a",), encode_audio)
    audio = encoded / "audio.m4a"
    mux_deps = {"tools": tools, "picture": _file_digest(premaster), "audio": _file_digest(audio)}
    result, _ = store.materialize("reel-master", mux_deps, ("final.mp4",), lambda temp: media.run_ffmpeg([
        *_FILE_PROTOCOLS, "-i", premaster, *_FILE_PROTOCOLS, "-i", audio,
        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", temp / "final.mp4",
    ]))
    return result / "final.mp4"


def _build_result(directory: Path, hit: bool, item: str, artifacts: dict[str, str]) -> dict:
    return {"item": item, "cache_hit": hit, "directory": str(directory),
            "manifest": str(directory / _MANIFEST),
            "artifacts": {name: str(directory / filename) for name, filename in artifacts.items()}}


def _qa_report(record: dict, destination: Path, required: set[str]) -> None:
    _validate(validate_qa_record, record)
    write_json(destination / "qa.json", record)
    hard_failures = sorted(name for name, check in record["checks"].items() if check["status"] == "failed")
    unverified = sorted(name for name in required if record["checks"].get(name, {}).get("status") != "passed"
                        and name not in hard_failures)
    if record["kind"] == "long":
        report = format_long_report(record)
    else:
        lines = [f"# Local reel QA for {record['item']}", "",
                 f"Policy revision `{record['policy_revision']}`.", ""]
        for name, check in record["checks"].items():
            details = check.get("reason") or " ".join(check["evidence"])
            lines.append(f"- **{name.replace('_', ' ')} · {check['status']}**. {details}")
        report = "\n".join(lines) + "\n"
    atomic_write(destination / "report.md", report)
    if hard_failures or unverified:
        msg = []
        if hard_failures:
            msg.append("failed local QA: " + ", ".join(hard_failures))
        if unverified:
            msg.append("required local QA lacks evidence: " + ", ".join(unverified))
        raise _data(". ".join(msg))


_READY_ARTIFACTS = {"timeline": "timeline.json", "media": "final.mp4",
                    "qa": "qa.json", "report": "report.md", "renderers": "renderers.json"}


def _reel_audio_clock(final: Path, timeline: dict) -> dict:
    streams = _probe(final, "-select_streams", "a:0", "-show_entries",
                     "stream=sample_rate,time_base,duration_ts,start_pts").get("streams", [])
    expected_rate, expected_samples = timeline["sample_rate"], timeline["duration_samples"]
    actual = {}
    if streams:
        stream = streams[0]
        rate = int(stream["sample_rate"])
        duration = int(stream["duration_ts"]) * Fraction(stream["time_base"]) * rate
        actual = {"sample_rate": rate, "duration_samples": str(duration), "start_pts": int(stream["start_pts"])}
        passed = duration == expected_samples and rate == expected_rate and actual["start_pts"] == 0
    else:
        passed = False
    return {"status": "passed" if passed else "failed", "values": {
        "expected": {"sample_rate": expected_rate, "duration_samples": expected_samples, "start_pts": 0},
        "actual": actual}, "evidence": ["FFprobe AAC presentation clock on the assembled final MP4"]}


def _build_reel(store, inputs, timeline, source, tools, draw, assets, settings, compile_policy, geometry,
                background, qa):
    item = timeline["item"]
    body, card_props = _reel_props(timeline, draw.brand, settings)
    cut = _reel_cut(store, timeline, source, tools, compile_policy, background)
    cards = {role: draw.cached(props) for role, props in card_props.items()}
    rendered_body = draw.cached(body, video=cut / "body.mp4", assets=assets, geometry=geometry)
    premaster = _reel_assembly(store, timeline, cut, rendered_body, cards, tools)
    master = _master_reel(store, timeline, cut, premaster, qa, tools)
    backends = {role: _read_json(directory / "backend.json")
                for role, directory in {"body": rendered_body, **cards}.items()}
    deps = {"timeline": sha256_obj(timeline), "media": _file_digest(master), "props": sha256_obj(body),
            "renderer": draw.identity, "backends": sha256_obj(backends), "tools": tools,
            "cards": {role: _file_digest(directory / "picture.mp4") for role, directory in cards.items()},
            "policy": sha256_obj([asdict(qa), asdict(geometry)]), "active_picture": 2}
    if "source_start" in timeline:
        deps["source_start"] = timeline["source_start"]
    artifacts = {**_READY_ARTIFACTS, "body_props": "body.props.json"}

    def produce(temp):
        path = temp / "timeline.json"
        atomic_write(path, canonical_json(timeline))
        final = temp / "final.mp4"
        shutil.copyfile(master, final)
        write_json(temp / "body.props.json", body)
        write_json(temp / "renderers.json", backends)
        counts = {row["kind"]: _frame_count(row["output"]["frames"]) for row in timeline["elements"]
                  if row["kind"] != "piece"}
        # The cumulative frame clock can allocate one frame to an adjacent
        # occurrence at a half-frame tie. Check the realized card partition.
        effective_qa = replace(qa, intro_frames=counts.get("intro"), outro_frames=counts["thought"])
        record = measure_production(final, path, effective_qa, sources={_SOURCE_ID: source},
                                    card_references={role: directory / "picture.mp4" for role, directory in cards.items()},
                                    reel_body=body, effect_geometry=geometry)
        record["checks"]["audio_presentation_clock"] = _reel_audio_clock(final, timeline)
        required = {"picture_format", "active_picture", "loudness", "true_peak", "quiet_lead_in", "quiet_tail",
                    "outro_frames", "outro_text_dwell"}
        if "intro" in cards:
            required.update(("intro_frames", "intro_text_dwell"))
        if timeline["caption_mode"] == "aligned_required":
            required.update(("last_word_protection", "audio_offset"))
        if timeline["effects"]:
            required.add("effect_caption_clearance")
        _qa_report(record, temp, required)
        inputs.verify()

    directory, hit = store.materialize("reel", deps, tuple(artifacts.values()), produce, item=item, finished=True)
    return _build_result(directory, hit, item, artifacts)


def _long_policy(settings: dict) -> long_plan.LongCompilePolicy:
    _validate_settings(settings, "long", ints=_LONG_COMPILE_SETTINGS)
    if any(settings[key] <= 0 for key in ("width", "height", "fps_num", "fps_den", "sample_rate",
                                           "hook_end_text_lead_ms")) or settings["max_cut_shift_ms"] < 0:
        raise _data("builders.long needs positive canvas, clocks and hook lead, and a nonnegative cut displacement")
    if settings["width"] % 2 or settings["height"] % 2:
        raise _data("builders.long.width and builders.long.height must be even for local H.264")
    return long_plan.LongCompilePolicy(
        {"width": settings["width"], "height": settings["height"]},
        {"num": settings["fps_num"], "den": settings["fps_den"]}, settings["sample_rate"],
        settings["max_cut_shift_ms"], settings["hook_end_text_lead_ms"],
    )


def _long_inputs(store, plan, source, info, tools, accepted, records, words_digest, transcript, settings):
    item = plan["item"]
    approval = records.get(item)
    whole = accepted.get("whole_recording") or {}
    if item == FULL and whole.get("kind") != "reel":
        approval = {"id": FULL, "type": FULL,
                    "start_ms": whole.get("start_ms", 0), "end_ms": whole.get("end_ms", info.duration_ms)}
    if approval is None or approval["type"] not in ("long", "full"):
        raise _data(f"{item}: long-plan.yaml must name an accepted long or full item")
    refs = [plan.get("body"), *(cut["range"] for cut in plan["cuts"]), *plan["teaser"]]
    if "hook" in plan:
        refs.append(plan["hook"]["range"])
    referenced = {ref["fragment"] for ref in refs if ref and "fragment" in ref} - {item}
    if missing := referenced - records.keys():
        raise _data(f"{item}: unknown accepted fragments: {', '.join(sorted(missing))}")
    ranges = {key: (records[key]["start_ms"], records[key]["end_ms"]) for key in sorted(referenced)}
    detector = None
    quiet = []
    if plan["cuts"]:
        _validate_settings(settings, "long", ints=("quiet_min_ms",), nums=("quiet_db",))
        db, minimum = settings["quiet_db"], settings["quiet_min_ms"]
        detector = {"tools": tools, "db": db, "min_ms": minimum}
        quiet = _silences(store, source, info, tools, db, minimum)
    fingerprints = {"plan": plan_digest(plan), "accepted": sha256_obj([
                        {name: approval[name] for name in ("id", "type", "start_ms", "end_ms")}, ranges]),
                    "transcript": transcript, "source_words": words_digest,
                    "sources": {_SOURCE_ID: info.media}}
    if detector is not None:
        fingerprints["analysis"] = {"quiet-detector": sha256_obj(detector)}
    return approval, ranges, quiet, fingerprints


def _compile_long(store, inputs, plan, source, info, tools, accepted, records, cues, words, words_digest,
                  transcript, settings, *, accept: bool):
    policy = _long_policy(settings)
    approval, ranges, quiet, fingerprints = _long_inputs(
        store, plan, source, info, tools, accepted, records, words_digest, transcript, settings)
    deps = {**fingerprints, "tools": tools, "policy": sha256_obj(asdict(policy)),
            "compiler": long_plan.COMPILER_REVISION, "preset": long_plan.PRESET_REVISION}

    def compile_current():
        return long_plan.compile_long_plan(
            plan, approval, cues, words, inputs=fingerprints, plan_transcript_sha256=transcript,
            word_times_transcript_sha256=transcript if words else None, source_id=_SOURCE_ID,
            source_frames=info.frames, fragments=ranges, quiet_spans_ms=quiet, policy=policy,
        )

    def produce(temp):
        timeline = _compile_with_binding(store, "long", fingerprints["plan"], transcript, compile_current)
        inputs.verify()
        atomic_write(temp / "timeline.json", canonical_json(timeline))

    names = ("timeline.json",)
    if accept:
        binding = store.path("bindings", "long", fingerprints["plan"] + ".json")
        addr = store.address("long-plan", deps)
        if store.valid(addr, "long-plan", deps, names) and not binding.is_file():
            raise _data("compiled long plan is missing its transcript binding")
        if binding.is_file() and _read_json(binding) != {"plan": fingerprints["plan"], "transcript": transcript}:
            _compile_with_binding(store, "long", fingerprints["plan"], transcript, compile_current)
        directory, hit = store.materialize("long-plan", deps, names, produce, item=plan["item"])
        return _build_result(directory, hit, plan["item"], {"timeline": "timeline.json"})

    directory = store.address("long-plan", deps)
    current = _compile_with_binding(store, "long", fingerprints["plan"], transcript, compile_current, accept=False)
    if not store.valid(directory, "long-plan", deps, names):
        raise _data("missing, stale or tampered long timeline: run vp long-plan for the current inputs")
    candidate = _read_json(directory / "timeline.json")
    if candidate != current:
        raise _data("stale long timeline: compiled candidate differs from current local evidence")
    return candidate, current


def _long_render_policy(settings: dict, brand: dict):
    _validate_settings(settings, "long", ints=("audio_channels", "max_audio_step_s16", "max_audio_lag_ms",
                                               "intro_readable_dwell_frames", "outro_readable_dwell_frames",
                                               "audio_lag_grid_ms", "audio_lag_search_ms"),
                       nums=("timeout_s", "min_card_ssim", "target_lufs", "lufs_tolerance", "max_dbtp",
                             "min_audio_correlation"))
    if (settings["audio_channels"] not in (1, 2) or settings["timeout_s"] <= 0
            or settings["max_audio_step_s16"] < 0):
        raise _data("invalid builders.long audio channels, subprocess timeout or seam step limit")
    render = LongRenderPolicy(brand, settings["chapter_box"], settings["subscribe_box"],
                              settings["audio_channels"], settings["timeout_s"])
    qa = ProductionQAPolicy(
        revision=_REVISION, target_lufs=settings["target_lufs"], lufs_tolerance=settings["lufs_tolerance"],
        max_dbtp=settings["max_dbtp"], mastering_retries=0,
        limiter_sample_rate=settings["sample_rate"],  # unused for the verified lossless long track
        audio_lag_grid_ms=settings["audio_lag_grid_ms"], audio_lag_search_ms=settings["audio_lag_search_ms"],
        max_audio_lag_ms=settings["max_audio_lag_ms"], min_audio_correlation=settings["min_audio_correlation"],
        # Reel-only quiet windows are disabled and excluded from the long record.
        silence_db=-1, quiet_lead_ms=0, audio_open_ms=0, quiet_tail_ms=0, tail_picture_ms=0,
        min_card_ssim=settings["min_card_ssim"], intro_frames=None, outro_frames=None,
        intro_readable_dwell_frames=settings["intro_readable_dwell_frames"],
        outro_readable_dwell_frames=settings["outro_readable_dwell_frames"],
    )
    return render, qa


def _long_media(store, timeline, current, source, tools, draw, policy):
    refs = {"hook": "hook.mp4", "outro": "outro.mp4"}
    cards = {row["id"]: refs[row["kind"]] for row in timeline["elements"] if row["kind"] in refs}
    names = ("final.mp4", "render-evidence.json", "renderers.json", *cards.values())
    deps = {"timeline": sha256_obj(timeline), "renderer": draw.identity, "tools": tools,
            "policy": sha256_obj(asdict(policy))}

    def produce(temp):
        public = temp / "public"
        public.mkdir()
        backends = {}

        def draw_unit(props, dest):
            video = None
            normalized = dict(props)
            if props["video"] is not None:
                video = store.paths.destination(public / props["video"])
                normalized["video"] = "input.mp4"
            result = draw.cached(normalized, video=video)
            shutil.copyfile(result / "picture.mp4", store.paths.destination(dest))
            backends[dest.name] = _read_json(result / "backend.json")
            if props["kind"] == "long_card" and props["role"] in refs:
                shutil.copyfile(dest, temp / refs[props["role"]])

        evidence = render_long(timeline, {_SOURCE_ID: source}, temp / "final.mp4",
                               current_timeline=current, public_dir=public, draw=draw_unit, policy=policy)
        write_json(temp / "render-evidence.json", asdict(evidence))
        write_json(temp / "renderers.json", backends)
        public.rmdir()

    directory, _ = store.materialize("long-media", deps, names, produce)
    return directory, {key: directory / name for key, name in cards.items()}


def _build_long(store, inputs, timeline, current, source, tools, draw, settings):
    render, qa = _long_render_policy(settings, draw.brand)
    media_dir, cards = _long_media(store, timeline, current, source, tools, draw, render)
    deps = {"timeline": sha256_obj(timeline), "renderer": draw.identity, "tools": tools,
            "render": _file_digest(media_dir / _MANIFEST), "media": _file_digest(media_dir / "final.mp4"),
            "qa": sha256_obj([asdict(qa), settings["max_audio_step_s16"]])}
    artifacts = {**_READY_ARTIFACTS, "subtitles": "final.srt", "chapters": "chapters.json",
                 "render_evidence": "render-evidence.json"}

    def produce(temp):
        path, final = temp / "timeline.json", temp / "final.mp4"
        atomic_write(path, canonical_json(timeline))
        for name in ("final.mp4", "render-evidence.json", "renderers.json"):
            shutil.copyfile(media_dir / name, temp / name)
        srt = temp / "final.srt"
        atomic_write(srt, format_srt(long_plan.long_srt_cues(timeline), renumber=True))
        chapters = temp / "chapters.json"
        write_json(chapters, long_plan.long_chapters(timeline))
        record = build_long_qa(timeline, policy_revision=_REVISION, media_path=final,
                               render=LongRenderEvidence(**_read_json(temp / "render-evidence.json")),
                               delivered_srt=srt.read_text(encoding="utf-8"), delivered_chapters=_read_json(chapters),
                               max_audio_step_s16=settings["max_audio_step_s16"])
        production = measure_production(final, path, qa, sources={_SOURCE_ID: source}, card_references=cards)
        reel_only = {"quiet_lead_in", "quiet_tail", "effect_caption_clearance"}
        record["checks"].update({key: value for key, value in production["checks"].items() if key not in reel_only})
        required = {"evidence_binding", "frame_coverage", "picture_format", "srt_positions",
                    "chapter_positions", "loudness", "true_peak"}
        if len(timeline["video_units"]) > 1:
            required.update(("picture_seams", "audio_seams", "audio_seam_step"))
        if any(row["kind"] == "hook" for row in timeline["elements"]):
            required.update(("hook_timing", "intro_frames", "intro_text_dwell"))
        if any(row["kind"] == "outro" for row in timeline["elements"]):
            required.update(("outro_frames", "outro_text_dwell"))
        _qa_report(record, temp, required)
        inputs.verify()

    directory, hit = store.materialize("long-render", deps, tuple(artifacts.values()), produce,
                                      item=timeline["item"], finished=True)
    return _build_result(directory, hit, timeline["item"], artifacts)


def _config(inputs: Inputs, home: Path) -> tuple[dict, dict]:
    # Local builders read the owner's document without the legacy default
    # initializer or its writable/service context.
    document = inputs.mapping(home / "config.yaml", optional=True) or {}
    settings = document.get("builders", {})
    if not isinstance(settings, dict):
        raise _data("builders must be a mapping in config.yaml")
    remotion = document.get("remotion") or {}
    if not isinstance(remotion, dict):
        raise _data("remotion must be a mapping in config.yaml")
    effective = {key: document[key] for key in ("brand", "render") if key in document}
    effective["remotion"] = {**remotion, "allow_browser_download": False}
    effective["vp_home"] = str(home)
    return settings, effective


def _dispatch(command: str, args, job: Job, inputs: Inputs, settings: dict, config: dict, result: dict) -> None:
    request = inputs.mapping(job.request_yaml)
    required = ["watched_roots"]
    if command != "long-plan":
        required.append("renderer_revision")
    plans = {}
    long = None
    if command == "reel":
        document = inputs.mapping(job.reels_yaml)
        _validate(validate_reels, document)
        if args.item is not None and args.item not in document["items"]:
            raise _data(f"reels.yaml has no configured item {args.item}")
        for item in [args.item] if args.item is not None else sorted(document["items"]):
            plans[item] = normalize_reel_item(document, item)
            required.extend(_reel_required(plans[item]))
    else:
        document = inputs.mapping(job.long_plan_yaml)
        _validate(validate_long_plan, document)
        long = normalize_long_plan(document)
        required.extend("long." + name for name in _LONG_COMPILE_SETTINGS)
        if long["cuts"]:
            required.extend(("long.quiet_db", "long.quiet_min_ms"))
        if command == "long-render":
            required.extend("long." + name for name in _LONG_RENDER_SETTINGS)
    _require(settings, required)
    watched = settings["watched_roots"]
    if not isinstance(watched, list) or any(not isinstance(root, str) or not Path(root).expanduser().is_absolute()
                                           for root in watched):
        raise _data("builders.watched_roots must be a list of absolute filesystem paths")
    if command != "long-plan" and (not isinstance(settings["renderer_revision"], str)
                                   or not settings["renderer_revision"].strip()):
        raise _data("builders.renderer_revision must identify the prepared browser, renderer and fonts")
    paths = BuildPaths(job, watched)
    result["work_root"] = str(paths.root)
    store = Store(paths)
    lock = Job(paths.root)
    store.path("job.lock")
    if os.path.lexists(job.lock_file):
        result["status"] = "locked"
        return
    with job_lock(lock) as acquired:
        if not acquired or os.path.lexists(job.lock_file):
            result["status"] = "locked"
            return
        try:
            _run_locked(command, inputs, request, settings, config, result, store, plans, long)
        except (VPError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            details = dict(exc.details) if isinstance(exc, VPError) else {}
            existing = details.pop("diagnostics", None)
            failed = Path(existing) if existing else store.failed_directory("command")
            atomic_write(store.output(failed, "error.json"), canonical_json({
                "command": command, "error": str(exc), "details": details,
            }))
            result["diagnostics"] = str(failed)
            raise


def _run_locked(command, inputs, request, settings, config, result, store, plans, long):
    source, digest, accepted, cues, timing = _source_and_selection(inputs)
    records = _fragments(accepted)
    transcript = inputs.hashes[inputs.job.subtitles_srt]
    tools = _tool_fingerprint()
    info = _source_info(store, source, digest, tools)
    words, words_digest = _word_times(timing, store, cues, transcript, digest)
    draw = Draw(store, config, settings["renderer_revision"], tools) if command != "long-plan" else None
    builds = []
    if command == "reel":
        for item, plan in plans.items():
            if item not in records or records[item]["type"] != "reel":
                raise _data(f"{item}: reels.yaml item must name an accepted reel")
            authored = plan_digest(plan)
            inherited = _copy_for_reel(inputs, plan, item, request, records[item])
            _validate(validate_reels, plan)
            reel = settings["reel"]
            assets = _assets(inputs, plan, item, reel)
            compile_policy, geometry, background, qa = _reel_policy(plan, item, reel, assets)
            timeline = _compile_reel(store, inputs, plan, item, source, info, tools, records, cues, words,
                                     words_digest, transcript, reel, compile_policy, inherited, authored)
            builds.append(_build_reel(store, inputs, timeline, source, tools, draw, assets, reel,
                                      compile_policy, geometry, background, qa))
    else:
        if info.source_start:
            raise _data("long video builds require picture and sound to start at time zero")
        policy = settings["long"]
        planning = command == "long-plan"
        candidate = _compile_long(store, inputs, long, source, info, tools, accepted, records, cues,
                                  words, words_digest, transcript, policy, accept=planning)
        if planning:
            builds.append(candidate)
        else:
            timeline, current = candidate
            builds.append(_build_long(store, inputs, timeline, current, source, tools, draw, policy))
    inputs.verify()
    result["builds"] = builds


def _main(command: str, argv) -> int:
    parser = argparse.ArgumentParser(prog=f"vp {command}")
    parser.add_argument("job")
    if command == "reel":
        parser.add_argument("--item")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    job = Job(args.job)
    inputs = Inputs(job)
    result = {"command": command, "status": "ok", "work_root": None, "builds": []}
    rc = 0
    try:
        settings, config = _config(inputs, vp_home().resolve())
        _dispatch(command, args, job, inputs, settings, config, result)
    except VPError as exc:
        result.update(status="error", error=exc.msg)
        rc = 2
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        result.update(status="error", error=f"local build input or artifact error: {exc}")
        rc = 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["status"] == "locked":
        print("LOCKED")
    elif rc:
        print(f"ERROR {result['error']}")
        if result.get("diagnostics"):
            print(f"Diagnostics: {result['diagnostics']}")
    else:
        for build in result["builds"]:
            state = "CACHED" if build["cache_hit"] else "DONE"
            primary = build["artifacts"].get("media", build["artifacts"]["timeline"])
            print(f"{state} {build['item']} {primary}")
    return rc


def reel_main(argv=None) -> int:
    return _main("reel", argv)


def long_plan_main(argv=None) -> int:
    return _main("long-plan", argv)


def long_render_main(argv=None) -> int:
    return _main("long-render", argv)
