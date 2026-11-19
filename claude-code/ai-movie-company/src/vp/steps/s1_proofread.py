"""S1 — proofreading (SPEC S1). Judgment by the model per chunk, bookkeeping by code.

Flow:
  1. Code filter BEFORE the model: prompt echo of the transcription prompt, subtitle-editor credits,
     cues after the end of speech (start of the trailing audio silence). Filtered cues never reach
     the model; S2 treats them as emptied.
  2. Remaining cues -> chunks of `ID|text` (~profile s1.chunk_size, nearly equal sizes) ->
     one judge task per chunk `s1.proofread.<NN>` (prompt: vp/prompts/s1_proofread.md).
  3. Every answer is validated by code (_s1_contract: all IDs, order, no dup/extra, no '|');
     echo/credit patterns must not reappear. Any violation -> VPError("contract"), nothing written.
  4. Result must be non-empty; share of cues emptied by the model > s1.empty_share_max ->
     outputs + report written, then Inconclusive.
  5. meaning_risk (code signal, not a verdict) per cue into report.json.

Outputs: s1_proofread/{cues.json, in_NN.txt, out_NN.txt, report.json}.

Human edits: out_NN.txt is S2's input. If a human edited out_NN.txt (its hash differs from the
machine answer recorded in report.json) and in_NN.txt is unchanged, the human version is kept
(validated by the same contract) and flows downstream. A judge answer whose task (prompt + inputs)
is unchanged is reused from judge/<task>/output.txt instead of asking the model again.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters import media
from ..core.io import (atomic_write, read_json, read_yaml, sha256_bytes, sha256_file, sha256_obj,
                       write_json)
from ..core.judge import compose_prompt
from ..core.srt import read_srt
from .base import Ctx, Inconclusive, JudgmentPending, StepSpec, VPError
from ._s1_contract import format_chunk, parse_chunk
from ._s1_text import (CREDIT_PATTERNS, ECHO_PATTERNS, FILLERS, NEGATIONS, chunk, compile_patterns,
                       contains_garbage, filter_cues, meaning_risk, sanitize, speech_end_ms)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "s1_proofread.md"
OUTPUT_NAME = "output.txt"
DEFAULT_CHUNK = 350
DEFAULT_EMPTY_MAX = 0.15


def task_name(n: int) -> str:
    return f"s1.proofread.{n:02d}"


def load_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise VPError("internal", f"prompt not found: {PROMPT_PATH}") from e


def s1_settings(ctx: Ctx) -> dict[str, Any]:
    """profile `s1` block with defaults. Keys: chunk_size, empty_share_max, echo_patterns (extra
    regexes), after_speech_filter (bool), silence_db, silence_min_s."""
    s = dict(ctx.profile.get("s1") or {})
    req_s1 = ctx.job.request().get("s1") or {}
    if isinstance(req_s1, dict):
        s.update(req_s1)
    return {
        "chunk_size": int(s.get("chunk_size") or DEFAULT_CHUNK),
        "empty_share_max": float(s.get("empty_share_max", DEFAULT_EMPTY_MAX)),
        "echo_patterns": [str(p) for p in (s.get("echo_patterns") or [])],
        "after_speech_filter": bool(s.get("after_speech_filter", True)),
        "silence_db": float(s.get("silence_db", -40.0)),
        "silence_min_s": float(s.get("silence_min_s", 0.5)),
    }


def names_dictionary(ctx: Ctx) -> list[str]:
    """Names from the profile (`names`) and request.yaml (`names:`), de-duplicated, order kept."""
    out: list[str] = []
    for src in (ctx.profile.get("names") or [], ctx.job.request().get("names") or []):
        if isinstance(src, str):
            src = [src]
        for n in src:
            n = str(n).strip()
            if n and n not in out:
                out.append(n)
    return out


def n_chunks(ctx: Ctx) -> int:
    """Number of chunks recorded in cues.json (0 if absent)."""
    cues = read_json(ctx.job.s1_cues, default=None)
    if not isinstance(cues, list):
        return 0
    ks = [c.get("chunk") for c in cues if isinstance(c, dict) and c.get("chunk") is not None]
    return max(ks) + 1 if ks else 0


def chunk_ids(cues: list[dict]) -> list[list[str]]:
    """IDs per chunk from cues.json entries."""
    n = max((c["chunk"] for c in cues if c.get("chunk") is not None), default=-1) + 1
    out: list[list[str]] = [[] for _ in range(n)]
    for c in cues:
        if c.get("chunk") is not None:
            out[c["chunk"]].append(str(c["id"]))
    return out


def read_texts(job, cues: list[dict]) -> dict[str, str]:
    """Validated proofread text per cue id from out_NN.txt (filtered cues -> ""). Used by S2."""
    texts = {str(c["id"]): "" for c in cues}
    for k, ids in enumerate(chunk_ids(cues)):
        p = job.s1_out(k)
        if not p.exists():
            raise VPError("data", f"{p.name} is missing")
        for i, t in parse_chunk(p.read_text(encoding="utf-8"), ids, label=p.name):
            texts[i] = t
    return texts


def _speech_end(ctx: Ctx, st: dict[str, Any]) -> int | None:
    if not st["after_speech_filter"]:
        return None
    src = ctx.job.source_video
    if not src.exists():
        return None
    info = media.probe(src)
    if not info.get("has_audio"):
        return None
    sil = media.silences(src, noise_db=st["silence_db"], min_s=st["silence_min_s"])
    return speech_end_ms(sil, info["duration_ms"])


class S1Proofread(StepSpec):
    id, deps, scope, version, gate = "S1", ("S0",), "job", "1", None

    def inputs(self, ctx):
        j = ctx.job
        ins = [j.source_srt]
        if s1_settings(ctx)["after_speech_filter"]:
            ins.append(j.source_video)
        return ins

    def config(self, ctx):
        st = s1_settings(ctx)
        return {**st, "prompt_sha": sha256_bytes(load_prompt()), "names": names_dictionary(ctx),
                "filters_sha": sha256_obj([ECHO_PATTERNS, CREDIT_PATTERNS, sorted(FILLERS),
                                           sorted(NEGATIONS)]),
                "judge_model": ctx.config.get("judge_model")}

    def outputs(self, ctx):
        j = ctx.job
        n = n_chunks(ctx)
        return ([j.s1_cues] + [j.s1_in(k) for k in range(n)] + [j.s1_out(k) for k in range(n)]
                + [j.s1_report])

    # ---- judge with reuse ---------------------------------------------------------------------
    def _ask(self, ctx: Ctx, task: str, prompt: str, inputs: dict[str, str], ids: list[str]):
        """(rows, source): validated answer, from the cached judge output when the task is unchanged."""
        d = ctx.job.judge_dir(task)
        meta = read_yaml(d / "task.yaml", default=None)
        cached = d / OUTPUT_NAME
        if (isinstance(meta, dict) and cached.exists()
                and meta.get("task_hash") == sha256_bytes(compose_prompt(prompt, inputs))):
            try:
                return parse_chunk(cached.read_text(encoding="utf-8"), ids, label=task), "cached"
            except VPError:
                pass  # an invalid old answer: ask again
        raw = ctx.retry(lambda: ctx.judge.run(task, prompt, inputs, OUTPUT_NAME, ctx))
        return parse_chunk(raw, ids, label=task), "judge"

    def run(self, ctx):
        j = ctx.job
        st = s1_settings(ctx)
        src = read_srt(j.source_srt)
        if not src:
            raise VPError("data", "source SRT has no cues")
        cues = [{"id": c.id, "start_ms": c.start_ms, "end_ms": c.end_ms, "speaker": c.speaker,
                 "text": sanitize(c.text)} for c in src]
        if len({c["id"] for c in cues}) != len(cues):
            raise VPError("data", "source SRT has duplicate cue ids")
        pats = compile_patterns(st["echo_patterns"])
        speech_end = _speech_end(ctx, st)
        filter_cues(cues, pats, speech_end)
        sent = [c for c in cues if c["filtered"] is None]
        if not sent:
            raise VPError("data", "every cue was filtered as recognizer garbage: nothing to proofread")
        chunks = chunk(sent, st["chunk_size"])
        for k, ch in enumerate(chunks):
            for c in ch:
                c["chunk"] = k
        for c in cues:
            c.setdefault("chunk", None)

        names = names_dictionary(ctx)
        prompt = load_prompt()
        prev = read_json(j.s1_report, default={}) or {}
        prev_chunks = {c.get("n"): c for c in prev.get("chunks") or [] if isinstance(c, dict)}

        results: list[dict[str, Any]] = []
        pending: JudgmentPending | None = None
        for k, ch in enumerate(chunks):
            ids = [c["id"] for c in ch]
            in_text = format_chunk([(c["id"], c["text"]) for c in ch])
            out_p, in_p = j.s1_out(k), j.s1_in(k)
            machine_sha = (prev_chunks.get(k) or {}).get("machine_sha")
            human = (out_p.exists() and in_p.exists() and machine_sha
                     and in_p.read_text(encoding="utf-8") == in_text
                     and sha256_file(out_p) != machine_sha)
            task = task_name(k)
            if human:
                rows = parse_chunk(out_p.read_text(encoding="utf-8"), ids, label=f"{out_p.name} (human edit)")
                results.append({"n": k, "task": task, "rows": rows, "source": "human",
                                "machine_sha": machine_sha, "in_text": in_text})
                continue
            inputs = {"chunk.txt": in_text}
            if names:
                inputs["names.txt"] = "".join(f"{n}\n" for n in names)
            try:
                rows, source = self._ask(ctx, task, prompt, inputs, ids)
            except JudgmentPending as jp:  # write every task first, then wait for all of them
                pending = pending or jp
                continue
            results.append({"n": k, "task": task, "rows": rows, "source": source,
                            "machine_sha": sha256_bytes(format_chunk(rows)), "in_text": in_text})
        if pending is not None:
            raise pending

        # ---- acceptance (code) -------------------------------------------------------------
        texts: dict[str, str] = {}
        for r in results:
            for i, t in r["rows"]:
                if contains_garbage(t, pats):
                    raise VPError("contract", f"{r['task']}: cue {i} still contains an echo/credit pattern",
                                  cls="echo", chunk=r["task"])
                texts[i] = t
        emptied = [c["id"] for c in sent if not texts[c["id"]]]
        if len(emptied) == len(sent):
            raise VPError("contract", "empty result: every cue was emptied", cls="empty")
        share = len(emptied) / len(sent)
        risks = []
        for c in sent:
            r = meaning_risk(c["text"], texts[c["id"]], names)
            if r:
                risks.append({"id": c["id"], "before": c["text"], "after": texts[c["id"]], **r})

        # ---- write (out_NN of a human edit is left untouched) ------------------------------
        write_json(j.s1_cues, cues)
        for r in results:
            atomic_write(j.s1_in(r["n"]), r["in_text"])
            if r["source"] != "human":
                atomic_write(j.s1_out(r["n"]), format_chunk(r["rows"]))
        for k in range(len(chunks), 1000):  # chunk files of an older, longer split
            stale = [p for p in (j.s1_in(k), j.s1_out(k)) if p.exists()]
            if not stale:
                break
            for p in stale:
                p.unlink()
        filtered: dict[str, list[str]] = {}
        for c in cues:
            if c["filtered"]:
                filtered.setdefault(c["filtered"], []).append(c["id"])
        report = {
            "cues_total": len(cues),
            "filtered": filtered,
            "filtered_total": sum(len(v) for v in filtered.values()),
            "cleaned": [c["id"] for c in cues if c.get("cleaned")],
            "speech_end_ms": speech_end,
            "sent": len(sent),
            "chunks": [{"n": r["n"], "task": r["task"], "cues": len(r["rows"]), "source": r["source"],
                        "machine_sha": r["machine_sha"],
                        "out_sha": sha256_file(j.s1_out(r["n"]))} for r in results],
            "human_edited": [r["n"] for r in results if r["source"] == "human"],
            "emptied": len(emptied),
            "emptied_ids": emptied,
            "emptied_share": round(share, 4),
            "empty_share_max": st["empty_share_max"],
            "echo_after": 0,
            "names": names,
            "meaning_risk": {"count": len(risks), "share": round(len(risks) / len(sent), 4),
                             "cues": risks},
        }
        write_json(j.s1_report, report)
        meta = {"cues": len(cues), "filtered": report["filtered_total"], "chunks": len(chunks),
                "emptied_share": report["emptied_share"], "meaning_risk": len(risks),
                "human_edited": len(report["human_edited"]),
                "judge_calls": sum(1 for r in results if r["source"] == "judge")}
        ctx.log.event("s1_summary", **meta)
        if share > st["empty_share_max"]:
            raise Inconclusive(f"S1: {len(emptied)} of {len(sent)} cues emptied ({share:.1%} > "
                               f"{st['empty_share_max']:.0%}); see report", report=j.rel(j.s1_report))
        return meta


STEP = S1Proofread()
