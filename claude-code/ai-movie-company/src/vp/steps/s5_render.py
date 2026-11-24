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
               -> `clip.words.json` next to clip.srt. They come from `_cut.json` (the file S2
               reads) or from `captions.words.json` (`vp captions exact`: word times asked for
               the captions alone, by the person's click). A renderer that lights words up one
               by one reads the file.
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
               person's decision: `caption_qa.transcribe` (auto | always | off, see
               `transcription_allowed`).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..adapters import media, picture
from ..adapters.render import brand_settings, card_durations, measure_lufs, render_settings, video_info
from ..analysis import align_words
from ..core.io import read_json, read_yaml, write_yaml
from ..core.srt import Cue, read_srt, write_srt
from ._s2_retime import load_words
from .base import Ctx, StepSpec, VPError, item_kind
from .s4_cut import fragment_record, waived_checks

CAPTION_QA_DEFAULTS: dict[str, Any] = {"transcribe": "auto", "p90_ms": align_words.SYNC_P90_MS,
                                       "max_ms": align_words.SYNC_MAX_MS,
                                       "lead_max_ms": align_words.LEAD_MAX_MS,
                                       "on_silence_max": align_words.ON_SILENCE_MAX}
TRANSCRIBE_MODES = ("auto", "always", "off")
BURNED_KINDS = ("reel",)              # kinds whose captions are burned into the picture
TRANSCRIPT_META = "transcript.yaml"   # written by `vp transcribe run` (vp.transcribe.META_NAME)


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
    if out["transcribe"] not in TRANSCRIBE_MODES:
        raise VPError("data", f"caption_qa.transcribe must be one of {list(TRANSCRIBE_MODES)}, "
                              f"got {out['transcribe']!r}")
    return out


def clip_words(job, item: str) -> Path:
    """Word times of the clip, next to clip.srt (S5 output for kinds with burned captions)."""
    return align_words.words_path(job.clip_srt(item))


def _recording_words(ctx: Ctx, rec: dict[str, Any], cues: list[Cue], clip_ms: int
                     ) -> tuple[align_words.Alignment | None, str | None, str]:
    """(alignment, where it came from, reason when there is none): the recording's word times
    on the clip's time base, aligned to the text of clip.srt. `_cut.json` first, then
    `captions.words.json` (word times asked for the captions alone)."""
    j = ctx.job
    reason = ""
    for name, path in (("source_words", j.source_words), ("caption_words", j.caption_words)):
        if not path.exists():
            continue
        try:
            words = load_words(read_json(path))
        except (ValueError, OSError):
            reason = reason or "the word times of the recording could not be read"
            continue
        found = align_words.align(cues, align_words.remap(words, rec["start_ms"], rec["end_ms"]), clip_ms)
        if found.usable:
            return found, name, ""
        reason = reason or (f"the word times of the recording do not match the subtitles "
                            f"({found.matched} of {found.total} words)")
    return None, None, reason or "the recording came without word times (subtitles only)"


def prepare_clip_words(ctx: Ctx, rec: dict[str, Any], clip_ms: int) -> dict[str, Any]:
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
    cues = read_srt(srt)
    alignment, source, reason = _recording_words(ctx, rec, cues, clip_ms)
    if alignment is not None:
        align_words.write_clip_words(srt, alignment, source)
    else:
        try:
            sound = media.pauses(j.cut_mp4(item), align_words.PAUSE_BELOW_MEAN_DB,
                                 align_words.PAUSE_MIN_MS, 0, clip_ms)
        except (VPError, OSError) as e:
            ctx.log.event("captions_pauses_unknown", item=item, error=getattr(e, "msg", str(e)))
            align_words.write_clip_estimate(
                srt, align_words.whole_phrases(cues), "estimated",
                f"{reason}; the pauses of the sound could not be measured, so every phrase is "
                f"shown whole")
        else:
            voiced = align_words.voiced_intervals(sound["pauses"], clip_ms)
            est = align_words.estimate_voiced(cues, voiced)
            align_words.write_clip_estimate(
                srt, est, "estimated_voiced", reason,
                {"mean_db": sound["mean_db"], "noise_db": sound["noise_db"],
                 "pauses": len(sound["pauses"])})
    _, info = align_words.read_clip_words(srt, cues)
    return align_words.caption_info(info, len(cues))


