#!/usr/bin/env python3
"""ai-movie-company hook: the dialog journal (K6, K27) — every prompt the person typed.

Wired in hooks/hooks.json beside record_user_input.py (which stores only the TIME, for the gates):
  UserPromptSubmit -> journal_prompt.py

Appends one JSON line per typed prompt to <home>/journal/<YYYY-MM-DD>.jsonl (local date),
<home> = $VP_HOME, else ~/.ai_video_pipeline (the pipeline home, same as the engine):
  {"at": ISO local time, "session_id": ..., "kind": "prompt", "cwd": ..., "text": ...}
Unexpected requests are new scenarios: the journal is read offline (`vp journal show`,
`vp feedback pack`) to find them. It stays on the person's disk; nothing is sent anywhere.

Prompts no person typed (background-agent reports, scheduled tasks, cross-session messages) are
skipped by their envelope, exactly like record_user_input.py. Anything that looks like a key or a
token is replaced by [redacted] before writing (same patterns as vp.journal.redact).

Contract: standard library only, fast, prints nothing (UserPromptSubmit stdout would become
context for the model), exit code 0 on every path.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

MAX_CHARS = 20000
MACHINE_PREFIXES = ("<task-notification", "<scheduled-task", "<cross-session-message",
                    "<background-agent")
# keep in sync with vp.journal.SECRET_PATTERNS (a test compares them)
SECRET_PATTERNS = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}",
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd|client[_-]?secret|refresh[_-]?token|"
    r"access[_-]?token|authorization)\b(\s*[:=]\s*)[\"']?[^\s\"']{6,}",
    r"\bsk[-_][A-Za-z0-9_-]{16,}",
    r"\bxi-[A-Za-z0-9]{16,}",
    r"\bAIza[0-9A-Za-z_-]{30,}",
    r"\bya29\.[0-9A-Za-z._-]{20,}",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\b\d{8,10}:[A-Za-z0-9_-]{30,}",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}\b",
)
_COMPILED = [re.compile(p) for p in SECRET_PATTERNS]


def redact(text: str) -> str:
    for rx in _COMPILED:
        if rx.groups >= 2:
            text = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
            text = rx.sub("[redacted]", text)
    return text


def home_dir() -> str:
    v = (os.environ.get("VP_HOME") or "").strip()
    return os.path.expanduser(v) if v else os.path.join(os.path.expanduser("~"), ".ai_video_pipeline")


def is_typed(prompt: object) -> bool:
    return isinstance(prompt, str) and bool(prompt.strip()) \
        and not prompt.lstrip().startswith(MACHINE_PREFIXES)


def record(event: dict, home: str | None = None, now: datetime | None = None) -> str | None:
    """Append the prompt; returns the journal file path, None when skipped."""
    if not isinstance(event, dict) or not is_typed(event.get("prompt")):
        return None
    now = now or datetime.now().astimezone()
    text = redact(str(event["prompt"]))
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + " […]"
    line = {"at": now.isoformat(timespec="seconds"),
            "session_id": str(event.get("session_id") or "unknown")[:200],
            "kind": "prompt", "cwd": str(event.get("cwd") or "")[:500], "text": text}
    d = os.path.join(home or home_dir(), "journal")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{now.strftime('%Y-%m-%d')}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        event = json.loads(raw) if raw.strip() else {}
        record(event if isinstance(event, dict) else {})
    except BaseException:  # noqa: BLE001 - a hook must never break or talk into the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
