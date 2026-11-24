"""Read the position of a job without running the pipeline or opening a gate."""
from __future__ import annotations

import os
import shlex
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from . import runsignal, safety, scenarios
from .core import progress
from .core.config import DEFAULTS, config_path, deep_merge, resolve_job_config
from .core.errors import VPError
from .core.graph import Graph, Instance, StepSpec
from .core.io import parse_iso, read_json, read_yaml, sha256_file
from .core.judge import pending_tasks
from .core.lock import holder
from .core.manifest import read_manifest
from .core.paths import Job
from .core.status import Status
from .steps import GRAPH
from .steps._package import current_decision, load_approval, read_package
from .steps.s0_intake import NEED_TRANSCRIPT

_GRAPH = Graph(StepSpec(id=step, deps=deps, scope=scope, gate=gate)
               for step, (deps, scope, gate) in GRAPH.items())
_COMPLETE = ("done", "skipped")
_READ_ERRORS = (OSError, ValueError, TypeError, KeyError, yaml.YAMLError, VPError)


def _command(verb: str, job: Job) -> str:
    return f"vp {verb} {shlex.quote(str(job.root))}"


def run_command(job: Job) -> str:
    return _command("run", job)


def report_path(job: Job) -> str | None:
    return str(job.report_md) if job.report_md.is_file() else None


def _sentence(text: Any) -> str:
    line = " ".join(str(text).split())
    return line if line.endswith((".", "!", "?")) else f"{line}."


def _time(value: Any) -> datetime | None:
    try:
        return parse_iso(str(value))
    except ValueError:
        return None


def _latest(*values: Any) -> str | None:
    times = [parsed for value in values if (parsed := _time(value)) is not None]
    return max(times).isoformat() if times else None


@lru_cache(maxsize=1)
def _crosswalk() -> dict:
    return read_json(Path(__file__).with_name("knowledge") / "position_map.json")


def _library_step(engine: str, whole: bool) -> str | None:
    crosswalk = _crosswalk()
    if whole and engine in crosswalk["whole_recording"]:
        return crosswalk["whole_recording"][engine]
    return crosswalk["engine"].get(engine)


@dataclass
class Moment:
    instance: Instance | None
    state: str
    action: str
    text: str
    command: str | None = None
    since: str | None = None
    waiting_for: dict | None = None
    step: str | None = None


def _wait(inst: Instance | None, state: str, code: str, why: str, action: str,
          text: str, command: str | None, since: str | None, *, step: str | None = None) -> Moment:
    return Moment(inst, state, action, text, command, since,
                  {"code": code, "text": _sentence(why)}, step)


def _ready(job: Job, inst: Instance, since: str | None = None, state: str = "ready") -> Moment:
    return Moment(inst, state, "run", f"Run the job at {inst.key}.", run_command(job), since)


def _approval_state(job: Job) -> tuple[list[str], list[str], list[dict]]:
    """The offered packages without a current decision, outdated packages, and decisions."""
    review = read_yaml(job.review_yaml, default={})
    rows = review.get("items") if isinstance(review, dict) else None
    if not isinstance(rows, dict) or not rows:
        return [], [], []
    approvals = load_approval(job)
    pending, outdated, decided = [], [], []
    for item, row in rows.items():
        if not isinstance(row, dict) or row.get("status") != "ok":
            continue
        pkg = read_package(job.package(item)) or {}
        package_hash = pkg.get("package_hash")
        if not package_hash or row.get("package_hash") != package_hash:
            outdated.append(item)
        elif decision := current_decision(approvals, item, package_hash):
            decided.append(decision)
        else:
            pending.append(item)
    return pending, outdated, decided


def approval_complete(job: Job) -> bool:
    try:
        pending, outdated, decided = _approval_state(job)
    except _READ_ERRORS:
        return False
    return bool(decided and not pending and not outdated)


def _review_status(job: Job, fallback: str, since: str | None) -> tuple[str | None, str | None]:
    """Return the remaining work and, when valid and closed, the latest review edit time."""
    if not job.fragments_review.is_file():
        return fallback, since
    try:
        # load_config() would create config.yaml when absent. Review validation needs only
        # the profiles, which use the same on-disk YAML and resolver as the runner.
        config = read_yaml(config_path(), default={})
        if not isinstance(config, dict):
            return fallback, since
        cfg = resolve_job_config(deep_merge(DEFAULTS, config), job.request(), job.design())
        if not isinstance(cfg["profile"], dict):
            return fallback, since
    except _READ_ERRORS:
        return fallback, since

    from .steps._fragments import load_timeline
    from .steps.s3b_review import check_review

    try:
        proposed = read_yaml(job.fragments_proposed, default=None)
        if not isinstance(proposed, dict) or "fragments" not in proposed:
            return "The proposed fragments are missing or malformed", since
        timeline = load_timeline(job, proposed.get("boundary_mode"))
        doc = read_yaml(job.fragments_review)
        checked = check_review(doc, proposed, sha256_file(job.fragments_proposed),
                               timeline, cfg["profile"])
        if checked.errors:
            error = checked.errors[0]
            return f"{error['where']}: {error['message']}", since
        if checked.open == ["no_fragments_confirmed"]:
            return "Confirm that there are no fragments to keep", since
        if checked.open:
            count = len(checked.open)
            return ("One fragment still needs a verdict" if count == 1 else
                    f"{count} fragments still need a verdict"), since
        edited = datetime.fromtimestamp(job.fragments_review.stat().st_mtime, timezone.utc)
        return None, _latest(since, checked.finished_at or edited.isoformat())
    except _READ_ERRORS as e:
        return f"The fragment review could not be validated: {e}", since


