"""Desktop apps the person would otherwise have to quit by hand: Claude and CapCut.

Why: a person told "quit Claude (Cmd+Q)" presses it in whatever app is in front — after a browser
page that is the browser (field test 2026-11-20, docs/research/ux-audit-hand-steps-2026-11-20.md,
fixes #2, #3, #6). Elsa never asks for a key combination; the engine does it on a button press.

How an app is quit (no macOS "Automation" prompt):
  * `NSRunningApplication.terminate` through the JavaScript-for-Automation ObjC bridge
    (`osascript -l JavaScript`, AppKit in-process). It is the same polite quit as Dock → Quit: the
    app gets its normal quit event and may still ask "save?". It does NOT script the target app
    (no `tell application ... to quit`), so no Apple Events consent is asked; checked on TextEdit
    2026-11-20 — quit at once, no dialog. `tell application "X" to quit` is avoided on purpose: it
    asks the person for the Automation permission ("… wants to control …").
  * Claude only, when the polite quit did not end it in time: SIGTERM to its main process (found by
    the executable path inside the bundle) — Chromium/Electron treat SIGTERM as a normal quit.
  * Never SIGKILL: CapCut rewrites its project list on exit and may lose it.

Detection is by bundle: the process whose executable lies inside <App>.app/Contents/MacOS or
Contents/Frameworks (helpers die with the app). Contents/Helpers is not counted — e.g. Claude's
chrome-native-host belongs to Chrome's process tree. The bundle id and version come from the
app's Info.plist at run time.

restart-claude: refused while `vp busy` says a render/upload/generation runs. Otherwise a helper
is spawned fully detached (new session, its own log in <VP_HOME>/logs/) so it survives the quit of
the Claude process tree that started it; the command prints RESTARTING and returns at once. The
helper waits a moment (the chat shows the answer), quits Claude politely, waits until it is gone,
then `open -b <bundle id>`. A marker <VP_HOME>/restart-pending.json tells the next session the
restart was on purpose (the mod shows its "you are back" splash and clears it).

CLI:
  vp app restart-claude [--reason R] [--project P] [--delay S] [--dry-run] [--json]
  vp app quit capcut [--timeout S] [--json]       QUIT | NOT_RUNNING | TIMEOUT
  vp app status [--json]
  vp app clear-restart                            remove the restart-pending marker
"""
from __future__ import annotations

import argparse
from urllib.parse import quote
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MARKER_NAME = "restart-pending.json"
EXIT_BUSY = 2


@dataclass(frozen=True)
class AppSpec:
    key: str
    name: str                       # for `open -a` and messages
    bundle_id: str                  # fallback when Info.plist cannot be read
    paths: tuple[Path, ...]
    env: str = ""                   # env var overriding the app location


CLAUDE = AppSpec("claude", "Claude", "com.anthropic.claudefordesktop",
                 (Path("/Applications/Claude.app"), Path.home() / "Applications" / "Claude.app"),
                 "VP_CLAUDE_APP")
CAPCUT = AppSpec("capcut", "CapCut", "com.lemon.lvoverseas",
                 (Path("/Applications/CapCut.app"), Path.home() / "Applications" / "CapCut.app"),
                 "VP_CAPCUT_APP")
APPS = {"claude": CLAUDE, "capcut": CAPCUT}


# ---- injectable system calls (tests replace these) -----------------------------------------

@dataclass
class System:
    """Everything that touches the machine. Tests pass a fake."""
    run: Callable[..., subprocess.CompletedProcess] = field(default=subprocess.run)
    popen: Callable[..., Any] = field(default=subprocess.Popen)
    kill: Callable[[int, int], None] = field(default=os.kill)
    sleep: Callable[[float], None] = field(default=time.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)

    def processes(self) -> list[tuple[int, str]]:
        """(pid, full command line) of every process; [] when ps fails."""
        try:
            p = self.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True,
                         timeout=10)
        except (OSError, subprocess.SubprocessError):
            return []
        out = []
        for line in (p.stdout or "").splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if pid.isdigit():
                out.append((int(pid), cmd.strip()))
        return out


