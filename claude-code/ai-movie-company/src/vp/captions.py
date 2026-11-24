"""`vp captions` — exact caption timing for a recording that came with subtitles only (BACKLOG
"Caption sync", incident 2026-11-23).

Burned captions of a reel light up word by word. A recording with an `.srt` and no word times
gets a local estimate (vp.steps.s5_render: the pauses of the sound, whole phrases where that is
not safe) — nothing leaves the computer for it. Exact timing needs the words heard in the sound,
and that means sending the sound to the recogniser (ElevenLabs Scribe, the person's key, the
person's money). So it is an offer, made once, and it happens only on the person's click:

  vp captions plan --job <folder> [--video F] [--json]    what the captions are timed by now,
                                                          whether exact times can be had and for
                                                          how much — nothing is sent
  vp captions exact --job <folder> [--video F] [--language ru] [--json]
                                                          the click: the recording's SOUND goes to
                                                          the recogniser, the word times come back
  vp captions keep --job <folder> [--json]                the other click: leave the captions as
                                                          they are; the offer is not made again

`exact` writes word times only: `<job>/captions.words.json` (the shape of `_cut.json`). The
person's subtitles (`_cut.srt`) are never read for writing and never replaced, and `_cut.json` is
not created: that file is an input of intake, retiming and selection, and a new one would make
them run again. S5 alone reads `captions.words.json`, aligns it to the proofread text of each
clip (the text on screen stays the proofread subtitles) and re-renders the reels. The answer is
recorded in `<job>/captions.choice.yaml`; with `exact` in it the caption check of S5 may hear the
finished reels with the same recogniser (`caption_qa.transcribe: auto`), so the price of the plan
counts those too.

Bookkeeping is code: the price is duration x the recogniser's price (vp.transcribe), the key
comes from the common key store and is never printed, logged or written into the job folder, and
the request is never repeated here (a request that timed out may already be paid). The raw
answer is kept in `<job>/transcript.raw.json` (as `vp transcribe run` keeps it): a lost
conversion is rebuilt without paying again.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import transcribe
from .analysis import align_words
from .core import progress
from .core.errors import VPError
from .core.io import now_iso, read_json, read_yaml, write_json, write_yaml
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


def word_times(job: Job) -> str:
    """Where a reel's captions get exact word times from: "recording" (`_cut.json`),
    "captions" (`captions.words.json`) or "none"."""
    if job.source_words.exists():
        return "recording"
    return "captions" if job.caption_words.exists() else "none"


def reels(job: Job) -> tuple[int | None, float]:
    """(number of reels, their total length in seconds) from fragments.yaml; (None, 0) while the
    fragments are not chosen yet."""
    data = read_yaml(job.fragments_yaml, default=None)
    frags = data.get("fragments") if isinstance(data, dict) else None
    if not isinstance(frags, list):
        return None, 0.0
    n, total_ms = 0, 0
    for f in frags:
        if not isinstance(f, dict) or (f.get("type") or f.get("kind")) != "reel":
            continue
        n += 1
        try:
            if "start_ms" in f:
                total_ms += max(0, int(f["end_ms"]) - int(f["start_ms"]))
            elif "in_ms" in f:
                total_ms += max(0, int(f["out_ms"]) - int(f["in_ms"]))
        except (KeyError, TypeError, ValueError):
            pass
    return n, total_ms / 1000


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
         config: dict[str, Any] | None = None) -> dict[str, Any]:
    """What exact caption timing would take for this job — nothing is sent.

    word_times   "recording" | "captions" | "none": exact word times already here, and from where
    obtainable   they can be had from the recording's sound (it is there, has sound, not too long)
    key          a recogniser key is connected
    cost_usd     approximate price: the recording's sound + the caption check of the finished
                 reels (`recording_s`, `reels_s`); 0 when a saved answer can be reused
    offer        True only when asking makes sense: subtitles without word times, reels whose
                 captions follow word times, a connected key, and no answer given before"""
    wt = word_times(job)
    n_reels, reels_s = reels(job)
    answered = choice(job)
    out: dict[str, Any] = {
        "word_times": wt, "subtitles": job.source_srt.exists(), "look": look(job, config),
        "reels": n_reels, "choice": answered["choice"] if answered else None,
        "service": transcribe.SERVICE, "model": transcribe.MODEL_ID,
        "key": transcribe.key_status()["present"],
        "obtainable": False, "why_not": None, "saved_answer": False,
        "recording_s": None, "reels_s": round(reels_s, 1), "cost_usd": None,
        "price_usd_per_hour": transcribe.PRICE_USD_PER_HOUR, "price_source": transcribe.PRICE_SOURCE,
    }
    try:
        est = transcribe.estimate(job, video, probe)
    except VPError as e:
        out["why_not"] = e.msg
    else:
        out["recording_s"] = est["duration_s"]
        if est["too_long"]:
            out["why_not"] = (f"the recording is longer than {transcribe.MAX_DURATION_S // 3600} h: "
                              f"the recogniser takes at most that per file")
        else:
            out["obtainable"] = True
            out["saved_answer"] = _saved_response(job, Path(est["video"])) is not None
            sent_s = (0.0 if out["saved_answer"] else est["duration_s"]) + reels_s
            out["cost_usd"] = transcribe.cost_usd(sent_s)
    out["offer"] = bool(out["subtitles"] and wt == "none" and out["look"] in WORD_LOOKS
                        and n_reels and out["obtainable"] and out["key"] and answered is None)
    return out


def _saved_response(job: Job, video: Path) -> dict[str, Any] | None:
    """The recogniser's saved answer for this very recording (transcript.raw.json), if any."""
    raw = job.root / transcribe.RAW_NAME
    try:
        saved = read_json(raw, default=None) if raw.exists() else None
        vid = transcribe._video_id(video)
    except (ValueError, OSError):
        return None
    if isinstance(saved, dict) and saved.get("video_id") == vid and isinstance(saved.get("response"), dict):
        return saved
    return None


