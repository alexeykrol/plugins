"""Content baselines for optional inputs added after the 0.8.8 manifests."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .errors import VPError
from .io import MISSING, read_json, sha256_file, write_json
from .manifest import read_manifest
from .paths import Job

STAGES = ("S0", "S2")
_preview: ContextVar[tuple[Path, dict[str, str]] | None] = ContextVar("adoption_preview", default=None)


def path_for(job: Job) -> Path:
    return job.stages_dir / "adoption.json"


def _paths(job: Job) -> tuple[Path, ...]:
    from ..transcribe import META_NAME, RAW_NAME

    return job.caption_words, job.caption_choice, job.root / META_NAME, job.root / RAW_NAME


def _digests(job: Job) -> dict[str, str]:
    return {job.rel(path): digest for path in _paths(job) if path.is_file()
            if (digest := sha256_file(path)) != MISSING}


def _baseline(job: Job) -> dict[str, str] | None:
    path = path_for(job)
    if path.is_symlink() or path.parent.resolve() != path.parent:
        raise VPError("data", "the adoption record must stay in the job's stages directory")
    data = read_json(path, default=None)
    if data is not None and (not isinstance(data, dict) or data.get("version") != 1
                             or not isinstance(data.get("inputs"), dict)):
        raise VPError("data", "the job's adoption record is invalid")
    return data["inputs"] if data is not None else None


def adopt(job: Job) -> None:
    """Persist the pre-write baseline while holding the job lock."""
    from ..transcribe import guard_inputs

    path = path_for(job)
    guard_inputs(job, path)
    if _baseline(job) is None:
        write_json(path, {"version": 1, "inputs": _digests(job)})


@contextmanager
def preview(job: Job):
    """Reuse one in-memory baseline when planning both stages."""
    baseline = _baseline(job)
    token = _preview.set((job.root, _digests(job) if baseline is None else baseline))
    try:
        yield
    finally:
        _preview.reset(token)


def recognition_inputs(job: Job, stage: str) -> list[Path]:
    """Keep recorded paths exact, and include real changes to previously unlisted files."""
    previous = read_manifest(job, stage)
    recorded = (previous or {}).get("inputs") or {}
    current = _digests(job)
    held = _preview.get()
    baseline = held[1] if held is not None and held[0] == job.root else _baseline(job)
    if baseline is None:
        baseline = current
    return [path for path in _paths(job)
            if (name := job.rel(path)) in recorded
            or (name in current if previous is None else current.get(name) != baseline.get(name))]


def existing_inputs(job: Job) -> list[Path]:
    """Inputs to record once the current engine has actually executed the step."""
    return [path for path in _paths(job) if path.is_file()]


def input_names(job: Job) -> set[str]:
    return {job.rel(path) for path in _paths(job)}
