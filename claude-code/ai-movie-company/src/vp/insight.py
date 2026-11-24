"""Build consent-gated insight cards from closed fields and a filtered process note."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from . import telemetry

EDIT_KINDS = frozenset("""
cut_moved segment_removed segment_added segment_reordered segment_trimmed
caption_text_changed caption_restyled caption_removed caption_added
music_volume_changed voice_volume_changed music_replaced music_removed
transition_added transition_removed effect_added speed_changed canvas_changed
text_fix fragment_swapped fragment_dropped fragment_added sketch_redone voice_changed
music_changed style_changed length_changed other
""".split())
REASONS = frozenset("""
pacing wrong_moment too_long too_short caption_error caption_look music_level music_choice
taste technical_defect unknown
""".split())
OUTCOMES = frozenset({"accepted", "redone", "abandoned"})
LIST_FIELDS = {"edit": (EDIT_KINDS, 8), "reason": (REASONS, 4)}
NOTE_LIMIT = 280
FIELDS = frozenset({"ts", "kind", "route", "step", "scenario", "run", "case",
                    "edit", "reason", "outcome", "redos", "note"})

_REDACTION = "…"
_WORD = r"[^\W\d_]+"
_WORDS = re.compile(_WORD)
_TOKEN = re.compile(rf"{_WORD}|…|[.!?,;:]|[^\s]", re.UNICODE)
_QUOTES = re.compile(
    r"""«[^»]*(?:»|$)|“[^”]*(?:”|$)|"(?:\\.|[^"\\])*(?:"|\\?$)|`[^`]*(?:`|$)"""
    r"""|(?<!\w)'(?:\\.|[^'\\]|'(?=\w))*(?:'(?!\w)|\\?$)|‘[^’]*(?:’|$)"""
)
_CHUNK = re.compile(r"\S+")
_LOCATOR = re.compile(r"[/\\@]|[\w-]\.[\w-]")
_NUMBERS = re.compile(r"(?<!\w)\+?\(?\d[\d(). -]*\d|\d{5,}")
_SENTENCE_END = frozenset(".!?")
_PUNCTUATION = _SENTENCE_END | {",", ";", ":"}
_TRAILING_PUNCTUATION = ".!?,;:"
_PLUGIN = Path(__file__).resolve().parents[2]


def _stems(text: str) -> set[str]:
    return {word.lower()[:5] for word in _WORDS.findall(text)}


@lru_cache(maxsize=1)
def vocabulary() -> frozenset[str]:
    words: set[str] = set()
    for pattern in ("skills/**/*.md", "commands/*.md", "src/vp/knowledge/*.json",
                    "src/vp/kpack/baseline/*.json"):
        for path in _PLUGIN.glob(pattern):
            if path.is_file():
                words.update(_stems(path.read_text(encoding="utf-8")))
    return frozenset(words)


def job_names(job: Path) -> set[str]:
    """Use path names and explicit titles. Do not follow links or enter the journal."""
    from .core.io import read_yaml

    names = _stems(job.name)
    names.update(_stems(job.resolve().name))

    def unreadable(exc: OSError) -> None:
        raise exc

    for _, dirs, files in os.walk(job, onerror=unreadable, followlinks=False):
        dirs[:] = [name for name in dirs if name.lower() != "journal"]
        for name in dirs + files:
            names.update(_stems(name))
    for filename in ("request.yaml", "brief.yaml", "brief.md"):
        path = job / filename
        if path.is_symlink() or not path.is_file():
            continue
        if filename == "brief.md":
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    heading = re.match(r"\s*(?:#{1,6}\s+|(?:title|название):\s*)(.+)",
                                       line, re.IGNORECASE)
                    if heading:
                        names.update(_stems(heading[1]))
            continue
        data = read_yaml(path, default={})
        if isinstance(data, dict):
            for key in ("title", "channel_title", "channel_name"):
                if isinstance(data.get(key), str):
                    names.update(_stems(data[key]))
    return names


