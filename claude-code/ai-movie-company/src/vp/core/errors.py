"""Error classes and control-flow signals raised by steps.

Error kinds (SPEC §7.1):
  auth       terminal, never retried; the instance is abandoned at once (+ one notification)
  transport  retried (see `with_retries`), then failed
  data       bad input / unmet acceptance -> failed
  contract   invalid model or adapter output -> failed
  internal   bug in our code -> failed, CLI exit code 3
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable

KINDS = ("auth", "transport", "data", "contract", "internal")


class VPError(Exception):
    """A classified pipeline error. `kind` is one of KINDS."""

    retryable_transport = True  # subclasses may opt out (e.g. ambiguous upload outcome)

    def __init__(self, kind: str, msg: str, **details: Any):
        if kind not in KINDS:
            raise ValueError(f"unknown error kind {kind!r}")
        super().__init__(msg)
        self.kind = kind
        self.msg = msg
        self.details = details

    @property
    def retryable(self) -> bool:
        return self.kind == "transport" and self.retryable_transport

    def __str__(self) -> str:
        return f"[{self.kind}] {self.msg}"


class WaitingHuman(Exception):
    """A human gate is closed. The run stops with status waiting_human (exit 0).

    gate: gate name (e.g. "S3b.review", "S9.approval"); what: short description of what the
    human must do (file to write); kind: human time kind review|full_scan|candidate_eval.
    """

    def __init__(self, gate: str, what: str, kind: str = "review"):
        super().__init__(f"waiting for human: {gate}: {what}")
        if kind not in ("review", "full_scan", "candidate_eval"):
            raise ValueError(f"unknown human time kind {kind!r}")
        self.gate = gate
        self.what = what
        self.kind = kind


class JudgmentPending(Exception):
    """Manual judge wrote a task and waits for its output file (status waiting_judgment)."""

    def __init__(self, task_path: str):
        super().__init__(f"judgment pending: {task_path}")
        self.task_path = str(task_path)


class Inconclusive(Exception):
    """The step cannot decide (e.g. S1 empty share > 15 %). Stops the run like a gate."""

    def __init__(self, msg: str, report: str | None = None):
        super().__init__(msg)
        self.msg = msg
        self.report = report


class WaitingQuota(Inconclusive):
    """Not enough YouTube quota left today for the uploads of this run. Raised BEFORE anything
    is uploaded; the run stops (status inconclusive, reason youtube_quota, exit 0) and the next
    run after the daily reset continues."""


EXIT_CONSENT_REQUIRED = 5   # = vp.safety.RC_NOT_ACCEPTED


class ConsentRequired(Exception):
    """The plugin's terms are not accepted, and the engine was about to touch a real platform
    (upload / publish / channel check) — e.g. a client such as ai_lms calling `vp run` or
    `vp approve set` without the dialog. Nothing was sent. Output prefix for clients:
    `REFUSED consent-required`; exit code 5. Rehearsals (publisher: fake) never raise it."""

    def __init__(self, msg: str):
        super().__init__(msg)
        self.msg = msg


class Skip(Exception):
    """The step is not applicable for this job/item (status skipped).

    The runner writes a manifest with the step's declared outputs that exist, so downstream
    steps treat it as satisfied.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def with_retries(fn: Callable[[], Any], n: int = 3, backoff: Iterable[float] = (2.0, 5.0, 15.0),
                 sleep: Callable[[float], None] = time.sleep,
                 on_retry: Callable[[int, VPError], None] | None = None) -> Any:
    """Call `fn()`; on a retryable transport VPError retry up to `n` more times.

    Total attempts = n + 1. `backoff` gives the pause before retry i (last value repeats).
    Non-transport errors and non-retryable transport errors propagate immediately.
    After the last failed attempt the last error is re-raised (the runner marks the step failed).
    """
    delays = list(backoff) or [0.0]
    attempt = 0
    while True:
        try:
            return fn()
        except VPError as e:
            if not e.retryable or attempt >= n:
                raise
            if on_retry is not None:
                on_retry(attempt + 1, e)
            sleep(delays[min(attempt, len(delays) - 1)])
            attempt += 1
