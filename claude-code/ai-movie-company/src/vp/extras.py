"""`vp extras` — the add-ons for the design look (animated captions, drawn cards, cover).

Since 0.8 (K25) the installer puts them in by default: one prompt = everything. The base pipeline
needs none of them; if a step fails the videos keep the plain look, `vp doctor` says what is
missing, and the director retries with `vp extras install` (resumable: done steps are skipped).

  vp extras status [--json]                what is in place (plain lines; JSON with --json)
  vp extras install [--json] [--job <job>] Node + Chrome Headless Shell + the Remotion workspace
                                           + the Remotion plugin for Claude
  vp extras check --event first-run|job-start|feature
                                           should the director offer the add-ons now? (JSON; records
                                           the reminder when it says yes — the cadence is code's)
  vp extras decide later                   the owner postponed it

The steps (pinned; https; sha256 checked before anything is unpacked; no sudo, no Homebrew;
nothing outside <vp_home>/extras, <vp_home>/remotion and Claude's own plugin folder):
  node      Node.js 24.22.2 (nodejs.org, SHASUMS256; released 2026-10-27)  ~28 MB, ~200 MB on disk
  browser   Chrome Headless Shell 158.0.8127.2 (Google, Chrome for Testing — the version Remotion
            4.0.540 is tested with; Google publishes no checksum file, the pin = sha256 of the file
            whose MD5 matched Google's x-goog-hash, 2026-11-19)              ~105 MB, ~205 MB on disk
  workspace the plugin's Remotion project: `npm ci --ignore-scripts` from the committed lockfile
            (every pin >= 14 days old: scripts/check_lock_age.py)            ~270 MB on disk
  plugin    the Remotion plugin for Claude (skills only, no code), from the Anthropic Directory
            built into Claude Code: `claude plugin install remotion@anthropic-plugin-directory`
            (the directory pins Remotion's repository to a reviewed commit; works without a
            claude.ai account). Any installed `remotion@…` counts (e.g. `remotion@synced`).

Last line of `install` (for the installer script): `EXTRAS: OK` or `EXTRAS: PARTIAL <step,…>`.
When the Remotion plugin was newly installed, a line `RESTART NEEDED: …` comes before it (and
`restart_needed: true` in --json): Claude must be quit (Cmd+Q) and opened again for it to load.
Exit 0 when every step is in place, 1 otherwise. Secrets are never read or printed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .core.errors import VPError
from .core.io import read_yaml, write_yaml
from .core.paths import vp_home

REMIND_EVERY_S = 3 * 24 * 3600          # job-start reminders after "later": at most once per 3 days
OK_MARK = ".vp-ok"
EVENTS = ("first-run", "job-start", "feature")
STEPS = ("node", "browser", "workspace", "plugin")
CLAUDE_PLUGIN = "remotion@anthropic-plugin-directory"
CLAUDE_PLUGIN_NAME = "remotion"
CLAUDE_TIMEOUT_S = 180
FIX_RU = "скажите Эльзе: «установи дополнения»"
FIX_EN = 'tell Elsa: "install the add-ons"'
RESTART_LINE = ("RESTART NEEDED: the Remotion plugin for Claude turns on after Claude is quit "
                "completely (Cmd+Q) and opened again / нужен перезапуск: закройте Claude "
                "полностью (Cmd+Q) и откройте снова")


@dataclass(frozen=True)
class Component:
    key: str          # node | browser
    title: str        # plain words for the owner
    version: str
    url: str
    sha256: str
    size: int         # bytes of the download
    archive: str      # tar.xz | zip
    top: str          # the folder inside the archive
    exe: str          # the executable, relative to the installed folder
    source: str       # where it comes from, in plain words

    @property
    def folder(self) -> str:
        return f"{self.key}-{self.version}"


_NODE_SRC = "nodejs.org (the official Node.js site)"
_SHELL_SRC = "Google (Chrome for Testing — a Chrome without windows, made for drawing video)"
_CFT = "https://storage.googleapis.com/chrome-for-testing-public/158.0.8127.2"
PINS: dict[str, tuple[Component, Component]] = {
    "arm64": (
        Component(key="node", title="Node.js", version="24.22.2",
                  url="https://nodejs.org/dist/v24.22.2/node-v24.22.2-darwin-arm64.tar.xz",
                  sha256="1f5ac075be50bed4780ea3be19a007fda95ea4d527d28d76d0b91de47d915115",
                  size=27_508_812, archive="tar.xz", top="node-v24.22.2-darwin-arm64", exe="bin/node",
                  source=_NODE_SRC),
        Component(key="browser", title="Chrome Headless Shell", version="158.0.8127.2",
                  url=f"{_CFT}/mac-arm64/chrome-headless-shell-mac-arm64.zip",
                  sha256="8268f070e7c443b0b1d2147f3cfc7a617e6550aa95715c7b985e9f32491a3bf9",
                  size=105_056_164, archive="zip", top="chrome-headless-shell-mac-arm64",
                  exe="chrome-headless-shell", source=_SHELL_SRC)),
    "x86_64": (
        Component(key="node", title="Node.js", version="24.22.2",
                  url="https://nodejs.org/dist/v24.22.2/node-v24.22.2-darwin-x64.tar.xz",
                  sha256="6965578fd823e72c1b5387f5ab84cb25368f886e02eb8779e6f0fce6b91c27b7",
                  size=29_283_332, archive="tar.xz", top="node-v24.22.2-darwin-x64", exe="bin/node",
                  source=_NODE_SRC),
        Component(key="browser", title="Chrome Headless Shell", version="158.0.8127.2",
                  url=f"{_CFT}/mac-x64/chrome-headless-shell-mac-x64.zip",
                  sha256="fc75411f42e693cc8f380b840a3d29678c5dafd92a94945d0c6fb1a5d9697ec5",
                  size=112_123_268, archive="zip", top="chrome-headless-shell-mac-x64",
                  exe="chrome-headless-shell", source=_SHELL_SRC)),
}
ARCH = platform.machine()
NODE, BROWSER = PINS.get(ARCH, PINS["arm64"])
COMPONENTS = (NODE, BROWSER)
DOWNLOAD_MB = round(sum(c.size for c in COMPONENTS) / 1e6)      # ~133
DISK_MB = 700                    # measured 2026-11-19: node 199 + shell 205 + workspace 266 MB


# ---- where things live ------------------------------------------------------------------------

def extras_dir(home: str | os.PathLike | None = None) -> Path:
    return Path(home or vp_home()).expanduser() / "extras"


def installed_exe(comp: Component, home: str | os.PathLike | None = None) -> Path | None:
    d = extras_dir(home) / comp.folder
    exe = d / comp.exe
    return exe if (d / OK_MARK).is_file() and exe.is_file() else None


def node_path(home: str | os.PathLike | None = None) -> Path | None:
    return installed_exe(NODE, home)


def browser_path(home: str | os.PathLike | None = None) -> Path | None:
    return installed_exe(BROWSER, home)


def platform_ok() -> tuple[bool, str | None]:
    if sys.platform == "darwin" and platform.machine() in PINS:
        return True, None
    return False, ("the add-ons are made for Macs for now; on this computer the videos keep the "
                   "plain look")


# ---- the Remotion plugin for Claude -----------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_NET_WORDS = ("enotfound", "eai_again", "etimedout", "econnreset", "econnrefused", "getaddrinfo",
              "network", "timed out", "timeout", "could not resolve", "fetch failed", "socket hang up",
              "unable to access", "connection")


def classify(text: str) -> str:
    """network | other — so the owner hears "the internet dropped" rather than a stack trace."""
    t = (text or "").lower()
    return "network" if any(w in t for w in _NET_WORDS) else "other"


def find_claude() -> str | None:
    """The Claude command line: PATH, then the official installer's place (~/.local/bin)."""
    w = shutil.which("claude")
    if w:
        return w
    p = Path.home() / ".local" / "bin" / "claude"
    return str(p) if p.is_file() else None


