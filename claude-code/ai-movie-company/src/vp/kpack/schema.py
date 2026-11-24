"""Closed schema for data that can be delivered independently of the release."""
from __future__ import annotations

import html
import json
import re
import unicodedata
from datetime import date, datetime
from urllib.parse import urlsplit

from ..scenarios import _raw
from ..tutorials import tutorials

MAX_ENVELOPE = 512 * 1024
AMBIGUOUS_QUESTION = "scenario.detect.ambiguous"
HINT_KEYS = {AMBIGUOUS_QUESTION}
ALLOWED_HOSTS = {"alexeykrol.com", "t.me"}
MATERIALS = {"idea", "text", "script", "video", "subtitles", "audio", "pictures", "slides",
             "facts", "house-style"}
URL = re.compile(
    r"""(?i)(?:[a-z][a-z0-9+.-]*://|\b(?:https?|ftp|file|javascript|data|mailto):(?=\S)"""
    r"""|(?<!:)//|\bwww\.)[^\s<>"'`]*"""
)
COMMAND = re.compile(
    r"`|\b(?:curl|wget|npx|sudo|chmod|bash|vp)\s"
    r"|\b(?:pip[0-9.]*\s+install\b|npm\s+i(?:nstall)?\b|brew\s+install\b|rm\s+-|sh\s+-c)", re.I
)
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
SCENARIO_ID = r"[A-Z][A-Z0-9-]{0,31}"


