"""`vp results` and `vp progress` — read-only views of a job for the chat (no API calls, no lock).

  results <job> [--json]        what came out: the single source for the final chat message and
                                for opening browser tabs (vp.steps._results.results)
  progress <job> [--json] [--wait N]
                                the current long step in one line ("S5 render l01: 62% (~1 min
                                left)") or "idle"; --json prints the progress.json record or
                                {"idle": true}. --wait N (N <= 120 s) blocks until the run's state
                                changes (progress.json finished/removed or replaced by another
                                step, status.yaml gets a waiting/failed/done entry, the run ends)
                                or N seconds pass, then prints the same line. Exit 0.
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Any, Callable

from .core import progress as P
from .core.lock import holder
from .core.paths import Job
from .core.status import Status

MAX_WAIT_S = 120.0
POLL_S = 0.5
SETTLED = ("waiting_human", "waiting_judgment", "inconclusive", "failed", "abandoned", "done",
           "skipped")


def _config_for(job: Job) -> dict[str, Any]:
    try:
        from .core.config import load_config, resolve_job_config
        return resolve_job_config(load_config(), job.request())
    except Exception:  # noqa: BLE001 — results must work even with a broken config
        return {}


def results_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp results", description="what came out of a job")
    ap.add_argument("job")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    from .steps._results import results, text_table
    job = Job(a.job)
    doc = results(job, _config_for(job))
    print(json.dumps(doc, ensure_ascii=False, indent=2) if a.json else text_table(doc))
    return 0


def current(job: Job) -> dict[str, Any] | None:
    return P.read(job.root, locked=holder(job) is not None)


def _snapshot(job: Job) -> tuple[Any, ...]:
    rec = P.read_json_raw(job.root)
    try:
        hist = Status(job).history()
    except Exception:  # noqa: BLE001
        hist = []
    last = hist[-1] if hist else {}
    return ((rec or {}).get("step"), (rec or {}).get("item"), rec is not None, len(hist),
            last.get("state"), holder(job) is not None)


def changed(before: tuple[Any, ...], after: tuple[Any, ...]) -> bool:
    """Did the run's state change in a way worth reporting?"""
    b_step, b_item, b_has, b_len, _, b_locked = before
    a_step, a_item, a_has, a_len, a_state, a_locked = after
    if b_has and (not a_has or (a_step, a_item) != (b_step, b_item)):
        return True                       # the step finished or another one started
    if a_len != b_len and a_state in SETTLED:
        return True                       # a gate, a failure, a finished stage
    if b_locked and not a_locked:
        return True                       # the run ended
    return False


def wait_for_change(job: Job, timeout_s: float, *, sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic) -> bool:
    """Block up to timeout_s; True if the state changed, False on timeout or when nothing runs."""
    before = _snapshot(job)
    if not before[2] and not before[5]:
        return False                      # idle and not locked: nothing to wait for
    start = clock()
    while clock() - start < timeout_s:
        sleep(min(POLL_S, max(timeout_s - (clock() - start), 0.0)))
        if changed(before, _snapshot(job)):
            return True
    return False


def progress_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp progress", description="current long step of a job")
    ap.add_argument("job")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--wait", type=float, default=0.0, metavar="N",
                    help=f"block up to N seconds (max {int(MAX_WAIT_S)}) until the state changes")
    a = ap.parse_args(argv)
    if a.wait < 0 or a.wait > MAX_WAIT_S:
        ap.error(f"--wait must be between 0 and {int(MAX_WAIT_S)} seconds")
    job = Job(a.job)
    if a.wait:
        wait_for_change(job, a.wait)
    rec = current(job)
    print(json.dumps(rec if rec else {"idle": True}, ensure_ascii=False) if a.json
          else P.describe(rec))
    return 0
