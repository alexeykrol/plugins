"""Notifier: messages to the owner and their button answers (BUILD §7).

  send(text, buttons=None) -> message_ref
  poll_answers() -> list[{"message_ref", "data", "at", "user"?}]

buttons: list of rows, each row a list of {"text": str, "data": str} (Telegram inline keyboard);
a flat list of dicts means one button per row. `data` is at most 64 bytes (Telegram limit).

SessionNotifier — the default inside a Claude session: logs the message to
<vp_home>/session_notifications.jsonl and nothing else; the session itself tells the user (gates
are passed in the dialog through `review ...` / `approve ...`). poll_answers() is always empty.
FileNotifier — <vp_home>/outbox/*.json and <vp_home>/inbox/*.json (tests, offline runs).
TelegramNotifier — Bot API over urllib; the token is read from the env var NAMED in config and is
never logged or put into error messages.
"""
from __future__ import annotations

import abc
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..core.errors import VPError
from ..core.io import now_iso, read_json, write_json

MAX_TEXT = 4096


def _norm_buttons(buttons: Any) -> list[list[dict[str, str]]] | None:
    if not buttons:
        return None
    rows = []
    for row in buttons:
        cells = [row] if isinstance(row, dict) else list(row)
        out_row = []
        for b in cells:
            text, data = str(b["text"]), str(b["data"])
            if len(data.encode("utf-8")) > 64:
                raise VPError("data", f"button data longer than 64 bytes: {data[:20]}...")
            out_row.append({"text": text, "data": data})
        rows.append(out_row)
    return rows


def _check_text(text: str) -> None:
    if not text:
        raise VPError("data", "empty notification")
    if len(text) > MAX_TEXT:
        raise VPError("data", f"notification longer than {MAX_TEXT} chars ({len(text)})")


class Notifier(abc.ABC):
    @abc.abstractmethod
    def send(self, text: str, buttons: Any = None) -> str:
        """Send a message; returns a message reference."""

    @abc.abstractmethod
    def poll_answers(self) -> list[dict[str, Any]]:
        """New button answers since the last poll (each returned once)."""


