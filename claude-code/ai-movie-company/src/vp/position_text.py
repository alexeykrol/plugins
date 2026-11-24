"""Read the current position of a text job from its local artifacts."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from shlex import quote

from . import scenarios, text
from .core import progress
from .core.errors import VPError
from .core.io import parse_iso, read_json

_ENGINE = {
    "text_new": "brief", "intake": "brief", "brief_gate": "brief", "text_brief_gate": "brief",
    "screenplay": "script", "sketches": "sketches", "sketch_gate": "approve_sketches",
    "voice": "voice", "music": "music", "animate": "animation", "render_text": "render",
    "capcut_text": "capcut", "images": "sketches", "stock": "sketches", "facts": "script",
    "ai_video_gate": "sketches", "ai_video": "sketches",
}
_PROGRESS_STEPS = {"render": "render_text", "master": "render_text", "voice": "voice"}
_VERSION = re.compile(r"v\d+")


def _mtime(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _latest(*paths: Path | None) -> Path | None:
    return max(((_mtime(p), p) for p in paths if p is not None),
               key=lambda pair: pair[0] if pair[0] is not None else float("-inf"),
               default=(None, None))[1]


def _sketches(job: Path, scenes: list[dict]) -> list[Path]:
    files = {f.stem.lower(): f for f in text._files(job / "sketches", text.IMAGE_EXT)}
    found = []
    for index, scene in enumerate(scenes, 1):
        names = (str(scene["id"]).lower(), f"s{index:02d}", f"s{index}")
        match = next((files[name] for name in names if name in files and files[name] not in found), None)
        if match is not None:
            found.append(match)
    return found


def _sketch_approval(job: Path, approved: dict, sketches: list[Path]) -> Path | None:
    if "sketches" not in approved:
        return None
    path = job / text.APPROVALS
    record = approved["sketches"]
    try:
        # Older approvals were written to whole seconds. A sketch within that same second
        # may precede the approval; use the file mtime to break a tie when it is reliable.
        at = parse_iso(str(record["at"])).timestamp()
    except (KeyError, TypeError, ValueError):
        at = _mtime(path)
    if at is not None:
        recorded = _mtime(path)
        if recorded is not None and int(recorded) == int(at):
            at = max(at, recorded)
        elif at == int(at) and isinstance(record, dict) and record.get("at"):
            at += 1
        if any(mtime > at for p in sketches if (mtime := _mtime(p)) is not None):
            return None
    return path


def _optional_artifacts(job: Path, music: Path | None) -> dict[str, Path | None]:
    return {
        "music": music,
        "images": _latest(*text._files(job / "library" / "generated", text.IMAGE_EXT)),
        "stock": _latest(*text._files(job / "library" / "stock",
                                     text.IMAGE_EXT + scenarios.VIDEO_EXT)),
        "facts": _latest(job / "facts.md"),
    }


def _version_evidence(job: Path, requested: str | None) -> tuple[str, list[Path], list[str], str]:
    renders = text.renders(job)
    versions = {m.group(1) for p in renders if (m := text._RENDER_STEM.fullmatch(p.stem))}
    exports = text.capcut_versions(job)
    versions.update(exports)
    for directory, pattern in ((job / "renders", r"(v\d+)\.timing\.json"),
                               (job / "capcut", r"(v\d+)")):
        if directory.is_dir():
            versions.update(m.group(1) for p in directory.iterdir()
                            if (p.is_file() if directory.name == "renders" else p.is_dir())
                            and (m := re.fullmatch(pattern, p.name)))
    if requested is not None and not _VERSION.fullmatch(requested):
        raise VPError("data", f"version must look like v1, v2 …, got {requested!r}")
    selected = requested or max(versions, key=lambda v: (int(v[1:]), v), default="v1")
    target = selected
    if selected in versions:
        target = max(text.next_render_version(job), f"v{max(int(v[1:]) for v in versions) + 1}",
                     key=lambda v: int(v[1:]))
    return selected, renders, exports, target


def _master_evidence(version: str, mix: Path) -> Path | None:
    mixed_at = _mtime(mix)
    if mixed_at is None:
        return None
    picture = mix.with_name(f"{version}-picture.mp4")
    stamp = mix.with_name(f"{version}.timing.json")
    for path in (stamp, picture):
        at = _mtime(path)
        if at is not None and at >= mixed_at:
            return path
    return None


def _activity(job: Path, version: str | None) -> tuple[dict | None, str | None]:
    """Read the job's tracker without consulting the shared progress mirror."""
    try:
        raw = progress.read_json_raw(job)
        if raw is None:
            return None, None
        live = progress.read(job, locked=False)
        record = live if live is not None else raw
        step, item = record.get("step"), record.get("item")
        if not isinstance(step, str) or step not in _PROGRESS_STEPS:
            return None, None
        if not isinstance(item, str) or not _VERSION.fullmatch(item) or version not in (None, item):
            return None, None
        if live is not None:
            return live, None
        # A normal tracker exit removes its file. Only report an interruption if the
        # expired record is still there after the freshness check.
        parse_iso(str(raw.get("updated_at")))
        if progress.read_json_raw(job) == raw:
            return None, f"An interrupted run was found for {step} {item}."
    except (OSError, ValueError):
        pass
    return None, None


