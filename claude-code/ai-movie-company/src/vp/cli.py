"""Command line: python -m vp <command> ... (BUILD §4).

  run <job>                                  run the pipeline until done / gate / failure
  status <job>                               print status summary
  stage <job> <id> [--item X] [--force] [--reset]
  unlock <job>                               remove a stale job.lock
  judge-pending <job>                        list judge tasks waiting for an output file (manual judge)
  resolve-upload <job> <item>                S8 uncertain upload -> vp.steps.s8_upload:resolve_upload(ctx, item)
  approve-ingest <job>                       Telegram answers -> approval.yaml: vp.steps.s9_publish:approve_ingest(ctx)
  learn ...                                  S3c: vp.knowledge.learn:main(argv)
  knowledge ...                              vp.knowledge.store:main(argv)
  review show|preview|set|add|close ...      S3b gate in the dialog: vp.gates.review:main(argv)
  approve show|set ...                       S9 gate in the dialog: vp.gates.approve:main(argv)
  doctor [--json]                            machine/config readiness: vp.doctor:main(argv)
  youtube authorize|convert-token|status     OAuth setup / connection check: vp.adapters.youtube:main(argv)
  results <job> [--json]                     what came out (links, files, states): vp.watch:results_main
  progress <job> [--json] [--wait N]         current long step, one line: vp.watch:progress_main
  capcut export <job> [item ...] | check     new CapCut desktop project per clip: vp.adapters.capcut:main
  capcut <job> [--version vN]                new VERSION as a CapCut project (text job: separate
                                             voice/music tracks); <job>/capcut/vN, never overwritten
  projects list|add|scan|forget [--json]     jobs the person worked on: vp.projects:main
  text new|status|estimate|timeline|approve  text-only jobs: vp.text:main
  check picture <video> [--json]             black/frozen picture + frame sheet: vp.check:main
  journal note|show                          dialog journal: vp.journal:main
  feedback pack [--reveal]                   journal + notes + doctor + versions -> one file in
                                             ~/Movies/AI Movie Company/Feedback
  resolve export <job> [item ...]            FCPXML rough cut + .srt (DaVinci Resolve, Final Cut Pro):
                                             vp.adapters.fcpxml:main (alias: fcpxml)
  asks add|set|list <job|-> ...              ledger of the person's requests: vp.asks:main

Global option: --home <dir> (anywhere on the line, also after `learn` / `knowledge`) sets VP_HOME
for the command — the pipeline home with config.yaml, knowledge store, journals.

Exit codes: 0 done/gate/locked, 2 failed/abandoned, 3 internal / not implemented.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from .core import runner
from .core.errors import VPError
from .core.lock import job_lock
from .core.paths import Job

EXIT_NOT_IMPLEMENTED = 3

# commands with their own argument parser: everything after the name goes to it untouched
SUBCOMMAND_MAINS = {
    "learn": "vp.knowledge.learn:main",
    "knowledge": "vp.knowledge.store:main",
    "review": "vp.gates.review:main",
    "approve": "vp.gates.approve:main",
    "doctor": "vp.doctor:main",
    "youtube": "vp.adapters.youtube:main",
    "results": "vp.watch:results_main",
    "progress": "vp.watch:progress_main",
    "capcut": "vp.adapters.capcut:main",
    "resolve": "vp.adapters.fcpxml:main",
    "fcpxml": "vp.adapters.fcpxml:main",
    "asks": "vp.asks:main",
    "design": "vp.design:main",
    "extras": "vp.extras:main",
    "projects": "vp.projects:main",
    "text": "vp.text:main",
    "check": "vp.check:main",
    "journal": "vp.journal:main",
    "feedback": "vp.journal:feedback_main",
}


def _lookup(path: str) -> Callable[..., Any] | None:
    """Resolve 'module:function'; None if the module or function does not exist (yet)."""
    mod, _, attr = path.partition(":")
    try:
        module = importlib.import_module(mod)
    except ModuleNotFoundError as e:
        if e.name and mod.startswith(e.name):
            return None
        raise
    fn = getattr(module, attr, None)
    return fn if callable(fn) else None


def _not_implemented(what: str) -> int:
    print(f"{what}: not implemented yet")
    return EXIT_NOT_IMPLEMENTED


def _with_ctx(job: Job, fn: Callable[..., int], *args: Any) -> int:
    """Run a job-level command under the job lock with a full context."""
    with job_lock(job) as got:
        if not got:
            print("LOCKED")
            return 0
        try:
            cfg = runner.prepare_config(job, None)
            ctx = runner.make_ctx(job, cfg)
            rc = fn(ctx, *args)
        except VPError as e:
            print(f"ERROR {e}")
            return 3 if e.kind == "internal" else 2
        return int(rc or 0)


def extract_home(argv: list[str]) -> tuple[list[str], str | None]:
    """Remove every `--home X` / `--home=X` from argv; returns (rest, last home or None)."""
    rest: list[str] = []
    home: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--home":
            if i + 1 >= len(argv):
                raise SystemExit("vp: --home needs a directory")
            home, i = argv[i + 1], i + 2
            continue
        if a.startswith("--home="):
            home = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1
    return rest, home


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vp", description="video pipeline")
    p.add_argument("--home", help="pipeline home (sets VP_HOME; default ~/.ai_video_pipeline)")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "status", "unlock", "approve-ingest", "judge-pending"):
        s = sub.add_parser(name)
        s.add_argument("job")
    s = sub.add_parser("stage")
    s.add_argument("job")
    s.add_argument("step")
    s.add_argument("--item")
    s.add_argument("--force", action="store_true")
    s.add_argument("--reset", action="store_true")
    s = sub.add_parser("resolve-upload")
    s.add_argument("job")
    s.add_argument("item")
    for name in ("learn", "knowledge", "review", "approve", "doctor", "youtube", "results",
                 "progress", "capcut", "resolve", "fcpxml", "asks", "design", "projects", "text",
                 "check", "journal", "feedback"):
        s = sub.add_parser(name, add_help=False)
        s.add_argument("rest", nargs=argparse.REMAINDER)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    raw, home = extract_home(list(argv) if argv is not None else sys.argv[1:])
    if home:
        os.environ["VP_HOME"] = str(Path(home).expanduser().resolve())
    # learn / knowledge: everything after the subcommand goes to their own parser untouched
    # (argparse REMAINDER cannot start with an option such as `--force`)
    if raw and raw[0] in SUBCOMMAND_MAINS:
        target = SUBCOMMAND_MAINS[raw[0]]
        fn = _lookup(target)
        try:
            return int(fn(raw[1:]) or 0) if fn else _not_implemented(raw[0])
        except VPError as e:
            print(f"ERROR {e}")
            return 3 if e.kind == "internal" else 2
    args = build_parser().parse_args(raw)
    cmd = args.command
    if cmd in ("run", "stage"):
        from .projects import safe_touch
        safe_touch(args.job)          # K3: every job worked on is listed by `vp projects list`
    try:
        if cmd == "run":
            return runner.run(Job(args.job))
        if cmd == "status":
            print(runner.status_report(Job(args.job)))
            return 0
        if cmd == "stage":
            return runner.run_stage(Job(args.job), args.step, args.item, force=args.force,
                                    reset=args.reset)
        if cmd == "unlock":
            return runner.unlock(Job(args.job))
        if cmd == "judge-pending":
            from .core.judge import pending_tasks
            tasks = pending_tasks(Job(args.job))
            for t in tasks:
                print(f"{t['task']}\t{t['output']}")
            if not tasks:
                print("no pending judge tasks")
            return 0
        if cmd == "resolve-upload":
            fn = _lookup("vp.steps.s8_upload:resolve_upload")
            return _with_ctx(Job(args.job), fn, args.item) if fn else _not_implemented(cmd)
        if cmd == "approve-ingest":
            fn = _lookup("vp.steps.s9_publish:approve_ingest")
            return _with_ctx(Job(args.job), fn) if fn else _not_implemented(cmd)
        if cmd == "learn":
            fn = _lookup("vp.knowledge.learn:main")
            return int(fn(args.rest) or 0) if fn else _not_implemented(cmd)
        if cmd == "knowledge":
            fn = _lookup("vp.knowledge.store:main")
            return int(fn(args.rest) or 0) if fn else _not_implemented(cmd)
    except VPError as e:
        print(f"ERROR {e}")
        return 3 if e.kind == "internal" else 2
    return 3  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
