"""Picture: where the real image is in a frame, and the code gate on what a human will see.

Invariant (docs/BUILD-v1.md §Picture): no clip ships with black bars, and every claim about the
picture is backed by a check. The pipeline works from transcripts; this module is what lets it
know the picture without a human looking first.

    active_area(path, start_ms, end_ms, policy="union") -> Rect
        cropdetect at several sampled points of [start, end). "union" (the widest rectangle seen,
        used for CROPPING: dark scenes never make us cut picture away) or "median" (each edge's
        median, used to report partial bars in horizontal videos).
    reel_crop_rect(frame_w, frame_h, area, x_frac, w_frac) -> (Rect, out_w, out_h)
        the largest 9:16 rectangle inside the active area, centred on the profile's band, and
        the output size (full source height, so a reel is never letterboxed or pillarboxed).
    bars(frame_w, frame_h, area) -> {side: px}   bars thicker than the noise threshold.
    inspect(path, kind, span=None, config=None, expect_size=None) -> report dict
        code gate: frame size / aspect, black bars (reel: in most of the frames sampled twice per
        second; horizontal: in every seek sample), black stretches, frozen picture, silence,
        audio present.
    require_clean(report, what, waive=()) -> raises VPError("data") naming every failed check.

Thresholds come from config `picture_qa` (vp.core.config DEFAULTS). `waive` lets the owner accept
a named check for one clip (clips/<item>/picture.accept.yaml) — a visible decision, never silent.
"""
from __future__ import annotations

import os
import re
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from ..core.errors import VPError
from . import media

REEL_ASPECT = 9 / 16

