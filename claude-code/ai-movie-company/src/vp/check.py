"""`vp check picture <video>` (K22): self-check of any rendered file before the person sees it.

Code part of the rule "look before you show": one decode pass with blackdetect + freezedetect
(vp.adapters.picture.scan_av, the same scan the render gate uses) and a contact sheet of evenly
spaced frames (vp.steps.s5v_visual.build_sheet, the S5v sheet) that the director then LOOKS AT —
the probe's defect (3 s of a near-empty screen at the start of a scene) was caught on the sheet,
not by a detector. Pass/fail is code: a black stretch of black_min_s (1 s) or longer, or a frozen
stretch of --max-freeze seconds or longer, fails. Stretches that end at the very end of the file
and start within the last --tail seconds (the closing fade / hold) are reported, not failed.

CLI:  vp check picture <video> [--sheet PNG] [--frames N] [--max-freeze SEC] [--tail SEC] [--json]
Exit: 0 pass, 2 fail (or unreadable file).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from .core.errors import VPError

FRAMES = 12
MAX_FREEZE_S = 3.0
TAIL_S = 1.5


def classify(intervals: list[tuple[float, float]], total_s: float, limit_s: float,
             tail_s: float) -> dict[str, Any]:
    """Split intervals (seconds) into failing and tail-only ones (pure)."""
    fail, tail = [], []
    for a, b in intervals:
        long_enough = (b - a) >= limit_s
        at_tail = b >= total_s - 0.05 and a >= total_s - tail_s
        if long_enough and not at_tail:
            fail.append((round(a, 2), round(b, 2)))
        elif at_tail:
            tail.append((round(a, 2), round(b, 2)))
    return {"fail": fail, "tail": tail}


def check_picture(video: str | Path, sheet: str | Path | None = None, frames: int = FRAMES,
                  max_freeze_s: float = MAX_FREEZE_S, tail_s: float = TAIL_S,
                  scan: Callable[..., dict] | None = None,
                  make_sheet: Callable[..., Path] | None = None) -> dict[str, Any]:
    from .adapters import media, picture
    from .steps.s5v_visual import build_sheet
    video = Path(video).expanduser().resolve()
    info = media.probe(video)
    if not info.get("width"):
        raise VPError("data", f"{video.name} has no picture (no video stream)")
    total_ms = int(info["duration_ms"])
    total_s = total_ms / 1000
    st = picture.settings(None)
    st["freeze_min_s"] = min(float(st["freeze_min_s"]), max(0.5, float(max_freeze_s)))
    av = (scan or picture.scan_av)(video, 0, total_ms, st, False)
    black = classify(av["black"], total_s, float(st["black_min_s"]), tail_s)
    frozen = classify(av["frozen"], total_s, max_freeze_s, tail_s)
    sheet_path = Path(sheet).expanduser().resolve() if sheet else \
        video.with_name(f"{video.stem}.sheet.png")
    n = max(1, min(int(frames), 30))
    (make_sheet or build_sheet)(video, 0, total_ms, n, sheet_path)
    failures = [f"black picture {a:g}–{b:g} s" for a, b in black["fail"]] + \
               [f"frozen picture {a:g}–{b:g} s" for a, b in frozen["fail"]]
    return {"ok": not failures, "video": str(video), "size": [info["width"], info["height"]],
            "duration_s": round(total_s, 2), "fps": info.get("fps"),
            "black": [list(x) for x in av["black"]], "frozen": [list(x) for x in av["frozen"]],
            "tail_only": {"black": black["tail"], "frozen": frozen["tail"]},
            "failures": failures, "sheet": str(sheet_path), "sheet_frames": n,
            "look_at_sheet": "open the sheet and look for empty, cut-off or broken frames before "
                             "showing the video"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp check", description="self-checks of rendered files")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("picture", help="black/frozen picture + a contact sheet of frames")
    p.add_argument("video")
    p.add_argument("--sheet", help="where to write the sheet PNG (default <video>.sheet.png)")
    p.add_argument("--frames", type=int, default=FRAMES)
    p.add_argument("--max-freeze", type=float, default=MAX_FREEZE_S)
    p.add_argument("--tail", type=float, default=TAIL_S)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        res = check_picture(a.video, a.sheet, a.frames, a.max_freeze, a.tail)
    except VPError as e:
        if a.json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"FAIL {e}")
        return 2
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(f"{'PASS' if res['ok'] else 'FAIL'} {res['video']} ({res['size'][0]}x{res['size'][1]}, "
              f"{res['duration_s']:g} s)")
        for f in res["failures"]:
            print(f"  - {f}")
        for kind, iv in res["tail_only"].items():
            for a_, b_ in iv:
                print(f"  note: {kind} {a_:g}–{b_:g} s at the very end (closing fade/hold, not failed)")
        print(f"sheet: {res['sheet']} ({res['sheet_frames']} frames) — look at it before showing")
    return 0 if res["ok"] else 2
