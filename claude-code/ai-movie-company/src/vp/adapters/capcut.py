"""CapCut desktop draft writer: a finished clip -> a NEW CapCut project to finish by hand.

The project has a video track with the cut and a text track with one editable subtitle segment
per cue (the same kind of segments CapCut makes when you import an .srt).

FORMAT IS VERSION-BOUND. CapCut's draft format is undocumented. The writer fills
`capcut_template.json`, derived from a real CapCut 9.8.0 (macOS) project by
`scripts/capcut_template_from_draft.py` — every field CapCut wrote is kept, only ids, paths,
times and texts are ours. When a CapCut update stops opening our drafts, re-derive the template
(see that script) instead of patching fields here.

Safety rules (owner, 2026-11-18):
  * only NEW folders are created in the CapCut drafts folder; existing projects are never
    touched, moved or deleted;
  * the project registry `root_meta_info.json` is updated only after a timestamped backup copy
    is saved under the pipeline home (`capcut/backups/`);
  * nothing is written while CapCut is running (it rewrites its registry on exit);
  * the cut is COPIED into the project (`Resources/media/`): CapCut is sandboxed and cannot read
    files outside ~/Movies that the user did not pick by hand.

Opener (owner, 2026-11-18): `--opener <video>` puts a title clip before the cut in the same
project. The opener is prepared once with ffmpeg into the project's media folder: fitted into the
cut's frame without stretching (black bars when the shapes differ), given a silent sound track
when it has none, and its last DISSOLVE_MS cross-dissolve into the first frames of the cut (baked
in: the template has no CapCut transition to copy). The cut segment then starts that far into the
cut, so the cut, its sound and the subtitles all move by (opener length - dissolve).

CLI:  vp capcut export <job> [item ...] [--opener VIDEO] [--dissolve SEC] [--drafts DIR]
      vp capcut check                         (is CapCut running, where are the drafts)
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..core.errors import VPError
from ..core.io import atomic_write
from ..core.paths import vp_home
from . import media
from .edit_timeline import ClipTimeline, build_clip_timeline, clip_items, fit_cues

TEMPLATE_PATH = Path(__file__).with_name("capcut_template.json")
DEFAULT_DRAFTS = Path.home() / "Movies" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
ROOT_META = "root_meta_info.json"
MEDIA_DIR = "Resources/media"   # the cut is copied here (CapCut cannot read the job folder)
SUB_Y = {"reel": -0.6, "long": -0.8, "full": -0.8}   # subtitle position, canvas units (-1 = bottom)
SUB_SIZE = {"reel": 6.0, "long": 5.0, "full": 5.0}    # CapCut text size units


def drafts_root() -> Path:
    env = os.environ.get("VP_CAPCUT_DRAFTS")
    return Path(env).expanduser() if env else DEFAULT_DRAFTS


def capcut_running() -> bool:
    """True if the CapCut desktop app is running (macOS process name 'CapCut').

    Fails CLOSED: when the check itself cannot run (no pgrep, timeout) the answer is an error,
    not "not running" — writing while CapCut runs loses the new project."""
    try:
        proc = subprocess.run(["pgrep", "-x", "CapCut"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise VPError("data", f"cannot tell whether CapCut is running ({e.__class__.__name__}); "
                              "nothing was written. Quit CapCut and try again.") from e
    if proc.returncode not in (0, 1):          # 1 = no such process; anything else = check failed
        raise VPError("data", f"cannot tell whether CapCut is running (pgrep exit "
                              f"{proc.returncode}); nothing was written.")
    return proc.returncode == 0


CAPCUT_APPS = (Path("/Applications/CapCut.app"), Path.home() / "Applications" / "CapCut.app")


def capcut_version(apps: tuple[Path, ...] | None = None) -> str | None:
    """CapCut desktop version from its Info.plist (VP_CAPCUT_APP overrides the location)."""
    import plistlib
    env = os.environ.get("VP_CAPCUT_APP")
    for app in ((Path(env).expanduser(),) if env else (apps or CAPCUT_APPS)):
        try:
            with open(app / "Contents" / "Info.plist", "rb") as f:
                return str(plistlib.load(f).get("CFBundleShortVersionString") or "") or None
        except (OSError, plistlib.InvalidFileException):
            continue
    return None


def version_warning(installed: str | None, tpl: dict) -> str | None:
    """A warning when the installed CapCut is not the version the draft template comes from."""
    want = tpl["source"]["capcut_app_version"]
    if installed is None:
        return (f"CapCut version unknown (CapCut.app not found); the project format follows "
                f"CapCut {want}")
    if installed != want:
        return (f"installed CapCut is {installed}, the project format follows CapCut {want}: if "
                f"the project does not open, the template must be re-derived "
                f"(scripts/capcut_template_from_draft.py)")
    return None


def load_template() -> dict[str, Any]:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def _us(ms: int) -> int:
    return int(ms) * 1000


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _extras(protos: list, materials: dict, uid: Callable[[], str]) -> list[str]:
    refs = []
    for kind, proto in protos:
        obj = copy.deepcopy(proto)
        obj["id"] = uid()
        materials.setdefault(kind, []).append(obj)
        refs.append(obj["id"])
    return refs


DISSOLVE_MS = 800      # opener -> cut cross-dissolve (owner: "~0.5-1 s")


@dataclasses.dataclass(frozen=True)
class Opener:
    """A prepared opener clip: same frame size as the cut, has sound, dissolve baked in."""
    path: Path
    duration_ms: int        # length on the timeline, dissolve included
    overlap_ms: int         # head of the cut already shown inside the dissolve
    width: int
    height: int
    notes: tuple[str, ...] = ()

    @property
    def shift_ms(self) -> int:
        """Where the cut's time 0 lands on the project timeline."""
        return self.duration_ms - self.overlap_ms


