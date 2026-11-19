"""S3c eval (code): metrics of a knowledge version, three SEPARATE groups (SPEC S3c п. 1).

Input is a list of evaluation units — one per (recording, type):

    {"recording_id", "type", "proposals": [{"outcome": accept|adjust|reject|unevaluated,
      "reason_class": str|None, "error_s": float|None}], "misses": int|None,
     "prompt_chars": int|None, "cost_usd": float|None}

plus per-recording info {rid: {"duration_ms", "review_minutes", "full_scan_minutes"}}.

Groups per type:
  quality  precision = (accept + 0.5 · adjust) / proposals, unevaluated counted as reject;
           accept share; boundary error (mean seconds of shift over adjusts; 0 when there are
           proposals but no adjust); reject share by reason class (+ "unevaluated")
  misses   added (live) / uncovered reference places (replay) per recording — ONLY over
           recordings with full_scan; else "not computed"
  load     candidates per recording, adjust share (+ overall: review minutes per recording, from
           human-time data, live only; never inherited by a replay)
Overall: cost per recording minute, prompt size per type.

The yardstick (metric definitions, reason vocabulary, matching rule) is pinned: YARDSTICK and its
hash are recorded in splits.yaml; the analyst cannot change it.
"""
from __future__ import annotations

from typing import Any, Iterable

from ..core.io import sha256_obj
from ..steps.s3b_review import REASON_CLASSES   # the reason vocabulary is part of the yardstick
from . import _matching as M

NOT_COMPUTED = "not computed"
OUTCOMES = ("accept", "adjust", "reject", "unevaluated")
ADJUST_WEIGHT = 0.5

# direction: +1 higher is better, -1 lower is better
GROUP_METRICS: dict[str, list[tuple[str, int]]] = {
    "quality": [("precision", +1), ("boundary_error_s", -1)],
    "misses": [("misses_per_recording", -1)],
    "load": [("candidates_per_recording", -1), ("adjust_share", -1)],
}
OVERALL_LOAD = [("review_minutes_per_recording", -1)]
GROUPS = tuple(GROUP_METRICS)

YARDSTICK: dict[str, Any] = {
    "version": "y1",
    "reason_classes": list(REASON_CLASSES),
    "matching": {"coverage_iou_min": M.COVERAGE_IOU_MIN,
                 "inherit_tolerance_ms": M.INHERIT_TOLERANCE_MS,
                 "inherit_same_subtitle_boundary": True},
    "metrics": {"adjust_weight": ADJUST_WEIGHT, "unevaluated_as": "reject",
                "groups": {g: [[m, d] for m, d in ms] for g, ms in GROUP_METRICS.items()},
                "misses_only_full_scan": True},
}
# names that denote the yardstick: an analyst candidate mentioning them as edit targets is refused
YARDSTICK_KEYS = ("yardstick", "metrics", "metric", "reason_classes", "reason_vocabulary",
                  "vocabulary", "matching", "tolerance", "iou", "splits", "holdout", "fix_set",
                  "verdicts")


def yardstick_hash() -> str:
    return sha256_obj(YARDSTICK)


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(float(x), n)