def transcription_allowed(ctx: Ctx, mode: str) -> tuple[bool, str]:
    """May the finished video's sound go to the recogniser for the caption check?

    Sound leaves the computer only by the person's decision (skills/pipeline: "Never send a
    recording without the click"). So by default (`auto`) the check recognises a reel only when
    this job's recording was already sent to the same recogniser by the person's click:
    `vp transcribe run` (transcript.yaml names the service) or `vp captions exact`
    (captions.choice.yaml, choice `exact`) — the same sound, the same service, nothing new
    leaves. `always` is a standing permission in config; `off` switches the recognition off."""
    if mode == "off":
        return False, "the caption check is switched off in the settings (caption_qa.transcribe: off)"
    if mode == "always":
        return True, ""
    name = getattr(ctx.transcriber, "name", None)
    meta = read_yaml(ctx.job.root / TRANSCRIPT_META, default=None)
    service = meta.get("service") if isinstance(meta, dict) else None
    if service and service == name:
        return True, ""
    answer = read_yaml(ctx.job.caption_choice, default=None)
    if isinstance(answer, dict) and answer.get("choice") == "exact" and answer.get("service") == name:
        return True, ""
    return False, ("the sound of this recording was never sent to speech recognition, and the check "
                   "does not send it on its own, so the captions were not compared with the voice "
                   "(caption_qa.transcribe: always allows it)")


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


def caption_voiced(ctx: Ctx, shown: dict[str, Any], intro_ms: int, clip_ms: int) -> dict[str, Any]:
    """The local check: the captions of the finished video against the pauses of its own sound
    (status passed | failed | not_run). Nothing leaves the computer. It is for captions without
    real word times; captions timed by the recording's word times are judged by `sync`."""
    j, item = ctx.job, ctx.item
    if shown.get("word_times") == "aligned":
        return align_words.not_run("the captions are timed by the recording's word times; whether "
                                   "they follow the voice is the caption_sync check")
    st = caption_qa_settings(ctx.config)
    try:
        sound = media.pauses(j.render_mp4(item), align_words.PAUSE_BELOW_MEAN_DB,
                             align_words.PAUSE_MIN_MS, intro_ms, intro_ms + clip_ms)
    except (VPError, OSError) as e:
        ctx.log.event("caption_voiced_not_run", item=item, error=getattr(e, "msg", str(e)))
        return align_words.not_run("the pauses of the sound could not be measured, so the captions "
                                   "were not compared with them")
    report = align_words.voiced_report(shown["onsets"], shown["shows"], sound["pauses"])
    return align_words.voiced_verdict(report, lead_max_ms=int(st["lead_max_ms"]),
                                      on_silence_max=float(st["on_silence_max"]))


