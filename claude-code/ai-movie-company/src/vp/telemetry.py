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

`record()` never raises and never blocks on the network.

What is sent (owner, alpha 2026-11-20): technical telemetry only, built from an explicit allowlist
(`sanitize_event`, ALLOWED_KINDS / ALLOWED_FIELDS): event kind, command/step names, exit codes,
durations, counts, cost numbers, version, OS and Python versions, error class; the random install
id goes in a header. No paths, no messages or tracebacks, no free text. The sanitizer runs when an
event is recorded and again on every line right before it is sent.

The run signal (roadmap 1.1, vp.runsignal): `step` events of every route carry a step name from
the closed vocabulary ROUTE_STEPS, the `route` (a key of ROUTE_STEPS) and the `scenario` letter
(an id of knowledge/scenarios.json). Anything else in these fields is dropped. The random run
number of a job (`run`), the `route` and the `scenario` are in the allowlist but leave the
machine only when the ACCEPTED terms version is in TERMS_ALLOWING_RUN — empty today: the
published privacy policy names step names and the install id, not the run, the route or the
scenario a person chose.

Consent: nothing is recorded or sent before the person accepted the current terms (vp.safety
consent.json); events of that time are dropped, not queued; lines older than the acceptance are
never sent.

Off switch: `vp telemetry off` (config) or AIMC_TELEMETRY=off — nothing is recorded, nothing is sent.

CLI:  vp telemetry status [--json] | flush [--detach] [--json] | show-id
      vp telemetry on | off        the person's switch, kept in <VP_HOME>/config.yaml
                                   (telemetry.enabled); off also deletes what is waiting
      vp telemetry show [--json]   exactly what would be sent (the waiting events, or a sample);
                                   sends nothing
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
RESERVED_KEYS = ("_recv", "_ver")      # the receiver writes these itself
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


# ---- paths -----------------------------------------------------------------------------------------

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


# ---- consent and the switch -----------------------------------------------------------------------

def env_off() -> bool:
    return os.environ.get(ENV_SWITCH, "").strip().lower() in ("off", "0", "false", "no")


def config_off() -> bool:
    """`telemetry.enabled: false` in <VP_HOME>/config.yaml (`vp telemetry off`). Read without
    creating the config file."""
    from .core.config import config_path
    try:
        import yaml
        data = yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - no file / unreadable: the default (on)
        return False
    sec = data.get("telemetry") if isinstance(data, dict) else None
    return isinstance(sec, dict) and sec.get("enabled") is False


def is_off() -> bool:
    """The person's switch: env AIMC_TELEMETRY=off or `vp telemetry off`."""
    return env_off() or config_off()


