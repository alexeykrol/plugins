#!/usr/bin/env python3
"""ai-movie-company hook: remember WHEN the user last gave input (never WHAT they wrote).

Wired in hooks/hooks.json:
  UserPromptSubmit                 -> record_user_input.py prompt   (the user sent a message)
  PostToolUse, AskUserQuestion     -> record_user_input.py ask      (the user answered buttons)

Writes <runtime>/user_input.json, <runtime> = $VP_RUNTIME_DIR, else $CLAUDE_PLUGIN_DATA, else
~/.ai_video_pipeline/runtime (same order as scripts/vp and vp.gates.human):
  {"latest": {"at": ISO, "session_id": ..., "source": "prompt"|"ask"},
   "sessions": {<session_id>: {"at": ISO, "source": ...}, ...}}      (newest 50 sessions)
The engine's dialog-gate decisions (`review set`, `approve set`, ...) are accepted only when this
time is later than the moment the item was shown (vp.gates.human).

Contract: standard library only, fast, prints nothing (UserPromptSubmit stdout would become
context for the model), exit code 0 on every path, the user's text is never stored.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

FILE = "user_input.json"
MAX_SESSIONS = 50
SOURCES = ("prompt", "ask")
# UserPromptSubmit also fires for prompts no person typed (background agent reports, scheduled
# tasks, messages from other sessions). The ones recognisable by their envelope are skipped.
MACHINE_PREFIXES = ("<task-notification", "<scheduled-task", "<cross-session-message",
                    "<background-agent")


def data_dir() -> str:
    for key in ("VP_RUNTIME_DIR", "CLAUDE_PLUGIN_DATA"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return os.path.expanduser(v)
    return os.path.join(os.path.expanduser("~"), ".ai_video_pipeline", "runtime")


def is_user_event(source: str, event: dict) -> bool:
    if source == "prompt":
        prompt = event.get("prompt")
        if isinstance(prompt, str) and prompt.lstrip().startswith(MACHINE_PREFIXES):
            return False
        return True
    tool = event.get("tool_name")
    return tool in (None, "AskUserQuestion")


def record(source: str, event: dict, directory: str | None = None, now: str | None = None) -> bool:
    """Update the record; True when written."""
    if source not in SOURCES or not isinstance(event, dict) or not is_user_event(source, event):
        return False
    directory = directory or data_dir()
    at = now or datetime.now(timezone.utc).isoformat(timespec="microseconds")
    sid = str(event.get("session_id") or "unknown")[:200]
    path = os.path.join(directory, FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    sessions[sid] = {"at": at, "source": source}
    if len(sessions) > MAX_SESSIONS:
        keep = sorted(sessions.items(), key=lambda kv: str((kv[1] or {}).get("at")), reverse=True)
        sessions = dict(keep[:MAX_SESSIONS])
    out = {"latest": {"at": at, "session_id": sid, "source": source}, "sessions": sessions}
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".user_input.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def main(argv: list[str]) -> int:
    try:
        source = argv[1] if len(argv) > 1 else ""
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        event = json.loads(raw) if raw.strip() else {}
        record(source, event if isinstance(event, dict) else {})
    except BaseException:  # noqa: BLE001 - a hook must never break or talk into the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
