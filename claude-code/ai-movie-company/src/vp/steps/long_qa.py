"""Verify long-builder artifacts against their compiled positions and measured join."""
from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path

from ..adapters.long_render import LongRenderEvidence
from ..builders.contracts import LongTimeline, QACheck, QARecord, validate_qa_record, validate_timeline
from ..core.errors import VPError
from ..core.io import MISSING, sha256_file, sha256_obj
from ..core.srt import parse_srt
from .long_plan import HOOK_MS, ChapterMarker, long_chapters, long_srt_cues

_MEDIA_CHECKS = (
    "frame_coverage", "picture_seams", "audio_seams", "silent_spans", "audio_seam_step", "hook_timing",
)


def _check(ok: bool, values: dict, evidence: str, reason: str) -> QACheck:
    row: QACheck = {"status": "passed" if ok else "failed", "values": values, "evidence": [evidence]}
    if not ok:
        row["reason"] = reason
    return row


def _not_run(reason: str) -> QACheck:
    return {"status": "not_run", "values": {}, "evidence": [], "reason": reason}


def _srt_check(timeline, delivered):
    if delivered is None:
        return _not_run("No local SRT was supplied.")
    expected = long_srt_cues(timeline)
    try:
        observed = parse_srt(delivered, strip_speaker=False)
    except VPError as exc:
        return _check(False, {"expected_cues": len(expected)}, "Parsed the supplied SRT.", exc.msg)
    values = {"expected_cues": len(expected), "observed_cues": len(observed)}
    if observed != expected:
        values["different_cue_ids"] = [
            a.id for a, b in zip(expected, observed) if a != b
        ] + [row.id for row in expected[len(observed):] + observed[len(expected):]]
    return _check(observed == expected, values,
                  "Compared delivered SRT text and edges with the compiled output sample positions.",
                  "Delivered SRT differs from the compiled captions.")


def _chapter_check(timeline, delivered):
    if delivered is None:
        return _not_run("No delivered local chapter markers were supplied.")
    expected = long_chapters(timeline)
    observed = list(delivered)
    order = all(a["start_ms"] < b["start_ms"] for a, b in zip(expected, expected[1:]))
    return _check(expected == observed and order, {"expected": expected, "observed": observed},
                  "Compared delivered chapter markers with the retained-body overlay start frames.",
                  "Chapter markers differ from the compiled positions or share an output timestamp.")


def _render_checks(timeline, render, max_audio_step):
    planned = timeline["video_units"]
    measured_units = render.units
    coverage = (
        render.frame_count == timeline["duration_frames"]
        and render.sample_count == timeline["duration_samples"]
        and len(measured_units) == len(planned)
        and all(
            actual["id"] == unit["id"]
            and actual["start_frame"] == unit["output"]["frames"]["start"]
            and actual["end_frame"] == unit["output"]["frames"]["end"]
            and actual["start_sample"] == unit["output"]["samples"]["start"]
            and actual["end_sample"] == unit["output"]["samples"]["end"]
            and actual["frames"] == actual["end_frame"] - actual["start_frame"]
            and actual["samples"] == actual["end_sample"] - actual["start_sample"]
            for actual, unit in zip(measured_units, planned)
        )
    )
    samples = render.seams
    ordered = len(samples) == len(planned) - 1 and all(
        seam["left_unit"] == left["id"]
        and seam["right_unit"] == right["id"]
        and seam["frame"] == right["output"]["frames"]["start"]
        and seam["sample"] == right["output"]["samples"]["start"]
        for seam, left, right in zip(samples, planned, planned[1:])
    )
    picture = ordered and all(
        seam["expected_picture_sha256"] == seam["observed_picture_sha256"] == [
            left["last_frame_sha256"], right["first_frame_sha256"],
        ]
        for seam, left, right in zip(samples, measured_units, measured_units[1:])
    ) and coverage
    audio = ordered and all(
        seam["expected_audio_s16"] == seam["observed_audio_s16"] for seam in samples
    ) and coverage
    planned_silence = [event["output"]["samples"] for element in timeline["elements"]
                       for event in element["events"] if event["kind"] == "silence"]
    verified_silence = [span for row in measured_units for span in row["verified_silent_spans"]]
    checks = {
        "frame_coverage": _check(
            coverage, {"frames": render.frame_count, "samples": render.sample_count,
                       "join": render.join, "fallback_reason": render.fallback_reason, "units": measured_units},
            "Decoded final video frame hashes and PTS, and checked every unit's PCM against the joined track.",
            "Decoded frames, samples or unit boundaries differ from the planned partition.",
        ),
        "picture_seams": _check(
            picture, {"seams": [{k: row[k] for k in (
                "left_unit", "right_unit", "frame", "expected_picture_sha256", "observed_picture_sha256",
            )} for row in samples]},
            "Compared decoded picture at each seam with the last and first frames of the rendered neighboring units.",
            "Seam coverage or decoded boundary pictures differ.",
        ),
        "audio_seams": _check(
            audio, {"seams": [{k: row[k] for k in (
                "left_unit", "right_unit", "sample", "expected_audio_s16", "observed_audio_s16", "audio_step_s16",
            )} for row in samples]},
            "Compared final decoded PCM against unit PCM and measured adjacent samples at every seam.",
            "Seam coverage or decoded boundary samples differ.",
        ),
        "silent_spans": _check(
            coverage and planned_silence == verified_silence,
            {"planned": planned_silence, "verified": verified_silence},
            "Checked every planned silent interval in PCM and verified its bytes in the joined track.",
            "A planned silent interval has sound or its measured coverage is incomplete.",
        ),
    }
    if coverage and len(planned) == 1 and not samples:
        for name in ("picture_seams", "audio_seams"):
            checks[name] = _not_run("This edit has one video unit and no seams.")
    if not samples and len(planned) == 1:
        checks["audio_seam_step"] = _not_run("This edit has one video unit and no seams.")
    elif max_audio_step is None:
        checks["audio_seam_step"] = _not_run("No maximum audio step policy was supplied.")
    else:
        steps = [row["audio_step_s16"] for row in samples]
        violations = [row["sample"] for row in samples if row["audio_step_s16"] > max_audio_step]
        checks["audio_seam_step"] = _check(
            audio and not violations,
            {"limit_s16": max_audio_step, "maximum_s16": max(steps, default=0), "violations": violations},
            "Measured the largest absolute adjacent-sample step per channel at each seam.",
            "A seam exceeds the audio step policy or its audio evidence failed.",
        )
    hook = timeline["elements"][0]
    if hook["kind"] != "hook":
        checks["hook_timing"] = _not_run("The compiled edit has no hook.")
    else:
        rate = timeline["frame_rate"]
        target = round(Fraction(HOOK_MS * rate["num"], 1000 * rate["den"]))
        actual = measured_units[0]["frames"] if measured_units else 0
        checks["hook_timing"] = _check(
            coverage and actual >= target,
            {"target_frames": target, "actual_frames": actual, "overage_frames": max(0, actual - target)},
            "Compared the measured hook frames with the owner preset target.",
            "Hook duration differs from the compiled or minimum preset duration.",
        )
    return checks


