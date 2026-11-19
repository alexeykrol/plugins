"""FCPXML rough-cut writer: a finished clip -> a timeline for DaVinci Resolve and Final Cut Pro.

The export is a rough cut the user finishes by hand in their own editor: every layer the
pipeline produced is a separate editable item —

  spine     intro card | the cut (video + its linked audio) | outro card
  lane 1    the card texts as editable titles over the cards ("Basic Title")
  .srt      the proofread subtitles on the same timeline, imported onto a subtitle track

Subtitles are NOT put inside the FCPXML: caption import from FCPXML differs between editors
(DaVinci Resolve may drop them), while an .srt imports the same way everywhere. Cards are clean
backgrounds in the brand colour, so the text above them stays editable; `--baked-cards` takes
the cards from render.mp4 instead (exactly what was published, text and channel plate baked in).

Format: FCPXML 1.10 (Final Cut Pro 10.6+, DaVinci Resolve 18+). NOT yet checked by importing:
the DaVinci Resolve 19.1 import test is deferred by the owner (2026-11-18), and Final Cut Pro is not
installed on the dev machine. The timeline rate is snapped to a standard rate (standard_rate).

Output goes into the job folder, a NEW folder per export (never overwritten: an editor project
keeps pointing at the card files of its own export):
  <job>/exports/edit/<item>[ N]/<job> <item>.fcpxml, <item>.srt, cards/intro.mp4, cards/outro.mp4

CLI:  vp resolve export <job> [item ...] [--baked-cards] [--out DIR]   (alias: vp fcpxml export)
"""
from __future__ import annotations

import argparse
import json
import shutil
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..core.errors import VPError
from ..core.io import atomic_write
from ..core.srt import write_srt
from . import media
from .edit_timeline import RoughCut, build_rough_cut, clip_items

FCPXML_VERSION = "1.10"
BASIC_TITLE_UID = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
WRAP = {"reel": 18, "long": 34, "full": 34}   # card title line length, characters
CARD_FONT_FRAC = 0.075                        # as the renderer: fraction of min(width, height)
DEFAULT_CARD_BG = "#14181F"


# ---- time ---------------------------------------------------------------------------------

STANDARD_RATES = {23.976: Fraction(1001, 24000), 24.0: Fraction(1, 24), 25.0: Fraction(1, 25),
                  29.97: Fraction(1001, 30000), 30.0: Fraction(1, 30), 50.0: Fraction(1, 50),
                  59.94: Fraction(1001, 60000), 60.0: Fraction(1, 60)}


def standard_rate(fps: float | None) -> float:
    """The nearest editor timeline rate. Zoom and phone recordings are variable-rate (ffprobe says
    e.g. 30.02); an editor timeline must be one of the standard rates."""
    fps = float(fps or 30.0)
    return min(STANDARD_RATES, key=lambda k: (abs(k - fps), k))


def frame_duration(fps: float | None) -> Fraction:
    return STANDARD_RATES[standard_rate(fps)]


def frames(ms: int, fd: Fraction) -> int:
    return round(Fraction(ms, 1000) / fd)


def rt(n_frames: int, fd: Fraction) -> str:
    """FCPXML rational time of n frames: '0s', '3s', '41/25s', '1001/30000s'."""
    v = fd * n_frames
    if v == 0:
        return "0s"
    return f"{v.numerator}s" if v.denominator == 1 else f"{v.numerator}/{v.denominator}s"


# ---- cards --------------------------------------------------------------------------------

def _card_bg() -> str:
    try:
        from ..core.config import load_config
        from .render import brand_settings
        return str(brand_settings(load_config())["card_bg"])
    except Exception:   # a card colour is not worth failing the export for
        return DEFAULT_CARD_BG


def _ff_rate(fd: Fraction) -> str:
    r = 1 / fd
    return str(r.numerator) if r.denominator == 1 else f"{r.numerator}/{r.denominator}"


def make_clean_card(out: Path, n_frames: int, fd: Fraction, w: int, h: int, color: str) -> Path:
    color = color.lstrip("#")
    return media._render_to(out, [
        "-f", "lavfi", "-i", f"color=c=0x{color}:s={w}x{h}:r={_ff_rate(fd)}",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-frames:v", str(n_frames), "-t", f"{float(fd * n_frames):.6f}",
        "-map", "0:v:0", "-map", "1:a:0", *media.VIDEO_ENC, *media.AUDIO_ENC,
        "-movflags", "+faststart"])


