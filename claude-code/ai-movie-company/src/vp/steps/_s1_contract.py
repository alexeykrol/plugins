"""The `ID|text` proofreading contract (SPEC S1), ported from `src/prototype/contract.py`.

Each out_NN.txt mirrors in_NN.txt: the same IDs in the same order, text only. Any violation is a
hard error — the model's output is never trusted for bookkeeping. Violation classes (`cls` in the
error details): no_separator, pipe_in_text, empty_id, duplicate, extra, missing, order.
"""
from __future__ import annotations

from ..core.errors import VPError

_FENCE = ("```", "```text", "```txt", "```plain", "```plaintext")


def format_chunk(rows: list[tuple[str, str]]) -> str:
    """[(id, text)] -> chunk text `ID|text` per line (trailing newline)."""
    return "".join(f"{i}|{t}\n" for i, t in rows)


def _strip_fence(lines: list[str]) -> list[str]:
    """Drop one wrapping markdown fence (```...```) around the whole answer, if present."""
    nonblank = [k for k, ln in enumerate(lines) if ln.strip()]
    if len(nonblank) >= 2:
        a, b = nonblank[0], nonblank[-1]
        if lines[a].strip().lower() in _FENCE and lines[b].strip() == "```":
            return lines[a + 1:b]
    return lines


def parse_chunk(text: str, expected_ids: list[str], label: str = "chunk") -> list[tuple[str, str]]:
    """Validate an `ID|text` answer against `expected_ids` (order matters); returns [(id, text)].

    Blank lines are ignored; text is stripped. Raises VPError("contract", ..., cls=...) on any
    violation.
    """
    got: list[tuple[str, str]] = []
    lines = _strip_fence(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    for ln, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if "|" not in line:
            raise VPError("contract", f'{label}:{ln}: no "|" separator', cls="no_separator", chunk=label)
        n, _, t = line.partition("|")
        if "|" in t:
            raise VPError("contract", f'{label}:{ln}: "|" inside text', cls="pipe_in_text", chunk=label)
        n = n.strip()
        if not n:
            raise VPError("contract", f"{label}:{ln}: empty ID", cls="empty_id", chunk=label)
        got.append((n, t.strip()))
    ids = [n for n, _ in got]
    expected = list(expected_ids)
    seen: set[str] = set()
    dup = sorted({n for n in ids if n in seen or seen.add(n)})
    exp_set, got_set = set(expected), set(ids)
    extra = sorted(got_set - exp_set)
    missing = [n for n in expected if n not in got_set]
    if dup:
        raise VPError("contract", f"{label}: duplicate IDs {dup[:10]}", cls="duplicate", chunk=label)
    if extra:
        raise VPError("contract", f"{label}: extra IDs {extra[:10]}", cls="extra", chunk=label)
    if missing:
        raise VPError("contract", f"{label}: missing IDs {missing[:10]}", cls="missing", chunk=label)
    if ids != expected:
        first = next(i for i, (a, b) in enumerate(zip(ids, expected)) if a != b)
        raise VPError("contract", f"{label}: order differs from source at position {first + 1}: "
                                  f"{ids[first]} != {expected[first]}", cls="order", chunk=label)
    return got


def normalize_answer(rows: list[tuple[str, str]]) -> str:
    """Canonical text of a validated answer (what is written to out_NN.txt)."""
    return format_chunk(rows)
