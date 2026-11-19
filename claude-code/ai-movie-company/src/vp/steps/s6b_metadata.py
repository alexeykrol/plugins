"""S6b — final metadata (SPEC S6b), after S5 so chapters live on the final timeline.

Judgment: task `s6b.<item>` -> 3 title variants, description, tags, hashtags (Russian); for
long/full also chapters named by START QUOTES. The model sees the subtitle TEXT only.

Code (bookkeeping):
  * chapter quotes -> times in final.srt (already shifted by render.manifest intro_ms):
    first 00:00, ascending, ≥ 10 s apart and ≥ 10 s before the end, ≥ 3 — else no chapters
    (reason recorded);
  * limits (_limits): title ≤ 100, description ≤ 5000 − reserve for the S10 links block,
    tags ≤ 500 − reserve for the vp-<key> tag (S8); tags are trimmed from the end (the model
    orders them by importance); no < or >;
  * author fields (_author_fields): locked byte-exact, extend = author prefix + continuation,
    suggest = author value + suggestion. Chapters and the hashtag line are appended to the
    description only when the description is free or `extend`; a locked/suggest description
    stays byte-exact and the chapters are kept in meta.yaml only (reason recorded);
  * chosen title = first valid variant, unless the author locked/suggested the title.

Output meta.yaml: item, kind, language, category_id, title, title_variants, description,
tags, hashtags, chapters [{start_ms, clock, title}], chapters_dropped, chapters_reason,
sources, suggestions, limits.

Config keys: s6b: {description_reserve: 600, tags_reserve: 20, min_chapters: 3,
chapter_min_gap_s: 10, category_id: "27", language: "ru"}.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.io import read_yaml, write_yaml
from ..core.srt import read_srt
from ..core.timefmt import fmt_clock
from ._author_fields import AuthorField, apply_field, author_fields, check_author_fields, fields_as_config
from ._chapters import chapter_violations, compute_chapters, format_chapters
from ._limits import (DESCRIPTION_MAX, HASHTAGS_MAX, TAGS_TOTAL_MAX, clean_tags, description_violations,
                      fit_tags, metadata_violations, normalize_hashtag, tags_length, title_violations)
from .base import Ctx, StepSpec, VPError
from .s4_cut import fragment_record
from .s6a_design_texts import author_input, clip_text, fragment_input, load_prompt, parse_json_object, prompt_hash

PROMPT = "s6b_metadata.md"
META_FIELDS = ("title", "description", "tags", "hashtags")
DEFAULTS: dict[str, Any] = {"description_reserve": 600, "tags_reserve": 20, "min_chapters": 3,
                            "chapter_min_gap_s": 10, "category_id": "27", "language": "ru",
                            "max_hashtags": 3}


def s6b_settings(config: dict[str, Any]) -> dict[str, Any]:
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (config.get("s6b") or {}).items() if k in DEFAULTS})
    return s


def budgets(settings: dict[str, Any]) -> tuple[int, int]:
    """(description budget, tags budget) for S6b output."""
    return (DESCRIPTION_MAX - int(settings["description_reserve"]),
            TAGS_TOTAL_MAX - int(settings["tags_reserve"]))


def _str_list(value: Any, task: str, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise VPError("contract", f"{task}: {name} must be a list of strings")
    return value


def build_titles(model: dict[str, Any], af: AuthorField | None, task: str
                 ) -> tuple[str, list[dict[str, Any]], str, Any]:
    """-> (chosen title, variants [{title, valid, violations}], source, suggestion)."""
    raw = model.get("titles")
    if af is not None and af.status == "locked":
        variants_raw: list[str] = []
    else:
        variants_raw = _str_list(raw, task, "titles")
        if af is None and len(variants_raw) != 3:
            raise VPError("contract", f"{task}: titles must have exactly 3 variants, got {len(variants_raw)}")
    variants = []
    suggestion = None
    for v in variants_raw:
        v = " ".join(v.split())
        final, sug = apply_field("title", af, v)
        if af is not None and af.status == "suggest":
            final = sug if sug is not None else af.value
            suggestion = suggestion if suggestion is not None else (sug if not title_violations(sug or "") else None)
        bad = title_violations(final)
        variants.append({"title": final, "valid": not bad, "violations": bad})
    if af is None:
        chosen = next((v["title"] for v in variants if v["valid"]), None)
        if chosen is None:
            raise VPError("contract", f"{task}: no valid title among the 3 variants")
        return chosen, variants, "model", None
    if title_violations(af.value):
        raise VPError("data", f"author title breaks the limits: {title_violations(af.value)}")
    if af.status == "extend":
        chosen = next((v["title"] for v in variants if v["valid"]), af.value)
        return chosen, variants, "author_extend", None
    return af.value, variants, f"author_{af.status}", suggestion


def build_meta(model: dict[str, Any], rec: dict[str, Any], fields: dict[str, AuthorField],
               final_cues, duration_ms: int, settings: dict[str, Any], task: str) -> dict[str, Any]:
    """Validate the model output and compute meta.yaml (pure; no I/O)."""
    desc_budget, tags_budget = budgets(settings)
    sources: dict[str, str] = {}
    suggestions: dict[str, Any] = {}

    title, variants, sources["title"], sug = build_titles(model, fields.get("title"), task)
    if sug is not None:
        suggestions["title"] = sug

    # description body (author rules apply to this text)
    daf = fields.get("description")
    raw_desc = model.get("description")
    if raw_desc is not None and not isinstance(raw_desc, str):
        raise VPError("contract", f"{task}: description must be a string")
    raw_desc = (raw_desc or "").strip()
    if daf is None:
        if not raw_desc:
            raise VPError("contract", f"{task}: description is empty")
        bad = description_violations(raw_desc, desc_budget)
        if bad:
            raise VPError("contract", f"{task}: description invalid {bad}")
    elif description_violations(daf.value, desc_budget):
        raise VPError("data", f"author description breaks the limits: {description_violations(daf.value, desc_budget)}")
    body, sug = apply_field("description", daf, raw_desc)
    if daf is not None and daf.status == "extend" and description_violations(body, desc_budget):
        body = daf.value
    if sug is not None and not description_violations(sug, desc_budget):
        suggestions["description"] = sug
    sources["description"] = "model" if daf is None else f"author_{daf.status}"

    # tags
    taf = fields.get("tags")
    model_tags = clean_tags(_str_list(model.get("tags"), task, "tags"))
    if taf is not None and tags_length(clean_tags(taf.value)) > tags_budget:
        raise VPError("data", "author tags exceed the tag length budget")
    if taf is not None and taf.status == "extend":
        tags, _ = apply_field("tags", taf, model_tags)
        tags = fit_tags(clean_tags(tags), tags_budget)
        if tags[:len(taf.value)] != taf.value:
            tags = list(taf.value)
    else:
        tags, sug = apply_field("tags", taf, fit_tags(model_tags, tags_budget))
        if sug:
            suggestions["tags"] = sug
    if taf is None and not tags:
        raise VPError("contract", f"{task}: no usable tags")
    sources["tags"] = "model" if taf is None else f"author_{taf.status}"

    # hashtags
    haf = fields.get("hashtags")
    model_h = [h for h in (normalize_hashtag(x) for x in _str_list(model.get("hashtags"), task, "hashtags")) if h]
    model_h = list(dict.fromkeys(model_h))[:int(settings["max_hashtags"])]
    hashtags, sug = apply_field("hashtags", haf, model_h)
    hashtags = list(hashtags)[:HASHTAGS_MAX] if haf is None or haf.status != "locked" else list(hashtags)
    if sug:
        suggestions["hashtags"] = sug
    sources["hashtags"] = "model" if haf is None else f"author_{haf.status}"

    # chapters (long/full only)
    chapters: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    reason = None
    if rec["kind"] in ("long", "full"):
        raw_ch = model.get("chapters")
        if not isinstance(raw_ch, list) or not all(isinstance(c, dict) for c in raw_ch):
            raise VPError("contract", f"{task}: chapters must be a list of objects")
        chapters, dropped, reason = compute_chapters(
            final_cues, raw_ch, duration_ms, min_gap_ms=int(settings["chapter_min_gap_s"]) * 1000,
            min_count=int(settings["min_chapters"]))
        for c in chapters:
            c["clock"] = fmt_clock(c["start_ms"])

    # final description: body [+ chapters] [+ hashtag line] — only when the body is not fixed
    description = body
    appendable = daf is None or daf.status == "extend"
    chapters_in_description = False
    if not appendable:
        if chapters:
            reason = reason or f"description_{daf.status}"
    else:
        chapters_in_description = bool(chapters)
        extra = []
        if chapters:
            extra.append(format_chapters(chapters))
        if hashtags:
            extra.append(" ".join(hashtags))
        candidate = "\n\n".join([description] + extra)
        if len(candidate) > desc_budget and chapters:
            dropped.extend({"title": c["title"], "reason": "description_limit"} for c in chapters)
            chapters, reason, chapters_in_description = [], "description_limit", False
            candidate = "\n\n".join([description] + ([" ".join(hashtags)] if hashtags else []))
        if len(candidate) > desc_budget:
            candidate = description
        description = candidate

    meta = {
        "item": rec["id"], "kind": rec["kind"],
        "language": settings["language"], "category_id": str(settings["category_id"]),
        "title": title, "title_variants": variants,
        "description": description, "tags": list(tags), "hashtags": list(hashtags),
        "chapters": chapters, "chapters_in_description": chapters_in_description,
        "chapters_dropped": dropped, "chapters_reason": reason,
        "sources": sources, "limits": {"description_max": desc_budget, "tags_max": tags_budget},
    }
    if suggestions:
        meta["suggestions"] = suggestions
    return meta


class S6bMetadata(StepSpec):
    id, deps, scope, version, gate = "S6b", ("S5v",), "item", "1", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        j, i = ctx.job, ctx.item
        return [j.final_srt(i), j.render_manifest(i)]

    def config(self, ctx: Ctx) -> dict:
        rec = fragment_record(ctx)
        fields = author_fields(ctx.job.request(), ctx.item)
        return {"fragment": {k: rec[k] for k in ("id", "kind", "title", "thesis")},
                "author": fields_as_config({n: f for n, f in fields.items() if n in META_FIELDS}),
                "s6b": s6b_settings(ctx.config), "prompt": prompt_hash(PROMPT)}

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.meta_yaml(ctx.item)]

    def run(self, ctx: Ctx) -> dict:
        j, item = ctx.job, ctx.item
        rec = fragment_record(ctx)
        request = j.request()
        fields = author_fields(request, item)
        settings = s6b_settings(ctx.config)
        manifest = read_yaml(j.render_manifest(item), default=None)
        if not isinstance(manifest, dict) or "duration_ms" not in manifest:
            raise VPError("data", f"{item}: render.manifest.yaml missing or invalid")
        cues = read_srt(j.final_srt(item))
        desc_budget, tags_budget = budgets(settings)
        task = f"s6b.{item}"
        inputs = {
            "fragment": fragment_input(rec),
            "text": clip_text(cues),
            "author": author_input(fields, META_FIELDS),
            "limits": json.dumps({"title_max_chars": 100, "description_max_chars": desc_budget - 800,
                                  "tags_max_total_chars": tags_budget,
                                  "chapters": rec["kind"] in ("long", "full")}, ensure_ascii=False),
        }
        raw = ctx.judge.run(task, load_prompt(PROMPT), inputs, "output.json", ctx)
        meta = build_meta(parse_json_object(raw, task), rec, fields, cues, int(manifest["duration_ms"]),
                          settings, task)

        # ---- acceptance -------------------------------------------------------------------
        bad = metadata_violations(meta, desc_budget, tags_budget)
        bad += chapter_violations(meta["chapters"], int(manifest["duration_ms"]),
                                  int(settings["chapter_min_gap_s"]) * 1000, int(settings["min_chapters"]))
        bad += [v["code"] + ":" + v["field"] for v in
                check_author_fields({k: meta[k] for k in META_FIELDS}, request, item)]
        if bad:
            raise VPError("contract", f"{item}: meta.yaml fails acceptance: {bad}")
        write_yaml(j.meta_yaml(item), meta)
        return {"kind": rec["kind"], "chapters": len(meta["chapters"]),
                "chapters_reason": meta["chapters_reason"], "title_source": meta["sources"]["title"]}


STEP = S6bMetadata()
