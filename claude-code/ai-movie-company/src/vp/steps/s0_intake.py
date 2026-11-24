"""S0 — intake and checks (SPEC S0). Code only.

Gate: the route starts only when request.yaml exists with `enabled: true` (A3). Otherwise the step
raises Inconclusive("route disabled ...") — the run stops (exit 0) and says why; no file is written,
so the step re-runs as soon as request.yaml changes.

Checks: request `platforms` and `brief` (optional text, at most 2000 characters); sources present
(video missing -> VPError data; video present but no SRT -> Inconclusive with the reason code
`NEED_TRANSCRIPT`, the run stops like a gate: `INCONCLUSIVE S0: NEED_TRANSCRIPT: ...`, exit 0);
ffprobe (duration, resolution, fps, audio); SRT parses and is non-empty; SRT duration <= video
duration + 1 s; profile known; boundary mode `words` when the source transcript provides usable
word timings, otherwise `srt_only`; YouTube channel of the token == configured channel
(auth problem or mismatch -> VPError auth). Outputs are deterministic job.yaml and a separate
word timing report. S3a reads the brief directly from the request, so a changed brief reruns
selection and leaves S1/S2 fresh.
"""
from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any

from .. import transcribe
from ..adapters import media
from ..analysis import align_words
from ..core.adoption import recognition_inputs
from ..core.config import request_brief
from ..core.io import MISSING, read_json, sha256_file, sha256_obj, write_yaml
from ..core.manifest import hash_files, read_manifest
from ..core.paths import Job
from ..core.srt import read_srt, srt_duration_ms
from .base import Ctx, Inconclusive, StepSpec, VPError
from ._s2_retime import load_words

CONTRACT_VERSION = 1
KEY_CONTRACT_VERSION_088 = 1
SRT_SLACK_MS = 1000
KNOWN_PLATFORMS = ("youtube",)
NEED_TRANSCRIPT = "NEED_TRANSCRIPT"   # reason code: video present, subtitles missing
INFERRED_WORD_TIMES = "the saved word times only contain inferred boundaries"


def intake_timing_path(job: Job) -> Path:
    return job.root / "intake.word-times.yaml"


def _group_transcript_words(job: Job, words: list[tuple[str, int, int, str]], *,
                            legacy_rows: bool = False) -> list[tuple[str, int, int, str]]:
    """Preserve the original S0 word count and S2 alignment for accepted transcript rows."""
    # The timing reader gives every token from the same row identical start and end times.
    if not any(a[1:3] == b[1:3] for a, b in pairwise(words)):
        return words
    if not legacy_rows and any(origin == "inferred" for _, _, _, origin in words):
        return words
    try:
        rows = load_words(read_json(job.source_words))
    except (OSError, TypeError, ValueError, OverflowError):
        return words
    flattened = [(token, row["start"], row["end"]) for row in rows for token in row["w"].split()]
    if flattened != [word[:3] for word in words]:
        return words
    grouped = []
    offset = 0
    for row in rows:
        grouped.append((row["w"], row["start"], row["end"], words[offset][3]))
        offset += len(row["w"].split())
    return grouped


def staged_word_times(job: Job, *, probe=None) -> transcribe.JobWordTimes:
    """Read the boundary source independently of any newer purchase made for captions."""
    if not job.source_words.is_file():
        reason = ("the available word times are separate from the source transcript"
                  if job.caption_words.is_file() else transcribe.NO_WORD_TIMES)
        return transcribe.JobWordTimes([], reason=reason)
    unreadable = "the word times of the recording could not be read"
    doc = transcribe._document(job.source_words)
    if not doc:
        return transcribe.JobWordTimes([], reason=unreadable)
    provenance, reason = transcribe._timing_validation(job, job.source_video, doc, "recording", probe)
    if reason:
        return transcribe.JobWordTimes([], reason=reason)
    source = doc["source"] if isinstance(doc.get("source"), dict) else {}
    digest = source.get("recording_sha256") or doc.get("recording_sha256")
    provider = transcribe.SERVICE in (doc.get("service"), source.get("service"))
    try:
        words = align_words.source_words(doc, legacy=provider and not digest)
    except (TypeError, ValueError, OverflowError):
        return transcribe.JobWordTimes([], reason=unreadable)
    if not words:
        return transcribe.JobWordTimes([], reason=transcribe.NO_WORD_TIMES)
    if provenance == transcribe.LEGACY_UNVERIFIED:
        words = [(word, start, end, provenance if origin == "measured" else origin)
                 for word, start, end, origin in words]
    legacy_rows = provider and transcribe.legacy_word_rows(doc, source)
    return transcribe.JobWordTimes(_group_transcript_words(job, words, legacy_rows=legacy_rows),
                                   provenance, "source_words", owned=bool(digest or provider),
                                   legacy_rows=legacy_rows)