DEFAULTS: dict[str, Any] = {
    "samples": 7,                  # cropdetect sample points per clip
    "sample_frames": 10,           # frames read at each point
    "crop_limit": 24,              # cropdetect black threshold, 0..255
    "bar_min_frac": 0.01,          # bars thinner than this share of the side (min 4 px) are noise
    "bar_max_luma": 20,            # a bar is uniformly black: 90th-percentile luma of the strip at
                                   # most this (bars measure 0 or 16); a dark wall or a black
                                   # terminal with text is darker on average but not uniform
    "bar_confirm_points": 3,       # sample points where a candidate bar strip is measured
    "aspect_tolerance": 0.01,      # reel width/height vs 9/16
    "black_min_s": 1.0,            # a black stretch this long fails the clip
    "black_pixel_threshold": 0.03, # blackdetect pix_th: limited-range luma <= ~22; dark IDE themes
                                   # (#0d1117, #1e1e1e: luma 27..42) are not black
    "freeze_noise_db": -60,
    "freeze_min_s": 2.0,
    # Calibrated 2026-11-18 on 6 real lectures/Zoom recordings + the incident source (56 cuts):
    # frozen share reached 0.90 on a screen-share reel and 0.70 on a long; pauses in lectures
    # reached 26 s. So frozen fails only a reel frozen almost entirely (a broken render), silence
    # fails only dead air in a reel; long/full report both without failing.
    # share of the clip with a frozen picture that fails it; None = reported, not failing (a long
    # video or the full recording may be a static screen share for minutes)
    "frozen_share_max": {"reel": 0.99, "long": None, "full": None},
    "silence_db": -40.0,
    "silence_max_s": {"reel": 10.0, "long": None, "full": None},  # None = reported only
}


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """DEFAULTS deep-merged with config `picture_qa`."""
    out = {**DEFAULTS, "silence_max_s": dict(DEFAULTS["silence_max_s"]),
           "frozen_share_max": dict(DEFAULTS["frozen_share_max"])}
    user = (config or {}).get("picture_qa") or {}
    for k, v in user.items():
        if k in ("silence_max_s", "frozen_share_max") and isinstance(v, dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def _even(v: float) -> int:
    n = int(v)
    return n - n % 2


_CROP_RE = re.compile(r"crop=(-?\d+):(-?\d+):(-?\d+):(-?\d+)")


def _crop_rect(values: tuple[str, str, str, str], W: int, H: int) -> Rect | None:
    w, h, x, y = map(int, values)
    if w > 0 and h > 0 and x >= 0 and y >= 0 and x + w <= W and y + h <= H:
        return Rect(x, y, w, h)
    return None


def _sample_points(start_ms: int, end_ms: int, n: int) -> list[int]:
    span = max(end_ms - start_ms, 1)
    return [int(start_ms + span * (i + 0.5) / n) for i in range(n)]


def crop_samples(path: str | os.PathLike, start_ms: int = 0, end_ms: int | None = None,
                 st: dict[str, Any] | None = None) -> list[Rect]:
    """Valid cropdetect rectangles at sampled points (all-black samples are dropped)."""
    st = st or DEFAULTS
    info = media.probe(path)
    W, H = info["width"], info["height"]
    end_ms = info["duration_ms"] if end_ms is None else min(end_ms, info["duration_ms"])
    rects = []
    for t in _sample_points(start_ms, end_ms, int(st["samples"])):
        proc = media.run_ffmpeg(["-ss", f"{t / 1000:.3f}", "-i", str(path), "-an",
                                 "-frames:v", str(int(st["sample_frames"])),
                                 "-vf", f"cropdetect=limit={st['crop_limit']}:round=2:reset=0",
                                 "-f", "null", "-"])
        found = _CROP_RE.findall(proc.stderr)
        if not found:
            continue
        rect = _crop_rect(found[-1], W, H)
        if rect is not None:
            rects.append(rect)
    return rects


def combine(rects: Iterable[Rect], frame_w: int, frame_h: int, policy: str) -> Rect | None:
    """Combined rectangle, or None when nothing was measured (unmeasured is not "no bars")."""
    rects = list(rects)
    if not rects:
        return None
    x1s, y1s = [r.x for r in rects], [r.y for r in rects]
    x2s, y2s = [r.x + r.w for r in rects], [r.y + r.h for r in rects]
    if policy == "union":
        x1, y1, x2, y2 = min(x1s), min(y1s), max(x2s), max(y2s)
    elif policy == "median":
        x1, y1 = int(statistics.median_low(x1s)), int(statistics.median_low(y1s))
        x2, y2 = int(statistics.median_high(x2s)), int(statistics.median_high(y2s))
    else:
        raise ValueError(policy)
    return Rect(x1, y1, x2 - x1, y2 - y1)


def snap(area: Rect, frame_w: int, frame_h: int, st: dict[str, Any] | None = None) -> Rect:
    """Edges closer to the frame edge than the noise threshold are moved onto it."""
    b = bars(frame_w, frame_h, area, st)
    x1 = area.x if "left" in b else 0
    y1 = area.y if "top" in b else 0
    x2 = area.x + area.w if "right" in b else frame_w
    y2 = area.y + area.h if "bottom" in b else frame_h
    return Rect(x1, y1, x2 - x1, y2 - y1)


def bars(frame_w: int, frame_h: int, area: Rect, st: dict[str, Any] | None = None) -> dict[str, int]:
    """{side: thickness px} of bars above the noise threshold."""
    frac = float((st or DEFAULTS)["bar_min_frac"])
    tw, th = max(4, round(frame_w * frac)), max(4, round(frame_h * frac))
    sides = {"top": area.y, "bottom": frame_h - area.y - area.h,
             "left": area.x, "right": frame_w - area.x - area.w}
    return {s: v for s, v in sides.items() if v > (th if s in ("top", "bottom") else tw)}


_YHIGH_RE = re.compile(r"lavfi\.signalstats\.YHIGH=([\d.]+)")


def _strip(side: str, area: Rect, W: int, H: int) -> Rect:
    return {"top": Rect(0, 0, W, area.y), "bottom": Rect(0, area.y + area.h, W, H - area.y - area.h),
            "left": Rect(0, 0, area.x, H), "right": Rect(area.x + area.w, 0, W - area.x - area.w, H)}[side]


def strip_luma(path: str | os.PathLike, t_ms: int, r: Rect) -> float | None:
    """90th-percentile luma of rectangle `r` in the frame at t_ms (None if unreadable)."""
    proc = media.run_ffmpeg(["-ss", f"{t_ms / 1000:.3f}", "-i", str(path), "-an", "-frames:v", "1",
                             "-vf", f"crop={r.w}:{r.h}:{r.x}:{r.y},signalstats,metadata=print",
                             "-f", "null", "-"])
    found = _YHIGH_RE.findall(proc.stderr or "")
    return float(found[-1]) if found else None


def confirm_bars(path: str | os.PathLike, start_ms: int, end_ms: int, W: int, H: int, area: Rect,
                 st: dict[str, Any]) -> dict[str, int]:
    """Bars of `area` that are really bars: the strip is uniformly black (YHIGH <= bar_max_luma)
    at most of the measured points. Dark content at the edge (a dark wall, a black terminal) reads
    as bars to cropdetect's average, never to this check."""
    cand = bars(W, H, area, st)
    out = {}
    times = _sample_points(start_ms, end_ms, int(st["bar_confirm_points"]))
    for side, px in cand.items():
        lumas = [strip_luma(path, t, _strip(side, area, W, H)) for t in times]
        lumas = [v for v in lumas if v is not None]
        if lumas and sum(v <= float(st["bar_max_luma"]) for v in lumas) * 2 > len(lumas):
            out[side] = px
    return out


def keep_sides(area: Rect, W: int, H: int, sides: Iterable[str]) -> Rect:
    """`area` with the edges NOT in `sides` moved onto the frame edge."""
    sides = set(sides)
    x1 = area.x if "left" in sides else 0
    y1 = area.y if "top" in sides else 0
    x2 = area.x + area.w if "right" in sides else W
    y2 = area.y + area.h if "bottom" in sides else H
    return Rect(x1, y1, x2 - x1, y2 - y1)


def measure(path: str | os.PathLike, start_ms: int = 0, end_ms: int | None = None,
            policy: str = "union", st: dict[str, Any] | None = None) -> tuple[Rect | None, int]:
    """(confirmed active area or None if nothing could be measured, number of measured samples)."""
    st = st or DEFAULTS
    info = media.probe(path)
    W, H = info["width"], info["height"]
    end_ms = info["duration_ms"] if end_ms is None else min(end_ms, info["duration_ms"])
    samples = crop_samples(path, start_ms, end_ms, st)
    area = combine(samples, W, H, policy)
    if area is None:
        return None, 0
    area = snap(area, W, H, st)
    real = confirm_bars(path, start_ms, end_ms, W, H, area, st) if bars(W, H, area, st) else {}
    return keep_sides(area, W, H, real), len(samples)


_REEL_BAR_FPS = "fps=2:start_time=0:eof_action=pass"
_REEL_BAR_MAX_LUMA_SPREAD = 4  # central 80% of the strip, allowing slight encoding noise
_REEL_STRIP_LUMA_RE = re.compile(
    r"\[metadata@(top|bottom|left|right) @ [^\]]+\] lavfi\.signalstats\.(YLOW|YHIGH)=(\d+)"
)


def _reel_area(path: str | os.PathLike, start_ms: int, end_ms: int, W: int, H: int,
               st: dict[str, Any]) -> tuple[Rect | None, int]:
    # Limit input before sampling so the FPS filter cannot draw frames from the outro.
    args = ["-ss", f"{start_ms / 1000:.3f}", "-t", f"{(end_ms - start_ms) / 1000:.3f}",
            "-i", str(path), "-an"]
    log = media.run_ffmpeg([
        *args, "-vf", f"{_REEL_BAR_FPS},cropdetect=limit={st['crop_limit']}:round=2:skip=0:reset=1",
        "-f", "null", "-",
    ]).stderr
    frames = [_crop_rect(values, W, H) for values in _CROP_RE.findall(log)]
    measured = [rect for rect in frames if rect is not None]
    area = combine(measured, W, H, "median")
    if area is None:
        return None, 0
    candidates = bars(W, H, area, st)
    if not candidates:
        return Rect(0, 0, W, H), len(measured)

    # Confirm each candidate on the same timeline, in a single decode for all strips.
    graph = []
    if len(candidates) > 1:
        inputs = "".join(f"[in_{side}]" for side in candidates)
        graph.append(f"[0:v]{_REEL_BAR_FPS},split={len(candidates)}{inputs}")
    for side in candidates:
        r = _strip(side, area, W, H)
        source = f"[in_{side}]" if len(candidates) > 1 else f"[0:v]{_REEL_BAR_FPS},"
        graph.append(f"{source}crop={r.w}:{r.h}:{r.x}:{r.y}:exact=1,"
                     f"signalstats,metadata@{side}=print[out_{side}]")
    maps = [arg for side in candidates for arg in ("-map", f"[out_{side}]")]
    log = media.run_ffmpeg([
        *args, "-filter_complex", ";".join(graph), *maps, "-f", "null", "-",
    ]).stderr
    lumas: dict[str, dict[str, list[int]]] = {
        side: {"YLOW": [], "YHIGH": []} for side in candidates
    }
    for side, key, value in _REEL_STRIP_LUMA_RE.findall(log):
        lumas[side][key].append(int(value))
    frame_bars = [bars(W, H, rect, st) if rect is not None else {} for rect in frames]
    confirmed = []
    for side, thickness in candidates.items():
        lows, highs = lumas[side]["YLOW"], lumas[side]["YHIGH"]
        if len(lows) != len(frames) or len(highs) != len(frames):
            raise VPError("data", f"could not confirm reel {side} bar across the sampled frames")
        votes = sum(edges.get(side, 0) >= thickness and high <= float(st["bar_max_luma"])
                    and high - low <= _REEL_BAR_MAX_LUMA_SPREAD
                    for edges, low, high in zip(frame_bars, lows, highs))
        if votes * 2 > len(frames):
            confirmed.append(side)
    return keep_sides(area, W, H, confirmed), len(measured)


def active_area(path: str | os.PathLike, start_ms: int = 0, end_ms: int | None = None,
                policy: str = "union", st: dict[str, Any] | None = None) -> Rect:
    """Confirmed active area for cropping. Nothing measurable (every sample black or unreadable)
    -> VPError(data): an unmeasured picture is never treated as clean."""
    area, n = measure(path, start_ms, end_ms, policy, st)
    if area is None:
        raise VPError("data", "the picture area could not be measured (every sample is black or "
                              "unreadable)")
    return area


def reel_crop_rect(frame_w: int, frame_h: int, area: Rect, x_frac: float,
                   w_frac: float) -> tuple[Rect, int, int]:
    """Largest 9:16 rectangle inside `area`, centred on the profile band (clamped into the area);
    output size = the frame height at 9:16. With no bars this equals the old fixed band."""
    h = _even(area.h)
    w = _even(h * REEL_ASPECT)
    if w > area.w:
        w = _even(area.w)
        h = _even(w / REEL_ASPECT)
    centre = frame_w * (x_frac + w_frac / 2)
    x = int(round(centre - w / 2))
    x = max(area.x, min(x, area.x + area.w - w))
    y = area.y + (area.h - h) // 2
    out_h = _even(frame_h)
    out_w = _even(out_h * REEL_ASPECT)
    return Rect(x, y, w, h), out_w, out_h


def long_crop_rect(frame_w: int, frame_h: int, area: Rect,
                   st: dict[str, Any] | None = None) -> Rect | None:
    """The active area (even sizes) when the source has bars, else None (keep the frame)."""
    if not bars(frame_w, frame_h, area, st):
        return None
    return Rect(area.x, area.y, _even(area.w), _even(area.h))


# ---- the code gate ----------------------------------------------------------------------------

_BLACK_RE = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")
_FREEZE_START = re.compile(r"freeze_start:\s*([\d.]+)")
_FREEZE_END = re.compile(r"freeze_end:\s*([\d.]+)")
_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*([\d.]+)")


def _intervals(starts: list[float], ends: list[float], total_s: float) -> list[tuple[float, float]]:
    out = []
    for k, s in enumerate(starts):
        e = ends[k] if k < len(ends) else total_s
        out.append((max(s, 0.0), min(e, total_s)))
    return out


def scan_av(path: str | os.PathLike, start_ms: int, end_ms: int, st: dict[str, Any],
            has_audio: bool) -> dict[str, list[tuple[float, float]]]:
    """One decode pass over [start, end): black, frozen and silent intervals (seconds from start)."""
    dur_s = (end_ms - start_ms) / 1000
    vf = (f"scale=320:-2,blackdetect=d={st['black_min_s']}:pix_th={st['black_pixel_threshold']},"
          f"freezedetect=n={st['freeze_noise_db']}dB:d={st['freeze_min_s']}")
    args = ["-ss", f"{start_ms / 1000:.3f}", "-i", str(path), "-t", f"{dur_s:.3f}", "-map", "0:v:0",
            "-vf", vf]
    if has_audio:
        args += ["-map", "0:a:0", "-af", f"silencedetect=n={st['silence_db']}dB:d=0.5"]
    proc = media.run_ffmpeg([*args, "-f", "null", "-"])
    err = proc.stderr
    black = [(float(a), float(b)) for a, b in _BLACK_RE.findall(err)]
    frozen = _intervals([float(x) for x in _FREEZE_START.findall(err)],
                        [float(x) for x in _FREEZE_END.findall(err)], dur_s)
    silent = _intervals([float(x) for x in _SIL_START.findall(err)],
                        [float(x) for x in _SIL_END.findall(err)], dur_s) if has_audio else []
    return {"black": black, "frozen": frozen, "silent": silent}


def _check(ok: bool, value: Any, detail: str) -> dict[str, Any]:
    return {"ok": bool(ok), "value": value, "detail": detail}


def inspect(path: str | os.PathLike, kind: str, span: tuple[int, int] | None = None,
            config: dict[str, Any] | None = None,
            expect_size: tuple[int, int] | None = None) -> dict[str, Any]:
    """Code gate on a clip. `span` = [start, end) ms of the picture to judge (render: without the
    title/end cards, whose dark background would read as black). Returns
    {ok, kind, size, span_ms, checks: {name: {ok, value, detail}}, failures: [text]}."""
    st = settings(config)
    info = media.probe(path)
    W, H = info["width"], info["height"]
    start, end = span if span else (0, info["duration_ms"])
    checks: dict[str, dict[str, Any]] = {}

    if kind == "reel":
        aspect = W / H if H else 0
        ok = abs(aspect - REEL_ASPECT) <= float(st["aspect_tolerance"])
        checks["size"] = _check(ok, [W, H], f"{W}x{H}" + ("" if ok else " is not 9:16"))
    else:
        ok = W >= H
        checks["size"] = _check(ok, [W, H], f"{W}x{H}" + ("" if ok else " is not horizontal"))
    if expect_size and ok and (W, H) != tuple(expect_size):
        checks["size"] = _check(False, [W, H], f"{W}x{H}, expected {expect_size[0]}x{expect_size[1]}")

    # Reel: confirm bars on a majority of independently sampled frames. Horizontal: bars
    # present THROUGHOUT fail (union: the crop in S4 must have removed them); bars in only part of
    # the video (a Zoom layout switch, e.g. speaker 16:9 -> wide screen share) are reported as
    # `partial_bars` — per-scene reframing is v2, YouTube shows such parts pillarboxed anyway.
    if kind == "reel":
        area, n_measured = _reel_area(path, start, min(end, info["duration_ms"]), W, H, st)
    else:
        area, n_measured = measure(path, start, end, "union", st)
    if area is None:
        found: dict[str, int] = {}
        checks["bars"] = _check(False, "unmeasured", "picture area could not be measured (every "
                                                     "sample black or unreadable)")
        partial: dict[str, int] = {}
    else:
        found = bars(W, H, area, st)
        checks["bars"] = _check(not found, found or {}, (
            "no black bars" if not found else
            "black bars: " + ", ".join(f"{s} {px} px" for s, px in found.items())
            + f" (picture is {area.w}x{area.h} at {area.x},{area.y} in a {W}x{H} frame)"))
        partial = {}
        if kind != "reel":
            med, _ = measure(path, start, end, "median", st)
            partial = bars(W, H, med, st) if med else {}

    checks["audio"] = _check(info["has_audio"], info["has_audio"],
                             "audio present" if info["has_audio"] else "no audio track")
    av = scan_av(path, start, end, st, info["has_audio"])
    total_s = max((end - start) / 1000, 0.001)

    longest_black = max((b - a for a, b in av["black"]), default=0.0)
    ok = longest_black < float(st["black_min_s"])
    checks["black"] = _check(ok, round(longest_black, 2), (
        "no black stretches" if not av["black"] else
        f"black picture for {longest_black:.1f} s ({len(av['black'])} stretch(es))"))

    frozen_s = sum(b - a for a, b in av["frozen"])
    share = frozen_s / total_s
    fmax = (st["frozen_share_max"] or {}).get(kind)
    ok = fmax is None or share < float(fmax)
    checks["frozen"] = _check(ok, round(share, 3), (
        f"picture frozen {share:.0%} of the time" if av["frozen"] else "picture moves")
        + ("" if fmax is not None else " (reported, not checked for this kind)"))

    limit = (st["silence_max_s"] or {}).get(kind)
    longest_sil = max((b - a for a, b in av["silent"]), default=0.0)
    if limit is None or not info["has_audio"]:
        checks["silence"] = _check(True, round(longest_sil, 2), "not checked for this kind")
    else:
        ok = longest_sil < float(limit)
        checks["silence"] = _check(ok, round(longest_sil, 2), (
            f"longest silence {longest_sil:.1f} s" + ("" if ok else f" (limit {limit} s)")))

    failures = [f"{name}: {c['detail']}" for name, c in checks.items() if not c["ok"]]
    return {"ok": not failures, "kind": kind, "size": [W, H], "span_ms": [start, end],
            "active_area": area.as_dict() if area else None, "samples_measured": n_measured, "partial_bars": partial, "checks": checks,
            "failures": failures}


def apply_waivers(report: dict[str, Any], waive: Iterable[str] = ()) -> dict[str, Any]:
    """Mark owner-waived checks; `ok` and `failures` then ignore them (kept visible as `waived`).
    A check that did not run carries `ok: None` (never True): neither a failure nor a pass."""
    waive = set(waive or ())
    waived = [n for n, c in report["checks"].items() if c["ok"] is False and n in waive]
    failures = [f"{n}: {c['detail']}" for n, c in report["checks"].items()
                if c["ok"] is False and n not in waive]
    return {**report, "ok": not failures, "failures": failures, "waived": waived}


def require_clean(report: dict[str, Any], what: str) -> None:
    """Fail closed: VPError(data) naming every failed check of `report`."""
    if not report["ok"]:
        raise VPError("data", f"{what}: picture check failed — " + "; ".join(report["failures"]))


def check_media(path: str | os.PathLike, kind: str, what: str,
                config: dict[str, Any] | None = None, waive: Iterable[str] = ()) -> dict[str, Any]:
    """inspect + waivers + require_clean, for callers outside the step graph (editor exports):
    the media that goes into a CapCut / DaVinci / Final Cut project must pass the same gate."""
    report = apply_waivers(inspect(path, kind, config=config), waive)
    require_clean(report, what)
    return report
