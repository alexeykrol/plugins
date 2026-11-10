"""Журнал прогонов и статус — только счётчики, пути и коды ошибок, без текста документов (S8, S12)."""
from __future__ import annotations

import json
from datetime import datetime

from .config import Base, write_private


def set_status(base: Base, stage: str, **counters) -> None:
    try:
        cur = json.loads(base.status.read_text(encoding="utf-8")) if base.status.exists() else {}
    except json.JSONDecodeError:
        cur = {}
    cur.setdefault("stages", {})[stage] = {"ts": datetime.now().isoformat(timespec="seconds"), **counters}
    cur["last_stage"] = stage
    write_private(base.status, json.dumps(cur, ensure_ascii=False, indent=1))


def log_run(base: Base, stage: str, *, units_in=0, units_out=0, tokens_est=0, duration=0.0, errors=()) -> dict:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    rec = dict(ts=ts, stage=stage, units_in=units_in, units_out=units_out, tokens_est=tokens_est,
               duration=round(duration, 3), errors=list(errors)[:200])
    write_private(base.runs / f"{ts}-{stage}.json", json.dumps(rec, ensure_ascii=False))
    return rec
