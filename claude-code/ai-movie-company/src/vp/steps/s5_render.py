"""S5 — render (SPEC S5). Code: ctx.renderer (FfmpegRenderer in v1).

  reel       intro/outro cards (texts from design_texts.yaml), burned subtitles from clip.srt,
             channel plate
  long/full  intro/outro cards; subtitles NOT burned (final.srt goes to the platform, R7)
  all        loudness −14 LUFS ± 1 (two-pass loudnorm inside the renderer)

Outputs: render.mp4; render.manifest.yaml {kind, intro_ms, outro_ms, clip_ms, duration_ms,
lufs, renderer}; final.srt = clip.srt shifted by intro_ms — the only timeline anything outside
the clip refers to (t_final = t_clip + intro_ms).

Acceptance (code): duration = clip + cards ± 1 frame and equals the manifest; LUFS measured on
render.mp4 within tolerance; final.srt has no overlaps, its last cue ends no later than the end
of speech (intro_ms + clip duration) — cues are clipped there, never moved; the picture gate
(vp.adapters.picture.inspect over the clip part, cards excluded: frame size = the cut's, no black
bars, no black stretch, picture not frozen, no long silence) passes. Its report goes to
render.qa.yaml (also on failure) and its verdict into render.manifest.yaml `picture`.

Captions follow the voice (BACKLOG "Caption sync", incident 2026-11-23). Three parts, all code:

  word times   before a reel is rendered, the recording's word times are moved onto the clip's
               time base and aligned to the proofread text of clip.srt (vp.analysis.align_words)
               -> `clip.words.json` next to clip.srt. The latest approved source purchase
               takes precedence over older `_cut.json` and `captions.words.json` timings.
               A renderer that lights words up one by one reads the file.
  no word      nothing is sent anywhere. The pauses of the clip's own sound are found locally
  times        (ffmpeg) and the words of a phrase are put on its voiced time only — but only
               where the pauses leave little to guess; every other phrase is shown whole for
               its own time, where nothing inside it can drift (`word_times: estimated_voiced`,
               with the count of whole phrases). When not even the pauses can be measured,
               every phrase is whole (`word_times: estimated`). Both are logged
               (`captions_word_times_estimated`) and written into render.qa.yaml `captions`.
  the gates    `caption_voiced` — local, for captions without real word times: no caption may
               appear in a pause more than `caption_qa.lead_max_ms` before the sound comes
               back, and captions may not be on screen mostly over silence
               (`caption_qa.on_silence_max`). It sees pauses, not words.
               `caption_sync` — the words on screen against the words in the sound of
               render.mp4, heard by the engine's own recogniser (ctx.transcriber, the one behind
               `vp transcribe`). Fails the item when nine words in ten are not within
               `caption_qa.p90_ms` (0.3 s) or any word is further than `caption_qa.max_ms`
               (0.8 s). When the sound may not or cannot be recognised the check is `not_run`
               with the reason in plain words — never passed. Sending a reel's sound out is the
               person's decision: a receipt must cover this recording and checks must be enabled
               in `caption_qa.transcribe`.
"""
from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from yaml import YAMLError

from .. import transcribe
from ..adapters import media, picture
from ..adapters.render import _merge, measure_lufs, render_settings, video_info
from ..analysis import align_words
from ..core.config import DEFAULTS, deep_merge
from ..core.graph import FreshCheckFailed
from ..core.io import read_json, read_yaml, sha256_file, sha256_obj, write_yaml
from ..core.manifest import read_manifest
from ..core.srt import Cue, read_srt, write_srt
from ..core.status import Status
from .base import Ctx, StepSpec, VPError, item_kind
from .s4_cut import (fragment_record, key_overlay, key_waivers, picture_key_settings,
                     waived_checks, written_key_config)

CAPTION_QA_DEFAULTS: dict[str, Any] = {"transcribe": "auto", "p90_ms": align_words.SYNC_P90_MS,
                                       "max_ms": align_words.SYNC_MAX_MS,
                                       "min_words": DEFAULTS["caption_qa"]["min_words"],
                                       "min_share": DEFAULTS["caption_qa"]["min_share"],
                                       "lead_max_ms": align_words.LEAD_MAX_MS,
                                       "on_silence_max": align_words.ON_SILENCE_MAX}
LEGACY_CAPTION_QA_DEFAULTS: dict[str, Any] = {"transcribe": "auto", "p90_ms": 300, "max_ms": 800,
                                              "lead_max_ms": 1500, "on_silence_max": 0.5}
# The render key follows the 0.8.8 defaults and field selection, even when the
# current renderer gains new settings or changes its defaults.
BRAND_KEY_DEFAULTS = {
    "channel_plate": None, "font_file": None, "font_name": "Arial", "text_color": "#FFFFFF",
    "plate_color": "#000000", "card_bg": "#14181F", "card_text_color": "#FFFFFF",
}
RENDER_KEY_DEFAULTS = {
    "cards": {
        "reel": {"intro_ms": 1500, "outro_ms": 1500},
        "long": {"intro_ms": 3000, "outro_ms": 4000},
        "full": {"intro_ms": 3000, "outro_ms": 4000},
    },
    "lufs": -14.0, "lufs_tolerance": 1.0, "true_peak": -1.0, "lra": 11.0,
    "card_font_frac": 0.075, "plate_font_frac": 0.045, "reel_sub_font_size": 16,
    "reel_sub_margin_v": 40, "thumb_font_px": 72, "thumb_max_bytes": 2 * 1024 * 1024,
}
REMOTION_KEY_DEFAULTS = {
    "font_family": "-apple-system, 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif",
    "accent_color": "#FFD23F", "crf": 16,
    "captions": {"highlight": "word", "font_frac": 0.085, "y_frac": 0.72, "plate": False,
                 "max_words": 3, "max_chars": 18},
}
TRANSCRIBE_MODES = ("auto", "always", "off")
BURNED_KINDS = ("reel",)              # kinds whose captions are burned into the picture