def words_json(resp: dict[str, Any]) -> dict[str, Any]:
    """A recogniser answer -> the `captions.words.json` object: word times only, in the shape
    `load_words` reads ({"words": [{"word", "start", "end"}]}, seconds)."""
    words = transcribe.response_words(resp)
    if not words:
        raise VPError("data", "no speech was recognised in the recording")
    return {"words": [{"word": w["text"], "start": round(w["start"] / 1000, 3),
                       "end": round(w["end"] / 1000, 3)} for w in words],
            "language": resp.get("language_code"),
            "source": {"service": transcribe.SERVICE, "model": transcribe.MODEL_ID,
                       "transcription_id": resp.get("transcription_id")}}


def _match(job: Job, doc: dict[str, Any]) -> tuple[int, int]:
    """(matched, total): how many words of the person's subtitles were found in the word times."""
    from .steps._s2_retime import load_words
    try:
        found = align_words.align(read_srt(job.source_srt), load_words(doc))
    except (ValueError, OSError, VPError):
        return 0, 0
    return found.matched, found.total


def exact(job: Job, video: str | os.PathLike | None = None, language: str | None = None,
          urlopen: transcribe.Urlopen | None = None,
          probe: Callable[[Path], dict[str, Any]] | None = None) -> dict[str, Any]:
    """The person's click: get the recording's word times for the captions. Sends the sound (never
    the picture) of the recording once, unless a saved answer for this recording is here. Writes
    `captions.words.json`, `transcript.raw.json` and `captions.choice.yaml`; touches nothing else.
    Holds the job lock, so it never writes under a running `vp run`."""
    with job_lock(job) as got:
        if not got:
            raise VPError("data", "the job is busy (another run holds job.lock): try again when it ends")
        return _exact_locked(job, video, language, urlopen, probe)


