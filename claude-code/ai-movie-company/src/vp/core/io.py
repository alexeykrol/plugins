"""File helpers: atomic writes, yaml/json, hashing.

Every derived file in a job folder is written through `atomic_write` (temp file in the same
directory + fsync + os.replace), so a reader never sees a half-written file.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

from .errors import VPError

MISSING = "MISSING"  # hash marker for a file that does not exist
HASH_PROGRESS_BYTES = 256 * (1 << 20)

# libyaml loader when available (≈10x faster, same objects). Writing stays on the pure-Python
# dumper: the C emitter escapes astral-plane characters (emoji) differently, and file bytes
# must not depend on how PyYAML was built (hashes, manifests).
_Loader = getattr(yaml, "CSafeLoader", None) if getattr(yaml, "__with_libyaml__", False) else None
_Loader = _Loader or yaml.SafeLoader

_SHA_CACHE: dict[tuple[Path, int, int, int, int], str] = {}
_SHA_LOCK = Lock()


def now_iso() -> str:
    """Current UTC time, ISO 8601 with seconds and offset, e.g. 2026-11-17T10:00:00+00:00."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp written by now_iso() (or with a trailing Z)."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def atomic_write(path: str | os.PathLike, data: bytes | str) -> Path:
    """Write `data` to `path` atomically. Creates parent dirs. Returns the Path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return p


def dump_yaml(obj: Any) -> str:
    """Serialize to YAML: unicode kept, key order kept."""
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, default_flow_style=False)


def read_yaml(path: str | os.PathLike, default: Any = None) -> Any:
    """Load YAML; returns `default` if the file does not exist. Empty file -> default."""
    p = Path(path)
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as f:
        data = yaml.load(f, Loader=_Loader)
    return default if data is None else data


def write_yaml(path: str | os.PathLike, obj: Any) -> Path:
    """Atomically write YAML."""
    return atomic_write(path, dump_yaml(obj))


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    """Load JSON; returns `default` if the file does not exist."""
    p = Path(path)
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str | os.PathLike, obj: Any, indent: int | None = 2) -> Path:
    """Atomically write JSON (UTF-8, non-ASCII kept)."""
    return atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=indent) + "\n")


def canonical_json(obj: Any) -> str:
    """Canonical JSON text: sorted keys, no whitespace, non-ASCII kept."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(data: bytes | str) -> str:
    """Hex sha256 of bytes (str is UTF-8 encoded)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_obj(obj: Any) -> str:
    """Hex sha256 of the canonical JSON form of `obj`."""
    return sha256_bytes(canonical_json(obj))


class FileChangedError(VPError):
    def __init__(self):
        super().__init__("data", "the media file changed while it was being verified")


def _stat_identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino


def file_identity(path: Path) -> tuple[Path, int, int, int, int]:
    resolved = path.resolve(strict=True)
    return resolved, *_stat_identity(resolved.stat())


def sha256_file(path: str | os.PathLike, *, missing_ok: bool = True,
                progress_root: Path | None = None) -> str:
    """Hex SHA-256 of a stable file, or MISSING when allowed and initially absent.

    The process cache keys on resolved path, size, mtime_ns, ctime_ns and inode.
    An uncached large read uses the active tracker or opens one at progress_root.
    """
    from . import progress

    p = Path(path)
    with _SHA_LOCK:
        try:
            key = file_identity(p)
        except FileNotFoundError:
            if missing_ok:
                return MISSING
            raise
        cached = _SHA_CACHE.get(key)
        if cached is not None:
            return cached

        resolved, size, *_ = key
        report = size > HASH_PROGRESS_BYTES
        tracking = (progress.tracking(progress_root, "verify recording", None, total=size, unit="bytes")
                    if report and progress_root is not None and progress.current() is None else nullcontext())
        with tracking as tracker:
            start = progress.clock() if report else None
            if report and tracker is None:
                progress.report(0, size, "bytes", since=start, force=True)
            h = hashlib.sha256()
            done = 0
            try:
                with open(resolved, "rb") as f:
                    if _stat_identity(os.fstat(f.fileno())) != key[1:]:
                        raise FileChangedError()
                    for block in iter(lambda: f.read(1 << 20), b""):
                        h.update(block)
                        done += len(block)
                        if report:
                            progress.report(done, size, "bytes", since=start, force=done == size)
                    if _stat_identity(os.fstat(f.fileno())) != key[1:] or file_identity(p) != key:
                        raise FileChangedError()
            except FileNotFoundError as exc:
                raise FileChangedError() from exc
        digest = h.hexdigest()
        _SHA_CACHE[key] = digest
        return digest


def append_line(path: str | os.PathLike, line: str) -> None:
    """Append one line (newline added) to a journal file; creates parent dirs.

    Used for append-only JSONL journals (costs, logs); a single small write is atomic enough
    with O_APPEND on POSIX.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