def caption_check(ctx: Ctx, kind: str, intro_ms: int, clip_ms: int) -> dict[str, Any]:
    """The `captions` section of render.qa.yaml: how the words were timed, `voiced` — the
    captions against the pauses of the sound of render.mp4 (local) — and `sync` — the words on
    screen against the words heard in render.mp4 (each with status passed | failed | not_run).
    Never raises for a check that could not run: that is `not_run` with the reason."""
    j, item = ctx.job, ctx.item
    if kind not in BURNED_KINDS:
        why = ("captions are not burned into this kind of video: its subtitles go to the platform "
               "as a separate track")
        return {"mode": "none", "word_times": "none", "voiced": align_words.not_run(why),
                "sync": align_words.not_run(why)}
    shown = _caption_timeline(ctx.renderer, j.clip_srt(item), clip_ms)
    words, flags = shown.pop("words"), shown.pop("phrase")
    onsets, shows = shown.pop("onsets"), shown.pop("shows")
    if shown.get("mode") == "none" or not words:
        why = "no captions are burned into this video"
        return {**shown, "voiced": align_words.not_run(why), "sync": align_words.not_run(why)}
    shown["voiced"] = caption_voiced(ctx, {**shown, "onsets": onsets, "shows": shows}, intro_ms, clip_ms)
    st = caption_qa_settings(ctx.config)
    ok, why = transcription_allowed(ctx, st["transcribe"])
    if ok:
        ok, why = ctx.transcriber.available()
        if not ok:
            why = f"{why}, so the captions were not compared with the voice"
    if not ok:
        return {**shown, "sync": align_words.not_run(why)}
    try:
        # one request, never retried here: a recognition that timed out may already be paid
        heard = ctx.transcriber.words(j.render_mp4(item), str(j.request().get("language") or "") or None)
    except VPError as e:
        ctx.log.event("caption_check_not_run", item=item, kind=e.kind, error=e.msg)
        return {**shown, "sync": align_words.not_run(
            f"speech recognition gave no answer ({e.kind}), so the captions were not compared "
            f"with the voice")}
    report = align_words.sync_report(words, heard, mode=shown["mode"], offset_ms=intro_ms,
                                     phrase=flags if shown["mode"] == "mixed" else None)
    return {**shown, "sync": align_words.sync_verdict(report, p90_ms=int(st["p90_ms"]),
                                                      max_ms=int(st["max_ms"]))}


def caption_sync_check(captions: dict[str, Any]) -> dict[str, Any]:
    """`captions.sync` as a check of the QA report: ok True (passed) | False (failed) | None
    (not run — neither), the measurements, and the reason in plain words."""
    sync = captions["sync"]
    value = {k: sync[k] for k in ("median_ms", "p90_ms", "max_ms", "compared", "words") if k in sync}
    return {"ok": {"passed": True, "failed": False}.get(sync["status"]), "status": sync["status"],
            "value": value or None, "detail": sync["reason"]}


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
    if captions.get("word_times") in align_words.ESTIMATES:
        n, whole = captions.get("cues"), captions.get("phrase_cues")
        how = "each phrase is shown whole for its own time"
        if n is not None and whole is not None and whole < n:
            how = (f"{whole} of {n} phrases are shown whole for their own time, the rest word by "
                   f"word by the pauses of the sound")
        parts.append(f"the captions are timed by estimate, not by the voice: {how} ("
                     + str(captions.get("reason") or "no word times") + ")")
    sync = captions["sync"]
    if sync["status"] == "not_run":
        parts.append("the captions were not checked against the voice: " + sync["reason"])
    return "; ".join(parts) or None


