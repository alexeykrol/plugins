"""Shots of a video or a folder of videos (capability К, first half: code only).

One ffmpeg decode pass per video finds the hard cuts (`select='gt(scene,T)'` + showinfo), samples
brightness and frame difference (signalstats YAVG / YDIF at a few frames a second — YDIF is the
motion proxy) and the momentary loudness (ebur128). Cuts closer than `min_shot_ms` to the previous
cut are dropped (flashes, fast pans). Per shot a keyframe JPEG from the middle of the shot is
written next to shots.json.

What a shot SHOWS is judgment: `description` stays null here; Elsa looks at the keyframes and fills
it with `vp shots describe-set <job> <shot-id> "<text>"` (optionally a 1–5 rating and tags).
Re-running detection keeps annotations by matching the closest starts within `KEEP_MS` on the
same source, with each annotation used at most once.

Layout (job = a folder of videos, or a job folder with request.yaml):
  sources   request.yaml `source.videos` (a folder or a list of files), else <job>/footage/,
            else the video files in <job> itself
  output    <job>/analysis/shots.json, <job>/analysis/keyframes/<shot-id>.jpg

CLI:
  vp shots detect <job> [--threshold 0.3] [--min-shot 1.0] [--json]
  vp shots list <job> [--pending] [--json]
  vp shots describe-set <job> <shot-id> "<text>" [--rating 1-5] [--tags a,b]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from ..adapters import media
from ..core.errors import VPError
from ..core.io import read_json, read_yaml, write_json

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".mpg", ".mpeg")
THRESHOLD = 0.3          # scene score for a hard cut (0..1)
MIN_SHOT_MS = 1000       # a cut closer than this to the previous one is ignored
SAMPLE_FPS = 5           # brightness / motion samples per second
KEEP_MS = 150            # a re-detected shot keeps its description if its start moved less than this
ANALYSIS_DIR = "analysis"
SCHEMA = 1

_SHOWINFO = re.compile(r"Parsed_showinfo.*?pts_time:\s*([\d.]+)")
_META_FRAME = re.compile(r"Parsed_metadata.*?pts_time:\s*([\d.]+)")
_META_KEY = re.compile(r"Parsed_metadata.*?lavfi\.signalstats\.(YAVG|YDIF)=([\d.]+)")
_EBUR = re.compile(r"Parsed_ebur128.*?t:\s*([\d.]+).*?M:\s*(-?[\d.]+)")
_FFMPEG_ERROR = re.compile(r"^(?:\[[^\]]+\] )*\[(?:error|fatal|panic)\] ")


# ---- where things are ---------------------------------------------------------------------

def job_root(job: str | Path) -> Path:
    root = Path(job).expanduser().resolve()
    if not root.is_dir():
        raise VPError("data", f"not a folder: {root}")
    return root


def analysis_dir(job: str | Path) -> Path:
    return job_root(job) / ANALYSIS_DIR


def shots_path(job: str | Path) -> Path:
    return analysis_dir(job) / "shots.json"


def _videos_in(folder: Path) -> list[Path]:
    return sorted((p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in VIDEO_EXT and not p.name.startswith(".")),
                  key=lambda p: p.name.lower())


def source_videos(job: str | Path) -> list[Path]:
    root = job_root(job)
    req = read_yaml(root / "request.yaml", {}) or {}
    src = (req.get("source") or {}).get("videos") if isinstance(req.get("source"), dict) else None
    if src:
        items = src if isinstance(src, list) else [src]
        out: list[Path] = []
        for it in items:
            p = Path(str(it)).expanduser()
            p = p if p.is_absolute() else root / p
            out.extend(_videos_in(p) if p.is_dir() else [p])
        missing = [str(p) for p in out if not p.is_file()]
        if missing:
            raise VPError("data", "source.videos: not found: " + ", ".join(missing))
        return out
    footage = root / "footage"
    vids = _videos_in(footage) if footage.is_dir() else _videos_in(root)
    if not vids:
        raise VPError("data", f"no videos in {root} (put them in {footage} or set source.videos "
                              "in request.yaml)")
    return vids


# ---- pure parts (tested without ffmpeg) ----------------------------------------------------

def merge_cuts(cuts_ms: list[int], duration_ms: int, min_shot_ms: int = MIN_SHOT_MS) -> list[tuple[int, int]]:
    """Shot ranges [(start, end)] from cut times: a cut closer than `min_shot_ms` to the previous
    cut (or to the start) is dropped; a last shot shorter than that joins the one before."""
    bounds = [0]
    for c in sorted(set(int(c) for c in cuts_ms)):
        if c - bounds[-1] >= min_shot_ms and duration_ms - c > 0:
            bounds.append(c)
    if len(bounds) > 1 and duration_ms - bounds[-1] < min_shot_ms:
        bounds.pop()
    bounds.append(duration_ms)
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def parse_pass(stderr: str) -> dict[str, Any]:
    """Cuts (ms), signalstats samples [(ms, yavg, ydif)] and loudness samples [(ms, lufs)]
    from the stderr of the analysis pass."""
    cuts: list[int] = []
    samples: list[list[float]] = []
    loud: list[tuple[int, float]] = []
    for line in stderr.splitlines():
        if "Parsed_showinfo" in line:
            m = _SHOWINFO.search(line)
            if m:
                cuts.append(int(round(float(m.group(1)) * 1000)))
        elif "Parsed_metadata" in line:
            m = _META_FRAME.search(line)
            if m:
                samples.append([float(m.group(1)) * 1000, math.nan, math.nan])
                continue
            m = _META_KEY.search(line)
            if m and samples:
                samples[-1][1 if m.group(1) == "YAVG" else 2] = float(m.group(2))
        elif "Parsed_ebur128" in line:
            m = _EBUR.search(line)
            if m:
                loud.append((int(round(float(m.group(1)) * 1000)), float(m.group(2))))
    return {"cuts": cuts, "samples": [(int(t), y, d) for t, y, d in samples], "loudness": loud}


def shot_measures(start: int, end: int, samples: list[tuple[int, float, float]],
                  loud: list[tuple[int, float]], has_audio: bool) -> dict[str, Any]:
    """brightness 0..1 (mean luma), motion 0..1 (mean frame difference, the first sample after the
    cut excluded), loudness LUFS (energy mean of momentary loudness; None without audio/silence)."""
    inside = [s for s in samples if start <= s[0] < end]
    ys = [s[1] for s in inside if not math.isnan(s[1])]
    ds = [s[2] for s in inside[1:] if not math.isnan(s[2])]
    lv = [v for t, v in loud if start <= t - 200 < end and v > -70]   # M: 400 ms window ending at t
    lufs = None
    if has_audio and lv:
        lufs = round(10 * math.log10(sum(10 ** (v / 10) for v in lv) / len(lv)), 1)
    return {"brightness": round(sum(ys) / len(ys) / 255, 3) if ys else None,
            "motion": round(sum(ds) / len(ds) / 255, 4) if ds else None,
            "loudness_lufs": lufs}


# ---- ffmpeg --------------------------------------------------------------------------------

def _video_probe(path: Path, entries: str) -> dict[str, Any]:
    cmd = [media.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", entries,
           "-of", "json", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as e:
        raise VPError("internal", "ffprobe not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise VPError("data", f"video timing probe of {path.name} timed out") from e
    if proc.returncode or proc.stderr.strip():
        raise VPError("data", f"ffprobe failed on {path.name}: {proc.stderr.strip()[-300:]}")
    return json.loads(proc.stdout)


def _seconds(value: object) -> float | None:
    try:
        seconds = float(str(value))
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None


def _video_bounds_ms(path: Path) -> tuple[int, int]:
    """Video bounds on FFmpeg's input-relative timeline, in milliseconds."""
    timing = _video_probe(path, "stream=start_time,duration:format=start_time")
    streams = timing.get("streams") or []
    if not streams:
        raise VPError("data", f"{path.name}: no video stream")
    stream = streams[0]
    origin = _seconds(timing.get("format", {}).get("start_time")) or 0.0
    first = _seconds(stream.get("start_time"))
    duration = _seconds(stream.get("duration"))
    if first is not None and duration is not None and duration > 0:
        end = first + duration
    else:
        # Some containers omit stream duration. Read only the video packets to find its end.
        packets = _video_probe(path, "packet=pts_time,duration_time").get("packets", [])
        starts, ends = [], []
        for packet in packets:
            pts = _seconds(packet.get("pts_time"))
            if pts is not None:
                starts.append(pts)
                ends.append(pts + (_seconds(packet.get("duration_time")) or 0.0))
        if not starts:
            raise VPError("data", f"{path.name}: cannot determine video stream duration")
        first = min(starts)
        end = max(ends)
    # Include the last timestamp even when no packet duration was available.
    start_ms = max(0, math.ceil((first - origin) * 1000))
    end_ms = max(start_ms + 1, math.ceil((end - origin) * 1000))
    return start_ms, end_ms


