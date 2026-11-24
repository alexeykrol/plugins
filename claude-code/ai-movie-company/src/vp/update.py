"""Updates of the plugin: the auto-update switch, the reminder schedule, "a newer version is here".

Facts (Claude Code docs, checked by the owner's session 2026-11-20): a third-party marketplace does
not auto-update by default; the switch is `"autoUpdate": true` on the marketplace's entry under
`extraKnownMarketplaces` in ~/.claude/settings.json. An update downloads in the background; a
running session keeps the old version (its folder is kept 14 days); the new one loads on the next
session; quitting Claude kills running background renders — so the "restart" line is shown only
while the engine is idle (vp.busy).

`vp update auto on|off` writes ONLY `extraKnownMarketplaces.<our marketplace>.autoUpdate` (and the
entry's `source` when the entry does not exist yet, copied from Claude's known_marketplaces.json):
the file is parsed as JSON and merged, a backup `settings.json.aimc-backup-<UTC>` is written first,
an unreadable settings file is never overwritten. `on` only after the person's own yes.

Our marketplace: the folder this code runs from (~/.claude/plugins/cache/<marketplace>/
ai-movie-company/<version>/), else env AIMC_MARKETPLACE, else the first of krol /
alexeykrol-private Claude knows, else krol.

Reminder schedule after «Не сейчас» (or `auto off`): REMINDER_DAYS after the refusal — days 2, 4,
6, then 3 and 5 days later (9, 14) — then never; a missed one is shown at the next chance, but
never two within MIN_GAP. State: <VP_HOME>/update.json.

CLI:  vp update auto status|on|off [--json] [--marketplace M]
      vp update decline                     the person said «Не сейчас» to the offer
      vp update reminder due|shown [--json]  due = time has come AND the engine is idle
      vp update check [--json]              running version vs the newest installed on disk; both
                                            with their release line (vp.buildinfo: stage, build,
                                            release time)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PLUGIN = "ai-movie-company"
OUR_MARKETPLACES = ("krol", "alexeykrol-private")
DEFAULT_SOURCES = {"krol": {"source": "github", "repo": "alexeykrol/plugins"}}
REMINDER_DAYS = (2, 4, 6, 9, 14)
MIN_GAP = timedelta(hours=20)


class SettingsError(Exception):
    pass


# ---- where things are ---------------------------------------------------------------------------

def claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def settings_path() -> Path:
    env = os.environ.get("AIMC_CLAUDE_SETTINGS")
    return Path(env).expanduser() if env else claude_dir() / "settings.json"


def known_marketplaces() -> dict[str, Any]:
    try:
        data = json.loads((claude_dir() / "plugins" / "known_marketplaces.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _running_from() -> tuple[str | None, Path | None]:
    """(marketplace, the plugin's folder in Claude's cache) when this code runs from there."""
    parts = Path(__file__).resolve().parts
    for i in range(len(parts) - 3):
        if parts[i] == "cache" and parts[i + 2] == PLUGIN:
            return parts[i + 1], Path(*parts[:i + 3])
    return None, None


def marketplace() -> str:
    mkt, _ = _running_from()
    if mkt:
        return mkt
    env = os.environ.get("AIMC_MARKETPLACE")
    if env:
        return env
    known = known_marketplaces()
    for m in OUR_MARKETPLACES:
        if m in known:
            return m
    return OUR_MARKETPLACES[0]


# ---- the settings switch -------------------------------------------------------------------------

def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SettingsError(f"{path} is not readable JSON ({type(e).__name__}); not touched") from e
    if not isinstance(data, dict):
        raise SettingsError(f"{path} is not a JSON object; not touched")
    return data


def auto_status(mkt: str | None = None) -> dict[str, Any]:
    mkt = mkt or marketplace()
    path = settings_path()
    st = _state()
    try:
        data = _load_settings(path)
        entry = (data.get("extraKnownMarketplaces") or {}).get(mkt)
        error = None
    except SettingsError as e:
        entry, error = None, str(e)
    auto = bool(isinstance(entry, dict) and entry.get("autoUpdate") is True)
    return {"marketplace": mkt, "auto_update": auto, "entry_exists": isinstance(entry, dict),
            "settings": str(path), "offered": bool(st.get("declined_at") or st.get("enabled_at")),
            "declined_at": st.get("declined_at"), "error": error}


def set_auto(on: bool, mkt: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Write extraKnownMarketplaces.<mkt>.autoUpdate; returns {"changed", "backup", ...}."""
    mkt = mkt or marketplace()
    path = settings_path()
    data = _load_settings(path)
    extra = data.get("extraKnownMarketplaces")
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise SettingsError(f"extraKnownMarketplaces in {path} is not an object; not touched")
    entry = extra.get(mkt)
    if entry is not None and not isinstance(entry, dict):
        raise SettingsError(f"extraKnownMarketplaces.{mkt} in {path} is not an object; not touched")
    if entry is None:
        if not on:              # off and no entry: already off, nothing to write
            _record_choice(on, now)
            return {"changed": False, "backup": None, **auto_status(mkt)}
        known = known_marketplaces().get(mkt)
        source = known.get("source") if isinstance(known, dict) and known.get("source") else None
        if isinstance(known, dict) and isinstance(source, str):
            source = {k: v for k, v in known.items() if k in ("source", "repo", "url", "path", "ref")}
        source = source or DEFAULT_SOURCES.get(mkt)
        if not source:
            raise SettingsError(f"the source of marketplace {mkt} is unknown; not touched")
        entry = {"source": source}
    if entry.get("autoUpdate") is on or (not on and "autoUpdate" not in entry):
        _record_choice(on, now)
        return {"changed": False, "backup": None, **auto_status(mkt)}
    entry = dict(entry)
    entry["autoUpdate"] = on
    new = dict(data)
    new["extraKnownMarketplaces"] = {**extra, mkt: entry}
    backup = None
    if path.exists():
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.aimc-backup-{stamp}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(new, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    _record_choice(on, now)
    from . import telemetry
    telemetry.record("auto_update", on=on, marketplace=mkt)
    return {"changed": True, "backup": str(backup) if backup else None, **auto_status(mkt)}


# ---- the reminder schedule -----------------------------------------------------------------------

def _state_path() -> Path:
    from .core.paths import vp_home
    return vp_home() / "update.json"


def _state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".update.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _record_choice(on: bool, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    st = _state()
    if on:
        st.update({"enabled_at": _iso(now), "declined_at": None, "reminders": []})
    else:
        st.update({"declined_at": _iso(now), "reminders": []})
    _save(st)


def decline(now: datetime | None = None) -> None:
    _record_choice(False, now)


def reminder_due(now: datetime | None = None, check_busy: bool = True,
                 auto_on: bool | None = None) -> dict[str, Any]:
    """Is a reminder to switch auto-update on due now? The schedule, then the idle rule."""
    now = now or datetime.now(timezone.utc)
    st = _state()
    if auto_on is None:
        auto_on = auto_status()["auto_update"]
    if auto_on:
        return {"due": False, "reason": "auto-update is on"}
    if not st.get("declined_at"):
        return {"due": False, "reason": "not offered yet"}
    n = len(st.get("reminders") or [])
    if n >= len(REMINDER_DAYS):
        return {"due": False, "reason": "schedule finished"}
    when = datetime.fromisoformat(st["declined_at"]) + timedelta(days=REMINDER_DAYS[n])
    if n:   # a missed day is caught up, but never two reminders within MIN_GAP
        when = max(when, datetime.fromisoformat(st["reminders"][-1]) + MIN_GAP)
    if now < when:
        return {"due": False, "reason": "not yet", "next": _iso(when), "number": n + 1}
    if check_busy:
        from .busy import state as busy_state
        if busy_state()["busy"]:
            return {"due": False, "reason": "busy", "number": n + 1}
    return {"due": True, "reason": "time", "number": n + 1, "of": len(REMINDER_DAYS)}


def reminder_shown(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    st = _state()
    st["reminders"] = list(st.get("reminders") or []) + [_iso(now)]
    _save(st)
    from . import telemetry
    telemetry.record("update_reminder_shown", number=len(st["reminders"]))
    return len(st["reminders"])


# ---- a newer version on disk ---------------------------------------------------------------------

def _vkey(v: str) -> tuple[int, ...] | None:
    from .messages import _vtuple
    return _vtuple(v)


def check(plugin_root: Path | None = None, running: str | None = None) -> dict[str, Any]:
    """The running version against the newest folder of the plugin in Claude's cache. A newer one
    there = downloaded by an update, loads on the next start of Claude.

    `running` / `installed_newest` stay plain X.Y.Z (they are compared and kept as keys); the line a
    person is shown is `running_label` / `installed_newest_label` ("0.9.0 alpha · build 58 ·
    2026-11-24 10:00 UTC"), and `installed_newest_build` carries its parts for the mod, which words
    the line in the person's language."""
    from dataclasses import replace

    from . import buildinfo
    from .telemetry import plugin_version
    mine = buildinfo.read()
    running = running or plugin_version()
    if plugin_root is None:
        _, plugin_root = _running_from()
    newest = None
    if plugin_root is not None and plugin_root.is_dir():
        versions = [(k, d.name) for d in plugin_root.iterdir()
                    if d.is_dir() and (d / ".claude-plugin" / "plugin.json").is_file()
                    and (k := _vkey(d.name)) is not None]
        if versions:
            newest = max(versions)[1]
    rk, nk = _vkey(running), _vkey(newest) if newest else None
    # the folder's name is the version Claude loads; its build.json says the rest
    theirs = replace(buildinfo.read(plugin_root / newest), version=newest) \
        if plugin_root is not None and newest else None
    return {"running": running, "installed_newest": newest,
            "restart_needed": bool(rk and nk and nk > rk),
            "running_label": buildinfo.label(replace(mine, version=running)),
            "installed_newest_label": buildinfo.label(theirs) if theirs else None,
            "installed_newest_build": {"stage": theirs.stage, "build": theirs.build,
                                       "released": theirs.released} if theirs else None}


# ---- CLI ------------------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp update", description="plugin updates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ = sub.add_parser("auto")
    a_.add_argument("action", choices=("status", "on", "off"))
    a_.add_argument("--json", action="store_true")
    a_.add_argument("--marketplace")
    sub.add_parser("decline")
    r = sub.add_parser("reminder")
    r.add_argument("action", choices=("due", "shown"))
    r.add_argument("--json", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "auto":
        try:
            res = auto_status(a.marketplace) if a.action == "status" \
                else set_auto(a.action == "on", a.marketplace)
        except SettingsError as e:
            print(json.dumps({"error": str(e)}) if a.json else f"ERROR {e}")
            return 2
        if a.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print(f"auto-update for {res['marketplace']}: {'on' if res['auto_update'] else 'off'}"
                  + (f" (backup {res['backup']})" if res.get("backup") else ""))
        return 0
    if a.cmd == "decline":
        decline()
        print("DECLINED auto-update; reminders on days " + ", ".join(map(str, REMINDER_DAYS)))
        return 0
    if a.cmd == "reminder":
        if a.action == "shown":
            n = reminder_shown()
            print(json.dumps({"shown": n}) if a.json else f"reminder {n} recorded")
            return 0
        res = reminder_due()
        print(json.dumps(res) if a.json else ("due" if res["due"] else f"not due: {res['reason']}"))
        return 0
    res = check()
    print(json.dumps(res) if a.json else
          (f"restart needed: {res['running_label']} -> {res['installed_newest_label']}"
           if res["restart_needed"] else f"running {res['running_label']}, nothing newer installed"))
    return 0
