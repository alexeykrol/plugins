"""Append-only cost journal: <vp_home>/costs.jsonl (SPEC §7.4).

One line per paid call: model tokens (Claude), API calls, YouTube quota units.
Record fields: at, job, kind (judge|youtube_quota|api), task/op, model, input_tokens,
output_tokens, cache_read_tokens, cache_write_tokens, usd, units.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .io import append_line, now_iso

_NUMERIC = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "usd", "units")


class CostJournal:
    """Cost journal under the pipeline home."""

    def __init__(self, home: str | os.PathLike, job_id: str | None = None):
        self.path = Path(home) / "costs.jsonl"
        self.job_id = job_id

    def add(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Append one record. Numeric fields default to 0."""
        rec: dict[str, Any] = {"at": now_iso(), "job": self.job_id, "kind": kind}
        for k in _NUMERIC:
            rec[k] = fields.pop(k, 0) or 0
        rec.update(fields)
        append_line(self.path, json.dumps(rec, ensure_ascii=False, default=str))
        return rec

    def records(self, job: str | None = None) -> list[dict[str, Any]]:
        """All records (optionally of one job). Corrupt lines are skipped."""
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if job is None or rec.get("job") == job:
                    out.append(rec)
        return out

    def totals(self, job: str | None = None, since: str | None = None) -> dict[str, float]:
        """Sum numeric fields (optionally per job and since an ISO timestamp)."""
        tot = {k: 0.0 for k in _NUMERIC}
        for r in self.records(job):
            if since and str(r.get("at", "")) < since:
                continue
            for k in _NUMERIC:
                tot[k] += float(r.get(k) or 0)
        return tot
