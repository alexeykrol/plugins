"""Exact caption timing for a recording with subtitles only.

`vp captions plan --job JOB` quotes the recording's audio and the enabled reel checks.
`vp captions exact --job JOB --quote ID` approves that quote and writes word times for S5
in captions.words.json. The source subtitles remain the caption text.
`vp captions keep --job JOB` records the person's decision to keep the current timing.

S5 uses a receipt from exact timing or source transcription for the paid caption check.
`exact --retry --quote ID` approves one source retry on the original receipt.
`exact --retry REEL --quote ID` approves one retry of that reel's sound within the existing receipt.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import transcribe
from .analysis import align_words
from .core.errors import VPError
from .core.io import now_iso, read_yaml, write_json, write_yaml
from .core.lock import job_lock
from .core.paths import Job, vp_home
from .core.srt import read_srt

CHOICES = ("exact", "keep")
# looks whose captions follow word times: Remotion, a lit word or pages of a few words. The plain
# look (ffmpeg) burns each subtitle whole — exact word times would change nothing on screen.
WORD_LOOKS = ("word", "pages")


def choice(job: Job) -> dict[str, Any] | None:
    """The recorded answer to the offer ({"choice": "exact" | "keep", ...}) or None."""
    data = read_yaml(job.caption_choice, default=None)
    return data if isinstance(data, dict) and data.get("choice") in CHOICES else None


def word_times(job: Job, video: str | os.PathLike | None = None,
               probe: Callable[[Path], dict[str, Any]] | None = None,
               duration_s: float | None = None) -> str:
    return timing(job, video, probe, duration_s)[0]


def timing(job: Job, video: str | os.PathLike | None = None,
           probe: Callable[[Path], dict[str, Any]] | None = None,
           duration_s: float | None = None) -> tuple[str, str | None]:
    kind, reason, _ = _timing_details(job, video, probe, duration_s)
    return kind, reason


def _timing_details(job: Job, video: str | os.PathLike | None,
                    probe: Callable[[Path], dict[str, Any]] | None,
                    duration_s: float | None = None) -> tuple[str, str | None, str | None]:
    path = transcribe._video_path(job, video)
    problem = None
    for kind, timing_path in (("recording", job.source_words), ("captions", job.caption_words)):
        if not timing_path.exists():
            continue
        doc = transcribe._document(timing_path)
        if not doc:
            problem = problem or "the word times of the recording could not be read"
            continue
        provenance, reason = transcribe._timing_validation(job, path, doc, kind, probe, duration_s)
        if reason is None:
            return kind, None, provenance
        problem = problem or reason
    # Some 0.8.8 jobs retained only the rendered reel's timing file.
    for item in job.items() or ():
        srt = job.clip_srt(item)
        doc = transcribe._document(align_words.words_path(srt))
        if doc.get("schema") != 1 or doc.get("word_times", "aligned") != "aligned":
            continue
        try:
            words, info = align_words.read_clip_words(srt, read_srt(srt))
        except (ValueError, OSError, VPError):
            continue
        if words is not None:
            return "clip", None, info.get("timing_provenance", transcribe.VERIFIED)
        problem = problem or info.get("reason")
    return "none", problem, None


def _checks_only(job: Job, language: str | None, have: str, raw: dict | None) -> bool:
    if raw is not None:
        return True
    if have == "none":
        return False
    meta_path = job.caption_choice if have != "recording" else job.root / transcribe.META_NAME
    meta = transcribe._document(meta_path, yaml=True)
    if "language_hint" in meta:
        return transcribe.language_hint(job, language) == meta["language_hint"]
    return language is None


def reels(job: Job, config: dict[str, Any], info: dict[str, Any], *,
          item: str | None = None) -> tuple[int | None, float, int]:
    """Number of reels and summed upload span including frame-rounded cards."""
    from .adapters.render import card_durations
    from .core.ctx import Ctx
    from .steps.base import item_kind
    from .steps.s4_cut import fragment_record
    from .steps.s5_render import caption_qa_settings

    items = job.items()
    if item is not None:
        if item not in (items or []) or item_kind(Ctx(job, item, config)) != "reel":
            raise VPError("data", f"unknown reel {item!r}")
        items = [item]
    elif items is None:
        return None, 0.0, 0
    else:
        items = [name for name in items if item_kind(Ctx(job, name, config)) == "reel"]
    if caption_qa_settings(config)["transcribe"] == "off" or look(job, config) == "none":
        return len(items), 0.0, 0
    fps = info.get("fps") or 25.0
    intro, outro = card_durations(config, "reel")
    card_frames = round(intro * fps / 1000) + round(outro * fps / 1000)
    seconds = []
    for item in items:
        rec = fragment_record(Ctx(job, item, config))
        end_ms = min(rec["end_ms"], round(info["duration_s"] * 1000))
        if end_ms > rec["start_ms"]:
            clip_frames = round((end_ms - rec["start_ms"]) * fps / 1000)
            seconds.append((clip_frames + card_frames) / fps)
    return len(items), round(sum(seconds), 6), sum(math.ceil(span) for span in seconds)


def look(job: Job, config: dict[str, Any] | None = None) -> str:
    """How this job's reels show their captions: "word" (a word lights up), "pages" (a few words
    at a time, no lit word), "plain" (each subtitle burned whole — the look without Remotion) or
    "none" (no burned captions)."""
    from .adapters.render_remotion import effective_renderer, remotion_settings
    if config is None:
        from .design import job_config
        config = job_config(job)
    if effective_renderer(config)[0] != "remotion":
        return "plain"
    hl = remotion_settings(config)["captions"].get("highlight", "word")
    return {"word": "word", "phrase": "pages"}.get(hl, "none")


def plan(job: Job, video: str | os.PathLike | None = None,
         probe: Callable[[Path], dict[str, Any]] | None = None,
         config: dict[str, Any] | None = None, *, language: str | None = None,
         retry_reel: str | None = None, item: str | None = None) -> dict[str, Any]:
    """Quote this recording's exact timing and enabled reel checks without sending anything."""
    from .design import finished_render_plan, job_config
    transcribe.guard_inputs(job, *transcribe.quote_targets(job), video=video)
    if retry_reel is not None:
        try:
            with job_lock(job) as got:
                if not got:
                    raise VPError("data", "the job is busy (another run holds job.lock): try again when it ends")
                return _retry_reel(job, retry_reel)
        except transcribe.RecognitionBlocked as exc:
            return {**exc.result(), "offer": False, "retry_reel": retry_reel, "quote_id": None}
    config = job_config(job) if config is None else config
    wt, local_note = "none", None
    answered = choice(job)
    out: dict[str, Any] = {
        "word_times": wt, "subtitles": job.source_srt.exists(), "look": look(job, config),
        "reels": None, "choice": answered["choice"] if answered else None,
        "service": transcribe.SERVICE, "model": transcribe.MODEL_ID,
        "key": transcribe.key_status()["present"],
        "obtainable": False, "why_not": None, "saved_answer": False,
        "recording_s": None, "reels_s": 0.0, "cost_usd": None,
        "cost_source_usd": None, "cost_checks_max_usd": None, "quote_id": None,
        "price_usd_per_hour": transcribe.PRICE_USD_PER_HOUR, "price_source": transcribe.PRICE_SOURCE,
    }
    try:
        info = transcribe._recording_info(job, video, probe)
        path = Path(info["video"])
        # Large-file progress is a job write. Serialize it with exact, keep and the runner.
        with job_lock(job) as got:
            if not got:
                raise VPError("data", "the job is busy (another run holds job.lock): try again when it ends")
            with transcribe._hash_progress(job, path):
                wt, local_note, provenance = _timing_details(job, video, probe, info["duration_s"])
                hint = transcribe.language_hint(job, language)
                raw = transcribe.local_response(job, path, "captions", hint, False, probe, info["duration_s"])
                if provenance == transcribe.LEGACY_UNVERIFIED or (wt == "none" and raw
                        and raw.get("timing_provenance") == transcribe.LEGACY_UNVERIFIED):
                    out["timing_provenance"] = transcribe.LEGACY_UNVERIFIED
                    local_note = transcribe.LEGACY_TIMING_NOTE
                checks_only = item is not None or _checks_only(job, language, wt, raw)
                out.update(word_times=wt, local_timing_note=local_note,
                           recording_s=info["duration_s"])
                with transcribe.source_quote(job, video, language, False, probe, config, "captions",
                                             checks_only=checks_only, info=info, reel_item=item) as (quote, audio):
                    if audio is None and not checks_only:
                        out["why_not"] = f"the recording is longer than {transcribe.MAX_DURATION_S // 3600} h"
                    else:
                        out.update(quote, obtainable=True)
                        if checks_only:
                            out["saved_answer"] = item is None and raw is not None
                        elif quote["source_blocked_note"]:
                            out.update(obtainable=False, why_not=quote["source_blocked_note"])
                if checks_only:
                    try:
                        transcribe.require_reel_receipt(job, quote["recording_sha256"], item)
                    except transcribe.RecognitionBlocked as exc:
                        out["reel_check_note"] = exc.msg
    except VPError as e:
        out["why_not"] = e.msg
    language_offer = language is not None or out.get("language_hint") is not None or out.get("charge_warning")
    exact_offer = (out["subtitles"] and (wt == "none" or language_offer)
                   and out["look"] in WORD_LOOKS and out["reels"]
                   and (answered is None or local_note == transcribe.LOCAL_RECORDING_CHANGED
                        or language_offer))
    checks_offer = out.get("checks_only") and out.get("checks_seconds", 0) > 0 and out.get("reel_check_note")
    if out.get("checks_only"):
        out.update(finished_render_plan(job, config))
        if item is not None:
            for name in ("reels_pending_inputs", "reels_to_redo", "reels_ready_to_check"):
                out[name] = [item] if item in out[name] else []
            out["reel_checks"] = {item: out["reel_checks"][item]} if item in out["reel_checks"] else {}
        pending, rebuilds, ready = (out["reels_pending_inputs"], out["reels_to_redo"], out["reels_ready_to_check"])
        if item in pending:
            checks_offer = False
            out["why_not"] = f"{item} has earlier work pending; its render cannot be verified for checking yet."
        elif item in rebuilds:
            checks_offer = False
            out["why_not"] = f"{item} needs a rebuild before its captions can be checked."
        elif item is None and not ready and (pending or rebuilds):
            checks_offer = False
            out["why_not"] = "No finished reels are ready to check."
    source_offer = out["subtitles"] if out.get("retry_source") else exact_offer
    out["offer"] = bool(out["obtainable"] and out["key"] and
                        (checks_offer if out.get("checks_only") else source_offer))
    return out


