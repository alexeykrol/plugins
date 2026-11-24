"""`vp design` — the Remotion design path the editing director runs on plain-words requests
("красивые титры", "отбивка в начале"): preview in the dialog, the owner's choice, layers.

  vp design show    <job>                         the look this job will render with
  vp design setup                                 install the design tools once (Node + Chrome needed)
  vp design preview <job> <item> [--captions word|phrase|none] [--no-cards]
                                                  render a preview (never touches S5 outputs):
                                                  previews/design/<item>.remotion.mp4 + .poster.png
  vp design set     <job> --renderer remotion|ffmpeg [--captions word|phrase|none] [--plate|--no-plate]
                                                  save the choice and list finished clips due to
                                                  render again (S5)
  vp design layers  <job> <item> [--captions]     editor layers in clips/<item>/layers/ (docs/LAYERS.md)
  vp design studio  <job> <item>                  open the composition in Remotion Studio on
                                                  127.0.0.1 only (for the owner; prints the address)

The preview and the poster are files; the director shows them inside the dialog (Claude Desktop
renders a video or an image file sent to the chat) and asks for the owner's word before `set`.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .adapters import media
from .adapters.render import video_info
from .core.config import RENDERERS, deep_merge, load_config, resolve_job_config
from .core.errors import VPError
from .core.io import read_yaml, sha256_bytes, sha256_obj, write_yaml
from .core.lock import job_lock
from .core.paths import FULL, Job

HIGHLIGHTS = ("word", "phrase", "none")


def item_kind(job: Job, item: str) -> str:
    if item == FULL:
        return "full"
    data = read_yaml(job.fragments_yaml, default={}) or {}
    for f in data.get("fragments") or []:
        if str(f.get("id")) == item:
            kind = f.get("type") or f.get("kind")
            if kind in ("reel", "long"):
                return kind
            raise VPError("data", f"fragment {item}: unknown type {kind!r}")
    raise VPError("data", f"fragment {item} not in fragments.yaml")


def job_config(job: Job, design: dict[str, Any] | None = None) -> dict[str, Any]:
    return resolve_job_config(load_config(), job.request(), job.design() if design is None else design)


def design_override(captions: str | None = None, plate: bool | None = None) -> dict[str, Any]:
    """The `remotion` part of design.yaml for the options given on the command line."""
    cap: dict[str, Any] = {}
    if captions is not None:
        if captions not in HIGHLIGHTS:
            raise VPError("data", f"--captions must be one of {list(HIGHLIGHTS)}")
        cap["highlight"] = captions
    if plate is not None:
        cap["plate"] = bool(plate)
    return {"captions": cap} if cap else {}


def set_design(job: Job, renderer: str | None, captions: str | None = None,
               plate: bool | None = None, *, report_reels: bool = False) -> dict[str, Any]:
    """Save the owner's choice and optionally report S5 freshness under that choice."""
    if renderer is not None and renderer not in RENDERERS:
        raise VPError("data", f"--renderer must be one of {list(RENDERERS)}")
    if renderer == "remotion":
        from .adapters.render_remotion import missing_tools
        miss = missing_tools(job_config(job))
        if miss:
            raise VPError("data", "the design look needs " + " and ".join(miss) + " on this computer; "
                                  "install it and ask again (the plain look stays for now)")
    design = job.design()
    if renderer is not None:
        design["renderer"] = renderer
    over = design_override(captions, plate)
    if over:
        design["remotion"] = deep_merge(design.get("remotion") or {}, over)
    result = {**design, **_look_report(job, design)} if report_reels else design
    write_yaml(job.design_yaml, design)
    return result


def _look_report(job: Job, design: dict[str, Any]) -> dict[str, Any]:
    result = finished_render_plan(job, job_config(job, design))
    redo = result["reels_to_redo"] + result["long_videos_to_redo"]
    pending = result["reels_pending_inputs"] + result["long_videos_pending_inputs"]
    if not any(job.manifest_path("S5", item).is_file() for item in job.items() or []):
        message = "This look will be used when your videos are rendered."
    elif redo:
        message = f"These videos will be redone: {', '.join(redo)}."
    elif pending:
        message = "The look is saved."
    else:
        message = ("Your videos already have this look, nothing is redone. The improved caption "
                   "display reaches a reel when it is redone.")
    if pending:
        message += (" Earlier edits need processing before I can tell whether videos "
                    f"{', '.join(pending)} will be redone.")
    return {**result, "message": message}


