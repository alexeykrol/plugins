"""Step specs and the dependency graph (BUILD §3).

A step is a `StepSpec` (subclass it, or pass callables as keyword arguments). The graph is data:
each spec lists predecessor step ids. Item-scoped steps are expanded into one instance per item
(fragment ids of fragments.yaml + "full").

Instance dependency rules:
  item step  <- job step   : depends on the single job instance
  item step  <- item step  : depends on the instance with the same item
  job step   <- item step  : depends on ALL instances of that step (all items)
While items are unknown (fragments.yaml absent), item instances do not exist and job steps that
transitively depend on an item step are left out of the plan (they cannot run yet).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .errors import VPError

if TYPE_CHECKING:  # pragma: no cover
    from .ctx import Ctx

_OVERRIDABLE = ("inputs", "config", "outputs", "run")


class StepSpec:
    """Specification of one pipeline step.

    Class attributes (override in a subclass or pass as keyword arguments):
      id       step id, e.g. "S4"
      deps     predecessor step ids
      scope    "job" | "item"
      version  bump when the step logic changes (invalidates manifests)
      gate     None | "human"
    Methods (override, or pass callables `fn(ctx)` as keyword arguments):
      inputs(ctx)  -> list[Path]  files hashed into the manifest (sources, upstream outputs, human files)
      config(ctx)  -> dict        settings hashed into the manifest (profile slice, knowledge version, prompt hash)
      outputs(ctx) -> list[Path]  files the step guarantees to produce (inside the job folder)
      run(ctx)     -> dict | None metadata for status; raises VPError / WaitingHuman / JudgmentPending /
                                  Inconclusive / Skip
    """

    id: str = ""
    deps: tuple[str, ...] | list[str] = ()
    scope: str = "job"
    version: str = "1"
    gate: str | None = None

    def __init__(self, **kw: Any):
        for k, v in kw.items():
            if k in ("id", "deps", "scope", "version", "gate") or k in _OVERRIDABLE:
                setattr(self, k, v)
            else:
                raise TypeError(f"StepSpec: unknown field {k!r}")
        self.deps = tuple(self.deps)
        if self.scope not in ("job", "item"):
            raise ValueError(f"{self.id}: scope must be job|item")
        if self.gate not in (None, "human"):
            raise ValueError(f"{self.id}: gate must be None|'human'")

    def __repr__(self) -> str:
        return f"<StepSpec {self.id} scope={self.scope} deps={list(self.deps)} v{self.version}>"

    def inputs(self, ctx: "Ctx") -> list[Path]:
        return []

    def config(self, ctx: "Ctx") -> dict:
        return {}

    def outputs(self, ctx: "Ctx") -> list[Path]:
        return []

    def run(self, ctx: "Ctx") -> dict | None:  # pragma: no cover - abstract
        raise NotImplementedError(f"step {self.id} has no run()")


@dataclass(frozen=True)
class Instance:
    """One runnable unit: a step, plus the item for item-scoped steps (None for job steps)."""

    step: str
    item: str | None = None

    @property
    def key(self) -> str:
        return self.step if self.item is None else f"{self.step}.{self.item}"

    def __str__(self) -> str:
        return self.key


class Graph:
    """Dependency graph of steps, expandable over items."""

    def __init__(self, specs: Iterable[StepSpec]):
        self.specs: dict[str, StepSpec] = {}
        for s in specs:
            if not s.id or s.id in self.specs:
                raise VPError("internal", f"duplicate or empty step id {s.id!r}")
            self.specs[s.id] = s
        for s in self.specs.values():
            for d in s.deps:
                if d not in self.specs:
                    raise VPError("internal", f"step {s.id} depends on unknown step {d}")
        self.order: list[str] = self._topo()

    def _topo(self) -> list[str]:
        """Kahn's algorithm, stable with respect to registration order."""
        ids = list(self.specs)
        indeg = {i: len(set(self.specs[i].deps)) for i in ids}
        out: list[str] = []
        ready = [i for i in ids if indeg[i] == 0]
        while ready:
            cur = ready.pop(0)
            out.append(cur)
            for i in ids:
                if cur in self.specs[i].deps and i not in out and i not in ready:
                    indeg[i] -= 1
                    if indeg[i] == 0:
                        ready.append(i)
                        ready.sort(key=ids.index)
        if len(out) != len(ids):
            raise VPError("internal", f"dependency cycle among {sorted(set(ids) - set(out))}")
        return out

    def spec(self, step: str) -> StepSpec:
        return self.specs[step]

    def step_ancestors(self, step: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.specs[step].deps)
        while stack:
            d = stack.pop()
            if d not in seen:
                seen.add(d)
                stack.extend(self.specs[d].deps)
        return seen

    def needs_items(self, step: str) -> bool:
        """True if the step is item-scoped or transitively depends on an item-scoped step."""
        if self.specs[step].scope == "item":
            return True
        return any(self.specs[a].scope == "item" for a in self.step_ancestors(step))

    def instances(self, items: list[str] | None) -> list[Instance]:
        """All instances in topological order (step-major, items in given order)."""
        out: list[Instance] = []
        for sid in self.order:
            spec = self.specs[sid]
            if items is None and self.needs_items(sid):
                continue
            if spec.scope == "item":
                out.extend(Instance(sid, it) for it in items or [])
            else:
                out.append(Instance(sid, None))
        return out

    def deps_of(self, inst: Instance, items: list[str] | None) -> list[Instance]:
        """Direct predecessor instances."""
        spec = self.specs[inst.step]
        out: list[Instance] = []
        for d in spec.deps:
            dspec = self.specs[d]
            if dspec.scope == "job":
                out.append(Instance(d, None))
            elif spec.scope == "item":
                out.append(Instance(d, inst.item))
            else:
                out.extend(Instance(d, it) for it in items or [])
        return out

    def descendants(self, inst: Instance, items: list[str] | None) -> list[Instance]:
        """All instances that transitively depend on `inst`, in topological order."""
        all_inst = self.instances(items)
        affected: set[Instance] = {inst}
        for cand in all_inst:  # topo order: predecessors are decided before successors
            if cand == inst:
                continue
            if any(d in affected for d in self.deps_of(cand, items)):
                affected.add(cand)
        return [i for i in all_inst if i in affected and i != inst]


def make_step(id: str, deps: Iterable[str] = (), scope: str = "job", version: str = "1",
              gate: str | None = None, *, inputs: Callable | None = None,
              config: Callable | None = None, outputs: Callable | None = None,
              run: Callable | None = None) -> StepSpec:
    """Convenience constructor for tests and small steps: callables take `ctx`."""
    kw: dict[str, Any] = {"id": id, "deps": tuple(deps), "scope": scope, "version": version, "gate": gate}
    for name, fn in (("inputs", inputs), ("config", config), ("outputs", outputs), ("run", run)):
        if fn is not None:
            kw[name] = fn
    return StepSpec(**kw)
