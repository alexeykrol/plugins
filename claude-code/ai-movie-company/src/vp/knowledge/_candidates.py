"""S3c critic, analyst and candidate_eval batches (SPEC S3c п. 2–4, Границы).

Holdout isolation is enforced here at every learning entry point: the critic input, the analyst
input, and every candidate's sources / new case (`guard_learning` -> store.check_not_holdout, plus
the recording must be explicitly in the fix-set). The prompt builder and store.create_version
enforce it for prompts and the case library.

Contracts (judge output is validated by code; invalid -> VPError("contract")):
  critic   {findings: [{title, type, reason_class, rules, evidence, likely_cause}]}
  analyst  {candidates: [{kind, hypothesis, expected_effect, sources, edit}]}
A structurally valid analyst candidate can still be refused one by one (`CandidateRefused`):
unknown source verdicts, edits that touch the yardstick, bad edit targets.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..core.errors import VPError
from ..core.io import dump_yaml, sha256_bytes
from . import _metrics as MX
from .store import CASE_LABELS, KINDS, RULE_KINDS, KnowledgeStore, KnowledgeVersion

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
CRITIC_TEMPLATE = PROMPTS / "s3c_critic.md"
ANALYST_TEMPLATE = PROMPTS / "s3c_analyst.md"
EDIT_KINDS = ("criteria_edit", "new_rule", "new_case", "pattern", "delete", "merge")
GENERALISING = ("criteria_edit", "new_rule", "pattern", "merge")
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.S)
LIMITS = {"title": 200, "likely_cause": 1000, "hypothesis": 1000, "expected_effect": 600,
          "rule_text": 400, "lesson": 600, "append": 3000}


class CandidateRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _load_yaml(text: str, who: str) -> Any:
    raw = (text or "").strip()
    m = _FENCE_RE.match(raw)
    if m:
        raw = m.group(1)
    try:
        return yaml.safe_load(raw) if raw else None
    except yaml.YAMLError as e:
        raise VPError("contract", f"{who} output is not YAML: {str(e)[:200]}") from e


def _text(v: Any, field: str, who: str, limit: int) -> str:
    if not isinstance(v, str) or not v.strip():
        raise VPError("contract", f"{who}: `{field}` must be a non-empty string")
    if len(v) > limit:
        raise VPError("contract", f"{who}: `{field}` too long")
    return v.strip()


# ---- holdout guard ------------------------------------------------------------------------------

def guard_learning(store: KnowledgeStore, recording_ids: Iterable[str], purpose: str) -> None:
    """Learning inputs may come only from fix-set recordings: holdout -> HoldoutLeak (hard stop);
    a recording in no split -> VPError(data)."""
    recs = set(map(str, recording_ids))
    store.check_not_holdout(recs, purpose)
    fix = set(map(str, store.read_splits().get("fix_set") or []))
    stray = recs - fix
    if stray:
        raise VPError("data", f"recording(s) {sorted(stray)} are not in the fix-set of splits.yaml; "
                              f"assign them before using them for {purpose}")


def learning_verdicts(store: KnowledgeStore, verdicts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Verdicts usable for learning: recordings of the fix-set (never holdout, never unassigned)."""
    fix = set(map(str, store.read_splits().get("fix_set") or [])) - store.holdout_recordings()
    return [v for v in verdicts if str(v.get("recording_id")) in fix]