def finished_render_plan(job: Job, config: dict[str, Any]) -> dict[str, Any]:
    """Summarize the same S5 rows for local timing, paid checks, and design."""
    from .core.runner import plan

    result = {"reels_to_redo": [], "reels_pending_inputs": [], "long_videos_to_redo": [],
              "long_videos_pending_inputs": [], "reels_ready_to_check": [], "reel_checks": {}}
    if not any(job.manifest_path("S5", item).is_file() for item in job.items() or []):
        return result
    for row in plan(job, config=config, through="S5")["steps"]:
        item = row["item"]
        if row["stage"] != "S5" or not job.manifest_path("S5", item).is_file():
            continue
        kind = "reels" if item_kind(job, item) == "reel" else "long_videos"
        if row["blocked"] or row["blocked_by_gate"] or row["execute"] is None:
            result[f"{kind}_pending_inputs"].append(item)
        elif row["execute"]:
            result[f"{kind}_to_redo"].append(item)
        elif kind == "reels":
            result["reels_ready_to_check"].append(item)
            result["reel_checks"][item] = (row["check"] if row["check"] == "waived"
                                           else row["saved_check"] or row["check"])
    return result


def _design_words(job: Job, item: str) -> Path:
    return job.previews_dir / "design" / f"{item}.words.json"


def _renderer(cfg: dict[str, Any], job: Job, item: str):
    from .adapters.render_remotion import RemotionRenderer
    sidecar = _design_words(job, item) if item_kind(job, item) == "reel" else None
    return RemotionRenderer(cfg, caption_sidecar=sidecar)


def _inputs(job: Job, item: str) -> tuple[Path, Path, dict[str, Any]]:
    cut, srt = job.cut_mp4(item), job.clip_srt(item)
    missing = [p.name for p in (cut, srt) if not p.is_file()]
    if missing:
        raise VPError("data", f"{item}: {', '.join(missing)} not ready yet — the run makes them "
                              f"after the fragments are approved (S4)")
    texts = read_yaml(job.design_texts(item), default=None)
    _word_times(job, item, cut, srt)
    return cut, srt, texts if isinstance(texts, dict) else {}


def _word_times(job: Job, item: str, cut: Path, srt: Path) -> None:
    """Use S5's timing preparation and keep the design cache separate from its render outputs."""
    from . import transcribe
    from .analysis import align_words
    if item_kind(job, item) != "reel":
        return
    from .core.ctx import Ctx
    from .steps import s4_cut, s5_render
    ctx = Ctx(job, item, job_config(job))
    fragment = s4_cut.fragment_record(ctx)
    # S5 can fall through to another candidate when the first cannot align to this clip.
    candidates = list(transcribe._job_word_candidates(job, job.source_video))
    old = transcribe._document(align_words.words_path(srt))
    identity = sha256_obj({
        "subtitles": sha256_bytes(srt.read_bytes()),
        "timing_sources": [vars(candidate) for candidate in candidates],
        "word_files": [(str(path), transcribe.media_digest(path))
                       for path in (job.source_words, job.caption_words) if path.is_file()],
        "legacy_sidecar": old if old.get("schema") == 1 else None,
        "range": [fragment["start_ms"], fragment["end_ms"]],
        "cut": transcribe.media_digest(cut),
        "s4": s5_render._cut_manifest_digests(ctx),
    })
    cache = _design_words(job, item)
    if align_words.clip_words_current(srt, sidecar=cache, timing_inputs=identity):
        return
    clip_ms = video_info(cut)["video_ms"] or media.duration_ms(cut)
    s5_render.prepare_clip_words(ctx, fragment, clip_ms, sidecar=cache, timing_inputs=identity)