class SessionNotifier(Notifier):
    """No-op notifier for interactive sessions: appends one JSON line per message to
    <vp_home>/session_notifications.jsonl (a local journal, never sent anywhere)."""

    LOG_NAME = "session_notifications.jsonl"

    def __init__(self, home: str | os.PathLike):
        self.home = Path(home)
        self.path = self.home / self.LOG_NAME

    def _count(self) -> int:
        if not self.path.exists():
            return 0
        with open(self.path, "rb") as f:
            return sum(1 for _ in f)

    def send(self, text: str, buttons: Any = None) -> str:
        _check_text(text)
        rows = _norm_buttons(buttons)
        self.home.mkdir(parents=True, exist_ok=True)
        ref = f"session-{self._count() + 1:06d}"
        line = json.dumps({"message_ref": ref, "at": now_iso(), "text": text, "buttons": rows},
                          ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return ref

    def sent(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def poll_answers(self) -> list[dict[str, Any]]:
        return []


class FileNotifier(Notifier):
    """Writes outbox/<n>.json; reads inbox/*.json (moved to inbox/processed/ after reading)."""

    def __init__(self, home: str | os.PathLike):
        self.home = Path(home)
        self.outbox = self.home / "outbox"
        self.inbox = self.home / "inbox"

    def send(self, text: str, buttons: Any = None) -> str:
        _check_text(text)
        rows = _norm_buttons(buttons)
        self.outbox.mkdir(parents=True, exist_ok=True)
        n = len(list(self.outbox.glob("*.json"))) + 1
        while (self.outbox / f"{n:06d}.json").exists():
            n += 1
        ref = f"file-{n:06d}"
        write_json(self.outbox / f"{n:06d}.json",
                   {"message_ref": ref, "at": now_iso(), "text": text, "buttons": rows})
        return ref

    def sent(self) -> list[dict[str, Any]]:
        """All messages in the outbox, oldest first (test helper)."""
        if not self.outbox.exists():
            return []
        return [read_json(p) for p in sorted(self.outbox.glob("*.json"))]

    def answer(self, message_ref: str, data: str, user: str = "owner") -> Path:
        """Simulate a button press (test helper): writes one inbox file."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        n = len(list(self.inbox.glob("*.json"))) + len(list((self.inbox / "processed").glob("*.json"))) + 1
        return write_json(self.inbox / f"{n:06d}.json",
                          {"message_ref": message_ref, "data": data, "at": now_iso(), "user": user})

    def poll_answers(self) -> list[dict[str, Any]]:
        if not self.inbox.exists():
            return []
        done = self.inbox / "processed"
        done.mkdir(parents=True, exist_ok=True)
        out = []
        for p in sorted(self.inbox.glob("*.json")):
            out.append(read_json(p))
            os.replace(p, done / p.name)
        return out


Opener = Callable[..., Any]


class TelegramNotifier(Notifier):
    """Telegram Bot API via urllib. Env var names come from config; values never logged."""

    API = "https://api.telegram.org"

    def __init__(self, token_env: str, chat_id_env: str, home: str | os.PathLike | None = None,
                 urlopen: Opener | None = None, timeout_s: float = 30):
        self.token_env = token_env
        self.chat_id_env = chat_id_env
        self.offset_path = Path(home) / "telegram_offset.json" if home else None
        self.urlopen = urlopen or urllib.request.urlopen
        self.timeout_s = timeout_s

    def _token(self) -> str:
        tok = os.environ.get(self.token_env)
        if not tok:
            raise VPError("auth", f"Telegram token env var {self.token_env} is not set")
        return tok

    def _chat_id(self) -> str:
        cid = os.environ.get(self.chat_id_env)
        if not cid:
            raise VPError("data", f"Telegram chat id env var {self.chat_id_env} is not set")
        return cid

    def build_request(self, method: str, payload: dict[str, Any]) -> urllib.request.Request:
        """POST JSON request for a Bot API method (the URL contains the token — never log it)."""
        url = f"{self.API}/bot{self._token()}/{method}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(url, data=body, method="POST",
                                      headers={"Content-Type": "application/json"})

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        req = self.build_request(method, payload)
        try:
            with self.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            code = e.code
            kind = "auth" if code in (401, 403) else ("transport" if code >= 500 or code == 429 else "data")
            raise VPError(kind, f"Telegram {method}: HTTP {code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise VPError("transport", f"Telegram {method}: {type(e).__name__}") from None
        except json.JSONDecodeError:
            raise VPError("contract", f"Telegram {method}: invalid JSON") from None
        if not data.get("ok"):
            raise VPError("data", f"Telegram {method}: {str(data.get('description'))[:200]}")
        return data.get("result")

    def send(self, text: str, buttons: Any = None) -> str:
        _check_text(text)
        payload: dict[str, Any] = {"chat_id": self._chat_id(), "text": text,
                                   "disable_web_page_preview": True}
        rows = _norm_buttons(buttons)
        if rows:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": b["text"], "callback_data": b["data"]} for b in row] for row in rows]}
        result = self._call("sendMessage", payload)
        return str(result["message_id"])

    def poll_answers(self) -> list[dict[str, Any]]:
        offset = 0
        if self.offset_path is not None:
            offset = int((read_json(self.offset_path, default={}) or {}).get("offset", 0))
        result = self._call("getUpdates", {"offset": offset, "timeout": 0,
                                           "allowed_updates": ["callback_query"]})
        out = []
        chat = self._chat_id()
        for upd in result or []:
            offset = max(offset, int(upd["update_id"]) + 1)
            cq = upd.get("callback_query")
            if not cq:
                continue
            msg = cq.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(chat):
                continue  # answers from other chats are ignored
            out.append({"message_ref": str(msg.get("message_id")), "data": cq.get("data"),
                        "at": now_iso(), "user": str((cq.get("from") or {}).get("id"))})
        if self.offset_path is not None:
            write_json(self.offset_path, {"offset": offset})
        return out
