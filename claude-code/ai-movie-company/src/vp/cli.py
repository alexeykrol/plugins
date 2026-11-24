"""Command line: python -m vp <command> ... (BUILD §4).

  run <job> [--whole-recording [full|reel]]  run the pipeline until done / gate / failure
  status <job>                               print status summary
  stage <job> <id> [--item X] [--force] [--reset]
  reel <job> [--item ID] [--json]             compile, render and check local reels
  long-plan <job> [--json]                    compile a local long-video timeline
  long-render <job> [--json]                  render and check the current long-video timeline
  unlock <job>                               remove a stale job.lock
  judge-pending <job>                        list judge tasks waiting for an output file (manual judge)
  resolve-upload <job> <item>                S8 uncertain upload -> vp.steps.s8_upload:resolve_upload(ctx, item)
  approve-ingest <job>                       Telegram answers -> approval.yaml: vp.steps.s9_publish:approve_ingest(ctx)
  learn ...                                  S3c: vp.knowledge.learn:main(argv)
  knowledge ...                              vp.knowledge.store:main(argv)
  review show|preview|set|add|close ...      S3b gate in the dialog: vp.gates.review:main(argv)
  approve show|set ...                       S9 gate in the dialog: vp.gates.approve:main(argv)
  doctor [--json]                            machine/config readiness: vp.doctor:main(argv)
  youtube authorize|convert-token|status|disconnect  OAuth setup / check / revoke + delete: vp.adapters.youtube:main(argv)
  results <job> [--json]                     what came out (links, files, states): vp.watch:results_main
  progress <job> [--json] [--wait N]         current long step, one line: vp.watch:progress_main
  capcut export <job> [item ...] | check     new CapCut desktop project per clip: vp.adapters.capcut:main
  capcut <job> [--version vN] [--label T]    new VERSION = new timeline in the job's ONE CapCut
                                             project, a track per layer (capcut/layers.json);
                                             <job>/capcut/vN, never overwritten
  capcut library [--json]                    library items layers.json can name
  capcut diff <job> [--version vN] [--json]  compare a written version with its saved CapCut edit
  capcut diff note <job> --change N --reason CODE [--version vN]
  projects list|summary|add|scan|forget [--json]  jobs the person worked on; `summary` says
                                             whether this is the first project: vp.projects:main
  tutorial list|for|set|offer|check          tutorials at the moment of need (T01–T08: a video
                                             link or a short card for the chat): vp.tutorials:main
  text new|status|estimate|timeline|timing|approve  text-only jobs: vp.text:main
  check picture <video> [--json]             black/frozen picture + frame sheet: vp.check:main
  transcribe estimate|run|import|purge|key ...  recognition and saved answers
                                             (ElevenLabs Scribe): vp.transcribe:main
  captions plan|exact|keep --job <job>       exact caption timing for a recording with subtitles
                                             only (word times by the person's click): vp.captions:main
  journal note|show                          dialog journal: vp.journal:main
  feedback pack [--reveal]                   journal + notes + doctor + versions -> one file in
                                             ~/Movies/AI Movie Company/Feedback
  resolve export <job> [item ...]            FCPXML rough cut + .srt (DaVinci Resolve, Final Cut Pro):
                                             vp.adapters.fcpxml:main (alias: fcpxml)
  asks add|set|list <job|-> ...              ledger of the person's requests: vp.asks:main
  consent status|show|accept|later|withdraw  the plugin's terms: vp.safety:consent_main
  guard status|approve|off|on                the install guard's stops and one-time approvals:
                                             vp.safety:guard_main (enforced by hooks/guard.py)
  mode status|offer|enable|decline|undo      "fewer questions" in Elsa's workspace only:
                                             vp.safety:mode_main
  scenario list|detect|plan|matrix|services|check
                                             scenario tree — route, steps, services, cost:
                                             vp.scenarios:main
  stock search <query> [--kind video|photo] [--orientation ...] [--min-duration S] [--json]
  stock get <id> <job>                       stock (Pexels / Pixabay) -> <job>/library/stock + provenance:
                                             vp.adapters.stock:main
  images estimate --n K | generate <job> --prompt ... --aspect 9:16|16:9|1:1 --n K
                                             image generation (Gemini / OpenAI) -> <job>/library/generated:
                                             vp.adapters.images:main
  keys status|set|check|forget               the person's own API keys (never printed): vp.keys:main
  voice list|estimate|say|scenes ...         voice-over with the person's ElevenLabs key -> <job>/voice
                                             (+ word timings): vp.adapters.elevenlabs:voice_main
  music estimate|make ...                    music with the person's ElevenLabs key (paid plans) ->
                                             <job>/music: vp.adapters.elevenlabs:music_main
  telemetry status|on|off|show|flush|show-id  test-period telemetry (allowlisted fields only,
                                             after the terms; `show` sends nothing): vp.telemetry:main
  insight add|preview       record or preview a filtered insight card: vp.insight:main
  messages poll|ack                          messages from the authors: vp.messages:main
  pick folder|file [--json]                 open the macOS folder or file chooser: vp.pick:main
  kpack status|sync|rollback|show            signed knowledge packs
  case list|show [--json]                    browse effective cases
  busy [--json]                              is a render/upload/generation running: vp.busy:main
  app restart-claude|quit capcut|status      quit/restart Claude and CapCut for the person (never
                                             Cmd+Q by hand): vp.app:main
  update auto status|on|off | decline | reminder due|shown | check
                                             plugin auto-update switch and reminders: vp.update:main
  version [--json] [--lang en|ru]            the release line — version, stage, build, release
                                             time ("0.8.8 alpha · build 57 · 2026-11-23 22:40 UTC"):
                                             vp.buildinfo:main

  shots detect|list|describe-set <job> ...   shots of a folder of videos, keyframes, descriptions
                                             (capability К): vp.analysis.scenes:main
  beats <music-file> [--json] [--out F]      tempo, beats, bars of a music file: vp.analysis.beats:main
  montage plan|render <job> ...              music montage cut on the beat (М, Б):
                                             vp.analysis.montage:main
  takes clean|set|apply <job> ...            cut pauses, fillers, restarts from raw takes (Ч):
                                             vp.analysis.takes:main

Every command records itself in the telemetry spool (vp.telemetry: after the terms are accepted;
`vp telemetry off` or AIMC_TELEMETRY=off disables), uncaught errors included (their class only), and starts a detached background flush (throttled) when it ends.
Long commands (BUSY_COMMANDS) hold a busy marker (vp.busy) while they run.

Global option: --home <dir> (anywhere on the line, also after `learn` / `knowledge`) sets VP_HOME
for the command — the pipeline home with config.yaml, knowledge store, journals.

Exit codes: 0 done/gate/locked, 2 failed/abandoned, 3 internal / not implemented,
5 consent-required: a real upload/publish (run, stage, resolve-upload, approve-ingest,
`approve set --decision approve`) without the accepted terms; the line starts with
`REFUSED consent-required`, nothing was sent; rehearsals (publisher: fake) are not affected.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from yaml import YAMLError

from .core import runner
from .core.errors import (EXIT_CONSENT_REQUIRED, ConsentRequired, VPError, failure_noted,
                          note_failure, take_failure)
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
    "consent": "vp.safety:consent_main",
    "guard": "vp.safety:guard_main",
    "mode": "vp.safety:mode_main",
    "telemetry": "vp.telemetry:main",
    "insight": "vp.insight:main",
    "messages": "vp.messages:main",
    "pick": "vp.pick:main",
    "reel": "vp.steps.local_build:reel_main",
    "long-plan": "vp.steps.local_build:long_plan_main",
    "long-render": "vp.steps.local_build:long_render_main",
    "kpack": "vp.kpack.cli:main",
    "case": "vp.kpack.cli:case_main",
    "busy": "vp.busy:main",
    "app": "vp.app:main",
    "update": "vp.update:main",
    "version": "vp.buildinfo:main",
    "transcribe": "vp.transcribe:main",
    "captions": "vp.captions:main",
    # scenario tree (route, steps, services, cost): vp.scenarios — keep this block together
    "scenario": "vp.scenarios:main",
    "tutorial": "vp.tutorials:main",
    # material without speech, music, raw takes (SCENARIOS К, Б, М, Ч)
    "shots": "vp.analysis.scenes:main",
    "beats": "vp.analysis.beats:main",
    "montage": "vp.analysis.montage:main",
    "takes": "vp.analysis.takes:main",
}

# ---- stock, image generation, voice, music and their keys (capabilities С, Г, docs/SCENARIOS.md)
MEDIA_COMMANDS = {
    "stock": "vp.adapters.stock:main",
    "images": "vp.adapters.images:main",
    "keys": "vp.keys:main",
    "voice": "vp.adapters.elevenlabs:voice_main",
    "music": "vp.adapters.elevenlabs:music_main",
}
SUBCOMMAND_MAINS.update(MEDIA_COMMANDS)
# ---------------------------------------------------------------------------------------------
# commands that may run long (render, upload, export, install): a busy marker while they run,
# except the quick sub-commands listed in QUICK_SUBCOMMANDS
BUSY_COMMANDS = {"run", "stage", "resolve-upload", "approve-ingest", "capcut", "resolve", "fcpxml",
                 "extras", "design", "check", "transcribe", "captions",
                 "stock", "images", "shots", "montage", "takes", "voice", "music", "text"}
QUICK_SUBCOMMANDS = {("extras", "status"), ("extras", "check"), ("extras", "decide"),
                     ("text", "new"), ("text", "status"), ("text", "approve"),
                     ("text", "estimate"), ("text", "timeline"), ("text", "timing"),
                     ("voice", "samples"),
                     ("capcut", "check"), ("capcut", "version"), ("capcut", "library"), ("design", "show"),
                     ("design", "set"), ("design", "layers"),
                     ("transcribe", "estimate"), ("transcribe", "key"),
                     ("captions", "plan"), ("captions", "keep"),
                     ("stock", "search"), ("images", "estimate"),
                     ("voice", "list"), ("voice", "estimate"), ("music", "estimate"),
                     ("shots", "list"), ("shots", "describe-set"), ("takes", "set")}
# the channel's own commands are neither recorded nor followed by a flush (the mod calls them);
# `version` only reads two files of the plugin (the installer and the release script ask it too)
LOCAL_BUILD_COMMANDS = {"reel", "long-plan", "long-render"}
SILENT_COMMANDS = {"telemetry", "insight", "messages", "kpack", "case", "busy", "progress", "version",
                   "pick", None} | LOCAL_BUILD_COMMANDS
# commands the mod runs in EVERY session (the channel, reminders, restart): they do not make the
# session Elsa's for the prompt journal (vp.journal.mark_session); any other command does
NOT_ELSA_COMMANDS = SILENT_COMMANDS | {"update", "app"}
LOCAL_SUBCOMMANDS = {("capcut", "diff")}


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
    note_failure(None, "not_implemented")
    return EXIT_NOT_IMPLEMENTED


def _with_ctx(job: Job, fn: Callable[..., int], *args: Any) -> int:
    """Run a job-level command under the job lock with a full context."""
    with job_lock(job) as got:
        if not got:
            print("LOCKED")
            return 0
        try:
            from .core.adoption import adopt
            adopt(job)
            cfg = runner.prepare_config(job, None)
            ctx = runner.make_ctx(job, cfg)
            rc = fn(ctx, *args)
        except ConsentRequired as c:
            note_failure(c, "consent")
            print(f"REFUSED consent-required: {c.msg}")
            return EXIT_CONSENT_REQUIRED
        except VPError as e:
            note_failure(e)
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
        s = sub.add_parser(name, allow_abbrev=name != "run")
        s.add_argument("job")
        if name == "run":
            s.add_argument("--whole-recording", nargs="?", const="full", choices=("full", "reel"),
                           help="make one full video or vertical reel from the entire recording")
            s.add_argument("--plan", action="store_true", help="show what a run would do without writing")
            s.add_argument("--json", action="store_true", help="print the plan as JSON")
        if name == "status":
            s.add_argument("--json", action="store_true")
    s = sub.add_parser("stage")
    s.add_argument("job")
    s.add_argument("step")
    s.add_argument("--item")
    s.add_argument("--force", action="store_true")
    s.add_argument("--reset", action="store_true")
    s.add_argument("--check-only", action="store_true",
                   help="check captions on existing fresh S5 renders without rebuilding")
    s = sub.add_parser("resolve-upload")
    s.add_argument("job")
    s.add_argument("item")
    for name in ("learn", "knowledge", "review", "approve", "doctor", "youtube", "results",
                 "progress", "capcut", "resolve", "fcpxml", "asks", "design", "projects", "text",
                 "check", "journal", "feedback", "consent", "guard", "mode", "telemetry", "insight",
                 "messages", "kpack", "case", "busy", "update", "version", "transcribe", "captions",
                 "scenario",
                 "tutorial",
                 "shots", "beats", "montage", "takes") + tuple(MEDIA_COMMANDS):
        s = sub.add_parser(name, add_help=False)
        s.add_argument("rest", nargs=argparse.REMAINDER)
    for name in sorted(LOCAL_BUILD_COMMANDS):
        sub.add_parser(name, add_help=False)
    return p


def _command_of(raw: list[str]) -> tuple[str | None, str | None]:
    """(command, sub-command) — the first two words that are not options; nothing else of argv
    goes to telemetry (the rest may be the person's text)."""
    words = [w for w in raw if not w.startswith("-")]
    cmd = words[0] if words else None
    sub = words[1] if len(words) > 1 and cmd in SUBCOMMAND_MAINS else None
    if sub is not None and ("/" in sub or len(sub) > 40):
        sub = None
    return cmd, sub


def main(argv: Sequence[str] | None = None) -> int:
    raw, home = extract_home(list(argv) if argv is not None else sys.argv[1:])
    if home:
        os.environ["VP_HOME"] = str(Path(home).expanduser().resolve())
    read_status = raw[:1] == ["status"] and os.environ.get("VP_STATUS_READ_ONLY") == "1"
    read_plan = raw[:1] == ["run"] and "--plan" in raw[:raw.index("--") if "--" in raw else len(raw)]
    if read_status or read_plan:
        # Read commands bypass session marks, busy markers, telemetry, and background sync.
        take_failure()
        try:
            return _dispatch(raw)
        finally:
            take_failure()
    cmd, sub = _command_of(raw)
    from . import telemetry
    from .busy import marker
    local = cmd in LOCAL_BUILD_COMMANDS or (cmd, sub) in LOCAL_SUBCOMMANDS
    quiet = cmd in SILENT_COMMANDS or local
    is_busy = cmd in BUSY_COMMANDS and (cmd, sub) not in QUICK_SUBCOMMANDS and not local
    t0 = time.monotonic()
    rc: int | None = None
    take_failure()                      # a stale note of an earlier in-process call never counts
    if not local and cmd not in NOT_ELSA_COMMANDS and os.environ.get("CLAUDE_CODE_SESSION_ID"):
        from .journal import mark_session
        mark_session(os.environ["CLAUDE_CODE_SESSION_ID"])
    try:
        with marker(f"{cmd} {sub}".strip() if sub else str(cmd)) if is_busy \
                else contextlib.nullcontext():
            rc = _dispatch(raw)
        return rc
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
        if rc and not failure_noted():
            note_failure(e, "usage")    # argparse: a missing or unknown argument / sub-command
        raise
    except BaseException as e:
        if not local and not isinstance(e, KeyboardInterrupt):
            telemetry.record_error("cli", e, command=cmd, sub=sub)
        note_failure(e, "interrupted" if isinstance(e, KeyboardInterrupt) else "crash")
        rc = 3
        raise
    finally:
        failure = take_failure()
        if not quiet:
            why: dict[str, str] = {}
            if rc not in (0, None):
                why = failure or {"error": "none", "error_kind": "unclassified"}
            telemetry.record("command", command=cmd, sub=sub, rc=rc,
                             duration_s=round(time.monotonic() - t0, 3), **why)
            telemetry.spawn_flush()
        elif (cmd, sub) == ("insight", "add") and rc == 0 and telemetry.insights_allowed():
            telemetry.spawn_flush()
        if cmd == "run" or (cmd, sub) == ("text", "new"):
            from . import kpack
            kpack.spawn_sync()


def _dispatch(raw: list[str]) -> int:
    # learn / knowledge: everything after the subcommand goes to their own parser untouched
    # (argparse REMAINDER cannot start with an option such as `--force`)
    if raw and raw[0] in SUBCOMMAND_MAINS:
        target = SUBCOMMAND_MAINS[raw[0]]
        fn = _lookup(target)
        try:
            return int(fn(raw[1:]) or 0) if fn else _not_implemented(raw[0])
        except VPError as e:
            note_failure(e)
            print(f"ERROR {e}")
            return 3 if e.kind == "internal" else 2
    parser = build_parser()
    args = parser.parse_args(raw)
    cmd = args.command
    if cmd == "run" and args.json and not args.plan:
        parser.error("--json requires --plan")
    if cmd == "stage" or (cmd == "run" and not args.plan):
        from .projects import safe_touch
        safe_touch(args.job)          # K3: every job worked on is listed by `vp projects list`
    try:
        if cmd == "run":
            job = Job(args.job)
            if args.plan:
                try:
                    report = runner.plan(job, whole_recording=args.whole_recording)
                except (VPError, OSError, ValueError, TypeError, YAMLError) as e:
                    kind = e.kind if isinstance(e, VPError) else "data"
                    print(f"ERROR {kind}: could not compute the run plan", file=sys.stderr)
                    return 3 if kind == "internal" else 2
                if args.json:
                    import json
                    print(json.dumps(report, ensure_ascii=False))
                else:
                    lines = [f"{step['key']}: {step['reason']}" for step in report["steps"]
                             if step["execute"] is True]
                    print("\n".join(lines) if lines else "nothing to do")
                return 0
            return runner.run(job, whole_recording=args.whole_recording,
                              out=lambda line: print(line, flush=True))
        if cmd == "status":
            from . import position
            job = Job(args.job)
            doc = position.of(job.root)
            failures = runner.caption_failures(job) if doc["kind"] == "recording" else {}
            if failures and doc["state"] != "running":
                from .core.graph import Instance

                item, failure = next(iter(failures.items()))
                inst = Instance("S5", item)
                if doc["engine"] and doc["engine"].split(".", 1)[0] in position._GRAPH.order:
                    order = position._GRAPH.order
                    if order.index(doc["engine"].split(".", 1)[0]) >= order.index(inst.step):
                        meta = {"kind": "data", "error": failure}
                        moment = position._at_instance(job, inst, {"state": "failed", "metadata": meta},
                                                       meta, doc["since"])
                        focus = {**(doc["focus"] or {}), "item": item}
                        doc = position._document(job, moment, scenario_id=doc["scenario"],
                                                 focus=focus)
            if args.json:
                import json
                from .core.lock import holder
                from .core.status import Status
                status = Status(job).load()
                if failures and doc["state"] == "failed" and doc["engine"] in {
                        f"S5.{item}" for item in failures}:
                    status.update(current_stage=doc["engine"], current_state="failed")
                print(json.dumps({**status, "job": job.job_id,
                                  "job_folder": str(job.root), "lock": holder(job),
                                  "position": doc}, ensure_ascii=False, indent=2, default=str))
            else:
                report = runner.status_report(job)
                if doc["state"] == "new":
                    report = report.replace("\ncurrent: None None", f"\ncurrent: {doc['engine']} new", 1)
                print(f"{report}\n\n{position.describe(doc)}")
            return 0
        if cmd == "stage":
            return runner.run_stage(Job(args.job), args.step, args.item, force=args.force,
                                    reset=args.reset, check_only=args.check_only)
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
        note_failure(e)
        print(f"ERROR {e}")
        return 3 if e.kind == "internal" else 2
    return 3  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
