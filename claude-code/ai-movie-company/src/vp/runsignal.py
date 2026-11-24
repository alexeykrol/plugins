"""The end-to-end run signal (roadmap 1.1): which step of which route a job reached.

Every route reports its steps as telemetry `step` events of one shape:
  {kind: step, event: start|end, step, route, scenario?, run?, outcome?, state?, duration_s?,
   error_kind?}
The step names, the routes and the scenario letters are closed vocabularies owned by
vp.telemetry (ROUTE_STEPS, known_scenarios); `telemetry.sanitize_event` drops anything else. No
job name, path, item or text is ever passed on: this module hands telemetry only the fields above.

Who reports what:
  recording route   vp.core.runner: every step instance (start / end), then `video_ready` when the
                    visual check of every clip of the job has passed (`after_recording_step`)
  text route        the engine commands of a stage report start / end (`step`): voice (`vp voice
                    scenes`), music (`vp music make`), animation (`vp text timeline`), render and
                    master (`vp text render|master`), the sketches approval (`vp text approve`);
                    the stages done in the dialog (brief, script, sketches) or by hand (own music)
                    leave only files, so they are noticed on disk at the next engine command
                    (`observe_text`: one `end` event with state `observed`, no duration);
                    `video_ready` after a successful master
  both              capcut_export (`vp capcut …`), fcpxml_export (`vp resolve export`)

State of a job: <job>/run.json, next to request.yaml / status.yaml / approvals.yaml:
  {"run": "<16 hex>",        random (secrets.token_hex), created with the job's first event; never
                             derived from the job's name, path, time or content
   "scenario": "D",          the scenario letter chosen for the job (`vp scenario plan <id> --job`,
                             `vp text new --scenario`)
   "seen": ["brief", …]}     text stages already reported (so each is reported once)
The run number, the route and the scenario are attached to events here, but
`telemetry.sanitize_event` lets them leave only when the accepted terms version allows it
(telemetry.TERMS_ALLOWING_RUN, empty today); under the published terms a step event carries the
step name, the outcome and the duration only.

Nothing here raises into the caller, and nothing is written (no run.json, no event) while
telemetry is off or the terms are not accepted.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Iterator

from . import telemetry

STATE_FILE = "run.json"
ROUTE_RECORDING, ROUTE_TEXT = "recording", "text"
READY = telemetry.READY_STEP
OBSERVED = "observed"                       # `state` of a stage noticed on disk, not run by a command
_TEXT_STAGE_STEP = {"capcut": "capcut_export"}     # vp.text.STAGES name -> step name (else the same)
_DONE_STATES = ("done", "skipped")


# ---- the job's state file -------------------------------------------------------------------------

def state_path(folder: str | os.PathLike) -> Path:
    return Path(folder).expanduser() / STATE_FILE


def read_state(folder: str | os.PathLike) -> dict[str, Any]:
    try:
        data = json.loads(state_path(folder).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(folder: str | os.PathLike, data: dict[str, Any]) -> None:
    from .core.io import write_json
    write_json(state_path(folder), data, indent=1)


def run_id(folder: str | os.PathLike, create: bool = True) -> str | None:
    """The job's random run number; created once and kept in <job>/run.json."""
    st = read_state(folder)
    got = st.get("run")
    if isinstance(got, str) and telemetry.RUN_RE.fullmatch(got):
        return got
    if not create or not Path(folder).expanduser().is_dir():
        return None
    st["run"] = secrets.token_hex(8)
    _write_state(folder, st)
    # two commands of one job starting at once: the number left on disk is the job's
    kept = read_state(folder).get("run")
    return kept if isinstance(kept, str) and telemetry.RUN_RE.fullmatch(kept) else st["run"]


def scenario_of(folder: str | os.PathLike) -> str | None:
    got = read_state(folder).get("scenario")
    return got if isinstance(got, str) and got in telemetry.known_scenarios() else None


def set_scenario(folder: str | os.PathLike, sid: str) -> bool:
    """Remember the scenario letter chosen for the job. False when the folder does not exist or
    the letter is not a scenario of knowledge/scenarios.json (nothing is written then)."""
    sid = str(sid or "").strip().upper()
    if sid not in telemetry.known_scenarios() or not Path(folder).expanduser().is_dir():
        return False
    st = read_state(folder)
    if st.get("scenario") != sid:
        st["scenario"] = sid
        _write_state(folder, st)
    return True


