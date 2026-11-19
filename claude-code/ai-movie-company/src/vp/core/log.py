"""Structured JSON-lines log in <vp_home>/logs/ (SPEC §7.7).

No raw texts and no secrets: values of secret-looking keys are replaced, long strings truncated.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io import append_line, now_iso

_SECRET_MARKERS = ("token", "secret", "password", "authorization", "api_key", "apikey", "cookie")
_SAFE_KEYS = {"vp_key", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
              "token_env", "chat_id_env"}
MAX_STR = 200


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    return k not in _SAFE_KEYS and any(m in k for m in _SECRET_MARKERS)


def scrub(value: Any, key: str = "") -> Any:
    """Recursively redact secret-looking keys and truncate long strings."""
    if key and _is_secret_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str) and len(value) > MAX_STR:
        return value[:MAX_STR] + f"...[+{len(value) - MAX_STR} chars]"
    if isinstance(value, (int, float, bool)) or value is None or isinstance(value, str):
        return value
    return str(value)


class Logger:
    """Append-only JSON-lines logger: <vp_home>/logs/vp-YYYY-MM-DD.jsonl."""

    def __init__(self, home: str | os.PathLike, job_id: str | None = None):
        self.dir = Path(home) / "logs"
        self.job_id = job_id

    def path(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.dir / f"vp-{day}.jsonl"

    def event(self, name: str, **fields: Any) -> dict[str, Any]:
        """Write one event; returns the scrubbed record."""
        rec = {"at": now_iso(), "event": name, "job": self.job_id}
        rec.update(scrub(fields))
        append_line(self.path(), json.dumps(rec, ensure_ascii=False, default=str))
        return rec

    def child(self, job_id: str) -> "Logger":
        return Logger(self.dir.parent, job_id)


class NullLogger(Logger):
    """Logger that writes nothing (for pure unit tests)."""

    def __init__(self) -> None:
        super().__init__("/nonexistent", None)

    def event(self, name: str, **fields: Any) -> dict[str, Any]:
        return {"event": name, **scrub(fields)}