class PackError(ValueError):
    """An error with a stable class that is safe to persist in local state."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind


def _require(condition, where):
    if not condition:
        raise PackError("schema", f"{where}: invalid value or fields")


def decode_json(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            _require(key not in obj, "JSON duplicate key")
            obj[key] = value
        return obj

    def reject_constant(_):
        raise PackError("schema", "non-finite JSON number")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, PackError):
            raise
        raise PackError("schema", "invalid UTF-8 JSON") from exc


def object_fields(value, required, where, optional=()):
    _require(isinstance(value, dict) and required <= value.keys()
             and value.keys() <= required.union(optional), where)


def semver(value):
    """Comparable SemVer precedence, excluding build metadata."""
    match = SEMVER.fullmatch(value) if isinstance(value, str) and len(value) <= 64 else None
    _require(match, "min_plugin")
    major, minor, patch, prerelease = match.groups()
    parts = prerelease.split(".") if prerelease else []
    _require(all(not p.isdigit() or p == "0" or not p.startswith("0") for p in parts), "min_plugin")
    order = tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)
    return int(major), int(minor), int(patch), prerelease is None, order


def _list(value, where, minimum=0, maximum=64):
    _require(isinstance(value, list) and minimum <= len(value) <= maximum, where)
    return value


def _text(value, where):
    _require(isinstance(value, str) and 0 < len(value) <= 2000 and value.strip(), where)
    normalized = unicodedata.normalize("NFKC", html.unescape(value))
    _require(all(c in "\t\n\r" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
                 for c in normalized), where)
    if COMMAND.search(normalized):
        raise PackError("text", f"{where}: forbidden command pattern")
    for match in URL.finditer(normalized):
        candidate = match.group().rstrip(".,;:!?)]}")
        if candidate.lower().startswith("www."):
            candidate = "//" + candidate
        try:
            parsed = urlsplit(candidate)
            allowed = (parsed.scheme in {"", "https", "http"} and parsed.hostname in ALLOWED_HOSTS
                       and parsed.username is None and "\\" not in candidate)
        except ValueError:
            allowed = False
        if not allowed:
            raise PackError("text", f"{where}: URL host is not allowed")


def _translated(value, where, *, many=False):
    object_fields(value, {"ru", "en"}, where)
    for lang, translation in value.items():
        field = f"{where}.{lang}"
        if many:
            for example in _list(translation, field, 1, 20):
                _text(example, field)
        else:
            _text(translation, field)


def _identifier(value, pattern, where):
    _require(isinstance(value, str) and len(value) <= 64 and re.fullmatch(pattern, value), where)
    return value


def _range(value, where):
    _require(isinstance(value, list) and len(value) == 2, where)
    _require(all(type(n) in (int, float) and 0 <= n <= 1_000_000 for n in value), where)
    _require(value[0] <= value[1], where)


def _defaults(value, where):
    object_fields(value, {"source_hours"}, where)
    source = value["source_hours"]
    if isinstance(source, dict) and "by_output" in source:
        object_fields(source, {"by_output"}, where)
        threshold = 0
        for row in _list(source["by_output"], where, 1, 32):
            _require(isinstance(row, list) and len(row) == 2, where)
            minutes, hours = row
            _require(type(minutes) in (int, float) and threshold < minutes <= 1_000_000, where)
            _range(hours, where)
            threshold = minutes
    else:
        object_fields(source, {"ratio_to_output"}, where)
        _range(source["ratio_to_output"], where)


def _scenario(value, baseline, where):
    object_fields(value, {"id", "name", "doc_status", "input", "detection", "intake_kinds", "steps"},
                  where, {"defaults"})
    sid = _identifier(value["id"], SCENARIO_ID, f"{where}.id")
    # Single-letter IDs are reserved by the release's telemetry contract.
    _require(len(sid) > 1 or sid in {s["id"] for s in baseline["scenarios"]}, f"{where}.id")
    _require(value["doc_status"] in ("built", "partial", "none"), f"{where}.doc_status")
    for key in ("name", "input"):
        _translated(value[key], f"{where}.{key}")
    detection = value["detection"]
    object_fields(detection, {"signals", "keywords", "choice"}, f"{where}.detection")
    for key in ("signals", "keywords"):
        field = f"{where}.detection.{key}"
        for item in _list(detection[key], field, 1):
            _text(item, field)
    _translated(detection["choice"], f"{where}.detection.choice")
    kinds = tuple(k for k in baseline["intake_checklists"] if k != "_all")
    for kind in _list(value["intake_kinds"], f"{where}.intake_kinds", 1, len(kinds)):
        _require(kind in kinds, f"{where}.intake_kinds")
    if "defaults" in value:
        _defaults(value["defaults"], f"{where}.defaults")
    seen = set()
    for step in _list(value["steps"], f"{where}.steps", 1):
        field = f"{where}.steps"
        object_fields(step, {"use"}, field, {"title", "optional"})
        step_id = step["use"]
        _require(isinstance(step_id, str) and step_id in baseline["lib"] and step_id not in seen,
                 f"{field}.use (unique release library step required)")
        seen.add(step_id)
        if "title" in step:
            _translated(step["title"], f"{field}.title")
        if "optional" in step:
            _require(type(step["optional"]) is bool
                     and (step["optional"] or not baseline["lib"][step_id].get("optional")),
                     f"{field}.optional")
    return sid


def validate(payload):
    """Validate a decoded payload. Raise PackError on the first violation."""
    object_fields(payload, {"format", "version", "created", "min_plugin", "content"}, "payload")
    _require(type(payload["format"]) is int and payload["format"] == 1, "payload.format")
    _require(type(payload["version"]) is int and 1 <= payload["version"] <= 2**63 - 1, "payload.version")
    created = payload["created"]
    _require(isinstance(created, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-5][0-9])?)?", created), "payload.created")
    try:
        if "T" in created:
            datetime.fromisoformat(created.replace("Z", "+00:00"))
        else:
            date.fromisoformat(created)
    except ValueError as exc:
        raise PackError("schema", "payload.created: invalid ISO date") from exc
    semver(payload["min_plugin"])

    content = payload["content"]
    object_fields(content, {"cases", "scenarios", "rules", "hints"}, "content")
    baseline = _raw()[0]
    scenario_ids = set()
    for i, entry in enumerate(_list(content["scenarios"], "scenarios", maximum=32)):
        sid = _scenario(entry, baseline, f"scenarios[{i}]")
        _require(sid not in scenario_ids, "duplicate scenario id")
        scenario_ids.add(sid)
    scenario_ids.update(s["id"] for s in baseline["scenarios"])

    tutorial_ids = {tutorial["id"] for tutorial in tutorials()}
    seen = set()
    for i, case in enumerate(_list(content["cases"], "cases", maximum=256)):
        field = f"cases[{i}]"
        object_fields(case, {"id", "title", "says", "scenario", "needs", "result", "status"},
                      field, {"gap", "tutorials"})
        cid = _identifier(case["id"], r"c-[a-z0-9-]+", f"{field}.id")
        _require(cid not in seen, "duplicate case id")
        seen.add(cid)
        for key in ("title", "says", "result"):
            _translated(case[key], f"{field}.{key}", many=key == "says")
        sid = _identifier(case["scenario"], SCENARIO_ID, f"{field}.scenario")
        _require(sid in scenario_ids, f"{field}.scenario")
        for need in _list(case["needs"], f"{field}.needs", maximum=len(MATERIALS)):
            _require(isinstance(need, str) and need in MATERIALS, f"{field}.needs")
        _require(case["status"] in ("ready", "partial"), f"{field}.status")
        _require(("gap" in case) == (case["status"] == "partial"), f"{field}.gap")
        if "gap" in case:
            _translated(case["gap"], f"{field}.gap")
        ids = _list(case.get("tutorials", []), f"{field}.tutorials", maximum=8)
        _require(all(isinstance(tid, str) and tid in tutorial_ids for tid in ids)
                 and len(set(ids)) == len(ids), f"{field}.tutorials")

    seen.clear()
    targets = scenario_ids | baseline["lib"].keys() | {"*"}
    for i, rule in enumerate(_list(content["rules"], "rules", maximum=128)):
        field = f"rules[{i}]"
        object_fields(rule, {"id", "applies_to", "text"}, field)
        rid = _identifier(rule["id"], r"[A-Za-z0-9][A-Za-z0-9_.-]*", f"{field}.id")
        _require(rid not in seen, "duplicate rule id")
        seen.add(rid)
        target = rule["applies_to"]
        _require(isinstance(target, str) and target in targets, f"{field}.applies_to")
        _translated(rule["text"], f"{field}.text")

    object_fields(content["hints"], set(), "hints", HINT_KEYS)
    for key, translation in content["hints"].items():
        _translated(translation, f"hints.{key}")