def build_long_qa(
    timeline: LongTimeline, *, policy_revision: str, media_path: Path | None = None,
    render: LongRenderEvidence | None = None, delivered_srt: str | None = None,
    delivered_chapters: Sequence[ChapterMarker] | None = None,
    max_audio_step_s16: int | None = None,
) -> QARecord:
    """Check delivered artifacts and require local evidence for each measured result."""
    errors = validate_timeline(timeline)
    if errors or timeline["kind"] != "long":
        raise VPError("data", "\n".join(errors) or "expected a long timeline")
    if max_audio_step_s16 is not None and (type(max_audio_step_s16) is not int or max_audio_step_s16 < 0):
        raise VPError("data", "max_audio_step_s16 must be a nonnegative integer")
    timeline_hash = sha256_obj(timeline)
    media_hash = sha256_file(media_path) if media_path is not None else None
    if media_hash == MISSING:
        media_hash = None
    checks = {
        "srt_positions": _srt_check(timeline, delivered_srt),
        "chapter_positions": _chapter_check(timeline, delivered_chapters),
        "independent_speech_recognition": _not_run("No local independent recognition evidence was supplied."),
        "player_review": _not_run("No human playback, listening or graphic-legibility evidence was supplied."),
    }
    if render is not None and media_hash is not None:
        bound = render.timeline_sha256 == timeline_hash and render.media_sha256 == media_hash
        checks["evidence_binding"] = _check(
            bound, {"timeline_sha256": render.timeline_sha256, "media_sha256": render.media_sha256},
            "Hashed the compiled timeline and the local output file.",
            "Render measurements belong to a different timeline or media artifact.",
        )
        reason = "Render measurements are stale for the supplied artifacts."
    else:
        bound = False
        reason = "A local output file and its render measurements are required."
        checks["evidence_binding"] = _not_run(reason)
    if bound:
        checks.update(_render_checks(timeline, render, max_audio_step_s16))
    else:
        for name in _MEDIA_CHECKS:
            checks[name] = _not_run(reason)
    record: QARecord = {
        "version": 1, "item": timeline["item"], "kind": "long", "timeline_sha256": timeline_hash,
        "media_sha256": media_hash, "policy_revision": policy_revision, "checks": checks,
    }
    errors = validate_qa_record(record)
    if errors:
        raise VPError("data", "\n".join(errors))
    return record


def format_long_report(record: QARecord) -> str:
    """Render a human-readable Markdown summary from the QA wire record."""
    checks = record["checks"]
    lines = [f"# Long video QA for {record['item']}", "", f"Policy revision `{record['policy_revision']}`", ""]
    for key, check in checks.items():
        details = check.get("reason") or " ".join(check["evidence"])
        lines.append(f"- **{key.replace('_', ' ')} · {check['status']}**. {details}")
    if coverage := checks["frame_coverage"]["values"]:
        lines += [
            "", f"Measured {coverage['frames']} frames and {coverage['samples']} audio samples "
            f"using the {coverage['join'].replace('_', ' ')} join.",
        ]
        if coverage["fallback_reason"]:
            lines.append(f"Stream copy fallback reason — {coverage['fallback_reason']}")
    timing = checks["hook_timing"]["values"]
    if timing.get("overage_frames"):
        lines.append(f"The measured hook exceeded its target by {timing['overage_frames']} frames.")
    seams = checks["audio_seams"]["values"].get("seams", [])
    if seams:
        lines += ["", "| Join | Sample | Largest adjacent-sample step (s16) |", "|---|---:|---:|"]
        for row in seams:
            lines.append(f"| {row['left_unit']} → {row['right_unit']} | {row['sample']} | {row['audio_step_s16']} |")
    return "\n".join(lines) + "\n"
