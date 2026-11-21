"""Music montage: edit plan from shots + beats, and its render (capabilities М and Б).

Planning is code only (bookkeeping): which shots, in what order, how long, where the cuts fall.
The judgment it uses was made before and is read from shots.json — Elsa's descriptions and the
person's ratings (`vp shots describe-set`).

Rules
  * The plan starts on the first downbeat of the music; every cut falls on a beat; a shot lasts a
    whole number of beats — normally `pace` bars (1 bar for 9:16, 2 for 16:9), one bar more for a
    shot rated 4–5, at most 4 bars; a shot shorter than a bar gets the whole beats it can hold.
  * The total is the target snapped to whole bars (never past the detected beats of the music);
    the segments sum to it exactly.
  * Shots are chosen by score (rating, then described, minus near-black, minus shorter than a bar)
    and placed in the order they were filmed. No shot is used twice unless the material runs out;
    then the reused shots follow, again in filming order, each from a different part of the shot
    when it has room (`reused: true`). A shot rated 1 is used only when nothing else is left.
  * Each segment is taken from the middle of its shot, away from the cut edges.

Plan (<job>/montage/plan.json) — the layer-3 edit plan (SPEC §0) of a montage:
  {schema, kind: "montage", canvas {w,h,fps,aspect}, target_ms, duration_ms, pace_bars,
   music {path, in_ms, duration_ms, tempo_bpm}, beats_ms (the grid used, from 0),
   segments [{n, shot, source_path, in_ms, out_ms, at_ms, duration_ms, beats, description, reused}],
   provenance {shots, beats}}

Render: the plan is rendered into a finished clip <job>/clips/<item>/ (cut.mp4 with the music,
an empty clip.srt, meta.yaml with the kind, cut.qa.yaml from the picture gate) — the same clip
folder S4 makes, so the existing editor exports take it unchanged:
  vp capcut export <job> <item>      vp resolve export <job> <item>
In those projects the montage is one video (cuts baked in); the cut list is plan.json.

CLI:
  vp montage plan <job> --music FILE --duration S [--aspect 9:16|16:9|1:1] [--pace BARS]
                  [--render] [--item ID] [--json]
  vp montage render <job> [--item ID] [--canvas WxH]
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ..adapters import media, picture
from ..core.errors import VPError
from ..core.io import read_json, write_json, write_yaml
from ..core.paths import Job
from . import beats as beats_mod
from . import render_plan
from .scenes import analysis_dir, job_root, load_shots

ASPECTS = {"9:16": (1080, 1920, "reel"), "16:9": (1920, 1080, "long"), "1:1": (1080, 1080, "long")}
FPS = 30.0
EDGE_MS = 150            # keep away from the shot's own cut edges
MAX_BARS = 4


# ---- scoring -------------------------------------------------------------------------------

def score(shot: dict[str, Any], bar_ms: float) -> float:
    r = shot.get("rating")
    s = float(r) if r else (3.0 if shot.get("description") else 2.0)
    b = shot.get("brightness")
    if b is not None and b < 0.06:
        s -= 1.5
    m = shot.get("motion")
    if m is not None and 0.003 <= m <= 0.2:
        s += 0.3
    if usable_ms(shot) < bar_ms:
        s -= 1.0
    return s


def usable_ms(shot: dict[str, Any]) -> int:
    d = int(shot["end_ms"]) - int(shot["start_ms"])
    return d - 2 * min(EDGE_MS, d // 6)


def pref_bars(shot: dict[str, Any], pace: int) -> int:
    return min(MAX_BARS, pace + (1 if (shot.get("rating") or 0) >= 4 else 0))


# ---- grid ----------------------------------------------------------------------------------

def grid(beats: dict[str, Any]) -> tuple[list[int], int, int]:
    """(beat times, index of the first downbeat, meter)."""
    bms = [int(b) for b in beats.get("beats_ms") or []]
    if len(bms) < 2:
        raise VPError("data", "music has fewer than 2 beats")
    meter = int(beats.get("meter") or 4)
    down = set(int(d) for d in beats.get("downbeats_ms") or [])
    s0 = next((i for i, b in enumerate(bms) if b in down), 0)
    return bms, s0, meter


def end_index(bms: list[int], s0: int, meter: int, target_ms: int) -> int:
    """Index of the last beat: the target snapped to whole bars (whole beats if under a bar),
    within the detected beats."""
    last = len(bms) - 1
    if last - s0 < 1:
        raise VPError("data", "no beats after the first downbeat")
    beat_ms = (bms[last] - bms[s0]) / (last - s0)
    bars = [s0 + meter * k for k in range(1, (last - s0) // meter + 1)]
    cands = bars if bars and target_ms >= beat_ms * meter else list(range(s0 + 1, last + 1))
    return min(cands, key=lambda e: (abs(bms[e] - bms[s0] - target_ms), e))


# ---- planning ------------------------------------------------------------------------------

def _fit(bms: list[int], i: int, avail_ms: int, want_beats: int, meter: int, left: int) -> int:
    """Beats a shot with `avail_ms` usable gets at grid index i: `want_beats` if it fits, else the
    longest whole bars, else the longest whole beats; never more than `left`."""
    def dur(n: int) -> int:
        return bms[i + n] - bms[i]
    want = min(want_beats, left)
    if want and dur(want) <= avail_ms:
        return want
    n = (min(want_beats, left) // meter) * meter
    while n >= meter:
        if dur(n) <= avail_ms:
            return n
        n -= meter
    n = min(meter - 1, left)
    while n >= 1:
        if dur(n) <= avail_ms:
            return n
        n -= 1
    return 0


def _order_key(shot: dict[str, Any], order: dict[str, int]) -> tuple:
    return (order.get(shot.get("source"), 0), int(shot["start_ms"]))


def layout(picks: list[tuple[dict[str, Any], int]], bms: list[int], s0: int, e: int, meter: int,
           pace: int, bonus: dict[str, int] | None = None, *, pack: bool = False) -> list[dict[str, Any]]:
    """Segments for `picks` [(shot, round)] in the given order; round 0 = first use, round k = k-th
    reuse. `bonus` = extra bars per shot id. `pack` fills as many whole beats as each shot can hold."""
    bonus = bonus or {}
    segs: list[dict[str, Any]] = []
    i = s0
    for shot, rnd in picks:
        left = e - i
        if left <= 0:
            break
        bars = MAX_BARS if pack else min(MAX_BARS, pref_bars(shot, pace) + bonus.get(shot["id"], 0))
        n = _fit(bms, i, usable_ms(shot), bars * meter, 1 if pack else meter, left)
        if n == 0:
            continue
        d = bms[i + n] - bms[i]
        s, en = int(shot["start_ms"]), int(shot["end_ms"])
        edge = min(EDGE_MS, (en - s) // 6)
        lo, hi = s + edge, en - edge - d
        mid = (s + en) // 2 - d // 2
        if rnd:         # a reuse takes another part of the shot when there is room: end, start, …
            mid = (hi, lo)[rnd % 2] if hi - mid >= mid - lo else (lo, hi)[rnd % 2]
        start = max(lo, min(hi, mid))
        segs.append({"n": len(segs) + 1, "shot": shot["id"], "source_path": shot["source_path"],
                     "in_ms": start, "out_ms": start + d, "at_ms": bms[i] - bms[s0],
                     "duration_ms": d, "beats": n, "description": shot.get("description"),
                     "reused": rnd > 0})
        i += n
    return segs


def plan(shots_data: dict[str, Any], beats: dict[str, Any], target_ms: int, aspect: str = "9:16",
         pace: int | None = None, music_path: str | None = None) -> dict[str, Any]:
    if not math.isfinite(target_ms) or target_ms <= 0:
        raise VPError("data", "montage target must be finite and positive")
    if aspect not in ASPECTS:
        raise VPError("data", f"aspect must be one of {', '.join(ASPECTS)}")
    cw, ch, kind = ASPECTS[aspect]
    pace = (1 if kind == "reel" else 2) if pace is None else pace
    if pace not in range(1, MAX_BARS + 1):
        raise VPError("data", f"pace must be 1..{MAX_BARS} bars")
    pace = int(pace)
    bms, s0, meter = grid(beats)
    e = end_index(bms, s0, meter, int(target_ms))
    need = e - s0
    bar_ms = (bms[e] - bms[s0]) / max(1, need) * meter
    order = {s["id"]: k for k, s in enumerate(shots_data.get("sources") or [])}
    shots = [s for s in shots_data["shots"] if usable_ms(s) > 0]
    if not shots:
        raise VPError("data", "no usable shots")
    ranked = sorted(shots, key=lambda s: (-score(s, bar_ms), _order_key(s, order)))
    queue = [(s, 0) for s in ranked if s.get("rating") != 1]
    fallback = [(s, 0) for s in ranked if s.get("rating") == 1]
    picks: list[tuple[dict[str, Any], int]] = []
    bonus: dict[str, int] = {}

    def run(*, pack: bool = False) -> list[dict[str, Any]]:
        return layout(sorted(picks, key=lambda p: (p[1], _order_key(p[0], order))), bms, s0, e,
                      meter, pace, bonus, pack=pack)

    def filled(segs: list[dict[str, Any]]) -> bool:
        return sum(x["beats"] for x in segs) >= need

    segs = run()
    lengthened = False
    rnd = 0
    while not filled(segs):
        if queue:
            picks.append(queue.pop(0))
        elif not lengthened:
            # material ran out: give the best shots more bars (up to MAX_BARS) before reusing any
            lengthened = True
            for _ in range(MAX_BARS):
                for shot, _r in sorted(picks, key=lambda p: ranked.index(p[0])):
                    if filled(segs):
                        break
                    if pref_bars(shot, pace) + bonus.get(shot["id"], 0) < MAX_BARS:
                        bonus[shot["id"]] = bonus.get(shot["id"], 0) + 1
                        new = run()
                        if sum(x["beats"] for x in new) > sum(x["beats"] for x in segs):
                            segs = new
                        else:
                            bonus[shot["id"]] -= 1
                if filled(segs):
                    break
            continue
        elif fallback:
            # Whole bars can leave gaps even when the preferred footage fits in whole beats.
            packed = run(pack=True)
            if filled(packed):
                segs = packed
                break
            queue, fallback = fallback, []
            lengthened = False
            continue
        else:
            rnd += 1
            if rnd > 50:
                raise VPError("data", "not enough material to fill the montage")
            queue = [(s, rnd) for s in ranked]
            picks.append(queue.pop(0))
        segs = run()
    total = bms[e] - bms[s0]
    assert sum(x["duration_ms"] for x in segs) == total
    return {"schema": 1, "kind": "montage", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "canvas": {"w": cw, "h": ch, "fps": FPS, "aspect": aspect, "kind": kind},
            "target_ms": int(target_ms), "duration_ms": total, "pace_bars": pace, "meter": meter,
            "music": {"path": music_path or beats.get("source"), "in_ms": bms[s0],
                      "duration_ms": total, "tempo_bpm": beats.get("tempo_bpm")},
            "beats_ms": [b - bms[s0] for b in bms[s0:e + 1]],
            "segments": segs, "shots_used": len({x["shot"] for x in segs}),
            "reused": sum(1 for x in segs if x["reused"])}


# ---- job level -----------------------------------------------------------------------------

def plan_path(job: str | Path) -> Path:
    return job_root(job) / "montage" / "plan.json"


def beats_for(job: str | Path, music: str | Path) -> dict[str, Any]:
    """beats.json of the music, cached in <job>/analysis/beats.json by path + size + mtime."""
    m = Path(music).expanduser().resolve()
    if not m.is_file():
        raise VPError("data", f"music file not found: {m}")
    st = m.stat()
    key = {"path": str(m), "size": st.st_size, "mtime": int(st.st_mtime)}
    cache = analysis_dir(job) / "beats.json"
    old = read_json(cache, None)
    if isinstance(old, dict) and old.get("key") == key:
        return old
    res = {**beats_mod.analyse(m), "key": key}
    write_json(cache, res, indent=1)
    return res


def make_plan(job: str | Path, music: str | Path, duration_s: float, aspect: str = "9:16",
              pace: int | None = None) -> dict[str, Any]:
    target_ms = duration_s * 1000
    if not math.isfinite(target_ms) or target_ms <= 0:
        raise VPError("data", "--duration must be finite and positive")
    if pace is not None and pace not in range(1, MAX_BARS + 1):
        raise VPError("data", f"pace must be 1..{MAX_BARS} bars")
    shots = load_shots(job)
    b = beats_for(job, music)
    p = plan(shots, b, max(1, round(target_ms)), aspect, pace, music_path=b["source"])
    p["provenance"] = {"shots": "analysis/shots.json", "beats": "analysis/beats.json"}
    write_json(plan_path(job), p, indent=1)
    return p


def _next_item(root: Path) -> str:
    n = 1
    while (root / "clips" / f"m{n:02d}").exists():
        n += 1
    return f"m{n:02d}"


def render(job: str | Path, item: str | None = None, canvas: tuple[int, int] | None = None,
           title: str | None = None) -> dict[str, Any]:
    root = job_root(job)
    p = read_json(plan_path(root), None)
    if not isinstance(p, dict) or not p.get("segments"):
        raise VPError("data", f"no montage plan in {plan_path(root)}: run `vp montage plan` first")
    item = _next_item(root) if item is None else item
    try:
        Job(root).item_dir(item)
    except ValueError as exc:
        raise VPError("data", str(exc)) from exc
    cdir = root / "clips" / item
    exists_msg = f"clip {item} already exists ({cdir}); clips are never overwritten"
    if cdir.exists() or cdir.is_symlink():
        raise VPError("data", exists_msg)
    c = p["canvas"]
    w, h = canvas or (int(c["w"]), int(c["h"]))
    if w < 2 or h < 2:
        raise VPError("data", "canvas width and height must each be at least 2 pixels")
    kind = c.get("kind") or ("reel" if h > w else "long")
    cdir.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".montage-", dir=cdir.parent) as tmp:
        stage = Path(tmp) / "clip"
        stage.mkdir()
        out = stage / "cut.mp4"
        render_plan.render(p["segments"], out, (w, h, float(c.get("fps") or FPS)),
                           music=p.get("music"))
        (stage / "clip.srt").write_text("", encoding="utf-8")
        write_yaml(stage / "meta.yaml", {"kind": kind, "title": title or f"{root.name} {item}",
                                         "source": "montage", "plan": "montage/plan.json"})
        report = picture.inspect(out, kind, expect_size=(w - w % 2, h - h % 2))
        write_yaml(stage / "cut.qa.yaml", report)
        write_json(stage / "plan.json", p, indent=1)
        info = media.probe(out)
        result = {"item": item, "folder": str(cdir), "duration_ms": info["duration_ms"],
                  "planned_ms": p["duration_ms"], "picture_ok": report["ok"],
                  "failures": report["failures"]}
        try:
            stage.rename(cdir)
        except OSError as exc:
            if cdir.exists() or cdir.is_symlink():
                raise VPError("data", exists_msg) from exc
            raise
    return result


# ---- CLI -----------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp montage", description="music montage from shots + beats")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="edit plan -> <job>/montage/plan.json")
    pl.add_argument("job")
    pl.add_argument("--music", required=True)
    pl.add_argument("--duration", type=float, required=True, help="target length, seconds")
    pl.add_argument("--aspect", default="9:16", choices=list(ASPECTS))
    pl.add_argument("--pace", type=int, help="bars per shot (default 1 for 9:16, 2 otherwise)")
    pl.add_argument("--render", action="store_true", help="render the plan into a clip right away")
    pl.add_argument("--item")
    pl.add_argument("--canvas", help="with --render: WxH override, e.g. 540x960 for a preview")
    pl.add_argument("--json", action="store_true")
    r = sub.add_parser("render", help="render the plan into <job>/clips/<item>/")
    r.add_argument("job")
    r.add_argument("--item")
    r.add_argument("--canvas", help="WxH override, e.g. 540x960 for a quick preview")
    r.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    out: dict[str, Any] = {}
    if a.cmd == "plan":
        p = make_plan(a.job, a.music, a.duration, a.aspect, a.pace)
        out["plan"] = p
        if not a.json:
            print(f"plan: {len(p['segments'])} cuts on the beat, {p['shots_used']} shots"
                  + (f" ({p['reused']} reused)" if p["reused"] else "")
                  + f", {p['duration_ms'] / 1000:.2f} s at {p['music']['tempo_bpm']} BPM "
                  f"(target {p['target_ms'] / 1000:.1f} s) -> {plan_path(a.job)}")
        if not a.render:
            if a.json:
                print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0
    canvas = None
    if a.canvas is not None:
        cw, _, chh = a.canvas.lower().partition("x")
        try:
            canvas = (int(cw), int(chh))
        except ValueError as exc:
            raise VPError("data", "--canvas must be WIDTHxHEIGHT in pixels") from exc
    res = render(a.job, a.item, canvas)
    out["clip"] = res
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"clip {res['item']}: {res['duration_ms'] / 1000:.2f} s -> {res['folder']}"
              + ("" if res["picture_ok"] else "  PICTURE CHECK FAILED: " + "; ".join(res["failures"])))
        print(f"editors: vp capcut export {a.job} {res['item']}  |  vp resolve export {a.job} {res['item']}")
    return 0 if res["picture_ok"] else 2