def closed_review_matches(job: Job) -> bool:
    review = read_manifest(job, "S3b") or {}
    saved = (review.get("outputs") or {}).get(job.rel(job.fragments_yaml))
    return bool(saved and saved != MISSING and sha256_file(job.fragments_yaml) == saved)


def _reviewed_sources_unchanged(job: Job) -> bool:
    previous = (read_manifest(job, "S0") or {}).get("inputs") or {}
    current = hash_files(job, (job.source_video, job.source_srt, job.source_words))
    return all(previous.get(path) == digest for path, digest in current.items())


def _changed_intake_fields(previous: dict, current: dict, prefix: str = "") -> list[str]:
    changed = []
    for name in sorted(previous.keys() | current.keys(), key=str):
        old, live = previous.get(name), current.get(name)
        if name in previous and name in current and type(old) is type(live) and old == live:
            continue
        field = f"{prefix}{name}"
        if isinstance(old, dict) and isinstance(live, dict):
            changed.extend(_changed_intake_fields(old, live, f"{field}."))
        else:
            changed.append(field)
    return changed


def check_channel(ctx: Ctx) -> dict[str, Any]:
    """Channel of the publisher token vs configured `youtube.channel_id`.

    Mismatch -> VPError(auth). Publisher auth errors propagate (auth). With the fake publisher and
    no configured channel the token channel is accepted; with a real publisher a configured channel
    is required (VPError data). With publisher fake (config or request.yaml) the configured
    real channel is not compared: a rehearsal never touches it."""
    actual = ctx.retry(lambda: ctx.publisher.channel_id())
    kind = ctx.config.get("publisher", "youtube")
    # a rehearsal (publisher fake) acts on its own fake channel: the real channel id is not compared
    expected = None if kind == "fake" else (ctx.config.get("youtube") or {}).get("channel_id")
    if not actual:
        raise VPError("auth", "publisher returned no channel for the token")
    if expected is None:
        if kind != "fake":
            raise VPError("data", "config youtube.channel_id is not set: cannot verify the channel")
    elif str(expected) != str(actual):
        raise VPError("auth", "token channel does not match the configured channel",
                      expected=str(expected), actual=str(actual))
    return {"publisher": kind, "channel_id": str(actual), "verified": expected is not None,
            "expected_handle": (ctx.config.get("youtube") or {}).get("expected_channel_handle")}