# ---- app facts ------------------------------------------------------------------------------

def app_path(spec: AppSpec) -> Path:
    env = os.environ.get(spec.env) if spec.env else None
    cands = (Path(env).expanduser(),) if env else spec.paths
    for p in cands:
        if (p / "Contents" / "Info.plist").is_file():
            return p
    return cands[0]


def info_plist(path: Path) -> dict[str, Any]:
    try:
        with open(path / "Contents" / "Info.plist", "rb") as f:
            d = plistlib.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}


@dataclass
class AppInfo:
    spec: AppSpec
    path: Path
    installed: bool
    bundle_id: str
    executable: str
    version: str | None

    @property
    def main_exe(self) -> str:
        return str(self.path / "Contents" / "MacOS" / self.executable)


def app_info(spec: AppSpec) -> AppInfo:
    path = app_path(spec)
    pl = info_plist(path)
    return AppInfo(spec=spec, path=path, installed=bool(pl),
                   bundle_id=str(pl.get("CFBundleIdentifier") or spec.bundle_id),
                   executable=str(pl.get("CFBundleExecutable") or spec.name),
                   version=(str(pl.get("CFBundleShortVersionString") or "") or None))


def app_pids(info: AppInfo, sysm: System) -> dict[str, list[int]]:
    """{'main': [...], 'all': [...]} — processes of this app bundle (MacOS/ + Frameworks/)."""
    root = str(info.path) + "/Contents/"
    main, every = [], []
    for pid, cmd in sysm.processes():
        if cmd == info.main_exe or cmd.startswith(info.main_exe + " "):
            main.append(pid)
            every.append(pid)
        elif cmd.startswith(root + "MacOS/") or cmd.startswith(root + "Frameworks/"):
            every.append(pid)
    return {"main": main, "all": every}


def is_running(info: AppInfo, sysm: System) -> bool:
    return bool(app_pids(info, sysm)["all"])


def polite_quit(info: AppInfo, sysm: System) -> bool:
    """Ask the app to quit (NSRunningApplication.terminate — no Automation prompt). True if sent."""
    js = ('ObjC.import("AppKit");'
          f'var a=$.NSRunningApplication.runningApplicationsWithBundleIdentifier({json.dumps(info.bundle_id)});'
          'var n=0;for(var i=0;i<a.count;i++){if(a.objectAtIndex(i).terminate)n++};String(n)')
    try:
        p = sysm.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True,
                     timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and (p.stdout or "").strip() not in ("", "0")


def wait_gone(info: AppInfo, sysm: System, timeout: float, step: float = 0.5) -> bool:
    end = sysm.clock() + timeout
    while True:
        if not is_running(info, sysm):
            return True
        if sysm.clock() >= end:
            return False
        sysm.sleep(step)


