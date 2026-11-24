"""Projects the person has worked on (K3): a registry of job folders in the pipeline home.

Before 0.8 the engine knew a job only when given its folder; nothing listed them. The registry
`<VP_HOME>/projects.json` fills that gap. A job folder is registered (or touched) whenever work on
it starts through the engine: `vp run`, `vp stage`, `vp text new`, a CapCut version export. Older
folders can be added by hand (`vp projects add`) or found under a folder (`vp projects scan`).

Registry format (bookkeeping only, written atomically):
  {"projects": {"<absolute folder>": {"title", "kind", "first_seen", "last_seen"}}}

The list (`vp projects list`) is computed, never stored: title (request.yaml `title`, else
`job_id`, else the folder name — "<parent>/<folder>" for ai_lms `_video` folders), kind
(recording | text), date (local YYYY-MM-DD of the last activity) and last_activity (ISO; the
newest of the registry touch and the job folder's top-level entries), folder, state; newest first:
  in_work         — work started, nothing waits for the person
  waiting_review  — the engine waits for the person (a dialog gate: review / approve)
  done            — recording job: the report step (S11) finished; text job: a CapCut version
                    was exported (further versions are still possible)
  missing         — the folder no longer exists (shown only with --all)

The summary (`vp projects summary`) answers one question for the dialog — is this the person's
first project? — from facts on disk, never from the model's memory: `with_video` counts the
registered jobs that already hold a finished video (text job: a render in `renders/`; recording
job: a rendered clip `clips/<id>/render.mp4` or the full video), `first_project` is true while
that count is zero. A job started and dropped before any video does not end the "first project".

CLI:  vp projects list [--json] [--all] [--limit N]
      vp projects summary [--json]
      vp projects add <folder> [--json]
      vp projects scan <folder> [--depth N] [--json]
      vp projects forget <folder>
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core.io import now_iso, read_json, read_yaml, write_json
from .core.paths import vp_home

FILE = "projects.json"
STATES = ("in_work", "waiting_review", "done", "missing")
_WAIT = ("waiting_human", "waiting_judgment")


def registry_path(home: Path | None = None) -> Path:
    return (home or vp_home()) / FILE


def _load(home: Path | None = None) -> dict[str, Any]:
    try:
        data = read_json(registry_path(home), default=None)
    except (ValueError, OSError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        data = {"projects": {}}
    return data


def _request(folder: Path) -> dict[str, Any]:
    try:
        req = read_yaml(folder / "request.yaml", default={})
    except Exception:  # noqa: BLE001 - a broken request.yaml must not break the list
        return {}
    return req if isinstance(req, dict) else {}


def job_title(folder: Path, req: dict[str, Any] | None = None) -> str:
    req = _request(folder) if req is None else req
    for key in ("title", "job_id"):
        v = req.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    if folder.name.startswith("_"):
        return f"{folder.parent.name}/{folder.name}"
    return folder.name


def job_kind(req: dict[str, Any]) -> str:
    return "text" if str(req.get("kind") or "").strip().lower() == "text" else "recording"


def touch(folder: str | os.PathLike, home: Path | None = None, now: str | None = None) -> bool:
    """Register `folder` (a job folder with request.yaml) or refresh its last_seen.
    False (nothing written) when the folder is not a job folder."""
    folder = Path(folder).expanduser().resolve()
    if not (folder / "request.yaml").is_file():
        return False
    req = _request(folder)
    data = _load(home)
    at = now or now_iso()
    rec = data["projects"].get(str(folder)) or {"first_seen": at}
    rec.update(title=job_title(folder, req), kind=job_kind(req), last_seen=at)
    data["projects"][str(folder)] = rec
    write_json(registry_path(home), data)
    return True


def safe_touch(folder: str | os.PathLike) -> None:
    """touch() that never raises: the registry must never stop real work."""
    try:
        touch(folder)
    except Exception:  # noqa: BLE001
        pass


def forget(folder: str | os.PathLike, home: Path | None = None) -> bool:
    folder = str(Path(folder).expanduser().resolve())
    data = _load(home)
    if folder not in data["projects"]:
        return False
    del data["projects"][folder]
    write_json(registry_path(home), data)
    return True


def scan(root: str | os.PathLike, depth: int = 4) -> list[Path]:
    """Job folders (with request.yaml) under `root`, at most `depth` levels down."""
    root = Path(root).expanduser().resolve()
    found: list[Path] = []

    def walk(d: Path, level: int) -> None:
        if (d / "request.yaml").is_file():
            found.append(d)
            return
        if level >= depth:
            return
        try:
            entries = sorted(e for e in d.iterdir() if e.is_dir() and not e.name.startswith("."))
        except OSError:
            return
        for e in entries:
            if e.name in ("node_modules", "__pycache__", "Legacy"):
                continue
            walk(e, level + 1)

    walk(root, 0)
    return found


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _parse(at: str | None) -> float:
    if not at:
        return 0.0
    try:
        return datetime.fromisoformat(str(at)).timestamp()
    except ValueError:
        return 0.0


def last_activity(folder: Path, registry_at: str | None = None) -> float:
    """Newest of the registry touch and the mtimes of the folder's top-level entries."""
    best = _parse(registry_at)
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name in ("job.lock",):
                    continue
                try:
                    best = max(best, e.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return best


def job_state(folder: Path, req: dict[str, Any] | None = None) -> str:
    if not folder.is_dir():
        return "missing"
    req = _request(folder) if req is None else req
    if job_kind(req) == "text":
        from .text import stages_present
        return "done" if stages_present(folder).get("capcut") else "in_work"
    try:
        st = read_yaml(folder / "status.yaml", default={}) or {}
    except Exception:  # noqa: BLE001
        st = {}
    history = st.get("history") if isinstance(st, dict) else None
    last: dict[tuple, str] = {}
    for e in history or []:
        if isinstance(e, dict):
            last[(e.get("stage"), e.get("item"))] = str(e.get("state"))
    if any(s in _WAIT for s in last.values()):
        return "waiting_review"
    if last.get(("S11", None)) == "done":
        return "done"
    return "in_work"


def job_has_video(folder: Path, req: dict[str, Any] | None = None) -> bool:
    """A finished video of the job exists on disk (text: a render; recording: a rendered clip)."""
    if not folder.is_dir():
        return False
    req = _request(folder) if req is None else req
    try:
        if job_kind(req) == "text":
            from .text import renders
            return bool(renders(folder))
        if (folder / "full" / "render.mp4").is_file():
            return True
        clips = folder / "clips"
        return clips.is_dir() and any((d / "render.mp4").is_file() for d in clips.iterdir()
                                      if d.is_dir())
    except OSError:
        return False


def summary(home: Path | None = None) -> dict[str, Any]:
    """{"total", "with_video", "first_project"} over the registered jobs that still exist."""
    total = with_video = 0
    for folder_s in _load(home)["projects"]:
        folder = Path(folder_s)
        if not folder.is_dir():
            continue
        total += 1
        with_video += 1 if job_has_video(folder) else 0
    return {"total": total, "with_video": with_video, "first_project": with_video == 0}


def list_projects(home: Path | None = None, include_missing: bool = False) -> list[dict[str, Any]]:
    out = []
    for folder_s, rec in _load(home)["projects"].items():
        folder = Path(folder_s)
        exists = folder.is_dir()
        if not exists and not include_missing:
            continue
        req = _request(folder) if exists else {}
        ts = last_activity(folder, rec.get("last_seen")) if exists else _parse(rec.get("last_seen"))
        at = _iso_from_ts(ts) if ts else None
        out.append({"title": job_title(folder, req) if exists else rec.get("title") or folder.name,
                    "folder": folder_s,
                    "kind": job_kind(req) if exists else rec.get("kind") or "recording",
                    "date": _local_day(at)[:10] if at else None,
                    "last_activity": at,
                    "state": job_state(folder, req) if exists else "missing",
                    "_ts": ts})
    out.sort(key=lambda r: r["_ts"], reverse=True)
    for r in out:
        del r["_ts"]
    return out


STATE_LABEL = {"in_work": "in work", "waiting_review": "waiting review", "done": "done",
               "missing": "missing"}


def _local_day(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp projects", description="projects the person worked on")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="every registered job, newest first")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--all", action="store_true", help="also folders that no longer exist")
    ls.add_argument("--limit", type=int, default=0)
    sm = sub.add_parser("summary", help="how many jobs, how many with a finished video; "
                                        "first_project = none has one yet")
    sm.add_argument("--json", action="store_true")
    ad = sub.add_parser("add", help="register a job folder")
    ad.add_argument("folder")
    ad.add_argument("--json", action="store_true")
    sc = sub.add_parser("scan", help="register every job folder found under a folder")
    sc.add_argument("folder")
    sc.add_argument("--depth", type=int, default=4)
    sc.add_argument("--json", action="store_true")
    fg = sub.add_parser("forget", help="remove a folder from the list (the folder is not touched)")
    fg.add_argument("folder")
    a = ap.parse_args(argv)

    if a.cmd == "list":
        rows = list_projects(include_missing=a.all)
        if a.limit > 0:
            rows = rows[:a.limit]
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 0
        if not rows:
            print("no projects yet")
            return 0
        for r in rows:
            print(f"{_local_day(r['last_activity'])}\t{STATE_LABEL[r['state']]}\t{r['kind']}\t"
                  f"{r['title']}\t{r['folder']}")
        return 0
    if a.cmd == "summary":
        res = summary()
        print(json.dumps(res, ensure_ascii=False) if a.json else
              f"projects: {res['total']}, with a finished video: {res['with_video']}, "
              f"first project: {'yes' if res['first_project'] else 'no'}")
        return 0
    if a.cmd == "add":
        ok = touch(a.folder)
        res = {"added": ok, "folder": str(Path(a.folder).expanduser().resolve())}
        print(json.dumps(res, ensure_ascii=False) if a.json else
              (f"ADDED {res['folder']}" if ok else f"NOT A JOB FOLDER (no request.yaml): {res['folder']}"))
        return 0 if ok else 2
    if a.cmd == "scan":
        found = [str(f) for f in scan(a.folder, a.depth) if touch(f)]
        if a.json:
            print(json.dumps({"added": found}, ensure_ascii=False, indent=1))
        else:
            for f in found:
                print(f"ADDED {f}")
            print(f"{len(found)} job folder(s) registered")
        return 0
    ok = forget(a.folder)
    print("FORGOTTEN" if ok else "NOT IN THE LIST")
    return 0