def final_cues(clip_cues: list[Cue], intro_ms: int, clip_ms: int) -> list[Cue]:
    """clip.srt -> final timeline: shift by intro_ms, clip at the end of speech
    (intro_ms + clip_ms), drop cues that end up empty; renumbered 1..n by the writer."""
    speech_end = intro_ms + clip_ms
    out = []
    for c in clip_cues:
        s = c.shifted(intro_ms)
        if s.start_ms >= speech_end:
            continue
        if s.end_ms > speech_end:
            s = replace(s, end_ms=speech_end)
        if s.end_ms > s.start_ms:
            out.append(s)
    return out


def final_srt_violations(cues: list[Cue], intro_ms: int, clip_ms: int) -> list[str]:
    """Acceptance of final.srt: no overlaps, nothing before the intro end, last cue not after speech."""
    out = []
    for a, b in zip(cues, cues[1:]):
        if b.start_ms < a.end_ms:
            out.append(f"overlap {a.id}/{b.id}")
    if cues and cues[0].start_ms < intro_ms:
        out.append("cue_in_intro")
    if cues and cues[-1].end_ms > intro_ms + clip_ms:
        out.append("last_cue_after_speech")
    return out


def render_qa(job, item: str) -> Path:
    """Picture-gate report of the render (S5 output)."""
    return job.item_dir(item) / "render.qa.yaml"


# ---- captions: word times and the gate ---------------------------------------------------------