def total_ms(tl: ClipTimeline, opener: Opener | None = None) -> int:
    return tl.duration_ms + (opener.shift_ms if opener else 0)


def timeline_cues(tl: ClipTimeline, opener: Opener | None = None) -> list:
    """Subtitles on the project timeline: moved with the cut when an opener comes first."""
    if not opener:
        return list(tl.cues)
    return fit_cues(tl.cues, -opener.shift_ms, total_ms(tl, opener))


def _video_segment(p: dict, mats: dict, uid: Callable[[], str], path: Path, width: int,
                   height: int, has_audio: bool, media_ms: int, src_start_ms: int,
                   length_ms: int, at_ms: int) -> dict[str, Any]:
    vmat = copy.deepcopy(p["video_material"])
    vmat.update(id=uid(), path=str(path), material_name=path.name, duration=_us(media_ms),
                width=width, height=height, has_audio=has_audio)
    mats["videos"].append(vmat)
    vseg = copy.deepcopy(p["video_segment"])
    vseg.update(id=uid(), material_id=vmat["id"],
                source_timerange={"start": _us(src_start_ms), "duration": _us(length_ms)},
                target_timerange={"start": _us(at_ms), "duration": _us(length_ms)},
                extra_material_refs=_extras(p["video_extra"], mats, uid))
    return vseg