def consent_accepted_at() -> datetime | None:
    """When the CURRENT terms were accepted (vp.safety consent.json); None = not accepted."""
    try:
        from .safety import consent_state
        st = consent_state()
        if not st["accepted"]:
            return None
        d = datetime.fromisoformat(str(st["accepted_at"]).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001 - unreadable consent = no consent
        return None


def can_send() -> bool:
    """Telemetry is recorded and sent only with the terms accepted and the switch on."""
    return not is_off() and consent_accepted_at() is not None


def why_not() -> str | None:
    if env_off():
        return f"off (env {ENV_SWITCH}=off)"
    if config_off():
        return "off (vp telemetry off)"
    if consent_accepted_at() is None:
        return "the terms are not accepted"
    return None


# ---- the allowlist: the ONLY fields that ever leave the machine ---------------------------------------
#
# Owner (alpha, 2026-11-20): nothing but technical telemetry leaves automatically — no dialogue, no
# content, no free text, no file or folder name, no macOS user name. An event is rebuilt from the
# allowlist below; every other field is dropped, and so is any value that is not of the expected
# shape. `sanitize_event` runs when an event is recorded AND again on every line right before it is
# sent (the sender applies it last), so a future caller cannot leak a path or a text.

ALLOWED_KINDS = frozenset({"command", "engine_error", "step", "cost", "auto_update",
                           "update_reminder_shown", "message_shown", "feedback_pack"})
# short machine words: a command, a state, an error class — never a path or a sentence
IDENT_FIELDS = frozenset({"where", "error", "event", "outcome", "state", "error_kind",
                          "gate", "cost_kind", "op", "model", "provider", "marketplace"})
VOCAB_FIELDS = frozenset({"command", "sub"})          # only known words, else "other"
PREFIX_FIELDS = frozenset({"task"})                   # only the family before the first dot
NUMBER_FIELDS = frozenset({"rc", "duration_s", "input_tokens", "output_tokens", "cache_read_tokens",
                           "cache_write_tokens", "usd", "units", "credits", "bytes", "days",
                           "number", "id"})
BOOL_FIELDS = frozenset({"on", "revealed", "estimate"})
# the run signal (roadmap 1.1): a value outside its closed set is dropped, never sent as text
CLOSED_FIELDS = frozenset({"step", "route", "scenario"})
RUN_FIELD = "run"                                     # gated by the accepted terms, see run_allowed
# Also gated by the accepted terms (same gate as the run number): which route and which scenario
# a person chose is not named by the published privacy policy, which names step names only. The
# receiver's funnel derives the route from the step name when the field is absent.
TERMS_GATED_FIELDS = frozenset({"route", "scenario"})
BASE_FIELDS = ("ts", "kind", "version", "os", "python")
ALLOWED_FIELDS = (frozenset(BASE_FIELDS) | IDENT_FIELDS | VOCAB_FIELDS | PREFIX_FIELDS
                  | NUMBER_FIELDS | BOOL_FIELDS | CLOSED_FIELDS | {RUN_FIELD})

# The steps of each route in funnel order: the ONLY step names that ever leave the machine and
# the only routes. Recording: the step ids of vp.steps.GRAPH; text: the stages of vp.text.STAGES
# (+ the sketches approval and the mastering); both: the exports and the moment the video is
# ready. services/telemetry/admin.py keeps a copy for the funnel (a test compares the two).
READY_STEP = "video_ready"
ROUTE_STEPS: dict[str, tuple[str, ...]] = {
    "recording": ("S0", "S1", "S2", "S3a", "S3b", "S4", "S6a", "S5", "S5v", READY_STEP,
                  "S7", "S6b", "S8", "S10", "S9", "PUB", "S11", "capcut_export", "fcpxml_export"),
    "text": ("brief", "script", "sketches", "approve_sketches", "voice", "music", "animation",
             "render", "master", READY_STEP, "capcut_export", "fcpxml_export"),
}
ROUTES = frozenset(ROUTE_STEPS)
STEPS = frozenset(s for steps in ROUTE_STEPS.values() for s in steps)

# Accepted terms versions under which the run number, the route and the scenario letter may leave
# the machine. EMPTY: the published privacy policy names only step names and the install id; the
# next terms version (roadmap 1.2) is added here once it exists and names them. Never add the
# current version without that text.
TERMS_ALLOWING_RUN: frozenset[str] = frozenset()
RUN_RE = re.compile(r"[0-9a-f]{16}")                  # secrets.token_hex(8); used with fullmatch

_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,63}$")
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?Z$")
_OS = re.compile(r"^[A-Za-z0-9 ._()+-]{1,80}$")
_VER_RX = re.compile(r"^[A-Za-z0-9._+-]{1,40}$")

KNOWN_SUBS = frozenset("""
ack add analyst apply approve authorize auto check clean clear-restart client-find client-install
close convert-token critic curve decide decline describe-set detect disconnect enable estimate eval
export flush forget generate get import install install-seed key layers list make matrix new note
observe off offer on pack picture plan poll preview quit reminder render replay restart-claude
rollback say scan scenes search services set setup show show-id snapshot splits stage status
studio undo version accept later withdraw run activate verify export-eval import-eval timeline
due shown request confirm master samples
""".split())


def _known_commands() -> frozenset[str]:
    try:
        from .cli import SUBCOMMAND_MAINS
        names = set(SUBCOMMAND_MAINS)
    except Exception:  # noqa: BLE001
        names = set()
    return frozenset(names | {"run", "status", "stage", "unlock", "approve-ingest", "judge-pending",
                              "resolve-upload"})