def caption_qa_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Config `caption_qa` over CAPTION_QA_DEFAULTS."""
    out = {**CAPTION_QA_DEFAULTS, **((config or {}).get("caption_qa") or {})}
    if out["transcribe"] is False:  # YAML 1.1 reads unquoted off as False
        out["transcribe"] = "off"
    if out["transcribe"] not in TRANSCRIBE_MODES:
        raise VPError("data", f"caption_qa.transcribe must be one of {list(TRANSCRIBE_MODES)}, "
                              f"got {out['transcribe']!r}")
    return out


def _caption_key_settings(overlay: dict[str, Any] | None) -> dict[str, Any]:
    out = {**LEGACY_CAPTION_QA_DEFAULTS, **(overlay or {})}
    if out["transcribe"] is False:  # YAML 1.1 reads unquoted off as False
        out["transcribe"] = "off"
    if out["transcribe"] not in TRANSCRIBE_MODES:
        raise VPError("data", f"caption_qa.transcribe must be one of {list(TRANSCRIBE_MODES)}, "
                              f"got {out['transcribe']!r}")
    return {**out, "words_schema": 1}


def clip_words(job, item: str) -> Path:
    """Word times of the clip, next to clip.srt (S5 output for kinds with burned captions)."""
    return align_words.words_path(job.clip_srt(item))


def _recording_words(ctx: Ctx, rec: dict[str, Any], cues: list[Cue], clip_ms: int
                     ) -> tuple[align_words.Alignment | None, transcribe.JobWordTimes | None, str]:
    """The selected recording timings aligned to the text of clip.srt."""
    j = ctx.job
    reason = ""
    for timing in transcribe._job_word_candidates(j, j.source_video):
        if timing.reason:
            reason = timing.reason if timing.reason == transcribe.LOCAL_RECORDING_CHANGED else reason or timing.reason
            continue
        if timing.owned and j.manifest_path("S4", ctx.item).exists():
            try:
                cut_recording_digest(ctx)
            except transcribe.RecognitionBlocked:
                reason = transcribe.LOCAL_RECORDING_CHANGED
                continue
        # 0.8.8 aligned every saved row, including those marked inferred. Keep those
        # caption positions while retaining the original origins in the timing reader.
        rows = [word[:3] for word in timing.words] if timing.legacy_rows else timing.words
        words = align_words.remap(rows, rec["start_ms"], rec["end_ms"],
                                 allow_legacy=timing.provenance == transcribe.LEGACY_UNVERIFIED)
        found = align_words.align(cues, words, clip_ms)
        if found.usable:
            return found, timing, ""
        reason = reason or (f"the word times of the recording do not match the subtitles "
                            f"({found.matched} of {found.total} words)")
    return None, None, reason or transcribe.NO_WORD_TIMES


def prepare_clip_words(ctx: Ctx, rec: dict[str, Any], clip_ms: int, *, sidecar: Path | None = None,
                       timing_inputs: str | None = None) -> dict[str, Any]:
    """Write `clip.words.json` for ctx.item and return what a report says about it
    (`align_words.caption_info`: {"word_times": "aligned" | "estimated_voiced" | "estimated", ...}).

    aligned            the recording's word times on the clip's time base, aligned to the
                       proofread text of clip.srt
    estimated_voiced   the recording has none (or they are not the words of this text): the
                       pauses of the clip's sound are found locally and the words are placed on
                       voiced time where that is safe; the other phrases are marked whole
    estimated          the pauses could not be measured either: every phrase is whole"""
    j, item = ctx.job, ctx.item
    srt = j.clip_srt(item)
    transcribe.guard_inputs(j, sidecar if sidecar is not None else align_words.words_path(srt))
    output = {"sidecar": sidecar, "timing_inputs": timing_inputs}
    cues = read_srt(srt)
    alignment, timing, reason = _recording_words(ctx, rec, cues, clip_ms)
    if alignment is not None:
        legacy = timing.provenance == transcribe.LEGACY_UNVERIFIED
        recording = transcribe.media_digest(j.source_video) if not legacy and j.source_video.is_file() else None
        cut = transcribe.media_digest(j.cut_mp4(item)) if recording and j.cut_mp4(item).is_file() else None
        align_words.write_clip_words(srt, alignment, timing.source, recording_sha256=recording, cut_sha256=cut,
                                     legacy_unverified=legacy, **output)
    else:
        # An independently verified 0.8.8 sidecar may be the only remaining timing file.
        if reason == transcribe.NO_WORD_TIMES:
            data = transcribe._document(align_words.words_path(srt))
            if data.get("schema") == 1 and data.get("word_times", "aligned") == "aligned":
                saved, info = align_words.read_clip_words(srt, cues)
                if saved is not None:
                    if sidecar is not None:
                        align_words._write_sidecar(srt, data, **output)
                    return align_words.caption_info(info, len(cues))
                reason = info.get("reason") or reason
        try:
            sound = media.pauses(j.cut_mp4(item), align_words.PAUSE_BELOW_MEAN_DB,
                                 align_words.PAUSE_MIN_MS, 0, clip_ms)
        except (VPError, OSError) as e:
            ctx.log.event("captions_pauses_unknown", item=item, error=getattr(e, "msg", str(e)))
            align_words.write_clip_estimate(
                srt, align_words.whole_phrases(cues), "estimated",
                f"{reason}; the pauses of the sound could not be measured, so every phrase is "
                f"shown whole", **output)
        else:
            voiced = align_words.voiced_intervals(sound["pauses"], clip_ms)
            est = align_words.estimate_voiced(cues, voiced)
            align_words.write_clip_estimate(
                srt, est, "estimated_voiced", reason,
                {"mean_db": sound["mean_db"], "noise_db": sound["noise_db"],
                 "pauses": len(sound["pauses"])}, **output)
    _, info = align_words.read_clip_words(srt, cues, sidecar=sidecar)
    return align_words.caption_info(info, len(cues))


def _cut_manifest_digests(ctx: Ctx) -> tuple[str | None, str | None]:
    """The S4 provenance used by S5, independent of manifest timestamps and subtitle inputs."""
    job = ctx.job
    try:
        manifest = read_manifest(job, "S4", ctx.item) or {}
        inputs, outputs = manifest.get("inputs"), manifest.get("outputs")
        if isinstance(inputs, dict) and isinstance(outputs, dict):
            source = inputs.get(job.rel(job.source_video))
            cut = outputs.get(job.rel(job.cut_mp4(ctx.item)))
            if isinstance(source, str) and isinstance(cut, str):
                return source, cut
    except (OSError, ValueError, YAMLError):
        pass
    return None, None


def cut_recording_digest(ctx: Ctx) -> str:
    """Verify which source the completed S4 cut used, including the exact cut bytes."""
    job = ctx.job
    expected_source, expected_cut = _cut_manifest_digests(ctx)
    try:
        if (expected_source and expected_cut and expected_source == transcribe.media_digest(job.source_video)
                and expected_cut == transcribe.media_digest(job.cut_mp4(ctx.item))):
            return expected_source
    except (OSError, ValueError):
        pass
    raise transcribe.RecognitionBlocked(transcribe.NO_APPROVAL,
                                       "the cut cannot be verified against the approved recording")


def _caption_timeline(renderer: Any, clip_srt: Path, clip_ms: int) -> dict[str, Any]:
    """What the renderer shows: {mode: word|phrase|mixed|none, word_times, words: [(text, start,
    end)], phrase: one flag per word (it belongs to a phrase shown whole), onsets: the moments
    something new appears, shows: [(start, end)] a caption is on screen}.
    A renderer that does not say is taken at the S5 contract: clip.srt burned phrase by phrase;
    one that gives the words only is completed from them."""
    fn = getattr(renderer, "caption_timeline", None)
    if fn is not None:
        out = dict(fn(clip_srt, clip_ms))
    else:
        words = [(w, c.start_ms, c.end_ms) for c in read_srt(clip_srt) for w in c.text.split()]
        out = {"mode": "phrase", "word_times": "phrase", "words": words}
    words = [(str(w), int(s), int(e)) for w, s, e in out.get("words") or ()]
    out["words"] = words
    if out.get("phrase") is None:
        out["phrase"] = [out.get("mode") == "phrase"] * len(words)
    if out.get("shows") is None:      # a whole phrase is on screen for its time, a word for its own
        out["shows"] = sorted({(s, e) for _, s, e in words})
    if out.get("onsets") is None:
        out["onsets"] = sorted({s for _, s, _ in words})
    return out


def caption_voiced(ctx: Ctx, shown: dict[str, Any], intro_ms: int, clip_ms: int, *,
                   render: Path | None = None) -> dict[str, Any]:
    """The local check: the captions of the finished video against the pauses of its own sound
    (status passed | failed | not_run). Nothing leaves the computer. It is for captions without
    real word times; captions timed by the recording's word times are judged by `sync`."""
    j, item = ctx.job, ctx.item
    if shown.get("word_times") == "aligned":
        return align_words.not_run("the captions are timed by the recording's word times; whether "
                                   "they follow the voice is the caption_sync check")
    st = caption_qa_settings(ctx.config)
    try:
        sound = media.pauses(render or j.render_mp4(item), align_words.PAUSE_BELOW_MEAN_DB,
                             align_words.PAUSE_MIN_MS, intro_ms, intro_ms + clip_ms)
    except (VPError, OSError) as e:
        ctx.log.event("caption_voiced_not_run", item=item, error=getattr(e, "msg", str(e)))
        return align_words.not_run("the pauses of the sound could not be measured, so the captions "
                                   "were not compared with them")
    report = align_words.voiced_report(shown["onsets"], shown["shows"], sound["pauses"])
    return align_words.voiced_verdict(report, lead_max_ms=int(st["lead_max_ms"]),
                                      on_silence_max=float(st["on_silence_max"]))


def _render_matches(path: Path, expected: str) -> bool:
    try:
        return transcribe.media_digest(path) == expected
    except (OSError, RuntimeError, transcribe.RecognitionBlocked):
        return False


def caption_check(ctx: Ctx, kind: str, intro_ms: int, clip_ms: int, *, render: Path | None = None,
                  render_sha256: str | None = None, timing_note: str | None = None,
                  timeline: dict[str, Any] | None = None) -> dict[str, Any]:
    """The `captions` section of render.qa.yaml: how the words were timed, `voiced` — the
    captions against the pauses of the sound of render.mp4 (local) — and `sync` — the words on
    screen against the words heard in render.mp4 (each with status passed | failed | not_run).
    Never raises for a check that could not run: that is `not_run` with the reason."""
    shown, captions = _caption_picture(ctx, kind, intro_ms, clip_ms, render=render,
                                       timing_note=timing_note, timeline=timeline)
    if shown["words"]:
        settings = caption_qa_settings(ctx.config)
        captions["sync"] = _sync_verdict(_measure_sync(ctx, shown, intro_ms, render, render_sha256), settings)
    return captions