def _gate(job: Job, inst: Instance, meta: dict, since: str | None) -> Moment:
    what = meta.get("what")
    if meta.get("gate") == "consent":
        try:
            consent = safety.consent_state()
        except _READ_ERRORS:
            consent = {}
        if consent.get("accepted"):
            return _ready(job, inst, _latest(since, consent.get("accepted_at")))
        return _wait(inst, "blocked", "terms_not_accepted",
                     what or "The current terms still need to be accepted", "fix",
                     "Accept the terms in the dialog first.", None, since)
    if inst.step == "S3b":
        why, since = _review_status(job, what or "The fragment review needs your decisions", since)
        if why is None:
            return _ready(job, inst, since)
        return _wait(inst, "waiting_person", "fragment_review", why, "review",
                     "Open the fragment review and record your decisions.",
                     _command("review show", job), since)
    if inst.step in ("S9", "PUB"):
        pending, outdated, decided = _approval_state(job)
        if pending:
            why = ("One video still needs a publishing decision" if len(pending) == 1 else
                   f"{len(pending)} videos still need a publishing decision")
        elif outdated or decided:
            closed = _latest(*(decision.get("decided_at") for decision in decided))
            return _ready(job, inst, _latest(since, closed))
        else:
            why = what or "The publishing packages need your approval"
        return _wait(inst, "waiting_person", "publish_approval", why, "approve",
                     "Open the publishing approval and record your decisions.",
                     _command("approve show", job), since)
    return _wait(inst, "blocked", "unknown_reason", what or "The job has a recorded stop",
                 "fix", "Check the recorded prerequisite before resuming.", None, since)


def _blocked(job: Job, inst: Instance, meta: dict, since: str | None) -> Moment:
    why = meta.get("message")
    if meta.get("reason") == "youtube_quota":
        return _wait(inst, "blocked", "youtube_quota", why or "The YouTube quota is exhausted",
                     "fix", "Wait for the YouTube quota to reset before running the job.",
                     None, since)
    if inst.step == "S0" and str(why).startswith(f"{NEED_TRANSCRIPT}:"):
        if job.source_srt.is_file():
            return _ready(job, inst)
        return _wait(inst, "blocked", "missing_transcript", str(why).partition(":")[2].strip(),
                     "transcribe", "Get a transcription estimate or import existing subtitles.",
                     _command("transcribe estimate", job), since, step="transcribe")
    if inst.step == "S0" and str(why).startswith("route disabled:"):
        if job.request().get("enabled") is True:
            return _ready(job, inst)
        return _wait(inst, "blocked", "route_disabled", why, "fix",
                     "Enable the recording route in request.yaml before running the job.",
                     None, since)
    return _wait(inst, "blocked", "inconclusive" if why else "unknown_reason",
                 why or "The job did not record why it stopped", "fix",
                 "Check the recorded prerequisite before resuming.", None, since)


def _at_instance(job: Job, inst: Instance, entry: dict, previous: dict,
                 ready_since: str | None) -> Moment:
    state = entry.get("state")
    meta = entry.get("metadata") or {}
    since = _latest(entry.get("at"))
    if state == "waiting_human":
        return _gate(job, inst, meta, since)
    if state == "waiting_judgment":
        tasks = pending_tasks(job)
        if tasks:
            names = ", ".join(t["task"] for t in tasks)
            return _wait(inst, "waiting_judgment", "judgment", f"Judgment is pending for {names}",
                         "judge", "List the tasks that need judgment.",
                         _command("judge-pending", job), since)
        if task_path := meta.get("task_path"):
            task_dir = job.judge_dir(Path(task_path).name)
            task = read_yaml(task_dir / "task.yaml", default={})
            if isinstance(task, dict) and task.get("output"):
                output = task_dir / task["output"]
                if output.is_file():
                    return _ready(job, inst)
        return _wait(inst, "blocked", "unknown_reason", "The pending judgment task is missing",
                     "fix", "Check the recorded judgment task before resuming.", None, since)
    if state == "inconclusive":
        return _blocked(job, inst, meta, since)
    if state in ("failed", "abandoned"):
        why = meta.get("error") or previous.get("error") or "The step did not record its error"
        return _wait(inst, "failed", "step_failed", why, "fix",
                     "Resolve the recorded step error before resuming.", None, since)
    if state == "running":
        return _ready(job, inst)
    if state is None and ((inst.step == "S3b" and job.fragments_review.is_file()) or
                          (inst.step == "S9" and job.review_yaml.is_file())):
        # A gate can write its files just before the process records the wait in the journal.
        return _gate(job, inst, {}, None)
    return _ready(job, inst, since or ready_since)


