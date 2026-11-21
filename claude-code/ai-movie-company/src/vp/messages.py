"""Messages from the plugin's authors to the person: the feed shown inside Claude.

Design: docs/channel-and-telemetry.md §2–3. Server contract v1:
  GET <base>/messages?since=<int>&install=<id>&version=<v>
  200 {"messages": [{"id": int, "created": ISO, "kind": "update"|"info",
                     "text": {"ru": "...", "en": "..."}, "min_version"?, "max_version"?,
                     "expires"?}], "latest": int}
  (the server filters by version and expiry and returns id > since).
<base> = env AIMC_TELEMETRY_URL (vp.telemetry.base_url).

Locally, <VP_HOME>/telemetry/messages.json keeps: `since` (the highest id fetched), `seen` (ids
shown), `pending` (fetched, not shown yet), `last_poll_at`. The network is asked at most every
POLL_EVERY_S unless --force; otherwise `poll` answers from what was fetched before. Failures are
silent: the answer is what is pending.

A message is shown by the mod only while the engine is idle (`vp busy`), never during a render or an
approval gate; after showing, `vp messages ack <id>` records it as seen and spools the telemetry
event {"kind": "message_shown", "id": N} — the receiver's acknowledgement.

Before the terms are accepted the feed is not asked at all (nothing leaves the Mac). With telemetry
off (`vp telemetry off` or AIMC_TELEMETRY=off) the feed is still asked, without the install id.

`poll --json` also carries `update` (a newer version installed on disk: restart needed) and
`reminder` (an auto-update reminder due by the schedule), see vp.update.

CLI:  vp messages poll [--json] [--lang ru|en] [--force]
      vp messages ack <id>
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import telemetry

POLL_EVERY_S = 6 * 3600
TIMEOUT_S = 6.0
LANGS = ("ru", "en")
MAX_KEEP = 200


def state_path() -> Path:
    return telemetry.tdir() / "messages.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("since", 0)
            data.setdefault("seen", [])
            data.setdefault("pending", [])
            return data
    except (OSError, ValueError):
        pass
    return {"since": 0, "seen": [], "pending": [], "last_poll_at": 0}


def _write(data: dict[str, Any]) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".messages.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _vtuple(v: Any) -> tuple[int, ...] | None:
    """Numeric version, as the receiver compares: "v" prefix, -pre and +build ignored, missing
    parts are zeros (1.2 == 1.2.0)."""
    import re
    s = re.split(r"[-+]", str(v).strip().lstrip("vV"), maxsplit=1)[0]
    try:
        parts = [int(x) for x in s.split(".")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.append(0)
    while len(parts) > 3 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def applies(msg: dict[str, Any], version: str, now: datetime | None = None) -> bool:
    """The local twin of the server's filter: version range and expiry."""
    mine = _vtuple(version)
    lo, hi = _vtuple(msg.get("min_version")), _vtuple(msg.get("max_version"))
    if mine is not None:
        if msg.get("min_version") and lo is not None and mine < lo:
            return False
        if msg.get("max_version") and hi is not None and mine > hi:
            return False
    exp = msg.get("expires")
    if exp:
        try:
            when = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when <= (now or datetime.now(timezone.utc)):
                return False
        except ValueError:
            pass
    return True


def text_in(msg: dict[str, Any], lang: str) -> str:
    """The text in the person's language, else English, else Russian, else any."""
    t = msg.get("text")
    if isinstance(t, str):
        return t
    if not isinstance(t, dict):
        return ""
    for k in (lang, "en", "ru"):
        if isinstance(t.get(k), str) and t[k].strip():
            return t[k].strip()
    for v in t.values():
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _valid(msg: Any) -> bool:
    return isinstance(msg, dict) and isinstance(msg.get("id"), int) and not isinstance(msg.get("id"), bool)


