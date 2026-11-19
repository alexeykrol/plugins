"""Assemble the S3a selection prompt from a knowledge version (SPEC S3a).

    build_prompt(store, version, kind, transcript, cap=, min_s=, max_s=, brief=None) -> BuiltPrompt

Prompt = template `vp/prompts/s3a_select.md` filled with: criteria of the type + rules of the type
+ k relevant cases + patterns. Cases are chosen by CODE:
  * lexical similarity to the transcript (share of the case's stemmed vocabulary present in the
    transcript), ties by case id;
  * balance hit/miss: positives and negatives are taken alternately;
  * a character budget for the cases block (`case_chars_budget`), each case text capped.
Holdout isolation (SPEC S3c п. 4): if any case of the version comes from a recording listed as
holdout in splits.yaml, the builder raises `HoldoutLeak` — such a version must not reach a prompt.

The director's task (`brief`, optional — request.yaml `brief` of a live job) becomes its own
section between "Length" and "The transcript": the template has one `{{brief_section}}` slot
there, filled from `vp/prompts/s3a_brief.md`. Invariant: WITHOUT a brief the slot renders to
nothing and the prompt is byte for byte what it was before the section existed — knowledge
evals, replays and prompt hashes of jobs without a brief do not depend on this feature.

The transcript itself is passed to the judge as the input `subtitles.txt`, not inside the prompt.
S3c replays use the same function with a candidate version id and no brief.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import VPError
from ..core.io import sha256_bytes
from ..steps._fragments import norm_tokens
from .store import KnowledgeStore, KnowledgeVersion

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "prompts" / "s3a_select.md"
BRIEF_TEMPLATE_PATH = TEMPLATE_PATH.with_name("s3a_brief.md")
BRIEF_SLOT = "{{brief_section}}"    # in the selection template, right before "## The transcript"
BRIEF_TEXT = "{{brief}}"            # in the brief template, between <brief> and </brief>
_BRIEF_CLOSE_RE = re.compile(r"<(\s*/\s*brief\s*)>", re.I)
# No lengths here: the job's own range goes into the "Length" section and must be the only one.
KIND_TITLE = {"reel": "reel (short vertical clip)",
              "long": "long fragment (a self-contained lesson)"}
WORDS_PER_S = 2.2           # Russian lecture speech, used only for the length hint in words
DEFAULTS = {"k_cases": 6, "case_chars_budget": 6000, "case_text_max": 700}


@dataclass
class BuiltPrompt:
    text: str
    version: str
    kind: str
    cases: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    chars: int = 0
    sha256: str = ""

    def meta(self) -> dict[str, Any]:
        return {"version": self.version, "kind": self.kind, "cases": self.cases, "rules": self.rules,
                "chars": self.chars, "sha256": self.sha256}


def template_text() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def template_hash() -> str:
    """Hash of the selection template as a job WITHOUT a brief gets it (the brief slot rendered
    to nothing). The optional section therefore changed the state of no job that has no brief;
    its own wording is hashed by `brief_template_hash` and enters the state of briefed jobs only."""
    return sha256_bytes(template_text().replace(BRIEF_SLOT, ""))


def brief_template_text() -> str:
    return BRIEF_TEMPLATE_PATH.read_text(encoding="utf-8")


def brief_template_hash() -> str:
    return sha256_bytes(brief_template_text())


def neutralize_brief(text: str) -> str:
    """The director's text made safe to sit inside <brief>…</brief>: a closing tag in it
    (`</brief>`, any case, any spacing) gets square brackets instead of angle ones, so it cannot
    end the block early. One pass is enough: the replacement holds no angle bracket, so no new
    closing tag can form around it."""
    return _BRIEF_CLOSE_RE.sub(lambda m: f"[{m.group(1)}]", text)


def brief_section(brief: str | None) -> str:
    """The "director's task" section that fills the brief slot; "" when there is no brief
    (None, empty or whitespace only). The text goes in as data: nothing in it is a placeholder."""
    body = (brief or "").strip()
    if not body:
        return ""
    tpl = brief_template_text().strip("\n")
    if tpl.count(BRIEF_TEXT) != 1:
        raise VPError("internal", f"{BRIEF_TEMPLATE_PATH.name} must hold exactly one {BRIEF_TEXT}")
    head, _, tail = tpl.partition(BRIEF_TEXT)
    return head + neutralize_brief(body) + tail + "\n\n"


def stems(text: str) -> set[str]:
    """Crude stems for lexical similarity: tokens of >= 4 letters cut to 6 characters."""
    return {t[:6] for t in norm_tokens(text) if len(t) >= 4 and not t.isdigit()}


def similarity(case_text: str, transcript_stems: set[str]) -> float:
    s = stems(case_text)
    return len(s & transcript_stems) / len(s) if s else 0.0


def select_cases(kv: KnowledgeVersion, kind: str, transcript: str, k: int, budget: int,
                 text_max: int) -> list[dict[str, Any]]:
    """k most similar cases of the type, alternating positive/negative, within `budget` chars."""
    tstems = stems(transcript)
    ranked: dict[str, list[tuple[float, str, dict]]] = {"positive": [], "negative": []}
    for c in kv.cases_for(kind):
        ranked[c["label"]].append((-similarity(c["text"], tstems), c["id"], c))
    for lst in ranked.values():
        lst.sort(key=lambda x: (x[0], x[1]))
    out: list[dict[str, Any]] = []
    used = 0
    queues = [list(ranked["positive"]), list(ranked["negative"])]
    turn = 0
    while len(out) < k and any(queues):
        q = queues[turn % 2] or queues[(turn + 1) % 2]
        turn += 1
        _, _, c = q.pop(0)
        size = len(format_case(c, text_max))
        if used + size > budget:
            continue
        out.append(c)
        used += size
    return out


def format_case(c: dict[str, Any], text_max: int) -> str:
    text = " ".join(str(c["text"]).split())
    if len(text) > text_max:
        text = text[:text_max].rsplit(" ", 1)[0] + " …"
    parts = [f"- **{c['id']}** ({c['label']}, {c['type']}): «{text}»"]
    if c.get("reason_class"):
        parts.append(f"  Reason: {c['reason_class']}" + (f" — {c['reason']}" if c.get("reason") else ""))
    parts.append(f"  Lesson: {c['lesson']}")
    return "\n".join(parts)


def format_rule(r: dict[str, Any]) -> str:
    return f"- **{r['id']}** ({r.get('kind', 'forbid')}): {r['text']}"


def build_prompt(store: KnowledgeStore, version: str, kind: str, transcript: str, *, cap: int,
                 min_s: float, max_s: float, settings: dict[str, Any] | None = None,
                 brief: str | None = None) -> BuiltPrompt:
    """Build the selection prompt of `kind` (reel|long) from knowledge `version`.

    `brief` — the director's task for this recording (free text) or None. Replays pass none."""
    if kind not in KIND_TITLE:
        raise ValueError(kind)
    opts = {**DEFAULTS, **(settings or {})}
    kv = store.load_version(version)
    store.check_not_holdout([c["recording"] for c in kv.cases], "the selection prompt (case library)")
    cases = select_cases(kv, kind, transcript, int(opts["k_cases"]), int(opts["case_chars_budget"]),
                         int(opts["case_text_max"]))
    rules = kv.rules_for(kind)
    values = {
        "kind": kind, "kind_title": KIND_TITLE[kind], "version": version,
        "criteria": kv.criteria[kind].strip(),
        "rules": "\n".join(format_rule(r) for r in rules) or "(no rules yet)",
        "cases": "\n".join(format_case(c, int(opts["case_text_max"])) for c in cases) or "(no cases yet)",
        "patterns": kv.patterns.strip() or "(no patterns yet)",
        "cap": str(int(cap)), "min_s": _num(min_s), "max_s": _num(max_s),
        "min_words": str(int(round(min_s * WORDS_PER_S))),
        "max_words": str(int(round(max_s * WORDS_PER_S))),
    }
    template = template_text()
    if template.count(BRIEF_SLOT) != 1:
        raise VPError("internal", f"{TEMPLATE_PATH.name} must hold exactly one {BRIEF_SLOT}")
    # The template is cut at the brief slot, each side is filled on its own and the section is
    # put between them afterwards. So the director's text is never scanned for placeholders, and
    # without a brief the result is the two sides joined — the template as it was before the slot.
    head, _, tail = template.partition(BRIEF_SLOT)
    text = _fill(head, values) + brief_section(brief) + _fill(tail, values)
    return BuiltPrompt(text=text, version=version, kind=kind, cases=[c["id"] for c in cases],
                       rules=[str(r["id"]) for r in rules], chars=len(text), sha256=sha256_bytes(text))


def _fill(text: str, values: dict[str, str]) -> str:
    for k_, v in values.items():
        text = text.replace("{{" + k_ + "}}", v)
    return text


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)
