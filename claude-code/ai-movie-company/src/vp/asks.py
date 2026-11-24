"""`asks ...` — the ledger of the person's requests and feedback (owner need 2026-11-19).

Nothing the person asked may silently get lost: every request goes into the ledger in the
person's own words and carries a status until it is done — or the reason why not is written.

  asks add <job|-> --text "..." [--decision]        record a request or agreed decision; prints its id.
                                                    The same text as a still-open ask -> the
                                                    existing id, nothing new is written.
  asks set <job|-> <id> --status S [--note ...] [--evidence ...] [--agreed] [--replaced-by <id>]
                                                    change the status; S is one of STATUSES.
  asks list <job|-> [--open]                        one line per ask: id, status, text.

`<job>` is a job folder (ledger `<job>/asks.yaml`); `-` is the pipeline home (requests not tied
to a job). asks.yaml is the source of truth, written only by this code (atomic write, history
appended, nothing ever deleted); asks.md next to it is regenerated after every write for people.

Rules (code, not judgment): `done` needs --evidence (a file, a step, a fact that shows it);
`not_done` and `dropped` need --note with the reason; `dropped` also needs --agreed — the note
must say that the person agreed to drop it.
`superseded` needs --replaced-by with another ask in the same ledger.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
from pathlib import Path
from typing import Any, Iterator

from .core.io import atomic_write, now_iso, read_yaml, write_yaml
from .core.paths import Job, vp_home
from .gates._common import RC_OK, Refused, guarded

STATUSES = ("open", "in_progress", "done", "not_done", "dropped", "superseded")
CLOSED = ("done", "dropped", "superseded")  # everything else is still owed to the person
LABELS = {"open": "открыто", "in_progress": "в работе", "done": "выполнено",
          "not_done": "не выполнено", "dropped": "снято по согласию",
          "superseded": "заменено"}
VERSION_REMINDER = ("Review the asks for this version with vp asks list <job> --open "
                    "and record status changes with vp asks set.")


class Ledger:
    """asks.yaml + asks.md of one folder (a job folder or the pipeline home)."""

    def __init__(self, target: str):
        if target == "-":
            self.root, self.title = vp_home(), "без привязки к проекту"
            self.root.mkdir(parents=True, exist_ok=True)
        else:
            root = Path(target).expanduser().resolve()
            if not root.is_dir():
                raise Refused(f"no such job folder: {target}")
            job = Job(root)
            self.root, self.title = job.root, job.job_id
        self.yaml = self.root / "asks.yaml"
        self.md = self.root / "asks.md"

    def load(self) -> list[dict[str, Any]]:
        data = read_yaml(self.yaml, default={})
        asks = data.get("asks") if isinstance(data, dict) else None
        return list(asks or [])

    def save(self, asks: list[dict[str, Any]]) -> None:
        write_yaml(self.yaml, {"asks": asks})
        atomic_write(self.md, render_md(self.title, asks))

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize read-modify-write. A separate lock from job.lock: requests must be recorded
        even while a pipeline run holds the job."""
        with open(self.root / ".asks.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def _clean(value: str | None) -> str:
    return " ".join(str(value or "").split())


def _next_id(asks: list[dict[str, Any]]) -> str:
    nums = [int(a["id"][1:]) for a in asks if str(a.get("id", "")).startswith("A")
            and str(a["id"])[1:].isdigit()]
    return f"A{max(nums, default=0) + 1}"


def add(ledger: Ledger, text: str, at: str | None = None, *,
        decision: bool = False) -> tuple[str, bool]:
    """Record a request or agreed decision verbatim, reusing an identical open entry."""
    words = str(text or "").strip()
    if not words:
        raise Refused("--text is empty: write the person's words")
    with ledger.locked():
        asks = ledger.load()
        for a in asks:
            if (a.get("status") not in CLOSED and a.get("decision", False) == decision
                    and str(a.get("text", "")).strip() == words):
                return a["id"], False
        at = at or now_iso()
        entry = {"id": _next_id(asks), "text": words, "at": at, "status": "open", "note": "",
                 "evidence": "", "history": [{"at": at, "status": "open"}]}
        if decision:
            entry["decision"] = True
        asks.append(entry)
        ledger.save(asks)
        return entry["id"], True


def set_status(ledger: Ledger, ask_id: str, status: str, note: str | None = None,
               evidence: str | None = None, agreed: bool = False,
               at: str | None = None, *, replaced_by: str | None = None) -> dict[str, Any]:
    """Change the status of one ask under the ledger rules; returns the updated entry."""
    if status not in STATUSES:
        raise Refused(f"status must be one of {', '.join(STATUSES)}")
    note, evidence = _clean(note), _clean(evidence)
    if status == "done" and not evidence:
        raise Refused("done needs --evidence: what shows it is done (a file, a step, a fact)")
    if status in ("not_done", "dropped") and not note:
        raise Refused(f"{status} needs --note with the reason")
    if status == "dropped" and not agreed:
        raise Refused("dropped needs --agreed: only when the person agreed to drop it "
                      "(say so in --note)")
    if status == "superseded" and not _clean(replaced_by):
        raise Refused("superseded needs --replaced-by with the replacement ask id")
    if status != "superseded" and replaced_by is not None:
        raise Refused("--replaced-by is only for superseded asks")
    with ledger.locked():
        asks = ledger.load()
        by_id = {a.get("id"): a for a in asks}
        ask_id = str(ask_id).strip().upper()
        entry = by_id.get(ask_id)
        if entry is None:
            known = ", ".join(str(a.get("id")) for a in asks) or "-"
            raise Refused(f"unknown ask {ask_id!r}; asks: {known}")
        change: dict[str, Any] = {"at": at or now_iso(), "status": status}
        if status == "superseded":
            replacement = _clean(replaced_by).upper()
            if replacement not in by_id:
                raise Refused(f"unknown replacement ask {replacement!r}")
            seen = {ask_id}
            current = replacement
            while current in by_id:
                if current in seen:
                    raise Refused("the replacement would create an ask cycle")
                seen.add(current)
                next_id = by_id[current].get("replaced_by")
                if not next_id:
                    break
                current = next_id
            entry["replaced_by"] = change["replaced_by"] = replacement
        else:
            entry.pop("replaced_by", None)
        if note:
            entry["note"] = change["note"] = note
        if evidence:
            entry["evidence"] = change["evidence"] = evidence
        if status == "dropped":
            change["agreed"] = True
        entry["status"] = status
        entry.setdefault("history", []).append(change)
        ledger.save(asks)
        return entry


def open_asks(asks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in asks if a.get("status") not in CLOSED]


def render_md(title: str, asks: list[dict[str, Any]]) -> str:
    """Human view of the ledger (Russian: the person reads it)."""
    left = open_asks(asks)
    lines = [f"# Поручения: {title}", "",
             f"Всего: {len(asks)}, ещё не закрыто: {len(left)}. "
             "Файл обновляется сам; правится только через режиссёра.", ""]
    for a in asks:
        lines.append(f"## {a['id']} — {LABELS.get(a.get('status'), a.get('status'))}")
        quote = [f"> {line}".rstrip() for line in str(a.get("text", "")).splitlines()] or [">"]
        lines += ["", *quote, "", f"- записано: {a.get('at', '')}"]
        if a.get("evidence"):
            lines.append(f"- чем подтверждено: {a['evidence']}")
        if a.get("note"):
            lines.append(f"- пояснение: {a['note']}")
        if a.get("decision"):
            lines.append("- согласованное решение")
        if a.get("replaced_by"):
            lines.append(f"- заменено поручением {a['replaced_by']}")
        lines.append("")
    if not asks:
        lines += ["Поручений пока нет.", ""]
    return "\n".join(lines)


def report_lines(job: Job) -> list[str]:
    """Report section for S11; [] when the job has no ledger."""
    if not job.asks_yaml.exists():
        return []
    data = read_yaml(job.asks_yaml, default={})
    asks = (data.get("asks") if isinstance(data, dict) else None) or []
    left = open_asks(asks)
    lines = ["", "## Поручения", ""]
    if not left:
        return lines + ["- открытых поручений нет"]
    for a in left:
        why = f" — {a['note']}" if a.get("note") else ""
        text = " ".join(str(a.get("text", "")).split())
        lines.append(f"- {a['id']} ({LABELS.get(a.get('status'), a.get('status'))}): {text}{why}")
    return lines


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vp asks", description="ledger of the person's requests")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("job", help="job folder, or - for requests not tied to a job")
    s.add_argument("--text", required=True, help="the person's words, verbatim")
    s.add_argument("--decision", action="store_true", help="record an agreed decision as an ask")
    s = sub.add_parser("set")
    s.add_argument("job")
    s.add_argument("id")
    s.add_argument("--status", required=True, choices=STATUSES)
    s.add_argument("--note")
    s.add_argument("--evidence")
    s.add_argument("--agreed", action="store_true", help="the person agreed (needed for dropped)")
    s.add_argument("--replaced-by", help="replacement ask id (needed for superseded)")
    s = sub.add_parser("list")
    s.add_argument("job")
    s.add_argument("--open", action="store_true", help="only asks still owed")
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    def body() -> int:
        ledger = Ledger(args.job)
        if args.cmd == "add":
            ask_id, created = add(ledger, args.text, decision=args.decision)
            print(ask_id if created else f"{ask_id} (already recorded, still open)")
        elif args.cmd == "set":
            e = set_status(ledger, args.id, args.status, args.note, args.evidence, args.agreed,
                           replaced_by=args.replaced_by)
            print(f"{e['id']}\t{e['status']}\t{' '.join(str(e['text']).split())}")
        else:
            asks = ledger.load()
            shown = open_asks(asks) if args.open else asks
            for a in shown:
                print(f"{a['id']}\t{a['status']}\t{' '.join(str(a['text']).split())}")
            if not shown:
                print("no open asks" if args.open else "no asks")
        return RC_OK
    return guarded(body)