def make_baked_card(out: Path, render: Path, start_frame: int, n_frames: int, fd: Fraction) -> Path:
    return media._render_to(out, [
        "-ss", f"{float(fd * start_frame):.6f}", "-i", str(render),
        "-frames:v", str(n_frames), "-t", f"{float(fd * n_frames):.6f}",
        "-map", "0:v:0", "-map", "0:a:0?", *media.VIDEO_ENC, *media.AUDIO_ENC,
        "-movflags", "+faststart"])


def wrap_title(text: str, kind: str) -> str:
    from .render import wrap_text
    return "\n".join(wrap_text(text, WRAP.get(kind, 34))) if text else ""


# ---- XML ----------------------------------------------------------------------------------

def build_fcpxml(rc: RoughCut, name: str, cards: dict[str, Path], titles: bool = True) -> str:
    """Pure: the rough cut + paths of the card media -> FCPXML text.

    `cards` maps 'intro'/'outro' to their media files (only the cards that exist).
    `titles`: put the card texts as editable titles over the cards (off for baked cards).
    """
    tl = rc.clip
    fd = frame_duration(tl.fps)
    n_clip = frames(tl.duration_ms, fd)
    n_card = {"intro": frames(rc.intro_ms, fd), "outro": frames(rc.outro_ms, fd)}
    text = {"intro": rc.intro_text, "outro": rc.outro_text}

    root = ET.Element("fcpxml", version=FCPXML_VERSION)
    res = ET.SubElement(root, "resources")
    ET.SubElement(res, "format", id="r1", name=f"{name} canvas", frameDuration=rt(1, fd),
                  width=str(tl.canvas_w), height=str(tl.canvas_h), colorSpace="1-1-1 (Rec. 709)")
    ET.SubElement(res, "format", id="r2", name=f"{name} media", frameDuration=rt(1, fd),
                  width=str(tl.width), height=str(tl.height), colorSpace="1-1-1 (Rec. 709)")
    n_res = 2

    def asset(label: str, path: Path, n: int, audio: bool) -> str:
        nonlocal n_res
        n_res += 1
        rid = f"r{n_res}"
        attrs: dict[str, str] = {"id": rid, "name": label, "start": "0s", "duration": rt(n, fd),
                                 "hasVideo": "1", "format": "r2", "videoSources": "1"}
        if audio:
            attrs.update(hasAudio="1", audioSources="1", audioChannels="2", audioRate="48000")
        a = ET.SubElement(res, "asset", attrs)
        ET.SubElement(a, "media-rep", kind="original-media", src=Path(path).resolve().as_uri())
        return rid

    order = [("intro", cards.get("intro")), ("clip", tl.video_path), ("outro", cards.get("outro"))]
    refs = {}
    for role, path in order:
        if path is None:
            continue
        n = n_clip if role == "clip" else n_card[role]
        if n <= 0:
            continue
        refs[role] = (asset(f"{name} {role}" if role != "clip" else f"{name} cut", path, n,
                            True if role != "clip" else tl.has_audio), n)
    want_titles = titles and any(text[r] for r in ("intro", "outro") if r in refs)
    if want_titles:
        n_res += 1
        title_ref = f"r{n_res}"
        ET.SubElement(res, "effect", id=title_ref, name="Basic Title", uid=BASIC_TITLE_UID)

    lib = ET.SubElement(root, "library")
    event = ET.SubElement(lib, "event", name="AI MovieCompany")
    project = ET.SubElement(event, "project", name=name)
    total = sum(n for _, n in refs.values())
    seq = ET.SubElement(project, "sequence", format="r1", duration=rt(total, fd), tcStart="0s",
                        tcFormat="NDF", audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(seq, "spine")
    offset, n_style = 0, 0
    size = str(max(12, int(min(tl.canvas_w, tl.canvas_h) * CARD_FONT_FRAC)))
    for role in ("intro", "clip", "outro"):
        if role not in refs:
            continue
        rid, n = refs[role]
        label = "cut" if role == "clip" else role
        clip = ET.SubElement(spine, "asset-clip", ref=rid, name=f"{name} {label}",
                             offset=rt(offset, fd), duration=rt(n, fd), format="r2", tcFormat="NDF")
        if want_titles and role != "clip" and text[role]:
            n_style += 1
            sid = f"ts{n_style}"
            t = ET.SubElement(clip, "title", ref=title_ref, lane="1", offset="0s",
                              name=f"{name} {role} text", duration=rt(n, fd))
            body = ET.SubElement(t, "text")
            ET.SubElement(body, "text-style", ref=sid).text = wrap_title(text[role], tl.kind)
            sdef = ET.SubElement(t, "text-style-def", id=sid)
            ET.SubElement(sdef, "text-style", font="Helvetica", fontSize=size, fontFace="Bold",
                          fontColor="1 1 1 1", alignment="center")
        offset += n
    ET.indent(root, space="  ")
    for t in root.iter("text"):          # no layout whitespace inside a title's text
        t.text = None
        for child in t:
            child.tail = None
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n' + body + "\n"


# ---- export -------------------------------------------------------------------------------

def _unique_dir(base: Path) -> Path:
    if not base.exists():
        return base
    n = 2
    while (base.parent / f"{base.name} {n}").exists():
        n += 1
    return base.parent / f"{base.name} {n}"


def export_clip(job_root: Path, item: str, out_root: Path | None = None, baked_cards: bool = False,
                card_bg: str | None = None) -> dict[str, Any]:
    rc = build_rough_cut(job_root, item)
    tl = rc.clip
    fd = frame_duration(tl.fps)
    name = f"{job_root.name} {item}"
    folder = _unique_dir((out_root or job_root / "exports" / "edit") / item)
    staging = folder.parent / f".vp-staging-{folder.name}"
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "cards").mkdir(parents=True)
    try:
        cards: dict[str, Path] = {}
        n_in, n_out = frames(rc.intro_ms, fd), frames(rc.outro_ms, fd)
        n_render_clip = frames(tl.duration_ms, fd)
        for role, n, start in (("intro", n_in, 0), ("outro", n_out, n_in + n_render_clip)):
            if n <= 0:
                continue
            dst = staging / "cards" / f"{role}.mp4"
            if baked_cards and rc.render_path:
                make_baked_card(dst, rc.render_path, start, n, fd)
            else:
                make_clean_card(dst, n, fd, tl.width, tl.height, card_bg or _card_bg())
            cards[role] = folder / "cards" / f"{role}.mp4"     # final location
        xml = build_fcpxml(rc, name, cards, titles=not (baked_cards and rc.render_path))
        atomic_write(staging / f"{name}.fcpxml", xml)
        if rc.cues:
            write_srt(staging / f"{item}.srt", rc.cues, renumber=True)
        folder.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(folder)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"item": item, "folder": str(folder), "fcpxml": str(folder / f"{name}.fcpxml"),
            "srt": str(folder / f"{item}.srt") if rc.cues else None, "cues": len(rc.cues),
            "canvas": f"{tl.canvas_w}x{tl.canvas_h}", "fps": float(1 / fd),
            "duration_ms": rc.total_ms, "cards": sorted(cards), "baked_cards": bool(baked_cards)}