def analyse_video(path: Path, threshold: float = THRESHOLD, timeout_s: float = 3600) -> dict[str, Any]:
    info = media.probe(path)
    if not info.get("width"):
        raise VPError("data", f"{path.name}: no video stream")
    graph = (f"[0:v:0]scale=320:-2,split[a][b];[a]select='gt(scene,{threshold})',showinfo[o1];"
             f"[b]fps={SAMPLE_FPS},scale=160:-2,signalstats,metadata=mode=print[o2]")
    outs = ["-map", "[o1]", "-f", "null", "-", "-map", "[o2]", "-f", "null", "-"]
    if info.get("has_audio"):
        graph += ";[0:a:0]ebur128=framelog=info[o3]"
        outs += ["-map", "[o3]", "-f", "null", "-"]
    cmd = [media.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "level+info", "-xerror",
           "-err_detect", "explode", "-i", str(path), "-filter_complex", graph, *outs]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError as e:
        raise VPError("internal", "ffmpeg not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise VPError("data", f"scene analysis of {path.name} timed out after {timeout_s}s") from e
    errors = [line for line in proc.stderr.splitlines() if _FFMPEG_ERROR.match(line)]
    if proc.returncode or errors:
        details = "\n".join(errors) if errors else proc.stderr
        raise VPError("data", f"scene analysis of {path.name} failed: {details.strip()[-400:]}")
    start_ms, end_ms = _video_bounds_ms(path)
    return {"info": {**info, "duration_ms": end_ms}, "video_start_ms": start_ms,
            **parse_pass(proc.stderr)}


def keyframe(src: Path, at_ms: int, out: Path) -> Path:
    return media._render_to(out, ["-ss", f"{at_ms / 1000:.3f}", "-i", str(src), "-map", "0:v:0",
                                  "-frames:v", "1",
                                  "-vf", "scale=-2:'min(720,ih)'", "-q:v", "3", "-update", "1",
                                  "-f", "image2"], timeout_s=120)


# ---- detect / describe ---------------------------------------------------------------------

def load_shots(job: str | Path) -> dict[str, Any]:
    data = read_json(shots_path(job), None)
    if not isinstance(data, dict) or not isinstance(data.get("shots"), list):
        raise VPError("data", f"no shots.json in {analysis_dir(job)}: run `vp shots detect` first")
    return data


def detect(job: str | Path, threshold: float = THRESHOLD, min_shot_ms: int = MIN_SHOT_MS,
           videos: list[Path] | None = None) -> dict[str, Any]:
    root = job_root(job)
    adir = analysis_dir(root)
    kdir = adir / "keyframes"
    old = read_json(shots_path(root), None) or {}
    kept: dict[str, dict[int, dict[str, Any]]] = {}
    for shot in old.get("shots") or []:
        if shot.get("source_path") and (shot.get("description") or shot.get("rating") or shot.get("tags")):
            kept.setdefault(shot["source_path"], {})[int(shot.get("start_ms", 0))] = shot
    sources, shots = [], []
    for vi, path in enumerate(videos or source_videos(root), 1):
        path = Path(path).resolve()
        sid = f"v{vi:02d}"
        a = analyse_video(path, threshold)
        info = a["info"]
        ranges = merge_cuts(a["cuts"], int(info["duration_ms"]), min_shot_ms)
        sources.append({"id": sid, "path": str(path), "duration_ms": info["duration_ms"],
                        "width": info["width"], "height": info["height"], "fps": info["fps"],
                        "has_audio": info["has_audio"], "cuts_found": len(a["cuts"])})
        for si, (s, e) in enumerate(ranges, 1):
            shot_id = f"{sid}-s{si:03d}"
            kf = kdir / f"{shot_id}.jpg"
            keyframe(path, (max(s, a["video_start_ms"]) + e) // 2, kf)
            rec = {"id": shot_id, "source": sid, "source_path": str(path), "start_ms": s,
                   "end_ms": e, "duration_ms": e - s, "keyframe": kf.relative_to(root).as_posix(),
                   "keyframes": [kf.relative_to(root).as_posix()],
                   **shot_measures(s, e, a["samples"], a["loudness"], bool(info["has_audio"])),
                   "has_audio": bool(info["has_audio"]),
                   "description": None, "rating": None, "tags": []}
            shots.append(rec)
    matches = []
    for index, shot in enumerate(shots):
        for start in kept.get(shot["source_path"], {}):
            distance = abs(start - shot["start_ms"])
            if distance <= KEEP_MS:
                matches.append((distance, index, start))
    matched = set()
    for _, index, start in sorted(matches):
        shot = shots[index]
        candidates = kept[shot["source_path"]]
        if index not in matched and start in candidates:
            prev = candidates.pop(start)
            shot.update(description=prev.get("description"), rating=prev.get("rating"),
                        tags=list(prev.get("tags") or []))
            matched.add(index)
    data = {"schema": SCHEMA, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "threshold": threshold, "min_shot_ms": min_shot_ms, "sources": sources, "shots": shots}
    write_json(shots_path(root), data, indent=1)
    return data


def describe_set(job: str | Path, shot_id: str, text: str | None = None, rating: int | None = None,
                 tags: list[str] | None = None) -> dict[str, Any]:
    data = load_shots(job)
    shot = next((s for s in data["shots"] if s["id"] == shot_id), None)
    if shot is None:
        raise VPError("data", f"no shot {shot_id!r}; shots: {data['shots'][0]['id'] if data['shots'] else 'none'}…")
    if text is not None:
        shot["description"] = text.strip() or None
    if rating is not None:
        if not 1 <= int(rating) <= 5:
            raise VPError("data", "rating must be 1..5")
        shot["rating"] = int(rating)
    if tags is not None:
        shot["tags"] = [t.strip() for t in tags if t.strip()]
    write_json(shots_path(job), data, indent=1)
    return shot


def pending(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in data["shots"] if not s.get("description")]


# ---- CLI -----------------------------------------------------------------------------------

def _fmt(ms: int) -> str:
    return f"{ms // 60000}:{ms // 1000 % 60:02d}.{ms % 1000 // 100}"


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return number


def _shot_milliseconds(value: str) -> int:
    milliseconds = _positive_float(value) * 1000
    if not math.isfinite(milliseconds):
        raise argparse.ArgumentTypeError("duration is too large")
    return max(1, round(milliseconds))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp shots", description="shots of a folder of videos")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect", help="find the shots, keyframes and measures -> analysis/shots.json")
    d.add_argument("job")
    d.add_argument("--threshold", type=_positive_float, default=THRESHOLD)
    d.add_argument("--min-shot", type=_shot_milliseconds, default=MIN_SHOT_MS, help="seconds")
    d.add_argument("--json", action="store_true")
    ls = sub.add_parser("list", help="shots with their keyframes")
    ls.add_argument("job")
    ls.add_argument("--pending", action="store_true", help="only shots without a description")
    ls.add_argument("--json", action="store_true")
    s = sub.add_parser("describe-set", help="write what a shot shows (after looking at its keyframe)")
    s.add_argument("job")
    s.add_argument("shot")
    s.add_argument("text", nargs="?")
    s.add_argument("--rating", type=int, choices=range(1, 6))
    s.add_argument("--tags")
    a = ap.parse_args(argv)
    if a.cmd == "detect":
        data = detect(a.job, a.threshold, a.min_shot)
        if a.json:
            print(json.dumps(data, ensure_ascii=False, indent=1))
        else:
            print(f"{len(data['shots'])} shots in {len(data['sources'])} video(s) -> "
                  f"{shots_path(a.job)}")
            print(f"next: look at the keyframes, then `vp shots describe-set {a.job} <shot> \"…\"`")
        return 0
    if a.cmd == "list":
        data = load_shots(a.job)
        root = job_root(a.job)
        if a.pending:
            shots = [{"id": x["id"], "keyframe": x["keyframe"],
                      "keyframe_path": str(root / x["keyframe"])} for x in pending(data)]
            if a.json:
                print(json.dumps(shots, ensure_ascii=False, indent=1))
            else:
                for x in shots:
                    print(f"{x['id']}\t{x['keyframe_path']}")
            return 0
        shots = data["shots"]
        if a.json:
            print(json.dumps([{**x, "keyframe_path": str(root / x["keyframe"])} for x in shots],
                             ensure_ascii=False, indent=1))
        else:
            for x in shots:
                print(f"{x['id']}\t{_fmt(x['start_ms'])}-{_fmt(x['end_ms'])}\t{root / x['keyframe']}"
                      f"\t{x.get('description') or '—'}")
        return 0
    shot = describe_set(a.job, a.shot, a.text, a.rating,
                        a.tags.split(",") if a.tags is not None else None)
    print(f"{shot['id']}: {shot.get('description') or '—'}"
          + (f" (rating {shot['rating']})" if shot.get("rating") else ""))
    return 0
