"""Telemetry of the test period: always written locally, shipped to the owner's receiver when online.

Design: docs/channel-and-telemetry.md §1. Server contract v1 (fixed):
  POST <base>/telemetry   Content-Type application/x-ndjson, Content-Encoding gzip,
                          X-Elsa-Install / X-Elsa-Version / X-Elsa-Batch (uuid4 per batch, the
                          idempotency key); body = JSON lines, each with "ts" and "kind".
  200 {"stored", "batch", "duplicate"} -> the batch file is deleted;
  429 (Retry-After), 5xx, timeout, no network -> kept, retried later;
  413 -> split in two and retried; 400 / 415 -> moved to quarantine with a local note (the
  receiver's `header` / `line` + `reason`), never resent; 503 with Retry-After -> like 429.
<base> = env AIMC_TELEMETRY_URL, default https://api.alexeykrol.com/elsa.

On disk, <VP_HOME>/telemetry/:
  install_id            random id of this machine, created once (0600)
  spool/current.jsonl   the open spool: `record()` appends one line per event
  spool/batch-<uuid>.jsonl  closed batches waiting to be sent (the uuid is X-Elsa-Batch)
  quarantine/           batches the receiver refused (400) + a .note.txt beside each
  state.json            retry_after, last flush, imported installer logs
The spool, batches and quarantine together are capped (CAP_BYTES): the oldest batches go first.

`record()` never raises and never blocks on the network. Secrets are never recorded: values of
secret-looking keys are replaced (vp.core.log.scrub) and strings pass the journal's redaction.

Off switch: AIMC_TELEMETRY=off — nothing is recorded, nothing is sent.

CLI:  vp telemetry status [--json] | flush [--detach] [--json] | show-id
      vp telemetry import-install [<log> ...]   installer logs -> events (default: every
                                                ~/.ai_video_pipeline/logs/install-*.log not yet imported)
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_URL = "https://api.alexeykrol.com/elsa"
ENV_URL = "AIMC_TELEMETRY_URL"
ENV_SWITCH = "AIMC_TELEMETRY"

MAX_LINE = 60 * 1024                  # server: <= 64 KiB per line
ROTATE_BYTES = 1024 * 1024            # a batch never exceeds 1 MiB uncompressed (server: 8 MiB)
ROTATE_LINES = 4000                   # server: <= 5000 lines
CAP_BYTES = 50 * 1024 * 1024          # everything kept locally
MAX_BATCHES_PER_FLUSH = 20            # server rate limit ~60 batches/hour/install
FLUSH_EVERY_S = 60.0                  # background flushes after commands at most this often
OFFLINE_BACKOFF_S = 60.0              # after a network failure, wait this long before retrying
DEFAULT_RETRY_AFTER_S = 300.0
TIMEOUT_S = 10.0
MAX_STRING = 8000
RESERVED_KEYS = ("_recv", "_ver")      # the receiver writes these itself
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SECRET_KEYS = ("token", "secret", "password", "authorization", "api_key", "apikey", "cookie")
_SAFE_KEYS = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}


# ---- paths and switches -------------------------------------------------------------------------

def is_off() -> bool:
    return os.environ.get(ENV_SWITCH, "").strip().lower() in ("off", "0", "false", "no")


def base_url() -> str:
    return (os.environ.get(ENV_URL) or DEFAULT_URL).rstrip("/")


def tdir() -> Path:
    from .core.paths import vp_home
    return vp_home() / "telemetry"


def spool_dir() -> Path:
    return tdir() / "spool"


def quarantine_dir() -> Path:
    return tdir() / "quarantine"


def state_path() -> Path:
    return tdir() / "state.json"


def logs_dir() -> Path:
    from .core.paths import vp_home
    return vp_home() / "logs"


_VERSION: str | None = None


def plugin_version() -> str:
    global _VERSION
    if _VERSION is None:
        _VERSION = "unknown"
        for root in Path(__file__).resolve().parents:
            p = root / ".claude-plugin" / "plugin.json"
            if p.is_file():
                try:
                    _VERSION = str(json.loads(p.read_text(encoding="utf-8")).get("version") or "unknown")
                except (OSError, ValueError):
                    pass
                break
    return _VERSION


def install_id(create: bool = True) -> str | None:
    """This machine's random id: created once, kept in telemetry/install_id (0600)."""
    path = tdir() / "install_id"
    try:
        got = path.read_text(encoding="utf-8").strip()
        if ID_RE.match(got):
            return got
    except OSError:
        pass
    if not create:
        return None
    new = secrets.token_urlsafe(24)           # 32 chars of [A-Za-z0-9_-]
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:                   # another process won the race: use its id
        got = path.read_text(encoding="utf-8").strip()
        return got if ID_RE.match(got) else new
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new + "\n")
    return new


