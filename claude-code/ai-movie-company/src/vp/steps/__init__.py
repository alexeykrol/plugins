"""Step registry (single writer: core). Steps are imported lazily.

Each module defines `STEP` — an instance of a `vp.steps.base.StepSpec` subclass whose id, deps,
scope and gate must match GRAPH below (BUILD §3); load_steps() checks this.
"""
from __future__ import annotations

import importlib

from ..core.errors import VPError
from ..core.graph import StepSpec

REGISTRY: list[str] = [
    "vp.steps.s0_intake:STEP",
    "vp.steps.s1_proofread:STEP",
    "vp.steps.s2_retime:STEP",
    "vp.steps.s3a_select:STEP",
    "vp.steps.s3b_review:STEP",
    "vp.steps.s4_cut:STEP",
    "vp.steps.s6a_design_texts:STEP",
    "vp.steps.s5_render:STEP",
    "vp.steps.s5v_visual:STEP",
    "vp.steps.s7_thumbnail:STEP",
    "vp.steps.s6b_metadata:STEP",
    "vp.steps.s8_upload:STEP",
    "vp.steps.s10_crosslinks:STEP",
    "vp.steps.s9_package:STEP",
    "vp.steps.s9_publish:STEP",
    "vp.steps.s11_report:STEP",
]

# id -> (deps, scope, gate): the graph of BUILD §3 / SPEC §5, as data.
GRAPH: dict[str, tuple[tuple[str, ...], str, str | None]] = {
    "S0": ((), "job", None),
    "S1": (("S0",), "job", None),
    "S2": (("S1",), "job", None),
    "S3a": (("S2",), "job", None),
    "S3b": (("S3a",), "job", "human"),
    "S4": (("S3b",), "item", None),
    "S6a": (("S4",), "item", None),
    "S5": (("S6a",), "item", None),
    "S5v": (("S5",), "item", None),
    "S7": (("S6a",), "item", None),
    "S6b": (("S5v",), "item", None),
    "S8": (("S6b", "S7"), "item", None),
    "S10": (("S8",), "job", None),
    "S9": (("S10",), "job", "human"),
    "PUB": (("S9",), "job", None),
    "S11": (("PUB",), "job", None),
}


def load_step(path: str) -> StepSpec:
    """Import 'module:ATTR' and return the StepSpec. Missing module -> VPError(internal)."""
    mod, _, attr = path.partition(":")
    try:
        module = importlib.import_module(mod)
    except ModuleNotFoundError as e:
        if e.name and mod.startswith(e.name):
            raise VPError("internal", f"step module {mod} is not implemented yet") from e
        raise
    spec = getattr(module, attr, None)
    if not isinstance(spec, StepSpec):
        raise VPError("internal", f"{path} is not a StepSpec")
    return spec


def check_spec(spec: StepSpec) -> None:
    """Raise VPError(internal) if the spec disagrees with GRAPH."""
    if spec.id not in GRAPH:
        raise VPError("internal", f"unknown step id {spec.id}")
    deps, scope, gate = GRAPH[spec.id]
    if tuple(sorted(spec.deps)) != tuple(sorted(deps)) or spec.scope != scope or spec.gate != gate:
        raise VPError("internal", f"step {spec.id} disagrees with GRAPH: deps={list(spec.deps)} "
                                  f"scope={spec.scope} gate={spec.gate}; expected {deps} {scope} {gate}")


def load_steps(paths: list[str] | None = None) -> list[StepSpec]:
    """Load all registered steps (in REGISTRY order) and validate them against GRAPH."""
    specs = [load_step(p) for p in (paths or REGISTRY)]
    for s in specs:
        check_spec(s)
    return specs