def filter_note(note: Any, job: Path | None = None) -> str | None:
    """Keep only release vocabulary, with the job's names removed when a job is supplied."""
    if not isinstance(note, str) or not note.strip():
        return None
    try:
        words = vocabulary()
        blocked = _stems(Path.home().name)
        if job is not None:
            blocked.update(job_names(job))
    except (OSError, ValueError, RuntimeError, yaml.YAMLError):
        return None
    # Keep a redacted quote inside its original URL or path token so the whole locator is cut.
    note = _QUOTES.sub(_REDACTION, note)

    def cut_locator(match: re.Match[str]) -> str:
        chunk = match[0]
        if not _LOCATOR.search(chunk):
            return chunk
        return _REDACTION + chunk[len(chunk.rstrip(_TRAILING_PUNCTUATION)):]

    note = _CHUNK.sub(cut_locator, note)
    note = _NUMBERS.sub(lambda m: f" {_REDACTION} " if sum(c.isdecimal() for c in m[0]) >= 5
                        else m[0], note)

    chunks: list[str] = []
    sentence_start = True
    cuts = kept_words = 0
    size = 0
    for token in _TOKEN.findall(note):
        if token in _PUNCTUATION:
            if token in _SENTENCE_END:
                sentence_start = True
        else:
            stem = token.lower()[:5]
            if stem not in words or stem in blocked or (not sentence_start and token[0].isupper()):
                token = _REDACTION
            sentence_start = False
        prefix = " " if chunks and token not in _PUNCTUATION else ""
        if size + len(token) + len(prefix) > NOTE_LIMIT:
            break
        if token == _REDACTION:
            cuts += 1
        elif token not in _PUNCTUATION:
            kept_words += 1
        if token == _REDACTION and chunks and chunks[-1].endswith(_REDACTION):
            continue
        chunk = prefix + token
        size += len(chunk)
        chunks.append(chunk)
    if not kept_words or cuts > kept_words:
        return None
    return "".join(chunks)


def sanitize_fields(rec: dict[str, Any], job: Path | None) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    case = rec.get("case")
    if isinstance(case, str):
        from . import kpack

        try:
            cases = kpack.load()["cases"]
        except (OSError, ValueError):
            cases = ()
        if any(case == entry["id"] for entry in cases):
            fields["case"] = case
    for key, (choices, limit) in LIST_FIELDS.items():
        value = rec.get(key)
        if isinstance(value, list):
            kept = [v for v in value if isinstance(v, str) and v in choices][:limit]
            if kept:
                fields[key] = kept
    outcome = rec.get("outcome")
    if isinstance(outcome, str) and outcome in OUTCOMES:
        fields["outcome"] = outcome
    redos = rec.get("redos")
    if type(redos) is int and 0 <= redos <= 99:
        fields["redos"] = redos
    note = filter_note(rec.get("note"), job)
    if note:
        fields["note"] = note
    return fields


def add(job: Path, **data: Any) -> bool:
    if not telemetry.insights_allowed() or not telemetry.can_send() or not job.is_dir():
        return False
    from . import runsignal

    event = telemetry.make_event("insight", {**data, **runsignal.fields(job)}, job=job)
    if event is None:
        return False
    # Keep an identical local copy. Hold its lock until the spool accepts the card so a
    # failed enqueue can roll back only this process's append.
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(job / "insights.jsonl", flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size
            line = (telemetry.event_line(event) + "\n").encode("utf-8")
            recorded = False
            try:
                recorded = os.write(fd, line) == len(line) and telemetry.append_event(event)
                return recorded
            finally:
                if not recorded:
                    os.ftruncate(fd, size)
        finally:
            os.close(fd)
    except OSError:
        return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="vp insight", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("add")
    record.add_argument("job", type=Path)
    record.add_argument("--step", required=True)
    record.add_argument("--case")
    record.add_argument("--edit", action="append", metavar="KIND",
                        help="Repeat up to 8 times. Kinds include " + ", ".join(sorted(EDIT_KINDS)))
    record.add_argument("--reason", action="append", metavar="CODE",
                        help="Repeat up to 4 times. Codes include " + ", ".join(sorted(REASONS)))
    record.add_argument("--outcome", choices=sorted(OUTCOMES))
    record.add_argument("--redos", type=int, metavar="0-99")
    record.add_argument("--note", help="Describe the process in general terms. Filtered locally.")
    record.add_argument("--json", action="store_true")
    preview = sub.add_parser("preview")
    preview.add_argument("--note", required=True)
    preview.add_argument("--job", type=Path)
    args = parser.parse_args(argv)
    if args.command == "preview":
        print(filter_note(args.note, args.job.expanduser() if args.job else None) or "")
        if not telemetry.insights_allowed():
            print("(not recorded and not sent: insight cards start with the new terms)")
        return 0
    recorded = add(args.job.expanduser(), step=args.step, case=args.case, edit=args.edit,
                   reason=args.reason, outcome=args.outcome, redos=args.redos, note=args.note)
    print(json.dumps({"recorded": recorded}) if args.json else
          ("Insight recorded." if recorded else "No insight was recorded."))
    return 0