def sigterm(pids: list[int], sysm: System) -> list[int]:
    sent = []
    for pid in pids:
        try:
            sysm.kill(pid, signal.SIGTERM)
            sent.append(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return sent


def open_cmd(info: AppInfo) -> list[str]:
    return ["open", "-b", info.bundle_id] if info.installed else ["open", "-a", info.spec.name]


# ---- marker ---------------------------------------------------------------------------------

def _home() -> Path:
    from .core.paths import vp_home
    return vp_home()


def marker_path() -> Path:
    return _home() / MARKER_NAME


def read_marker() -> dict[str, Any] | None:
    try:
        d = json.loads(marker_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def write_marker(rec: dict[str, Any]) -> Path:
    p = marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return p


def _update_marker(**kw: Any) -> None:
    rec = read_marker() or {}
    rec.update(kw)
    try:
        write_marker(rec)
    except OSError:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- restart-claude -------------------------------------------------------------------------

def _busy_state() -> dict[str, Any]:
    from .busy import state
    return state()


def helper_argv(delay: float, timeout: float, open_session: str | None = None) -> list[str]:
    argv = [sys.executable, "-m", "vp.app", "--helper", "--delay", str(delay),
            "--timeout", str(timeout)]
    return argv + (["--open-session", open_session] if open_session else [])


def session_link(text: str) -> str:
    """claude://code/new?q=<text>: Claude Desktop opens a new Code session with the text typed in;
    nothing is sent until the person presses Enter."""
    return "claude://code/new?q=" + quote(text, safe="")


def restart_plan(sysm: System, delay: float, timeout: float,
                 open_session: str | None = None) -> dict[str, Any]:
    info = app_info(CLAUDE)
    pids = app_pids(info, sysm)
    return {"app": str(info.path), "installed": info.installed, "bundle_id": info.bundle_id,
            "version": info.version, "main_pids": pids["main"], "pids": len(pids["all"]),
            "delay_s": delay, "timeout_s": timeout,
            "quit": "NSRunningApplication.terminate (osascript -l JavaScript); "
                    "SIGTERM to main pid if still running after the timeout; never SIGKILL",
            "reopen": open_cmd(info), "helper": helper_argv(delay, timeout, open_session),
            "then_open": session_link(open_session) if open_session else None,
            "marker": str(marker_path()), "log": str(_home() / "logs" / "app-restart.log")}


def spawn_helper(sysm: System, delay: float, timeout: float,
                 open_session: str | None = None) -> Any:
    """Start the detached helper: own session, no tty, output to the log. Returns the Popen."""
    logs = _home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = open(logs / "app-restart.log", "ab")
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        return sysm.popen(helper_argv(delay, timeout, open_session), stdin=subprocess.DEVNULL, stdout=log,
                          stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                          cwd=str(_home()), env=env)
    finally:
        log.close()


def restart_claude(sysm: System, delay: float = 2.0, timeout: float = 30.0, dry_run: bool = False,
                   reason: str = "", project: str | None = None,
                   open_session: str | None = None) -> tuple[int, dict[str, Any]]:
    busy = _busy_state()
    if busy.get("busy"):
        return EXIT_BUSY, {"result": "BUSY", "what": busy.get("what", []),
                           "jobs": busy.get("jobs", [])}
    plan = restart_plan(sysm, delay, timeout, open_session)
    if dry_run:
        return 0, {"result": "DRY_RUN", **plan}
    write_marker({"reason": reason or "restart", "at": _now(), "project": project,
                  "state": "pending", "pid_before": (plan["main_pids"] or [None])[0],
                  "version_before": plan["version"]})
    proc = spawn_helper(sysm, delay, timeout, open_session)
    return 0, {"result": "RESTARTING", "helper_pid": getattr(proc, "pid", None),
               "marker": plan["marker"], "log": plan["log"]}


def helper(sysm: System, delay: float, timeout: float, open_session: str | None = None) -> int:
    """Runs detached: quit Claude, wait, open it again. Logs to stdout (the log file)."""
    def log(msg: str) -> None:
        print(f"{_now()} {msg}", flush=True)
    sysm.sleep(delay)
    info = app_info(CLAUDE)
    log(f"restart: {info.path} bundle={info.bundle_id}")
    gone = True
    if is_running(info, sysm):
        log(f"polite quit sent={polite_quit(info, sysm)}")
        gone = wait_gone(info, sysm, timeout)
        if not gone:
            sent = sigterm(app_pids(info, sysm)["main"], sysm)
            log(f"still running after {timeout}s; SIGTERM {sent}")
            gone = wait_gone(info, sysm, 10.0)
    _update_marker(state="quit" if gone else "quit_timeout", quit_at=_now())
    if not gone:
        log("Claude did not quit; opening it to bring the window back")
    cmd = open_cmd(info)
    try:
        rc = sysm.run(cmd, capture_output=True, text=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError) as e:
        log(f"open failed: {e.__class__.__name__}")
        _update_marker(state="open_failed")
        return 1
    log(f"{' '.join(cmd)} -> {rc}")
    _update_marker(state=("reopened" if gone else "not_quit") if rc == 0 else "open_failed",
                   reopened_at=_now())
    if rc == 0 and open_session:
        sysm.sleep(5.0)                        # let the window come up before the deep link
        try:
            rc2 = sysm.run(["open", session_link(open_session)], capture_output=True, text=True,
                           timeout=30).returncode
            log(f"open new session -> {rc2}")
        except (OSError, subprocess.SubprocessError) as e:
            log(f"open new session failed: {e.__class__.__name__}")
    return 0 if rc == 0 and gone else 1


# ---- quit capcut ----------------------------------------------------------------------------

def quit_app(spec: AppSpec, sysm: System, timeout: float = 30.0) -> dict[str, Any]:
    info = app_info(spec)
    if not is_running(info, sysm):
        return {"result": "NOT_RUNNING", "app": spec.name}
    sent = polite_quit(info, sysm)
    gone = wait_gone(info, sysm, timeout)
    return {"result": "QUIT" if gone else "TIMEOUT", "app": spec.name, "asked": sent,
            **({} if gone else {"hint": f"{spec.name} may be asking something on screen "
                                        "(e.g. save?)"})}


# ---- status ---------------------------------------------------------------------------------

def status(sysm: System) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, spec in APPS.items():
        info = app_info(spec)
        out[key] = {"installed": info.installed, "running": is_running(info, sysm),
                    "bundle_id": info.bundle_id, "version": info.version}
    out["restart_pending"] = read_marker()
    return out


# ---- CLI ------------------------------------------------------------------------------------

def _emit(rec: dict[str, Any], as_json: bool, line: str) -> None:
    print(json.dumps(rec, ensure_ascii=False) if as_json else line)


def main(argv: list[str], sysm: System | None = None) -> int:
    sysm = sysm or System()
    ap = argparse.ArgumentParser(prog="vp app", description="quit / restart Claude and CapCut")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restart-claude")
    r.add_argument("--delay", type=float, default=2.0)
    r.add_argument("--timeout", type=float, default=30.0)
    r.add_argument("--reason", default="")
    r.add_argument("--project")
    r.add_argument("--open-session", help="after the reopen, a new Code session with this text typed in")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--json", action="store_true")
    q = sub.add_parser("quit")
    q.add_argument("app", choices=["capcut"])
    q.add_argument("--timeout", type=float, default=30.0)
    q.add_argument("--json", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    sub.add_parser("clear-restart")
    a = ap.parse_args(argv)

    if a.cmd == "restart-claude":
        rc, rec = restart_claude(sysm, a.delay, a.timeout, a.dry_run, a.reason, a.project,
                                  a.open_session)
        if rec["result"] == "BUSY":
            line = "BUSY: " + (", ".join(str(w) for w in rec["what"]) or "a long step is running")
        elif rec["result"] == "DRY_RUN":
            line = "\n".join(["DRY_RUN"] + [f"  {k}: {v}" for k, v in rec.items() if k != "result"])
        else:
            line = "RESTARTING"
        _emit(rec, a.json, line)
        return rc
    if a.cmd == "quit":
        rec = quit_app(APPS[a.app], sysm, a.timeout)
        _emit(rec, a.json, rec["result"])
        return 0 if rec["result"] in ("QUIT", "NOT_RUNNING") else 1
    if a.cmd == "status":
        rec = status(sysm)
        line = "\n".join(f"{k}: {'running' if v['running'] else 'not running'}"
                         f"{' ' + v['version'] if v.get('version') else ''}"
                         for k, v in rec.items() if k != "restart_pending")
        line += f"\nrestart pending: {'yes' if rec['restart_pending'] else 'no'}"
        _emit(rec, a.json, line)
        return 0
    if a.cmd == "clear-restart":
        try:
            marker_path().unlink()
        except FileNotFoundError:
            pass
        print("CLEARED")
        return 0
    return 2


def _helper_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp.app --helper")
    ap.add_argument("--helper", action="store_true")
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--open-session")
    a = ap.parse_args(argv)
    return helper(System(), a.delay, a.timeout, a.open_session)


if __name__ == "__main__":  # the detached helper: python -m vp.app --helper ...
    raise SystemExit(_helper_main(sys.argv[1:]))