def route_of(folder: str | os.PathLike) -> str:
    from .text import is_text_job
    try:
        return ROUTE_TEXT if is_text_job(folder) else ROUTE_RECORDING
    except Exception:  # noqa: BLE001 - an unreadable request: the default route
        return ROUTE_RECORDING


def fields(folder: str | os.PathLike) -> dict[str, Any]:
    """The job fields of a step event: route, scenario (when chosen), run. Empty when telemetry
    records nothing (so no run.json appears) or the folder does not exist. Never raises."""
    try:
        if not telemetry.can_send() or not Path(folder).expanduser().is_dir():
            return {}
        return {"route": route_of(folder), "scenario": scenario_of(folder),
                telemetry.RUN_FIELD: run_id(folder)}
    except Exception:  # noqa: BLE001 - the signal must never break the caller
        return {}


# ---- events -----------------------------------------------------------------------------------------

def record(folder: str | os.PathLike, step: str, event: str, **data: Any) -> bool:
    """One `step` event of a job. Never raises."""
    return telemetry.record("step", event=event, step=step, **fields(folder), **data)


def _mark_seen(folder: str | os.PathLike, step: str) -> None:
    try:
        if not telemetry.can_send() or not Path(folder).expanduser().is_dir():
            return
        st = read_state(folder)
        seen = [s for s in st.get("seen") or [] if isinstance(s, str)]
        if step not in seen:
            st["seen"] = seen + [step]
            _write_state(folder, st)
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def step(folder: str | os.PathLike, name: str) -> Iterator[None]:
    """A step run by an engine command: `start`, then `end` with the outcome (done / failed /
    crashed), the duration and — for a classified engine error — its kind. Errors pass through."""
    from .core.errors import VPError
    observe_text(folder)                    # the stages done before this one come first
    record(folder, name, "start")
    t0 = time.monotonic()
    outcome, kind = "crashed", None
    try:
        yield
        outcome = "done"
    except VPError as e:
        outcome, kind = "failed", e.kind
        raise
    except Exception:
        outcome = "failed"
        raise
    finally:
        record(folder, name, "end", outcome=outcome, state=outcome,
               duration_s=round(time.monotonic() - t0, 3), error_kind=kind)
        if outcome == "done" and route_of(folder) == ROUTE_TEXT:
            _mark_seen(folder, name)        # the text route's "noticed on disk" skips it then


def video_ready(folder: str | os.PathLike) -> bool:
    """The moment a finished video of the job exists."""
    return record(folder, READY, "end", outcome="done", state="done")


def observe_text(folder: str | os.PathLike) -> list[str]:
    """Text route: report the stages that appeared on disk since the last look (the brief, the
    screenplay and the sketches are written in the dialog; a music file may be the person's own).
    One `end` event per stage, state `observed`. Returns the steps reported now. Never raises."""
    try:
        if not telemetry.can_send():
            return []
        from . import text
        root = Path(folder).expanduser()
        if not root.is_dir() or not text.is_text_job(root):
            return []
        have = text.stages_present(root)
        order: list[str] = []
        for stage in text.STAGES:
            if have.get(stage):
                order.append(_TEXT_STAGE_STEP.get(stage, stage))
            if stage == "sketches" and "sketches" in text.approvals(root):
                order.append("approve_sketches")
        seen = {s for s in read_state(root).get("seen") or [] if isinstance(s, str)}
        new = [s for s in order if s not in seen and s in telemetry.STEPS]
        for s in new:
            record(root, s, "end", outcome="done", state=OBSERVED)
            _mark_seen(root, s)
        return new
    except Exception:  # noqa: BLE001
        return []


def after_recording_step(job: Any, step_id: str, outcome: str) -> bool:
    """Recording route, called by the runner after a step instance ends: when the visual check
    (S5v) of the LAST clip has passed — every item's S5v is done or skipped — the videos are
    ready. A later re-render that ends the same way reports it again. Never raises."""
    try:
        if step_id != "S5v" or outcome not in _DONE_STATES or not telemetry.can_send():
            return False
        from .core.status import Status
        status = Status(job)
        items = job.items() or []
        if not items:
            return False
        for it in items:
            last = status.last("S5v", it)
            if not last or last.get("state") not in _DONE_STATES:
                return False
        return video_ready(job.root)
    except Exception:  # noqa: BLE001
        return False
