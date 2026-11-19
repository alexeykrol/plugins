"""Registry of default fake-judge responders (used by FakeJudge and e2e tests).

A responder is `fn(task, prompt, inputs) -> str` returning a VALID output for its task.
Step workers put theirs in `vp/testing/responders_<step>.py` and call `register(prefix, fn)` at
import time, e.g. register("s1.proofread.", respond_proofread). Those modules are imported
automatically on the first lookup. The longest matching task prefix wins.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Callable

from ..core.errors import VPError

Responder = Callable[[str, str, dict[str, str]], str]

_RESPONDERS: dict[str, Responder] = {}
_loaded = False


def register(task_prefix: str, fn: Responder) -> None:
    """Register `fn` for tasks starting with `task_prefix` (later registration replaces)."""
    if not task_prefix:
        raise ValueError("empty task prefix")
    _RESPONDERS[task_prefix] = fn


def unregister(task_prefix: str) -> None:
    _RESPONDERS.pop(task_prefix, None)


def load_all() -> list[str]:
    """Import every vp.testing.responders_* module once; returns their names."""
    global _loaded
    names = []
    from .. import testing as pkg
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("responders_"):
            importlib.import_module(f"{pkg.__name__}.{info.name}")
            names.append(info.name)
    _loaded = True
    return names


def find(task: str) -> Responder | None:
    """Responder with the longest prefix matching `task`."""
    if not _loaded:
        load_all()
    best = None
    for prefix in _RESPONDERS:
        if task.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return _RESPONDERS[best] if best is not None else None


def default_responder(task: str, prompt: str, inputs: dict[str, str]) -> str:
    """Dispatch to the registered responder; no responder -> VPError(internal)."""
    fn = find(task)
    if fn is None:
        raise VPError("internal", f"no fake responder registered for task {task!r}")
    return fn(task, prompt, inputs)
