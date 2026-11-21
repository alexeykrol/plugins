#!/usr/bin/env python3
"""ai-movie-company hook: the dialog journal (K6, K27) — every prompt the person typed.

Wired in hooks/hooks.json beside record_user_input.py (which stores only the TIME, for the gates):
  UserPromptSubmit -> journal_prompt.py

Appends one JSON line per typed prompt to <home>/journal/<YYYY-MM-DD>.jsonl (local date),
<home> = $VP_HOME, else ~/.ai_video_pipeline (the pipeline home, same as the engine):
  {"at": ISO local time, "session_id": ..., "kind": "prompt", "cwd": ..., "text": ...}

Only Elsa's sessions are recorded (owner, 2026-11-20), not every Claude Code session while the
plugin is enabled. A session is Elsa's once
  * a prompt calls her: a command of this plugin (/ai-movie-company:..., /elsa, /director) or her
    name in the text ("Эльза", "Elsa", "Эльзочка"); that prompt is recorded too; or
  * the engine ran a pipeline command in it (vp.cli marks CLAUDE_CODE_SESSION_ID through
    vp.journal.mark_session).
The marks live in <home>/journal/elsa-sessions.json (the last MAX_SESSIONS ids). /director-off
records its own prompt and then unmarks the session. (The mod's Leave button sends no prompt, so a
session left by the button stays marked until /director-off.)
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

# keep in sync with vp.journal (a test compares them)
SESSIONS_FILE = "elsa-sessions.json"
MAX_SESSIONS = 200
ENTER_PREFIXES = ("/ai-movie-company:", "/elsa", "/director")
LEAVE_PREFIXES = ("/director-off",)
NAME_PATTERN = r"(?<![\w])(эльз[а-яё]{0,4}|elsa)(?![\w])"
NAME_RE = re.compile(NAME_PATTERN, re.IGNORECASE)


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


def _first_word(prompt: str) -> str:
    words = prompt.strip().split()
    return words[0].lower() if words else ""


def leaves(prompt: str) -> bool:
    return _first_word(prompt) in LEAVE_PREFIXES


def enters(prompt: str) -> bool:
    """The prompt calls Elsa: a command of this plugin, or her name."""
    first = _first_word(prompt)
    if first in LEAVE_PREFIXES:
        return False
    if first.startswith(ENTER_PREFIXES[0]) or first in ENTER_PREFIXES[1:]:
        return True
    return bool(NAME_RE.search(prompt))


def _sessions_path(home: str) -> str:
    return os.path.join(home, "journal", SESSIONS_FILE)


def elsa_sessions(home: str) -> list:
    try:
        with open(_sessions_path(home), encoding="utf-8") as f:
            data = json.load(f)
        got = data.get("sessions") if isinstance(data, dict) else None
        return [s for s in got if isinstance(s, str)] if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def mark(home: str, session_id: str, on: bool) -> None:
    ids = [s for s in elsa_sessions(home) if s != session_id]
    if on:
        ids.append(session_id)
    path = _sessions_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"sessions": ids[-MAX_SESSIONS:]}, f)
    os.replace(tmp, path)


def record(event: dict, home: str | None = None, now: datetime | None = None) -> str | None:
    """Append the prompt if this is Elsa's session; returns the journal file path, None when
    skipped."""
    if not isinstance(event, dict) or not is_typed(event.get("prompt")):
        return None
    home = home or home_dir()
    prompt = str(event["prompt"])
    sid = str(event.get("session_id") or "").strip()[:200]
    known = bool(sid) and sid in elsa_sessions(home)
    if not known:
        if not enters(prompt):
            return None                   # not Elsa's session: nothing is written
        if sid:
            mark(home, sid, True)
    now = now or datetime.now().astimezone()
    text = redact(prompt)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + " […]"
    line = {"at": now.isoformat(timespec="seconds"),
            "session_id": str(event.get("session_id") or "unknown")[:200],
            "kind": "prompt", "cwd": str(event.get("cwd") or "")[:500], "text": text}
    d = os.path.join(home, "journal")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{now.strftime('%Y-%m-%d')}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    if sid and leaves(prompt):
        mark(home, sid, False)
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