def words_json(resp: dict[str, Any]) -> dict[str, Any]:
    """A recogniser answer -> the `captions.words.json` object: word times only, in the shape
    `load_words` reads ({"words": [{"word", "start", "end"}]}, seconds)."""
    words = transcribe.response_words(resp)
    if not words:
        raise VPError("data", "no speech was recognised in the recording")
    return {"words": [{"word": w["text"], "start": round(w["start"] / 1000, 3),
                       "end": round(w["end"] / 1000, 3), "provenance": w["provenance"]} for w in words],
            "language": resp.get("language_code"),
            "source": {"service": transcribe.SERVICE, "model": transcribe.MODEL_ID,
                       "transcription_id": resp.get("transcription_id")}}


def _match(job: Job, doc: dict[str, Any]) -> tuple[int, int]:
    """(matched, total): how many words of the person's subtitles were found in the word times."""
    try:
        found = align_words.align(read_srt(job.source_srt), align_words.source_words(doc))
    except (ValueError, OSError, VPError):
        return 0, 0
    return found.matched, found.total


def exact(job: Job, video: str | os.PathLike | None = None, language: str | None = None,
          urlopen: transcribe.Urlopen | None = None,
          probe: Callable[[Path], dict[str, Any]] | None = None, *,
          quote: str | None = None, retry: bool = False, retry_reel: str | None = None,
          item: str | None = None) -> dict[str, Any]:
    """Approve a shown quote, recover saved word times, or explicitly retry an ambiguous attempt."""
    from .core.adoption import adopt
    from .design import job_config

    transcribe.guard_inputs(job, *transcribe.recognition_targets(job),
                            job.caption_words, job.caption_choice, video=video)
    with job_lock(job) as got:
        if not got:
            raise VPError("data", "the job is busy (another run holds job.lock): try again when it ends")
        adopt(job)
        try:
            if retry_reel is not None:
                if quote is None:
                    raise transcribe.RecognitionBlocked(transcribe.NO_APPROVAL,
                                                       "approve the price shown for this reel retry with --quote")
                return _retry_reel(job, retry_reel, quote)
            return _exact_locked(job, video, language, urlopen, probe, quote, retry, job_config(job), item)
        except transcribe.RecognitionBlocked as exc:
            return {**exc.result(), "words": None}


