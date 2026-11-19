"""Human-input guard of the dialog gates: a decision needs a real user input AFTER the item was shown.

Why: the plugin session (a model) runs `review set` / `approve set` itself. A text rule ("never
decide for the user") did not hold against a user who said "decide everything yourself". The engine
therefore accepts a gate decision only when the user demonstrably answered after seeing the item.

Evidence of user input: the plugin hooks (hooks/hooks.json -> hooks/record_user_input.py) stamp
the time of every user prompt (`UserPromptSubmit`) and every answer to a question with buttons
(`PostToolUse` on `AskUserQuestion`) into `<runtime>/user_input.json`. No user text is stored.
<runtime> = $VP_RUNTIME_DIR, else $CLAUDE_PLUGIN_DATA, else ~/.ai_video_pipeline/runtime (the same
order as scripts/vp and the hook script).

Evidence of showing: `review show` / `review preview` and `approve show` stamp per-item `shown_at`
(the first show of the item's current content; a re-show without a new answer does not move it).

Rules (CLI decision commands only; the file path — a human editing fragments.review.yaml,
approve-ingest from a notifier — is not affected):
  review set <id>, approve set <id>   last user input > shown_at of that item
  review add, review close            last user input > the last show of the review
Otherwise: `REFUSED user-input-required <id>: ...`, exit 4, nothing written. No user-input file at
all (hooks not installed / old plugin) also refuses with exit 4 — the guard fails closed.

VP_GUARD=off disables the guard. DEV/TEST ONLY: the plugin's commands and skills never set it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..core.io import parse_iso, read_yaml, write_yaml
from ._common import UserInputRequired

GUARD_ENV = "VP_GUARD"
USER_INPUT_FILE = "user_input.json"


def runtime_dir() -> Path:
    for key in ("VP_RUNTIME_DIR", "CLAUDE_PLUGIN_DATA"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return Path(v).expanduser()
    return Path.home() / ".ai_video_pipeline" / "runtime"


def user_input_path() -> Path:
    return runtime_dir() / USER_INPUT_FILE


def guard_enabled() -> bool:
    return (os.environ.get(GUARD_ENV) or "").strip().lower() != "off"


def now_precise() -> str:
    """UTC ISO time with microseconds (a show and an answer can fall into the same second)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_iso(str(value))
    except (ValueError, TypeError):
        return None


def required_message(item: str) -> str:
    return f"user-input-required {item}: show it to the user and record the user's own answer"


def unavailable_message(path: Path, why: str) -> str:
    return (f"user-input-unknown: {why} ({path}). The plugin's hooks that record the user's answers "
            f"are not active: the plugin is outdated or partly installed. Update the plugin "
            f"(`claude plugin update ai-movie-company` or reinstall it), restart Claude, show the "
            f"item again and record the user's own answer")


def last_user_input() -> datetime:
    """Time of the latest real user input; refuses (fail closed) when there is no evidence."""
    path = user_input_path()
    if not path.exists():
        raise UserInputRequired(unavailable_message(path, "no record of user input"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise UserInputRequired(unavailable_message(path, "the user-input record is unreadable"))
    stamps: list[datetime] = []
    # Inside Claude Code the engine runs under the session's Bash, which exports its session id.
    # Then only THIS session's answers count: a message typed in another chat is not an answer
    # to this dialog. Without the variable (a terminal run) any session counts.
    own = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if own and isinstance(sessions, dict):
        s = sessions.get(own)
        at = _ts(s.get("at")) if isinstance(s, dict) else None
        if at is None:
            raise UserInputRequired(required_message("all"))
        return at
    if isinstance(data, dict):
        latest = data.get("latest")
        if isinstance(latest, dict):
            stamps.append(_ts(latest.get("at")))
        for s in (data.get("sessions") or {}).values() if isinstance(data.get("sessions"), dict) else ():
            if isinstance(s, dict):
                stamps.append(_ts(s.get("at")))
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        raise UserInputRequired(unavailable_message(path, "the user-input record has no time"))
    return max(stamps)


def require_user_after(items: Iterable[tuple[str, Any]]) -> None:
    """items = [(id, shown_at)]. Every item must have been shown before the last user input."""
    if not guard_enabled():
        return
    items = list(items)
    last = last_user_input()
    for item, shown in items:
        s = _ts(shown)
        if s is None or not last > s:
            raise UserInputRequired(required_message(item))


# ---- shown records ------------------------------------------------------------------------------

def load_shown(path: Path) -> dict[str, Any]:
    doc = read_yaml(path, default=None)
    if not isinstance(doc, dict):
        doc = {}
    if not isinstance(doc.get("items"), dict):
        doc["items"] = {}
    return doc


def mark_shown(path: Path, keys: dict[str, Any], key_field: str) -> dict[str, Any]:
    """Stamp items as shown now. keys = {id: content key}; `shown_at` = the first show of the
    current key (kept on re-shows, reset when the key changes); `last_shown_at` = now, also at
    the document level (the last show of anything)."""
    doc = load_shown(path)
    at = now_precise()
    for item, key in keys.items():
        old = doc["items"].get(item)
        same = isinstance(old, dict) and old.get(key_field) == key and _ts(old.get("shown_at"))
        doc["items"][item] = {key_field: key,
                              "shown_at": old["shown_at"] if same else at,
                              "last_shown_at": at}
    doc.setdefault("first_shown_at", at)
    doc["last_shown_at"] = at
    write_yaml(path, doc)
    return doc