def _caption_picture(ctx: Ctx, kind: str, intro_ms: int, clip_ms: int, *, render: Path | None,
                     timing_note: str | None, timeline: dict[str, Any] | None
                     ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prepare the on-screen timeline and the local caption QA for a new render."""
    if kind not in BURNED_KINDS:
        why = ("captions are not burned into this kind of video: its subtitles go to the platform "
               "as a separate track")
        return {"words": []}, {"mode": "none", "word_times": "none", "voiced": align_words.not_run(why),
                               "sync": align_words.not_run(why)}
    shown = dict(timeline) if timeline is not None else _caption_timeline(ctx.renderer, ctx.job.clip_srt(ctx.item), clip_ms)
    captions = {k: v for k, v in shown.items() if k not in ("words", "phrase", "onsets", "shows")}
    if timing_note:
        captions["local_timing_note"] = timing_note
    if shown.get("mode") == "none" or not shown["words"]:
        why = "no captions are burned into this video"
        return {"words": []}, {**captions, "voiced": align_words.not_run(why), "sync": align_words.not_run(why)}
    captions["voiced"] = caption_voiced(ctx, shown, intro_ms, clip_ms, render=render)
    settings = caption_qa_settings(ctx.config)
    why = ("the paid caption check is switched off" if settings["transcribe"] == "off" else
           transcribe.approval_reason(ctx.job) if not transcribe.approval_path(ctx.job).exists() else
           "see caption.sync.yaml for the check of this render")
    captions["sync"] = align_words.not_run(why)
    return shown, captions


def _measure_sync(ctx: Ctx, shown: dict[str, Any], intro_ms: int, render: Path | None,
                  render_sha256: str | None) -> dict[str, Any]:
    """Measure against verified audio. The recognition ledger owns receipt and retry enforcement."""
    j, item = ctx.job, ctx.item
    public = j.render_mp4(item)
    render = render or public
    if caption_qa_settings(ctx.config)["transcribe"] == "off":
        return align_words.not_run("the paid caption check is switched off")
    try:
        has_receipt = transcribe.approval_path(j).exists()
        if not has_receipt and not transcribe.saved_for_reel(j):
            raise transcribe.RecognitionBlocked(transcribe.NO_APPROVAL, transcribe.approval_reason(j))
        recording_sha256 = cut_recording_digest(ctx)
        language = transcribe.language_hint(j)
        if has_receipt:
            try:
                language = transcribe.require_reel_receipt(j, recording_sha256, item)
            except transcribe.RecognitionBlocked as exc:
                if exc.reason != transcribe.NO_APPROVAL or not transcribe.saved_for_reel(j):
                    raise
        if render_sha256 is None:
            raise transcribe.RecognitionBlocked(transcribe.NO_APPROVAL, "there is no verified S5 render for this check")
        if (not _render_matches(public, render_sha256)
                or (render != public and not _render_matches(render, render_sha256))):
            raise transcribe.RecognitionBlocked(transcribe.RENDER_MISMATCH, transcribe.RENDER_CHANGED)
        heard = ctx.transcriber.words(j, render, recording_sha256, language,
                                      render_sha256=render_sha256, item=item)
    except transcribe.RecognitionBlocked as e:
        return {**align_words.not_run(e.msg), "reason_code": e.reason,
                **({"request_attempted": True} if e.attempted else {})}
    except (VPError, OSError, ValueError) as e:
        ctx.log.event("caption_check_not_run", item=item, error=getattr(e, "msg", str(e)))
        return align_words.not_run(f"the caption check could not run: {getattr(e, 'msg', str(e))}")
    return align_words.sync_report(shown["words"], heard, mode=shown["mode"], offset_ms=intro_ms,
                                   phrase=shown["phrase"] if shown["mode"] == "mixed" else None)


def _sync_verdict(measurement: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    if "status" in measurement:
        return measurement
    return align_words.sync_verdict(measurement, p90_ms=int(settings["p90_ms"]),
                                    max_ms=int(settings["max_ms"]), min_words=int(settings["min_words"]),
                                    min_share=float(settings["min_share"]))


def caption_sync_check(captions: dict[str, Any]) -> dict[str, Any]:
    """`captions.sync` as a check of the QA report: ok True (passed) | False (failed) | None
    (not run — neither), the measurements, and the reason in plain words."""
    sync = captions["sync"]
    value = {k: sync[k] for k in ("median_ms", "p90_ms", "max_ms", "compared", "words", "coverage") if k in sync}
    check = {"ok": {"passed": True, "failed": False}.get(sync["status"]), "status": sync["status"],
             "value": value or None, "detail": sync["reason"]}
    if "reason_code" in sync:
        check["reason"] = sync["reason_code"]
    return check


def caption_voiced_check(captions: dict[str, Any]) -> dict[str, Any]:
    """`captions.voiced` as a check of the QA report (same shape as `caption_sync_check`)."""
    v = captions["voiced"]
    value = {k: v[k] for k in ("max_lead_ms", "on_silence_share", "captions", "pauses") if k in v}
    return {"ok": {"passed": True, "failed": False}.get(v["status"]), "status": v["status"],
            "value": value or None, "detail": v["reason"]}


def caption_note(kind: str, captions: dict[str, Any]) -> str | None:
    """One plain sentence for the person when the captions of a reel are not backed by a
    measurement (no real word times and/or a check that did not run)."""
    if kind not in BURNED_KINDS or captions.get("mode") == "none":
        return None
    parts = []
    if captions.get("word_times") == transcribe.LEGACY_UNVERIFIED:
        parts.append(transcribe.LEGACY_TIMING_NOTE)
    if captions.get("word_times") in align_words.ESTIMATES:
        n, whole = captions.get("cues"), captions.get("phrase_cues")
        how = "each phrase is shown whole for its own time"
        if n is not None and whole is not None and whole < n:
            how = (f"{whole} of {n} phrases are shown whole for their own time, the rest word by "
                   f"word by the pauses of the sound")
        parts.append(f"the captions are timed by estimate: {how} ("
                     + str(captions.get("reason") or "no word times") + ")")
    if captions.get("estimated_cues"):
        parts.append(f"estimated timing for {captions['estimated_words']} words in "
                     f"{captions['estimated_cues']} of {captions['cues']} phrases")
    timing_note = captions.get("local_timing_note")
    if timing_note and (not parts or timing_note not in parts[0]):
        parts.append(timing_note)
    sync = captions["sync"]
    if sync["status"] == "not_run":
        parts.append("the captions were not checked against the voice: " + sync["reason"])
    return "; ".join(parts) or None


def caption_sync_report(job, item: str) -> Path:
    return job.item_dir(item) / "caption.sync.yaml"


def _check_receipt(job, item: str, digest: str, *, snapshot: bool) -> str | dict[str, Any] | None:
    path = transcribe.approval_path(job)
    if not path.exists():
        return None
    document = transcribe._document(path)
    if not transcribe.reel_scope_covers(document.get("reel_scope", "all"), item):
        return None
    if not isinstance(document.get("receipt_id"), str):
        return sha256_file(path)
    receipt = {"receipt_id": document["receipt_id"]}
    revision = transcribe.reel_retry_revision(job, item, digest, receipt["receipt_id"], snapshot=snapshot)
    if revision is not None:
        receipt["retry_revision"] = revision
    return receipt


def _caption_verdict(report: dict[str, Any]) -> dict[str, Any]:
    return (report.get("checks") or {}).get("caption_sync") or {}


def _unsaved_caption_failure(job, item: str, digest: str) -> dict[str, Any]:
    """Recover a failed measurement recorded in status after its sidecar write failed."""
    for entry in reversed(Status(job).entries("S5", item)):
        if entry["state"] == "done":
            break
        details = (entry.get("metadata") or {}).get("details") or {}
        report = details.get("unsaved_caption_failure") or {}
        if report.get("render_sha256") != digest:
            continue
        prior = transcribe._document(caption_sync_report(job, item), yaml=True)
        if details.get("previous_report_sha256") == sha256_obj(prior):
            return report
        break
    return {}


def _saved_caption_report(job, item: str, outputs: Mapping[str, str]) -> tuple[Path, dict[str, Any]]:
    """Read the verdict bound to these verified S5 output bytes, including 0.8.8 QA."""
    path = caption_sync_report(job, item)
    digest = outputs.get(job.rel(job.render_mp4(item)))
    if not digest:
        return path, {}
    unsaved = _unsaved_caption_failure(job, item, digest)
    if unsaved:
        return path, unsaved
    prior = transcribe._document(path, yaml=True)
    if prior.get("render_sha256") != digest:
        prior = {}
    if _caption_verdict(prior).get("status") in ("passed", "failed"):
        return path, prior
    qa_path = render_qa(job, item)
    qa_hash = outputs.get(job.rel(qa_path))
    if qa_hash and sha256_file(qa_path) == qa_hash:
        qa = transcribe._document(qa_path, yaml=True)
        if (qa.get("render_sha256") in (None, digest)
                and _caption_verdict(qa).get("status") in ("passed", "failed")):
            return qa_path, qa
    return path, prior


def saved_caption_check(job, item: str, outputs: Mapping[str, str]) -> dict[str, Any]:
    """Return the report for a render whose S5 outputs have been verified."""
    return _saved_caption_report(job, item, outputs)[1]


def saved_caption_failure(job, item: str, outputs: Mapping[str, str]) -> str | None:
    """Recover a durable blocking verdict even if status recording was interrupted."""
    path, report = _saved_caption_report(job, item, outputs)
    check = _caption_verdict(report)
    if check.get("status") != "failed":
        return None
    if not _render_matches(job.render_mp4(item), outputs[job.rel(job.render_mp4(item))]):
        return None
    if "caption_sync" in (report.get("waived") or []) and "caption_sync" in waived_checks(Ctx(job, item, {})):
        return None
    return _caption_failure(job, item, path, check)


def _caption_failure(job, item: str, path: Path, check: dict[str, Any]) -> str:
    return f"{item} render ({job.rel(path)}): picture check failed — caption_sync: {check['detail']}"


def _rendered_timeline(ctx: Ctx, rendered: dict[str, Any], qa: dict[str, Any],
                       outputs: Mapping[str, str]) -> dict[str, Any] | None:
    """Recover what the verified render showed using its own saved outputs."""
    j, item = ctx.job, ctx.item
    srt = j.clip_srt(item)
    if "caption_timeline" in qa:
        return qa["caption_timeline"]
    if rendered.get("renderer") == "FfmpegRenderer":
        return _caption_timeline(None, srt, rendered["clip_ms"])
    if rendered.get("renderer") != "RemotionRenderer" or j.rel(clip_words(j, item)) not in outputs:
        return None

    from ..adapters.render_remotion import RemotionRenderer, caption_schedule
    renderer = RemotionRenderer(ctx.config)
    clip_ms = rendered["clip_ms"]
    highlight = renderer.rs["captions"]["highlight"]
    if highlight == "none":
        return {"words": []}
    legacy = "caption_display" not in rendered
    if legacy:
        # The stage has already verified the old sidecar's bytes. Read the saved caption data
        # directly so current recording-timing eligibility cannot change the old timeline.
        data = read_json(clip_words(j, item), default={}) or {}
        cues = read_srt(srt)
        tokens = renderer._caption_tokens(cues, clip_ms, data.get("cues"), data)
    else:
        tokens = renderer.captions(srt, clip_ms)
    fps = video_info(j.cut_mp4(item))["fps"]
    schedule = caption_schedule(tokens, fps, max(1, round(clip_ms * fps / 1000)), highlight,
                                legacy=legacy)
    return renderer._caption_timeline(schedule, fps)


def _measure_rendered_sync(ctx: Ctx, outputs: Mapping[str, str], digest: str,
                           rendered: dict[str, Any], qa: dict[str, Any]) -> dict[str, Any]:
    render = ctx.job.render_mp4(ctx.item)
    try:
        shown = _rendered_timeline(ctx, rendered, qa, outputs)
        if shown is None:
            return align_words.not_run("the caption timeline of this render could not be verified")
        if not shown["words"]:
            return align_words.not_run("no captions are burned into this video")
        # Bind the request to the verified inode even if the public name is replaced.
        with TemporaryDirectory(prefix=".vp-caption-check-", dir=render.parent) as folder:
            snapshot = Path(folder) / render.name
            try:
                snapshot.hardlink_to(render)
            except OSError:
                shutil.copy2(render, snapshot)
            measurement = _measure_sync(ctx, shown, rendered["intro_ms"], snapshot, digest)
        if not _render_matches(render, digest):
            return {**align_words.not_run(transcribe.RENDER_CHANGED),
                    "reason_code": transcribe.RENDER_MISMATCH}
        return measurement
    except (OSError, VPError, ValueError, TypeError, KeyError, YAMLError) as exc:
        return align_words.not_run(f"the caption check could not run: {exc}")


def _reusable_measurement(measurement: Any) -> bool:
    return isinstance(measurement, dict) and (
        "compared" in measurement or measurement.get("request_attempted") is True
        or measurement.get("reason_code") == transcribe.REEL_RETRY_REQUIRED)


def _check_key(ctx: Ctx, digest: str, settings: dict[str, Any], *, snapshot: bool = False) -> dict[str, Any]:
    receipt = (_check_receipt(ctx.job, ctx.item, digest, snapshot=snapshot)
               if settings["transcribe"] != "off" else None)
    return {"render_sha256": digest, "receipt": receipt, "waived": "caption_sync" in waived_checks(ctx),
            "settings": {k: settings[k] for k in ("transcribe", "p90_ms", "max_ms", "min_words", "min_share")}}


def _measurement_for(prior: dict[str, Any], key: dict[str, Any]) -> dict[str, Any] | None:
    evidence = prior.get("evidence") or {}
    saved = evidence.get("measurement")
    if (evidence.get("render_sha256") == key["render_sha256"] and evidence.get("receipt") == key["receipt"]
            and _reusable_measurement(saved)):
        return saved
    return None


def _check_pending(prior: dict[str, Any], stored: dict[str, Any], key: dict[str, Any]) -> bool:
    if prior or key["receipt"] is not None:
        return (prior.get("key") != key or not _caption_verdict(prior)
                or (key["receipt"] is not None and _measurement_for(prior, key) is None))
    return (_caption_verdict(stored).get("status") == "failed"
            and key["waived"] != ("caption_sync" in (stored.get("waived") or [])))


def _checkable_render(ctx: Ctx, outputs: Mapping[str, str]) -> str | None:
    j, item = ctx.job, ctx.item
    if all(j.rel(p) in outputs for p in (j.render_manifest(item), render_qa(j, item))):
        return outputs.get(j.rel(j.render_mp4(item)))
    return None


def _check_render(ctx: Ctx, outputs: Mapping[str, str], *, report_result: bool = False) -> str | None:
    j, item = ctx.job, ctx.item
    path = caption_sync_report(j, item)
    digest = _checkable_render(ctx, outputs)
    if not report_result and not path.exists() and not transcribe.approval_path(j).exists():
        # A successful 0.8.8 manifest was written after its render-time check.
        # Read that verdict only when it is requested; there is no hook work.
        if not digest or not _unsaved_caption_failure(j, item, digest):
            return None
    if item_kind(ctx) not in BURNED_KINDS:
        return None

    manifest_path, qa_path = j.render_manifest(item), render_qa(j, item)
    if not digest:
        return f"S5.{item} caption_sync not_run: the render could not be verified" if report_result else None
    settings = caption_qa_settings(ctx.config)
    key = _check_key(ctx, digest, settings)
    receipt, waived = key["receipt"], key["waived"]
    prior = read_yaml(path, default={})
    if not isinstance(prior, dict):
        raise VPError("data", f"{j.rel(path)} is not a readable caption check report")
    stored_path, stored = _saved_caption_report(j, item, outputs)
    previous_check = _caption_verdict(stored)
    changed = _check_pending(prior, stored, key)
    if not changed and not previous_check:
        why = ("the paid caption check is switched off" if settings["transcribe"] == "off"
               else transcribe.approval_reason(j))
        return f"S5.{item} caption_sync not_run: {why}" if report_result else None
    evidence = prior.get("evidence") or {}
    if changed:
        transcribe.guard_inputs(j, path)
        qa = read_yaml(qa_path)
        captions = dict(qa.get("captions") or {})
        saved = _measurement_for(prior, key)
        if qa.get("render_sha256") not in (None, digest):
            measurement = {**align_words.not_run(transcribe.RENDER_CHANGED),
                           "reason_code": transcribe.RENDER_MISMATCH}
            evidence = {}
        elif settings["transcribe"] == "off":
            measurement = align_words.not_run("the paid caption check is switched off")
        elif receipt is None:
            measurement = {**align_words.not_run(transcribe.approval_reason(j)),
                           "reason_code": transcribe.NO_APPROVAL}
        elif saved is not None:
            measurement = saved
        else:
            rendered = read_yaml(manifest_path)
            measurement = _measure_rendered_sync(ctx, outputs, digest, rendered, qa)
            evidence = ({"render_sha256": digest, "receipt": receipt, "measurement": measurement}
                        if _reusable_measurement(measurement) else {})
        captions["sync"] = _sync_verdict(measurement, settings)
        check = caption_sync_check(captions)
        preserve_failed = previous_check.get("status") == "failed" and (
            saved is not None or check["status"] == "not_run")
        preserve_legacy = stored_path == qa_path and check["status"] == "not_run"
        if preserve_failed or preserve_legacy:
            check = previous_check
            captions = dict(stored.get("captions") or captions)
        report = picture.apply_waivers(
            {"key": key, "render_sha256": digest, "evidence": evidence, "captions": captions,
             "checks": {"caption_sync": check}, "caption_note": caption_note("reel", captions)},
            ["caption_sync"] if waived else [])
    else:
        path = stored_path
        report = stored
        check = previous_check
    failure = None
    if check["status"] == "failed" and not waived:
        failure = _caption_failure(j, item, path, check)
    if changed:
        try:
            write_yaml(path, report)
        except Exception as exc:
            if failure:
                error = FreshCheckFailed(f"{failure}. Could not save the caption check report: {exc}")
                error.details.update(unsaved_caption_failure={name: report[name] for name in
                                                               ("render_sha256", "captions", "checks", "waived")},
                                     previous_report_sha256=sha256_obj(prior))
                raise error from exc
            raise
    if failure:
        raise FreshCheckFailed(failure)
    if report_result or (changed and receipt is not None):
        return f"S5.{item} caption_sync {check['status']}: {check['detail']} ({j.rel(path)})"
    return None


class S5Render(StepSpec):
    id, deps, scope, version, gate = "S5", ("S6a",), "item", "2", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        j, i = ctx.job, ctx.item
        out = [j.cut_mp4(i), j.clip_srt(i), j.design_texts(i)]
        if item_kind(ctx) in BURNED_KINDS:
            out += [j.source_words, j.caption_words]
        return out

    def config(self, ctx: Ctx) -> dict:
        kind = item_kind(ctx)
        from ..adapters.render_remotion import effective_renderer
        # The renderer that will really draw (remotion falls back to ffmpeg without Node/Chrome),
        # so installing the tools later re-renders the items made in the plain look.
        written = written_key_config(ctx.config)

        def overlay(section: str) -> Any:
            live = ctx.config.get(section)
            return live if written is None else key_overlay(live, DEFAULTS[section], written.get(section))

        settings = _merge(RENDER_KEY_DEFAULTS, overlay("render"))
        cards = settings["cards"].get(kind) or {}
        previous = (read_manifest(ctx.job, self.id, ctx.item) or {}).get("config") or {}
        cfg = {"kind": kind, "renderer": effective_renderer(ctx.config)[0],
               "brand": _merge(BRAND_KEY_DEFAULTS, overlay("brand")), "render": settings,
               "cards": [int(cards.get(name, 0)) for name in ("intro_ms", "outro_ms")],
               "picture_qa": picture_key_settings(ctx), "waive": key_waivers(waived_checks(ctx), previous)}
        if cfg["renderer"] == "remotion":  # the look chosen in the dialog re-renders the item
            live = ctx.config.get("remotion")
            defaults = {name: DEFAULTS["remotion"][name] for name in REMOTION_KEY_DEFAULTS}
            if written is not None and defaults != REMOTION_KEY_DEFAULTS:
                authored = deep_merge(written.get("remotion") or {}, ctx.job.design().get("remotion") or {})
                live = key_overlay(live, defaults, authored)
            rs = _merge(REMOTION_KEY_DEFAULTS, live)
            cfg["remotion"] = {k: rs[k] for k in REMOTION_KEY_DEFAULTS}
        if kind in BURNED_KINDS:
            cfg["caption_qa"] = _caption_key_settings(overlay("caption_qa"))
        return cfg

    def outputs(self, ctx: Ctx) -> list[Path]:
        j, i = ctx.job, ctx.item
        out = [j.render_mp4(i), j.render_manifest(i), j.final_srt(i), render_qa(j, i)]
        if item_kind(ctx) in BURNED_KINDS:
            out.append(clip_words(j, i))
        return out

    def on_fresh(self, ctx: Ctx, outputs: Mapping[str, str], *, report_result: bool = False) -> str | None:
        try:
            return _check_render(ctx, outputs, report_result=report_result)
        except FreshCheckFailed:
            raise
        except Exception as exc:
            failure = saved_caption_failure(ctx.job, ctx.item, outputs)
            if failure:
                raise FreshCheckFailed(f"check not_run: {exc}. {failure}") from exc
            raise

    def fresh_check(self, ctx: Ctx, outputs: Mapping[str, str]) -> str:
        if item_kind(ctx) not in BURNED_KINDS:
            return "none"
        digest = _checkable_render(ctx, outputs)
        if not digest:
            return "not_run"
        try:
            key = _check_key(ctx, digest, caption_qa_settings(ctx.config), snapshot=True)
        except transcribe.RecognitionBlocked:
            return "pending"
        prior = transcribe._document(caption_sync_report(ctx.job, ctx.item), yaml=True)
        stored = saved_caption_check(ctx.job, ctx.item, outputs)
        if _check_pending(prior, stored, key):
            return "pending"
        check = _caption_verdict(stored)
        if check.get("status") == "failed" and key["waived"]:
            return "waived"
        return check.get("status", "not_run")

    def run(self, ctx: Ctx) -> dict:
        j, item = ctx.job, ctx.item
        rec = fragment_record(ctx)
        texts = read_yaml(j.design_texts(item), default=None)
        if not isinstance(texts, dict):
            raise VPError("data", f"{item}: design_texts.yaml missing or invalid")
        transcribe.guard_inputs(j, *self.outputs(ctx))
        public = j.render_mp4(item)
        # The private directory is on the output volume for atomic installation. Keep the
        # item directory name for the renderer's layer manifest.
        with TemporaryDirectory(prefix=".vp-render-", dir=public.parent) as folder:
            staged = Path(folder) / public.parent.name / public.name
            staged.parent.mkdir()
            return self._render(ctx, rec, texts, public, staged)

    def _render(self, ctx: Ctx, rec: dict[str, Any], texts: dict[str, Any], public: Path, staged: Path) -> dict:
        j, item = ctx.job, ctx.item
        kind = rec["kind"]
        clip_info = video_info(j.cut_mp4(item))
        clip_ms = clip_info["video_ms"] or media.duration_ms(j.cut_mp4(item))
        timing_note = None
        if kind in BURNED_KINDS:
            local = prepare_clip_words(ctx, rec, clip_ms)
            reason = local.get("reason") or ""
            if reason.startswith((transcribe.LOCAL_RECORDING_CHANGED, transcribe.LOCAL_UNVERIFIED,
                                  "the cut changed")):
                timing_note = reason
        res = ctx.renderer.render_clip(j.cut_mp4(item), j.clip_srt(item), texts, kind, staged)
        try:
            intro_ms, outro_ms = int(res["intro_ms"]), int(res["outro_ms"])
        except (KeyError, TypeError, ValueError) as e:
            raise VPError("contract", f"{item}: renderer returned {res!r}") from e

        render_digest = None
        inspected = public
        if kind in BURNED_KINDS:
            # Retain the original inode for local QA, independently of when paid QA runs.
            candidate = staged.with_name("install.mp4")
            try:
                candidate.hardlink_to(staged)
            except OSError:
                shutil.copy2(staged, candidate)
            render_digest = transcribe.media_digest(staged)
            candidate.replace(public)
            inspected = staged
        else:
            staged.replace(public)
        staged_layers = staged.parent / "layers"
        if staged_layers.is_dir():
            layers = public.parent / "layers"
            layers.mkdir(exist_ok=True)
            (layers / "captions.mov").unlink(missing_ok=True)
            for name in ("intro.mp4", "outro.mp4", "layers.yaml"):
                part = staged_layers / name
                if part.is_file():
                    part.replace(layers / name)

        # ---- acceptance -------------------------------------------------------------------
        frame_ms = 1000.0 / (clip_info["fps"] or 25.0)
        duration_ms = media.duration_ms(inspected)
        expected = clip_ms + intro_ms + outro_ms
        if abs(duration_ms - expected) > frame_ms + 0.5:
            raise VPError("data", f"{item}: render is {duration_ms} ms, expected clip+cards {expected} ± 1 frame")
        if res.get("duration_ms") is not None and abs(int(res["duration_ms"]) - duration_ms) > frame_ms + 0.5:
            raise VPError("contract", f"{item}: renderer reported {res['duration_ms']} ms, file is {duration_ms} ms")
        if not media.has_audio(inspected):
            raise VPError("data", f"{item}: render has no audio")
        rs = render_settings(ctx.config)
        lufs = measure_lufs(inspected)
        if abs(lufs - float(rs["lufs"])) > float(rs["lufs_tolerance"]):
            raise VPError("data", f"{item}: loudness {lufs:.2f} LUFS, want {rs['lufs']} ± {rs['lufs_tolerance']}")
        cues = final_cues(read_srt(j.clip_srt(item)), intro_ms, clip_ms)
        bad = final_srt_violations(cues, intro_ms, clip_ms)
        if bad:
            raise VPError("data", f"{item}: final.srt invalid: {bad[:5]}")

        report = picture.inspect(inspected, kind, span=(intro_ms, duration_ms - outro_ms),
                                 config=ctx.config, expect_size=(clip_info["width"], clip_info["height"]))
        timeline = res.get("caption_timeline")
        shown, captions = _caption_picture(ctx, kind, intro_ms, clip_ms, render=inspected,
                                           timing_note=timing_note, timeline=timeline)
        if render_digest:
            report["render_sha256"] = render_digest
            report["caption_timeline"] = {k: shown[k] for k in ("mode", "words", "phrase") if k in shown}
            if not _render_matches(public, render_digest):
                captions["sync"] = {**align_words.not_run(transcribe.RENDER_CHANGED),
                                    "reason_code": transcribe.RENDER_MISMATCH}
        if captions.get("word_times") in align_words.ESTIMATES:
            ctx.log.event("captions_word_times_estimated", item=item, word_times=captions["word_times"],
                          phrase_cues=captions.get("phrase_cues"), cues=captions.get("cues"),
                          reason=captions.get("reason"))
        report["captions"] = captions
        report["checks"]["caption_voiced"] = caption_voiced_check(captions)
        report["checks"]["caption_sync"] = caption_sync_check(captions)
        report = picture.apply_waivers(report, waived_checks(ctx))
        render_changed = captions["sync"].get("reason_code") == transcribe.RENDER_MISMATCH
        if render_changed:
            report["ok"] = False
        write_yaml(render_qa(j, item), report)
        if render_changed:
            raise VPError("data", transcribe.RENDER_CHANGED)
        picture.require_clean(report, f"{item} render")

        write_srt(j.final_srt(item), cues, renumber=True)
        manifest: dict[str, Any] = {"item": item, "kind": kind, "intro_ms": intro_ms, "outro_ms": outro_ms,
                                    "clip_ms": clip_ms, "duration_ms": duration_ms,
                                    "lufs": round(lufs, 2), "renderer": type(ctx.renderer).__name__,
                                    "picture": {"ok": report["ok"], "waived": report["waived"],
                                                "checks": sorted(report["checks"])},
                                    "captions": {"word_times": captions.get("word_times"),
                                                 "mode": captions.get("mode"),
                                                 "voiced": captions["voiced"]["status"],
                                                 "sync": captions["sync"]["status"]}}
        note = getattr(ctx.renderer, "design_note", None)
        if note:
            manifest["design_note"] = note
        cap_note = caption_note(kind, captions)
        if cap_note:
            manifest["caption_note"] = cap_note
        gl = {key: res[key] for key in ("gl", "gl_effective", "gl_fallback") if key in res}
        manifest.update(gl)
        if timeline is not None:
            from ..adapters.render_remotion import CAPTION_DISPLAY_REVISION
            manifest["caption_display"] = CAPTION_DISPLAY_REVISION
        write_yaml(j.render_manifest(item), manifest)
        out = {"kind": kind, "duration_ms": duration_ms, "intro_ms": intro_ms, "outro_ms": outro_ms,
               "lufs": round(lufs, 2), "caption_voiced": captions["voiced"]["status"],
               "caption_sync": captions["sync"]["status"]}
        notes = [n for n in (note, cap_note) if n]
        if notes:
            out["note"] = "; ".join(notes)
        return {**out, **gl}


STEP = S5Render()
