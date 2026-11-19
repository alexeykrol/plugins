"""job.lock — one executor per job (SPEC §5).

Created with O_CREAT|O_EXCL (atomic). A stale lock (dead process) is never removed
automatically; only `python -m vp unlock <job>`.
"""
from __future__ import annotations

import contextlib
import os
import socket
from typing import Iterator

from .io import dump_yaml, now_iso, read_yaml
from .paths import Job


def acquire(job: Job) -> bool:
    """Try to take the lock. Returns False if it is already held (by anyone)."""
    job.root.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(job.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(dump_yaml({"pid": os.getpid(), "host": socket.gethostname(), "at": now_iso()}))
    return True


def holder(job: Job) -> dict | None:
    """Contents of the lock file (pid, host, at) or None if unlocked."""
    if not job.lock_file.exists():
        return None
    data = read_yaml(job.lock_file, default={})
    return data if isinstance(data, dict) else {}


def release(job: Job) -> None:
    """Remove the lock if it belongs to this process (same pid and host)."""
    h = holder(job)
    if h is None:
        return
    if h.get("pid") == os.getpid() and h.get("host") == socket.gethostname():
        try:
            os.unlink(job.lock_file)
        except FileNotFoundError:
            pass


def unlock(job: Job) -> bool:
    """Manual unlock (CLI). Removes the lock regardless of owner. True if a lock was removed."""
    try:
        os.unlink(job.lock_file)
        return True
    except FileNotFoundError:
        return False


@contextlib.contextmanager
def job_lock(job: Job) -> Iterator[bool]:
    """Context manager yielding True if the lock was taken; released on exit (also on errors)."""
    got = acquire(job)
    try:
        yield got
    finally:
        if got:
            release(job)
