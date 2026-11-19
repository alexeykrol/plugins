"""S6a — design texts (SPEC S6a): intro card text, series announce, thumbnail text (≤ 5 words).

Judgment: task `s6a.<item>`; the model gets the fragment's kind/title/thesis and the TEXT of
clip.srt (no timecodes, no ids) plus the author's fixed fields. Code validates the output and
applies the author fields (locked / extend / suggest, see _author_fields). Output:

    design_texts.yaml:
      item, kind
      intro_text, announce_text, thumbnail_text   final values
      sources: {field: model | author_locked | author_extend | author_suggest}
      suggestions: {field: model alternative}      (suggest fields only)
      notes: [...]                                  e.g. an extend continuation dropped for length

Also exports `load_prompt(name)`, `prompt_hash(name)`, `parse_json_object(text)` and
`clip_text(cues)` for S6b / S7.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..core.io import sha256_bytes, write_yaml
from ..core.srt import read_srt
from ._author_fields import AuthorField, apply_field, author_fields, fields_as_config
from ._limits import has_forbidden
from .base import Ctx, StepSpec, VPError
from .s4_cut import fragment_record

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
PROMPT = "s6a_design_texts.md"

DESIGN_FIELDS = ("intro_text", "announce_text", "thumbnail_text")
MAX_CHARS = {"intro_text": 80, "announce_text": 140, "thumbnail_text": 40}
THUMB_MAX_WORDS = 5

_FENCE_RE = re.compile(r"^\s*```(?:json|yaml)?\s*\n(.*?)\n\s*```\s*$", re.S)


def load_prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def prompt_hash(name: str) -> str:
    return sha256_bytes(load_prompt(name))[:16]


def parse_json_object(text: str, task: str) -> dict[str, Any]:
    """Model output -> dict. A single surrounding code fence is tolerated; anything else that is
    not a JSON object raises VPError(contract)."""
    raw = text.strip()
    m = _FENCE_RE.match(raw)
    if m:
        raw = m.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise VPError("contract", f"{task}: output is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise VPError("contract", f"{task}: output must be a JSON object")
    return data


def clip_text(cues) -> str:
    """Subtitle texts one per line — what the model sees instead of an SRT (no times, no ids)."""
    return "\n".join(" ".join(c.text.split()) for c in cues if c.text.strip())


def fragment_input(rec: dict[str, Any]) -> str:
    return json.dumps({"type": rec["kind"], "title": rec.get("title", ""), "thesis": rec.get("thesis", "")},
                      ensure_ascii=False, indent=1)


def author_input(fields: dict[str, AuthorField], names: tuple[str, ...]) -> str:
    """Author instructions for the model: only the fields of this step."""
    out = {n: {"status": f.status, "author_text": f.value} for n, f in fields.items() if n in names}
    return json.dumps(out, ensure_ascii=False, indent=1) if out else "{}"


def text_violation(name: str, value: Any) -> str | None:
    """Why a final design text is invalid, or None."""
    if not isinstance(value, str) or not value.strip():
        return "empty"
    if "\n" in value.strip():
        return "newline"
    if has_forbidden(value):
        return "forbidden_chars"
    if len(value) > MAX_CHARS[name]:
        return f"longer_than_{MAX_CHARS[name]}"
    if name == "thumbnail_text" and len(value.split()) > THUMB_MAX_WORDS:
        return f"more_than_{THUMB_MAX_WORDS}_words"
    return None


def build_design_texts(model: dict[str, Any], fields: dict[str, AuthorField], task: str
                       ) -> dict[str, Any]:
    """Validate model output and apply author fields. Raises VPError(contract) for an invalid
    model value, VPError(data) for an author value that itself breaks a limit."""
    final: dict[str, Any] = {}
    sources: dict[str, str] = {}
    suggestions: dict[str, Any] = {}
    notes: list[str] = []
    for name in DESIGN_FIELDS:
        af = fields.get(name)
        raw = model.get(name)
        if raw is not None and not isinstance(raw, str):
            raise VPError("contract", f"{task}: {name} must be a string")
        raw = " ".join(raw.split()) if isinstance(raw, str) else ""
        if af is None:
            bad = text_violation(name, raw)
            if bad:
                raise VPError("contract", f"{task}: {name} invalid ({bad})")
            final[name], sources[name] = raw, "model"
            continue
        bad_author = text_violation(name, af.value)
        if bad_author:
            raise VPError("data", f"author {name} breaks the limit ({bad_author})")
        value, suggestion = apply_field(name, af, raw)
        if af.status == "extend" and text_violation(name, value):
            notes.append(f"{name}: continuation dropped ({text_violation(name, value)})")
            value = af.value
        if suggestion is not None and text_violation(name, suggestion):
            notes.append(f"{name}: suggestion dropped ({text_violation(name, suggestion)})")
            suggestion = None
        final[name], sources[name] = value, f"author_{af.status}"
        if suggestion is not None:
            suggestions[name] = suggestion
    out = dict(final)
    out["sources"] = sources
    if suggestions:
        out["suggestions"] = suggestions
    if notes:
        out["notes"] = notes
    return out


class S6aDesignTexts(StepSpec):
    id, deps, scope, version, gate = "S6a", ("S4",), "item", "1", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.clip_srt(ctx.item)]

    def config(self, ctx: Ctx) -> dict:
        rec = fragment_record(ctx)
        fields = author_fields(ctx.job.request(), ctx.item)
        return {"fragment": {k: rec[k] for k in ("id", "kind", "title", "thesis")},
                "author": fields_as_config({n: f for n, f in fields.items() if n in DESIGN_FIELDS}),
                "prompt": prompt_hash(PROMPT)}

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.design_texts(ctx.item)]

    def run(self, ctx: Ctx) -> dict:
        item = ctx.item
        rec = fragment_record(ctx)
        fields = author_fields(ctx.job.request(), item)
        cues = read_srt(ctx.job.clip_srt(item))
        task = f"s6a.{item}"
        inputs = {"fragment": fragment_input(rec), "text": clip_text(cues),
                  "author": author_input(fields, DESIGN_FIELDS)}
        raw = ctx.judge.run(task, load_prompt(PROMPT), inputs, "output.json", ctx)
        texts = build_design_texts(parse_json_object(raw, task), fields, task)
        doc = {"item": item, "kind": rec["kind"], **texts}
        write_yaml(ctx.job.design_texts(item), doc)
        return {"kind": rec["kind"], "sources": texts["sources"]}


STEP = S6aDesignTexts()