def _claude_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = str(Path.home() / ".local" / "bin") + os.pathsep + env.get("PATH", "")
    return env


def parse_plugin_list(out: str) -> str | None:
    """The id of an installed Remotion plugin in `claude plugin list --json`, else None."""
    try:
        items = json.loads(out or "[]")
    except ValueError:
        return None
    for it in items if isinstance(items, list) else []:
        pid = str((it or {}).get("id", "")) if isinstance(it, dict) else ""
        if pid.split("@", 1)[0] == CLAUDE_PLUGIN_NAME and it.get("enabled", True) is not False:
            return pid
    return None


def claude_plugin_state(run: Runner = subprocess.run, claude: str | None = None) -> dict[str, Any]:
    """{"installed": bool, "id": str|None, "checked": bool, "why": str|None}. Never raises."""
    exe = claude or find_claude()
    if not exe:
        return {"installed": False, "id": None, "checked": False,
                "why": "the Claude command line is not on this computer"}
    try:
        p = run([exe, "plugin", "list", "--json"], capture_output=True, text=True, timeout=60,
                env=_claude_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"installed": False, "id": None, "checked": False, "why": f"claude did not answer ({type(e).__name__})"}
    if p.returncode != 0:
        return {"installed": False, "id": None, "checked": False,
                "why": "claude plugin list failed: " + (p.stderr or p.stdout or "").strip()[-200:]}
    pid = parse_plugin_list(p.stdout)
    return {"installed": bool(pid), "id": pid, "checked": True, "why": None}


