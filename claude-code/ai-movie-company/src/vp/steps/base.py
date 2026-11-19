"""What a step module needs, in one import.

    from vp.steps.base import StepSpec, Ctx, VPError, WaitingHuman, JudgmentPending, Inconclusive, Skip

    class S4Cut(StepSpec):
        id, deps, scope, version, gate = "S4", ("S3b",), "item", "1", None
        def inputs(self, ctx):  return [ctx.job.source_video, ctx.job.fragments_yaml, ctx.job.subtitles_srt]
        def config(self, ctx):  return {"reel_crop": ctx.profile["reel_crop"]}
        def outputs(self, ctx): return [ctx.job.cut_mp4(ctx.item), ctx.job.clip_srt(ctx.item)]
        def run(self, ctx):     ...; return {"duration_ms": ...}

    STEP = S4Cut()

Rules for steps (BUILD §0, §3):
  * read only declared inputs; write only declared outputs (+ judge/ files via ctx.judge);
  * write every file with vp.core.io.atomic_write / write_yaml / write_json / write_srt;
  * never write the manifest yourself — the runner writes it last;
  * wrap network calls in ctx.retry(fn); raise VPError(kind, msg) for failures;
  * a model never sees timecodes / ids / URLs; validate every judge output (else VPError("contract")).
"""
from __future__ import annotations

from ..core.ctx import Ctx
from ..core.errors import (Inconclusive, JudgmentPending, Skip, VPError, WaitingHuman,
                           with_retries)
from ..core.graph import StepSpec, make_step
from ..core.paths import FULL, Job

__all__ = ["StepSpec", "Ctx", "Job", "FULL", "VPError", "WaitingHuman", "JudgmentPending",
           "Inconclusive", "Skip", "with_retries", "make_step", "item_kind"]


def item_kind(ctx: Ctx) -> str:
    """'full' for the full video, else the fragment type from fragments.yaml (reel|long)."""
    if ctx.item == FULL:
        return "full"
    from ..core.io import read_yaml
    data = read_yaml(ctx.job.fragments_yaml, default={}) or {}
    for f in data.get("fragments") or []:
        if str(f.get("id")) == ctx.item:
            kind = f.get("type") or f.get("kind")
            if kind in ("reel", "long"):
                return kind
            raise VPError("data", f"fragment {ctx.item}: unknown type {kind!r}")
    raise VPError("data", f"fragment {ctx.item} not in fragments.yaml")
