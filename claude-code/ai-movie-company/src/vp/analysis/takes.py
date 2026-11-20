"""Cleaning raw talking-head takes (capability Ч), first version: code only.

From the word timings of the recording (`_cut.json`, Whisper verbose_json — the same file and the
same reader S3a uses) the code proposes cuts of three kinds:

  pause     a gap between words longer than `pause_ms` (default 1000) — cut down to `pad_ms` of
            air on each side
  filler    hesitations («э», «эм», «ммм», "uh", "um", …) always; words that carry meaning in
            other places («ну», «вот», «как бы», «то есть», "like", "you know") only when isolated:
            a pause of at least `iso_ms` before AND after
  restart   the same 3+ words said again within `restart_s` seconds: the earlier take(s) and
            everything between are cut, the last take is kept

Overlapping cuts are merged. Result (<job>/analysis/takes.json):
  {schema, source_video, source_words, duration_ms, settings,
   cuts [{id, start_ms, end_ms, reason, text, status}], keep [{start_ms, end_ms}],
   removed_ms, review: null}

Hook for review: every cut has `status: proposed`; a model or the person may set it to
`rejected` (`vp takes set <job> <cut-id> keep`) or `accepted`; `keep` is recomputed from the cuts
not rejected. `review` is reserved for a model's pass over the proposals (not done in this
version: the model would see the words around each cut, never the times).

Apply: `vp takes apply <job>` renders the keep ranges back to back (own sound, 10 ms fades at the
joins) into <job>/analysis/takes/clean.mp4 and writes the word timings moved onto the new
timeline as clean.json (same format as _cut.json). To run the speech pipeline on the clean take,
point request.yaml `source.video` / `source.words` at them (the pipeline never edits request.yaml).

CLI:
  vp takes clean <job> [--pause 1.0] [--json]
  vp takes set <job> <cut-id> keep|cut
  vp takes apply <job>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..adapters import media
from ..core.errors import VPError
from ..core.io import read_json, write_json
from ..core.paths import Job
from ..steps._fragments import Word, load_words
from . import render_plan
from .scenes import analysis_dir, job_root

PAUSE_MS = 1000
PAD_MS = 150
ISO_MS = 250
RESTART_S = 6.0
RESTART_MIN = 3

HESITATIONS = {"э", "ээ", "эээ", "эм", "эмм", "ммм", "мм", "м", "а-а", "uh", "uhh", "um", "umm",
               "uhm", "erm", "er", "hmm", "mm", "ah"}
FILLERS_ISOLATED = [("как", "бы"), ("то", "есть"), ("you", "know"), ("i", "mean"), ("ну",),
                    ("вот",), ("like",), ("so",), ("короче",), ("типа",), ("значит",)]


def _cut(start: int, end: int, reason: str, words: list[Word]) -> dict[str, Any]:
    return {"start_ms": int(start), "end_ms": int(end), "reason": reason,
            "text": " ".join(w.norm for w in words)}


def find_pauses(words: list[Word], pause_ms: int = PAUSE_MS, pad_ms: int = PAD_MS) -> list[dict]:
    out = []
    for a, b in zip(words, words[1:]):
        if b.start_ms - a.end_ms > pause_ms:
            out.append(_cut(a.end_ms + pad_ms, b.start_ms - pad_ms, "pause", []))
    return out


def _gap_before(words: list[Word], i: int) -> int:
    return words[i].start_ms - words[i - 1].end_ms if i > 0 else 10 ** 9


def _gap_after(words: list[Word], j: int) -> int:
    return words[j + 1].start_ms - words[j].end_ms if j + 1 < len(words) else 10 ** 9


def _span(words: list[Word], i: int, j: int, pad_ms: int) -> tuple[int, int]:
    """Cut for words[i..j] with the air around it, leaving `pad_ms` next to the neighbours."""
    s, e = words[i].start_ms, words[j].end_ms
    if i > 0 and _gap_before(words, i) > pad_ms:
        s = words[i - 1].end_ms + pad_ms
    if j + 1 < len(words) and _gap_after(words, j) > pad_ms:
        e = words[j + 1].start_ms - pad_ms
    return s, e


def find_fillers(words: list[Word], iso_ms: int = ISO_MS, pad_ms: int = PAD_MS) -> list[dict]:
    out = []
    i = 0
    while i < len(words):
        if words[i].norm in HESITATIONS:
            out.append(_cut(*_span(words, i, i, pad_ms), "filler", words[i:i + 1]))
            i += 1
            continue
        hit = None
        for ph in FILLERS_ISOLATED:
            j = i + len(ph) - 1
            if j < len(words) and tuple(x.norm for x in words[i:j + 1]) == ph \
                    and _gap_before(words, i) >= iso_ms and _gap_after(words, j) >= iso_ms:
                hit = j
                break
        if hit is None:
            i += 1
            continue
        out.append(_cut(*_span(words, i, hit, pad_ms), "filler", words[i:hit + 1]))
        i = hit + 1
    return out


def find_restarts(words: list[Word], restart_s: float = RESTART_S, min_words: int = RESTART_MIN,
                  pad_ms: int = PAD_MS) -> list[dict]:
    """The same `min_words`+ words repeated within `restart_s`: cut from the first take to the
    start of the last one."""
    out = []
    toks = [w.norm for w in words]
    i = 0
    while i + min_words <= len(words):
        last_j = None
        for j in range(i + min_words, len(words) - min_words + 1):
            if words[j].start_ms - words[i].start_ms > restart_s * 1000:
                break
            if toks[j:j + min_words] == toks[i:i + min_words]:
                last_j = j
        if last_j is None:
            i += 1
            continue
        start = _span(words, i, i, pad_ms)[0]
        end = words[last_j].start_ms - min(pad_ms, max(0, _gap_before(words, last_j)) // 2)
        out.append(_cut(start, end, "restart", words[i:last_j]))
        i = last_j
    return out


def merge(cuts: list[dict], duration_ms: int) -> list[dict]:
    """Sort, clip to [0, duration], merge overlapping cuts (reasons joined)."""
    cs = sorted((dict(c, start_ms=max(0, c["start_ms"]), end_ms=min(duration_ms, c["end_ms"]))
                 for c in cuts), key=lambda c: (c["start_ms"], c["end_ms"]))
    out: list[dict] = []
    for c in cs:
        if c["end_ms"] - c["start_ms"] <= 0:
            continue
        if out and c["start_ms"] <= out[-1]["end_ms"]:
            o = out[-1]
            o["end_ms"] = max(o["end_ms"], c["end_ms"])
            if c["reason"] not in o["reason"].split("+"):
                o["reason"] += "+" + c["reason"]
            o["text"] = " ".join(t for t in (o["text"], c["text"]) if t)
        else:
            out.append(dict(c))
    for k, c in enumerate(out, 1):
        c["id"] = f"c{k:03d}"
        c.setdefault("status", "proposed")
    return out


def keep_ranges(cuts: list[dict], duration_ms: int) -> list[dict]:
    keep, t = [], 0
    for c in sorted((c for c in cuts if c.get("status") != "rejected"), key=lambda c: c["start_ms"]):
        if c["start_ms"] > t:
            keep.append({"start_ms": t, "end_ms": c["start_ms"]})
        t = max(t, c["end_ms"])
    if duration_ms > t:
        keep.append({"start_ms": t, "end_ms": duration_ms})
    return keep


def propose(words: list[Word], duration_ms: int, pause_ms: int = PAUSE_MS, pad_ms: int = PAD_MS,
            iso_ms: int = ISO_MS, restart_s: float = RESTART_S) -> dict[str, Any]:
    cuts = merge(find_pauses(words, pause_ms, pad_ms) + find_fillers(words, iso_ms, pad_ms)
                 + find_restarts(words, restart_s, RESTART_MIN, pad_ms), duration_ms)
    keep = keep_ranges(cuts, duration_ms)
    return {"duration_ms": duration_ms, "cuts": cuts, "keep": keep,
            "removed_ms": duration_ms - sum(k["end_ms"] - k["start_ms"] for k in keep),
            "settings": {"pause_ms": pause_ms, "pad_ms": pad_ms, "iso_ms": iso_ms,
                         "restart_s": restart_s, "restart_min_words": RESTART_MIN},
            "review": None}


def remap_words(words_json: dict[str, Any], keep: list[dict]) -> dict[str, Any]:
    """Whisper-style words moved onto the kept timeline; words inside a cut are dropped."""
    raw = []
    if isinstance(words_json, dict):
        for seg in words_json.get("segments") or []:
            raw.extend((seg or {}).get("words") or [])
        raw.extend(words_json.get("words") or [])
    out = []
    for w in raw:
        s, e = float(w["start"]) * 1000, float(w["end"]) * 1000
        off = 0.0
        for k in keep:
            if k["start_ms"] <= s < k["end_ms"]:
                ns = off + s - k["start_ms"]
                ne = off + min(e, k["end_ms"]) - k["start_ms"]
                out.append({"word": w.get("word", ""), "start": round(ns / 1000, 3),
                            "end": round(max(ns, ne) / 1000, 3)})
                break
            off += k["end_ms"] - k["start_ms"]
    return {"words": out}


# ---- job level -----------------------------------------------------------------------------

def takes_path(job: str | Path) -> Path:
    return analysis_dir(job) / "takes.json"


def clean(job: str | Path, pause_ms: int = PAUSE_MS) -> dict[str, Any]:
    j = Job(job_root(job))
    words = load_words(j.source_words)
    if not words:
        raise VPError("data", f"no word timings in {j.source_words}")
    video = j.source_video
    dur = media.duration_ms(video) if video.is_file() else words[-1].end_ms + 500
    res = {"schema": 1, "source_video": str(video) if video.is_file() else None,
           "source_words": str(j.source_words), **propose(words, dur, pause_ms)}
    write_json(takes_path(job), res, indent=1)
    return res


def load(job: str | Path) -> dict[str, Any]:
    data = read_json(takes_path(job), None)
    if not isinstance(data, dict) or "cuts" not in data:
        raise VPError("data", f"no takes.json in {analysis_dir(job)}: run `vp takes clean` first")
    return data


def set_status(job: str | Path, cut_id: str, decision: str) -> dict[str, Any]:
    data = load(job)
    c = next((c for c in data["cuts"] if c["id"] == cut_id), None)
    if c is None:
        raise VPError("data", f"no cut {cut_id!r}")
    c["status"] = {"keep": "rejected", "cut": "accepted"}[decision]
    data["keep"] = keep_ranges(data["cuts"], data["duration_ms"])
    data["removed_ms"] = data["duration_ms"] - sum(k["end_ms"] - k["start_ms"] for k in data["keep"])
    write_json(takes_path(job), data, indent=1)
    return c


def apply(job: str | Path) -> dict[str, Any]:
    data = load(job)
    if not data.get("source_video"):
        raise VPError("data", "no source video to cut")
    src = Path(data["source_video"])
    info = media.probe(src)
    if not info.get("has_audio"):
        raise VPError("data", f"{src.name} has no audio")
    segs = [{"source_path": str(src), "in_ms": k["start_ms"],
             "duration_ms": k["end_ms"] - k["start_ms"]} for k in data["keep"]]
    out_dir = analysis_dir(job) / "takes"
    out = render_plan.render(segs, out_dir / "clean.mp4",
                             (int(info["width"]), int(info["height"]), float(info.get("fps") or 30)),
                             keep_audio=True)
    words_json = read_json(Path(data["source_words"]), {}) or {}
    write_json(out_dir / "clean.json", remap_words(words_json, data["keep"]), indent=1)
    return {"video": str(out), "words": str(out_dir / "clean.json"),
            "duration_ms": media.duration_ms(out), "removed_ms": data["removed_ms"]}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp takes", description="clean raw takes by word timings")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("clean", help="propose cuts -> <job>/analysis/takes.json")
    c.add_argument("job")
    c.add_argument("--pause", type=float, default=PAUSE_MS / 1000, help="seconds")
    c.add_argument("--json", action="store_true")
    s = sub.add_parser("set", help="keep (reject the cut) or cut (accept it)")
    s.add_argument("job")
    s.add_argument("cut")
    s.add_argument("decision", choices=("keep", "cut"))
    ap_ = sub.add_parser("apply", help="render the keep ranges -> analysis/takes/clean.mp4")
    ap_.add_argument("job")
    ap_.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "clean":
        res = clean(a.job, int(round(a.pause * 1000)))
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            by: dict[str, int] = {}
            for x in res["cuts"]:
                by[x["reason"]] = by.get(x["reason"], 0) + 1
            print(f"{len(res['cuts'])} cuts proposed ("
                  + ", ".join(f"{k} {v}" for k, v in sorted(by.items())) + "), "
                  f"{res['removed_ms'] / 1000:.1f} s of {res['duration_ms'] / 1000:.1f} s -> "
                  f"{takes_path(a.job)}")
        return 0
    if a.cmd == "set":
        x = set_status(a.job, a.cut, a.decision)
        print(f"{x['id']} {x['reason']} «{x['text']}»: {x['status']}")
        return 0
    res = apply(a.job)
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else
          f"clean take {res['duration_ms'] / 1000:.1f} s -> {res['video']} (words: {res['words']})")
    return 0