def install_claude_plugin(run: Runner = subprocess.run, log: Callable[[str], None] = print,
                          claude: str | None = None) -> str:
    """Install the Remotion plugin for Claude (idempotent). Returns its id; VPError on failure."""
    exe = claude or find_claude()
    st = claude_plugin_state(run, exe)
    if st["installed"]:
        log(f"Remotion plugin for Claude: already installed ({st['id']})")
        return str(st["id"])
    if not exe:
        raise VPError("internal", "the Claude command line is not on this computer — run the plugin "
                                  "installer again")
    log("Remotion plugin for Claude: installing from the Anthropic Directory…")
    try:
        p = run([exe, "plugin", "install", CLAUDE_PLUGIN, "--json"], capture_output=True, text=True,
                timeout=CLAUDE_TIMEOUT_S, env=_claude_env())
    except subprocess.TimeoutExpired as e:
        raise VPError("transport", "the Remotion plugin did not install in time — the internet is slow "
                                   "or down; try again later") from e
    except OSError as e:
        raise VPError("internal", f"could not run claude ({type(e).__name__})") from e
    outcome = None
    for line in reversed((p.stdout or "").strip().splitlines()):
        try:
            outcome = json.loads(line)
            break
        except ValueError:
            continue
    ok = p.returncode == 0 and (outcome is None or outcome.get("outcome") == "ok")
    if not ok:
        msg = ((outcome or {}).get("message") or p.stderr or p.stdout or "").strip()[-300:]
        kind = "transport" if classify(msg) == "network" else "internal"
        raise VPError(kind, ("could not reach the plugin directory — the internet connection dropped; "
                             "try again later" if kind == "transport" else
                             f"the Remotion plugin did not install: {msg}"))
    log("Remotion plugin for Claude: installed (it takes effect in the next Claude session)")
    return str((outcome or {}).get("pluginId") or CLAUDE_PLUGIN)


# ---- state: the owner's decision; the reminder cadence belongs to code -------------------------

def _state_path(home) -> Path:
    return extras_dir(home) / "state.yaml"


def read_state(home=None) -> dict[str, Any]:
    s = read_yaml(_state_path(home), default={})
    return s if isinstance(s, dict) else {}


def _write_state(home, **changes: Any) -> dict[str, Any]:
    s = read_state(home)
    s.update(changes)
    _state_path(home).parent.mkdir(parents=True, exist_ok=True)
    write_yaml(_state_path(home), s)
    return s


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def _config(home) -> dict[str, Any]:
    from .core.config import load_config
    return load_config(home)


def design_ready(home=None, config: dict[str, Any] | None = None) -> bool:
    """The design look can run here — from the add-ons or from the owner's own Node and Chrome."""
    from .adapters.render_remotion import missing_tools
    return not missing_tools(config if config is not None else _config(home))


def _workspace_ready(cfg: dict[str, Any]) -> bool:
    from .adapters.render_remotion import RemotionWorkspace
    try:
        return bool(RemotionWorkspace(cfg).ready())
    except Exception:  # noqa: BLE001 — status never crashes
        return False


