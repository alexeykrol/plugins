"""Is the engine busy? A render, an upload, a generation — anything a restart of Claude would break.

Signals:
  * busy markers: <VP_HOME>/busy/<pid>-<n>.json, written by long-running commands (`with
    marker(...)`: `vp run`, `vp stage`, text renders, exports, uploads) and removed in `finally`;
    a marker whose process is gone (same host, dead pid) is stale and is removed when read;
  * the progress mirror <VP_HOME>/progress-live.json updated within progress.STALE_S.

The mod asks `vp busy --json` before it shows a message from the authors (an update notice must
never ask for a restart in the middle of a render). Approval gates are known to the mod itself
(its document pane waiting for an answer).

CLI:  vp busy [--json]
"""
from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_seq = itertools.count(1)


def busy_dir() -> Path:
    from .core.paths import vp_home
    return vp_home() / "busy"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextlib.contextmanager
def marker(what: str, job: str | None = None) -> Iterator[Path | None]:
    """Mark the engine busy for the duration of the block. Never breaks the caller."""
    path: Path | None = None
    try:
        d = busy_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{os.getpid()}-{next(_seq)}.json"
        path.write_text(json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(), "what": what, "job": job,
            "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}), encoding="utf-8")
    except OSError:
        path = None
    try:
        yield path
    finally:
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()


def markers() -> list[dict[str, Any]]:
    """Live markers; stale ones (dead pid on this host, unreadable) are removed."""
    d = busy_dir()
    if not d.is_dir():
        return []
    host = socket.gethostname()
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            pid = int(rec.get("pid"))
        except (OSError, ValueError, TypeError):
            with contextlib.suppress(OSError):
                if time.time() - p.stat().st_mtime > 60:
                    p.unlink()
            continue
        if rec.get("host") == host and not _alive(pid):
            with contextlib.suppress(OSError):
                p.unlink()
            continue
        out.append(rec)
    return out


def live_progress() -> dict[str, Any] | None:
    from .core import progress
    from .core.io import read_json, parse_iso
    from .core.paths import vp_home
    rec = read_json(vp_home() / progress.LIVE_NAME, default=None)
    if not isinstance(rec, dict) or not rec.get("step"):
        return None
    try:
        age = time.time() - parse_iso(str(rec["updated_at"])).timestamp()
    except (KeyError, ValueError):
        return None
    return rec if age <= progress.STALE_S else None


def state() -> dict[str, Any]:
    ms = markers()
    live = live_progress()
    what = [m.get("what") for m in ms]
    if live:
        what.append(f"step {live.get('step')}")
    return {"busy": bool(ms or live), "what": what,
            "jobs": sorted({m["job"] for m in ms if m.get("job")})}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp busy", description="is a render/upload/generation running")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    st = state()
    print(json.dumps(st, ensure_ascii=False) if a.json
          else ("busy: " + ", ".join(str(w) for w in st["what"]) if st["busy"] else "idle"))
    return 0