class S5Render(StepSpec):
    id, deps, scope, version, gate = "S5", ("S6a",), "item", "2", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        j, i = ctx.job, ctx.item
        out = [j.cut_mp4(i), j.clip_srt(i), j.design_texts(i)]
        if item_kind(ctx) in BURNED_KINDS:
            # optional: the captions' word times come from one of them
            out += [j.source_words, j.caption_words]
        return out

    def config(self, ctx: Ctx) -> dict:
        kind = fragment_record(ctx)["kind"]
        from ..adapters.render_remotion import effective_renderer
        # The renderer that will really draw (remotion falls back to ffmpeg without Node/Chrome),
        # so installing the tools later re-renders the items made in the plain look.
        cfg = {"kind": kind, "renderer": effective_renderer(ctx.config)[0],
               "brand": brand_settings(ctx.config), "render": render_settings(ctx.config),
               "cards": list(card_durations(ctx.config, kind)),
               "picture_qa": picture.settings(ctx.config), "waive": waived_checks(ctx)}
        if cfg["renderer"] == "remotion":  # the look chosen in the dialog re-renders the item
            from ..adapters.render_remotion import remotion_settings
            rs = remotion_settings(ctx.config)
            cfg["remotion"] = {k: rs[k] for k in ("font_family", "accent_color", "captions", "crf")}
        if kind in BURNED_KINDS:    # only these re-render when the caption path changes
            cfg["caption_qa"] = {**caption_qa_settings(ctx.config), "words_schema": align_words.SCHEMA}
        return cfg

    def outputs(self, ctx: Ctx) -> list[Path]:
        j, i = ctx.job, ctx.item
        out = [j.render_mp4(i), j.render_manifest(i), j.final_srt(i), render_qa(j, i)]
        if item_kind(ctx) in BURNED_KINDS:
            out.append(clip_words(j, i))
        return out

    def run(self, ctx: Ctx) -> dict:
        j, item = ctx.job, ctx.item
        rec = fragment_record(ctx)
        kind = rec["kind"]
        texts = read_yaml(j.design_texts(item), default=None)
        if not isinstance(texts, dict):
            raise VPError("data", f"{item}: design_texts.yaml missing or invalid")
        clip_info = video_info(j.cut_mp4(item))
        clip_ms = clip_info["video_ms"] or media.duration_ms(j.cut_mp4(item))
        if kind in BURNED_KINDS:
            prepare_clip_words(ctx, rec, clip_ms)
        res = ctx.renderer.render_clip(j.cut_mp4(item), j.clip_srt(item), texts, kind, j.render_mp4(item))
        try:
            intro_ms, outro_ms = int(res["intro_ms"]), int(res["outro_ms"])
        except (KeyError, TypeError, ValueError) as e:
            raise VPError("contract", f"{item}: renderer returned {res!r}") from e

        # ---- acceptance -------------------------------------------------------------------
        frame_ms = 1000.0 / (clip_info["fps"] or 25.0)
        duration_ms = media.duration_ms(j.render_mp4(item))
        expected = clip_ms + intro_ms + outro_ms
        if abs(duration_ms - expected) > frame_ms + 0.5:
            raise VPError("data", f"{item}: render is {duration_ms} ms, expected clip+cards {expected} ± 1 frame")
        if res.get("duration_ms") is not None and abs(int(res["duration_ms"]) - duration_ms) > frame_ms + 0.5:
            raise VPError("contract", f"{item}: renderer reported {res['duration_ms']} ms, file is {duration_ms} ms")
        if not media.has_audio(j.render_mp4(item)):
            raise VPError("data", f"{item}: render has no audio")
        rs = render_settings(ctx.config)
        lufs = measure_lufs(j.render_mp4(item))
        if abs(lufs - float(rs["lufs"])) > float(rs["lufs_tolerance"]):
            raise VPError("data", f"{item}: loudness {lufs:.2f} LUFS, want {rs['lufs']} ± {rs['lufs_tolerance']}")
        cues = final_cues(read_srt(j.clip_srt(item)), intro_ms, clip_ms)
        bad = final_srt_violations(cues, intro_ms, clip_ms)
        if bad:
            raise VPError("data", f"{item}: final.srt invalid: {bad[:5]}")

        report = picture.inspect(j.render_mp4(item), kind, span=(intro_ms, duration_ms - outro_ms),
                                 config=ctx.config, expect_size=(clip_info["width"], clip_info["height"]))
        captions = caption_check(ctx, kind, intro_ms, clip_ms)
        if captions.get("word_times") in align_words.ESTIMATES:
            ctx.log.event("captions_word_times_estimated", item=item, word_times=captions["word_times"],
                          phrase_cues=captions.get("phrase_cues"), cues=captions.get("cues"),
                          reason=captions.get("reason"))
        report["captions"] = captions
        report["checks"]["caption_voiced"] = caption_voiced_check(captions)
        report["checks"]["caption_sync"] = caption_sync_check(captions)
        report = picture.apply_waivers(report, waived_checks(ctx))
        write_yaml(render_qa(j, item), report)
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
        write_yaml(j.render_manifest(item), manifest)
        out = {"kind": kind, "duration_ms": duration_ms, "intro_ms": intro_ms, "outro_ms": outro_ms,
               "lufs": round(lufs, 2), "caption_voiced": captions["voiced"]["status"],
               "caption_sync": captions["sync"]["status"]}
        notes = [n for n in (note, cap_note) if n]
        if notes:
            out["note"] = "; ".join(notes)
        return out


STEP = S5Render()
