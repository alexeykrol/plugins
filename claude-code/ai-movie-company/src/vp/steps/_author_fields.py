"""Author fields (SPEC §5 «Авторские поля»): what the author fixed in request.yaml, and the
code-side enforcement used by S6a, S6b (applying) and S9 (checking).

request.yaml format:

    author:
      title:       {value: "Серия про агентов", status: suggest}   # series level: every item
      tags:        {value: ["ИИ", "агенты"], status: extend}
      items:
        r03:                                                       # item level wins over series
          title:          {value: "Почему демо ломаются", status: locked}
          thumbnail_text: {value: "Демо ломается", status: locked}
        full:
          description:    {value: "Полная запись лекции.", status: extend}

A field may be given as a bare value (`title: "…"`) — status then defaults to `locked`
(the conservative reading: the author wrote it, nobody changes it). `text:` is accepted as an
alias of `value:`. A field the author did not set is free (mass mode).

Statuses:
  locked   the final value equals the author value byte for byte; the model is not asked
  extend   the final value starts with the author value unchanged; the model writes only the
           CONTINUATION, code joins (author + separator + continuation) — so changing the prefix
           is structurally impossible
  suggest  the final value is the author value; the model's alternative is kept as a
           suggestion (shown to the owner at approval)

Known fields: text — title, description, intro_text, announce_text, thumbnail_text;
lists — tags, hashtags.

Public API:
  author_fields(request, item) -> dict[name, AuthorField]       resolve series + item level
  apply_field(name, field, model_value) -> (final, suggestion)  S6a / S6b
  check_author_fields(values, request, item, ignore_link_block=True) -> list[dict]   S9

`check_author_fields` returns violations [{"field", "status", "code"}], codes:
  author_locked_changed   locked field differs from the author value
  author_extend_changed   extend field does not start with the author value (lists: prefix)
  author_suggest_changed  suggest field differs from the author value (owner did not approve
                          a replacement)
  author_field_missing    the author set the field but `values` has no such key
With ignore_link_block (default) the S10 block ⟦vp-links⟧…⟦/vp-links⟧ and the whitespace before
it are removed from text values before comparing: links are added by the pipeline after S6b and
are the only allowed addition to a locked description.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..core.errors import VPError

STATUSES = ("locked", "extend", "suggest")
TEXT_FIELDS: dict[str, str] = {          # field -> separator used by `extend`
    "title": " ",
    "description": "\n\n",
    "intro_text": " ",
    "announce_text": " ",
    "thumbnail_text": " ",
}
LIST_FIELDS = ("tags", "hashtags")
FIELDS = tuple(TEXT_FIELDS) + LIST_FIELDS

LINK_BLOCK_RE = re.compile(r"\s*⟦vp-links⟧.*?⟦/vp-links⟧", re.S)


@dataclass(frozen=True)
class AuthorField:
    name: str
    value: Any            # str for text fields, list[str] for list fields
    status: str           # locked | extend | suggest
    level: str            # series | item


def _parse_field(name: str, raw: Any, level: str) -> AuthorField:
    if isinstance(raw, dict):
        if "value" in raw:
            value = raw["value"]
        elif "text" in raw:
            value = raw["text"]
        else:
            raise VPError("data", f"author.{name}: no `value`")
        status = raw.get("status", "locked")
    else:
        value, status = raw, "locked"
    if status not in STATUSES:
        raise VPError("data", f"author.{name}: unknown status {status!r}; expected {STATUSES}")
    if name in TEXT_FIELDS:
        if not isinstance(value, str) or not value.strip():
            raise VPError("data", f"author.{name}: value must be a non-empty string")
    else:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise VPError("data", f"author.{name}: value must be a list of non-empty strings")
        value = list(value)
    return AuthorField(name, value, status, level)


def author_fields(request: dict[str, Any] | None, item: str | None) -> dict[str, AuthorField]:
    """Resolve the author's fields for one item: item level overrides series level per field.

    Unknown field names raise VPError(data) (a typo must not silently free a field).
    """
    author = (request or {}).get("author") or {}
    if not isinstance(author, dict):
        raise VPError("data", "request.yaml author: must be a mapping")
    out: dict[str, AuthorField] = {}
    for name, raw in author.items():
        if name == "items":
            continue
        if name not in FIELDS:
            raise VPError("data", f"request.yaml author: unknown field {name!r}; known: {FIELDS}")
        out[name] = _parse_field(name, raw, "series")
    items = author.get("items") or {}
    if not isinstance(items, dict):
        raise VPError("data", "request.yaml author.items: must be a mapping")
    own = items.get(item) if item is not None else None
    if own:
        if not isinstance(own, dict):
            raise VPError("data", f"request.yaml author.items.{item}: must be a mapping")
        for name, raw in own.items():
            if name not in FIELDS:
                raise VPError("data", f"request.yaml author.items.{item}: unknown field {name!r}")
            out[name] = _parse_field(name, raw, "item")
    return out


def fields_as_config(fields: dict[str, AuthorField]) -> dict[str, Any]:
    """JSON-serialisable form for step manifests."""
    return {n: {"value": f.value, "status": f.status, "level": f.level} for n, f in sorted(fields.items())}


def apply_field(name: str, field: AuthorField | None, model_value: Any) -> tuple[Any, Any]:
    """Final value of a field given the author's setting and the model's output.

    Returns (final, suggestion): suggestion is the model's alternative for `suggest`, else None.
    For `extend`, `model_value` is the continuation (text) or the extra items (list); an empty
    continuation leaves the author value alone. Duplicates of author list items are dropped.
    """
    if field is None:
        return model_value, None
    if field.status == "locked":
        return field.value, None
    if field.status == "suggest":
        suggestion = model_value if model_value not in (None, "", []) and model_value != field.value else None
        return field.value, suggestion
    # extend
    if name in TEXT_FIELDS:
        cont = (model_value or "").strip() if isinstance(model_value, str) else ""
        return (field.value + TEXT_FIELDS[name] + cont) if cont else field.value, None
    extra = [v for v in (model_value or []) if isinstance(v, str)]
    seen = {v.casefold() for v in field.value}
    out = list(field.value)
    for v in extra:
        if v.casefold() not in seen:
            seen.add(v.casefold())
            out.append(v)
    return out, None


def strip_link_block(text: str) -> str:
    """Remove the S10 links block (and the whitespace before it)."""
    return LINK_BLOCK_RE.sub("", text)


def field_violation(name: str, field: AuthorField, value: Any,
                    ignore_link_block: bool = True) -> str | None:
    """Violation code for one field, or None."""
    if isinstance(value, str) and ignore_link_block:
        value = strip_link_block(value)
    if field.status == "locked":
        return None if value == field.value else "author_locked_changed"
    if field.status == "suggest":
        return None if value == field.value else "author_suggest_changed"
    if name in TEXT_FIELDS:
        ok = isinstance(value, str) and value.startswith(field.value)
    else:
        ok = isinstance(value, list) and value[:len(field.value)] == field.value
    return None if ok else "author_extend_changed"


def check_author_fields(values: dict[str, Any], request: dict[str, Any] | None, item: str | None,
                        ignore_link_block: bool = True,
                        approved: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Check final values against the author's fields (S6 and S9 acceptance).

    values: {field: final value} — e.g. meta.yaml {title, description, tags, hashtags} and/or
    design_texts.yaml {intro_text, announce_text, thumbnail_text}. Only fields present in either
    `values` or the author's spec are checked; author fields absent from `values` are reported
    only if they belong to the same group (S6a design texts vs S6b metadata) as some key in
    `values`, so S9 can check meta.yaml and design_texts.yaml separately.
    approved: {field: value} replacements the owner approved for `suggest` fields (optional);
    an approved replacement is then accepted.
    Returns [{"field", "status", "code"}] (empty = ok).
    """
    fields = author_fields(request, item)
    groups = ({"title", "description", "tags", "hashtags"},
              {"intro_text", "announce_text", "thumbnail_text"})
    present_groups = [g for g in groups if g & set(values)]
    out = []
    for name, field in sorted(fields.items()):
        if name not in values:
            if any(name in g for g in present_groups):
                out.append({"field": name, "status": field.status, "code": "author_field_missing"})
            continue
        value = values[name]
        if field.status == "suggest" and approved and name in approved and value == approved[name]:
            continue
        code = field_violation(name, field, value, ignore_link_block)
        if code:
            out.append({"field": name, "status": field.status, "code": code})
    return out
