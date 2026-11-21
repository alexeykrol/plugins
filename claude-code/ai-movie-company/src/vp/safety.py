"""Safety on the person's computer: the terms, the install guard, the permission mode (0.8.2).

Three small command groups; the hook `hooks/guard.py` enforces what they record.

  vp consent status [--json]      accepted the current terms? exit 0 yes, 5 no / an older version
  vp consent show [--json]        the short summary + where the full text is; stamps "shown"
  vp consent accept               after the person's own «Принимаю» (human gate) -> consent.json
  vp consent later                «Не сейчас»: nothing installs or publishes until accepted
  vp consent withdraw             takes the acceptance back

  vp guard status [--json]        on/off, the latest stops (id, what, when)
  vp guard approve <id> --verdict clean|caution [--report P] [--command C]
                                  after the person's own «Ставим» (human gate): ONE run of exactly
                                  that command (or the safer C the vet pinned), valid 30 minutes
  vp guard off --request | --confirm
                                  the explicit off switch: --request stamps the question, --confirm
                                  needs the person's own answer after it (human gate)
  vp guard on

  vp mode status [--json]         this session's permission mode (recorded by the hook from
                                  `permission_mode`), whether it asks before commands, Elsa's
                                  workspace, and whether to offer "fewer questions" now
  vp mode offer                   stamps the moment Elsa asks (the human gate of `enable`)
  vp mode enable [--workspace D]  after the person's own «Да»: writes ONLY <D>/.claude/settings.json
                                  (default D = ~/Movies/AI Movie Company): defaultMode acceptEdits +
                                  allow rules for the plugin's engine; the hook then lets a plain
                                  engine call in D through without Claude's card. Never the global
                                  ~/.claude/settings.json.
  vp mode decline                 «Нет, оставить как есть» — not offered again on its own
  vp mode undo                    «верни как было»: the settings file back byte for byte (or removed)

Files: <vp_home>/consent.json, <vp_home>/mode.json, <vp_home>/guard/{settings,pending,approvals}.json,
<vp_home>/guard/log.jsonl, <runtime>/session_modes.json (written by the hook). The hook forbids
Claude's tools to write them; only these commands do, behind vp.gates.human.

Why "acceptEdits" and not "auto": Claude Code ignores `defaultMode: auto` in project settings
("values auto and bypassPermissions don't take effect from project or local settings" —
code.claude.com/docs/en/settings, read 2026-11-19), and the owner forbids global settings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core.paths import vp_home
from .gates import human
from .gates._common import UserInputRequired

TERMS_VERSION = "2026-11-21-alpha1"      # keep in sync with hooks/guard.py and TERMS*.md
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
APPROVAL_TTL = timedelta(minutes=30)
RC_NOT_ACCEPTED = 5
RC_USER_INPUT = 4
RC_REFUSED = 2
ASKING_MODES = ("default", "acceptEdits", "plan")
VERDICTS = ("clean", "caution")

SUMMARY_RU = """\
Коротко об условиях альфа-теста SIMovie — плагина AI Movie Company с режиссёром монтажа Эльзой
(версия {version}):
- Это альфа: пользуетесь на свой риск, «как есть», без гарантий; может сломаться, измениться или
  закрыться. Храните копии своих файлов. Мы стараемся только об одном — о безопасности и о
  приватности в той мере, в какой можем её обеспечить.
- Плагин работает на вашем Mac. Ваши записи, видео, диалоги и журнал разговоров с Эльзой остаются
  у вас и никуда не отправляются.
- Автоматически, и только после вашего согласия, уходит техническая телеметрия: команды, шаги,
  коды ошибок, время, версии — без путей, имён, текстов и содержимого. Посмотреть: vp telemetry
  show; выключить в любой момент: vp telemetry off. Всё остальное вы присылаете нам сами.
