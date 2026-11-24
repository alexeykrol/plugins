"""The plugin's release line: version, stage, build number, time of the release.

    0.8.8 alpha · build 57 · 2026-11-23 22:40 UTC

  version    .claude-plugin/plugin.json `version` — plain X.Y.Z (the store, the telemetry header and
             the message feed compare it; the stage never goes into it)
  stage      build.json `stage` — one of STAGES; `release` shows no word
  build      build.json `build` — a number that grows by one with every release
  released   build.json `released` — UTC, "YYYY-MM-DDTHH:MM:SSZ"

build.json sits next to the folder `.claude-plugin/` (the plugin's root). Its `build` and
`released` are written only by scripts/stamp_build.py, which scripts/release.sh runs: by code,
never by hand and never by a model. A missing or broken build.json (a dev checkout, an install
older than build.json) never raises: the line degrades to "0.8.7 alpha".

`label()` is the single source of the line for the engine (`vp version`, `vp doctor`, `vp update
check`, the feedback pack). The mod draws the same line from the same two files with
hooks/persona.ts `releaseLabel` (it has no Python); tests hold the two to the same words.

CLI:  vp version [--json] [--lang en|ru]
Standard library only, no imports from the rest of vp: scripts/stamp_build.py and
scripts/release.sh load this module with the system Python.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STAGES = ("alpha", "beta", "release")
DEFAULT_STAGE = "alpha"            # what an install without build.json is called
QUIET_STAGE = "release"            # the stage that shows no word
BUILD_FILE = "build.json"
MANIFEST = ".claude-plugin/plugin.json"
UNKNOWN = "unknown"
SEP = " · "
BUILD_WORD = {"en": "build", "ru": "сборка"}
LANGS = ("en", "ru")

_RELEASED_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::(\d\d))?Z$")


@dataclass(frozen=True)
class BuildInfo:
    version: str                   # "0.8.7"; "unknown" without a readable manifest
    stage: str = DEFAULT_STAGE     # one of STAGES
    build: int | None = None       # None: not known (no build.json, or a broken one)
    released: str | None = None    # "2026-11-23T22:40:00Z"; None: not known

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "stage": self.stage, "build": self.build,
                "released": self.released, "label": label(self)}


def plugin_root() -> Path | None:
    """The folder that holds .claude-plugin/plugin.json above this file (an installed plugin or
    the repository); None when there is none."""
    for root in Path(__file__).resolve().parents:
        if (root / MANIFEST).is_file():
            return root
    return None


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_released(value: Any) -> datetime | None:
    """The stamp's own form only ("2026-11-23T22:40:00Z", seconds optional); anything else: None."""
    m = _RELEASED_RE.match(value) if isinstance(value, str) else None
    if not m:
        return None
    try:
        return datetime(*(int(g or 0) for g in m.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None


def format_released(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(root: Path | None = None) -> BuildInfo:
    """The release line of the plugin at `root` (default: the one this code runs from). Every
    field is checked on its own; a bad one falls back, nothing raises."""
    root = root if root is not None else plugin_root()
    if root is None:
        return BuildInfo(UNKNOWN)
    version = _load(root / MANIFEST).get("version")
    held = _load(root / BUILD_FILE)
    stage, build = held.get("stage"), held.get("build")
    at = parse_released(held.get("released"))
    return BuildInfo(
        version=version if isinstance(version, str) and version.strip() else UNKNOWN,
        stage=stage if stage in STAGES else DEFAULT_STAGE,
        build=build if isinstance(build, int) and not isinstance(build, bool) and build > 0 else None,
        released=format_released(at) if at else None)


def released_text(released: str | None, lang: str = "en") -> str | None:
    """"2026-11-23 22:40 UTC" (ru: "23.11.2026 22:40 UTC"); None when the time is not known."""
    at = parse_released(released)
    if at is None:
        return None
    return at.strftime("%d.%m.%Y %H:%M UTC" if lang == "ru" else "%Y-%m-%d %H:%M UTC")


def label(info: BuildInfo | None = None, lang: str = "en") -> str:
    """The line a person sees: "0.8.8 alpha · build 57 · 2026-11-23 22:40 UTC". The word of the
    stage stays English in every language; what is not known is left out."""
    info = info if info is not None else read()
    lang = lang if lang in LANGS else "en"
    parts = [info.version if info.stage == QUIET_STAGE else f"{info.version} {info.stage}"]
    if info.build is not None:
        parts.append(f"{BUILD_WORD[lang]} {info.build}")
    when = released_text(info.released, lang)
    if when:
        parts.append(when)
    return SEP.join(parts)


def changelog_heading(info: BuildInfo) -> str:
    """The heading of a release in CHANGELOG.md: "## 0.8.8 alpha — build 57 — 2026-11-23 22:40 UTC"
    (written by scripts/stamp_build.py)."""
    return "## " + label(info).replace(SEP, " — ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vp version",
                                 description="the plugin's version, stage, build and release time")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--lang", choices=LANGS, default="en")
    a = ap.parse_args(argv)
    info = read()
    print(json.dumps(info.as_dict(), ensure_ascii=False) if a.json else label(info, a.lang))
    return 0


if __name__ == "__main__":  # pragma: no cover - `python3 -m vp.buildinfo` for the release script
    sys.exit(main())
