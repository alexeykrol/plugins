"""Step manifests and the freshness rule (SPEC §5, BUILD §3).

Manifest stages/<id>[.<item>].yaml:
  {stage, item, version, inputs: {relpath: sha256}, config_hash, config: {...},
   outputs: {relpath: sha256}, started_at, finished_at}

An instance is FRESH iff: the manifest exists, step version == current, input hashes == current,
config hash == current, and every recorded output exists with the recorded hash.
Unchanged upstream output => unchanged downstream input hashes => downstream stays fresh.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .errors import VPError
from .io import MISSING, now_iso, read_yaml, sha256_file, sha256_obj, write_yaml
from .paths import Job

if TYPE_CHECKING:  # pragma: no cover
    from .ctx import Ctx
    from .graph import StepSpec


def hash_files(job: Job, paths: Iterable[str | Path]) -> dict[str, str]:
    """{relpath: sha256 or MISSING} for the given files, in the given order (deduplicated)."""
    out: dict[str, str] = {}
    for p in paths:
        rel = job.rel(p)
        if rel not in out:
            out[rel] = sha256_file(job.abs(rel), progress_root=job.root)
    return out


def read_manifest(job: Job, stage: str, item: str | None = None) -> dict[str, Any] | None:
    data = read_yaml(job.manifest_path(stage, item), default=None)
    return data if isinstance(data, dict) else None


def write_manifest(job: Job, stage: str, item: str | None, version: str,
                   inputs: dict[str, str], config: dict[str, Any], outputs: dict[str, str],
                   started_at: str, finished_at: str | None = None,
                   extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the manifest atomically (call LAST, after all outputs are written)."""
    m = {"stage": stage, "item": item, "version": str(version), "inputs": dict(inputs),
         "config_hash": sha256_obj(config), "config": config, "outputs": dict(outputs),
         "started_at": started_at, "finished_at": finished_at or now_iso()}
    if extra:
        m.update(extra)
    write_yaml(job.manifest_path(stage, item), m)
    return m


def delete_manifest(job: Job, stage: str, item: str | None = None) -> None:
    try:
        job.manifest_path(stage, item).unlink()
    except FileNotFoundError:
        pass


@dataclass
class Freshness:
    """Result of a freshness check. `inputs`/`config` are the CURRENT values (for the new manifest)."""

    fresh: bool
    reason: str
    inputs: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] | None = None


def check_fresh(spec: "StepSpec", ctx: "Ctx") -> Freshness:
    """Apply the freshness rule to one instance (ctx.item selects the instance)."""
    job = ctx.job
    inputs = hash_files(job, spec.inputs(ctx))
    config = spec.config(ctx) or {}
    m = read_manifest(job, spec.id, ctx.item)
    if m is None:
        return Freshness(False, "no manifest", inputs, config, None)
    if str(m.get("version")) != str(spec.version):
        return Freshness(False, f"version {m.get('version')} != {spec.version}", inputs, config, m)
    if (m.get("inputs") or {}) != inputs:
        old = m.get("inputs") or {}
        changed = sorted(k for k in set(old) | set(inputs) if old.get(k) != inputs.get(k))
        return Freshness(False, f"inputs changed: {changed[:5]}", inputs, config, m)
    if m.get("config_hash") != sha256_obj(config):
        return Freshness(False, "config changed", inputs, config, m)
    for rel, sha in (m.get("outputs") or {}).items():
        cur = sha256_file(job.abs(rel), progress_root=job.root)
        if cur == MISSING:
            return Freshness(False, f"output missing: {rel}", inputs, config, m)
        if cur != sha:
            return Freshness(False, f"output changed: {rel}", inputs, config, m)
    return Freshness(True, "fresh", inputs, config, m)


def hash_outputs(spec: "StepSpec", ctx: "Ctx", require_all: bool = True) -> dict[str, str]:
    """Hashes of the step's declared outputs after a run.

    require_all: a declared output that does not exist is a contract error (the step promised it).
    Outputs must lie inside the job folder.
    """
    job = ctx.job
    out = hash_files(job, spec.outputs(ctx))
    for rel, sha in list(out.items()):
        if Path(rel).is_absolute():
            raise VPError("internal", f"{spec.id}: output outside the job folder: {rel}")
        if sha == MISSING:
            if require_all:
                raise VPError("contract", f"{spec.id}: declared output not produced: {rel}")
            del out[rel]
    return out
