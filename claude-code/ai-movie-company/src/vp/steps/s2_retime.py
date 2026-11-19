"""S2 — retiming (SPEC S2). Code only; a port of `src/prototype/retime.py` with all guarantees.

Input: s1_proofread/cues.json (source timings, speakers, chunk assignment) + out_NN.txt (proofread
text, possibly edited by a human; validated by the same `ID|text` contract) + in `words` mode the
words file. Title start = start of its first source cue (no start drift by construction).

Gate: any HARD violation or a RATE above 0.5 % -> subtitles.srt is NOT written (an old one is
removed), subtitles.srt.rejected.json is written, VPError("data"). In `srt_only` mode a cue long by
time and indivisible by text therefore fails (long>7s); in `words` mode it is split by the word
timestamps (no `estimated` pieces).

Outputs: subtitles.srt + subtitles.debug.json.
"""
from __future__ import annotations

import json
from typing import Any

from ..core.io import atomic_write, read_json, read_yaml, write_json
from ..core.srt import Cue, format_srt
from .base import Ctx, StepSpec, VPError
from . import _s2_retime as rt
from .s1_proofread import n_chunks, read_texts


def boundary_mode(ctx: Ctx) -> str:
    job = read_yaml(ctx.job.job_yaml, default={}) or {}
    mode = job.get("boundary_mode") or "srt_only"
    if mode not in ("words", "srt_only"):
        raise VPError("data", f"job.yaml: unknown boundary_mode {mode!r}")
    return mode


def fragments(cues: list[dict], texts: dict[str, str]) -> list[dict]:
    """cues.json entries + proofread texts -> retime fragments (filtered cues have text "")."""
    return [{"id": str(c["id"]), "start": int(c["start_ms"]), "end": int(c["end_ms"]),
             "spk": c.get("speaker") if c.get("speaker") is not None else "?",
             "text": texts.get(str(c["id"]), "")} for c in cues]


def render_srt(titles: list[dict]) -> str:
    """Titles -> SRT text, numbered 1..n, wrapped (byte-identical to the prototype output)."""
    return format_srt([Cue(str(i), t["start"], t["end"], rt.wrap(t["text"]))
                       for i, t in enumerate(titles, 1)])


def retime(frags: list[dict], words: list[dict] | None = None) -> dict[str, Any]:
    """Pure S2: {titles, issues, drift, violations, stats}. `words` (ms) switches on words mode."""
    words_by_id = rt.words_for_cues(frags, words) if words is not None else None
    titles = rt.build(frags, words_by_id)
    issues, drift = rt.validate(frags, titles)
    return {"titles": titles, "issues": issues, "drift": drift,
            "violations": rt.violations(issues, len(titles)), "stats": rt.stats(titles, drift)}


class S2Retime(StepSpec):
    id, deps, scope, version, gate = "S2", ("S1",), "job", "1", None

    def inputs(self, ctx):
        j = ctx.job
        ins = [j.s1_cues] + [j.s1_out(k) for k in range(n_chunks(ctx))]
        if j.job_yaml.exists() and boundary_mode(ctx) == "words":
            ins.append(j.source_words)
        return ins

    def config(self, ctx):
        return {"constants": rt.CONSTANTS,
                "boundary_mode": boundary_mode(ctx) if ctx.job.job_yaml.exists() else None}

    def outputs(self, ctx):
        return [ctx.job.subtitles_srt, ctx.job.subtitles_debug]

    def run(self, ctx):
        j = ctx.job
        cues = read_json(j.s1_cues, default=None)
        if not isinstance(cues, list) or not cues:
            raise VPError("data", "s1_proofread/cues.json is missing or empty")
        texts = read_texts(j, cues)
        frags = fragments(cues, texts)
        mode = boundary_mode(ctx)
        words = None
        if mode == "words":
            try:
                words = rt.load_words(read_json(j.source_words))
            except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
                raise VPError("data", f"words file unreadable in words mode: {e}") from e
        res = retime(frags, words)
        titles, bad = res["titles"], res["violations"]
        if bad:
            for p in (j.subtitles_srt, j.subtitles_debug):
                if p.exists():
                    p.unlink()
            write_json(j.subtitles_rejected, {"violations": bad, "issues": res["issues"],
                                              "mode": mode, "titles": titles}, indent=0)
            raise VPError("data", f"S2 guarantees violated: {bad}", violations=bad,
                          rejected=j.rel(j.subtitles_rejected))
        atomic_write(j.subtitles_srt, render_srt(titles))
        write_json(j.subtitles_debug, {"mode": mode, "fragments": len(frags), "stats": res["stats"],
                                       "issues": res["issues"], "titles": titles}, indent=0)
        if j.subtitles_rejected.exists():
            j.subtitles_rejected.unlink()
        st = res["stats"]
        return {"mode": mode, "fragments": len(frags), "titles": st["titles"],
                "estimated": st["estimated"], "words_timed": st["words_timed"],
                "trimmed": st["trimmed"]}


STEP = S2Retime()