def quality(proposals: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(proposals)
    cnt = {o: sum(1 for p in proposals if p["outcome"] == o) for o in OUTCOMES}
    out: dict[str, Any] = {"proposals": n, "evaluated": n - cnt["unevaluated"], **{f"n_{o}": cnt[o] for o in OUTCOMES}}
    if n == 0:
        out.update({"precision": None, "accept_share": None, "boundary_error_s": None,
                    "reject_share_by_reason": {}, "unevaluated_share": None})
        return out
    errs = [float(p.get("error_s") or 0.0) for p in proposals if p["outcome"] == "adjust"]
    by_reason: dict[str, int] = {}
    for p in proposals:
        if p["outcome"] == "reject":
            rc = p.get("reason_class") or "unspecified"
            by_reason[rc] = by_reason.get(rc, 0) + 1
        elif p["outcome"] == "unevaluated":
            by_reason["unevaluated"] = by_reason.get("unevaluated", 0) + 1
    out.update({
        "precision": _r((cnt["accept"] + ADJUST_WEIGHT * cnt["adjust"]) / n),
        "accept_share": _r(cnt["accept"] / n),
        "boundary_error_s": _r(sum(errs) / len(errs) if errs else 0.0, 2),
        "reject_share_by_reason": {k: _r(v / n) for k, v in sorted(by_reason.items())},
        "unevaluated_share": _r(cnt["unevaluated"] / n),
    })
    return out


def aggregate(units: Iterable[dict[str, Any]], recs: dict[str, dict[str, Any]],
              kinds: Iterable[str] = ("reel", "long")) -> dict[str, Any]:
    """Metrics of a set of evaluation units (see module docstring)."""
    units = list(units)
    types: dict[str, Any] = {}
    for kind in kinds:
        us = [u for u in units if u["type"] == kind]
        props = [p for u in us for p in u["proposals"]]
        rec_ids = sorted({u["recording_id"] for u in us})
        full = [u for u in us if u.get("misses") is not None]
        q = quality(props)
        misses: dict[str, Any] = {"full_scan_recordings": len(full)}
        if full:
            total = sum(int(u["misses"]) for u in full)
            misses.update({"misses": total, "misses_per_recording": _r(total / len(full))})
        else:
            misses.update({"misses": NOT_COMPUTED, "misses_per_recording": NOT_COMPUTED})
        load = {"recordings": len(rec_ids),
                "candidates_per_recording": _r(len(props) / len(rec_ids)) if rec_ids else None,
                "adjust_share": _r(q["n_adjust"] / len(props)) if props else None}
        chars = [u["prompt_chars"] for u in us if u.get("prompt_chars")]
        types[kind] = {"quality": q, "misses": misses, "load": load,
                       "prompt_chars": int(round(sum(chars) / len(chars))) if chars else None}
    rec_ids = sorted({u["recording_id"] for u in units})
    minutes = sum(float((recs.get(r) or {}).get("duration_ms") or 0) for r in rec_ids) / 60000.0
    costs = [u["cost_usd"] for u in units if u.get("cost_usd") is not None]
    rmins = [float(recs[r]["review_minutes"]) for r in rec_ids
             if (recs.get(r) or {}).get("review_minutes") is not None]
    fmins = [float(recs[r]["full_scan_minutes"]) for r in rec_ids
             if (recs.get(r) or {}).get("full_scan_minutes") is not None]
    overall = {
        "recordings": len(rec_ids),
        "recording_minutes": _r(minutes, 2),
        "cost_usd": _r(sum(costs), 6) if costs else None,
        "cost_per_recording_min_usd": _r(sum(costs) / minutes, 6) if costs and minutes else None,
        "prompt_chars": max([t["prompt_chars"] for t in types.values() if t["prompt_chars"]] or [0]) or None,
        "review_minutes_per_recording": _r(sum(rmins) / len(rmins), 2) if rmins else NOT_COMPUTED,
        "full_scan_minutes_per_recording": _r(sum(fmins) / len(fmins), 2) if fmins else NOT_COMPUTED,
        "unevaluated_share": _unevaluated_share(units),
    }
    return {"types": types, "overall": overall}


def _unevaluated_share(units: list[dict[str, Any]]) -> float | None:
    props = [p for u in units for p in u["proposals"]]
    if not props:
        return None
    return _r(sum(1 for p in props if p["outcome"] == "unevaluated") / len(props))


# ---- comparison ---------------------------------------------------------------------------------

def _num(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def compare_metric(cand: Any, base: Any, direction: int, tolerance: float = 0.0) -> str:
    """better | worse | equal | n/a. A candidate that loses a metric the baseline has = worse."""
    c, b = _num(cand), _num(base)
    if c is None and b is None:
        return "n/a"
    if c is None:
        return "worse"
    if b is None:
        return "n/a"
    d = (c - b) * direction
    if d < -abs(tolerance) - 1e-9:
        return "worse"
    if d > 1e-9:
        return "better"
    return "equal"


def _fold(verdicts: Iterable[str]) -> str:
    vs = [v for v in verdicts if v != "n/a"]
    if not vs:
        return "n/a"
    if "worse" in vs:
        return "worse"
    return "better" if "better" in vs else "equal"


def compare(cand: dict[str, Any], base: dict[str, Any], tolerance: dict[str, float] | None = None,
            include_overall_load: bool = False) -> dict[str, Any]:
    """Compare two aggregates group by group (across types). Returns
    {"groups": {group: better|worse|equal|n/a}, "details": [...]}"""
    tol = tolerance or {}
    details = []
    per_group: dict[str, list[str]] = {g: [] for g in GROUPS}
    for kind in sorted(set(cand.get("types", {})) | set(base.get("types", {}))):
        ct, bt = cand["types"].get(kind) or {}, base["types"].get(kind) or {}
        for g, ms in GROUP_METRICS.items():
            for m, direction in ms:
                cv, bv = (ct.get(g) or {}).get(m), (bt.get(g) or {}).get(m)
                v = compare_metric(cv, bv, direction, float(tol.get(m, 0.0)))
                per_group[g].append(v)
                details.append({"type": kind, "group": g, "metric": m, "candidate": cv,
                                "baseline": bv, "verdict": v})
    if include_overall_load:
        for m, direction in OVERALL_LOAD:
            cv, bv = cand["overall"].get(m), base["overall"].get(m)
            v = compare_metric(cv, bv, direction, float(tol.get(m, 0.0)))
            per_group["load"].append(v)
            details.append({"type": "all", "group": "load", "metric": m, "candidate": cv,
                            "baseline": bv, "verdict": v})
    return {"groups": {g: _fold(vs) for g, vs in per_group.items()}, "details": details}


# ---- text table ------------------------------------------------------------------------------------

def _fmt(x: Any, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, str):
        return "n/c" if x == NOT_COMPUTED else x
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def table(rows: list[tuple[str, dict[str, Any]]], kinds: Iterable[str] = ("reel", "long")) -> str:
    """Small text table: one line per (label, metrics) and type."""
    head = (f"{'version':10} {'type':5} {'recs':>4} {'prop':>5} {'prec':>5} {'acc':>5} {'berr_s':>6} "
            f"{'uneval':>6} {'miss/rec':>8} {'cand/rec':>8} {'adj':>5} {'prompt':>7} {'$/min':>8}")
    lines = [head, "-" * len(head)]
    for label, m in rows:
        for kind in kinds:
            t = (m.get("types") or {}).get(kind)
            if not t:
                continue
            q, mi, lo = t["quality"], t["misses"], t["load"]
            lines.append(
                f"{label:10} {kind:5} {lo['recordings']:>4} {q['proposals']:>5} {_fmt(q['precision']):>5} "
                f"{_fmt(q['accept_share']):>5} {_fmt(q['boundary_error_s'], 1):>6} "
                f"{_fmt(q['unevaluated_share']):>6} {_fmt(mi['misses_per_recording']):>8} "
                f"{_fmt(lo['candidates_per_recording']):>8} {_fmt(lo['adjust_share']):>5} "
                f"{_fmt(t['prompt_chars']):>7} {_fmt(m['overall'].get('cost_per_recording_min_usd'), 4):>8}")
    return "\n".join(lines)
