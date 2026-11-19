"""Editor-neutral timeline of a finished clip, for export to hand editors (CapCut, later DaVinci).

A clip folder `clips/<id>/` holds `cut.mp4` (the cut, no cards, no burned subtitles) and
`final.srt` (proofread subtitles timed to the rendered video, i.e. shifted by the intro card).
The timeline places the cut at 0 and the subtitles on the cut's own time axis, so an editor
project shows exactly the material the pipeline worked from. Writers (capcut.py, a future
FCPXML writer) take a `ClipTimeline` and know nothing about job folders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core.errors import VPError
from ..core.io import read_yaml
from ..core.srt import Cue, read_srt
from . import media, picture

CANVAS = {"reel": (1080, 1920), "long": (1920, 1080), "full": (1920, 1080)}


@dataclass
class ClipTimeline:
    item: str
    kind: str                       # reel | long | full
    title: str
    video_path: Path                # absolute
    duration_ms: int
    width: int
    height: int
    fps: float
    has_audio: bool
    canvas_w: int
    canvas_h: int
    cues: list[Cue] = field(default_factory=list)   # on the cut's time axis, within [0, duration]


def clip_items(job_root: str | Path) -> list[str]:
    """Ids of clips that have a cut video, sorted."""
    clips = Path(job_root) / "clips"
    if not clips.is_dir():
        return []
    return sorted(p.name for p in clips.iterdir() if (p / "cut.mp4").is_file())


def _kind(clip_dir: Path, item: str, width: int, height: int) -> str:
    for name in ("render.manifest.yaml", "meta.yaml"):
        k = (read_yaml(clip_dir / name, {}) or {}).get("kind")
        if k in CANVAS:
            return k
    if item[:1] == "r":
        return "reel"
    return "reel" if height > width else "long"


def fit_cues(cues: list[Cue], shift_ms: int, duration_ms: int) -> list[Cue]:
    """Shift cues by -shift_ms, drop those outside [0, duration], clamp the rest. Keeps order."""
    out: list[Cue] = []
    for c in cues:
        s, e = c.start_ms - shift_ms, c.end_ms - shift_ms
        if e <= 0 or s >= duration_ms:
            continue
        s, e = max(0, s), min(duration_ms, e)
        if e > s and c.text.strip():
            out.append(Cue(id=c.id, start_ms=s, end_ms=e, text=c.text.strip(), speaker=c.speaker))
    return out


def picture_gate(clip_dir: Path, item: str, cut: Path, kind: str) -> None:
    """Fail closed before an editor project is built (picture invariant, BUILD §3): the cut's own
    `cut.qa.yaml` from S4 must say ok; a cut made before the gate existed is inspected here."""
    qa = read_yaml(clip_dir / "cut.qa.yaml", None)
    if isinstance(qa, dict) and qa.get("ok") is True:
        return
    if isinstance(qa, dict):
        raise VPError("data", f"clip {item} editor export: picture check failed — "
                              + "; ".join(qa.get("failures") or ["see cut.qa.yaml"]))
    accept = read_yaml(clip_dir / "picture.accept.yaml", {}) or {}
    waive = accept.get("waive") or [] if str(accept.get("reason") or "").strip() else []
    picture.check_media(cut, kind, f"clip {item} editor export", waive=waive)


def build_clip_timeline(job_root: str | Path, item: str) -> ClipTimeline:
    root = Path(job_root).expanduser().resolve()
    clip_dir = root / "clips" / item
    cut = clip_dir / "cut.mp4"
    if not cut.is_file():
        raise VPError("data", f"clip {item}: no cut.mp4 in {clip_dir}")
    info = media.probe(cut)
    if not info.get("width"):
        raise VPError("data", f"clip {item}: cut.mp4 has no video stream")
    w, h = int(info["width"]), int(info["height"])
    manifest = read_yaml(clip_dir / "render.manifest.yaml", {}) or {}
    if (clip_dir / "final.srt").is_file():
        cues = read_srt(clip_dir / "final.srt")
        shift = int(manifest.get("intro_ms") or 0)
    elif (clip_dir / "clip.srt").is_file():
        cues, shift = read_srt(clip_dir / "clip.srt"), 0
    else:
        raise VPError("data", f"clip {item}: no final.srt or clip.srt in {clip_dir}")
    kind = _kind(clip_dir, item, w, h)
    picture_gate(clip_dir, item, cut, kind)
    cw, ch = CANVAS[kind]
    meta = read_yaml(clip_dir / "meta.yaml", {}) or {}
    title = str(meta.get("title") or item)
    return ClipTimeline(item=item, kind=kind, title=title, video_path=cut,
                        duration_ms=int(info["duration_ms"]), width=w, height=h,
                        fps=float(info.get("fps") or 30.0), has_audio=bool(info.get("has_audio")),
                        canvas_w=cw, canvas_h=ch,
                        cues=fit_cues(cues, shift, int(info["duration_ms"])))


# ---- rough cut: every layer the pipeline produced, as separate editable items -----------------

@dataclass
class RoughCut:
    """The rendered clip taken apart: intro card, the cut, outro card, card texts, subtitles.

    Times are on the RENDERED timeline (intro + cut + outro), the same timeline as `final.srt`,
    so the subtitles line up with no shifting. Writers decide how to draw each card (a clean
    background + an editable title, or the rendered card itself from `render_path`).
    """
    clip: ClipTimeline              # the cut (video + its own audio)
    intro_ms: int                   # card durations, frame-exact (from render.manifest.yaml)
    outro_ms: int
    intro_text: str                 # card texts (design_texts.yaml)
    outro_text: str
    render_path: Path | None        # render.mp4, if rendered — source of the baked cards
    cues: list[Cue] = field(default_factory=list)   # on the rendered timeline

    @property
    def total_ms(self) -> int:
        return self.intro_ms + self.clip.duration_ms + self.outro_ms


def build_rough_cut(job_root: str | Path, item: str) -> RoughCut:
    root = Path(job_root).expanduser().resolve()
    clip_dir = root / "clips" / item
    tl = build_clip_timeline(root, item)
    manifest = read_yaml(clip_dir / "render.manifest.yaml", {}) or {}
    render = clip_dir / "render.mp4"
    rendered = render.is_file() and bool(manifest)
    intro_ms = int(manifest.get("intro_ms") or 0) if rendered else 0
    outro_ms = int(manifest.get("outro_ms") or 0) if rendered else 0
    texts = read_yaml(clip_dir / "design_texts.yaml", {}) or {}
    total = intro_ms + tl.duration_ms + outro_ms
    if rendered and (clip_dir / "final.srt").is_file():
        cues = fit_cues(read_srt(clip_dir / "final.srt"), 0, total)
    else:
        cues = fit_cues(tl.cues, -intro_ms, total)
    return RoughCut(clip=tl, intro_ms=intro_ms, outro_ms=outro_ms,
                    intro_text=str(texts.get("intro_text") or "").strip() if intro_ms else "",
                    outro_text=str(texts.get("announce_text") or texts.get("outro_text") or "").strip()
                    if outro_ms else "",
                    render_path=render if rendered else None, cues=cues)
