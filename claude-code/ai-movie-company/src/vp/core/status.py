"""Append-only status.yaml (SPEC §5, BUILD §4).

Format: {current_stage, current_state, history: [{stage, item, state, at, by, metadata}]}.
Every append rewrites the file atomically with the previous history unchanged + one new entry.
A manual reset is recorded as state `stale` with metadata {reset: true}.
"""
from __future__ import annotations

import copy
import os
from typing import Any

from .io import now_iso, read_yaml, write_yaml
from .paths import Job

STATES = ("running", "done", "failed", "stale", "waiting_human", "waiting_judgment",
          "abandoned", "inconclusive", "skipped")


# Parsed status.yaml per path, keyed by the file identity: every write is atomic (new inode),
# so any change — by this process or another — invalidates the entry.
_CACHE: dict[str, tuple[tuple[int, int, int], dict[str, Any]]] = {}


class Status:
    """status.yaml of one job."""

    def __init__(self, job: Job):
        self.job = job

    def _data(self) -> dict[str, Any]:
        """Parsed file, cached by (inode, size, mtime_ns). Internal: callers must not mutate."""
        path = self.job.status_yaml
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return {"current_stage": None, "current_state": None, "history": []}
        key = (st.st_ino, st.st_size, st.st_mtime_ns)
        hit = _CACHE.get(str(path))
        if hit is not None and hit[0] == key:
            return hit[1]
        data = read_yaml(path, default=None)
        if not isinstance(data, dict):
            data = {"current_stage": None, "current_state": None, "history": []}
        data.setdefault("history", [])
        _CACHE[str(path)] = (key, data)
        return data

    def load(self) -> dict[str, Any]:
        return copy.deepcopy(self._data())

    def history(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._data()["history"])

    def append(self, stage: str, item: str | None, state: str, by: str = "runner",
               metadata: dict[str, Any] | None = None, at: str | None = None) -> dict[str, Any]:
        """Append one entry; returns it. Previous history is preserved exactly."""
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        data = self._data()
        entry = {"stage": stage, "item": item, "state": state, "at": at or now_iso(), "by": by,
                 "metadata": dict(metadata or {})}
        history = data["history"] + [entry]
        out = {"current_stage": stage if item is None else f"{stage}.{item}",
               "current_state": state, "history": history}
        write_yaml(self.job.status_yaml, out)
        return entry

    def entries(self, stage: str, item: str | None) -> list[dict[str, Any]]:
        """History entries of one instance, oldest first (copies)."""
        return copy.deepcopy([e for e in self._data()["history"]
                              if e.get("stage") == stage and e.get("item") == item])

    def last(self, stage: str, item: str | None) -> dict[str, Any] | None:
        es = self.entries(stage, item)
        return es[-1] if es else None

    @staticmethod
    def _since_reset(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for i in range(len(entries) - 1, -1, -1):
            if (entries[i].get("metadata") or {}).get("reset"):
                return entries[i + 1:]
        return entries

    def failures(self, stage: str, item: str | None) -> int:
        """Consecutive failures since the last manual reset or the last `done`."""
        n = 0
        for e in self._since_reset(self.entries(stage, item)):
            if e["state"] == "done":
                n = 0
            elif e["state"] == "failed":
                n += 1
        return n

    def is_abandoned(self, stage: str, item: str | None) -> bool:
        """True if the instance was abandoned and not manually reset since."""
        return any(e["state"] == "abandoned" for e in self._since_reset(self.entries(stage, item)))

    def reset(self, stage: str, item: str | None, by: str = "cli") -> dict[str, Any]:
        """Manual reset: clears the loop guard for the instance."""
        return self.append(stage, item, "stale", by=by, metadata={"reset": True})

    def open_wait(self, stage: str, item: str | None) -> dict[str, Any] | None:
        """The waiting_human entry that opened the current gate (after the last done/reset)."""
        found = None
        for e in self._since_reset(self.entries(stage, item)):
            if e["state"] == "done":
                found = None
            elif e["state"] == "waiting_human" and found is None:
                found = e
        return found
