"""S3a — fragment proposals (SPEC S3a): two independent judge passes `s3a.reel`, `s3a.long`.

Judgment (model): reads subtitles WITHOUT times (lines `#<number> <text>`), returns per fragment
verbatim start/end quotes (8–30 words), title, thesis, why, applied rule/case ids, score 1–5, in
the strict YAML format of `vp/prompts/s3a_select.md` (validated by `parse_output`; malformed ->
VPError("contract")). Zero fragments is valid.

The director's task (request.yaml `brief`, optional free text) goes into the prompt as its own
section and into the step's state: adding, changing or removing it makes S3a stale, the same way
a change of the lengths in `overrides` does. A job without a brief has the prompt and the state
it had before the brief existed.

Bookkeeping (code): quote matching, boundaries per mode (vp.steps._fragments), discards with a
recorded reason (quote_length, quote_not_found, end_before_start, out_of_range,
duplicate_overlap, over_cap), the proposal cap = ceil(expected × selection_margin), ids r01../l01..
in time order, `fragments.proposed.yaml` with times in ms, text and the knowledge version.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ..core.config import request_brief
from ..core.io import now_iso, sha256_file, write_yaml
from ..core.srt import read_srt, srt_duration_ms
from ..knowledge.prompt_builder import brief_template_hash, build_prompt, template_hash
from ..knowledge.store import KnowledgeStore
from . import _fragments as F
from .base import Ctx, Skip, StepSpec, VPError

OUTPUT_KEYS = ("start_quote", "end_quote", "title", "thesis", "why", "applied", "score")
REQUIRED_KEYS = ("start_quote", "end_quote", "title", "thesis", "why", "score")
QUOTE_MIN_WORDS, QUOTE_MAX_WORDS = 8, 30
TEXT_MAX = {"title": 200, "thesis": 600, "why": 1000}
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.S)


def transcript_text(cues) -> str:
    """Model input: one subtitle per line, `#<number> <text>`; no times. Empty subtitles skipped."""
    lines = []
    for c in cues:
        t = " ".join(c.text.split())
        if t:
            lines.append(f"#{c.id} {t}")
    return "\n".join(lines) + "\n"


def parse_output(text: str) -> list[dict[str, Any]]:
    """Validate the model's YAML. Returns the fragment list (may be empty).

    Contract errors: not YAML; not {fragments: [...]}; a fragment not a mapping; unknown or missing
    keys; non-string text fields; empty quotes; score not an integer 1..5; applied not a list of
    strings. Quote LENGTH is checked later (a violation discards the fragment, not the run)."""
    raw = (text or "").strip()
    m = _FENCE_RE.match(raw)
    if m:
        raw = m.group(1)
    try:
        data = yaml.safe_load(raw) if raw else None
    except yaml.YAMLError as e:
        raise VPError("contract", f"s3a output is not YAML: {str(e)[:200]}") from e
    if not isinstance(data, dict) or set(data) != {"fragments"}:
        raise VPError("contract", "s3a output must be a mapping with the single key `fragments`")
    frags = data["fragments"]
    if frags is None:
        frags = []
    if not isinstance(frags, list):
        raise VPError("contract", "`fragments` must be a list")
    out = []
    for i, f in enumerate(frags, 1):
        if not isinstance(f, dict):
            raise VPError("contract", f"fragment {i} is not a mapping")
        extra = set(f) - set(OUTPUT_KEYS)
        if extra:
            raise VPError("contract", f"fragment {i}: unknown keys {sorted(extra)}")
        missing = [k for k in REQUIRED_KEYS if k not in f]
        if missing:
            raise VPError("contract", f"fragment {i}: missing keys {missing}")
        for k in ("start_quote", "end_quote", "title", "thesis", "why"):
            if not isinstance(f[k], str) or not f[k].strip():
                raise VPError("contract", f"fragment {i}: `{k}` must be a non-empty string")
            if len(f[k]) > TEXT_MAX.get(k, 2000):
                raise VPError("contract", f"fragment {i}: `{k}` too long")
        score = f["score"]
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise VPError("contract", f"fragment {i}: score must be an integer 1..5")
        applied = f.get("applied") or []
        if not isinstance(applied, list) or not all(isinstance(a, (str, int)) for a in applied):
            raise VPError("contract", f"fragment {i}: `applied` must be a list of ids")
        out.append({"start_quote": f["start_quote"].strip(), "end_quote": f["end_quote"].strip(),
                    "title": f["title"].strip(), "thesis": f["thesis"].strip(),
                    "why": f["why"].strip(), "applied": [str(a) for a in applied], "score": score})
    return out


def place_fragments(kind: str, items: list[dict[str, Any]], timeline: F.Timeline,
                    rng: tuple[int, int], cap: int, known_refs: set[str]) -> tuple[list[dict], list[dict]]:
    """Code bookkeeping for one type: match, range, dedup, cap. Returns (kept, discarded);
    kept is in time order without ids."""
    discarded: list[dict[str, Any]] = []
    placed: list[dict[str, Any]] = []

    def drop(item: dict[str, Any], reason: str, detail: str) -> None:
        discarded.append({"type": kind, "title": item["title"], "start_quote": item["start_quote"],
                          "end_quote": item["end_quote"], "score": item["score"],
                          "reason": reason, "detail": detail})

    for order, it in enumerate(items):
        bad_len = [k for k in ("start_quote", "end_quote")
                   if not QUOTE_MIN_WORDS <= F.word_count(it[k]) <= QUOTE_MAX_WORDS]
        if bad_len:
            drop(it, "quote_length", f"{', '.join(bad_len)} must have {QUOTE_MIN_WORDS}-{QUOTE_MAX_WORDS} words")
            continue
        try:
            fr = timeline.fragment(it["start_quote"], it["end_quote"], quotes=True)
        except F.BoundaryError as e:
            drop(it, e.reason, e.msg)
            continue
        if not F.in_range(fr["duration_ms"], rng):
            drop(it, "out_of_range", f"duration {fr['duration_ms'] / 1000:.1f}s not in "
                                     f"{rng[0] / 1000:g}-{rng[1] / 1000:g}s")
            continue
        applied = [a for a in it["applied"] if a in known_refs]
        unknown = [a for a in it["applied"] if a not in known_refs]
        placed.append({**fr, "type": kind, "title": it["title"], "thesis": it["thesis"],
                       "why": it["why"], "score": it["score"], "applied": applied,
                       "applied_unknown": unknown, "start_quote": it["start_quote"],
                       "end_quote": it["end_quote"], "_order": order})
    # dedup: best score first (model order breaks ties), drop > 50 % overlap with a kept one
    kept: list[dict[str, Any]] = []
    for fr in sorted(placed, key=lambda x: (-x["score"], x["_order"])):
        clash = next((k for k in kept if F.overlap_share((fr["start_ms"], fr["end_ms"]),
                                                          (k["start_ms"], k["end_ms"])) > F.OVERLAP_MAX), None)
        if clash is not None:
            drop(fr, "duplicate_overlap", f"overlaps > {int(F.OVERLAP_MAX * 100)}% with «{clash['title']}»")
            continue
        if len(kept) >= cap:
            drop(fr, "over_cap", f"more than {cap} proposals")
            continue
        kept.append(fr)
    kept.sort(key=lambda x: x["start_ms"])
    for k in kept:
        k.pop("_order", None)
        if not k["applied_unknown"]:
            k.pop("applied_unknown")
    return kept, discarded


def selection_settings(ctx: Ctx) -> dict[str, Any]:
    """Config `selection` (prompt builder settings) — optional."""
    return dict(ctx.config.get("selection") or {})


def job_brief(ctx: Ctx) -> str | None:
    """The director's task for this recording: request.yaml `brief`, stripped; None when absent
    or empty. Not a string or too long -> VPError(data) (S0 checks the same at intake)."""
    return request_brief(ctx.job.request())


class S3aSelect(StepSpec):
    id, deps, scope, version, gate = "S3a", ("S2",), "job", "1", None

    def _store(self, ctx: Ctx) -> KnowledgeStore:
        return KnowledgeStore(ctx.home)

    def inputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.subtitles_srt, ctx.job.source_words, ctx.job.job_yaml]

    def config(self, ctx: Ctx) -> dict:
        if ctx.config.get("whole_recording"):
            return {"whole_recording": ctx.config["whole_recording"]}
        store = self._store(ctx)
        v = store.active_version()
        p = ctx.profile
        state = {"knowledge_version": v, "knowledge_hash": store.manifest(v)["content_hash"],
                 "template_hash": template_hash(), "reel": p.get("reel"), "long": p.get("long"),
                 "selection_margin": p.get("selection_margin", 1.5),
                 "selection_expected_per_hour": p.get("selection_expected_per_hour"),
                 "selection": selection_settings(ctx)}
        brief = job_brief(ctx)
        if brief is not None:
            # The key exists only for a briefed job: without a brief the state is exactly what it
            # was before the brief existed, so no such job turns stale because of this feature.
            state["brief"] = {"text": brief, "template_hash": brief_template_hash()}
        return state

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.fragments_proposed]

    def run(self, ctx: Ctx) -> dict:
        if ctx.config.get("whole_recording"):
            raise Skip("the whole recording was requested")
        store = self._store(ctx)
        version = store.active_version()
        kv = store.load_version(version)
        timeline = F.load_timeline(ctx.job)
        transcript = transcript_text(timeline.cues)
        rec_ms = srt_duration_ms(timeline.cues)
        brief = job_brief(ctx)
        result: dict[str, Any] = {"fragments": [], "discarded": [], "caps": {}, "prompts": {}}
        for kind in F.TYPES:
            rng = F.duration_range(ctx.profile, kind)
            cap = F.proposal_cap(kind, rec_ms, ctx.profile)
            built = build_prompt(store, version, kind, transcript, cap=cap, min_s=rng[0] / 1000,
                                 max_s=rng[1] / 1000, settings=selection_settings(ctx), brief=brief)
            raw = ctx.judge.run(f"s3a.{kind}", built.text, {"subtitles.txt": transcript},
                                "output.yaml", ctx)
            items = parse_output(raw)
            kept, dropped = place_fragments(kind, items, timeline, rng, cap, kv.ref_ids())
            for n, fr in enumerate(kept, 1):
                fr_out = {"id": f"{F.TYPE_PREFIX[kind]}{n:02d}", **fr,
                          "start": F.clock(fr["start_ms"]), "end": F.clock(fr["end_ms"])}
                result["fragments"].append(fr_out)
            result["discarded"].extend(dropped)
            result["caps"][kind] = cap
            result["prompts"][kind] = {**built.meta(), "returned": len(items), "kept": len(kept)}
        doc = {
            "job_id": ctx.job.job_id, "created_at": now_iso(), "knowledge_version": version,
            "knowledge_hash": kv.content_hash, "boundary_mode": timeline.mode,
            "subtitles_sha256": sha256_file(ctx.job.subtitles_srt),
            "caps": result["caps"], "prompts": result["prompts"],
            "fragments": result["fragments"], "discarded": result["discarded"],
        }
        write_yaml(ctx.job.fragments_proposed, doc)
        counts = {k: sum(1 for f in result["fragments"] if f["type"] == k) for k in F.TYPES}
        ctx.log.event("s3a_proposed", reel=counts["reel"], long=counts["long"],
                      discarded=len(result["discarded"]), knowledge_version=version)
        return {"knowledge_version": version, "boundary_mode": timeline.mode, "proposed": counts,
                "discarded": len(result["discarded"])}


STEP = S3aSelect()