def _short(text: Any, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + " …"


# ---- critic -------------------------------------------------------------------------------------

def critic_groups(verdicts: list[dict[str, Any]], examples: int = 3, text_chars: int = 300,
                  max_groups: int = 30) -> list[dict[str, Any]]:
    """Group verdicts by (type, verdict, reason class); errors first, larger groups first."""
    groups: dict[tuple, dict[str, Any]] = {}
    for v in verdicts:
        g = (v.get("type") or "?", v["verdict"], v.get("reason_class") or None)
        e = groups.setdefault(g, {"type": g[0], "verdict": g[1], "reason_class": g[2], "count": 0,
                                  "keys": [], "rules_cited": {}, "examples": []})
        e["count"] += 1
        e["keys"].append(str(v["key"]))
        for a in (v.get("proposed") or {}).get("applied") or []:
            e["rules_cited"][str(a)] = e["rules_cited"].get(str(a), 0) + 1
        lim = 1 if v["verdict"] == "accept" else examples
        if len(e["examples"]) < lim:
            e["examples"].append({"key": str(v["key"]), "text": _short(v.get("text"), text_chars),
                                  "reason": _short(v.get("reason"), 200) or None})
    order = sorted(groups.values(), key=lambda e: (e["verdict"] == "accept", -e["count"],
                                                   e["type"], e["verdict"], str(e["reason_class"])))
    return order[:max_groups]


def critic_input(store: KnowledgeStore, verdicts: list[dict[str, Any]], settings: dict[str, Any]
                 ) -> tuple[str, str, set[str]]:
    """(prompt, input text, keys) for the critic. Holdout / unassigned verdicts are refused."""
    guard_learning(store, [v["recording_id"] for v in verdicts], "the critic input")
    groups = critic_groups(verdicts, int(settings.get("examples_per_group", 3)),
                           int(settings.get("text_chars", 300)), int(settings.get("max_groups", 30)))
    keys = {k for g in groups for k in g["keys"]}
    prompt = CRITIC_TEMPLATE.read_text(encoding="utf-8")
    prompt = prompt.replace("{{reason_classes}}", ", ".join(MX.REASON_CLASSES)).replace(
        "{{max_findings}}", str(int(settings.get("max_findings", 8))))
    return prompt, dump_yaml({"groups": groups}), keys


def parse_critic(text: str, input_keys: set[str], known_refs: set[str]) -> list[dict[str, Any]]:
    data = _load_yaml(text, "critic")
    if not isinstance(data, dict) or set(data) != {"findings"}:
        raise VPError("contract", "critic output must be a mapping with the single key `findings`")
    items = data["findings"] or []
    if not isinstance(items, list):
        raise VPError("contract", "`findings` must be a list")
    allowed = {"title", "type", "reason_class", "rules", "evidence", "likely_cause"}
    out = []
    for i, f in enumerate(items, 1):
        who = f"critic finding {i}"
        if not isinstance(f, dict):
            raise VPError("contract", f"{who} is not a mapping")
        extra = set(f) - allowed
        if extra:
            raise VPError("contract", f"{who}: unknown keys {sorted(extra)}")
        title = _text(f.get("title"), "title", who, LIMITS["title"])
        cause = _text(f.get("likely_cause"), "likely_cause", who, LIMITS["likely_cause"])
        typ = f.get("type", "any")
        if typ not in (*KINDS, "any"):
            raise VPError("contract", f"{who}: type must be reel, long or any")
        rc = f.get("reason_class")
        if rc not in (None, "") and rc not in MX.REASON_CLASSES:
            raise VPError("contract", f"{who}: reason_class {rc!r} is not in the vocabulary")
        rules = f.get("rules") or []
        if not isinstance(rules, list) or not all(isinstance(r, (str, int)) for r in rules):
            raise VPError("contract", f"{who}: rules must be a list of ids")
        unknown_refs = [str(r) for r in rules if str(r) not in known_refs]
        if unknown_refs:
            raise VPError("contract", f"{who}: unknown rule/case ids {unknown_refs}")
        ev = f.get("evidence") or []
        if not isinstance(ev, list) or not ev:
            raise VPError("contract", f"{who}: evidence must be a non-empty list of verdict keys")
        bad = [str(k) for k in ev if str(k) not in input_keys]
        if bad:
            raise VPError("contract", f"{who}: evidence keys not in the input: {bad}")
        out.append({"title": title, "type": typ, "reason_class": rc or None,
                    "rules": [str(r) for r in rules], "evidence": [str(k) for k in ev],
                    "likely_cause": cause})
    return out


# ---- analyst ------------------------------------------------------------------------------------

def knowledge_index(kv: KnowledgeVersion) -> dict[str, Any]:
    return {"version": kv.id,
            "rules": [{"id": str(r["id"]), "applies_to": r.get("applies_to") or list(KINDS),
                       "kind": r.get("kind", "forbid"), "text": r["text"]} for r in kv.rules],
            "cases": [{"id": c["id"], "type": c["type"], "label": c["label"],
                       "lesson": _short(c.get("lesson"), 200)} for c in kv.cases]}


def analyst_input(store: KnowledgeStore, findings: list[dict[str, Any]],
                  verdicts: list[dict[str, Any]], kv: KnowledgeVersion, history: dict[str, Any],
                  settings: dict[str, Any]) -> tuple[str, dict[str, str], set[str]]:
    """(prompt, inputs, allowed source keys). Every evidence verdict must be a fix-set verdict:
    a holdout verdict anywhere in the input is refused (HoldoutLeak)."""
    by_key = {str(v["key"]): v for v in verdicts}
    ev = []
    for f in findings:
        for k in f.get("evidence") or []:
            if k not in by_key:
                raise VPError("data", f"critic evidence {k} is not a known verdict")
            ev.append(by_key[k])
    guard_learning(store, [v["recording_id"] for v in verdicts], "the analyst input")
    text_chars = int(settings.get("text_chars", 300))
    vlist = [{"key": str(v["key"]), "type": v.get("type"), "verdict": v["verdict"],
              "reason_class": v.get("reason_class"), "reason": _short(v.get("reason"), 200) or None,
              "applied": (v.get("proposed") or {}).get("applied") or [],
              "text": _short(v.get("text"), text_chars)} for v in verdicts]
    prompt = ANALYST_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{max_candidates}}", str(int(settings.get("max_candidates", 5))))
    inputs = {"findings.yaml": dump_yaml({"findings": findings}),
              "verdicts.yaml": dump_yaml({"verdicts": vlist}),
              "knowledge.yaml": dump_yaml(knowledge_index(kv)),
              "history.yaml": dump_yaml(history)}
    return prompt, inputs, set(by_key)


