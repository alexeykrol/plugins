"""Fetch, activate, and read signed knowledge packs."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .. import telemetry
from ..scenarios import _raw
from .envelope import verify
from .schema import MAX_ENVELOPE, PackError, decode_json, semver

TIMEOUT_S = 3.0
SYNC_EVERY_S = 3600
ENV_SWITCH = "AIMC_KNOWLEDGE_SYNC"
# Accepted terms versions under which the plugin may ask our server for a knowledge pack. EMPTY:
# the published terms do not name this data flow (even without identifiers the server sees an
# address and a time), so under them no request is made and the release baseline carries all the
# knowledge. The terms version that names it (roadmap 1.2) is added here, together with
# telemetry.TERMS_ALLOWING_RUN. Never add a version whose text does not name the knowledge update.
TERMS_ALLOWING_KNOWLEDGE_SYNC: frozenset[str] = frozenset()
KEEP_PACKS = 5
BASELINE_DIR = Path(__file__).resolve().parent / "baseline"
ETAG = re.compile(r'(?:W/)?"[!#-~]{1,128}"')


def _root():
    from ..core.paths import vp_home

    return vp_home() / "kpack"


def _pack_path(root, version):
    return root / "packs" / f"{version}.json"


def _valid_version(value):
    return type(value) is int and 1 <= value <= 2**63 - 1


def _read_state(root):
    try:
        with (root / "state.json").open("rb") as stream:
            raw = stream.read(16385)
        state = json.loads(raw) if len(raw) <= 16384 else None
    except (OSError, ValueError, RecursionError):
        state = None
    if not isinstance(state, dict):
        state = {}
    for key in ("active", "previous", "pinned_below", "etag_version"):
        if not _valid_version(state.get(key)):
            state[key] = None
    return state


def _write_state(root, state):
    from ..core.io import atomic_write

    atomic_write(root / "state.json", json.dumps(state, indent=2) + "\n")


@contextlib.contextmanager
def _state_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _compatible(pack):
    return semver(pack["min_plugin"]) <= semver(telemetry.plugin_version())


def _read_pack(root, version, public_keys):
    if version is None:
        return None
    try:
        with _pack_path(root, version).open("rb") as stream:
            pack = verify(stream.read(MAX_ENVELOPE + 1), public_keys)
        if pack["version"] == version and _compatible(pack):
            return pack
    except (OSError, PackError):
        pass
    return None


def _usable_pack(root, state, public_keys):
    for key in ("active", "previous"):
        pack = _read_pack(root, state.get(key), public_keys)
        if pack is not None:
            return pack
    return None


def read_content(directory=BASELINE_DIR):
    """Read pack content files, with optional scenario overrides."""
    def read(name):
        with (directory / f"{name}.json").open("rb") as stream:
            data = stream.read(MAX_ENVELOPE + 1)
        if len(data) > MAX_ENVELOPE:
            raise PackError("size", "input exceeds 512 KiB")
        return decode_json(data)

    content = {name: read(name) for name in ("cases", "rules", "hints")}
    try:
        content["scenarios"] = read("scenarios")
    except FileNotFoundError:
        content["scenarios"] = []
    return content


def _content(pack):
    scenarios = {s["id"]: s for s in _raw()[0]["scenarios"]}
    content = pack["content"] if pack else read_content()
    scenarios.update((s["id"], s) for s in content["scenarios"])
    return {**content, "scenarios": list(scenarios.values())}


def load(*, public_keys=None):
    """Effective content, checking the active and previous envelopes before using either."""
    root = _root()
    return _content(_usable_pack(root, _read_state(root), public_keys))


def status():
    root = _root()
    state = _read_state(root)
    pack = _usable_pack(root, state, None)
    return {**state, "effective": pack["version"] if pack else None}


def sync_allowed() -> bool:
    """May the plugin ask the server for a pack: only when the terms the person ACCEPTED are a
    version that names the knowledge update (TERMS_ALLOWING_KNOWLEDGE_SYNC)."""
    if not TERMS_ALLOWING_KNOWLEDGE_SYNC:
        return False
    try:
        from ..safety import consent_state
        st = consent_state()
        return bool(st["accepted"]) and st.get("accepted_version") in TERMS_ALLOWING_KNOWLEDGE_SYNC
    except Exception:  # noqa: BLE001 — unreadable consent = not allowed
        return False


def _channel():
    from ..core.config import config_path
    from ..core.io import read_yaml

    cfg = read_yaml(config_path(), default={})
    return "canary" if isinstance(cfg, dict) and cfg.get("knowledge_channel") == "canary" else "stable"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(url, etag):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment or "\\" in url):
        raise PackError("url", "knowledge packs require an HTTPS endpoint")
    headers = {"Accept": "application/json"}
    if etag is not None:
        headers["If-None-Match"] = etag
    req = Request(url, headers=headers)
    deadline = time.monotonic() + TIMEOUT_S
    try:
        with build_opener(_NoRedirect()).open(req, timeout=TIMEOUT_S) as response:
            if response.status != 200:
                raise PackError("http", "unexpected HTTP status")
            raw = bytearray()
            while len(raw) <= MAX_ENVELOPE:
                if time.monotonic() >= deadline:
                    raise TimeoutError("knowledge download timed out")
                chunk = response.read1(min(65536, MAX_ENVELOPE + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            return bytes(raw), response.headers.get("ETag")
    except HTTPError as exc:
        with exc:
            if exc.code == 304 and etag is not None:
                return None, etag
            raise PackError(f"http_{exc.code}", "knowledge endpoint rejected the request") from exc


def _etag(state, url, root, public_keys):
    tag = state.get("etag")
    active = state.get("active")
    if (state.get("etag_url") == url and state.get("etag_version") == active
            and isinstance(tag, str) and ETAG.fullmatch(tag)
            and _read_pack(root, active, public_keys) is not None):
        return tag
    return None


def _stamp(state, error=None):
    state.update(last_sync_at=datetime.now(timezone.utc).isoformat(), last_error=error)


def _prune(root, active, previous):
    packs = root / "packs"
    versions = sorted((int(p.stem) for p in packs.glob("*.json")
                       if re.fullmatch(r"[1-9][0-9]{0,18}", p.stem)), reverse=True)
    protected = {v for v in (active, previous) if v is not None}
    newest = [v for v in versions if v not in protected][:KEEP_PACKS - len(protected)]
    for version in versions:
        if version not in protected and version not in newest:
            with contextlib.suppress(OSError):
                _pack_path(root, version).unlink()


def _apply(root, raw, pack, url, tag, public_keys):
    from ..core.io import atomic_write

    with _state_lock(root):
        state = _read_state(root)
        _stamp(state)
        if pack is None:
            result = "unchanged"
        elif pack["version"] <= max(state.get("active") or 0, state.get("pinned_below") or 0):
            result = "ignored_version"
        elif not _compatible(pack):
            result = "skipped_plugin_version"
        else:
            old = _usable_pack(root, state, public_keys)
            version = pack["version"]
            atomic_write(_pack_path(root, version), raw)
            state.update(active=version, previous=old["version"] if old else None, pinned_below=None,
                         etag=tag if isinstance(tag, str) and ETAG.fullmatch(tag) else None,
                         etag_version=version, etag_url=url)
            result = "activated"
        _write_state(root, state)
        if result == "activated":
            _prune(root, state["active"], state["previous"])
        return result


def sync(*, public_keys=None):
    """Try one conditional GET — only under accepted terms that name it (sync_allowed)."""
    root = None
    try:
        root = _root()
        if not sync_allowed():
            return "skipped_terms"
        url = telemetry.base_url() + "/knowledge/" + _channel()
        tag = _etag(_read_state(root), url, root, public_keys)
        raw, tag = _fetch(url, tag)
        pack = None if raw is None else verify(raw, public_keys)
        return _apply(root, raw, pack, url, tag, public_keys)
    except Exception as exc:  # noqa: BLE001 — background delivery must never break the session
        error = exc.kind if isinstance(exc, PackError) else type(exc).__name__
        if root is not None:
            with contextlib.suppress(Exception), _state_lock(root):
                state = _read_state(root)
                _stamp(state, error)
                _write_state(root, state)
        return error


def spawn_sync():
    """Start a detached sync at most once an hour, only when sync_allowed().
    AIMC_KNOWLEDGE_SYNC=off (set by the test package) keeps the background process from starting."""
    try:
        if os.environ.get(ENV_SWITCH) == "off" or not sync_allowed():
            return False
        root = _root()
        with _state_lock(root):
            state = _read_state(root)
            now = datetime.now(timezone.utc)
            for key in ("last_sync_at", "last_spawn_at"):
                try:
                    age = now - datetime.fromisoformat(state.get(key))
                except (TypeError, ValueError):
                    continue
                if age.total_seconds() < SYNC_EVERY_S:
                    return False
            state["last_spawn_at"] = now.isoformat()
            _write_state(root, state)
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        subprocess.Popen([sys.executable, "-m", "vp", "kpack", "sync"], env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return True
    except Exception:  # noqa: BLE001 — background delivery must never break the project
        return False


def rollback(*, public_keys=None):
    """Return the restored version, 0 for the release baseline, or None if there is no active pack."""
    root = _root()
    with _state_lock(root):
        state = _read_state(root)
        if state["active"] is None:
            return None
        previous = _read_pack(root, state["previous"], public_keys)
        version = previous["version"] if previous else None
        state.update(active=version, previous=None,
                     pinned_below=max(state["active"], state["pinned_below"] or 0),
                     etag=None, etag_version=None, etag_url=None)
        _write_state(root, state)
        return version or 0
