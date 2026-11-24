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

from typing import Any

from .. import transcribe
from ..core.io import MISSING, atomic_write, read_json, read_yaml, sha256_obj, write_json
from ..core.manifest import hash_files, read_manifest
from ..core.srt import Cue, format_srt
from .base import StepSpec, VPError
from . import _s2_retime as rt
from .s0_intake import (INFERRED_WORD_TIMES, closed_review_matches, intake_timing_path,
                        recognition_inputs, staged_word_times)
from .s1_proofread import n_chunks, read_texts

KEY_CONSTANTS_088 = {
    "MAX_CHARS": 80, "LINE": 42, "MAX_SPAN": 6500, "MIN_DUR": 1000, "GAP": 80,
    "CHAIN": 400, "BREAK_GAP": 1000, "SENT_MIN": 15, "ACCEPT_CHARS": 84,
    "HARD": ["nonpositive", "overlap", "nonmonotonic", "long>7s", "ids_mismatch",
             "pause_inside", "drift", "empty"],
    "RATE": ["short<0.7s", "chars>84"], "RATE_MAX": 0.005,
}


def boundary_mode(job: dict) -> str:
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

    def _boundary_inputs(self, ctx):
        j = ctx.job
        ins = [j.s1_cues] + [j.s1_out(k) for k in range(n_chunks(ctx))]
        if self.config(ctx)["boundary_mode"] == "words":
            ins.append(j.source_words)
        return ins

    def inputs(self, ctx):
        return self._boundary_inputs(ctx) + recognition_inputs(ctx.job, self.id)

    def config(self, ctx):
        # Only boundary_mode from job.yaml affects the retiming algorithm. The intake sidecar
        # carries diagnostic reasons and provenance, which do not change subtitle freshness.
        return {"constants": KEY_CONSTANTS_088,
                "boundary_mode": boundary_mode(read_yaml(ctx.job.job_yaml, default={}) or {})
                if ctx.job.job_yaml.exists() else None}

    def outputs(self, ctx):
        return [ctx.job.subtitles_srt, ctx.job.subtitles_debug]

    def _keep_reviewed_subtitles(self, ctx) -> bool:
        """Register a caption timing edit without changing the subtitles used for selection."""
        j = ctx.job
        saved = read_manifest(j, self.id) or {}
        if saved.get("version") != self.version or saved.get("config_hash") != sha256_obj(self.config(ctx)):
            return False
        previous = saved.get("inputs") or {}
        boundaries = hash_files(j, self._boundary_inputs(ctx))
        if any(previous.get(path) != digest for path, digest in boundaries.items()):
            return False
        recognition = hash_files(j, recognition_inputs(j, self.id))
        if all(previous.get(path) == digest for path, digest in recognition.items()) or not closed_review_matches(j):
            return False
        outputs = hash_files(j, self.outputs(ctx))
        return MISSING not in outputs.values() and outputs == saved.get("outputs")

    def run(self, ctx):
        j = ctx.job
        if self._keep_reviewed_subtitles(ctx):
            print("S2 kept the subtitles and boundaries from the closed fragment review.", flush=True)
            return {"kept_reviewed_subtitles": True}
        cues = read_json(j.s1_cues, default=None)
        if not isinstance(cues, list) or not cues:
            raise VPError("data", "s1_proofread/cues.json is missing or empty")
        texts = read_texts(j, cues)
        frags = fragments(cues, texts)
        job = read_yaml(j.job_yaml, default={}) or {}
        mode = boundary_mode(job)
        words = None
        timing_meta = {}
        intake_timing = read_yaml(intake_timing_path(j), default={}) or {}
        reason = intake_timing.get("word_times_reason", job.get("word_times_reason"))
        if mode == "words":
            timing = staged_word_times(j)
            words = [{"w": w, "start": start, "end": end, "provenance": origin}
                     for w, start, end, origin in timing.words
                     if timing.legacy_rows or origin != "inferred"] or None
            if words:
                timing_meta["word_times_provenance"] = timing.provenance
            else:
                mode = "srt_only"
                reason = timing.reason or INFERRED_WORD_TIMES
        if not words and reason:
            timing_meta["word_times_reason"] = reason
            if reason != transcribe.NO_WORD_TIMES:
                print(f"S2 is using subtitles only because {reason}.", flush=True)
        res = retime(frags, words)
        titles, bad = res["titles"], res["violations"]
        if bad:
            for p in (j.subtitles_srt, j.subtitles_debug):
                if p.exists():
                    p.unlink()
            write_json(j.subtitles_rejected, {"violations": bad, "issues": res["issues"],
                                              "mode": mode, "titles": titles, **timing_meta}, indent=0)
            raise VPError("data", f"S2 guarantees violated: {bad}", violations=bad,
                          rejected=j.rel(j.subtitles_rejected))
        atomic_write(j.subtitles_srt, render_srt(titles))
        write_json(j.subtitles_debug, {"mode": mode, "fragments": len(frags), "stats": res["stats"],
                                       "issues": res["issues"], "titles": titles, **timing_meta}, indent=0)
        if j.subtitles_rejected.exists():
            j.subtitles_rejected.unlink()
        st = res["stats"]
        return {"mode": mode, "fragments": len(frags), "titles": st["titles"],
                "estimated": st["estimated"], "words_timed": st["words_timed"],
                "trimmed": st["trimmed"], **timing_meta}


STEP = S2Retime()