# ---- state --------------------------------------------------------------------------------------

def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(data: dict[str, Any]) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".state.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _update_state(**changes: Any) -> dict[str, Any]:
    st = _read_state()
    st.update(changes)
    _write_state(st)
    return st


# ---- recording ----------------------------------------------------------------------------------

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clean(value: Any, key: str = "") -> Any:
    k = key.lower()
    if k and k not in _SAFE_KEYS and any(m in k for m in _SECRET_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(kk): _clean(v, str(kk)) for kk, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, str):
        from .journal import redact
        s = redact(value)
        return s if len(s) <= MAX_STRING else s[:MAX_STRING] + f"...[+{len(s) - MAX_STRING} chars]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clean(str(value))


def make_event(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    rec: dict[str, Any] = {"ts": _utc_iso(), "kind": str(kind)[:40],
                           "version": plugin_version(),
                           "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
                           "python": platform.python_version()}
    for k, v in data.items():
        if k not in ("ts", "kind") and k not in RESERVED_KEYS:
            rec[k] = _clean(v, k)
    return rec


def _line(rec: dict[str, Any]) -> str:
    line = json.dumps(rec, ensure_ascii=False, default=str)
    if len(line.encode("utf-8")) <= MAX_LINE:
        return line
    # too long: cut every long string harder, then give up on the payload
    small = {k: (v[:1000] + "...[cut]" if isinstance(v, str) and len(v) > 1000 else v)
             for k, v in rec.items()}
    small["truncated"] = True
    line = json.dumps(small, ensure_ascii=False, default=str)
    if len(line.encode("utf-8")) <= MAX_LINE:
        return line
    return json.dumps({k: rec[k] for k in ("ts", "kind", "version", "os", "python")}
                      | {"truncated": True}, ensure_ascii=False)


def record(kind: str, **data: Any) -> bool:
    """Append one event to the spool. Never raises; returns False when nothing was written."""
    if is_off():
        return False
    try:
        line = _line(make_event(kind, data)) + "\n"
        d = spool_dir()
        d.mkdir(parents=True, exist_ok=True)
        install_id()
        cur = d / "current.jsonl"
        fd = os.open(cur, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            size = cur.stat().st_size
        except OSError:
            size = 0
        if size >= ROTATE_BYTES:
            rotate()
        return True
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        return False


def record_error(where: str, exc: BaseException, **data: Any) -> bool:
    """An uncaught error: type, message and the last frames of the traceback."""
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return record("engine_error", where=where, error=type(exc).__name__, message=str(exc)[:2000],
                  traceback="".join(tb)[-6000:], **data)


# ---- spool → batches ----------------------------------------------------------------------------

def _new_batch_path(d: Path | None = None) -> Path:
    return (d or spool_dir()) / f"batch-{uuid.uuid4()}.jsonl"


def rotate() -> list[Path]:
    """Close the open spool into batch files (each within ROTATE_BYTES / ROTATE_LINES)."""
    cur = spool_dir() / "current.jsonl"
    if not cur.is_file() or cur.stat().st_size == 0:
        return []
    staged = spool_dir() / f".rotating-{uuid.uuid4()}.jsonl"
    try:
        os.replace(cur, staged)
    except OSError:
        return []
    out: list[Path] = []
    try:
        lines = staged.read_text(encoding="utf-8", errors="replace").splitlines()
        chunk: list[str] = []
        size = 0
        for ln in lines:
            if not ln.strip():
                continue
            n = len(ln.encode("utf-8")) + 1
            if chunk and (size + n > ROTATE_BYTES or len(chunk) >= ROTATE_LINES):
                out.append(_write_batch(chunk))
                chunk, size = [], 0
            chunk.append(ln)
            size += n
        if chunk:
            out.append(_write_batch(chunk))
    finally:
        with contextlib.suppress(OSError):
            staged.unlink()
    enforce_cap()
    return out


def _write_batch(lines: list[str], d: Path | None = None) -> Path:
    p = _new_batch_path(d)
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def batches() -> list[Path]:
    d = spool_dir()
    if not d.is_dir():
        return []
    return sorted(d.glob("batch-*.jsonl"), key=lambda p: (p.stat().st_mtime, p.name))


def _batch_id(p: Path) -> str:
    return p.stem[len("batch-"):]


def enforce_cap(cap: int = CAP_BYTES) -> int:
    """Drop the oldest batches (then quarantined files) while everything kept exceeds `cap`."""
    files = []
    for d in (spool_dir(), quarantine_dir()):
        if d.is_dir():
            files += [p for p in d.iterdir() if p.is_file()]
    sized = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        sized.append((st.st_mtime, p, st.st_size))
    total = sum(s for _, _, s in sized)
    dropped = 0
    for _, p, s in sorted(sized, key=lambda t: t[0]):
        if total <= cap:
            break
        if p.name == "current.jsonl":
            continue
        with contextlib.suppress(OSError):
            p.unlink()
            total -= s
            dropped += 1
    return dropped


# ---- sending ------------------------------------------------------------------------------------

class _Result:
    def __init__(self, code: int | None, body: str = "", retry_after: float | None = None,
                 error: str | None = None):
        self.code, self.body, self.retry_after, self.error = code, body, retry_after, error


def _post(lines: list[str], batch: str, iid: str) -> _Result:
    body = gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))
    req = urllib.request.Request(
        base_url() + "/telemetry", data=body, method="POST",
        headers={"Content-Type": "application/x-ndjson", "Content-Encoding": "gzip",
                 "X-Elsa-Install": iid, "X-Elsa-Version": plugin_version(),
                 "X-Elsa-Batch": batch, "User-Agent": f"ai-movie-company/{plugin_version()}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return _Result(r.status, r.read(65536).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        ra = None
        raw = e.headers.get("Retry-After") if e.headers else None
        if raw:
            try:
                ra = float(raw)
            except ValueError:
                ra = None
        try:
            text = e.read(65536).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        return _Result(e.code, text, ra)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return _Result(None, error=f"{type(e).__name__}: {e}"[:300])


def _why(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body[:500]
    if isinstance(data, dict):
        parts = [f"{k}={data[k]}" for k in ("error", "header", "line", "reason") if k in data]
        if parts:
            return "; ".join(str(x) for x in parts)[:500]
    return body[:500]


def _quarantine(p: Path, why: str) -> None:
    q = quarantine_dir()
    q.mkdir(parents=True, exist_ok=True)
    dest = q / p.name
    os.replace(p, dest)
    (q / (p.stem + ".note.txt")).write_text(f"{_utc_iso()} {why}\n", encoding="utf-8")


@contextlib.contextmanager
def _flush_lock() -> Iterator[bool]:
    """One flush at a time per machine (non-blocking)."""
    import fcntl
    d = tdir()
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / "flush.lock", "a+")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def flush(max_batches: int = MAX_BATCHES_PER_FLUSH, now: float | None = None) -> dict[str, Any]:
    """Send waiting batches. Never raises. Returns a summary."""
    out: dict[str, Any] = {"sent": 0, "duplicates": 0, "split": 0, "quarantined": 0,
                           "left": 0, "stopped": None}
    if is_off():
        out["stopped"] = "off"
        return out
    try:
        with _flush_lock() as got:
            if not got:
                out["stopped"] = "another flush is running"
                return out
            return _flush_locked(out, max_batches, now)
    except Exception as e:  # noqa: BLE001
        out["stopped"] = f"error: {type(e).__name__}: {e}"[:300]
        return out


def _flush_locked(out: dict[str, Any], max_batches: int, now: float | None) -> dict[str, Any]:
    now = time.time() if now is None else now
    st = _update_state(last_flush_at=now)
    with contextlib.suppress(Exception):
        import_pending_installs()
    if float(st.get("retry_after") or 0) > now:
        out["stopped"] = "retry_after"
        out["left"] = len(batches())
        return out
    rotate()
    iid = install_id() or ""
    attempts = 0
    queue = batches()
    while queue and attempts < max_batches:
        p = queue.pop(0)
        attempts += 1
        try:
            lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip()]
        except OSError:
            continue
        if not lines:
            p.unlink(missing_ok=True)
            continue
        res = _post(lines, _batch_id(p), iid)
        if res.code == 200:
            p.unlink(missing_ok=True)
            out["sent"] += 1
            try:
                if json.loads(res.body).get("duplicate"):
                    out["duplicates"] += 1
            except (ValueError, AttributeError):
                pass
            continue
        if res.code == 413:
            if len(lines) > 1:
                half = len(lines) // 2
                a = _write_batch(lines[:half])
                b = _write_batch(lines[half:])
                p.unlink(missing_ok=True)
                queue[:0] = [a, b]
                out["split"] += 1
            else:
                _quarantine(p, "413: a single line is too large for the receiver")
                out["quarantined"] += 1
            continue
        if res.code in (400, 415):
            # 400: the body names the bad `header` or the `line` and its `reason`; 415: an
            # unsupported Content-Encoding. Never resent as is.
            _quarantine(p, f"{res.code}: {_why(res.body)}")
            out["quarantined"] += 1
            continue
        if res.code == 429 or (res.code == 503 and res.retry_after is not None):
            wait = res.retry_after if res.retry_after is not None else DEFAULT_RETRY_AFTER_S
            _update_state(retry_after=now + wait)
            out["stopped"] = f"429 retry after {int(wait)} s"
            break
        # 5xx, other codes, timeout, no network: keep it, try later
        _update_state(retry_after=now + OFFLINE_BACKOFF_S)
        out["stopped"] = f"http {res.code}" if res.code is not None else (res.error or "offline")
        break
    out["left"] = len(batches())
    return out


def spawn_flush(force: bool = False) -> bool:
    """Start `vp telemetry flush` detached, so the caller never waits. Throttled: at most every
    FLUSH_EVERY_S unless `force`; nothing to send or a pending retry_after -> no process."""
    if is_off():
        return False
    try:
        st = _read_state()
        now = time.time()
        if float(st.get("retry_after") or 0) > now:
            return False
        if not force and now - float(st.get("last_spawn_at") or 0) < FLUSH_EVERY_S:
            return False
        cur = spool_dir() / "current.jsonl"
        pending_installs = bool(_unimported_install_logs())
        if not batches() and not (cur.is_file() and cur.stat().st_size > 0) and not pending_installs:
            return False
        _update_state(last_spawn_at=now)
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        subprocess.Popen([sys.executable, "-m", "vp", "telemetry", "flush"], env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- the installer's log --------------------------------------------------------------------------

def _unimported_install_logs() -> list[Path]:
    d = logs_dir()
    if not d.is_dir():
        return []
    done = set(_read_state().get("imported_installs") or [])
    return sorted(p for p in d.glob("install-*.log") if p.name not in done)


def import_install(path: Path) -> int:
    """Turn an installer log into events: one `install_step` per line, one `install_result`."""
    if is_off():
        return 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    n = 0
    status = None
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        if ln.startswith("STATUS:") or " STATUS: " in ln:
            status = ln.split("STATUS:", 1)[1].strip()
        if record("install_step", log=path.name, n=i, line=ln[:2000]):
            n += 1
    record("install_result", log=path.name, status=status or "unfinished", lines=n)
    done = list(_read_state().get("imported_installs") or [])
    if path.name not in done:
        done.append(path.name)
        _update_state(imported_installs=done[-200:])
    return n


def import_pending_installs() -> int:
    return sum(import_install(p) for p in _unimported_install_logs())


# ---- status / CLI -------------------------------------------------------------------------------

def status() -> dict[str, Any]:
    cur = spool_dir() / "current.jsonl"
    q = quarantine_dir()
    st = _read_state()
    bs = batches()
    return {"enabled": not is_off(), "url": base_url(), "install_id": install_id(create=False),
            "version": plugin_version(), "dir": str(tdir()),
            "open_spool_bytes": cur.stat().st_size if cur.is_file() else 0,
            "batches": len(bs), "batch_bytes": sum(p.stat().st_size for p in bs),
            "quarantined": len(list(q.glob("batch-*.jsonl"))) if q.is_dir() else 0,
            "retry_after": st.get("retry_after"), "last_flush_at": st.get("last_flush_at"),
            "install_logs_waiting": len(_unimported_install_logs())}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp telemetry", description="test-period telemetry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    f = sub.add_parser("flush")
    f.add_argument("--detach", action="store_true", help="start the flush in the background and return")
    f.add_argument("--json", action="store_true")
    sub.add_parser("show-id")
    im = sub.add_parser("import-install")
    im.add_argument("logs", nargs="*")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        st = status()
        print(json.dumps(st, ensure_ascii=False) if a.json
              else "\n".join(f"{k}: {v}" for k, v in st.items()))
        return 0
    if a.cmd == "flush":
        if a.detach:
            started = spawn_flush(force=True)
            print(json.dumps({"started": started}) if a.json else ("started" if started else "nothing to do"))
            return 0
        res = flush()
        print(json.dumps(res, ensure_ascii=False) if a.json
              else " ".join(f"{k}={v}" for k, v in res.items()))
        return 0
    if a.cmd == "show-id":
        print(install_id() or "")
        return 0
    if a.cmd == "import-install":
        n = sum(import_install(Path(p).expanduser()) for p in a.logs) if a.logs \
            else import_pending_installs()
        print(f"imported {n} lines")
        return 0
    return 2
