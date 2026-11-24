"""`doctor` — is this machine ready to run the pipeline? Read-only except a probe file in VP_HOME.

First line: the plugin's release line (`INFO plugin: 0.8.8 alpha · build 57 · 2026-11-23 22:40
UTC`, vp.buildinfo) — which build this report is about.
Checks: Python version, virtualenv, dependencies importable, ffmpeg/ffprobe (+ the `subtitles`
filter from libass and `drawtext`), VP_HOME writable, config sanity (publisher, notifier, judge).
Secret contents are never read or printed: for credential files only "exists / missing" is shown,
for env vars only "set / not set".

Also the add-ons for the design look (Node, Chrome Headless Shell, the Remotion project, the
Remotion plugin for Claude — `vp extras`): a missing one is WARN with the fix in plain words.

Output: one line per check `OK|INFO|WARN|FAIL <name>: <detail>`; exit 0 without FAIL, 2 with a
FAIL. INFO is a plain fact (e.g. add-ons not made for this computer), never a problem.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .core.config import config_path, load_config
from .core.paths import vp_home

MIN_PY = (3, 12)
REQUIRED_MODULES = ("yaml",)
YOUTUBE_MODULES = ("googleapiclient", "google_auth_oauthlib", "google.oauth2", "httplib2")


def _module_ok(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _ffmpeg_filters(ffmpeg: str) -> set[str]:
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    out = set()
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) <= 4 and set(parts[0]) <= set("TSC.|"):
            out.add(parts[1])
    return out


def checks(home: Path | None = None) -> list[tuple[str, str, str]]:
    """[(level, name, detail)], level OK|INFO|WARN|FAIL."""
    res: list[tuple[str, str, str]] = []
    add = lambda lvl, name, detail: res.append((lvl, name, detail))  # noqa: E731

    from . import buildinfo
    add("INFO", "plugin", buildinfo.label())
    v = sys.version_info
    add("OK" if (v.major, v.minor) >= MIN_PY else "FAIL", "python",
        f"{v.major}.{v.minor}.{v.micro} at {sys.executable} (need >= {MIN_PY[0]}.{MIN_PY[1]})")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    venv = os.environ.get("VP_VENV") or (sys.prefix if in_venv else "")
    add("OK" if in_venv else "WARN", "venv", venv or "not running in a virtualenv")

    for m in REQUIRED_MODULES:
        add("OK" if _module_ok(m) else "FAIL", f"module {m}", "importable" if _module_ok(m) else "missing")

    ffmpeg = os.environ.get("VP_FFMPEG", "ffmpeg")
    ffprobe = os.environ.get("VP_FFPROBE", "ffprobe")
    fpath, ppath = shutil.which(ffmpeg), shutil.which(ffprobe)
    add("OK" if fpath else "FAIL", "ffmpeg", fpath or "not found (macOS: ask Claude to run the plugin installer again)")
    add("OK" if ppath else "FAIL", "ffprobe", ppath or "not found (installed with ffmpeg)")
    if fpath:
        filters = _ffmpeg_filters(fpath)
        for f, why in (("subtitles", "burned-in subtitles (ffmpeg built with libass)"),
                       ("drawtext", "cards and thumbnails (ffmpeg built with libfreetype)")):
            add("OK" if f in filters else "FAIL", f"ffmpeg filter {f}",
                ("present — " if f in filters else "missing — needed for ") + why)

    h = Path(home) if home else vp_home()
    try:
        h.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".doctor.", dir=h)
        os.close(fd)
        os.unlink(tmp)
        add("OK", "VP_HOME", f"{h} writable")
    except OSError as e:
        add("FAIL", "VP_HOME", f"{h} not writable ({type(e).__name__})")
        return res

    try:
        cfg = load_config(h)
    except Exception as e:  # noqa: BLE001 — report, do not crash
        add("FAIL", "config", f"{config_path(h)}: {e}")
        return res
    add("OK", "config", str(config_path(h)))
    res.extend(_publisher_checks(cfg))
    res.extend(_notifier_checks(cfg))
    res.extend(_judge_checks(cfg))
    res.extend(_design_checks(cfg))
    return res


def _design_checks(cfg: dict[str, Any], plugin: dict[str, Any] | None = None) -> list[tuple[str, str, str]]:
    """The add-ons for the design look (installed by default since 0.8): Node, Chrome, the Remotion
    project, the Remotion plugin for Claude. A missing one is WARN, never FAIL: without it the videos
    are made in the plain look. Every WARN carries the fix in plain words (ru + en)."""
    from . import extras
    from .adapters.render_remotion import RemotionWorkspace, find_browser, find_node, remotion_settings
    rs = remotion_settings(cfg)
    home = cfg.get("vp_home")
    node, chrome = find_node(rs.get("node_bin"), home), find_browser(rs.get("browser_executable"), home)
    try:
        ws = RemotionWorkspace(cfg).ready()
    except Exception:  # noqa: BLE001 — doctor reports, never crashes
        ws = False
    pl = plugin if plugin is not None else extras.claude_plugin_state()
    fix = f" — {extras.FIX_RU} / {extras.FIX_EN}"
    plain = " (until then the videos keep the plain look)"
    ok, why = extras.platform_ok()
    if not ok and not (node and chrome):
        return [("INFO", "add-ons", str(why))]
    return [
        ("OK", "add-on node", node) if node else
        ("WARN", "add-on node", "Node.js not found — needed for animated captions and drawn cards"
                                + plain + fix),
        ("OK", "add-on chrome", chrome) if chrome else
        ("WARN", "add-on chrome", "no Chrome to draw the video in" + plain + fix),
        ("OK", "add-on remotion project", "installed") if ws else
        ("WARN", "add-on remotion project", "the drawing tools are not installed yet" + plain + fix),
        ("OK", "add-on remotion plugin", f"{pl.get('id')} (Remotion skills for Claude)")
        if pl.get("installed") else
        ("WARN", "add-on remotion plugin", "the Remotion plugin for Claude is not installed"
         + (f" (could not check: {pl.get('why')})" if not pl.get("checked", True) else "") + fix),
    ]


def _publisher_checks(cfg: dict[str, Any]) -> list[tuple[str, str, str]]:
    kind = cfg.get("publisher")
    if kind == "fake":
        return [("OK", "publisher", "fake — REHEARSAL: nothing is uploaded to YouTube (set "
                                    "publisher: youtube in config.yaml for real private uploads)")]
    if kind != "youtube":
        return [("FAIL", "publisher", f"unknown publisher {kind!r} (youtube | fake)")]
    out = [("OK", "publisher", "youtube (videos are uploaded as private drafts)")]
    missing = [m for m in YOUTUBE_MODULES if not _module_ok(m)]
    out.append(("FAIL" if missing else "OK", "youtube modules",
                "missing: " + ", ".join(missing) if missing else "importable"))
    yt = cfg.get("youtube") or {}
    secrets = yt.get("client_secrets_path")
    if not secrets:
        out.append(("FAIL", "youtube.client_secrets_path",
                    "not set — put the path of the Google OAuth client file (client_secret_*.json, "
                    "Desktop app) into youtube.client_secrets_path in config.yaml"))
    else:
        p = Path(str(secrets)).expanduser()
        out.append(("OK", "youtube.client_secrets_path", f"{p} exists") if p.is_file() else
                   ("FAIL", "youtube.client_secrets_path",
                    f"{p} missing — put the Google OAuth client file (client_secret_*.json, "
                    f"Desktop app) there, or point youtube.client_secrets_path at it"))
    token = yt.get("token_path")
    if not token:
        out.append(("FAIL", "youtube.token_path",
                    "not set — choose a path for the login token in config.yaml "
                    "(e.g. ~/.ai_video_pipeline/youtube_token.json), then run `vp youtube authorize`"))
    else:
        p = Path(str(token)).expanduser()
        out.append(("OK", "youtube.token_path", f"{p} exists") if p.is_file() else
                   ("FAIL", "youtube.token_path",
                    f"{p} missing — the channel is not connected: run `vp youtube authorize` "
                    f"(browser login), then `vp youtube status`"))
        out.extend(_login_problem(cfg, p))
    out.append(("OK" if yt.get("channel_id") else "FAIL", "youtube.channel_id",
                "set" if yt.get("channel_id") else
                "not set — run `vp youtube authorize` (browser login): it fills the channel id in "
                "itself"))
    return out


def _login_problem(cfg: dict[str, Any], token: Path) -> list[tuple[str, str, str]]:
    """The last login problem seen by `vp youtube authorize|status` (access_denied, expired or
    revoked token), unless the token file was written again after it. Offline: no API call."""
    from .adapters.youtube import explain_auth_problem, read_connection
    from .core.io import parse_iso
    state = read_connection(cfg.get("vp_home"))
    problem = (state or {}).get("problem")
    if not problem:
        return []
    try:
        seen = parse_iso(str(state.get("at"))).timestamp()
    except (ValueError, TypeError):
        seen = 0.0
    if token.is_file() and token.stat().st_mtime > seen:
        return []
    return [("FAIL", "youtube login", explain_auth_problem(str(problem), cfg.get("youtube") or {}))]


def _notifier_checks(cfg: dict[str, Any]) -> list[tuple[str, str, str]]:
    kind = cfg.get("notifier")
    if kind in ("session", "file"):
        return [("OK", "notifier", f"{kind}")]
    if kind != "telegram":
        return [("FAIL", "notifier", f"unknown notifier {kind!r}")]
    tg = cfg.get("telegram") or {}
    out = [("OK", "notifier", "telegram")]
    for key in ("token_env", "chat_id_env"):
        name = tg.get(key)
        ok = bool(name and os.environ.get(str(name)))
        out.append(("OK" if ok else "FAIL", f"telegram.{key}", f"env {name} " + ("set" if ok else "not set")))
    return out


def _judge_checks(cfg: dict[str, Any]) -> list[tuple[str, str, str]]:
    kind = cfg.get("judge")
    src = "env VP_JUDGE" if os.environ.get("VP_JUDGE") else "config"
    if kind == "manual":
        return [("OK", "judge", f"manual ({src}): the Claude session performs judgment tasks")]
    if kind == "fake":
        return [("WARN", "judge", f"fake ({src}): canned answers, tests only")]
    if kind == "claude_cli":
        exe = shutil.which("claude")
        return [("OK" if exe else "FAIL", "judge",
                 f"claude_cli ({src}): " + (exe or "`claude` not on PATH"))]
    return [("FAIL", "judge", f"unknown judge {kind!r}")]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vp doctor", description="check the machine and config")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    res = checks()
    if a.json:
        import json
        print(json.dumps([{"level": l, "check": n, "detail": d} for l, n, d in res],
                         ensure_ascii=False, indent=2))
    else:
        for lvl, name, detail in res:
            print(f"{lvl:<4} {name}: {detail}")
    fails = sum(1 for r in res if r[0] == "FAIL")
    if not a.json:
        print(f"doctor: {'ready' if not fails else str(fails) + ' problem(s)'}")
    return 2 if fails else 0