def _exact_locked(job: Job, video: str | os.PathLike | None, language: str | None,
                  urlopen: transcribe.Urlopen | None, probe: Callable[[Path], dict[str, Any]] | None,
                  approved_quote: str | None, retry: bool, config: dict[str, Any],
                  item: str | None = None) -> dict[str, Any]:
    from .design import finished_render_plan

    path = transcribe._video_path(job, video)
    if not path.exists():
        raise VPError("data", f"recording not found: {path.name}")
    hint = transcribe.language_hint(job, language)
    with transcribe._hash_progress(job, path):
        if item is not None and approved_quote is None:
            raise transcribe.RecognitionBlocked(transcribe.NO_APPROVAL,
                                               "approve the price shown for this reel with --quote")
        have, reason, provenance = _timing_details(job, video, probe)
        if approved_quote is None and not retry:
            if have != "none":
                note = (transcribe.LEGACY_TIMING_NOTE if provenance == transcribe.LEGACY_UNVERIFIED
                        else "the captions already have exact word times")
                return {"word_times": have, "sent": False, "words": None,
                        "timing_provenance": provenance, "note": note}
            if reason in (transcribe.LOCAL_RECORDING_CHANGED, transcribe.LOCAL_UNVERIFIED):
                raise transcribe.RecognitionBlocked(transcribe.NO_APPROVAL, reason)
        raw = transcribe.local_response(job, path, "captions", hint, False, probe,
                                        recover=approved_quote is None and not retry)
        checks_only = item is not None or (_checks_only(job, language, have, raw)
                                           and approved_quote is not None and not retry)
        if raw is not None and have == "none" and approved_quote is None and not retry:
            # Recover a lost converted file from the local response without extracting audio.
            info = transcribe._recording_info(job, video, probe)
            recording = transcribe.media_digest(path) if raw["timing_provenance"] == transcribe.VERIFIED else None
            quote = {**info, "recording_sha256": recording,
                     "audio_sha256": raw.get("audio_sha256"), "diarize": False, "language_hint": hint,
                     "cost_usd": 0, "cost_source_usd": 0, "cost_checks_max_usd": 0, "charge_warning": None}
            result, sent = {**raw, "created_at": raw.get("created_at"), "receipt_id": None}, False
        else:
            if not checks_only and not job.source_srt.exists():
                raise VPError("data", "the job has no subtitles yet: `vp transcribe run` makes the "
                                      "subtitles and the word times together")
            with transcribe.source_quote(job, video, language, False, probe, config, "captions",
                                         checks_only=checks_only, reel_item=item) as (quote, audio):
                if checks_only:
                    if approved_quote != quote["quote_id"]:
                        raise transcribe.RecognitionBlocked(transcribe.NO_APPROVAL, transcribe.STALE_QUOTE)
                    if not quote["checks_seconds"]:
                        raise transcribe.RecognitionBlocked(transcribe.NO_APPROVAL, "there are no reel checks to approve")
                    transcribe._approve(job, quote)
                    if item is not None or have != "none" or raw is None:
                        approved = f"reel check approved for {item}" if item is not None else "reel checks approved"
                        return {"word_times": have, "sent": False, "words": None, "checks_only": True,
                                "timing_provenance": provenance,
                                "receipt_id": approved_quote, "covers": quote["covers"],
                                "reel_scope": quote["reel_scope"],
                                "note": f"{approved}. No source audio was sent."}
                    result, sent = {**raw, "created_at": raw.get("created_at"), "receipt_id": approved_quote}, False
                else:
                    if audio is None:
                        raise VPError("data", f"the recording is longer than {transcribe.MAX_DURATION_S // 3600} h")
                    result, sent = transcribe.tracked_source_result(job, "captions", quote, audio,
                                                                    approved_quote, urlopen, retry)
    resp = result["response"]
    meta = transcribe.result_metadata(quote, result, sent)
    raw_path = job.root / transcribe.RAW_NAME
    if sent or not raw_path.exists():
        write_json(raw_path, {**meta, "response": resp})
    if sent:
        _record_cost(job, quote["source_seconds"])
    doc = words_json(resp)
    matched, total = _match(job, doc)
    transcribe._bind_words(doc, meta)
    write_json(job.caption_words, doc)
    res = {"choice": "exact", "at": now_iso(), **meta, "words": len(doc["words"]),
           "matched": matched, "total": total, "language": resp.get("language_code")}
    write_yaml(job.caption_choice, res)
    checks_fields = ({"checks_only": True, "covers": quote["covers"], "reel_scope": quote["reel_scope"]}
                     if quote.get("checks_only") else {})
    return {**res, **checks_fields, "word_times": "captions", "file": job.rel(job.caption_words),
            "usable": bool(total) and matched / total >= align_words.MIN_MATCH_SHARE,
            **finished_render_plan(job, config)}