- YouTube, ElevenLabs, стоки и Claude работают через ваши аккаунты и ключи, по их условиям
  (для YouTube — условия YouTube: https://www.youtube.com/t/terms). Отключить YouTube: vp youtube
  disconnect. Публикация — только после вашего «ок»; за свой контент отвечаете вы.
- Не пользуетесь личной ссылкой на установку 7 дней — место может перейти следующему. Возраст 18+.
- Полные условия: alexeykrol.com/elsa/terms/ · приватность: alexeykrol.com/elsa/privacy/ ·
  support@alexeykrol.com"""

SUMMARY_EN = """\
SIMovie alpha terms in short — the AI Movie Company plugin with editing director Elsa
(version {version}):
- This is an alpha: use it at your own risk, "as is", no warranty; it may break, change or stop.
  Keep copies of your files. The one thing we try to do is keep it safe, and private as far as we
  can.
- The plugin runs on your Mac. Your recordings, videos, dialogues and the journal of your talks
  with Elsa stay with you and are never sent.
- Automatically, and only after you agree, technical telemetry is sent: commands, steps, error
  codes, timings, versions — no paths, names, text or content. See it: vp telemetry show; turn it
  off any time: vp telemetry off. Anything else, you send us yourself.
- YouTube, ElevenLabs, stock sites and Claude run under your own accounts and keys and their terms
  (for YouTube, YouTube's Terms: https://www.youtube.com/t/terms). Disconnect YouTube: vp youtube
  disconnect. Publishing only after your "ok"; you're responsible for your content.
- Don't use your personal install link within 7 days and your place may go to the next person.
  Age 18+.
- Full terms: alexeykrol.com/elsa/terms/ · privacy: alexeykrol.com/elsa/privacy/ ·
  support@alexeykrol.com"""


# ---- small helpers ------------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None = None) -> str:
    return (t or _now()).isoformat(timespec="microseconds")


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _out(data: dict[str, Any], as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print("\n".join(lines))


def _refused(e: UserInputRequired) -> int:
    print(f"REFUSED {e.message}")
    return RC_USER_INPUT


def consent_path() -> Path:
    return vp_home() / "consent.json"


def guard_dir() -> Path:
    return vp_home() / "guard"


def mode_path() -> Path:
    return vp_home() / "mode.json"


def terms_files() -> dict[str, str]:
    return {"ru": str(PLUGIN_ROOT / "TERMS.ru.md"), "en": str(PLUGIN_ROOT / "TERMS.md")}


# ---- consent ------------------------------------------------------------------------------------

def consent_state() -> dict[str, Any]:
    c = _read(consent_path())
    accepted = c.get("version") == TERMS_VERSION and bool(c.get("accepted_at"))
    return {"accepted": accepted, "current_version": TERMS_VERSION,
            "accepted_version": c.get("version"), "accepted_at": c.get("accepted_at"),
            "later_at": c.get("later_at"), "shown_at": c.get("shown_at"),
            "outdated": bool(c.get("version")) and c.get("version") != TERMS_VERSION,
            "files": terms_files()}


def require_consent(publisher_kind: str | None, what: str = "upload or publish") -> None:
    """Headless gate (owner, 2026-11-19): a real platform (anything but the fake rehearsal
    publisher) is touched only after the person accepted the CURRENT terms — also when a client
    such as ai_lms calls the engine without the dialog. Raises ConsentRequired; nothing is sent."""
    from .core.errors import ConsentRequired
    if (publisher_kind or "youtube") == "fake":
        return
    st = consent_state()
    if st["accepted"]:
        return
    why = (f"the terms were accepted in an older version ({st['accepted_version']})"
           if st["outdated"] else "the terms are not accepted")
    raise ConsentRequired(
        f"{why}; {what} on {publisher_kind or 'youtube'} waits until the person accepts the "
        f"terms {TERMS_VERSION} in the dialog (`vp consent show`, then their own «Принимаю»). "
        f"Nothing was sent. Rehearsals (publisher: fake) are not affected.")


def consent_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vp consent")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "show"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    for name in ("accept", "later", "withdraw"):
        sub.add_parser(name)
    a = p.parse_args(argv)
    path = consent_path()
    c = _read(path)
    if a.cmd == "status":
        st = consent_state()
        _out(st, a.json, [f"terms {TERMS_VERSION}: " + ("accepted " + str(st["accepted_at"])
                                                         if st["accepted"] else "NOT accepted")])
        return 0 if st["accepted"] else RC_NOT_ACCEPTED
    if a.cmd == "show":
        c["shown_at"] = _iso()
        c["shown_version"] = TERMS_VERSION
        _write(path, c)
        data = {"version": TERMS_VERSION, "summary_ru": SUMMARY_RU.format(version=TERMS_VERSION),
                "summary_en": SUMMARY_EN.format(version=TERMS_VERSION), "files": terms_files(),
                "buttons_ru": ["Принимаю", "Прочитать полностью", "Не сейчас"]}
        _out(data, a.json, [data["summary_ru"], "", f"full text: {terms_files()['ru']}",
                            f"English: {terms_files()['en']}"])
        return 0
    if a.cmd == "accept":
        if c.get("shown_version") != TERMS_VERSION:
            print("REFUSED terms-not-shown: run `vp consent show`, show it to the person, ask")
            return RC_USER_INPUT
        try:
            human.require_user_after([("terms", c.get("shown_at"))])
        except UserInputRequired as e:
            return _refused(e)
        c.update({"version": TERMS_VERSION, "accepted_at": _iso()})
        c.pop("later_at", None)
        _write(path, c)
        print(f"ACCEPTED terms {TERMS_VERSION}")
        return 0
    if a.cmd == "later":
        c["later_at"] = _iso()
        _write(path, c)
        print("LATER: nothing will be installed or published until the terms are accepted")
        return 0
    # withdraw
    for k in ("version", "accepted_at"):
        c.pop(k, None)
    c["withdrawn_at"] = _iso()
    _write(path, c)
    print("WITHDRAWN: installs and publishing wait for the terms again")
    return 0


# ---- guard --------------------------------------------------------------------------------------

def guard_enabled() -> tuple[bool, str]:
    if (os.environ.get("AIMC_GUARD") or "").strip().lower() == "off":
        return False, "env AIMC_GUARD=off"
    s = _read(guard_dir() / "settings.json")
    if s.get("enabled") is False:
        return False, f"turned off {s.get('changed_at')}"
    return True, "on (default)"


def command_sha(command: str) -> str:
    return hashlib.sha256(command.strip().encode("utf-8")).hexdigest()


def guard_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vp guard")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").add_argument("--json", action="store_true")
    s = sub.add_parser("approve")
    s.add_argument("id")
    s.add_argument("--verdict", required=True, choices=VERDICTS + ("do-not-install",))
    s.add_argument("--report")
    s.add_argument("--command", dest="command_override")
    s = sub.add_parser("off")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--request", action="store_true")
    g.add_argument("--confirm", action="store_true")
    sub.add_parser("on")
    a = p.parse_args(argv)
    gd = guard_dir()
    if a.cmd == "status":
        on, why = guard_enabled()
        pending = _read(gd / "pending.json").get("items") or {}
        recent = sorted(pending.items(), key=lambda kv: str(kv[1].get("at")), reverse=True)[:5]
        data = {"enabled": on, "why": why,
                "stops": [{"id": k, "what": v.get("what"), "at": v.get("at")} for k, v in recent]}
        _out(data, a.json, [f"install guard: {'ON' if on else 'OFF'} ({why})"]
             + [f"  stop {d['id']}: {d['what']} ({d['at']})" for d in data["stops"]])
        return 0
    if a.cmd == "approve":
        if a.verdict == "do-not-install":
            gd.mkdir(parents=True, exist_ok=True)
            with open(gd / "log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": _iso(), "event": "do-not-install", "id": a.id,
                                    "report": a.report}, ensure_ascii=False) + "\n")
            print(f"REFUSED do-not-install: {a.id} stays stopped; offer the person another way")
            return RC_REFUSED
        item = (_read(gd / "pending.json").get("items") or {}).get(a.id)
        if not isinstance(item, dict):
            print(f"REFUSED unknown-stop {a.id}: run the command once to get a stop id")
            return RC_REFUSED
        if not consent_state()["accepted"]:
            print("REFUSED terms-not-accepted: `vp consent show` first")
            return RC_NOT_ACCEPTED
        try:
            human.require_user_after([(a.id, item.get("at"))])
        except UserInputRequired as e:
            return _refused(e)
        command = (a.command_override or item.get("command") or "").strip()
        approvals = _read(gd / "approvals.json").get("items") or {}
        approvals[command_sha(command)] = {
            "id": a.id, "command": command, "verdict": a.verdict, "report": a.report,
            "approved_at": _iso(), "expires_at": _iso(_now() + APPROVAL_TTL),
            "stopped_command": item.get("command")}
        _write(gd / "approvals.json", {"items": approvals})
        print(f"APPROVED {a.id} ({a.verdict}) for ONE run within 30 minutes of exactly:\n{command}")
        return 0
    s = _read(gd / "settings.json")
    if a.cmd == "on":
        s.update({"enabled": True, "changed_at": _iso()})
        _write(gd / "settings.json", s)
        print("install guard: ON")
        return 0
    if a.request:
        s["off_requested_at"] = _iso()
        _write(gd / "settings.json", s)
        print("ASK the person with buttons whether to turn the install guard off "
              "(«Отключить проверку» / «Оставить включённой»); only their own answer counts")
        return 0
    try:
        human.require_user_after([("guard-off", s.get("off_requested_at"))])
    except UserInputRequired as e:
        return _refused(e)
    s.update({"enabled": False, "changed_at": _iso()})
    s.pop("off_requested_at", None)
    _write(gd / "settings.json", s)
    print("install guard: OFF (`vp guard on` turns it back on)")
    return 0


# ---- permission mode ----------------------------------------------------------------------------

def workspace_default() -> Path:
    return Path.home() / "Movies" / "AI Movie Company"


def allow_rules() -> list[str]:
    """Rules for the plugin's engine in any installed version. Per the docs an allow rule does
    not match past an arbitrary leading `VAR=value` (code.claude.com/docs/en/permissions), so the
    usual `VP_JUDGE=manual … vp` calls rely on the hook's allow; these rules cover plain calls."""
    base = f"{Path.home()}/.claude/plugins/cache/*/ai-movie-company/*/scripts/vp"
    return [f"Bash({base} *)", f'Bash("{base}" *)']


def session_mode() -> dict[str, Any]:
    data = _read(human.runtime_dir() / "session_modes.json")
    own = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    rec = sessions.get(own) if own else None
    source = "this session"
    if not isinstance(rec, dict):
        rec, source = (data.get("latest") if isinstance(data.get("latest"), dict) else {}), "latest"
    return {"mode": rec.get("mode"), "cwd": rec.get("cwd"), "at": rec.get("at"),
            "source": source if rec else "unknown"}


def _within(path: str | None, ws: Path) -> bool:
    if not path:
        return False
    try:
        here, w = Path(path).expanduser().resolve(), ws.expanduser().resolve()
    except OSError:
        return False
    return here == w or w in here.parents


def mode_state() -> dict[str, Any]:
    m = _read(mode_path())
    ses = session_mode()
    ws = Path(m.get("workspace") or workspace_default())
    asks = ses["mode"] in ASKING_MODES if ses["mode"] else None
    offer = bool(asks) and not m.get("enabled") and not m.get("declined_at")
    return {"session": ses, "asks_before_commands": asks, "enabled": bool(m.get("enabled")),
            "workspace": str(ws), "in_workspace": _within(ses.get("cwd"), ws),
            "settings_file": str(ws / ".claude" / "settings.json"),
            "offered_at": m.get("offered_at"), "declined_at": m.get("declined_at"),
            "enabled_at": m.get("enabled_at"), "offer_now": offer}


def mode_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vp mode")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").add_argument("--json", action="store_true")
    sub.add_parser("offer")
    sub.add_parser("enable").add_argument("--workspace")
    sub.add_parser("decline")
    sub.add_parser("undo")
    a = p.parse_args(argv)
    m = _read(mode_path())
    if a.cmd == "status":
        st = mode_state()
        _out(st, a.json, [f"session mode: {st['session']['mode'] or 'unknown'} "
                          f"({'asks before commands' if st['asks_before_commands'] else 'no routine asking' if st['asks_before_commands'] is False else 'not recorded yet'})",
                          f"fewer questions in {st['workspace']}: "
                          f"{'on' if st['enabled'] else 'off'}"])
        return 0
    if a.cmd == "offer":
        m["offered_at"] = _iso()
        _write(mode_path(), m)
        print("ASK the person now (buttons); `vp mode enable` needs their own answer after this")
        return 0
    if a.cmd == "decline":
        m["declined_at"] = _iso()
        _write(mode_path(), m)
        print("DECLINED: the mode stays as it is; not offered again unless the person asks")
        return 0
    if a.cmd == "undo":
        return _undo(m)
    try:
        human.require_user_after([("mode", m.get("offered_at"))])
    except UserInputRequired as e:
        return _refused(e)
    ws = Path(a.workspace).expanduser() if a.workspace else workspace_default()
    if m.get("enabled"):
        _undo(m, quiet=True)
        m = _read(mode_path())
    settings = ws / ".claude" / "settings.json"
    existed = settings.exists()
    before = settings.read_text(encoding="utf-8") if existed else None
    try:
        doc = json.loads(before) if before else {}
        if not isinstance(doc, dict):
            raise ValueError
    except ValueError:
        print(f"REFUSED unreadable-settings {settings}: not touched")
        return RC_REFUSED
    perms = doc.setdefault("permissions", {})
    if not isinstance(perms, dict):
        print(f"REFUSED unreadable-settings {settings}: not touched")
        return RC_REFUSED
    perms.setdefault("defaultMode", "acceptEdits")
    allow = perms.setdefault("allow", [])
    for r in allow_rules():
        if r not in allow:
            allow.append(r)
    created_dir = not settings.parent.exists()
    ws.mkdir(parents=True, exist_ok=True)
    _write(settings, doc)
    m.update({"enabled": True, "workspace": str(ws), "settings_file": str(settings),
              "existed_before": existed, "backup": before, "created_claude_dir": created_dir,
              "enabled_at": _iso()})
    m.pop("declined_at", None)
    _write(mode_path(), m)
    print(f"ENABLED fewer questions in {ws}: {settings} (defaultMode acceptEdits + the engine's "
          f"allow rules); global settings untouched. It applies to sessions started in that "
          f"folder; `vp mode undo` puts it back.")
    return 0


def _undo(m: dict[str, Any], quiet: bool = False) -> int:
    if not m.get("enabled") or not m.get("settings_file"):
        if not quiet:
            print("NOTHING to undo: fewer questions was not on")
        return 0
    settings = Path(m["settings_file"])
    if m.get("existed_before") and m.get("backup") is not None:
        settings.write_text(m["backup"], encoding="utf-8")
    else:
        try:
            settings.unlink()
        except FileNotFoundError:
            pass
        if m.get("created_claude_dir"):
            try:
                settings.parent.rmdir()
            except OSError:
                pass
    for k in ("enabled", "backup", "existed_before", "created_claude_dir", "enabled_at"):
        m.pop(k, None)
    m["undone_at"] = _iso()
    _write(mode_path(), m)
    if not quiet:
        print(f"UNDONE: {settings} is back as it was; the session mode is yours to choose")
    return 0
