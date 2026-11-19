"""File helpers: atomic writes, yaml/json, hashing.

Every derived file in a job folder is written through `atomic_write` (temp file in the same
directory + fsync + os.replace), so a reader never sees a half-written file.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

MISSING = "MISSING"  # hash marker for a file that does not exist

# libyaml loader when available (≈10x faster, same objects). Writing stays on the pure-Python
# dumper: the C emitter escapes astral-plane characters (emoji) differently, and file bytes
# must not depend on how PyYAML was built (hashes, manifests).
_Loader = getattr(yaml, "CSafeLoader", None) if getattr(yaml, "__with_libyaml__", False) else None
_Loader = _Loader or yaml.SafeLoader

_SHA_CACHE: dict[tuple, str] = {}


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


def sha256_file(path: str | os.PathLike) -> str:
    """Hex sha256 of a file's bytes, or MISSING if it does not exist.

    Cached in-process by (path, inode, size, mtime_ns): atomic rewrites change the inode.
    """
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        return MISSING
    key = (str(p.resolve()), st.st_ino, st.st_size, st.st_mtime_ns)
    cached = _SHA_CACHE.get(key)
    if cached is not None:
        return cached
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
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