def export_job(job_root: str | Path, items: list[str] | None = None, out_root: Path | None = None,
               baked_cards: bool = False, card_bg: str | None = None) -> list[dict[str, Any]]:
    root = Path(job_root).expanduser().resolve()
    have = clip_items(root)
    if not have:
        raise VPError("data", f"{root}: no finished clips (clips/<id>/cut.mp4)")
    todo = items or have
    missing = [i for i in todo if i not in have]
    if missing:
        raise VPError("data", f"no finished clip {', '.join(missing)} in {root} (have: {', '.join(have)})")
    return [export_clip(root, i, out_root, baked_cards, card_bg) for i in todo]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp resolve",
                                 description="FCPXML rough cut for DaVinci Resolve / Final Cut Pro")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write an FCPXML rough cut + .srt per finished clip")
    e.add_argument("job")
    e.add_argument("items", nargs="*", help="clip ids (default: all finished clips)")
    e.add_argument("--baked-cards", action="store_true",
                   help="take intro/outro cards from render.mp4 (text baked in) instead of "
                        "clean backgrounds with editable titles")
    e.add_argument("--out", help="folder for the exports (default: <job>/exports/edit)")
    e.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = export_job(a.job, a.items or None, Path(a.out).expanduser() if a.out else None, a.baked_cards)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    for r in res:
        print(f"CREATED {r['fcpxml']}\t{r['cues']} subtitles\t{r['canvas']} @ {r['fps']:g} fps")
    print("DaVinci Resolve: File > Import > Timeline… > the .fcpxml; then File > Import > "
          "Subtitle… > the .srt and drag it onto the timeline at 00:00.")
    return 0