def _activity(job: Job, lock: dict | None) -> tuple[dict | None, tuple[str, str, str | None] | None]:
    if lock is None:
        return None, None
    pid, host = lock.get("pid"), lock.get("host")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not host:
        return None, ("stale_lock", "A stale job lock has no valid holder", _command("unlock", job))
    if host != socket.gethostname():
        return None, ("unverified_lock", "The job lock belongs to a process on another host", None)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return None, ("stale_lock", "A stale job lock remains after the process exited",
                      _command("unlock", job))
    except PermissionError:
        pass  # The process exists, although we cannot signal it.
    lock_at = _time(lock.get("at"))
    try:
        record = progress.read(job.root, locked=False)
    except _READ_ERRORS:
        record = None
    if record and record["step"] in GRAPH:
        updated = _time(record.get("updated_at"))
        if updated and lock_at and updated >= lock_at:
            return record, None
    return None, ("lock_no_progress", "The job lock is held without fresh progress", None)


def _scenario(job: Job, last: dict[Instance, dict]) -> str | None:
    saved = runsignal.read_state(job.root).get("scenario")
    if saved:
        return saved if isinstance(saved, str) else None
    intake = last.get(Instance("S0"), {})
    why = (intake.get("metadata") or {}).get("message")
    if str(why).startswith(f"{NEED_TRANSCRIPT}:") or (job.root / "transcript.yaml").is_file():
        return "E"
    if job.job_yaml.is_file() or (job.source_video.is_file() and job.source_srt.is_file()):
        return "D"
    if job.request_yaml.is_file() and job.source_video.is_file():
        return "E"
    return None


def _map(scenario_id: str | None, step: str | None) -> tuple[str | None, int | None, int | None]:
    if scenario_id is None:
        return None, None, None
    route = next((s for s in scenarios.scenario_list() if s["id"] == scenario_id), None)
    if not route or route.get("doc_status") != "built":
        return None, None, None
    ids = [row["id"] for row in scenarios.steps_of(route)]
    return scenario_id, ids.index(step) + 1 if step in ids else None, len(ids)


def _document(job: Job, moment: Moment, *, scenario_id: str | None = None, whole: bool = False,
              focus: dict | None = None, kind: str = "recording") -> dict:
    inst = moment.instance
    step = moment.step or (_library_step(inst.step, whole) if inst else None)
    title = scenarios.tree()["lib"][step]["title"] if step else None
    scenario_id, index, total = _map(scenario_id, step)
    return {
        "kind": kind, "scenario": scenario_id, "step": step, "title": title,
        "index": index, "total": total, "engine": inst.key if inst else None,
        "state": moment.state, "waiting_for": moment.waiting_for,
        "next": {"action": moment.action, "command": moment.command, "text": moment.text},
        "since": moment.since, "focus": focus, "report": report_path(job),
    }


def _evidence(job: Job, inst: Instance, entry: dict) -> dict:
    """Use completion manifests when a run stopped between writing the manifest and the journal."""
    if entry.get("state") in (*_COMPLETE, "stale", "failed", "abandoned"):
        return entry
    manifest = read_manifest(job, inst.step, inst.item)
    finished = _time(manifest.get("finished_at")) if manifest else None
    recorded = _time(entry.get("at"))
    if finished and (recorded is None or finished > recorded):
        return {"state": "done", "at": finished.isoformat()}
    return entry


def _item_focus(items: list[str] | None, selected: str | None, current: Instance | None,
                pending: list[Instance], live: Instance | None, whole: bool) -> dict | None:
    if items is None and selected is None:
        return None
    frontier = {}
    for candidate in pending:
        if candidate.item:
            frontier.setdefault(candidate.item, candidate.step)
    if live and live.item:
        frontier[live.item] = live.step
    total = len(items or [])
    if current and current.item:
        step = _library_step(current.step, whole)
        at_step = sum(_library_step(stage, whole) == step for stage in frontier.values())
    elif current and _GRAPH.needs_items(current.step):
        at_step = total - len(frontier)
    else:
        at_step = total if current else 0
    return {"item": selected if selected is not None else current.item if current else None,
            "version": None, "items_total": total, "items_at_step": at_step}