def _retry_reel(job: Job, item: str, quote: str | None = None) -> dict[str, Any]:
    from .core.ctx import Ctx
    from .design import job_config
    from .steps.base import item_kind
    from .steps.s5_render import caption_qa_settings, caption_sync_report, cut_recording_digest, render_qa

    ctx = Ctx(job, item, job_config(job))
    if item_kind(ctx) != "reel" or caption_qa_settings(ctx.config)["transcribe"] == "off":
        raise VPError("data", "paid caption checks are disabled for this item")
    if not job.source_video.is_file():
        # cut_recording_digest supplies the same provenance refusal used by S5.
        cut_recording_digest(ctx)
    with transcribe._hash_progress(job, job.source_video):
        recording_sha256 = cut_recording_digest(ctx)
    transcribe.require_reel_receipt(job, recording_sha256, item)
    prior = (transcribe._document(caption_sync_report(job, item), yaml=True)
             or transcribe._document(render_qa(job, item), yaml=True))
    check = (prior.get("checks") or {}).get("caption_sync") or {}
    if check.get("reason") == transcribe.RENDER_MISMATCH:
        raise transcribe.RecognitionBlocked(transcribe.RENDER_MISMATCH, transcribe.RENDER_CHANGED)
    render_digest = prior.get("render_sha256")
    if not render_digest:
        raise transcribe.RecognitionBlocked(transcribe.NO_APPROVAL,
                                           "there is no verified S5 render for this reel retry")
    render = job.render_mp4(item)
    if not render.is_file():
        raise transcribe.RecognitionBlocked(transcribe.RENDER_MISMATCH, transcribe.RENDER_CHANGED)
    with transcribe._hash_progress(job, render), tempfile.TemporaryDirectory(prefix="vp-recognition-") as tmp:
        audio = transcribe._verified_audio(render, Path(tmp), render_digest, transcribe.RENDER_CHANGED,
                                          reason=transcribe.RENDER_MISMATCH)
        return transcribe.reel_retry_quote(job, item, audio, recording_sha256, render_digest, quote)


