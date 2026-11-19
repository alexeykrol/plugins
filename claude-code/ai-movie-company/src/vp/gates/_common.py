"""Shared helpers of the gate commands: job context, lock, output."""
from __future__ import annotations

import json
from typing import Any, Callable

from ..core import runner
from ..core.ctx import Ctx
from ..core.errors import EXIT_CONSENT_REQUIRED, ConsentRequired, VPError
from ..core.lock import job_lock
from ..core.paths import Job

RC_OK, RC_REFUSED, RC_INTERNAL, RC_USER_INPUT = 0, 2, 3, 4


class Refused(Exception):
    """Input rejected; nothing was written. `errors` = [{where, message}]."""

    def __init__(self, message: str, errors: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []


class UserInputRequired(Refused):
    """A gate decision without a real user input after the item was shown (exit 4).
    See vp.gates.human."""


def job_ctx(job: Job) -> Ctx:
    cfg = runner.prepare_config(job, None)
    return runner.make_ctx(job, cfg)


def locked(job: Job, fn: Callable[[], int]) -> int:
    """Run a writing command under the job lock; a held lock refuses (another runner is active)."""
    with job_lock(job) as got:
        if not got:
            print("LOCKED: another runner holds job.lock; nothing was written")
            return RC_REFUSED
        return fn()


def guarded(fn: Callable[[], int]) -> int:
    """Uniform error output: REFUSED (+ error lines) / ERROR; exit codes 2 / 3; a missing user
    input (UserInputRequired) is REFUSED with exit code 4; a real publish without the accepted
    terms (ConsentRequired) is `REFUSED consent-required` with exit code 5."""
    try:
        return fn()
    except ConsentRequired as e:
        print(f"REFUSED consent-required: {e.msg}")
        return EXIT_CONSENT_REQUIRED
    except UserInputRequired as e:
        print(f"REFUSED {e.message}")
        return RC_USER_INPUT
    except Refused as e:
        print(f"REFUSED {e.message}")
        for err in e.errors:
            print(f"  - {err['where']}: {err['message']}")
        return RC_REFUSED
    except VPError as e:
        print(f"ERROR {e}")
        return RC_INTERNAL if e.kind == "internal" else RC_REFUSED


def print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
