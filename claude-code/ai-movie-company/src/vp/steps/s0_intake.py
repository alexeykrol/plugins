"""S0 — intake and checks (SPEC S0). Code only.

Gate: the route starts only when request.yaml exists with `enabled: true` (A3). Otherwise the step
raises Inconclusive("route disabled ...") — the run stops (exit 0) and says why; no file is written,
so the step re-runs as soon as request.yaml changes.

Checks: request `platforms` and `brief` (optional text, at most 2000 characters); sources present;
ffprobe (duration, resolution, fps, audio); SRT parses and is non-empty; SRT duration <= video
duration + 1 s; profile known; boundary mode `words` iff the words file has word timings, else
`srt_only`; YouTube channel of the token == configured channel (auth problem or mismatch ->
VPError auth, the route does not start). Output: job.yaml (deterministic — no clock values, so
re-running S0 on unchanged inputs does not cascade). The brief is only checked here, not copied
into job.yaml: S3a reads it from the request, so a changed brief re-runs the selection (S3a and
what follows from it) and leaves S1/S2 alone.
"""
from __future__ import annotations

import json
from typing import Any

from ..adapters import media
from ..core.config import request_brief
from ..core.io import read_json, write_yaml
from ..core.srt import read_srt, srt_duration_ms
from .base import Ctx, Inconclusive, StepSpec, VPError
from ._s2_retime import load_words

CONTRACT_VERSION = 1
SRT_SLACK_MS = 1000
KNOWN_PLATFORMS = ("youtube",)


def boundary_mode(ctx: Ctx) -> tuple[str, int]:
    """('words', n_words) if the words file exists and carries word timings, else ('srt_only', 0).
    A words file that exists but is not valid JSON / has broken timings -> VPError(data)."""
    p = ctx.job.source_words
    if not p.exists():
        return "srt_only", 0
    try:
        data = read_json(p)
        words = load_words(data)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        raise VPError("data", f"words file {p.name} is unreadable: {e}") from e
    return ("words", len(words)) if words else ("srt_only", 0)


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
        return [j.request_yaml, j.source_video, j.source_srt, j.source_words]

    def config(self, ctx):
        return {"contract_version": CONTRACT_VERSION, "profile": ctx.config.get("profile_name"),
                "publisher": ctx.config.get("publisher", "youtube"),
                "channel_id": (ctx.config.get("youtube") or {}).get("channel_id")}

    def outputs(self, ctx):
        return [ctx.job.job_yaml]

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

        for label, p in (("video", j.source_video), ("srt", j.source_srt)):
            if not p.exists():
                raise VPError("data", f"source {label} not found: {p.name}")
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
        mode, n_words = boundary_mode(ctx)
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
        write_yaml(j.job_yaml, job)
        return {"boundary_mode": mode, "profile": profile, "cues": len(cues),
                "duration_ms": info["duration_ms"], "channel_verified": bool(channel and channel["verified"])}


STEP = S0Intake()
