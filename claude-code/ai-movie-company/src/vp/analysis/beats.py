"""Tempo, beats and bars of a music file (capability Б), ffmpeg + standard library only.

  1. ffmpeg decodes the file to mono 16-bit PCM at SR Hz as two channels: full band and a
     low band (kick drum, bass), interleaved.
  2. Onset strength per hop (HOP samples): the rise of log energy in each band ("energy flux"),
     each band scaled by its own spread, summed, minus a local mean, half-wave rectified.
  3. Tempo: autocorrelation of the onset strength over the lags of 60–200 BPM, weighted by a soft
     prior around 120 BPM (octave errors), peak refined by a parabola.
  4. Beats: dynamic programming (Ellis 2007) — each beat on an onset peak, the spacing close to
     the period; each beat refined to the sub-hop peak of the onset strength. The reported tempo
     is the slope of a least-squares line through the beat times, so it is not limited by the hop.
  5. Bars: 4/4 assumed; the downbeat phase is the one of the 4 whose beats carry the most low-band
     onset strength. This is a guess and is marked as such (`meter_guess`).

Output (beats.json):
  {schema, source, duration_ms, tempo_bpm, period_ms, meter, meter_guess, confidence,
   beats_ms: [...], downbeats_ms: [...], onsets_ms: [...],
   beats: [{ms, bar, beat, downbeat, strength}]}

CLI:  vp beats <music-file> [--json] [--out FILE]
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import operator
import subprocess
import sys
from array import array
from pathlib import Path
from typing import Any

from ..adapters import media
from ..core.errors import VPError
from ..core.io import write_json

SR = 11025
HOP = 256                # ~23 ms
WIN = 512
BPM_MIN, BPM_MAX = 60.0, 200.0
BPM_PRIOR = 120.0
PRIOR_OCTAVES = 1.0      # width of the log-normal tempo prior, in octaves
TIGHTNESS = 100.0        # DP: penalty for a beat spacing away from the period
METER = 4
SCHEMA = 1


# ---- decode --------------------------------------------------------------------------------

def decode(path: str | Path, sr: int = SR, timeout_s: float = 600) -> tuple[array, array]:
    """(full band, low band) mono PCM samples of the file."""
    p = Path(path)
    if not p.is_file():
        raise VPError("data", f"music file not found: {p}")
    graph = (f"[0:a:0]aresample={sr},aformat=sample_fmts=fltp:channel_layouts=mono,asplit=2[f][b];"
             "[b]lowpass=f=150,lowpass=f=150[l];"
             "[f][l]join=inputs=2:channel_layout=stereo,aformat=sample_fmts=s16[o]")
    cmd = [media.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", str(p),
           "-filter_complex", graph, "-map", "[o]", "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except FileNotFoundError as e:
        raise VPError("internal", "ffmpeg not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise VPError("data", f"decoding {p.name} timed out") from e
    if proc.returncode != 0:
        raise VPError("data", f"decoding {p.name} failed: "
                              f"{proc.stderr.decode('utf-8', 'replace').strip()[-400:]}")
    data = array("h")
    raw = proc.stdout[: len(proc.stdout) - len(proc.stdout) % 4]
    data.frombytes(raw)
    if sys.byteorder != "little":
        data.byteswap()
    if len(data) < 2 * WIN:
        raise VPError("data", f"{p.name}: no audio (or shorter than {WIN / sr:.2f} s)")
    return data[0::2], data[1::2]


# ---- onset strength ------------------------------------------------------------------------

def frame_energy(x: array | list[float], hop: int = HOP, win: int = WIN) -> list[float]:
    cs = [0, *itertools.accumulate(map(operator.mul, x, x))]
    n = len(x)
    return [float(cs[min(i + win, n)] - cs[i]) for i in range(0, n - win + 1, hop)]


def _flux(energy: list[float]) -> list[float]:
    mean = sum(energy) / len(energy) if energy else 0.0
    floor = max(mean * 1e-3, 1e-9)
    logs = [math.log(e + floor) for e in energy]
    flux = [0.0] + [max(0.0, b - a) for a, b in zip(logs, logs[1:])]
    m = sum(flux) / len(flux)
    sd = math.sqrt(sum((v - m) ** 2 for v in flux) / len(flux)) or 1.0
    return [v / sd for v in flux]


def onset_strength(full: array | list[float], low: array | list[float],
                   hop: int = HOP, win: int = WIN, local: int = 16) -> list[float]:
    a, b = _flux(frame_energy(full, hop, win)), _flux(frame_energy(low, hop, win))
    o = [x + y for x, y in zip(a, b)]
    cs = [0.0, *itertools.accumulate(o)]
    n, h = len(o), local // 2
    out = []
    for i, v in enumerate(o):
        lo, hi = max(0, i - h), min(n, i + h + 1)
        out.append(max(0.0, v - (cs[hi] - cs[lo]) / (hi - lo)))
    return out


# ---- tempo ---------------------------------------------------------------------------------

def _parabola(ym: float, y0: float, yp: float) -> float:
    d = ym - 2 * y0 + yp
    return 0.0 if d == 0 else max(-0.5, min(0.5, 0.5 * (ym - yp) / d))


def tempo_period(o: list[float], fps: float) -> tuple[float, float]:
    """(period in frames (fractional), confidence 0..1) by weighted autocorrelation."""
    lo = max(2, int(math.floor(60.0 / BPM_MAX * fps)))
    hi = int(math.ceil(60.0 / BPM_MIN * fps))
    if len(o) < hi * 3:
        raise VPError("data", "music too short to estimate a tempo (need ~3 s)")
    # onsets are one frame wide and the period is not a whole number of frames: smooth before
    # correlating, or the true period loses to its double (seen on a 140 BPM click track)
    k = (1.0, 2.0, 3.0, 2.0, 1.0)
    o = [sum(k[j] * o[i + j - 2] for j in range(5) if 0 <= i + j - 2 < len(o)) for i in range(len(o))]
    r = {lag: sum(map(operator.mul, o[:-lag], o[lag:])) / (len(o) - lag)
         for lag in range(lo - 1, hi + 2)}
    r0 = sum(v * v for v in o) / len(o) or 1.0

    def w(lag: float) -> float:
        bpm = 60.0 * fps / lag
        return math.exp(-0.5 * (math.log2(bpm / BPM_PRIOR) / PRIOR_OCTAVES) ** 2)
    best = max(range(lo, hi + 1), key=lambda lag: r[lag] * w(lag))
    frac = best + _parabola(r[best - 1], r[best], r[best + 1])
    return frac, max(0.0, min(1.0, r[best] / r0))


# ---- beats ---------------------------------------------------------------------------------

def track_beats(o: list[float], period: float, tightness: float = TIGHTNESS) -> list[int]:
    """Beat frames by dynamic programming (Ellis 2007)."""
    n = len(o)
    score = list(o)
    back = [-1] * n
    pmin, pmax = max(1, int(round(period / 2))), int(round(period * 2))
    for t in range(n):
        best, arg = -math.inf, -1
        for p in range(max(0, t - pmax), t - pmin + 1):
            v = score[p] - tightness * math.log((t - p) / period) ** 2
            if v > best:
                best, arg = v, p
        if arg >= 0 and best > 0:
            score[t] = o[t] + best
            back[t] = arg
    tail = range(max(0, n - int(round(period))), n)
    t = max(tail, key=lambda i: score[i])
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats.reverse()
    # after the music ends the DP keeps "beating" one period on (the spacing alone scores); drop
    # leading / trailing beats with no onset under them
    if beats:
        strengths = sorted(o[b] for b in beats)
        floor = 0.1 * strengths[len(strengths) // 2]
        while beats and o[beats[-1]] <= floor:
            beats.pop()
        while beats and o[beats[0]] <= floor:
            beats.pop(0)
    return beats


def refine(o: list[float], frames: list[int], radius: int = 2) -> list[float]:
    """Each beat moved to the local peak of the onset strength, with a sub-frame parabola."""
    out = []
    for f in frames:
        lo, hi = max(1, f - radius), min(len(o) - 2, f + radius)
        k = max(range(lo, hi + 1), key=lambda i: o[i]) if hi >= lo else f
        if 1 <= k < len(o) - 1:
            out.append(k + _parabola(o[k - 1], o[k], o[k + 1]))
        else:
            out.append(float(k))
    return out


def regression_period(times: list[float]) -> float | None:
    """Slope of the least-squares line through (index, time)."""
    n = len(times)
    if n < 4:
        return None
    mx, my = (n - 1) / 2, sum(times) / n
    num = sum((i - mx) * (t - my) for i, t in enumerate(times))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else None


def peaks(o: list[float], fps: float, k: float = 1.0, min_gap_s: float = 0.08) -> list[float]:
    """Onset times (s): local maxima above mean + k·sd, at least `min_gap_s` apart."""
    m = sum(o) / len(o)
    sd = math.sqrt(sum((v - m) ** 2 for v in o) / len(o))
    gap = max(1, int(min_gap_s * fps))
    out: list[int] = []
    for i in range(1, len(o) - 1):
        if o[i] > m + k * sd and o[i] >= o[i - 1] and o[i] > o[i + 1]:
            if out and i - out[-1] < gap:
                if o[i] > o[out[-1]]:
                    out[-1] = i
                continue
            out.append(i)
    return [i / fps for i in out]


def analyse_signal(full: array | list[float], low: array | list[float], sr: int = SR) -> dict[str, Any]:
    fps = sr / HOP
    o = onset_strength(full, low)
    # frame i covers samples [i*HOP, i*HOP+WIN); its flux rises when an onset enters the window,
    # i.e. the onset lies in the last hop of the window: on average at i*HOP + WIN - HOP/2
    t0 = (WIN - HOP / 2) / sr
    period, conf = tempo_period(o, fps)
    frames = track_beats(o, period)
    beat_s = [f / fps + t0 for f in refine(o, frames)]
    slope = regression_period(beat_s)
    tempo = 60.0 / slope if slope else 60.0 * fps / period
    # downbeat phase from the low band onset strength at the beats
    lowo = _flux(frame_energy(low))
    phase_score = [0.0] * METER
    for i, f in enumerate(frames):
        phase_score[i % METER] += lowo[f] if f < len(lowo) else 0.0
    phase = max(range(METER), key=lambda k: phase_score[k])
    top = max(o[f] for f in frames) if frames else 1.0
    beats = []
    for i, (f, t) in enumerate(zip(frames, beat_s)):
        k = (i - phase) % METER
        beats.append({"ms": int(round(t * 1000)), "bar": (i - phase) // METER + 1, "beat": k + 1,
                      "downbeat": k == 0, "strength": round(o[f] / top, 3) if top else 0.0})
    return {"tempo_bpm": round(tempo, 2), "period_ms": round(60000.0 / tempo, 1),
            "meter": METER, "meter_guess": True, "confidence": round(conf, 3),
            "beats_ms": [b["ms"] for b in beats],
            "downbeats_ms": [b["ms"] for b in beats if b["downbeat"]],
            "onsets_ms": [int(round((t + t0) * 1000)) for t in peaks(o, fps)],
            "beats": beats, "duration_ms": int(round(len(full) / sr * 1000))}


def analyse(path: str | Path) -> dict[str, Any]:
    full, low = decode(path)
    res = analyse_signal(full, low)
    return {"schema": SCHEMA, "source": str(Path(path).resolve()), **res}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp beats", description="tempo, beats, bars of a music file")
    ap.add_argument("music")
    ap.add_argument("--json", action="store_true", help="print the whole result")
    ap.add_argument("--out", help="also write beats.json here (the music folder is never written)")
    a = ap.parse_args(argv)
    res = analyse(a.music)
    if a.out:
        write_json(Path(a.out), res, indent=1)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(f"tempo {res['tempo_bpm']} BPM, {len(res['beats_ms'])} beats, "
              f"{len(res['downbeats_ms'])} bars (4/4 guessed), confidence {res['confidence']}")
    return 0
