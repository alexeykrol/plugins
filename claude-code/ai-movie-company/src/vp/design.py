"""`vp design` — the Remotion design path the editing director runs on plain-words requests
("красивые титры", "отбивка в начале"): preview in the dialog, the owner's choice, layers.

  vp design show    <job>                         the look this job will render with
  vp design setup                                 install the design tools once (Node + Chrome needed)
  vp design preview <job> <item> [--captions word|phrase|none] [--no-cards]
                                                  render a preview (never touches S5 outputs):
                                                  previews/design/<item>.remotion.mp4 + .poster.png
  vp design set     <job> --renderer remotion|ffmpeg [--captions word|phrase|none] [--plate|--no-plate]
                                                  write the choice to <job>/design.yaml; the next run
                                                  re-renders the items whose look changed (S5)
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
from .core.io import read_yaml, write_yaml
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
               plate: bool | None = None) -> dict[str, Any]:
    """Merge the owner's choice into <job>/design.yaml (other keys kept). Returns the new design."""
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
    write_yaml(job.design_yaml, design)
    return design


def _renderer(cfg: dict[str, Any]):
    from .adapters.render_remotion import RemotionRenderer
    return RemotionRenderer(cfg)


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
    """A reel's look rendered before S5 ran gets the same word times S5 gives its captions
    (`clip.words.json` next to clip.srt, see vp.steps.s5_render). A file made for the present
    text of clip.srt is left alone; one made for another text is made again (the renderer would
    refuse it and show every phrase whole)."""
    from .analysis import align_words
    if item_kind(job, item) != "reel" or align_words.clip_words_current(srt):
        return
    from .core.ctx import Ctx
    from .steps import s4_cut, s5_render
    ctx = Ctx(job, item, job_config(job))
    clip_ms = video_info(cut)["video_ms"] or media.duration_ms(cut)
    s5_render.prepare_clip_words(ctx, s4_cut.fragment_record(ctx), clip_ms)


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
    r = _renderer(cfg)
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
    r = _renderer(cfg)
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
    r = _renderer(job_config(job))
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
    props = {"width": info["width"], "height": info["height"], "fps": info["fps"],
             "durationInFrames": max(1, round(clip_ms * info["fps"] / 1000)),
             "video": "cut.mp4", "captions": r.captions(srt, clip_ms) if kind == "reel" else [],
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
        print(json.dumps(set_design(job, a.renderer, a.captions, a.plate), ensure_ascii=False, indent=2))
    elif a.cmd == "layers":
        print(layers(job, a.item, a.captions))
    elif a.cmd == "studio":
        return studio(job, a.item)
    return 0
