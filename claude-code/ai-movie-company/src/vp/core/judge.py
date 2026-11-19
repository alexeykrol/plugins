"""Judge — every model judgment goes through here (BUILD §5).

    judge.run(task, prompt, inputs, output_name, ctx) -> str

Writes judge/<task>/{task.yaml, prompt.md, input.<name>...} and judge/<task>/<output_name>,
returns the raw output text. The calling step validates it (invalid -> VPError("contract")).

output_name is the file name inside judge/<task>/, by convention "output.<ext>"
(e.g. "output.txt", "output.yaml", "output.json").

Implementations:
  ClaudeCliJudge  `claude -p --output-format json --model <M>`, full prompt on stdin;
                  output = JSON field `result`; usage -> cost journal
  FakeJudge       responder(task, prompt, inputs) -> str (tests; default = vp.testing.fake_responders)
  ManualJudge     writes the task; returns judge/<task>/<output_name> if it exists, else raises
                  JudgmentPending (status waiting_judgment) — Claude Code in the plugin session judges
Selection: config `judge` (claude_cli|fake|manual) or env VP_JUDGE.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .errors import JudgmentPending, VPError
from .io import atomic_write, now_iso, read_yaml, sha256_bytes, write_yaml

if TYPE_CHECKING:  # pragma: no cover
    from .ctx import Ctx

Responder = Callable[[str, str, dict[str, str]], str]

_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_AUTH_PAT = re.compile(r"(not logged in|invalid api key|authentication|unauthori[sz]ed|"
                       r"please run /login|oauth token|\b401\b)", re.I)


def compose_prompt(prompt: str, inputs: dict[str, str]) -> str:
    """Prompt + inputs as one text (for executors that take a single message)."""
    parts = [prompt.rstrip()]
    for name, text in inputs.items():
        parts.append(f'<input name="{name}">\n{text.rstrip()}\n</input>')
    return "\n\n".join(parts) + "\n"


class Judge:
    """Base class: writes the task folder; subclasses implement `_produce`."""

    kind = "base"

    def task_dir(self, task: str, ctx: "Ctx") -> Path:
        if not _TASK_RE.match(task):
            raise VPError("internal", f"bad judge task name {task!r}")
        return ctx.job.judge_dir(task)

    def write_task(self, task: str, prompt: str, inputs: dict[str, str], output_name: str,
                   ctx: "Ctx") -> Path:
        """Write task.yaml, prompt.md and input.<name> files; returns the task dir.

        If a previous task with a DIFFERENT prompt/inputs left an output, that output is renamed
        to <output_name>.superseded (it answered another question).
        """
        if not _NAME_RE.match(output_name):
            raise VPError("internal", f"bad output name {output_name!r}")
        d = self.task_dir(task, ctx)
        for name in inputs:
            if not _NAME_RE.match(name):
                raise VPError("internal", f"bad input name {name!r}")
        task_hash = sha256_bytes(compose_prompt(prompt, inputs))
        old = read_yaml(d / "task.yaml", default=None)
        out = d / output_name
        if isinstance(old, dict) and old.get("task_hash") != task_hash and out.exists():
            os.replace(out, d / f"{output_name}.superseded")
        atomic_write(d / "prompt.md", prompt)
        for name, text in inputs.items():
            atomic_write(d / f"input.{name}", text)
        write_yaml(d / "task.yaml", {
            "task": task, "judge": self.kind, "output": output_name,
            "inputs": [f"input.{n}" for n in inputs], "task_hash": task_hash,
            "created_at": now_iso(),
            "instructions": f"Read prompt.md and the input.* files; write the answer to {output_name}.",
        })
        return d

    def run(self, task: str, prompt: str, inputs: dict[str, str], output_name: str,
            ctx: "Ctx") -> str:
        d = self.write_task(task, prompt, inputs, output_name, ctx)
        text = self._produce(task, prompt, inputs, output_name, d, ctx)
        atomic_write(d / output_name, text)
        return text

    def _produce(self, task: str, prompt: str, inputs: dict[str, str], output_name: str,
                 d: Path, ctx: "Ctx") -> str:  # pragma: no cover - abstract
        raise NotImplementedError


class FakeJudge(Judge):
    """Deterministic judge for tests: `responder(task, prompt, inputs) -> str`."""

    kind = "fake"

    def __init__(self, responder: Responder | None = None):
        if responder is None:
            from ..testing.fake_responders import default_responder
            responder = default_responder
        self.responder = responder
        self.calls: list[str] = []

    def _produce(self, task, prompt, inputs, output_name, d, ctx) -> str:
        self.calls.append(task)
        text = self.responder(task, prompt, dict(inputs))
        ctx.costs.add("judge", task=task, model="fake")
        return text


class ManualJudge(Judge):
    """Writes the task and waits for a human/agent to write the output file."""

    kind = "manual"

    def run(self, task: str, prompt: str, inputs: dict[str, str], output_name: str,
            ctx: "Ctx") -> str:
        d = self.write_task(task, prompt, inputs, output_name, ctx)
        out = d / output_name
        if out.exists():
            return out.read_text(encoding="utf-8")
        raise JudgmentPending(str(d))


class ClaudeCliJudge(Judge):
    """Runs `claude -p --output-format json --model <model>` with the full prompt on stdin."""

    kind = "claude_cli"

    def __init__(self, model: str = "sonnet", executable: str = "claude", timeout_s: float = 900,
                 env: dict[str, str] | None = None):
        self.model = model
        self.executable = executable
        self.timeout_s = timeout_s
        self.env = env

    def command(self) -> list[str]:
        return [self.executable, "-p", "--output-format", "json", "--model", self.model]

    def _produce(self, task, prompt, inputs, output_name, d, ctx) -> str:
        full = compose_prompt(prompt, inputs)
        try:
            proc = subprocess.run(self.command(), input=full, capture_output=True, text=True,
                                  timeout=self.timeout_s, env=self.env)
        except FileNotFoundError as e:
            raise VPError("internal", f"judge executable not found: {self.executable}") from e
        except subprocess.TimeoutExpired as e:
            raise VPError("transport", f"judge timed out after {self.timeout_s}s on {task}") from e
        tail = (proc.stderr or "")[-300:]
        data: dict[str, Any] | None = None
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else None
        except json.JSONDecodeError:
            data = None
        if proc.returncode != 0 or data is None or data.get("is_error"):
            detail = tail or (str(data.get("result"))[:300] if isinstance(data, dict) else "")
            kind = "auth" if _AUTH_PAT.search(detail) else (
                "contract" if proc.returncode == 0 and data is None else "transport")
            raise VPError(kind, f"judge {task} failed (exit {proc.returncode}): {detail}")
        result = data.get("result")
        if not isinstance(result, str):
            raise VPError("contract", f"judge {task}: no text `result` in CLI output")
        usage = data.get("usage") or {}
        ctx.costs.add("judge", task=task, model=self.model,
                      input_tokens=int(usage.get("input_tokens") or 0),
                      output_tokens=int(usage.get("output_tokens") or 0),
                      cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                      cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                      usd=float(data.get("total_cost_usd") or 0.0))
        ctx.log.event("judge", task=task, model=self.model,
                      input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))
        return result


def make_judge(config: dict[str, Any]) -> Judge:
    """Judge from config `judge` (env VP_JUDGE wins)."""
    kind = os.environ.get("VP_JUDGE") or config.get("judge", "claude_cli")
    if kind == "fake":
        return FakeJudge()
    if kind == "manual":
        return ManualJudge()
    if kind == "claude_cli":
        return ClaudeCliJudge(model=config.get("judge_model", "sonnet"),
                              timeout_s=float(config.get("judge_timeout_s", 900)))
    raise VPError("data", f"unknown judge {kind!r}")


def pending_tasks(job: Any) -> list[dict[str, Any]]:
    """Judge tasks of a job whose output file is not written yet (ManualJudge path).

    Each: {task, dir, output (absolute path), prompt (path), inputs (paths)}. Used by
    `python -m vp judge-pending <job>` and the plugin's run skill."""
    root = job.root / "judge"
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        meta = read_yaml(d / "task.yaml", default=None)
        if not isinstance(meta, dict) or not meta.get("output"):
            continue
        target = d / str(meta["output"])
        if target.exists():
            continue
        out.append({"task": str(meta.get("task") or d.name), "dir": str(d), "output": str(target),
                    "prompt": str(d / "prompt.md"),
                    "inputs": [str(d / n) for n in meta.get("inputs") or []]})
    return out