def _inspect(job: Path, version: str | None = None) -> tuple[dict, dict, dict, dict]:
    """Return the position and the evidence needed by the legacy status fields."""
    job = job.expanduser().resolve()
    request = job / "request.yaml"
    plain_text = text.is_text_job(job)
    try:
        run = read_json(job / "run.json", default={})
    except (OSError, ValueError):
        run = {}
    stored = run.get("scenario") if isinstance(run, dict) else None
    scenario = stored if isinstance(stored, str) and stored else ("B" if plain_text else None)
    tree = scenarios.tree()
    routes = {route["id"]: route for route in tree["scenarios"]}
    route = scenarios.steps_of(routes[scenario]) if scenario in routes else []
    workflow = route if any(row["id"] == "render_text" for row in route) else scenarios.steps_of(routes["B"])
    places = {row["id"]: n for n, row in enumerate(route, 1)}
    library = tree["lib"]

    live, interrupted = _activity(job, version)
    selected, renders, exports, target = _version_evidence(job, live["item"] if live else version)
    mix = job / "renders" / f"{selected}.mp4"
    version_render = _latest(*(p for p in renders if (m := text._RENDER_STEM.fullmatch(p.stem))
                               and m.group(1) == selected))
    master = _master_evidence(selected, mix)
    export_dir = job / "capcut" / selected
    export = export_dir / "export.json" if selected in exports else None
    approved = text.approvals(job)

    detail = {"sketches": len(text._files(job / "sketches", text.IMAGE_EXT)),
              "renders": [p.name for p in renders], "capcut_versions": exports}
    try:
        data = text.load_scenes(job)
    except VPError as exc:
        data = None
        detail["scenes_error"] = str(exc)
    scenes = data["scenes"] if data else []
    sketch_files = _sketches(job, scenes)
    voice_files = [p for n, scene in enumerate(scenes)
                   if (p := text.scene_voice(job, scene, n)) is not None]
    if data:
        detail.update(scenes=len(scenes), voiced_scenes=len(voice_files))

    brief = job / "brief.md"
    scenes_json = job / "scenes.json"
    script = scenes_json if scenes_json.is_file() else job / "script.md"
    timeline = job / "timeline.json"
    music = text.pick_music(job)
    optional = _optional_artifacts(job, music)
    have = {
        "brief": brief.is_file(), "script": script.is_file(),
        "sketches": bool(scenes) and len(sketch_files) == len(scenes),
        "voice": bool(scenes) and len(voice_files) == len(scenes),
        "music": music is not None, "animation": timeline.is_file(),
        "render": master is not None, "capcut": export is not None,
    }
    j = quote(str(job))
    evidence = request if request.is_file() else None

    def result(step=None, state="ready", *, at=None, count=0, code=None, waiting=None,
               action="dialog", command=None, message, since=None):
        if since is None and (stamp := _mtime(at or evidence)) is not None:
            since = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        if interrupted:
            code = code or "interrupted_run"
            waiting = f"{waiting} {interrupted}" if waiting else interrupted
        index = places.get(step)
        pos = {
            "kind": "text", "scenario": scenario, "step": step,
            "title": library[step]["title"] if step in library else None,
            "index": index, "total": len(route) if route else None,
            "engine": _ENGINE.get(step), "state": state,
            "waiting_for": {"code": code, "text": waiting} if code else None,
            "next": {"action": action, "command": command, "text": message},
            "since": since,
            "focus": {"item": None, "version": selected, "items_total": len(scenes),
                      "items_at_step": count},
            "report": None,
        }
        return pos, have, detail, approved

    if not plain_text and not stored:
        return result(state="unknown", action="none", message="No text job is recorded here.")
    if route and workflow is not route:
        return result(state="unknown", action="none",
                      message="The recorded scenario does not have a text render route.")
    if live:
        step = _PROGRESS_STEPS[live["step"]]
        return result(step, "running", action="none", since=live["updated_at"],
                      message=f"Work is in progress on {selected} ({live['step']}).")
    if not have["brief"]:
        step = "intake" if "intake" in places or not route else "text_brief_gate"
        return result(step, "new", message="Write brief.md and show the brief for approval.")
    evidence = brief
    for row in workflow:
        step = row["id"]
        if step in ("brief_gate", "text_brief_gate"):
            if "brief" in approved:
                evidence = job / text.APPROVALS
            elif have["script"]:
                evidence = script
            else:
                return result(step, "waiting_person", code="brief_approval",
                              waiting="The brief is waiting for the person's approval.",
                              message=f"Show the brief and record approval with vp text approve {j} brief.")
        elif step == "screenplay":
            if have["script"] and not scenes:
                return result(step, "blocked", at=script, action="fix",
                              message=f"Add valid scene IDs and spoken lines to {script.name}.")
            if not have["script"]:
                return result(step, message="Write script.md with a scene-by-scene voice and picture table.")
            evidence = script
        elif step == "sketches":
            if not have["sketches"]:
                return result(step, at=_latest(*sketch_files), count=len(sketch_files),
                              message="Draw the missing scene sketches and show the complete storyboard.")
            evidence = _latest(*sketch_files)
        elif step == "sketch_gate":
            approval = _sketch_approval(job, approved, sketch_files)
            if approval is None:
                return result(step, "waiting_person", count=len(sketch_files), code="sketch_approval",
                              waiting="The current storyboard is waiting for the person's approval.",
                              message=f"Show the sketches and record approval with vp text approve {j} sketches.")
            evidence = approval
        elif step == "music":
            # the one optional step asked in the dialog: «Без музыки» must survive a pause
            if music is not None:
                evidence = music
            elif "no_music" in approved:
                evidence = job / text.APPROVALS
            elif version_render is None:
                return result(step, "waiting_person", code="music_choice",
                              waiting="The music choice is waiting for the person.",
                              message=(f"Offer generated music, the person's own track, or «Без музыки», "
                                       f"and record «Без музыки» with vp text approve {j} no_music."))
        elif row.get("optional"):
            # chosen in the brief, with graphics by code as the fallback: never a stop of its own
            artifact = optional.get(step)
            if artifact is not None and artifact.is_file():
                evidence = artifact
        elif step == "voice":
            if not have["voice"]:
                return result(step, at=_latest(*voice_files), count=len(voice_files), action="run",
                              command=f"vp voice scenes {j} --voice <voice_id>",
                              message="Voice the missing scenes with the voice chosen by the person.")
            evidence = _latest(*voice_files)
        elif step == "animate":
            if not have["animation"] and version_render is None:
                return result(step, action="run", command=f"vp text timeline {j}",
                              message="Measure the voice files and build the timeline for animation.")
            if have["animation"]:
                evidence = timeline
        elif step == "render_text":
            # an export of this very version means the person went on (jobs from before mastering)
            if master is None and export is None:
                check = f"vp check picture {quote(str(mix.with_name(f'{target}.mp4')))}"
                if mix.is_file():
                    check = f"vp check picture {quote(str(mix))}"
                    return result(step, at=mix, action="run",
                                  command=f"vp text master {j} --version {selected}",
                                  message=f"Master {selected} and run {check} before showing it.")
                return result(step, at=version_render, action="run",
                              command=f"vp text render {j} --composition <Id> --version {target}",
                              message=f"Render {target} and run {check} before showing it.")
            evidence = master or export
        elif step == "capcut_text" and row is workflow[-1]:
            if export is None:
                if export_dir.exists():
                    return result(step, "blocked", at=export_dir, action="fix",
                                  message=f"Inspect the incomplete CapCut folder for {selected} before retrying.")
                return result(step, action="run", command=f"vp capcut {j} --version {selected}",
                              message=f"After the picture self-check, close CapCut and export the {selected} timeline.")
            evidence = export
    final = "capcut_text" if workflow[-1]["id"] == "capcut_text" else "render_text"
    return result(final, "done", action="none", message=f"Version {selected} is complete.")


def of(job: Path, *, version: str | None = None) -> dict:
    """Return the selected version's position without changing the job or reading VP_HOME."""
    return _inspect(job, version)[0]