def _record_cost(job: Job, duration_s: float) -> None:
    from .core.costs import CostJournal
    try:
        CostJournal(vp_home(), job.job_id).add(
            "api", provider=transcribe.SERVICE, op="caption_words", model=transcribe.MODEL_ID,
            usd=round(float(duration_s) / 3600 * transcribe.PRICE_USD_PER_HOUR, 4), estimate=True)
    except OSError:
        pass    # bookkeeping; the answer is already here


def keep(job: Job) -> dict[str, Any]:
    """The person's "leave as is": recorded, so the offer is not made again. An earlier `exact`
    is not undone (its word times stay in use)."""
    from .core.adoption import adopt

    transcribe.guard_inputs(job, job.lock_file, job.caption_choice)
    with job_lock(job) as got:
        if not got:
            raise VPError("data", "the job is busy (another run holds job.lock): try again when it ends")
        adopt(job)
        prev = choice(job)
        if prev and prev["choice"] == "exact":
            return {"choice": "exact", "note": "the existing exact-timing choice was kept"}
        res = {"choice": "keep", "at": now_iso()}
        write_yaml(job.caption_choice, res)
        return res


# ---- CLI ---------------------------------------------------------------------------------------

def _plan_lines(p: dict[str, Any]) -> list[str]:
    if p.get("retry_reel"):
        return [line for line in (p.get("charge_warning"), p.get("note") if not p["offer"] else None,
                                  f"retry quote {p['quote_id']} for {p['retry_reel']}" if p.get("quote_id") else None)
                if line]
    lines = [f"captions: word times {p['word_times']}, look {p['look']}, reels "
             f"{p['reels'] if p['reels'] is not None else 'not chosen yet'}"]
    lines.extend(transcribe.recognition_quote_lines(p))
    if p["obtainable"] and p.get("retry_source"):
        lines.append(f"source retry quote {p['quote_id']} (approve with --retry --quote)")
    elif p["obtainable"]:
        lines.append(f"{'reel checks only' if p['checks_only'] else 'exact timing'} up to "
                     f"${p['cost_usd']:.2f} ({p['service']} {p['model']})")
        lines.append(f"source ${p['cost_source_usd']:.8g}, reel checks up to ${p['cost_checks_max_usd']:.8g}")
        lines.append(transcribe.checks_rule(p))
        if p["checks_seconds"]:
            scope = p["reel_scope"]
            lines.append("reel check scope: " + ("all reels" if scope == "all" else ", ".join(scope)))
        lines.append(f"quote {p['quote_id']} for {p['recording']}")
    if p["why_not"]:
        lines.append(p["why_not"])
    lines.extend(p[k] for k in ("local_timing_note", "reel_check_note") if p.get(k))
    lines.append(f"key: {'present' if p['key'] else 'missing'}; answer: {p['choice'] or 'none'}; "
                 f"offer: {'yes' if p['offer'] else 'no'}")
    return lines


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp captions",
                                 description="exact caption timing for a recording with subtitles only")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "exact", "keep"):
        s = sub.add_parser(name)
        s.add_argument("--job", required=True, help="the job folder")
        s.add_argument("--json", action="store_true")
        if name != "keep":
            s.add_argument("--video", help="the recording (default: the job's source video)")
        if name != "keep":
            s.add_argument("--language", help="ISO 639-1/3 hint, e.g. ru (default: request.yaml "
                                              "`language`, else auto)")
            s.add_argument("--item", help="limit a check-only quote to this reel")
        if name == "plan":
            s.add_argument("--retry", metavar="REEL", help="quote one retry of an ambiguous reel check")
        if name == "exact":
            s.add_argument("--quote", help="quote_id from the approved captions plan")
            s.add_argument("--retry", nargs="?", const="", metavar="REEL",
                           help="approve a retry of an ambiguous source request or a named reel")
    a = ap.parse_args(argv)
    if a.cmd != "keep" and a.item is not None and a.retry is not None:
        ap.error("--item and --retry are separate approval routes")
    job = Job(a.job)
    if a.cmd == "plan":
        res = plan(job, a.video, language=a.language, retry_reel=a.retry, item=a.item)
        lines = _plan_lines(res)
    elif a.cmd == "keep":
        res = keep(job)
        lines = [f"KEPT the captions as they are ({res['choice']})"]
    else:
        res = exact(job, a.video, a.language, quote=a.quote, retry=a.retry == "",
                    retry_reel=a.retry or None, item=a.item)
        if res.get("status") == "not_run":
            lines = [f"RECOGNITION NOT COMPLETED: {res['note']}"]
        elif not res.get("sent") and res.get("words") is None:
            lines = [res["note"]]
        else:
            lines = [f"WORD TIMES {res['file']}: {res['words']} words, {res['matched']} of "
                     f"{res['total']} subtitle words found"
                     + ("" if res["sent"] else " (saved answer reused, nothing paid)")
                     + ("" if res["usable"] else " — too few to use: the captions stay as they are")]
            reels, long_videos = res["reels_to_redo"], res["long_videos_to_redo"]
            pending = res["reels_pending_inputs"] + res["long_videos_pending_inputs"]
            if reels:
                lines.append(f"These {len(reels)} finished reels will be redone: {', '.join(reels)}.")
            if long_videos:
                lines.append(f"These finished long videos will be redone: {', '.join(long_videos)}.")
            if pending:
                lines.append(f"Earlier edits need processing before the rebuild can be determined: {', '.join(pending)}.")
            if not (reels or long_videos or pending):
                lines.append("No finished videos will be redone.")
    if a.cmd == "exact" and res.get("charge_warning"):
        lines.append(res["charge_warning"])
    if a.cmd == "exact" and res.get("timing_provenance") == transcribe.LEGACY_UNVERIFIED:
        if transcribe.LEGACY_TIMING_NOTE not in lines:
            lines.append(transcribe.LEGACY_TIMING_NOTE)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        for ln in lines:
            print(ln)
    return 0