def build_draft_info(tl: ClipTimeline, tpl: dict, timeline_id: str,
                     uid: Callable[[], str] = _uid, opener: Opener | None = None) -> dict[str, Any]:
    """The draft_info.json document for a timeline (pure: no file system access)."""
    p = tpl["proto"]
    info = copy.deepcopy(tpl["draft_info"])
    mats = info["materials"]
    dur = _us(total_ms(tl, opener))
    info.update(id=timeline_id, duration=dur, fps=float(round(tl.fps, 3)) or 30.0)
    info["canvas_config"] = dict(info.get("canvas_config") or {}, ratio="original",
                                 width=tl.canvas_w, height=tl.canvas_h, background=None)

    vsegs = []
    if opener:
        vsegs.append(_video_segment(p, mats, uid, opener.path, opener.width, opener.height, True,
                                    opener.duration_ms, 0, opener.duration_ms, 0))
    skip = opener.overlap_ms if opener else 0
    vsegs.append(_video_segment(p, mats, uid, tl.video_path, tl.width, tl.height, tl.has_audio,
                                tl.duration_ms, skip, tl.duration_ms - skip,
                                opener.duration_ms if opener else 0))
    vtrack = dict(copy.deepcopy(p["video_track"]), id=uid(), segments=vsegs)

    style_tpl = json.loads(p["text_material"]["content"])
    group = f"import_{int(time.time() * 1000)}"
    tsegs = []
    base_index = p["text_segment"].get("render_index", 14000)
    for n, cue in enumerate(timeline_cues(tl, opener)):
        style = copy.deepcopy(style_tpl)
        style["text"] = cue.text
        for s in style.get("styles", []):
            s["range"] = [0, _utf16_len(cue.text)]
            s["size"] = SUB_SIZE.get(tl.kind, 5.0)
        tmat = copy.deepcopy(p["text_material"])
        tmat.update(id=uid(), content=json.dumps(style, ensure_ascii=False), group_id=group,
                    font_size=SUB_SIZE.get(tl.kind, 5.0))
        mats["texts"].append(tmat)
        seg = copy.deepcopy(p["text_segment"])
        seg.update(id=uid(), material_id=tmat["id"], render_index=base_index + n,
                   target_timerange={"start": _us(cue.start_ms),
                                     "duration": _us(cue.end_ms - cue.start_ms)},
                   extra_material_refs=_extras(p["text_extra"], mats, uid))
        seg["clip"] = copy.deepcopy(seg["clip"])
        seg["clip"]["transform"] = {"x": 0.0, "y": SUB_Y.get(tl.kind, -0.8)}
        tsegs.append(seg)
    info["tracks"] = [vtrack]
    if tsegs:
        info["tracks"].append(dict(copy.deepcopy(p["text_track"]), id=uid(), segments=tsegs))
    return info


def build_meta(tl: ClipTimeline, tpl: dict, name: str, folder: Path, draft_id: str,
               now_us: int, size_of: Path | list[Path] | None = None,
               opener: Opener | None = None) -> dict[str, Any]:
    meta = copy.deepcopy(tpl["draft_meta_info"])
    probe_paths = size_of if isinstance(size_of, list) else [size_of or tl.video_path]
    size = sum(pp.stat().st_size for pp in probe_paths if pp.exists())
    meta.update(draft_cover="draft_cover.jpg", draft_fold_path=str(folder),
                draft_root_path=str(folder.parent), draft_id=draft_id, draft_name=name,
                tm_draft_create=now_us, tm_draft_modified=now_us,
                tm_duration=_us(total_ms(tl, opener)), draft_timeline_materials_size_=size)
    if tpl.get("meta_video_entry"):
        pieces = [(tl.video_path, tl.duration_ms, tl.width, tl.height)]
        if opener:
            pieces.insert(0, (opener.path, opener.duration_ms, opener.width, opener.height))
        entries = []
        for path, ms, w, h in pieces:
            e = copy.deepcopy(tpl["meta_video_entry"])
            e.update(id=str(uuid.uuid4()), file_Path=str(path), extra_info=path.name,
                     duration=_us(ms), width=w, height=h,
                     create_time=now_us // 1_000_000, import_time=now_us // 1_000_000,
                     import_time_ms=now_us, roughcut_time_range={"duration": _us(ms), "start": 0})
            entries.append(e)
        for g in meta.get("draft_materials", []):
            if g.get("type") == 0:
                g["value"] = entries
    return meta