def preview(job: Job, item: str, captions: str | None = None, cards: bool = True) -> dict[str, Any]:
    """Render the Remotion look of one item into previews/design/ (S5 outputs untouched)."""
    cut, srt, texts = _inputs(job, item)
    kind = item_kind(job, item)
    design = deep_merge(job.design(), {"remotion": design_override(captions)})
    cfg = job_config(job, design)
    if not cards:
        cfg = deep_merge(cfg, {"render": {"cards": {kind: {"intro_ms": 0, "outro_ms": 0}}}})
    out_dir = job.previews_dir / "design"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{item}.remotion.mp4"
    r = _renderer(cfg, job, item)
    r.workspace.ensure()
    res = r.render_clip(cut, srt, texts, kind, out, layers_dir=None)
    # Poster: a frame inside the clip where captions are on screen (about a third in).
    clip_ms = int(res["duration_ms"]) - int(res["intro_ms"]) - int(res["outro_ms"])
    at_ms = int(res["intro_ms"]) + max(0, clip_ms // 3)
    poster = out_dir / f"{item}.remotion.poster.png"
    media.run_ffmpeg(["-ss", f"{at_ms / 1000:.3f}", "-i", str(out), "-frames:v", "1",
                      "-update", "1", str(poster)])
    info = video_info(out)
    return {"item": item, "kind": kind, "preview": str(out), "poster": str(poster),
            "duration_ms": res["duration_ms"], "width": info["width"], "height": info["height"],
            "captions": r.rs["captions"]["highlight"] if kind == "reel" else "youtube-track",
            # aligned = the words light up by the voice; estimated_voiced = phrases whole, words by
            # the pauses of the sound where that is safe; estimated = every phrase whole
            "word_times": r.caption_info.get("word_times") if kind == "reel" else None,
            "seconds": r.timings.get("total")}


def layers(job: Job, item: str, captions: bool = False) -> Path:
    cut, srt, texts = _inputs(job, item)
    kind = item_kind(job, item)
    cfg = job_config(job)
    manifest = read_yaml(job.render_manifest(item), default={}) or {}
    final = "../render.mp4" if job.render_mp4(item).is_file() else None
    r = _renderer(cfg, job, item)
    r.workspace.ensure()
    path = r.render_layers(cut, srt, texts, kind, job.item_dir(item) / "layers", captions=captions,
                           final=final)
    data = read_yaml(path, default={}) or {}
    data["final_renderer"] = manifest.get("renderer")  # the final may come from ffmpeg
    write_yaml(path, data)
    return path


def show(job: Job) -> dict[str, Any]:
    from .adapters.render_remotion import remotion_settings
    cfg = job_config(job)
    out: dict[str, Any] = {"renderer": cfg.get("renderer", "ffmpeg"), "design_yaml": job.design_yaml.is_file()}
    if out["renderer"] == "remotion":
        rs = remotion_settings(cfg)
        out["captions"] = rs["captions"]
        out["accent_color"] = rs["accent_color"]
    return out


def studio(job: Job, item: str) -> int:
    """Remotion Studio on 127.0.0.1 with this item's props as defaults (owner's tool)."""
    cut, srt, texts = _inputs(job, item)
    kind = item_kind(job, item)
    r = _renderer(job_config(job), job, item)
    ws = r.workspace
    ws.ensure()
    info = video_info(cut)
    clip_ms = info["video_ms"] or media.duration_ms(cut)
    public = ws.dir / "public"
    if public.exists():
        shutil.rmtree(public)
    public.mkdir()
    os.link(cut, public / "cut.mp4") if os.stat(cut).st_dev == os.stat(public).st_dev \
        else os.symlink(Path(cut).resolve(), public / "cut.mp4")
    fps = info["fps"]
    frames = max(1, round(clip_ms * fps / 1000))
    props = {"width": info["width"], "height": info["height"], "fps": fps,
             "durationInFrames": frames, "video": "cut.mp4",
             "captions": r.scheduled_captions(srt, clip_ms, fps, frames) if kind == "reel" else [],
             "captionStyle": r.caption_style(), "brand": r.brand_props(), "showPlate": True}
    pf = ws.dir / "studio.props.json"
    pf.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
    port = 3123
    print(f"Remotion Studio: http://127.0.0.1:{port}  (composition Main; Ctrl+C to stop)")
    cmd = [ws.node, str(ws.cli_js()), "studio", "src/index.ts", "--no-open", f"--port={port}",
           f"--props={pf}", f"--public-dir={public}"]
    return subprocess.call(cmd, cwd=ws.dir, env=ws.env())


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vp design", description="the Remotion design path")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show"); s.add_argument("job")
    sub.add_parser("setup")
    s = sub.add_parser("preview"); s.add_argument("job"); s.add_argument("item")
    s.add_argument("--captions", choices=HIGHLIGHTS); s.add_argument("--no-cards", action="store_true")
    s = sub.add_parser("set"); s.add_argument("job")
    s.add_argument("--renderer", choices=RENDERERS); s.add_argument("--captions", choices=HIGHLIGHTS)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--plate", dest="plate", action="store_true", default=None)
    g.add_argument("--no-plate", dest="plate", action="store_false")
    s = sub.add_parser("layers"); s.add_argument("job"); s.add_argument("item")
    s.add_argument("--captions", action="store_true")
    s = sub.add_parser("studio"); s.add_argument("job"); s.add_argument("item")
    a = p.parse_args(argv)

    if a.cmd == "setup":
        from .adapters.render_remotion import RemotionWorkspace
        ws = RemotionWorkspace(load_config())
        ws.ensure()
        ws.browser_args()  # says plainly when Chrome is missing
        print(f"design tools ready: {ws.dir}")
        return 0
    job = Job(a.job)
    if a.cmd == "show":
        print(json.dumps(show(job), ensure_ascii=False, indent=2))
    elif a.cmd == "preview":
        print(json.dumps(preview(job, a.item, a.captions, not a.no_cards), ensure_ascii=False, indent=2))
    elif a.cmd == "set":
        if a.renderer is None and a.captions is None and a.plate is None:
            p.error("set: nothing to set")
        with job_lock(job) as got:
            if not got:
                raise VPError("data", "the job is busy; take this look after the current run ends")
            from .core.adoption import adopt
            adopt(job)
            result = set_design(job, a.renderer, a.captions, a.plate, report_reels=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif a.cmd == "layers":
        print(layers(job, a.item, a.captions))
    elif a.cmd == "studio":
        return studio(job, a.item)
    return 0