def _home_markers() -> tuple[str, ...]:
    marks = ["/Users/", "/home/", "~"]
    with contextlib.suppress(Exception):
        home = str(Path.home())
        if home and home != "/":
            marks.append(home)
            name = Path(home).name
            if len(name) >= 4:
                marks.append(name)
    return tuple(marks)


def _leaks(s: str, marks: tuple[str, ...]) -> bool:
    """Belt and braces after the shape checks: a path separator, the home dir or its user name."""
    if "/" in s or "\\" in s:
        return True
    low = s.lower()
    return any(m.lower() in low for m in marks)


_SCENARIOS: frozenset[str] | None = None


def known_scenarios() -> frozenset[str]:
    """The scenario letters of knowledge/scenarios.json (the closed set of `scenario`)."""
    global _SCENARIOS
    if _SCENARIOS is None:
        try:
            from .scenarios import scenario_list
            _SCENARIOS = frozenset(sid for sid in (str(s.get("id") or "") for s in scenario_list())
                                   if re.fullmatch(r"[A-Z]", sid))
        except Exception:  # noqa: BLE001 - unreadable data: no scenario is sent
            return frozenset()
    return _SCENARIOS


def run_allowed() -> bool:
    """May the run number leave the machine: only when the terms the person ACCEPTED are a
    version that names it (TERMS_ALLOWING_RUN)."""
    if not TERMS_ALLOWING_RUN:
        return False
    try:
        from .safety import consent_state
        st = consent_state()
        return bool(st["accepted"]) and st.get("accepted_version") in TERMS_ALLOWING_RUN
    except Exception:  # noqa: BLE001 - unreadable consent = not allowed
        return False


def _closed_ok(key: str, v: str) -> bool:
    if key == "step":
        return v in STEPS
    if key == "route":
        return v in ROUTES
    return v in known_scenarios()


def sanitize_event(rec: Any, allow_run: bool | None = None) -> dict[str, Any] | None:
    """The single gate of what is sent: a new dict with allowlisted fields of the right shape only;
    None when the event's kind is not allowlisted (the whole event is dropped). `allow_run`:
    whether the run number may stay (default: asked from the accepted terms, `run_allowed`)."""
    if not isinstance(rec, dict):
        return None
    kind = rec.get("kind")
    if kind not in ALLOWED_KINDS:
        return None
    marks = _home_markers()
    out: dict[str, Any] = {"kind": kind}
    ts = rec.get("ts")
    out["ts"] = ts if isinstance(ts, str) and _TS.match(ts) else _utc_iso()
    for key, rx in (("version", _VER_RX), ("os", _OS), ("python", _VER_RX)):
        v = rec.get(key)
        if isinstance(v, str) and rx.match(v) and not _leaks(v.replace("/", ""), marks):
            out[key] = v
    commands = None
    for key, v in rec.items():
        if key in BASE_FIELDS or key not in ALLOWED_FIELDS or v is None:
            continue
        if key in NUMBER_FIELDS:
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v \
                    and abs(v) < 1e15:
                out[key] = v
        elif key in BOOL_FIELDS:
            if isinstance(v, bool):
                out[key] = v
        elif not isinstance(v, str):
            continue
        elif key in CLOSED_FIELDS:
            if key in TERMS_GATED_FIELDS:
                if allow_run is None:
                    allow_run = run_allowed()
                if not allow_run:
                    continue
            if _closed_ok(key, v):
                out[key] = v
        elif key == RUN_FIELD:
            if RUN_RE.fullmatch(v):
                if allow_run is None:
                    allow_run = run_allowed()
                if allow_run:
                    out[key] = v
        elif key in VOCAB_FIELDS:
            if key == "command":
                commands = commands or _known_commands()
                out[key] = v if v in commands else "other"
            else:
                out[key] = v if v in KNOWN_SUBS else "other"
        elif key in PREFIX_FIELDS:
            fam = v.split(".", 1)[0]
            if _IDENT.match(fam) and not _leaks(fam, marks):
                out[key] = fam
        elif key in IDENT_FIELDS:
            if _IDENT.match(v) and not _leaks(v, marks):
                out[key] = v
    return out