def status(home=None, config: dict[str, Any] | None = None, run: Runner = subprocess.run,
           plugin: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else _config(home)
    ok, why = platform_ok()
    comps = {c.key: {"title": c.title, "version": c.version, "source": c.source,
                     "download_mb": round(c.size / 1e6, 1),
                     "installed": installed_exe(c, home) is not None} for c in COMPONENTS}
    ws = _workspace_ready(cfg)
    pl = plugin if plugin is not None else claude_plugin_state(run)
    st = read_state(home)
    missing = [k for k in ("node", "browser") if not comps[k]["installed"]]
    if not ws:
        missing.append("workspace")
    if not pl.get("installed"):
        missing.append("plugin")
    return {"all_installed": not missing, "missing": missing,
            "pack_installed": all(v["installed"] for v in comps.values()),
            "design_ready": design_ready(home, cfg),
            "workspace_ready": ws, "claude_plugin": pl,
            "platform_supported": ok, "platform_note": why,
            "components": comps, "download_mb": DOWNLOAD_MB, "disk_mb": DISK_MB,
            "decision": st.get("decision"), "offered_at": st.get("offered_at"),
            "last_reminded_at": st.get("last_reminded_at"), "installed_at": st.get("installed_at"),
            "last_install": st.get("last_install")}


def status_lines(s: dict[str, Any]) -> list[str]:
    """Plain lines for the owner (and for `vp extras status` without --json)."""
    c = s["components"]
    pl = s["claude_plugin"]
    mark = lambda ok: "OK  " if ok else "MISS"  # noqa: E731
    lines = [f"{mark(c['node']['installed'])} Node.js {c['node']['version']}",
             f"{mark(c['browser']['installed'])} Chrome Headless Shell {c['browser']['version']}",
             f"{mark(s['workspace_ready'])} Remotion project (the drawing tools)",
             f"{mark(pl.get('installed'))} Remotion plugin for Claude"
             + (f" ({pl['id']})" if pl.get("id") else "")
             + ("" if pl.get("checked", True) else f" — not checked: {pl.get('why')}")]
    if not s["platform_supported"]:
        lines.append(str(s["platform_note"]))
    lines.append("add-ons: all in place" if s["all_installed"] else
                 f"add-ons: missing {', '.join(s['missing'])} — the videos keep the plain look until "
                 f"then; {FIX_EN} / {FIX_RU}")
    return lines


def check(event: str, home=None, now: float | None = None,
          config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Should the director offer the add-ons now? Code decides; a "yes" is recorded right away.

      design ready (add-ons or own tools) -> no
      platform not supported              -> no (feature: yes, to say plainly why it is not there)
      event feature                       -> yes, always ("this needs the add-ons you postponed")
      never offered / no decision         -> yes (first offer)
      "later"                             -> yes once per REMIND_EVERY_S, else no
    """
    if event not in EVENTS:
        raise VPError("data", f"--event must be one of {list(EVENTS)}")
    now = time.time() if now is None else now
    st = read_state(home)
    ok, why = platform_ok()
    if design_ready(home, config):
        out = {"remind": False, "why": "ready"}
    elif not ok:
        out = {"remind": event == "feature", "why": "unsupported_platform", "note": why}
    elif event == "feature":
        out = {"remind": True, "why": "feature_needs_pack"}
    elif not st.get("decision"):
        out = {"remind": True, "why": "first_offer"}
    else:
        last = _ts(st.get("last_reminded_at")) or 0.0
        due = now - last >= REMIND_EVERY_S
        out = {"remind": due, "why": "periodic" if due else "reminded_recently"}
    if out["remind"] and out["why"] != "unsupported_platform":
        changes = {"last_reminded_at": _iso(now)}
        if not st.get("offered_at"):
            changes["offered_at"] = _iso(now)
        _write_state(home, **changes)
    out.update({"download_mb": DOWNLOAD_MB, "disk_mb": DISK_MB, "decision": st.get("decision")})
    return out


def decide(decision: str, home=None, now: float | None = None) -> dict[str, Any]:
    if decision != "later":
        raise VPError("data", "decide: only 'later' (installing records 'installed' by itself)")
    now = time.time() if now is None else now
    st = read_state(home)
    return _write_state(home, decision="later", offered_at=st.get("offered_at") or _iso(now),
                        last_reminded_at=_iso(now))


# ---- install --------------------------------------------------------------------------------

Fetch = Callable[[str, Path, Callable[[int], None]], None]


def http_fetch(url: str, dst: Path, on_bytes: Callable[[int], None]) -> None:
    """Plain https download; progress via on_bytes(total_so_far). Network faults -> transport."""
    if not url.startswith("https://"):
        raise VPError("internal", f"refusing a non-https download: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "ai-movie-company-extras"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
            if not r.geturl().startswith("https://"):
                raise VPError("internal", f"refusing a redirect away from https: {url}")
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                on_bytes(got)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VPError("transport", f"could not download {url.rsplit('/', 1)[-1]} — the internet "
                                   f"connection dropped or the site did not answer ({type(e).__name__}); "
                                   f"try again later, nothing was installed") from e


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(comp: Component, archive: Path, into: Path) -> None:
    if comp.archive == "tar.xz":
        with tarfile.open(archive, "r:xz") as t:
            t.extractall(into, filter="data")      # no absolute paths, no escaping links
    elif comp.archive == "zip":
        # ditto keeps the executable bits and links of macOS archives (zipfile drops them).
        tool = shutil.which("ditto")
        cmd = [tool, "-x", "-k", str(archive), str(into)] if tool else ["unzip", "-q", str(archive), "-d", str(into)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise VPError("data", f"could not unpack {archive.name}: {p.stderr.strip()[-300:]}")
    else:
        raise VPError("internal", f"unknown archive type {comp.archive}")


def install_component(comp: Component, home=None, fetch: Fetch = http_fetch,
                      log: Callable[[str], None] = print,
                      report: Callable[[float, float], None] | None = None) -> Path:
    """Download, check the pinned sha256, unpack into a staging folder, then swap it in.
    Idempotent: an installed component is left as it is. A wrong checksum installs nothing."""
    done = installed_exe(comp, home)
    if done:
        log(f"{comp.title} {comp.version}: already installed")
        return done
    base = extras_dir(home)
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{comp.key}-", dir=base))
    try:
        archive = staging / f"download.{comp.archive}"
        total_mb = comp.size / 1e6
        step = max(1, comp.size // 4)
        last = [0]

        def on_bytes(got: int) -> None:
            if report:
                report(got / 1e6, total_mb)
            if got // step > last[0]:
                last[0] = got // step
                log(f"  {comp.title}: {got / 1e6:.0f} of {total_mb:.0f} MB")
        log(f"{comp.title} {comp.version}: downloading {total_mb:.0f} MB from {comp.source}")
        fetch(comp.url, archive, on_bytes)
        got = _sha256(archive)
        if got != comp.sha256:
            raise VPError("data", f"{comp.title}: the downloaded file is not the one we expect "
                                  f"(checksum {got[:12]}… ≠ {comp.sha256[:12]}…); nothing was installed. "
                                  f"It may be a network fault — try again later.")
        unpack = staging / "unpack"
        unpack.mkdir()
        _extract(comp, archive, unpack)
        top = unpack / comp.top
        if not (top / comp.exe).is_file():
            raise VPError("data", f"{comp.title}: {comp.exe} not found in the archive")
        (top / OK_MARK).write_text(f"{comp.version} {comp.sha256}\n")
        final = base / comp.folder
        if final.exists():
            shutil.rmtree(final)
        os.replace(top, final)
        log(f"{comp.title} {comp.version}: installed")
        return final / comp.exe
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _plain(e: BaseException) -> tuple[str, str]:
    """(class, message): network | checksum | platform | other."""
    msg = str(e)
    if isinstance(e, VPError):
        if e.kind == "transport":
            return "network", msg
        if "checksum" in msg:
            return "checksum", msg
    return "other", msg


def install(home=None, fetch: Fetch = http_fetch, log: Callable[[str], None] = print,
            report: Callable[[float, float, str], None] | None = None,
            workspace: bool = True, plugin: bool = True, run: Runner = subprocess.run,
            now: float | None = None) -> dict[str, Any]:
    """Every step in order; a failed step does not stop the others (a step that needs it is
    skipped). Returns status() plus `steps` {step: ok|failed|skipped} and `errors`."""
    steps: dict[str, str] = {}
    errors: dict[str, dict[str, str]] = {}

    def attempt(name: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            steps[name] = "ok"
        except (VPError, OSError, subprocess.SubprocessError) as e:
            cls, msg = _plain(e)
            steps[name] = "failed"
            errors[name] = {"class": cls, "message": msg}
            log(f"{name}: did not work — {msg}")

    ok, why = platform_ok()
    if not ok:
        for name in ("node", "browser", "workspace"):
            steps[name] = "skipped"
            errors[name] = {"class": "platform", "message": str(why)}
        log(str(why))
    else:
        for comp in COMPONENTS:
            attempt(comp.key, lambda comp=comp: install_component(
                comp, home, fetch, log,
                (lambda d, t, c=comp: report(d, t, f"MB {c.key}")) if report else None))
        if not workspace:
            steps["workspace"] = "skipped"
        elif steps.get("node") != "ok":
            steps["workspace"] = "skipped"
            errors["workspace"] = {"class": "other", "message": "needs Node.js, which did not install"}
        else:
            def _ws() -> None:
                from .adapters import render_remotion as rr
                log("Remotion project: checking the drawing tools (first time: a pinned list, "
                    "about a minute)…")
                rr._missing_tools.cache_clear()            # the tools just appeared
                ws = rr.RemotionWorkspace(_config(home))
                if ws.ready():
                    ws.ensure(log)                         # refreshes the code only; no network
                    log("Remotion project: already installed")
                    return
                ws.ensure(lambda m: None)                  # its own line is said here, once
                log("Remotion project: ready")
            attempt("workspace", _ws)
    if plugin:
        new_plugin: list[bool] = []

        def _plugin() -> None:
            before = claude_plugin_state(run).get("installed")
            install_claude_plugin(run, log)
            new_plugin.append(not before)
        attempt("plugin", _plugin)
        restart = bool(new_plugin and new_plugin[0])
    else:
        steps["plugin"] = "skipped"
        restart = False

    t = _iso(time.time() if now is None else now)
    failed = [k for k, v in steps.items() if v != "ok" and not (k == "plugin" and not plugin)
              and not (k == "workspace" and not workspace)]     # a step the caller turned off is not a failure
    changes: dict[str, Any] = {"last_install": {"at": t, "steps": dict(steps),
                                                "errors": {k: v["class"] for k, v in errors.items()}}}
    if not failed:
        changes.update(decision="installed", installed_at=t)
    _write_state(home, **changes)
    out = status(home, run=run)
    out.update({"steps": steps, "errors": errors, "failed": failed, "restart_needed": restart})
    if restart:
        log(RESTART_LINE)
    if failed:
        log("Not everything installed (" + ", ".join(failed) + "). The videos still work in the plain "
            f"look; to retry later — {FIX_EN} / {FIX_RU}.")
    else:
        log("The add-ons are ready: animated captions, drawn cards and the cover are available.")
    return out


# ---- CLI ------------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vp extras", description="the add-ons for the design look")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("check")
    s.add_argument("--event", required=True, choices=EVENTS)
    s = sub.add_parser("decide")
    s.add_argument("decision", choices=("later",))
    s = sub.add_parser("install")
    s.add_argument("--json", action="store_true", help="print the result as JSON at the end")
    s.add_argument("--job", help="show progress in this job's progress band")
    s.add_argument("--no-plugin", action="store_true", help="skip the Remotion plugin for Claude")
    a = p.parse_args(argv)

    if a.cmd == "status":
        st = status()
        if a.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            print("\n".join(status_lines(st)))
        return 0
    if a.cmd == "check":
        print(json.dumps(check(a.event), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "decide":
        print(json.dumps(decide(a.decision), ensure_ascii=False, indent=2))
        return 0
    log = (lambda m: print(m, file=sys.stderr, flush=True)) if a.json else (lambda m: print(m, flush=True))
    if a.job:
        from .core import progress
        from .core.paths import Job
        with progress.tracking(Job(a.job).root, "extras", None, total=DOWNLOAD_MB, unit="MB"):
            out = install(log=log, plugin=not a.no_plugin, report=lambda d, t, unit: progress.report(
                d + (NODE.size / 1e6 if "browser" in unit else 0.0), DOWNLOAD_MB, "MB"))
    else:
        out = install(log=log, plugin=not a.no_plugin)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    print("EXTRAS: OK" if not out["failed"] else "EXTRAS: PARTIAL " + ",".join(out["failed"]),
          flush=True)
    return 0 if not out["failed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