class S0Intake(StepSpec):
    id, deps, scope, version, gate = "S0", (), "job", "1", None

    def inputs(self, ctx):
        j = ctx.job
        return [j.request_yaml, j.source_video, j.source_srt, j.source_words, *recognition_inputs(j, self.id)]

    def config(self, ctx):
        return {"contract_version": KEY_CONTRACT_VERSION_088, "profile": ctx.config.get("profile_name"),
                "publisher": ctx.config.get("publisher", "youtube"),
                "channel_id": (ctx.config.get("youtube") or {}).get("channel_id")}

    def outputs(self, ctx):
        return [ctx.job.job_yaml, intake_timing_path(ctx.job)]

    def run(self, ctx):
        j = ctx.job
        if not j.request_yaml.exists():
            raise Inconclusive("route disabled: request.yaml is missing — the route does not start")
        req = j.request()
        if req.get("enabled") is not True:
            raise Inconclusive("route disabled: request.yaml has enabled != true — the route does not start",
                               report=j.rel(j.request_yaml))
        platforms = req.get("platforms", ["youtube"])
        if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
            raise VPError("data", "request.yaml platforms must be a list of names")
        unknown = [p for p in platforms if p not in KNOWN_PLATFORMS]
        if unknown:
            raise VPError("data", f"unknown platforms {unknown}; known: {list(KNOWN_PLATFORMS)}")
        request_brief(req)  # a bad `brief` stops the route here, before any model work (S3a uses it)
        transcribe.guard_inputs(j, *self.outputs(ctx))

        if not j.source_video.exists():
            raise VPError("data", f"source video not found: {j.source_video.name}")
        if not j.source_srt.exists():
            # a recording without subtitles is a normal start (newcomers): the run stops like a
            # gate with a reason code the skill acts on (`vp transcribe`); nothing is written, so
            # S0 re-runs as soon as the subtitles appear
            raise Inconclusive(f"{NEED_TRANSCRIPT}: the recording has no subtitles "
                               f"({j.source_srt.name}) — `vp transcribe estimate` / `run`, "
                               "or `vp transcribe import` for the person's own file")
        info = media.probe(j.source_video)
        if not info.get("width") or not info.get("duration_ms"):
            raise VPError("data", "source video has no video stream or zero duration")
        if not info.get("has_audio"):
            raise VPError("data", "source video has no audio stream")

        cues = read_srt(j.source_srt)
        if not cues:
            raise VPError("data", "source SRT has no cues")
        srt_ms = srt_duration_ms(cues)
        if srt_ms > info["duration_ms"] + SRT_SLACK_MS:
            raise VPError("data", f"SRT ends at {srt_ms} ms, after the video end "
                                  f"{info['duration_ms']} ms + {SRT_SLACK_MS} ms")

        profile = ctx.config.get("profile_name")
        if not profile or not ctx.profile:
            raise VPError("data", "profile is not determined")
        timing = staged_word_times(j, probe=lambda _: info)
        n_words = sum(timing.legacy_rows or origin != "inferred" for _, _, _, origin in timing.words)
        mode = "words" if n_words else "srt_only"
        if n_words:
            timing_meta = {"word_times_provenance": timing.provenance}
        else:
            reason = timing.reason or INFERRED_WORD_TIMES
            timing_meta = {"word_times_reason": reason}
        channel = check_channel(ctx) if "youtube" in platforms else None

        job = {
            "contract_version": CONTRACT_VERSION,
            "job_id": j.job_id,
            "sources": {"video": j.rel(j.source_video), "srt": j.rel(j.source_srt),
                        "words": j.rel(j.source_words) if j.source_words.exists() else None},
            "video": {k: info.get(k) for k in ("duration_ms", "width", "height", "fps", "has_audio",
                                               "video_codec", "audio_codec", "sample_rate")},
            "srt": {"cues": len(cues), "duration_ms": srt_ms},
            "profile": profile,
            "boundary_mode": mode,
            "words": n_words,
            "platforms": platforms,
            "channel": channel,
            "stages": req.get("stages") or {},
        }
        previous = transcribe._document(j.job_yaml, yaml=True)
        # Keep the reviewed timing interpretation when its sources have not changed. The rest
        # of the intake stays live because selection and review also depend on that data.
        reviewed = closed_review_matches(j)
        if (reviewed and previous.get("boundary_mode") in ("words", "srt_only")
                and "words" in previous and _reviewed_sources_unchanged(j)):
            if previous.get("words") != n_words:
                timing_meta["usable_words"] = n_words
            job.update(boundary_mode=previous["boundary_mode"], words=previous["words"])
        mode = job["boundary_mode"]
        if sha256_obj(previous) != sha256_obj(job):
            write_yaml(j.job_yaml, job)
            if reviewed and previous:
                fields = ", ".join(_changed_intake_fields(previous, job))
                print(f"S0 updated the intake used for fragment selection and review: {fields}.", flush=True)
        if mode == "srt_only" and timing_meta.get("word_times_reason") not in (None, transcribe.NO_WORD_TIMES):
            print(f"S0 is using subtitles only because {timing_meta['word_times_reason']}.", flush=True)
        write_yaml(intake_timing_path(j), timing_meta)
        return {"boundary_mode": mode, "profile": profile, "cues": len(cues),
                "duration_ms": info["duration_ms"], "channel_verified": bool(channel and channel["verified"]),
                **timing_meta}


STEP = S0Intake()
