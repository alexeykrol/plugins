"""The runner: walks the graph, runs non-fresh instances, records status (BUILD §3–4).

Outcomes of one instance and what the runner does:
  done            manifest written last, status done (+ human_time if a gate was open)
  skipped         (step raised Skip) manifest with existing outputs, status skipped; counts as satisfied
  waiting_human   (WaitingHuman) status waiting_human with opened_at; the whole run stops, exit 0
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
from typing import Any, Callable, Iterable

from .config import load_config, resolve_job_config
from .ctx import Ctx
from . import progress
from .errors import Inconclusive, JudgmentPending, Skip, VPError, WaitingHuman, WaitingQuota
from .graph import Graph, Instance, StepSpec
from .io import now_iso, parse_iso
from .lock import holder, job_lock
from .lock import unlock as _unlock
from .log import scrub
from .manifest import check_fresh, hash_outputs, read_manifest, write_manifest
from .paths import Job
from .status import Status

EXIT_OK, EXIT_FAILED, EXIT_INTERNAL = 0, 2, 3
_WAIT_STATES = ("waiting_human", "waiting_judgment", "inconclusive")
_STOP = "stop"

Out = Callable[[str], None]


def prepare_config(job: Job, config: dict[str, Any] | None) -> dict[str, Any]:
    """Load <vp_home>/config.yaml if `config` is None, then resolve the job profile."""
    cfg = config if config is not None else load_config()
    if "profile" not in cfg:
        cfg = resolve_job_config(cfg, job.request(), job.design())
    return cfg


def make_ctx(job: Job, config: dict[str, Any], services: dict[str, Any] | None = None,
             factories: dict[str, Callable] | None = None,
             sleep: Callable[[float], None] | None = None) -> Ctx:
    """Base context for a run (item None). `services` = ready objects (judge/publisher/...)."""
    ctx = Ctx(job, None, config, factories=factories, **(services or {}))
    if sleep is not None:
        ctx.sleep = sleep
    return ctx


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

    # ---- whole run -----------------------------------------------------------------------
    def run_all(self) -> int:
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
                    last = self.status.last(inst.step, inst.item)
                    if last and last["state"] == "stale":
                        self.status.append(inst.step, inst.item, "done", metadata={"revalidated": True})
                    satisfied.add(inst)
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
        if self.internal_error:
            return EXIT_INTERNAL
        return EXIT_FAILED if any_failed else EXIT_OK

    # ---- one stage ------------------------------------------------------------------------
    def run_one(self, step: str, item: str | None, force: bool = False) -> int:
        if step not in self.graph.specs:
            self.out(f"unknown step {step}")
            return EXIT_FAILED
        spec = self.graph.spec(step)
        items = self.job.items()
        if spec.scope == "item":
            if not item:
                self.out(f"{step} is item-scoped: --item is required")
                return EXIT_FAILED
            if items is None or item not in items:
                self.out(f"unknown item {item!r} (items: {items})")
                return EXIT_FAILED
        elif item:
            self.out(f"{step} is job-scoped: --item not allowed")
            return EXIT_FAILED
        inst = Instance(step, item if spec.scope == "item" else None)
        if self.status.is_abandoned(inst.step, inst.item):
            self.out(f"{inst.key} is abandoned; run `stage ... --reset` first")
            return EXIT_FAILED
        for d in self.graph.deps_of(inst, items):
            if not check_fresh(self.graph.spec(d.step), self.ctx.for_item(d.item)).fresh:
                self.out(f"dependency not fresh: {d.key}")
                return EXIT_FAILED
        ctx = self.ctx.for_item(inst.item)
        fr = check_fresh(spec, ctx)
        if fr.fresh and not force:
            self.out(f"{inst.key}: fresh, nothing to do (use --force)")
            return EXIT_OK
        outcome = self._run_instance(inst, spec, ctx, fr, items)
        if outcome in ("done", "skipped", _STOP):
            return EXIT_OK
        return EXIT_INTERNAL if self.internal_error else EXIT_FAILED

    # ---- internals ------------------------------------------------------------------------
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
            return self._run_tracked(inst, spec, ctx, fr, items)

    def _run_tracked(self, inst: Instance, spec: StepSpec, ctx: Ctx, fr: Any,
                     items: list[str] | None) -> str:
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
            write_manifest(self.job, spec.id, inst.item, spec.version, fr.inputs, fr.config,
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
        write_manifest(self.job, spec.id, inst.item, spec.version, fr.inputs, fr.config,
                       outputs, started)
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

    def _failure(self, inst: Instance, ctx: Ctx, e: VPError) -> str:
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
        if n >= self.max_failures:
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


def run(job: Job, *, steps: Iterable[StepSpec] | None = None, config: dict[str, Any] | None = None,
        services: dict[str, Any] | None = None, factories: dict[str, Callable] | None = None,
        out: Out = print, sleep: Callable[[float], None] | None = None) -> int:
    """`python -m vp run <job>`. Returns the exit code (0 done/gate/locked, 2 failed, 3 internal)."""
    with job_lock(job) as got:
        if not got:
            out("LOCKED")
            return EXIT_OK
        try:
            cfg = prepare_config(job, config)
            specs = list(steps) if steps is not None else _load_default_steps()
        except VPError as e:
            out(f"ERROR {e}")
            return EXIT_INTERNAL if e.kind == "internal" else EXIT_FAILED
        ctx = make_ctx(job, cfg, services, factories, sleep)
        return Runner(job, specs, ctx, out).run_all()


def run_stage(job: Job, stage: str, item: str | None = None, *, force: bool = False,
              reset: bool = False, steps: Iterable[StepSpec] | None = None,
              config: dict[str, Any] | None = None, services: dict[str, Any] | None = None,
              factories: dict[str, Callable] | None = None, out: Out = print,
              sleep: Callable[[float], None] | None = None) -> int:
    """`python -m vp stage <job> <id> [--item X] [--force] [--reset]`.

    --reset only records a manual reset (clears the loop guard / abandoned) and exits.
    """
    with job_lock(job) as got:
        if not got:
            out("LOCKED")
            return EXIT_OK
        if reset:
            Status(job).reset(stage, item)
            out(f"reset {stage}{'.' + item if item else ''}")
            return EXIT_OK
        try:
            cfg = prepare_config(job, config)
            specs = list(steps) if steps is not None else _load_default_steps()
        except VPError as e:
            out(f"ERROR {e}")
            return EXIT_INTERNAL if e.kind == "internal" else EXIT_FAILED
        ctx = make_ctx(job, cfg, services, factories, sleep)
        return Runner(job, specs, ctx, out).run_one(stage, item, force=force)


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
    lines = [f"job: {job.job_id}",
             f"current: {data.get('current_stage')} {data.get('current_state')}"]
    h = holder(job)
    if h is not None:
        lines.append(f"locked by pid {h.get('pid')} on {h.get('host')} since {h.get('at')}")
    last: dict[tuple, dict] = {}
    for e in data["history"]:
        last[(e.get("stage"), e.get("item"))] = e
    for (stage, item), e in last.items():
        key = stage if item is None else f"{stage}.{item}"
        lines.append(f"  {key:<20} {e['state']:<17} {e.get('at')}")
    return "\n".join(lines)