# ---- recording ----------------------------------------------------------------------------------

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_event(kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """The event as it would be sent: the base fields + `data`, through the allowlist."""
    rec: dict[str, Any] = {"ts": _utc_iso(), "kind": str(kind)[:40],
                           "version": plugin_version(),
                           "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
                           "python": platform.python_version()}
    for k, v in data.items():
        if k not in ("ts", "kind") and k not in RESERVED_KEYS:
            rec[k] = v
    return sanitize_event(rec)


def _line(rec: dict[str, Any]) -> str:
    line = json.dumps(rec, ensure_ascii=False)
    if len(line.encode("utf-8")) <= MAX_LINE:
        return line
    return json.dumps({k: rec[k] for k in BASE_FIELDS if k in rec}, ensure_ascii=False)


def record(kind: str, **data: Any) -> bool:
    """Append one event to the spool. Never raises; returns False when nothing was written.
    Before the terms are accepted, or with the switch off, the event is dropped (not queued)."""
    try:
        if not can_send():
            return False
        ev = make_event(kind, data)
        if ev is None:
            return False
        line = _line(ev) + "\n"
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
    """An uncaught error: its class only (no message, no traceback — they carry paths and text)."""
    return record("engine_error", where=where, error=type(exc).__name__, **data)


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


def _rewrite(p: Path, lines: list[str]) -> None:
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, p)


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


def _ts_of(ev: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ev.get("ts")).replace("Z", "+00:00"))
    except ValueError:
        return None


def outgoing(lines: list[str], since: datetime | None) -> list[str]:
    """The lines exactly as they leave: each one parsed and passed through `sanitize_event` (the
    last step before the network); events of unknown kinds, unreadable lines and events recorded
    before the terms were accepted (`since`) are dropped."""
    out = []
    allow_run = run_allowed()
    for ln in lines:
        try:
            ev = sanitize_event(json.loads(ln), allow_run)
        except ValueError:
            continue
        if ev is None:
            continue
        if since is not None:
            when = _ts_of(ev)
            if when is None or when < since:
                continue
        out.append(_line(ev))
    return out


def _post(lines: list[str], batch: str, iid: str) -> _Result:
    if not ID_RE.match(iid or ""):
        return _Result(None, error="no valid install id")
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
    if consent_accepted_at() is None:
        out["stopped"] = "no consent"
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
    since = consent_accepted_at()
    if since is None:
        out["stopped"] = "no consent"
        return out
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
        lines = outgoing(lines, since)
        if not lines:               # nothing allowlisted in it: never sent, removed
            p.unlink(missing_ok=True)
            continue
        _rewrite(p, lines)          # on disk now exactly what is sent
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
    if not can_send():
        return False
    try:
        st = _read_state()
        now = time.time()
        if float(st.get("retry_after") or 0) > now:
            return False
        if not force and now - float(st.get("last_spawn_at") or 0) < FLUSH_EVERY_S:
            return False
        cur = spool_dir() / "current.jsonl"
        if not batches() and not (cur.is_file() and cur.stat().st_size > 0):
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


# ---- status / switch / preview / CLI ------------------------------------------------------------

def status() -> dict[str, Any]:
    cur = spool_dir() / "current.jsonl"
    q = quarantine_dir()
    st = _read_state()
    bs = batches()
    acc = consent_accepted_at()
    return {"enabled": not is_off(), "sending": can_send(), "why_not": why_not(),
            "switch": ("env" if env_off() else "config" if config_off() else "on"),
            "terms_accepted": acc is not None, "url": base_url(),
            "install_id": install_id(create=False), "version": plugin_version(), "dir": str(tdir()),
            "open_spool_bytes": cur.stat().st_size if cur.is_file() else 0,
            "batches": len(bs), "batch_bytes": sum(p.stat().st_size for p in bs),
            "quarantined": len(list(q.glob("batch-*.jsonl"))) if q.is_dir() else 0,
            "retry_after": st.get("retry_after"), "last_flush_at": st.get("last_flush_at")}


