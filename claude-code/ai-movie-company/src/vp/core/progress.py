"""Progress of the current long step (UX U7): `<job>/progress.json`, written atomically.

Shape (stable, read by `vp progress` and the plugin):
  {"step": str, "item": str|null, "done": number, "total": number, "unit": str,
   "percent": number, "elapsed_s": number, "eta_s": number|null, "updated_at": ISO}

Writers:
  * the runner opens a tracker per step instance (`tracking`) and writes the coarse position
    (instances of this step done / total, unit "items");
  * ffmpeg calls (vp.adapters.media.run_ffmpeg) report seconds of media processed from
    `-progress pipe:1` (unit "s");
  * the YouTube upload reports bytes sent (unit "bytes").
A tracker writes at most every MIN_INTERVAL_S (first and forced updates always); the file is
removed when the instance ends. Finer reporters find the tracker through a context variable, so
nothing has to be passed down the call chain; without an active tracker `report()` is a no-op.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .io import now_iso, parse_iso, read_json, write_json

FILE_NAME = "progress.json"
MIN_INTERVAL_S = 2.0
STALE_S = 120.0     # a file not updated for this long while the job is not locked = idle

STEP_NAMES = {"S0": "intake", "S1": "proofread", "S2": "retime", "S3a": "select", "S3b": "review",
              "S4": "cut", "S5": "render", "S5v": "picture check", "S6a": "design texts", "S6b": "metadata",
              "S7": "thumbnail", "S8": "upload", "S10": "crosslinks", "S9": "package",
              "PUB": "publish", "S11": "report", "preview": "preview"}

_current: contextvars.ContextVar["Tracker | None"] = contextvars.ContextVar("vp_progress",
                                                                            default=None)


def eta(done: float, total: float, elapsed_s: float) -> float | None:
    """Remaining seconds by linear extrapolation; None when nothing is done yet."""
    if total <= 0 or done <= 0 or elapsed_s <= 0:
        return None
    if done >= total:
        return 0.0
    return round(elapsed_s * (total - done) / done, 1)


class Tracker:
    """Progress of one step instance in one job folder."""

    def __init__(self, path: Path, step: str, item: str | None, *,
                 clock: Callable[[], float] = time.monotonic,
                 min_interval_s: float = MIN_INTERVAL_S):
        self.path = Path(path)
        self.step = step
        self.item = item
        self.clock = clock
        self.min_interval_s = min_interval_s
        self.started = clock()
        self._last_write: float | None = None

    def update(self, done: float, total: float, unit: str, *, since: float | None = None,
               force: bool = False) -> bool:
        """Record a position. `since` = clock value when this measured quantity started (an
        ffmpeg pass, an upload); default = the tracker start. Returns True if written."""
        now = self.clock()
        if not force and self._last_write is not None \
                and now - self._last_write < self.min_interval_s:
            return False
        total = max(float(total), 0.0)
        done = min(max(float(done), 0.0), total) if total else max(float(done), 0.0)
        elapsed = max(now - (self.started if since is None else since), 0.0)
        rec = {"step": self.step, "item": self.item, "done": _num(done), "total": _num(total),
               "unit": unit, "percent": round(100.0 * done / total, 1) if total else 0.0,
               "elapsed_s": round(elapsed, 1), "eta_s": eta(done, total, elapsed),
               "updated_at": now_iso()}
        try:
            write_json(self.path, rec)
        except OSError:
            return False   # progress is best effort: never break the step
        self._last_write = now
        return True

    def finish(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def _num(x: float) -> float | int:
    return int(x) if float(x).is_integer() else round(float(x), 3)


def current() -> Tracker | None:
    return _current.get()


def report(done: float, total: float, unit: str, *, since: float | None = None,
           force: bool = False) -> None:
    """Report a finer position to the active tracker (no-op without one)."""
    t = _current.get()
    if t is not None:
        t.update(done, total, unit, since=since, force=force)


def clock() -> float:
    """The clock of the active tracker (for `since=`), else time.monotonic()."""
    t = _current.get()
    return t.clock() if t is not None else time.monotonic()


@contextlib.contextmanager
def tracking(job_root: Path, step: str, item: str | None, *, done: float = 0, total: float = 1,
             unit: str = "items", clock: Callable[[], float] = time.monotonic) -> Iterator[Tracker]:
    """Open a tracker for one step instance; the file is removed at the end (any outcome)."""
    t = Tracker(Path(job_root) / FILE_NAME, step, item, clock=clock)
    t.update(done, total, unit, force=True)
    token = _current.set(t)
    try:
        yield t
    finally:
        _current.reset(token)
        t.finish()


# ---- reading (CLI `vp progress`) -----------------------------------------------------------

def read_json_raw(job_root: Path) -> dict[str, Any] | None:
    rec = read_json(Path(job_root) / FILE_NAME, default=None)
    return rec if isinstance(rec, dict) and rec.get("step") else None


def read(job_root: Path, locked: bool | None = None, now: float | None = None) -> dict[str, Any] | None:
    """The current progress record, or None when idle (no file, or a stale file of a run that
    is no longer active: job not locked and not updated for STALE_S)."""
    rec = read_json(Path(job_root) / FILE_NAME, default=None)
    if not isinstance(rec, dict) or not rec.get("step"):
        return None
    if locked:
        return rec
    try:
        age = (now if now is not None else time.time()) - parse_iso(str(rec["updated_at"])).timestamp()
    except (KeyError, ValueError):
        return None
    return rec if age <= STALE_S else None


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "estimating"
    if seconds < 60:
        return f"~{max(int(round(seconds)), 1)} s left"
    if seconds < 3600:
        return f"~{int(round(seconds / 60))} min left"
    return f"~{seconds / 3600:.1f} h left"


def describe(rec: dict[str, Any] | None) -> str:
    """One line, e.g. "S5 render l01: 62% (~1 min left)"; "idle" without a record."""
    if not rec:
        return "idle"
    step = str(rec.get("step"))
    name = STEP_NAMES.get(step)
    head = f"{step} {name}" if name and name != step else step
    if rec.get("item"):
        head += f" {rec['item']}"
    pct = float(rec.get("percent") or 0)
    pct_s = f"{int(pct)}%" if pct == int(pct) else f"{pct:.0f}%"
    if rec.get("unit") == "items":
        detail = f"{_num(float(rec.get('done') or 0))}/{_num(float(rec.get('total') or 0))} items"
        return f"{head}: {detail}, {pct_s} ({fmt_eta(rec.get('eta_s'))})"
    return f"{head}: {pct_s} ({fmt_eta(rec.get('eta_s'))})"