def of(job: Path, *, item: str | None = None, version: str | None = None) -> dict:
    """Return one recording or text position from the available local evidence."""
    job = Job(job)
    if runsignal.route_of(job.root) == "text":
        try:
            from . import position_text
        except ImportError:
            unknown = Moment(None, "unknown", "none", "Open the text job status.",
                             _command("text status", job))
            return _document(job, unknown, kind="text")
        return position_text.of(job.root, version=version)

    history = Status(job).history()
    last, errors = {}, {}
    for entry in history:
        inst = Instance(entry["stage"], entry.get("item"))
        last[inst] = entry
        meta = entry.get("metadata") or {}
        if entry["state"] in _COMPLETE or meta.get("reset"):
            errors.pop(inst, None)
        elif meta.get("error"):
            errors[inst] = meta

    items = job.items()
    candidates = _GRAPH.instances(items)
    selection = read_manifest(job, "S3a") or {}
    fragments = read_yaml(job.fragments_yaml, default={}) or {}
    whole = ((fragments.get("whole_recording") or {}).get("kind") or
             (selection.get("config") or {}).get("whole_recording")) in ("full", "reel")
    active, lock_issue = _activity(job, lock := holder(job))
    live = Instance(active["step"], active.get("item")) if active else None
    ended = Instance("S11")

    entries = {inst: _evidence(job, inst, last.get(inst, {})) for inst in candidates}
    pending = [inst for inst in candidates if entries[inst].get("state") not in _COMPLETE]
    first = next((inst for inst in pending if item is None or inst.item in (item, None)), None)
    if item is not None and item not in (items or []) and (not live or live.item != item):
        moment = Moment(None, "unknown", "none", "Choose an item recorded for this job.")
    elif live and (item is None or live.item in (item, None)):
        entry = last.get(live, {})
        started = _time(entry.get("at"))
        since = entry["at"] if entry.get("state") == "running" and started and (
            started >= _time(lock["at"])) else active.get("updated_at")
        moment = Moment(live, "running", "none", "Wait for the current step to finish.", since=since)
    elif first is None and entries.get(ended, {}).get("state") in _COMPLETE:
        moment = Moment(ended, "done", "none", "The recording route is complete.",
                        since=_latest(entries[ended].get("at")))
    elif first is None:
        moment = _wait(Instance("S3b"), "blocked", "missing_fragments",
                       "The recording has no saved fragment list", "fix",
                       "Restore the fragment list or rerun the job to recreate it.",
                       run_command(job), None)
    elif first.item is None and pending[0] != first:
        blocking = pending[0]
        blocked_by = _at_instance(job, blocking, entries[blocking], errors.get(blocking, {}), None)
        action, text, command = blocked_by.action, blocked_by.text, blocked_by.command
        if live and live.item and live.item != item:
            action, text, command = "none", "Wait for the other item to finish.", None
        moment = _wait(first, "blocked", "item_dependency",
                       f"{first.key} is waiting for {blocking.key} to finish",
                       action, text, command, blocked_by.since)
    elif first and (history or any(e for e in entries.values())):
        prior = [_latest(entries[inst].get("at")) for inst in candidates[:candidates.index(first)]
                 if entries[inst].get("state") in _COMPLETE and
                 (item is None or inst.item in (item, None))]
        moment = _at_instance(job, first, entries[first], errors.get(first, {}), _latest(*prior))
    elif job.request_yaml.is_file():
        moment = _ready(job, Instance("S0"), state="new")
    else:
        moment = Moment(None, "unknown", "none", "Choose a registered recording job folder.")

    if lock_issue:
        code, why, command = lock_issue
        if moment.state == "done":
            moment.waiting_for = {"code": code, "text": _sentence(why)}
        else:
            moment = _wait(moment.instance, "blocked", code, why, "fix",
                           "Unlock the job before resuming." if command else
                           "Check the job lock before resuming.", command,
                           None, step=moment.step)

    focus = _item_focus(items, item, moment.instance, pending, live, whole)
    return _document(job, moment, scenario_id=_scenario(job, last), whole=whole, focus=focus)


def describe(position: dict) -> str:
    """The position block of the terminal status display."""
    title = (position["title"] or {}).get("en")
    label = " — ".join(part for part in (position["engine"], title) if part) or "unknown step"
    if position["index"] is not None:
        label = f"{position['index']}/{position['total']} {label}"
    lines = [f"position: {label}", f"state: {position['state']}"]
    if position["waiting_for"]:
        lines.append(f"waiting for: {position['waiting_for']['text']}")
    action = position["next"]
    command = f" ({action['command']})" if action["command"] else ""
    lines.append(f"next: {action['text']}{command}")
    if position["report"]:
        lines.append(f"report: {position['report']}")
    return "\n".join(lines)