def _exact_locked(job: Job, video: str | os.PathLike | None, language: str | None,
                  urlopen: transcribe.Urlopen | None,
                  probe: Callable[[Path], dict[str, Any]] | None) -> dict[str, Any]:
    if not job.source_srt.exists():
        raise VPError("data", "the job has no subtitles yet: `vp transcribe run` makes the "
                              "subtitles and the word times together")
    have = word_times(job)
    if have != "none":
        return {"word_times": have, "sent": False, "words": None,
                "note": "the captions already have exact word times: nothing was sent"}
    est = transcribe.estimate(job, video, probe)
    if est["too_long"]:
        raise VPError("data", f"the recording is longer than {transcribe.MAX_DURATION_S // 3600} h: "
                              "ElevenLabs takes at most that per file")
    vp = Path(est["video"])
    language = language or (str(job.request().get("language") or "") or None)
    vid = transcribe._video_id(vp)
    saved = _saved_response(job, vp)
    if saved is not None:
        resp, created, sent = saved["response"], saved.get("created_at"), False
    else:
        key, _ = transcribe.find_key()
        if not key:
            raise VPError("auth", "NEED_KEY:elevenlabs — no ElevenLabs key yet: copy the key, then run "
                                  "`vp keys set elevenlabs --from-clipboard`")
        with progress.tracking(job.root, "captions", None, total=1, unit="bytes"), \
                tempfile.TemporaryDirectory(prefix="vp-captions-") as tmp:
            audio = transcribe._extract_audio(vp, Path(tmp))        # the sound only, never the picture
            resp = transcribe.call_api(audio, key, language, False, urlopen)
        created, sent = now_iso(), True
        write_json(job.root / transcribe.RAW_NAME,
                   {"service": transcribe.SERVICE, "model": transcribe.MODEL_ID, "created_at": created,
                    "video_id": vid, "diarize": False, "response": resp})
        _record_cost(job, est["duration_s"])
    doc = words_json(resp)
    matched, total = _match(job, doc)
    write_json(job.caption_words, doc)
    res = {"choice": "exact", "at": now_iso(), "service": transcribe.SERVICE, "model": transcribe.MODEL_ID,
           "created_at": created, "sent": sent, "duration_s": est["duration_s"],
           "cost_usd_estimate": transcribe.cost_usd(est["duration_s"]) if sent else 0.0,
           "price_source": transcribe.PRICE_SOURCE, "words": len(doc["words"]),
           "matched": matched, "total": total, "language": resp.get("language_code")}
    write_yaml(job.caption_choice, res)
    return {**res, "word_times": "captions", "file": job.rel(job.caption_words),
            # fewer than half of the subtitle words found: S5 will not use these times and the
            # captions stay at the local estimate — the caller must say so, not "done"
            "usable": bool(total) and matched / total >= align_words.MIN_MATCH_SHARE}


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
    prev = choice(job)
    if prev and prev["choice"] == "exact":
        return {"choice": "exact", "note": "exact word times were already obtained: they stay in use"}
    res = {"choice": "keep", "at": now_iso()}
    write_yaml(job.caption_choice, res)
    return res


# ---- CLI ---------------------------------------------------------------------------------------

def _plan_lines(p: dict[str, Any]) -> list[str]:
    lines = [f"captions: word times {p['word_times']}, look {p['look']}, reels "
             f"{p['reels'] if p['reels'] is not None else 'not chosen yet'}"]
    if p["obtainable"]:
        lines.append(f"exact timing ≈ ${p['cost_usd']:.2f} ({p['service']} {p['model']}: recording "
                     f"{p['recording_s']} s" + (" — saved answer, nothing to send" if p["saved_answer"] else "")
                     + f" + reels {p['reels_s']} s)")
    elif p["why_not"]:
        lines.append(f"exact timing not possible: {p['why_not']}")
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
        if name == "exact":
            s.add_argument("--language", help="ISO 639-1/3 hint, e.g. ru (default: request.yaml "
                                              "`language`, else auto)")
    a = ap.parse_args(argv)
    job = Job(a.job)
    if a.cmd == "plan":
        res = plan(job, a.video)
        lines = _plan_lines(res)
    elif a.cmd == "keep":
        res = keep(job)
        lines = [f"KEPT the captions as they are ({res['choice']})"]
    else:
        res = exact(job, a.video, a.language)
        if not res.get("sent") and res.get("words") is None:
            lines = [f"NOTHING SENT: {res['note']}"]
        else:
            lines = [f"WORD TIMES {res['file']}: {res['words']} words, {res['matched']} of "
                     f"{res['total']} subtitle words found"
                     + ("" if res["sent"] else " (saved answer reused, nothing paid)")
                     + ("" if res["usable"] else " — too few to use: the captions stay as they are")]
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        for ln in lines:
            print(ln)
    return 0