def fetch(since: int) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """One GET of the feed: (messages, latest, error). Nothing leaves the Mac before the terms are
    accepted (no request at all); the install id goes only when telemetry may be sent."""
    if telemetry.consent_accepted_at() is None:
        return [], None, "no consent"
    q = {"since": str(int(since)), "version": telemetry.plugin_version()}
    if telemetry.can_send():
        iid = telemetry.install_id()
        if iid:
            q["install"] = iid
    url = telemetry.base_url() + "/messages?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": f"ai-movie-company/{telemetry.plugin_version()}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read(1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as e:
        return [], None, f"http {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return [], None, f"{type(e).__name__}"
    if not isinstance(data, dict):
        return [], None, "bad answer"
    msgs = [m for m in (data.get("messages") or []) if _valid(m)]
    latest = data.get("latest")
    return msgs, latest if isinstance(latest, int) else None, None


def poll(lang: str = "en", force: bool = False, now: float | None = None) -> dict[str, Any]:
    """Unseen messages in `lang`, oldest first; asks the network at most every POLL_EVERY_S."""
    now = time.time() if now is None else now
    st = _read()
    polled, error = False, None
    if force or now - float(st.get("last_poll_at") or 0) >= POLL_EVERY_S:
        msgs, latest, error = fetch(int(st.get("since") or 0))
        if error is None:
            polled = True
            seen = set(st["seen"])
            have = {m["id"] for m in st["pending"] if _valid(m)}
            for m in msgs:
                if m["id"] not in seen and m["id"] not in have:
                    st["pending"].append(m)
                    have.add(m["id"])
            # `since` follows what was received, not `latest`: a message the server held back for
            # this version may apply after an update
            st["since"] = max([int(st.get("since") or 0)] + [m["id"] for m in msgs])
            st["latest"] = latest
            st["last_poll_at"] = now
            _write(st)
    version = telemetry.plugin_version()
    seen = set(st["seen"])
    out = []
    for m in sorted((m for m in st["pending"] if _valid(m)), key=lambda m: m["id"]):
        if m["id"] in seen or not applies(m, version):
            continue
        text = text_in(m, lang)
        if text:
            out.append({"id": m["id"], "kind": m.get("kind") or "info", "created": m.get("created"),
                        "text": text})
    return {"messages": out, "polled": polled, "error": error}


def ack(msg_id: int) -> bool:
    """Record a message as shown: seen locally + a `message_shown` telemetry event."""
    st = _read()
    if msg_id not in st["seen"]:
        st["seen"] = (st["seen"] + [msg_id])[-MAX_KEEP:]
    st["pending"] = [m for m in st["pending"] if not (_valid(m) and m["id"] == msg_id)]
    st["last_seen"] = max(int(st.get("last_seen") or 0), msg_id)
    _write(st)
    telemetry.record("message_shown", id=msg_id)
    return True


def local_signals() -> dict[str, Any]:
    """What the mod shows beside the feed, found on this machine (vp.update): a newer version
    downloaded by an update (restart needed), and whether an auto-update reminder is due by the
    schedule (the idle rule is the mod's: it asks `vp busy` before showing anything)."""
    from . import update
    out: dict[str, Any] = {"update": None, "reminder": None}
    try:
        chk = update.check()
        out["update"] = chk if chk["restart_needed"] else None
    except Exception:  # noqa: BLE001 - silent, like the feed
        pass
    try:
        due = update.reminder_due(check_busy=False)
        out["reminder"] = due if due["due"] else None
    except Exception:  # noqa: BLE001
        pass
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp messages", description="messages from the plugin's authors")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll")
    p.add_argument("--json", action="store_true")
    p.add_argument("--lang", default="en")
    p.add_argument("--force", action="store_true")
    a_ = sub.add_parser("ack")
    a_.add_argument("id", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "poll":
        lang = a.lang if a.lang in LANGS else "en"
        res = poll(lang, force=a.force)
        res.update(local_signals())
        if a.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            for m in res["messages"]:
                print(f"{m['id']}\t{m['kind']}\t{m['text']}")
            if not res["messages"]:
                print("no new messages")
        return 0
    ack(a.id)
    print(f"ACKED {a.id}")
    return 0