def drop_pending() -> int:
    """Delete everything waiting to be sent (open spool, batches, quarantine). Returns files removed."""
    n = 0
    for d in (spool_dir(), quarantine_dir()):
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file():
                    with contextlib.suppress(OSError):
                        p.unlink()
                        n += 1
    return n


def set_enabled(on: bool) -> dict[str, Any]:
    """`vp telemetry on|off`, kept in <VP_HOME>/config.yaml (telemetry.enabled). Off also deletes
    what is waiting, so nothing recorded before is sent later."""
    from .core.config import set_config_value
    set_config_value("telemetry", "enabled", bool(on))
    dropped = 0 if on else drop_pending()
    return {"enabled": not is_off(), "dropped_files": dropped, "why_not": why_not()}


def pending_lines() -> list[str]:
    """What waits on disk, as it would be sent (sanitized), without sending or changing anything."""
    raw: list[str] = []
    cur = spool_dir() / "current.jsonl"
    for p in ([cur] if cur.is_file() else []) + batches():
        with contextlib.suppress(OSError):
            raw += [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                    if ln.strip()]
    return outgoing(raw, consent_accepted_at())


def preview() -> dict[str, Any]:
    """`vp telemetry show`: exactly what would be sent, the headers included; sends nothing."""
    lines = pending_lines()
    sample = not lines
    if sample:
        evs = [make_event("command", {"command": "run", "sub": None, "rc": 0, "duration_s": 12.5}),
               # a step of a job: the run number is in it only when the accepted terms allow it
               make_event("step", {"event": "end", "step": "render", "route": "text",
                                   "scenario": "D", "outcome": "done", "duration_s": 95.0,
                                   RUN_FIELD: "0" * 16})]
        lines = [_line(ev) for ev in evs if ev]
    iid = install_id(create=False)
    return {"would_send_now": can_send(), "why_not": why_not(), "sample": sample,
            "run_number_sent": run_allowed(),
            "url": base_url() + "/telemetry",
            "headers": {"X-Elsa-Install": iid or "(created when the first event is recorded)",
                        "X-Elsa-Version": plugin_version(), "X-Elsa-Batch": "(a random id per batch)",
                        "User-Agent": f"ai-movie-company/{plugin_version()}"},
            "events": [json.loads(ln) for ln in lines],
            "allowed_fields": sorted(ALLOWED_FIELDS), "allowed_kinds": sorted(ALLOWED_KINDS)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp telemetry", description="test-period telemetry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    f = sub.add_parser("flush")
    f.add_argument("--detach", action="store_true", help="start the flush in the background and return")
    f.add_argument("--json", action="store_true")
    sub.add_parser("show-id")
    sh = sub.add_parser("show", help="print exactly what would be sent; sends nothing")
    sh.add_argument("--json", action="store_true")
    sub.add_parser("on")
    sub.add_parser("off")
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
        print(install_id(create=False) or "(none yet)")
        return 0
    if a.cmd == "show":
        pv = preview()
        if a.json:
            print(json.dumps(pv, ensure_ascii=False, indent=1))
            return 0
        print("Telemetry: " + ("sent when online" if pv["would_send_now"]
                               else f"NOT sent now ({pv['why_not']})") + ". Nothing is sent by this command.")
        print(f"POST {pv['url']}")
        for k, v in pv["headers"].items():
            print(f"{k}: {v}")
        print("run number of a job, route and scenario: "
              + ("sent with its step events" if pv["run_number_sent"]
                 else "NOT sent (the accepted terms do not name them)"))
        print("(sample events: nothing is waiting)" if pv["sample"]
              else f"{len(pv['events'])} event(s) waiting:")
        for ev in pv["events"]:
            print(json.dumps(ev, ensure_ascii=False))
        return 0
    if a.cmd in ("on", "off"):
        res = set_enabled(a.cmd == "on")
        if a.cmd == "off":
            print("telemetry: OFF — nothing is recorded or sent"
                  + (f"; {res['dropped_files']} waiting file(s) deleted" if res["dropped_files"] else ""))
        else:
            print("telemetry: ON" + (f" (but nothing is sent: {res['why_not']})" if res["why_not"] else ""))
        return 0
    return 2
