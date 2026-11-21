"""The dialog journal (K6, K27): what the person said, where they got stuck, one-step feedback.

Two sources, both JSON lines on the person's disk, nothing sent anywhere:
  * typed prompts of Elsa's sessions only — written by the hook hooks/journal_prompt.py to
    <VP_HOME>/journal/<date>.jsonl ({"at", "session_id", "kind": "prompt", "cwd", "text"}); a
    session is Elsa's once a prompt calls her (a plugin command or her name) or the engine runs a
    pipeline command in it (`mark_session`, from vp.cli);
  * tagged notes — `vp journal note`, written by the director when something happens worth
    learning from: `deadend` (stuck, an error, gave up, irritation, "не понимаю"), `surprise`
    (a request the plugin did not foresee — a new scenario), `feedback` (the person's opinion).
    With a job: <job>/journal.jsonl; without (`-`): the global day file.

`vp feedback pack` gathers the last 14 days (global journal + notes of every registered project +
`vp doctor` + versions) into ONE Markdown file to send to the owner:
~/Movies/AI Movie Company/Feedback/Elsa-feedback-<date>.md (<VP_HOME>/feedback/ when there is no
~/Movies). Not the Desktop: on macOS the Desktop is privacy-protected and the first write there pops
"Claude would like to access files in your Desktop folder"; ~/Movies is not protected, and the text
projects already live in ~/Movies/AI Movie Company. `--reveal` then shows the file selected in a
Finder window (`open -R`), so the person drags it into a message without looking for it. It never reads .env or token files, and everything that looks like a key or a token is
replaced by [redacted].

CLI:  vp journal note <job|-> --kind deadend|surprise|feedback --text "..." [--json]
      vp journal show [--days N] [--job J] [--json]
      vp feedback pack [--days N] [--out DIR] [--reveal] [--json]
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import platform
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .core.errors import VPError
from .core.paths import vp_home

NOTE_KINDS = ("deadend", "surprise", "feedback")
KINDS = ("prompt",) + NOTE_KINDS
JOB_FILE = "journal.jsonl"
PACK_DAYS = 14
MAX_NOTE = 4000

# keep in sync with hooks/journal_prompt.py SECRET_PATTERNS (a test compares them)
SECRET_PATTERNS = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}",
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd|client[_-]?secret|refresh[_-]?token|"
    r"access[_-]?token|authorization)\b(\s*[:=]\s*)[\"']?[^\s\"']{6,}",
    r"\bsk[-_][A-Za-z0-9_-]{16,}",
    r"\bxi-[A-Za-z0-9]{16,}",
    r"\bAIza[0-9A-Za-z_-]{30,}",
    r"\bya29\.[0-9A-Za-z._-]{20,}",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\b\d{8,10}:[A-Za-z0-9_-]{30,}",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}\b",
)
_COMPILED = [re.compile(p) for p in SECRET_PATTERNS]


def redact(text: str) -> str:
    """Replace everything that looks like a key, token or password by [redacted]."""
    for rx in _COMPILED:
        if rx.groups >= 2:
            text = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
            text = rx.sub("[redacted]", text)
    return text


def journal_dir(home: Path | None = None) -> Path:
    return (home or vp_home()) / "journal"


# Elsa's sessions: the prompt hook records only these (keep in sync with hooks/journal_prompt.py,
# a test compares them)
SESSIONS_FILE = "elsa-sessions.json"
MAX_SESSIONS = 200


def elsa_sessions(home: Path | None = None) -> list[str]:
    try:
        data = json.loads((journal_dir(home) / SESSIONS_FILE).read_text(encoding="utf-8"))
        got = data.get("sessions") if isinstance(data, dict) else None
        return [s for s in got if isinstance(s, str)] if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def mark_session(session_id: str | None, home: Path | None = None) -> bool:
    """Mark a Claude Code session as Elsa's (the engine ran a pipeline command in it), so the
    prompt hook journals its prompts from now on. Never raises; True when newly marked."""
    sid = (session_id or "").strip()[:200]
    if not sid:
        return False
    try:
        ids = elsa_sessions(home)
        if sid in ids:
            return False
        ids = (ids + [sid])[-MAX_SESSIONS:]
        path = journal_dir(home) / SESSIONS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"sessions": ids}), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 - bookkeeping must never break a command
        return False


def _now() -> datetime:
    return datetime.now().astimezone()


def _append(path: Path, rec: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def note(job: str | None, kind: str, text: str, now: datetime | None = None) -> dict[str, Any]:
    """Append a tagged event; job-scoped when `job` is a job folder, else global ('-' / None)."""
    if kind not in NOTE_KINDS:
        raise VPError("data", f"kind must be one of {', '.join(NOTE_KINDS)}")
    text = redact((text or "").strip())
    if not text:
        raise VPError("data", "the note text is empty")
    if len(text) > MAX_NOTE:
        text = text[:MAX_NOTE] + " […]"
    now = now or _now()
    rec: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "kind": kind, "text": text}
    if job and job != "-":
        folder = Path(job).expanduser().resolve()
        if not (folder / "request.yaml").is_file():
            raise VPError("data", f"no job in {folder} (request.yaml missing); use '-' for a global note")
        rec["job"] = str(folder)
        path = _append(folder / JOB_FILE, rec)
    else:
        path = _append(journal_dir() / f"{now.strftime('%Y-%m-%d')}.jsonl", rec)
    # local only (owner, alpha 2026-11-20): a note is free text about the person; it is never
    # sent automatically — it goes into `vp feedback pack`, which the person sends themselves
    return {**rec, "file": str(path)}


def _read_lines(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("at"):
                    yield rec
    except OSError:
        return


def _parse_at(at: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(at))
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def events(days: int = 3, job: str | None = None, home: Path | None = None,
           today: date | None = None, all_projects: bool = True) -> list[dict[str, Any]]:
    """Journal events of the last `days` days (today included), oldest first: the global day
    files, plus the notes of `job` (or of every registered project when all_projects)."""
    today = today or _now().date()
    since = today - timedelta(days=max(1, days) - 1)
    out: list[dict[str, Any]] = []
    jd = journal_dir(home)
    if jd.is_dir():
        for p in sorted(jd.glob("*.jsonl")):
            try:
                day = date.fromisoformat(p.stem)
            except ValueError:
                continue
            if since <= day <= today:
                out += list(_read_lines(p))
    folders: list[Path] = []
    if job and job != "-":
        folders.append(Path(job).expanduser().resolve())
    elif all_projects:
        from .projects import _load
        folders += [Path(f) for f in _load(home)["projects"]]
    for folder in folders:
        for rec in _read_lines(folder / JOB_FILE):
            d = _parse_at(rec["at"])
            if d and since <= d.date() <= today:
                rec.setdefault("job", str(folder))
                out.append(rec)
    out.sort(key=lambda r: (_parse_at(r["at"]) or datetime.min.astimezone()))
    return out


def _one_line(text: str, limit: int = 200) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= limit else t[:limit - 1] + "…"


def digest_lines(evts: list[dict[str, Any]], limit: int = 200) -> list[str]:
    if not evts:
        return ["(the journal is empty for this period)"]
    counts = {k: sum(1 for e in evts if e.get("kind") == k) for k in KINDS}
    lines = ["prompts: {prompt}, dead ends: {deadend}, surprises: {surprise}, "
             "feedback: {feedback}".format(**counts)]
    day = None
    for e in evts:
        d = _parse_at(e["at"])
        dd = d.strftime("%Y-%m-%d") if d else "?"
        if dd != day:
            day = dd
            lines.append(f"== {dd}")
        tag = e.get("kind") or "?"
        mark = "" if tag == "prompt" else "* "
        job = f" [{Path(e['job']).name}]" if e.get("job") else ""
        lines.append(f"{d.strftime('%H:%M') if d else '??:??'}  {mark}{tag:<8}{job} "
                     f"{_one_line(e.get('text', ''), limit)}")
    return lines


# ---- feedback pack -------------------------------------------------------------------------

def _plugin_version() -> str | None:
    for root in Path(__file__).resolve().parents:
        p = root / ".claude-plugin" / "plugin.json"
        if p.is_file():
            try:
                return str(json.loads(p.read_text(encoding="utf-8")).get("version"))
            except (OSError, ValueError):
                return None
    return None


def _cmd_version(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return "not found"
    out = (p.stdout or p.stderr or "").strip().splitlines()
    return out[0][:160] if out else f"exit {p.returncode}"


def versions() -> dict[str, str]:
    from .adapters import capcut, media
    return {"plugin": _plugin_version() or "unknown",
            "python": sys.version.split()[0],
            "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "ffmpeg": _cmd_version([media.FFMPEG, "-hide_banner", "-version"]),
            "node": _cmd_version(["node", "--version"]),
            "capcut": capcut.capcut_version() or "not found"}


def doctor_text() -> str:
    """`vp doctor` output, captured (doctor never prints secret contents)."""
    from . import doctor
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            doctor.main([])
    except Exception as e:  # noqa: BLE001 - the pack is written even when doctor breaks
        buf.write(f"\n(doctor failed: {type(e).__name__}: {e})\n")
    return buf.getvalue()


def feedback_dir() -> Path | None:
    """~/Movies/AI Movie Company/Feedback — outside macOS's protected folders (Desktop, Documents,
    Downloads), next to the text projects; None when there is no ~/Movies."""
    movies = Path.home() / "Movies"
    return movies / "AI Movie Company" / "Feedback" if movies.is_dir() else None


def reveal(path: Path) -> bool:
    """Show the file selected in a Finder window (macOS `open -R`); False when it cannot."""
    if sys.platform != "darwin":
        return False
    try:
        return subprocess.run(["/usr/bin/open", "-R", str(path)], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _free_path(d: Path, stem: str) -> Path:
    p, n = d / f"{stem}.md", 2
    while p.exists():
        p, n = d / f"{stem}-{n}.md", n + 1
    return p


def build_pack(days: int = PACK_DAYS, today: date | None = None,
               doctor: str | None = None, vers: dict[str, str] | None = None) -> str:
    today = today or _now().date()
    evts = events(days, today=today)
    from .projects import list_projects
    projects = list_projects()
    notes = [e for e in evts if e.get("kind") in NOTE_KINDS]
    v = vers if vers is not None else versions()
    lines = [f"# Elsa feedback — {today.isoformat()}", "",
             f"Period: the last {days} days. Prompts: {sum(1 for e in evts if e.get('kind') == 'prompt')}; "
             f"notes: {len(notes)}. Keys and tokens are replaced by [redacted].", "",
             "## Versions", ""]
    lines += [f"- {k}: {val}" for k, val in v.items()]
    lines += ["", "## Dead ends, surprises, feedback", ""]
    if notes:
        for e in notes:
            job = f" ({Path(e['job']).name})" if e.get("job") else ""
            lines.append(f"- {e['at']} **{e['kind']}**{job}: {_one_line(e['text'], 1000)}")
    else:
        lines.append("(none recorded)")
    lines += ["", "## Projects", ""]
    lines += [f"- {p['last_activity'] or '?'} — {p['state']} — {p['kind']} — {p['title']}"
              for p in projects] or ["(none)"]
    lines += ["", "## Dialog journal", "", "```"]
    lines += digest_lines(evts, limit=600)
    lines += ["```", "", "## Doctor", "", "```"]
    lines += (doctor if doctor is not None else doctor_text()).rstrip().splitlines()
    lines += ["```", ""]
    return redact("\n".join(lines))


def feedback_pack(days: int = PACK_DAYS, out_dir: str | Path | None = None,
                  today: date | None = None, doctor: str | None = None,
                  vers: dict[str, str] | None = None) -> Path:
    today = today or _now().date()
    d = Path(out_dir).expanduser() if out_dir else (feedback_dir() or vp_home() / "feedback")
    d.mkdir(parents=True, exist_ok=True)
    path = _free_path(d, f"Elsa-feedback-{today.isoformat()}")
    path.write_text(build_pack(days, today, doctor, vers), encoding="utf-8")
    return path


# ---- CLI ------------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp journal", description="the dialog journal")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("note", help="append a tagged event")
    n.add_argument("job", help="job folder, or - for a global note")
    n.add_argument("--kind", required=True, choices=NOTE_KINDS)
    n.add_argument("--text", required=True)
    n.add_argument("--json", action="store_true")
    s = sub.add_parser("show", help="a readable digest of the journal")
    s.add_argument("--days", type=int, default=3)
    s.add_argument("--job")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "note":
        rec = note(a.job, a.kind, a.text)
        print(json.dumps(rec, ensure_ascii=False) if a.json else f"NOTED {rec['kind']}\t{rec['file']}")
        return 0
    evts = events(a.days, job=a.job)
    if a.json:
        print(json.dumps(evts, ensure_ascii=False, indent=1))
    else:
        print("\n".join(digest_lines(evts)))
    return 0


def feedback_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp feedback", description="feedback for the plugin's owner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack", help="one Markdown file with the journal, notes, doctor, versions")
    p.add_argument("--days", type=int, default=PACK_DAYS)
    p.add_argument("--out", help="folder for the file (default: ~/Movies/AI Movie Company/Feedback)")
    p.add_argument("--reveal", action="store_true", help="show the file in a Finder window (macOS)")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    path = feedback_pack(a.days, a.out)
    shown = reveal(path) if a.reveal else False
    from . import telemetry
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    telemetry.record("feedback_pack", bytes=size, days=a.days, revealed=shown)   # no path
    print(json.dumps({"file": str(path), "revealed": shown}, ensure_ascii=False) if a.json
          else str(path))
    return 0