def parse_analyst(text: str, max_candidates: int = 5) -> list[dict[str, Any]]:
    """Structural contract only; per-candidate checks are in check_candidate."""
    data = _load_yaml(text, "analyst")
    if not isinstance(data, dict) or set(data) != {"candidates"}:
        raise VPError("contract", "analyst output must be a mapping with the single key `candidates`")
    items = data["candidates"] or []
    if not isinstance(items, list):
        raise VPError("contract", "`candidates` must be a list")
    if len(items) > max_candidates:
        raise VPError("contract", f"analyst returned {len(items)} candidates > {max_candidates}")
    for i, c in enumerate(items, 1):
        if not isinstance(c, dict):
            raise VPError("contract", f"analyst candidate {i} is not a mapping")
    return items


def _yardstick_touch(obj: Any, path: str = "") -> str | None:
    """Path of a key that names the yardstick, if any."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kk = str(k).lower()
            if kk in MX.YARDSTICK_KEYS:
                return f"{path}{k}"
            hit = _yardstick_touch(v, f"{path}{k}.")
            if hit:
                return hit
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hit = _yardstick_touch(v, f"{path}{i}.")
            if hit:
                return hit
    return None


def check_candidate(store: KnowledgeStore, c: dict[str, Any], all_keys: dict[str, dict[str, Any]],
                    kv: KnowledgeVersion) -> dict[str, Any]:
    """Validate one analyst candidate. Returns it normalized; raises CandidateRefused (with a
    reason) or HoldoutLeak (a source from a holdout recording)."""
    allowed = {"kind", "hypothesis", "expected_effect", "sources", "edit"}
    extra = set(c) - allowed
    hit = _yardstick_touch(c)
    if hit:
        raise CandidateRefused(f"touches the yardstick ({hit}); metrics, reason vocabulary, matching "
                               f"rule and splits are pinned")
    if extra:
        raise CandidateRefused(f"unknown keys {sorted(extra)}")
    kind = c.get("kind")
    if kind not in EDIT_KINDS:
        raise CandidateRefused(f"kind must be one of {EDIT_KINDS}")
    try:
        hyp = _text(c.get("hypothesis"), "hypothesis", "candidate", LIMITS["hypothesis"])
        eff = _text(c.get("expected_effect"), "expected_effect", "candidate", LIMITS["expected_effect"])
    except VPError as e:
        raise CandidateRefused(e.msg) from e
    srcs = c.get("sources")
    if not isinstance(srcs, list) or not srcs or not all(isinstance(s, str) for s in srcs):
        raise CandidateRefused("sources must be a non-empty list of verdict keys")
    srcs = list(dict.fromkeys(srcs))
    unknown = [s for s in srcs if s not in all_keys]
    if unknown:
        raise CandidateRefused(f"sources reference verdicts that do not exist: {unknown}")
    guard_learning(store, [all_keys[s]["recording_id"] for s in srcs], "analyst candidate sources")
    edit = c.get("edit")
    if not isinstance(edit, dict):
        raise CandidateRefused("edit must be a mapping")
    refs = kv.ref_ids()
    if kind == "criteria_edit":
        if edit.get("type") not in KINDS or not set(edit) <= {"type", "append"}:
            raise CandidateRefused("criteria_edit needs {type: reel|long, append: text}")
        _edit_text(edit.get("append"), "append", LIMITS["append"])
    elif kind in ("new_rule", "merge"):
        _check_rule(edit.get("rule"))
        if kind == "merge":
            t = edit.get("targets")
            rule_ids = {str(r["id"]) for r in kv.rules}
            if not isinstance(t, list) or len(t) < 2 or not all(str(x) in rule_ids for x in t):
                raise CandidateRefused("merge needs targets: at least two existing rule ids")
            if not set(edit) <= {"targets", "rule"}:
                raise CandidateRefused("merge edit has unknown keys")
        elif not set(edit) <= {"rule"}:
            raise CandidateRefused("new_rule edit has unknown keys")
    elif kind == "new_case":
        if not set(edit) <= {"source", "label", "lesson"}:
            raise CandidateRefused("new_case edit has unknown keys")
        src = str(edit.get("source") or srcs[0])
        if src not in srcs:
            raise CandidateRefused("new_case source must be one of the candidate's sources")
        if edit.get("label") not in CASE_LABELS:
            raise CandidateRefused(f"new_case label must be one of {CASE_LABELS}")
        _edit_text(edit.get("lesson"), "lesson", LIMITS["lesson"])
        if not str(all_keys[src].get("text") or "").strip():
            raise CandidateRefused("the source verdict has no fragment text")
    elif kind == "pattern":
        if not set(edit) <= {"append"}:
            raise CandidateRefused("pattern edit needs {append: text}")
        _edit_text(edit.get("append"), "append", LIMITS["append"])
    elif kind == "delete":
        if not set(edit) <= {"target"} or str(edit.get("target")) not in refs:
            raise CandidateRefused("delete needs target: an existing rule or case id")
    return {"kind": kind, "hypothesis": hyp, "expected_effect": eff, "sources": srcs, "edit": edit}


def _edit_text(v: Any, field: str, limit: int) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise CandidateRefused(f"edit.{field} must be a non-empty string up to {limit} characters")
    return v.strip()


def _check_rule(r: Any) -> None:
    if not isinstance(r, dict) or not set(r) <= {"applies_to", "kind", "text"}:
        raise CandidateRefused("rule must be {applies_to, kind, text}")
    _edit_text(r.get("text"), "rule.text", LIMITS["rule_text"])
    if r.get("kind", "forbid") not in RULE_KINDS:
        raise CandidateRefused(f"rule kind must be one of {RULE_KINDS}")
    at = r.get("applies_to") or list(KINDS)
    if not isinstance(at, list) or not all(k in KINDS for k in at):
        raise CandidateRefused("rule applies_to must be a list of reel/long")


def evidence_gate(c: dict[str, Any], all_keys: dict[str, dict[str, Any]], min_cases: int,
                  min_recordings: int) -> str | None:
    """None if the candidate has enough evidence, else the reason it waits (SPEC «Границы»)."""
    if c["kind"] not in GENERALISING:
        return None
    vs = [all_keys[s] for s in c["sources"]]
    cases = {(str(v["recording_id"]), str(v.get("fragment_id"))) for v in vs}
    recs = {str(v["recording_id"]) for v in vs}
    if len(cases) < min_cases:
        return f"too few independent cases: {len(cases)} < {min_cases}"
    if len(recs) < min_recordings:
        return (f"generalisation from {len(recs)} recording(s) needs confirmation on another "
                f"recording (min {min_recordings})")
    return None


def _next_rule_id(rules: list[dict[str, Any]]) -> str:
    nums = [int(m.group(1)) for r in rules if (m := re.fullmatch(r"R(\d+)", str(r["id"])))]
    return f"R{(max(nums) if nums else 0) + 1:03d}"


def build_changes(c: dict[str, Any], files: dict[str, str], kv: KnowledgeVersion,
                  all_keys: dict[str, dict[str, Any]]) -> tuple[dict[str, str], list[str]]:
    """Turn a checked candidate into (changes, removals) for store.derive_version."""
    kind, edit = c["kind"], c["edit"]
    changes: dict[str, str] = {}
    removals: list[str] = []
    rules = [dict(r) for r in kv.rules]
    if kind == "criteria_edit":
        rel = f"criteria/{edit['type']}.md"
        changes[rel] = files[rel].rstrip() + "\n\n" + edit["append"].strip() + "\n"
    elif kind == "pattern":
        changes["patterns.md"] = files["patterns.md"].rstrip() + "\n\n" + edit["append"].strip() + "\n"
    elif kind in ("new_rule", "merge"):
        r = edit["rule"]
        srcs = list(c["sources"])
        if kind == "merge":
            targets = {str(t) for t in edit["targets"]}
            for old in rules:
                if str(old["id"]) in targets:
                    srcs += [str(s) for s in old.get("sources") or []]
            rules = [x for x in rules if str(x["id"]) not in targets]
        rules.append({"id": _next_rule_id(kv.rules), "applies_to": list(r.get("applies_to") or KINDS),
                      "kind": r.get("kind", "forbid"), "text": r["text"].strip(),
                      "sources": list(dict.fromkeys(srcs))})
        changes["rules.yaml"] = dump_yaml({"rules": rules})
    elif kind == "new_case":
        src = str(edit.get("source") or c["sources"][0])
        v = all_keys[src]
        cid = f"c{src[:10]}"
        rel = f"cases/{cid}.yaml"
        if rel in files or cid in kv.ref_ids():
            raise CandidateRefused(f"case {cid} already exists")
        case = {"id": cid, "type": v.get("type") or "any", "label": edit["label"],
                "recording": str(v["recording_id"]), "verdict": v["verdict"],
                "reason_class": v.get("reason_class"), "reason": v.get("reason"),
                "text": " ".join(str(v.get("text") or "").split()),
                "lesson": edit["lesson"].strip(), "sources": [src]}
        changes[rel] = dump_yaml(case)
    elif kind == "delete":
        target = str(edit["target"])
        if any(str(r["id"]) == target for r in rules):
            rules = [r for r in rules if str(r["id"]) != target]
            changes["rules.yaml"] = dump_yaml({"rules": rules})
        else:
            for rel, text in files.items():
                if rel.startswith("cases/") and rel.endswith(".yaml"):
                    doc = yaml.safe_load(text) or {}
                    if str(doc.get("id")) == target:
                        removals.append(rel)
            if not removals:
                raise CandidateRefused(f"delete target {target} not found")
    return changes, removals


# ---- candidate_eval batch -----------------------------------------------------------------------

EVAL_HEADER = """\
# Candidate evaluation (S3c, human time kind candidate_eval). New proposals of knowledge version
# {version} that no earlier verdict covers. Give EVERY item a verdict, save, then run
#   python -m vp learn import-eval {version}
#   verdict:      accept | reject | adjust
#   reason:       required for reject and adjust
#   reason_class: optional, one of: {reasons}
#   start / end:  only for adjust: "#412" (subtitle number), "1:24:20" (time) or a quote
#   started_at / finished_at: optional ISO time of your work
"""


def batch_id(items: list[dict[str, Any]]) -> str:
    return sha256_bytes(dump_yaml(items))[:16]


def eval_template(version: str, items: list[dict[str, Any]], bid: str) -> str:
    doc = {"version": version, "batch_id": bid, "started_at": None, "finished_at": None,
           "items": items}
    return EVAL_HEADER.format(version=version, reasons=" ".join(MX.REASON_CLASSES)) + dump_yaml(doc)
