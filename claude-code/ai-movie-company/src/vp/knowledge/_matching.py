"""S3c matching — two DISTINCT operations (SPEC S3c п. 4, E0-02 п. 4). Part of the yardstick.

  * coverage (misses metric): a reference strong place is found if a proposal of the same type
    overlaps it with IoU >= COVERAGE_IOU_MIN (0.5);
  * verdict inheritance (quality metric): an old verdict transfers to a new proposal only if BOTH
    the start and the end coincide within tolerance — the same subtitle boundary (subtitle mode on
    both sides) or <= INHERIT_TOLERANCE_MS (1 s). Otherwise the proposal is "unevaluated", even
    with a large overlap: an accept of [0–60] does not confirm [20–80].

A place / proposal is a dict with start_ms, end_ms, first_cue, last_cue, type and optionally
`boundary` ("subtitle" | "words").
"""
from __future__ import annotations

from typing import Any, Iterable

COVERAGE_IOU_MIN = 0.5
INHERIT_TOLERANCE_MS = 1000


def iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    """Intersection over union of two [start, end) intervals (0..1)."""
    inter = min(a[1], b[1]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def _span(p: dict[str, Any]) -> tuple[int, int]:
    return int(p["start_ms"]), int(p["end_ms"])


def boundary_match(new: dict[str, Any], old: dict[str, Any], side: str,
                   tolerance_ms: int = INHERIT_TOLERANCE_MS) -> bool:
    """One side coincides: |Δ| <= tolerance, or both are subtitle boundaries of the same subtitle."""
    if side not in ("start", "end"):
        raise ValueError(side)
    ms_key, cue_key = ("start_ms", "first_cue") if side == "start" else ("end_ms", "last_cue")
    if abs(int(new[ms_key]) - int(old[ms_key])) <= tolerance_ms:
        return True
    if new.get("boundary") == "subtitle" and old.get("boundary") == "subtitle":
        a, b = new.get(cue_key), old.get(cue_key)
        return a is not None and b is not None and str(a) == str(b)
    return False


def inherits(new: dict[str, Any], old: dict[str, Any],
             tolerance_ms: int = INHERIT_TOLERANCE_MS) -> bool:
    """True iff the old verdict may be transferred to the new proposal (same type, both sides)."""
    if new.get("type") and old.get("type") and new["type"] != old["type"]:
        return False
    return (boundary_match(new, old, "start", tolerance_ms)
            and boundary_match(new, old, "end", tolerance_ms))


def covers(proposal: dict[str, Any], reference: dict[str, Any],
           iou_min: float = COVERAGE_IOU_MIN) -> bool:
    """Coverage: same type and IoU >= iou_min."""
    if proposal.get("type") and reference.get("type") and proposal["type"] != reference["type"]:
        return False
    return iou(_span(proposal), _span(reference)) >= iou_min


def find_inherited(proposal: dict[str, Any], places: Iterable[dict[str, Any]],
                   tolerance_ms: int = INHERIT_TOLERANCE_MS) -> dict[str, Any] | None:
    """The verdict place the proposal inherits from: among matching places the LAST one wins
    (places are given oldest first, so a newer human verdict overrides an older one)."""
    found = None
    for pl in places:
        if inherits(proposal, pl, tolerance_ms):
            found = pl
    return found


def uncovered(references: Iterable[dict[str, Any]], proposals: list[dict[str, Any]],
              iou_min: float = COVERAGE_IOU_MIN) -> list[dict[str, Any]]:
    """Reference places not covered by any proposal (= misses)."""
    return [r for r in references if not any(covers(p, r, iou_min) for p in proposals)]


def dedup_places(places: list[dict[str, Any]], iou_min: float = COVERAGE_IOU_MIN) -> list[dict[str, Any]]:
    """Merge reference places that denote the same strong place (same type, IoU >= iou_min);
    the later one wins."""
    out: list[dict[str, Any]] = []
    for p in places:
        out = [q for q in out if not covers(q, p, iou_min)]
        out.append(p)
    return out