def fit_box(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int, int, int]:
    """Fit (src_w, src_h) inside the box keeping its shape: (w, h, x, y), even sizes, centred."""
    k = min(box_w / src_w, box_h / src_h)
    w = min(box_w, max(2, int(src_w * k) // 2 * 2))
    h = min(box_h, max(2, int(src_h * k) // 2 * 2))
    return w, h, (box_w - w) // 2 // 2 * 2, (box_h - h) // 2 // 2 * 2


def _on_frames(ms: float, fps: float) -> int:
    """ms rounded down to a whole frame."""
    fps = fps or 25.0
    return int(int(ms * fps / 1000 + 1e-6) * 1000 / fps)


def opener_plan(src: dict, tl: ClipTimeline, dissolve_ms: int = DISSOLVE_MS) -> dict[str, Any]:
    """What to do with an opener (pure). `src` = media.probe() of the opener file."""
    if not src.get("width") or not src.get("height"):
        raise VPError("data", "the opener has no picture (no video stream)")
    fps = tl.fps or 25.0
    dur = _on_frames(src["duration_ms"], fps)
    if dur < 200:
        raise VPError("data", f"the opener is too short ({src['duration_ms']} ms)")
    d = _on_frames(max(0, min(dissolve_ms, dur // 2, tl.duration_ms // 2)), fps)
    w, h, x, y = fit_box(src["width"], src["height"], tl.width, tl.height)
    notes = []
    same_shape = abs(src["width"] * tl.height - src["height"] * tl.width) <= max(tl.width, tl.height)
    if same_shape:
        if (src["width"], src["height"]) != (tl.width, tl.height):
            notes.append(f"opener {src['width']}x{src['height']} scaled to the cut's frame "
                         f"{tl.width}x{tl.height}")
    else:
        bars = "top and bottom" if y else "left and right"
        notes.append(f"opener {src['width']}x{src['height']} fitted into the cut's frame "
                     f"{tl.width}x{tl.height} without stretching: {w}x{h}, black bars {bars}")
    if not src.get("has_audio"):
        notes.append("opener has no sound: a silent track was added")
    notes.append(f"{d / 1000:g} s cross-dissolve into the cut (baked into the opener clip)"
                 if d else "hard cut into the clip (no dissolve)")
    return {"duration_ms": dur, "overlap_ms": d, "box": (w, h, x, y), "fps": fps,
            "has_audio": bool(src.get("has_audio")), "notes": tuple(notes)}


def opener_ffmpeg_args(src: Path, tl: ClipTimeline, plan: dict) -> list[str]:
    """ffmpeg arguments (without the output path) that build the prepared opener."""
    w, h, x, y = plan["box"]
    fps, dur, d = plan["fps"], plan["duration_ms"] / 1000, plan["overlap_ms"] / 1000
    afmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    args = ["-i", str(src), "-i", str(tl.video_path),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    f = [f"[0:v]scale={w}:{h},setsar=1,pad={tl.width}:{tl.height}:{x}:{y}:black,"
         f"format=yuv420p,trim=duration={dur},setpts=PTS-STARTPTS,fps={fps}[ov]",
         f"[{0 if plan['has_audio'] else 2}:a]{afmt},apad,atrim=duration={dur},"
         f"asetpts=PTS-STARTPTS[oa]"]
    if d:
        f += [f"[1:v]trim=duration={d},setpts=PTS-STARTPTS,scale={tl.width}:{tl.height},setsar=1,"
              f"fps={fps},format=yuv420p[cv]",
              f"[ov][cv]xfade=transition=fade:duration={d}:offset={dur - d},"
              f"tpad=stop_mode=clone:stop=2[v]",      # xfade drops the last frame; -t trims
              f"[{1 if tl.has_audio else 2}:a]{afmt},atrim=duration={d},asetpts=PTS-STARTPTS[ca]",
              f"[oa][ca]acrossfade=d={d}[a]"]
        vout, aout = "[v]", "[a]"
    else:
        vout, aout = "[ov]", "[oa]"
    return [*args, "-filter_complex", ";".join(f), "-map", vout, "-map", aout,
            "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart"]


def _video_ms(path: Path) -> int | None:
    """Length of the picture stream alone (ffprobe), or None."""
    try:
        proc = subprocess.run([media.FFPROBE, "-v", "error", "-select_streams", "v:0",
                               "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
                              capture_output=True, text=True, timeout=60)
        return int(round(float(proc.stdout.strip()) * 1000))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def prepare_opener(src: str | Path, tl: ClipTimeline, out: Path,
                   dissolve_ms: int = DISSOLVE_MS) -> Opener:
    src = Path(src).expanduser().resolve()
    plan = opener_plan(media.probe(src), tl, dissolve_ms)
    media._render_to(out, opener_ffmpeg_args(src, tl, plan), timeout_s=1800)
    got = media.probe(out)
    # the picture can end a frame before the sound: the segment must not outrun the picture
    length = min(plan["duration_ms"], _on_frames(_video_ms(out) or got["duration_ms"], plan["fps"]))
    return Opener(path=out, duration_ms=length, overlap_ms=plan["overlap_ms"],
                  width=got["width"] or tl.width, height=got["height"] or tl.height,
                  notes=plan["notes"])


def build_root_entry(tpl: dict, meta: dict) -> dict[str, Any]:
    e = copy.deepcopy(tpl["root_entry"])
    folder = meta["draft_fold_path"]
    e.update(draft_cover=f"{folder}/draft_cover.jpg", draft_fold_path=folder,
             draft_id=meta["draft_id"], draft_json_file=f"{folder}/draft_info.json",
             draft_name=meta["draft_name"], draft_root_path=meta["draft_root_path"],
             tm_draft_create=meta["tm_draft_create"], tm_draft_modified=meta["tm_draft_modified"],
             tm_duration=meta["tm_duration"],
             draft_timeline_materials_size=meta["draft_timeline_materials_size_"])
    return e


def build_project_json(tpl: dict, timeline_id: str, now_us: int) -> dict[str, Any]:
    pj = copy.deepcopy(tpl["timelines_project"])
    pj.update(id=_uid(), main_timeline_id=timeline_id, create_time=now_us, update_time=now_us)
    pj["timelines"] = [dict(pj["timelines"][0], id=timeline_id, create_time=now_us,
                            update_time=now_us)]
    return pj


def safe_name(s: str) -> str:
    s = re.sub(r"[/\\:\x00-\x1f]+", " ", s).strip().strip(".")
    return re.sub(r"\s+", " ", s)[:80] or "draft"


def unique_folder(root: Path, name: str) -> Path:
    cand, n = root / name, 2
    while cand.exists():
        cand, n = root / f"{name} {n}", n + 1
    return cand


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _cover(tl: ClipTimeline, out: Path) -> None:
    """A frame of the cut as draft_cover.jpg; a missing cover is not fatal for CapCut."""
    at = max(0, min(tl.duration_ms // 2, tl.duration_ms - 100))
    try:
        media.run_ffmpeg(["-ss", f"{at / 1000:.3f}", "-i", str(tl.video_path), "-frames:v", "1",
                          "-vf", "scale=-2:480", "-q:v", "4", str(out)], timeout_s=120)
    except VPError:
        pass


def register(root: Path, entry: dict, backup_dir: Path) -> Path | None:
    """Put `entry` first in root_meta_info.json. Saves a timestamped backup first.

    Returns the backup path (None if there was no registry yet)."""
    reg = root / ROOT_META
    backup = None
    if reg.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"root_meta_info.{time.strftime('%Y%m%d-%H%M%S')}.json"
        n = 2
        while backup.exists():
            backup = backup_dir / f"root_meta_info.{time.strftime('%Y%m%d-%H%M%S')}-{n}.json"
            n += 1
        shutil.copy2(reg, backup)
        data = json.loads(reg.read_text(encoding="utf-8"))
    else:
        data = {"all_draft_store": [], "draft_ids": 0, "root_path": str(root)}
    store = data.setdefault("all_draft_store", [])
    store.insert(0, entry)
    data["draft_ids"] = int(data.get("draft_ids") or 0) + 1
    data.setdefault("root_path", str(root))
    atomic_write(reg, _dump(data))
    return backup


def write_draft(tl: ClipTimeline, name: str, root: Path | None = None,
                running: Callable[[], bool] = capcut_running,
                backup_dir: Path | None = None, cover: bool = True,
                opener_src: str | Path | None = None,
                dissolve_ms: int = DISSOLVE_MS) -> dict[str, Any]:
    """Create one new CapCut project folder for `tl` and register it. Never overwrites."""
    root = Path(root) if root else drafts_root()
    if running():
        raise VPError("data", "CapCut is running. Quit CapCut (Cmd+Q) and run the export again: "
                              "CapCut rewrites its project list on exit and would drop the new draft.")
    if not root.is_dir():
        raise VPError("data", f"CapCut drafts folder not found: {root}. Is CapCut desktop installed "
                              "and opened at least once? (or set VP_CAPCUT_DRAFTS)")
    tpl = load_template()
    folder = unique_folder(root, safe_name(name))
    now_us = int(time.time() * 1_000_000)
    timeline_id, draft_id = _uid(), _uid()
    # CapCut is sandboxed: it reads ~/Movies (its drafts live there) and files the user picked by
    # hand, nothing else — a path into the job folder shows "File not accessible". So the cut is
    # copied into the project folder and the draft points at the copy.
    media_rel = Path(MEDIA_DIR) / f"{safe_name(tl.item)}_{tl.video_path.name}"
    opener_rel = Path(MEDIA_DIR) / f"{safe_name(tl.item)}_opener.mp4"
    src_tl = tl
    tl = dataclasses.replace(tl, video_path=folder / media_rel)
    project = build_project_json(tpl, timeline_id, now_us)
    opener = None

    stage = root / f".vp-staging-{uuid.uuid4().hex[:8]}"
    stage.mkdir()
    try:
        (stage / media_rel).parent.mkdir(parents=True)
        shutil.copy2(src_tl.video_path, stage / media_rel)
        sizes = [stage / media_rel]
        if opener_src:
            staged = prepare_opener(opener_src, src_tl, stage / opener_rel, dissolve_ms)
            opener = dataclasses.replace(staged, path=folder / opener_rel)
            sizes.append(stage / opener_rel)
        info = build_draft_info(tl, tpl, timeline_id, opener=opener)
        meta = build_meta(tl, tpl, folder.name, folder, draft_id, now_us, size_of=sizes,
                          opener=opener)
        tdir = stage / "Timelines" / timeline_id
        tdir.mkdir(parents=True)
        (stage / "draft_info.json").write_text(_dump(info), encoding="utf-8")
        (tdir / "draft_info.json").write_text(_dump(info), encoding="utf-8")
        (stage / "draft_meta_info.json").write_text(_dump(meta), encoding="utf-8")
        (stage / "Timelines" / "project.json").write_text(_dump(project), encoding="utf-8")
        if cover:
            _cover(dataclasses.replace(tl, video_path=stage / media_rel), stage / "draft_cover.jpg")
            if (stage / "draft_cover.jpg").exists():
                shutil.copy2(stage / "draft_cover.jpg", tdir / "draft_cover.jpg")
        if folder.exists():          # appeared meanwhile: never write into an existing folder
            raise VPError("data", f"folder appeared while exporting: {folder}")
        os.rename(stage, folder)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    backup = register(root, build_root_entry(tpl, meta),
                      backup_dir or vp_home() / "capcut" / "backups")
    return {"item": tl.item, "folder": str(folder), "name": folder.name,
            "cues": len(timeline_cues(tl, opener)), "duration_ms": total_ms(tl, opener),
            "canvas": f"{tl.canvas_w}x{tl.canvas_h}",
            "opener": None if not opener else {"duration_ms": opener.duration_ms,
                                               "dissolve_ms": opener.overlap_ms,
                                               "cut_starts_at_ms": opener.shift_ms,
                                               "notes": list(opener.notes)},
            "registry_backup": str(backup) if backup else None}


def export_job(job_root: str | Path, items: list[str] | None = None, root: Path | None = None,
               running: Callable[[], bool] = capcut_running,
               backup_dir: Path | None = None, opener: str | Path | None = None,
               dissolve_ms: int = DISSOLVE_MS) -> list[dict[str, Any]]:
    job_root = Path(job_root).expanduser().resolve()
    if opener and not Path(opener).expanduser().is_file():
        raise VPError("data", f"opener file not found: {opener}")
    available = clip_items(job_root)
    items = items or available
    missing = [i for i in items if i not in available]
    if missing:
        raise VPError("data", f"no finished clip (clips/<id>/cut.mp4) for: {', '.join(missing)}; "
                              f"available: {', '.join(available) or 'none'}")
    if running():
        raise VPError("data", "CapCut is running. Quit CapCut (Cmd+Q) and run the export again.")
    warning = version_warning(capcut_version(), load_template())
    out = []
    for item in items:
        tl = build_clip_timeline(job_root, item)
        name = f"{job_root.name} {item}" + (" opener" if opener else "")
        out.append(write_draft(tl, name, root=root, running=running, backup_dir=backup_dir,
                               opener_src=opener, dissolve_ms=dissolve_ms))
        out[-1]["warning"] = warning
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp capcut", description="CapCut desktop draft export")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="make a new CapCut project per finished clip")
    e.add_argument("job")
    e.add_argument("items", nargs="*", help="clip ids (default: all finished clips)")
    e.add_argument("--drafts", help="CapCut drafts folder (default: the CapCut desktop one)")
    e.add_argument("--opener", help="a title/opener video to put before the clip")
    e.add_argument("--dissolve", type=float, default=DISSOLVE_MS / 1000,
                   help="opener -> clip cross-dissolve, seconds (0 = hard cut; default %(default)s)")
    e.add_argument("--json", action="store_true")
    c = sub.add_parser("check", help="is CapCut running; where its drafts live")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        try:
            is_running: bool | None = capcut_running()
        except VPError:
            is_running = None
        res = {"running": is_running, "installed_version": capcut_version(),
               "drafts": str(drafts_root()),
               "drafts_exists": drafts_root().is_dir(),
               "format": load_template()["source"]}
        print(json.dumps(res, ensure_ascii=False) if a.json else
              f"CapCut running: {res['running']}\ndrafts: {res['drafts']} "
              f"({'found' if res['drafts_exists'] else 'missing'})\n"
              f"draft format: CapCut {res['format']['capcut_app_version']}; installed: "
              f"{res['installed_version'] or 'not found'}")
        return 0
    res = export_job(a.job, a.items or None, root=Path(a.drafts).expanduser() if a.drafts else None,
                     opener=a.opener, dissolve_ms=int(round(max(0.0, a.dissolve) * 1000)))
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        for r in res:
            print(f"CREATED {r['name']}\t{r['folder']}\t{r['cues']} subtitles\t{r['canvas']}")
            if r.get("warning"):
                print(f"  WARNING: {r['warning']}")
            if r.get("opener"):
                o = r["opener"]
                print(f"  opener: {o['duration_ms'] / 1000:g} s, the clip starts at "
                      f"{o['cut_starts_at_ms'] / 1000:g} s (subtitles moved with it)")
                for n in o["notes"]:
                    print(f"  - {n}")
        print("Open CapCut: the new projects are at the top of the project list.")
    return 0
