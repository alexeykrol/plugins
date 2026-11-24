"""The runner: walks the graph, runs non-fresh instances, records status (BUILD §3–4).

Outcomes of one instance and what the runner does:
  done            manifest written last, status done (+ human_time if a gate was open)
  skipped         (step raised Skip) manifest with existing outputs, status skipped; counts as satisfied
  waiting_human   (WaitingHuman) status waiting_human with opened_at; the whole run stops, exit 0
  consent         (ConsentRequired) a real platform was about to be touched without the accepted
                  terms: status waiting_human (gate consent), output `REFUSED consent-required ...`,
                  nothing sent; the whole run stops, exit 5
  waiting_judgment(JudgmentPending) status waiting_judgment; run stops, exit 0
  inconclusive    (Inconclusive) status inconclusive + one notification; run stops, exit 0
  failed          VPError data/contract/transport (after the step's own retries) or internal bug;
                  descendants are blocked, independent instances continue; exit 2 (3 if internal)
  abandoned       auth error (at once) or the loop guard (3 consecutive failures); one notification;
                  later runs skip it until `stage ... --reset`

Gates do not notify from the runner — the gated step itself sends the owner what is needed.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from copy import copy
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable

from . import adoption
from .config import DEFAULTS, config_path, deep_merge, load_config, resolve_job_config
from .ctx import Ctx
from . import progress
from .errors import (EXIT_CONSENT_REQUIRED, ConsentRequired, Inconclusive, JudgmentPending, Skip,
                     VPError, WaitingHuman, WaitingQuota, failure_noted, note_failure)
from .graph import FreshCheckFailed, Graph, Instance, StepSpec
from .io import now_iso, parse_iso, read_yaml, sha256_file, sha256_obj
from .lock import holder, job_lock
from .lock import unlock as _unlock
from .log import scrub
from .manifest import Freshness, check_fresh, hash_files, hash_outputs, read_manifest, write_manifest
from .paths import Job, vp_home
from .status import Status

EXIT_OK, EXIT_FAILED, EXIT_INTERNAL = 0, 2, 3
_WAIT_STATES = ("waiting_human", "waiting_judgment", "inconclusive")
_STOP = "stop"

Out = Callable[[str], None]


def _failed_exit(kind: str, code: int = EXIT_FAILED) -> int:
    if not failure_noted():
        note_failure(kind=kind)
    return code


def prepare_config(job: Job, config: dict[str, Any] | None, *,
                   read_only: bool = False) -> dict[str, Any]:
    """Load <vp_home>/config.yaml if `config` is None, then resolve the job profile."""
    if config is None and read_only:
        user = read_yaml(config_path(), default={})
        if not isinstance(user, dict):
            raise VPError("data", "config.yaml must be a mapping")
        config = deep_merge(DEFAULTS, user)
        config["vp_home"] = str(vp_home())
    cfg = config if config is not None else load_config()
    if "profile" not in cfg:
        cfg = resolve_job_config(cfg, job.request(), job.design())
    return cfg


def _recording_route(job: Job, cfg: dict[str, Any],
                     whole_recording: str | None = None) -> dict[str, Any]:
    fragments = read_yaml(job.fragments_yaml, default=None)
    whole = fragments.get("whole_recording") if isinstance(fragments, dict) else None
    saved = whole.get("kind") if isinstance(whole, dict) else None
    if not job.fragments_yaml.exists():
        saved = ((read_manifest(job, "S3a") or {}).get("config") or {}).get("whole_recording")
    if saved in ("full", "reel"):
        if whole_recording and whole_recording != saved:
            raise VPError("data", f"the single clip is already set to {saved}; use a new job folder "
                                  f"for --whole-recording {whole_recording}")
        return {**cfg, "whole_recording": saved}
    if whole_recording:
        if any(p.exists() for p in (job.fragments_yaml, job.fragments_proposed, job.fragments_review)):
            raise VPError("data", "fragment selection has already started; use a new job folder "
                                  "for --whole-recording")
    return {**cfg, "whole_recording": whole_recording}


def make_ctx(job: Job, config: dict[str, Any], services: dict[str, Any] | None = None,
             factories: dict[str, Callable] | None = None,
             sleep: Callable[[float], None] | None = None) -> Ctx:
    """Base context for a run (item None). `services` = ready objects (judge/publisher/...)."""
    ctx = Ctx(job, None, config, factories=factories, **(services or {}))
    if sleep is not None:
        ctx.sleep = sleep
    return ctx


class _ReadOnlyProgress(progress.Tracker):
    def update(self, *args, **kwargs) -> bool:
        return False


@contextmanager
def _without_progress_writes(job: Job):
    # Hashing sees an active tracker, so it does not create its own progress files.
    token = progress._current.set(_ReadOnlyProgress(job.root / progress.FILE_NAME, "plan", None))
    try:
        yield
    finally:
        progress._current.reset(token)


def _plan_freshness(spec: StepSpec, ctx: Ctx) -> Freshness:
    manifest = read_manifest(ctx.job, spec.id, ctx.item)
    if manifest is None:
        return Freshness(False, "no manifest")
    if spec.id == "S3a" and hasattr(spec, "_store") and not ctx.config.get("whole_recording"):
        from ..knowledge.store import SEED_ID, KnowledgeStore, content_hash

        class SelectionSnapshot(KnowledgeStore):
            def active_version(self):
                if self.active_file.exists():
                    active = self.active_file.read_text(encoding="utf-8").strip()
                    if active not in self.list_versions():
                        raise VPError("data", "the active selection version is missing")
                    return active
                versions = self.list_versions()
                return versions[0] if versions else SEED_ID

            def manifest(self, version):
                if not self.active_file.exists() and not self.list_versions():
                    if not self.seed_dir.is_dir():
                        raise VPError("internal", "the selection seed is missing")
                    return {"content_hash": content_hash(self.seed_dir)}
                return super().manifest(version)

        spec = copy(spec)
        spec._store = lambda ctx: SelectionSnapshot(ctx.home)
    return check_fresh(spec, ctx)


def _changed_names(old: dict, new: dict, prefix: str = "") -> list[str]:
    names = []
    for key in sorted(old.keys() | new.keys(), key=str):
        if key in old and key in new and type(old[key]) is type(new[key]) and old[key] == new[key]:
            continue
        name = f"{prefix}{key}"
        if isinstance(old.get(key), dict) and isinstance(new.get(key), dict):
            names.extend(_changed_names(old[key], new[key], f"{name}."))
        else:
            names.append(name)
    return names


def _plan_reason(fr: Freshness) -> str:
    previous = fr.manifest or {}
    if fr.reason.startswith("inputs changed:"):
        names = _changed_names(previous.get("inputs") or {}, fr.inputs)
        reason = "inputs changed: " + ", ".join(names)
        if previous.get("config_hash") != sha256_obj(fr.config):
            fields = _changed_names(previous.get("config") or {}, fr.config)
            reason += f" (config changed: {', '.join(fields)})" if fields else " (config changed)"
        return reason
    if fr.reason == "config changed":
        names = _changed_names(previous.get("config") or {}, fr.config)
        return "config changed: " + ", ".join(names) if names else "config changed"
    if fr.reason.startswith("version "):
        return "version changed"
    return fr.reason


def _planned_gate(spec: StepSpec, ctx: Ctx, status: Status, inst: Instance) -> bool:
    """Recognize known gates on the current inputs without invoking a step or media probe."""
    entry = status.last(inst.step, inst.item) or {}
    meta = entry.get("metadata") or {}
    if entry.get("state") == "waiting_judgment" and meta.get("task_path"):
        task_dir = ctx.job.judge_dir(Path(meta["task_path"]).name)
        task = read_yaml(task_dir / "task.yaml", default={})
        if isinstance(task, dict) and task.get("output"):
            return not (task_dir / task["output"]).is_file()
    if entry.get("state") == "waiting_human" and meta.get("gate") == "consent":
        from ..safety import consent_state

        return ctx.publisher_kind == "youtube" and not consent_state().get("accepted")
    if inst.step == "S0" and type(spec).__module__ == "vp.steps.s0_intake":
        j = ctx.job
        return j.request().get("enabled") is not True or (
            j.source_video.is_file() and not j.source_srt.is_file())
    if spec.gate != "human" or ctx.config.get("whole_recording") and inst.step == "S3b":
        return False
    if inst.step == "S3b":
        from yaml import YAMLError

        from ..steps._fragments import Timeline, Word, _video_end_ms, norm_tokens
        from ..steps.s0_intake import staged_word_times
        from ..steps.s3b_review import check_review
        from .srt import read_srt

        j = ctx.job
        try:
            proposed = read_yaml(j.fragments_proposed, default={})
            review = read_yaml(j.fragments_review, default=None)
        except (YAMLError, UnicodeError):
            return True
        if not isinstance(review, dict) or not isinstance(proposed, dict) or "fragments" not in proposed:
            return True
        duration = _video_end_ms(j)
        words = None
        if proposed.get("boundary_mode") == "words":
            timing = staged_word_times(j, probe=lambda _: {"duration_ms": duration})
            words = [Word(token, start, end) for text, start, end, origin in timing.words
                     if origin != "inferred" for token in norm_tokens(text)]
        timeline = Timeline(read_srt(j.subtitles_srt), words, end_limit_ms=duration)
        checked = check_review(review, proposed, sha256_file(j.fragments_proposed), timeline, ctx.profile)
        return bool(checked.errors or checked.open)
    if inst.step == "S9":
        from ..position import approval_complete

        saved = status.open_wait(inst.step, inst.item)
        return bool(saved and not approval_complete(ctx.job))
    return bool(status.open_wait(inst.step, inst.item))


def _registers_timing_only(inst: Instance, ctx: Ctx, fr: Freshness) -> bool:
    """The closed review can survive a timing registration without new fingerprinted outputs."""
    if inst.step not in adoption.STAGES or not fr.manifest or not fr.reason.startswith("inputs changed:"):
        return False
    from ..steps.s0_intake import closed_review_matches

    manifest = fr.manifest
    changed = _changed_names(manifest.get("inputs") or {}, fr.inputs)
    if (not set(changed) <= adoption.input_names(ctx.job)
            or manifest.get("config_hash") != sha256_obj(fr.config)
            or not closed_review_matches(ctx.job)):
        return False
    outputs = manifest.get("outputs") or {}
    if inst.step == "S0":
        intake = read_yaml(ctx.job.job_yaml, default={})
        channel = intake.get("channel") if isinstance(intake, dict) else None
        expected_handle = (ctx.config.get("youtube") or {}).get("expected_channel_handle")
        if (not isinstance(intake, dict) or intake.get("job_id") != ctx.job.job_id
                or isinstance(channel, dict) and channel.get("expected_handle") != expected_handle):
            return False
        paths = [ctx.job.rel(ctx.job.job_yaml)]
    else:
        paths = [ctx.job.rel(ctx.job.subtitles_srt), ctx.job.rel(ctx.job.subtitles_debug)]
    return all(path in outputs and sha256_file(ctx.job.abs(path)) == outputs[path] for path in paths)


def _caption_failure(job: Job, item: str) -> str | None:
    from ..steps.s5_render import saved_caption_failure

    saved = read_manifest(job, "S5", item) or {}
    return saved_caption_failure(job, item, saved.get("outputs") or {})


def caption_failures(job: Job) -> dict[str, str]:
    """Measured failures for the current render, even if the status append was interrupted."""
    with _without_progress_writes(job):
        return {item: failure for item in job.items() or [] if
                (failure := _caption_failure(job, item)) is not None}


def plan(job: Job, *, steps: Iterable[StepSpec] | None = None,
         config: dict[str, Any] | None = None, whole_recording: str | None = None,
         through: str | None = None) -> dict:
    """Snapshot current keys and forecast work in run order, without any writers or hooks."""
    with _without_progress_writes(job), adoption.preview(job):
        cfg = _recording_route(job, prepare_config(job, config, read_only=True), whole_recording)
        ctx = make_ctx(job, cfg)
        graph = Graph(steps if steps is not None else _load_default_steps())
        items = job.items()
        status = Status(job)
        rows: dict[Instance, dict] = {}
        gate = None
        stages = set(graph.order[:graph.order.index(through) + 1]) if through else graph.specs
        for inst in graph.instances(items):
            if inst.step not in stages:
                break
            spec = graph.spec(inst.step)
            item_ctx = ctx.for_item(inst.item)
            pending = list(dict.fromkeys(root for dep in graph.deps_of(inst, items)
                                         for root in rows[dep]["pending_inputs"] +
                                         ([dep.key] if (rows[dep]["execute"] is True and
                                                        not rows[dep]["registers_timing_only"]) or
                                           rows[dep]["check"] == "pending" else [])))
            blocked = [dep.key for dep in graph.deps_of(inst, items)
                       if rows[dep]["blocked"] or rows[dep]["check"] == "failed"]
            if status.is_abandoned(inst.step, inst.item):
                blocked.append(inst.key)
            fr = _plan_freshness(spec, item_ctx)
            execute = False if gate or blocked else None if pending else not fr.fresh
            check = "none"
            caption_failed = False
            saved_check = None
            here = None
            if not gate and not blocked and not pending:
                if fr.fresh:
                    outputs = MappingProxyType(fr.manifest.get("outputs") or {})
                    check = spec.fresh_check(item_ctx, outputs)
                    if inst.step == "S5" and inst.item:
                        from ..steps.s5_render import saved_caption_check, saved_caption_failure

                        report = saved_caption_check(job, inst.item, outputs)
                        saved_check = ((report.get("checks") or {}).get("caption_sync") or {}).get("status")
                        caption_failed = saved_caption_failure(job, inst.item, outputs) is not None
                elif _planned_gate(spec, item_ctx, status, inst):
                    here = inst.key
            rows[inst] = {"stage": inst.step, "item": inst.item, "key": inst.key,
                          "execute": execute, "fresh": fr.fresh, "reason": _plan_reason(fr),
                          "blocked_by_gate": gate or here, "blocked": blocked,
                          "pending_inputs": pending, "check": check, "saved_check": saved_check,
                          "caption_failed": caption_failed,
                          "registers_timing_only": _registers_timing_only(inst, item_ctx, fr)}
            if fr.manifest is not None:
                rows[inst].update(
                    changed_inputs=_changed_names(fr.manifest.get("inputs") or {}, fr.inputs),
                    changed_config=_changed_names(fr.manifest.get("config") or {}, fr.config))
            if here:
                gate = here
        return {"steps": list(rows.values())}


class Runner:
    """Executes a graph of steps over one job. Use `run()` / `run_stage()` helpers below."""

    def __init__(self, job: Job, steps: Iterable[StepSpec], ctx: Ctx, out: Out = print):
        self.job = job
        self.graph = Graph(steps)
        self.ctx = ctx
        self.status = Status(job)
        self.out = out
        self.max_failures = int((ctx.config.get("loop_guard") or {}).get("max_failures", 3))
        self.internal_error = False
        self.consent_refused = False
        self._first_failure_kind: str | None = None

    # ---- whole run -----------------------------------------------------------------------
    def run_all(self) -> int:
        self._first_failure_kind = None
        failed_this_run: set[Instance] = set()
        any_failed = False
        while True:
            items = self.job.items()
            satisfied: set[Instance] = set()
            replan = False
            for inst in self.graph.instances(items):
                if inst in failed_this_run:
                    continue
                if any(d not in satisfied for d in self.graph.deps_of(inst, items)):
                    continue
                if self.status.is_abandoned(inst.step, inst.item):
                    any_failed = True
                    continue
                spec = self.graph.spec(inst.step)
                ctx = self.ctx.for_item(inst.item)
                try:
                    fr = check_fresh(spec, ctx)
                except VPError as e:
                    self._failure(inst, ctx, e)
                    failed_this_run.add(inst)
                    any_failed = True
                    continue
                if fr.fresh:
                    if self._verify_fresh(inst, spec, ctx, fr, revalidate_stale=True):
                        satisfied.add(inst)
                    else:
                        failed_this_run.add(inst)
                        any_failed = True
                    continue
                outcome = self._run_instance(inst, spec, ctx, fr, items)
                if outcome in ("done", "skipped"):
                    satisfied.add(inst)
                    if self.job.items() != items:
                        replan = True
                        break
                elif outcome == _STOP:
                    return self._exit(any_failed)
                else:
                    failed_this_run.add(inst)
                    any_failed = True
            if not replan:
                break
        return self._exit(any_failed)

    def _exit(self, any_failed: bool) -> int:
        if self.consent_refused:
            return EXIT_CONSENT_REQUIRED
        if self.internal_error:
            return _failed_exit(self._first_failure_kind or "internal", EXIT_INTERNAL)
        return _failed_exit(self._first_failure_kind or "refused") if any_failed else EXIT_OK

    # ---- one stage ------------------------------------------------------------------------
    def run_one(self, step: str, item: str | None, force: bool = False) -> int:
        self._first_failure_kind = None
        if step not in self.graph.specs:
            self.out(f"unknown step {step}")
            return _failed_exit("usage")
        spec = self.graph.spec(step)
        items = self.job.items()
        if spec.scope == "item":
            if not item:
                self.out(f"{step} is item-scoped: --item is required")
                return _failed_exit("usage")
            if items is None or item not in items:
                self.out(f"unknown item {item!r} (items: {items})")
                return _failed_exit("usage")
        elif item:
            self.out(f"{step} is job-scoped: --item not allowed")
            return _failed_exit("usage")
        inst = Instance(step, item if spec.scope == "item" else None)
        if self.status.is_abandoned(inst.step, inst.item):
            self.out(f"{inst.key} is abandoned; run `stage ... --reset` first")
            return _failed_exit("refused")
        if "S5" in self.graph.step_ancestors(step):
            for reel in ([item] if item else items or []):
                if _caption_failure(self.job, reel):
                    self.out(f"dependency failed: S5.{reel}")
                    return _failed_exit("refused")
        for d in self.graph.deps_of(inst, items):
            dep = self.graph.spec(d.step)
            if not check_fresh(dep, self.ctx.for_item(d.item)).fresh:
                self.out(f"dependency not fresh: {d.key}")
                return _failed_exit("refused")
            if type(dep).on_fresh is not StepSpec.on_fresh:
                last = self.status.last(d.step, d.item) or {}
                if last.get("state") in ("failed", "abandoned"):
                    self.out(f"dependency failed: {d.key}")
                    return _failed_exit("refused")
        ctx = self.ctx.for_item(inst.item)
        fr = check_fresh(spec, ctx)
        if fr.fresh and not force:
            if not self._verify_fresh(inst, spec, ctx, fr):
                return self._exit(any_failed=True)
            self.out(f"{inst.key}: fresh, nothing to do (use --force)")
            return EXIT_OK
        outcome = self._run_instance(inst, spec, ctx, fr, items)
        if self.consent_refused:
            return EXIT_CONSENT_REQUIRED
        if outcome in ("done", "skipped", _STOP):
            return EXIT_OK
        return self._exit(any_failed=True)

    def check_reel_captions(self, item: str | None) -> int:
        """Run only the S5 hook, on renders that can be checked without rebuilding."""
        from ..steps.base import item_kind

        self._first_failure_kind = None
        items = self.job.items() or []
        if item and item not in items:
            self.out(f"Unknown reel {item!r}.")
            return _failed_exit("usage")
        reels = [name for name in ([item] if item else items)
                 if item_kind(self.ctx.for_item(name)) == "reel"]
        if not reels:
            self.out(f"Clip {item} has no burned-in captions to check." if item
                     else "There are no reels to check.")
            return EXIT_OK

        plan_rows = {row["item"]: row for row in
                     plan(self.job, steps=self.graph.specs.values(), config=self.ctx.config,
                          through="S5")["steps"]
                     if row["stage"] == "S5"}
        spec = self.graph.spec("S5")
        any_failed = False
        for reel in reels:
            inst = Instance("S5", reel)
            row = plan_rows[reel]
            if self.status.is_abandoned(inst.step, inst.item):
                self.out(f"{inst.key} is abandoned; run `stage ... --reset` first")
                any_failed = True
                continue
            if row["blocked"] or row["blocked_by_gate"] or row["execute"] is None:
                self.out(f"Reel {reel} has earlier work pending; its render cannot be verified for checking yet.")
                any_failed = True
                continue
            ctx = self.ctx.for_item(reel)
            fr = check_fresh(spec, ctx)
            if not fr.fresh:
                self.out(f"Reel {reel} needs a rebuild before its captions can be checked.")
                any_failed = True
            elif not self._verify_fresh(inst, spec, ctx, fr, report_result=True):
                any_failed = True
        return self._exit(any_failed)

    # ---- internals ------------------------------------------------------------------------
    def _verify_fresh(self, inst: Instance, spec: StepSpec, ctx: Ctx, fr: Freshness, *,
                      revalidate_stale: bool = False, report_result: bool = False) -> bool:
        result = self._on_fresh(inst, spec, ctx, fr.manifest.get("outputs") or {}, report_result=report_result)
        if result == "failed":
            return False
        last = self.status.last(inst.step, inst.item) or {}
        if result == "unavailable":
            # A bookkeeping failure cannot satisfy an item that still needs revalidation.
            return not report_result and last.get("state") not in ("failed", "stale")
        if last.get("state") == "failed" or (revalidate_stale and last.get("state") == "stale"):
            self.status.append(inst.step, inst.item, "done", metadata={"revalidated": True})
        return True

    def _on_fresh(self, inst: Instance, spec: StepSpec, ctx: Ctx, outputs: dict[str, str], *,
                  report_result: bool = False) -> str | None:
        try:
            verified = MappingProxyType(outputs)
            message = (spec.on_fresh(ctx, verified, report_result=True) if report_result
                       else spec.on_fresh(ctx, verified))
        except FreshCheckFailed as e:
            return self._failure(inst, ctx, e, loop_guard=False)
        except Exception as e:
            self.out(f"{inst.key} check not_run: {e}")
            return "unavailable"
        if message:
            self.out(message)
        return None

    def _last_meaningful(self, inst: Instance) -> dict[str, Any] | None:
        for e in reversed(self.status.entries(inst.step, inst.item)):
            if e["state"] not in ("running", "stale"):
                return e
        return None

    def _mark_stale(self, inst: Instance, had_manifest: bool, reason: str,
                    items: list[str] | None) -> None:
        targets = [inst] if had_manifest else []
        targets += [d for d in self.graph.descendants(inst, items)
                    if read_manifest(self.job, d.step, d.item) is not None]
        for t in targets:
            last = self.status.last(t.step, t.item)
            if last and last["state"] == "stale" and not (last.get("metadata") or {}).get("reset"):
                continue
            self.status.append(t.step, t.item, "stale",
                               metadata={"cause": inst.key, "reason": reason})

    def _run_instance(self, inst: Instance, spec: StepSpec, ctx: Ctx, fr: Any,
                      items: list[str] | None) -> str:
        same = [i for i in self.graph.instances(items) if i.step == inst.step]
        done = same.index(inst) if inst in same else 0
        with progress.tracking(self.job.root, inst.step, inst.item, done=done,
                               total=max(len(same), 1)):
            return self._run_recorded(inst, spec, ctx, fr, items)

    def _run_recorded(self, inst: Instance, spec: StepSpec, ctx: Ctx, fr: Any,
                      items: list[str] | None) -> str:
        """Telemetry of one step instance: its start, then how it ended and how long it took."""
        from .. import runsignal, telemetry
        # allowlisted fields only (vp.telemetry.sanitize_event): no job, folder, item or free text;
        # `job` = the route, the scenario letter and the job's random run number (vp.runsignal)
        job = runsignal.fields(self.job.root)
        telemetry.record("step", event="start", step=inst.step, **job)
        t0 = time.monotonic()
        outcome = "crashed"
        try:
            outcome = self._run_tracked(inst, spec, ctx, fr, items)
            return outcome
        finally:
            last = self.status.last(inst.step, inst.item) or {}
            meta = last.get("metadata") or {}
            telemetry.record("step", event="end", step=inst.step,
                             outcome=outcome, state=last.get("state"),
                             duration_s=round(time.monotonic() - t0, 3),
                             error_kind=meta.get("kind") if last.get("state") in
                             ("failed", "abandoned") else None,
                             gate=meta.get("gate"), **job)
            runsignal.after_recording_step(self.job, inst.step, outcome)

    def _run_tracked(self, inst: Instance, spec: StepSpec, ctx: Ctx, fr: Any,
                     items: list[str] | None) -> str:
        inputs = fr.inputs
        if inst.item is None and inst.step in adoption.STAGES:
            inputs = {**inputs, **hash_files(self.job, adoption.existing_inputs(self.job))}
        self._mark_stale(inst, fr.manifest is not None, fr.reason, items)
        prev = self._last_meaningful(inst)
        if not (prev and prev["state"] in _WAIT_STATES):
            self.status.append(inst.step, inst.item, "running", metadata={"reason": fr.reason})
        started = now_iso()
        t0 = time.monotonic()
        ctx.log.event("step_start", step=inst.step, item=inst.item, reason=fr.reason)
        try:
            meta = spec.run(ctx) or {}
            if not isinstance(meta, dict):
                raise VPError("internal", f"{spec.id}.run() must return dict or None")
            outputs = hash_outputs(spec, ctx, require_all=True)
        except Skip as s:
            outputs = hash_outputs(spec, ctx, require_all=False)
            write_manifest(self.job, spec.id, inst.item, spec.version, inputs, fr.config,
                           outputs, started)
            self.status.append(inst.step, inst.item, "skipped", metadata={"reason": s.reason})
            ctx.log.event("step_skipped", step=inst.step, item=inst.item)
            return "skipped"
        except WaitingHuman as w:
            if not (prev and prev["state"] == "waiting_human"
                    and (prev.get("metadata") or {}).get("gate") == w.gate):
                self.status.append(inst.step, inst.item, "waiting_human",
                                   metadata={"gate": w.gate, "what": w.what, "kind": w.kind,
                                             "opened_at": now_iso()})
            self.out(f"WAITING_HUMAN {inst.key}: {w.what}")
            ctx.log.event("waiting_human", step=inst.step, item=inst.item, gate=w.gate)
            return _STOP
        except ConsentRequired as c:
            if not (prev and prev["state"] == "waiting_human"
                    and (prev.get("metadata") or {}).get("gate") == "consent"):
                self.status.append(inst.step, inst.item, "waiting_human",
                                   metadata={"gate": "consent", "what": c.msg,
                                             "opened_at": now_iso()})
            self.consent_refused = True
            self.out(f"REFUSED consent-required {inst.key}: {c.msg}")
            ctx.log.event("consent_required", step=inst.step, item=inst.item)
            return _STOP
        except JudgmentPending as j:
            if not (prev and prev["state"] == "waiting_judgment"
                    and (prev.get("metadata") or {}).get("task_path") == j.task_path):
                self.status.append(inst.step, inst.item, "waiting_judgment",
                                   metadata={"task_path": j.task_path, "opened_at": now_iso()})
            self.out(f"WAITING_JUDGMENT {inst.key}: {j.task_path}")
            ctx.log.event("waiting_judgment", step=inst.step, item=inst.item)
            return _STOP
        except WaitingQuota as wq:
            same = (prev and prev["state"] == "inconclusive"
                    and (prev.get("metadata") or {}).get("message") == wq.msg)
            if not same:
                self.status.append(inst.step, inst.item, "inconclusive",
                                   metadata={"message": wq.msg, "reason": "youtube_quota"})
                self._notify(ctx, f"vp: {self.job.job_id} {inst.key} waiting for YouTube quota: {wq.msg}")
            self.out(f"WAITING_QUOTA {inst.key}: {wq.msg}")
            return _STOP
        except Inconclusive as ic:
            same = (prev and prev["state"] == "inconclusive"
                    and (prev.get("metadata") or {}).get("message") == ic.msg)
            if not same:
                self.status.append(inst.step, inst.item, "inconclusive",
                                   metadata={"message": ic.msg, "report": ic.report})
                self._notify(ctx, f"vp: {self.job.job_id} {inst.key} inconclusive: {ic.msg}")
            self.out(f"INCONCLUSIVE {inst.key}: {ic.msg}")
            return _STOP
        except VPError as e:
            return self._failure(inst, ctx, e)
        except Exception as e:  # a bug in a step: classified as internal
            self.internal_error = True
            return self._failure(inst, ctx, VPError("internal", f"{type(e).__name__}: {e}"))

        meta = dict(meta)
        human = self._human_time(inst, meta)
        if human:
            meta["human_time"] = human
        meta["duration_s"] = round(time.monotonic() - t0, 3)
        write_manifest(self.job, spec.id, inst.item, spec.version, inputs, fr.config,
                       outputs, started)
        hook_failure = self._on_fresh(inst, spec, ctx, outputs)
        if hook_failure == "failed":
            return hook_failure
        self.status.append(inst.step, inst.item, "done", metadata=scrub(meta))
        ctx.log.event("step_done", step=inst.step, item=inst.item, duration_s=meta["duration_s"])
        return "done"

    def _human_time(self, inst: Instance, meta: dict[str, Any]) -> dict[str, Any] | None:
        reported = meta.pop("human_time_reported", None)
        closed_proxy = meta.pop("gate_closed_at", None)
        wait = self.status.open_wait(inst.step, inst.item)
        if wait is None:
            return None
        wm = wait.get("metadata") or {}
        kind = wm.get("kind", "review")
        if isinstance(reported, dict) and reported.get("opened_at") and reported.get("closed_at"):
            opened, closed, source = str(reported["opened_at"]), str(reported["closed_at"]), "reported"
            kind = reported.get("kind", kind)
        else:
            opened = str(wm.get("opened_at") or wait["at"])
            closed = str(closed_proxy or now_iso())
            source = "proxy"
        minutes = round((parse_iso(closed) - parse_iso(opened)).total_seconds() / 60.0, 2)
        return {"kind": kind, "opened_at": opened, "closed_at": closed, "minutes": minutes,
                "source": source}

    def _failure(self, inst: Instance, ctx: Ctx, e: VPError, *, loop_guard: bool = True) -> str:
        # The first current failure wins in run order, including a loop-guard transition
        # whose final status entry does not carry the underlying failure kind.
        if self._first_failure_kind is None:
            self._first_failure_kind = e.kind
        meta = {"kind": e.kind, "error": str(e.msg)[:500]}
        if e.details:
            meta["details"] = scrub(e.details)
        ctx.log.event("step_failed", step=inst.step, item=inst.item, kind=e.kind)
        if e.kind == "internal":
            self.internal_error = True
        if e.kind == "auth":
            self.status.append(inst.step, inst.item, "abandoned", metadata={**meta, "reason": "auth"})
            self._notify(ctx, f"vp: {self.job.job_id} {inst.key} abandoned (auth): {e.msg}"[:900])
            self.out(f"ABANDONED {inst.key}: {e}")
            return "abandoned"
        self.status.append(inst.step, inst.item, "failed", metadata=meta)
        n = self.status.failures(inst.step, inst.item)
        if loop_guard and n >= self.max_failures:
            self.status.append(inst.step, inst.item, "abandoned",
                               metadata={"reason": "loop_guard", "failures": n})
            self._notify(ctx, f"vp: {self.job.job_id} {inst.key} abandoned after {n} failures: "
                              f"{e.msg}"[:900])
            self.out(f"ABANDONED {inst.key} after {n} failures: {e}")
            return "abandoned"
        self.out(f"FAILED {inst.key}: {e}")
        return "failed"

    def _notify(self, ctx: Ctx, text: str) -> None:
        try:
            ctx.notifier.send(text)
        except Exception as ne:  # notification must never break the runner
            ctx.log.event("notify_failed", error=f"{type(ne).__name__}")


# ---- public helpers used by the CLI ---------------------------------------------------------

def _load_default_steps() -> list[StepSpec]:
    from ..steps import load_steps
    return load_steps()


def _guard_runner_inputs(job: Job, config: dict[str, Any] | None, out: Out) -> bool:
    """Check runner-owned files before creating the lock or starting progress."""
    from .. import runsignal, transcribe
    from .log import Logger

    home = vp_home()
    log_home = home if config is None else config.get("vp_home") or Path.home() / ".ai_video_pipeline"
    try:
        transcribe.guard_inputs(job, job.lock_file, job.status_yaml, adoption.path_for(job),
                                runsignal.state_path(job.root),
                                job.root / progress.FILE_NAME, home / progress.LIVE_NAME,
                                config_path(home), Logger(log_home).path(),
                                *job.stages_dir.glob("*.yaml"), manifest_dir=job.stages_dir)
    except VPError as e:
        out(f"REFUSED {e.msg}")
        return False
    return True


def run(job: Job, *, steps: Iterable[StepSpec] | None = None, config: dict[str, Any] | None = None,
        services: dict[str, Any] | None = None, factories: dict[str, Callable] | None = None,
        out: Out = print, sleep: Callable[[float], None] | None = None,
        whole_recording: str | None = None) -> int:
    """`python -m vp run <job> [--whole-recording [full|reel]]`.

    Returns the exit code (0 done/gate/locked, 2 failed, 3 internal,
    5 consent-required)."""
    if not _guard_runner_inputs(job, config, out):
        return _failed_exit("refused")
    with job_lock(job) as got:
        if not got:
            out("LOCKED")
            return EXIT_OK
        out(f"job folder: {job.root}")
        try:
            adoption.adopt(job)
            cfg = _recording_route(job, prepare_config(job, config), whole_recording)
            specs = list(steps) if steps is not None else _load_default_steps()
        except VPError as e:
            out(f"ERROR {e}")
            return _failed_exit(e.kind, EXIT_INTERNAL if e.kind == "internal" else EXIT_FAILED)
        ctx = make_ctx(job, cfg, services, factories, sleep)
        return Runner(job, specs, ctx, out).run_all()


def run_stage(job: Job, stage: str, item: str | None = None, *, force: bool = False,
              reset: bool = False, check_only: bool = False, steps: Iterable[StepSpec] | None = None,
              config: dict[str, Any] | None = None, services: dict[str, Any] | None = None,
              factories: dict[str, Callable] | None = None, out: Out = print,
              sleep: Callable[[float], None] | None = None) -> int:
    """`python -m vp stage <job> <id> [--item X] [--force | --reset | --check-only]`.

    --reset only records a manual reset (clears the loop guard / abandoned) and exits.
    --check-only runs the caption check for one or all fresh S5 reels without rendering.
    """
    if check_only and (stage != "S5" or force or reset):
        out("--check-only is only for S5 and cannot be used with --force or --reset")
        return _failed_exit("usage")
    if not _guard_runner_inputs(job, config, out):
        return _failed_exit("refused")
    with job_lock(job) as got:
        if not got:
            out("LOCKED")
            return EXIT_OK
        try:
            adoption.adopt(job)
            if reset:
                Status(job).reset(stage, item)
                out(f"reset {stage}{'.' + item if item else ''}")
                return EXIT_OK
            cfg = _recording_route(job, prepare_config(job, config))
            specs = list(steps) if steps is not None else _load_default_steps()
        except VPError as e:
            out(f"ERROR {e}")
            return _failed_exit(e.kind, EXIT_INTERNAL if e.kind == "internal" else EXIT_FAILED)
        ctx = make_ctx(job, cfg, services, factories, sleep)
        runner = Runner(job, specs, ctx, out)
        if check_only:
            return runner.check_reel_captions(item)
        return runner.run_one(stage, item, force=force)


def unlock(job: Job, out: Out = print) -> int:
    """`python -m vp unlock <job>`: remove a stale job.lock."""
    h = holder(job)
    if _unlock(job):
        out(f"unlocked (was held by pid {h.get('pid') if h else '?'} on {h.get('host') if h else '?'})")
    else:
        out("not locked")
    return EXIT_OK


def status_report(job: Job) -> str:
    """Human-readable summary of status.yaml: current state + last state of each instance."""
    st = Status(job)
    data = st.load()
    failures = caption_failures(job)
    if failures:
        data["current_stage"] = f"S5.{next(iter(failures))}"
        data["current_state"] = "failed"
    lines = [f"job: {job.job_id}", f"job folder: {job.root}",
             f"current: {data.get('current_stage')} {data.get('current_state')}"]
    h = holder(job)
    if h is not None:
        lines.append(f"locked by pid {h.get('pid')} on {h.get('host')} since {h.get('at')}")
    last: dict[tuple, dict] = {}
    for e in data["history"]:
        last[(e.get("stage"), e.get("item"))] = e
    for (stage, item), e in last.items():
        key = stage if item is None else f"{stage}.{item}"
        state = "failed" if stage == "S5" and item in failures else e["state"]
        lines.append(f"  {key:<20} {state:<17} {e.get('at')}")
    for item in failures:
        if ("S5", item) not in last:
            lines.append(f"  {f'S5.{item}':<20} failed")
    return "\n".join(lines)
