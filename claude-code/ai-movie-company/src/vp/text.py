"""Text-only jobs (K15, K20, K21): "here is a text, make a video of it".

The creative path (brief, screenplay, storyboard sketches, voice, music, Remotion animation) is
done by the director in the dialog, as in the 2026-11-18 probe (spikes/text-to-video). The engine
owns only the bookkeeping: the job folder, where each stage stands, the length/credit estimate,
the timeline built from the measured voice files, and the CapCut export (vp.adapters.capcut).

Job folder of a text job (the contract the dialog writes against):

  (folder chosen by the dialog, e.g. ~/Movies/AI Movie Company/<date> <title>)
  request.yaml        kind: text, title, source.text, enabled: false (the recording route never
                      runs on it)
  source.txt|.md      the person's text: when `--file` already lies in the job folder it is used
                      in place; otherwise it is copied to source.md. Never modified.
  brief.md            stage "brief"
  script.md           stage "script": the screenplay table `| S1 | voice text | screen | ≈ s |`;
                      its rows give the scenes when there is no scenes.json (cues split by code)
  scenes.json         optional machine form of the scenes (see below), wins over script.md
  sketches/*.png      stage "sketches" (static storyboard pictures, one per scene)
  approvals.yaml      `vp text approve <job> sketches` — production waits for it
  voice/S01.mp3 …     stage "voice": one file per scene, matched by scene id (S1 -> S01.mp3/s1.mp3),
                      made by `vp voice scenes` (+ voice/S01.alignment.json, word timings);
                      voice/samples/ (voice trials) and voice/flow.txt are ignored
  music/*.mp3|wav     stage "music" (`vp music make`, or the person's own track)
  timeline.json       stage "animation": built by `vp text timeline` from the voice files; the
                      Remotion composition is laid out on it (`vp text render` passes it as props)
  remotion-video/     the job's own Remotion project (src/index.ts registers the compositions);
                      its node_modules is a link to the engine's Remotion workspace — no install
  renders/vN*.mp4     stage "render" by `vp text render` (vN.mp4 = sound baked in, loudness
                      mastered; vN-picture.mp4 = picture only; vN-chat.mp4 = the copy small
                      enough for the chat, only when vN.mp4 is too big)
  renders/vN.timing.json  when the version first finished mastering (`vp text timing`)
  capcut/vN/          stage "capcut": one folder per exported CapCut version (vp capcut)
  run.json            the run signal's state (vp.runsignal): the job's random run number, the
                      scenario letter, the stages already reported; written only by the engine

scenes.json (written in the dialog; texts only — no times, the times come from the voice):
  {"fps": 30, "scenes": [{"id": "S1", "cues": ["subtitle line", ...],
     "voice": "voice/s01.mp3"            (optional; default voice/<id>.mp3 or voice/sNN.mp3)
     "lead_in_frames": 15, "gap_after_frames": 10   (optional; defaults below)
     "subtitles": true                    (optional)
     "anchors": {"3": 6.25}}]}            (optional, measured by code or the person: cue 3
                                           starts at 6.25 s into the voice file)

Estimate (measured in the probe, eleven_v4, voice «Алина»): ≈ 14.8 characters per second of voice;
pauses: a lead-in, a gap between scenes, a hold at the end (frame counts of the probe), plus any
ElevenLabs <break time="Xs"/> tags. Credits: voice 1 per character (v4); voice trials = 3 voices
× one line of up to 200 characters (the choice form holds 3 voices + «other voices»); music 900
per minute = 15 per second (elevenlabs.io/pricing; measured 2026-11-19: 945 credits for 63 s).

CLI:  vp text new <job> --file <text.md> [--title T] [--scenario <letter>] [--json]
      vp text status <job> [--json]          stages, the next step and its exact command
      vp text approve <job> sketches|… [--note T]
      vp text estimate <job> [--json]
      vp text timing <job> [--json]          time from job creation to each finished render
      vp text timeline <job> [--json]
      vp text render <job> --composition <Id> [--version vN] [--project DIR] [--entry F]
                     [--no-props] [--force] [--json]
                     Remotion render of the job's own project with the engine's Node and Chrome
                     (never `npx`), then `master`
      vp text master <job> [--version vN] [--json]
                     loudness −14 LUFS (two-pass loudnorm, video copied), vN-picture.mp4 and,
                     when vN.mp4 is too big for the chat, vN-chat.mp4
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from .core.errors import VPError
from .core.io import now_iso, parse_iso, read_json, read_yaml, write_json, write_yaml

KIND = "text"
STAGES = ("brief", "script", "sketches", "voice", "music", "animation", "render", "capcut")
SOURCE = "source.md"
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".svg")

CHARS_PER_S = 14.8          # eleven_v4, measured: 1,025 chars -> 69.5 s of voice
FPS = 30
TAIL_SILENCE_S = 0.2        # every ElevenLabs line ends with ~0.2 s of silence
LEAD_IN_FRAMES = 15         # before the first line
GAP_FRAMES = 10             # after a line (probe: 10..24)
END_HOLD_FRAMES = 80        # after the last line
EST_GAP_S = 0.4             # average pause between scenes for the estimate (probe: 204 frames
                            # of gaps over 10 scenes, last hold excluded -> ~0.4 s)
SAMPLE_VOICES = 3           # voice trials: 3 different voices, one take each (K9); the choice
                            # form = these 3 + «other voices» (4 = the most a form holds)
SAMPLE_CHARS = 200          # length of the trial line
MUSIC_CREDITS_PER_S = 900 / 60   # = elevenlabs.MUSIC_CREDITS_PER_MIN / 60 (was 900/80: 25 % low)

# mastering of a text-path render (the same targets as the recording path, vp.adapters.render)
MASTER_LUFS = -14.0
MASTER_TP = -1.0
MASTER_LRA = 11.0
MASTER_TOLERANCE = 0.5      # already within ±0.5 LU and under the peak -> left as it is
CHAT_MAX_BYTES = 14_000_000  # the same cap as `vp review preview` (phone and web uploads)
CHAT_SHORT_SIDE = 720       # the chat copy: short side 720 px, then 480 if it still does not fit
REMOTION_PROJECT = "remotion-video"
REMOTION_ENTRY = "src/index.ts"

_BREAK = re.compile(r"<break\s+time\s*=\s*[\"']?([\d.]+)\s*(ms|s)?[\"']?\s*/?>", re.I)


# ---- the job folder -------------------------------------------------------------------------

def is_text_job(folder: str | Path) -> bool:
    req = read_yaml(Path(folder) / "request.yaml", default={}) or {}
    return isinstance(req, dict) and str(req.get("kind") or "").lower() == KIND


def new_job(folder: str | Path, text_file: str | Path, title: str | None = None) -> dict[str, Any]:
    """Create a text job: request.yaml + source.md. Never overwrites an existing job."""
    folder = Path(folder).expanduser().resolve()
    src = Path(text_file).expanduser().resolve()
    if not src.is_file():
        raise VPError("data", f"text file not found: {src}")
    text = src.read_text(encoding="utf-8")
    if not text.strip():
        raise VPError("data", f"the text file is empty: {src}")
    if (folder / "request.yaml").exists():
        raise VPError("data", f"a job already exists in {folder} (request.yaml); choose a new folder")
    folder.mkdir(parents=True, exist_ok=True)
    if src.parent == folder:
        rel = src.name                       # the text already lives in the job folder: kept as is
    else:
        rel = SOURCE
        if (folder / SOURCE).exists():
            raise VPError("data", f"{folder / SOURCE} already exists; choose a new folder")
        shutil.copyfile(src, folder / SOURCE)
    title = (title or "").strip() or _title_from_text(text) or folder.name
    write_yaml(folder / "request.yaml", {"kind": KIND, "title": title, "enabled": False,
                                         "created_at": now_iso(), "source": {"text": rel}})
    from .projects import safe_touch
    safe_touch(folder)
    return {"folder": str(folder), "title": title, "source": str(folder / rel),
            "estimate": estimate_job(folder)}


def source_path(folder: Path) -> Path:
    req = read_yaml(Path(folder) / "request.yaml", default={}) or {}
    rel = ((req.get("source") or {}) if isinstance(req, dict) else {}).get("text") or SOURCE
    p = Path(str(rel)).expanduser()
    return p if p.is_absolute() else Path(folder) / p


def _title_from_text(text: str) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip().strip("*_").strip()
        if s:
            return s[:60].rstrip()
    return ""


def _files(d: Path, ext: tuple[str, ...]) -> list[Path]:
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in ext
                  and not p.name.startswith("."))


_SCENE_ROW = re.compile(r"^\s*\|\s*\**\s*(S\d+)\s*\**\s*\|([^|]+)\|", re.I)
CUE_MAX = 45                 # subtitle line length when cues are split by code


def split_cues(text: str, limit: int = CUE_MAX) -> list[str]:
    """Subtitle lines from a scene's voice text: split after punctuation, then pack words so a
    line stays within `limit` characters where possible (pure bookkeeping, no model)."""
    text = re.sub(r"\s+", " ", text).strip()
    parts = [p.strip() for p in re.split(r"(?<=[,.;:!?…—])\s+", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) + 1 + len(p) <= limit and not out[-1].endswith((".", "!", "?", "…")):
            out[-1] = f"{out[-1]} {p}"
            continue
        line = ""
        for w in p.split(" "):
            if line and len(line) + 1 + len(w) > limit:
                out.append(line)
                line = w
            else:
                line = f"{line} {w}".strip()
        if line:
            out.append(line)
    return out


def scenes_from_script(folder: Path) -> dict[str, Any] | None:
    """Scenes from the screenplay table of script.md (rows `| S1 | voice text | … |`, as in the
    probe), cues split by code. None when script.md has no such rows."""
    p = Path(folder) / "script.md"
    if not p.is_file():
        return None
    scenes = []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _SCENE_ROW.match(line)
        if m:
            cues = split_cues(voice_text(m.group(2)))
            if cues:
                scenes.append({"id": m.group(1).upper(), "cues": cues})
    return {"fps": FPS, "scenes": scenes, "from": "script.md"} if scenes else None


def load_scenes(folder: Path) -> dict[str, Any] | None:
    """scenes.json if present, else the scene table of script.md, else None."""
    p = Path(folder) / "scenes.json"
    if not p.exists():
        return scenes_from_script(folder)
    try:
        data = read_json(p)
    except ValueError as e:
        raise VPError("data", f"scenes.json is not valid JSON: {e}") from e
    scenes = data.get("scenes") if isinstance(data, dict) else None
    if not isinstance(scenes, list) or not scenes:
        raise VPError("data", "scenes.json: needs {\"scenes\": [{\"id\": ..., \"cues\": [...]}, ...]}")
    for n, s in enumerate(scenes):
        if not isinstance(s, dict) or not s.get("id") or not isinstance(s.get("cues"), list) \
                or not all(isinstance(c, str) and c.strip() for c in s["cues"]) or not s["cues"]:
            raise VPError("data", f"scenes.json: scene {n + 1} needs an id and non-empty cues")
    return data


def scene_voice(folder: Path, scene: dict[str, Any], index: int) -> Path | None:
    """The voice file of a scene: `voice` if given, else voice/<id>.<ext> or voice/sNN.<ext>."""
    if scene.get("voice"):
        p = Path(str(scene["voice"])).expanduser()
        p = p if p.is_absolute() else folder / p
        return p if p.is_file() else None
    files = _files(folder / "voice", AUDIO_EXT)
    want = {str(scene["id"]).lower(), f"s{index + 1:02d}", f"s{index + 1}"}
    for f in files:
        if f.stem.lower() in want:
            return f
    return None


def renders(folder: Path) -> list[Path]:
    return _files(Path(folder) / "renders", (".mp4", ".mov", ".webm"))


def capcut_versions(folder: Path) -> list[str]:
    d = Path(folder) / "capcut"
    if not d.is_dir():
        return []
    vs = [p.name for p in d.iterdir() if p.is_dir() and re.fullmatch(r"v\d+", p.name)
          and (p / "export.json").is_file()]
    return sorted(vs, key=lambda v: int(v[1:]))


def stages_present(folder: str | Path) -> dict[str, bool]:
    folder = Path(folder)
    voice_files = _files(folder / "voice", AUDIO_EXT)
    return {
        "brief": (folder / "brief.md").is_file(),
        "script": (folder / "script.md").is_file() or (folder / "scenes.json").is_file(),
        "sketches": bool(_files(folder / "sketches", IMAGE_EXT)),
        "voice": bool(voice_files),
        "music": bool(_files(folder / "music", AUDIO_EXT)),
        "animation": (folder / "timeline.json").is_file(),
        "render": bool(renders(folder)),
        "capcut": bool(capcut_versions(folder)),
    }


def status(folder: str | Path) -> dict[str, Any]:
    folder = Path(folder).expanduser().resolve()
    if not (folder / "request.yaml").is_file():
        raise VPError("data", f"no job in {folder} (request.yaml missing)")
    if not is_text_job(folder):
        raise VPError("data", f"{folder} is not a text job (request.yaml kind is not 'text')")
    from .position_text import _inspect
    position, have, detail, approved = _inspect(folder)
    nxt = None if position["state"] == "done" else position["engine"]
    action = position["next"]
    req = read_yaml(folder / "request.yaml", default={}) or {}
    return {"folder": str(folder), "title": req.get("title") or folder.name, "stages": have,
            "approved": approved, "next": nxt,
            "next_step": {"what": action["text"], "command": action["command"]},
            "detail": detail, "position": position}


APPROVALS = "approvals.yaml"
SKIP_APPROVALS = ("no_music",)
APPROVABLE = ("brief", "script", "sketches", "voice", "music", "render") + SKIP_APPROVALS


def approvals(folder: Path) -> dict[str, Any]:
    data = read_yaml(Path(folder) / APPROVALS, default={}) or {}
    return data if isinstance(data, dict) else {}


def approve(folder: str | Path, what: str, note: str | None = None) -> dict[str, Any]:
    """Record the person's approval of a stage (e.g. the storyboard sketches) in approvals.yaml."""
    folder = Path(folder).expanduser().resolve()
    if not is_text_job(folder):
        raise VPError("data", f"{folder} is not a text job")
    if what not in APPROVABLE:
        raise VPError("data", f"can approve: {', '.join(APPROVABLE)}")
    if what not in SKIP_APPROVALS and not stages_present(folder).get(what):
        raise VPError("data", f"nothing to approve yet: stage {what} has no files")
    data = approvals(folder)
    data[what] = {"at": now_iso(), **({"note": note} if note else {})}
    write_yaml(folder / APPROVALS, data)
    return data


# ---- estimate -------------------------------------------------------------------------------

def voice_text(text: str) -> str:
    """What the voice will read: markdown marks and break tags removed, spaces collapsed."""
    t = _BREAK.sub(" ", text)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"(?m)^\s{0,3}(#{1,6}|>|[-*+]\s|\d+[.)]\s)", " ", t)
    t = re.sub(r"[*_`~|]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def break_seconds(text: str) -> float:
    total = 0.0
    for v, unit in _BREAK.findall(text):
        total += float(v) / 1000 if (unit or "s").lower() == "ms" else float(v)
    return total


def paragraphs(text: str) -> int:
    return max(1, len([p for p in re.split(r"\n\s*\n", text) if voice_text(p)]))


def estimate(text: str, scenes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Length and ElevenLabs credits before any generation (pure)."""
    if scenes:
        lines = [c for s in scenes["scenes"] for c in s["cues"]]
        spoken = voice_text(" ".join(lines))
        n = len(scenes["scenes"])
        fps = float(scenes.get("fps") or FPS)
        pauses = 0.0
        for k, s in enumerate(scenes["scenes"]):
            lead = s.get("lead_in_frames", LEAD_IN_FRAMES if k == 0 else 0)
            gap = s.get("gap_after_frames", END_HOLD_FRAMES if k == n - 1 else GAP_FRAMES)
            pauses += (float(lead) + float(gap)) / fps
        source = scenes.get("from") or "scenes.json"
        breaks = sum(break_seconds(c) for c in lines)
    else:
        spoken = voice_text(text)
        n = paragraphs(text)
        pauses = LEAD_IN_FRAMES / FPS + (n - 1) * EST_GAP_S + END_HOLD_FRAMES / FPS
        source = "text"
        breaks = break_seconds(text)
    chars = len(spoken)
    voice_s = chars / CHARS_PER_S
    total_s = voice_s + pauses + breaks
    credits_voice = chars
    credits_samples = SAMPLE_VOICES * min(chars, SAMPLE_CHARS)
    credits_music = int(math.ceil(total_s * MUSIC_CREDITS_PER_S))
    return {"chars": chars, "scenes": n, "counted_from": source,
            "voice_s": round(voice_s, 1), "pauses_s": round(pauses + breaks, 1),
            "total_s": round(total_s, 1),
            "credits": {"voice_samples": credits_samples, "voice": credits_voice,
                        "music": credits_music,
                        "total": credits_samples + credits_voice + credits_music},
            "basis": f"{CHARS_PER_S} chars/s (eleven_v4, measured); voice 1 credit/char; "
                     f"samples {SAMPLE_VOICES} voices x up to {SAMPLE_CHARS} chars; "
                     f"music ~{MUSIC_CREDITS_PER_S:.2f} credits/s"}


def estimate_job(folder: str | Path) -> dict[str, Any]:
    folder = Path(folder).expanduser().resolve()
    src = source_path(folder)
    text = src.read_text(encoding="utf-8") if src.is_file() else ""
    scenes = load_scenes(folder)
    if not text.strip() and not scenes:
        raise VPError("data", f"no text in {folder} ({SOURCE} missing or empty)")
    return estimate(text, scenes)


def _mmss(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def estimate_lines(e: dict[str, Any]) -> list[str]:
    c = e["credits"]
    return [f"length ≈ {e['total_s']:g} s ({_mmss(e['total_s'])}): voice ≈ {e['voice_s']:g} s "
            f"for {e['chars']} characters + pauses ≈ {e['pauses_s']:g} s "
            f"({e['scenes']} scene(s), counted from {e['counted_from']})",
            f"ElevenLabs credits ≈ {c['total']}: voice trials ≈ {c['voice_samples']}, "
            f"voice ≈ {c['voice']}, music ≈ {c['music']}",
            f"basis: {e['basis']}"]


# ---- timeline from the measured voice files (port of the probe's ai-race-timeline.py) -------

def spread(cues: list[str], start: float, end: float) -> list[tuple[float, float]]:
    """Cue times over [start, end) in proportion to the cue lengths."""
    total = sum(len(c) for c in cues) or 1
    t, out = start, []
    for c in cues:
        d = (end - start) * len(c) / total
        out.append((t, t + d))
        t += d
    return out


def build_timeline(scenes: dict[str, Any], voice_s: list[float], voice_files: list[str],
                   tail_silence_s: float = TAIL_SILENCE_S) -> dict[str, Any]:
    """Timeline (frames) from the scenes and the measured voice lengths (pure).

    Scene length = lead-in + voice + gap after. Subtitle cues are spread over the voiced part
    (voice minus its tail silence) in proportion to their length; `anchors` pin a cue start to a
    measured second of the voice file. Output = the probe's timeline.json format:
      {fps, durationInFrames, scenes: [{id, from, durationInFrames, leadInFrames, voiceFile,
       voiceFrames, showSubtitles, cues: [{text, from, to}]}]}   (cue frames relative to the scene)
    """
    fps = int(scenes.get("fps") or FPS)
    sc = scenes["scenes"]
    if len(voice_s) != len(sc) or len(voice_files) != len(sc):
        raise VPError("data", f"{len(sc)} scenes but {len(voice_s)} voice lengths")
    out, cursor = [], 0
    for k, (s, vdur, vfile) in enumerate(zip(sc, voice_s, voice_files)):
        cues = [c.strip() for c in s["cues"]]
        lead = int(s.get("lead_in_frames", LEAD_IN_FRAMES if k == 0 else 0))
        gap = int(s.get("gap_after_frames", END_HOLD_FRAMES if k == len(sc) - 1 else GAP_FRAMES))
        voiced = max(0.05, vdur - tail_silence_s)
        anchors = {int(i): float(v) for i, v in (s.get("anchors") or {}).items()
                   if 0 < int(i) < len(cues) and 0 < float(v) < voiced}
        keys = sorted(anchors)
        bounds = [0.0] + [anchors[i] for i in keys] + [voiced]
        if bounds != sorted(bounds):
            raise VPError("data", f"scene {s['id']}: anchors are not in order")
        splits = [0] + keys + [len(cues)]
        times: list[tuple[float, float]] = []
        for a in range(len(splits) - 1):
            times += spread(cues[splits[a]:splits[a + 1]], bounds[a], bounds[a + 1])
        vframes = round(vdur * fps)
        dur = lead + vframes + gap
        out.append({"id": str(s["id"]), "from": cursor, "durationInFrames": dur,
                    "leadInFrames": lead, "voiceFile": vfile, "voiceFrames": vframes,
                    "showSubtitles": bool(s.get("subtitles", True)),
                    "cues": [{"text": c, "from": lead + round(a * fps), "to": lead + round(b * fps)}
                             for c, (a, b) in zip(cues, times)]})
        cursor += dur
    return {"fps": fps, "durationInFrames": cursor, "scenes": out}


def _measure_s(path: Path) -> float:
    from .adapters import media
    return media.probe(path)["duration_ms"] / 1000


def make_timeline(folder: str | Path, measure: Callable[[Path], float] = _measure_s) -> dict[str, Any]:
    """Measure the voice files of scenes.json and write timeline.json."""
    folder = Path(folder).expanduser().resolve()
    sc = load_scenes(folder)
    if not sc:
        raise VPError("data", f"no scenes.json in {folder}: write the screenplay's scenes first")
    files, lengths, missing = [], [], []
    for n, s in enumerate(sc["scenes"]):
        p = scene_voice(folder, s, n)
        if p is None:
            missing.append(str(s["id"]))
            continue
        files.append(p.relative_to(folder).as_posix() if p.is_relative_to(folder) else str(p))
        lengths.append(measure(p))
    if missing:
        raise VPError("data", "no voice file for scene(s): " + ", ".join(missing))
    tl = build_timeline(sc, lengths, files)
    write_json(folder / "timeline.json", tl, indent=1)
    return tl


def load_timeline(folder: Path) -> dict[str, Any]:
    p = Path(folder) / "timeline.json"
    if not p.is_file():
        raise VPError("data", f"no timeline.json in {folder}: run `vp text timeline` first")
    return read_json(p)


# ---- CapCut parts of a text job (used by vp.adapters.capcut) --------------------------------

VOICE_VOLUME = 1.585   # +4 dB: the raw mix measured -18 LUFS, target -14 (probe master)
MUSIC_VOLUME = 0.285   # music ~15 dB under the voice, then the same +4 dB (probe)
MUSIC_LIFT = 1.6       # the baked mix raised the music x1.6 in the pauses (probe Root.tsx)


def pick_render(folder: Path, version: str) -> Path:
    """The picture for a CapCut version: renders/<v>-picture.*, renders/<v>.*, else the newest
    picture-only render, else the newest render."""
    files = [f for f in renders(folder) if not f.stem.endswith("-chat")]   # chat copies: never
    if not files:
        raise VPError("data", f"no render in {folder / 'renders'}")
    for want in (f"{version}-picture", version):
        for f in files:
            if f.stem == want:
                return f
    pics = [f for f in files if f.stem.endswith("-picture")]
    return max(pics or files, key=lambda p: p.stat().st_mtime)


def pick_music(folder: Path) -> Path | None:
    files = _files(Path(folder) / "music", AUDIO_EXT)
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def music_bed_args(src: Path, total_ms: int, fade_in_s: float = 0.5,
                   fade_out_s: float = 1.5) -> list[str]:
    """ffmpeg arguments (without the output path): the music cut to the video length with a fade
    in and out (probe recipe)."""
    t = total_ms / 1000
    out_at = max(0.0, t - fade_out_s)
    return ["-i", str(src), "-vn", "-af",
            f"atrim=0:{t:.3f},asetpts=PTS-STARTPTS,afade=t=in:d={fade_in_s},"
            f"afade=t=out:st={out_at:.3f}:d={fade_out_s}", "-ar", "48000", "-b:a", "192k"]


def capcut_parts(folder: str | Path, version: str, work_dir: Path, *, video: str | Path | None = None,
                 music: str | Path | None = None, voice_volume: float = VOICE_VOLUME,
                 music_volume: float = MUSIC_VOLUME, music_lift: float = MUSIC_LIFT,
                 probe: Callable[[Path], dict] | None = None,
                 make_bed: Callable[[Path, int, Path], Path] | None = None):
    """(ClipTimeline, [AudioClip], inputs) of a text job for a CapCut version.

    Picture + subtitles from timeline.json, every scene's voice at its place on track «Голос»,
    the music bed (cut to length, faded) on track «Музыка» lifted in the voice pauses."""
    from .adapters import media
    from .adapters.edit_timeline import ClipTimeline
    folder = Path(folder).expanduser().resolve()
    probe = probe or media.probe
    tl_json = load_timeline(folder)
    fps = float(tl_json.get("fps") or FPS)
    vpath = Path(video).expanduser().resolve() if video else pick_render(folder, version)
    vinfo = probe(vpath)
    if not vinfo.get("width") or not vinfo.get("height"):
        raise VPError("data", f"{vpath.name} has no picture")
    total = int(round(tl_json["durationInFrames"] * 1000 / fps))
    dur = min(int(vinfo["duration_ms"]), total) if vinfo.get("duration_ms") else total
    cues, audio, inputs = capcut_audio_cues(folder, work_dir, music=music, probe=probe,
                                            make_bed=make_bed, voice_volume=voice_volume,
                                            music_volume=music_volume, music_lift=music_lift,
                                            total_ms=dur)
    w, h = int(vinfo["width"]), int(vinfo["height"])
    kind = "reel" if h > w else "long"
    req = read_yaml(folder / "request.yaml", default={}) or {}
    tl = ClipTimeline(item=version, kind=kind, title=str(req.get("title") or folder.name),
                      video_path=vpath, duration_ms=dur, width=w, height=h,
                      fps=float(vinfo.get("fps") or fps), has_audio=bool(vinfo.get("has_audio")),
                      canvas_w=w, canvas_h=h, cues=cues)
    return tl, audio, dict(inputs, video=str(vpath))


def capcut_audio_cues(folder: str | Path, work_dir: Path, *, music: str | Path | None = None,
                      voice_volume: float = VOICE_VOLUME, music_volume: float = MUSIC_VOLUME,
                      music_lift: float = MUSIC_LIFT, total_ms: int | None = None,
                      probe: Callable[[Path], dict] | None = None,
                      make_bed: Callable[[Path, int, Path], Path] | None = None):
    """(subtitle cues, [AudioClip], inputs) of a text job, all timed from timeline.json by code.

    Every scene's voice at its place on track «Голос»; the music bed (cut to length, faded) on
    track «Музыка», lifted in the voice pauses. `total_ms` = the length to cover (default the
    timeline's)."""
    from .adapters import capcut, media
    from .core.srt import Cue
    folder = Path(folder).expanduser().resolve()
    probe = probe or media.probe
    tl_json = load_timeline(folder)
    fps = float(tl_json.get("fps") or FPS)

    def ms(fr: float) -> int:
        return int(round(fr * 1000 / fps))

    dur = total_ms if total_ms is not None else ms(tl_json["durationInFrames"])
    cues, voice, windows = [], [], []
    for s in tl_json["scenes"]:
        if s.get("showSubtitles", True):
            for c in s["cues"]:
                a, b = ms(s["from"] + c["from"]), ms(s["from"] + c["to"])
                if b > a and a < dur:
                    cues.append(Cue(id=str(len(cues) + 1), start_ms=a, end_ms=min(b, dur),
                                    text=c["text"]))
        vf = Path(s["voiceFile"])
        vf = vf if vf.is_absolute() else folder / vf
        if not vf.is_file():
            raise VPError("data", f"voice file missing: {vf}")
        at = ms(s["from"] + s["leadInFrames"])
        vlen = int(probe(vf)["duration_ms"])
        voice.append(capcut.AudioClip(path=vf, at_ms=at, duration_ms=vlen, volume=voice_volume,
                                      track="Голос"))
        windows.append((at, at + ms(s["voiceFrames"])))
    audio = list(voice)
    msrc = Path(music).expanduser().resolve() if music else pick_music(folder)
    bed = None
    if msrc:
        if not msrc.is_file():
            raise VPError("data", f"music file not found: {msrc}")
        work_dir.mkdir(parents=True, exist_ok=True)
        bed = (make_bed or _make_bed)(msrc, dur, work_dir / "music-bed.mp3")
        keys = capcut.music_keyframes(windows, dur, music_volume, music_lift)
        audio.append(capcut.AudioClip(path=bed, at_ms=0, duration_ms=dur, volume=music_volume,
                                      track="Музыка", keyframes=keys))
    inputs = {"voice": [str(c.path) for c in voice],
              "music": str(msrc) if msrc else None, "music_bed": str(bed) if bed else None,
              "volumes": {"voice": voice_volume, "music": music_volume, "music_lift": music_lift}}
    return cues, audio, inputs


def _make_bed(src: Path, total_ms: int, out: Path) -> Path:
    from .adapters import media
    return media._render_to(out, music_bed_args(src, total_ms), timeout_s=600)


# ---- render, loudness and the chat copy (test signals 2026-11-21: M7, M9, M11) ---------------
# The render used to be `npx remotion render`, which the install guard blocks, and nothing
# mastered its loudness (−19.9 LUFS measured) or made a copy small enough for the chat. Now the
# engine renders with its own Node, Chrome and Remotion workspace, then masters and copies.

_RENDER_STEM = re.compile(r"(v\d+)(-picture|-chat)?")
_COMPOSITION = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _text_folder(folder: str | Path) -> Path:
    folder = Path(folder).expanduser().resolve()
    if not is_text_job(folder):
        raise VPError("data", f"{folder} is not a text job (request.yaml kind is not 'text')")
    return folder


def render_versions(folder: Path) -> list[str]:
    """Versions that have a sound render renders/vN.mp4, oldest first."""
    vs = {p.stem for p in renders(folder) if p.suffix.lower() == ".mp4"
          and re.fullmatch(r"v\d+", p.stem)}
    return sorted(vs, key=lambda v: int(v[1:]))


def next_render_version(folder: Path) -> str:
    nums = [int(m.group(1)[1:]) for p in renders(folder) if (m := _RENDER_STEM.fullmatch(p.stem))]
    return f"v{max(nums, default=0) + 1}"


def timing(folder: str | Path) -> dict[str, Any]:
    """Wall-clock time from text job creation to the first successful master of each version."""
    folder = _text_folder(folder)
    req = read_yaml(folder / "request.yaml")
    started_at = req.get("created_at")
    start = parse_iso(str(started_at)) if started_at else None
    versions = []
    for version in render_versions(folder):
        stamp = read_json(folder / "renders" / f"{version}.timing.json", default={})
        finished_at = stamp.get("finished_at")
        elapsed = (int((parse_iso(finished_at) - start).total_seconds())
                   if start is not None and finished_at else None)
        versions.append({"version": version, "finished_at": finished_at, "elapsed_s": elapsed})
    return {"started_at": str(started_at) if started_at else None, "versions": versions}


def chat_copy(src: Path, out: Path, max_bytes: int = CHAT_MAX_BYTES,
              info: dict[str, Any] | None = None) -> tuple[Path, bool]:
    """A copy small enough to send into the chat: H.264 at a bitrate that fits `max_bytes`, short
    side 720 px (then 480 px with a smaller budget). Returns (path, fits)."""
    from .adapters import media
    info = info or media.probe(src)
    dur = max(0.5, int(info.get("duration_ms") or 0) / 1000)
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    total = int(0.90 * max_bytes * 8 / dur)
    audio = min(128_000, max(32_000, total // 8))
    video = max(100_000, total - audio)
    for short, share in ((CHAT_SHORT_SIDE, 1.0), (480, 0.75)):
        vb = int(video * share)
        if w and h and min(w, h) <= short:
            scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        else:
            scale = f"scale={short}:-2" if w <= h else f"scale=-2:{short}"
        media._render_to(out, ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", scale,
                               "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(vb),
                               "-maxrate", str(int(vb * 1.2)), "-bufsize", str(vb * 2),
                               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", str(audio),
                               "-movflags", "+faststart"])
        if out.stat().st_size <= max_bytes:
            return out, True
    return out, False


def master(folder: str | Path, version: str | None = None,
           chat_max_bytes: int = CHAT_MAX_BYTES) -> dict[str, Any]:
    """Finish a text-path render renders/<v>.mp4: loudness to −14 LUFS (two-pass loudnorm, the
    picture is copied, nothing re-rendered), the picture-only file <v>-picture.mp4 for CapCut when
    missing, and the chat copy <v>-chat.mp4 when <v>.mp4 is bigger than the chat takes.
    Returns the measured loudness and the file to send into the chat (`chat`).
    Run signal (vp.runsignal): step `master`, then `video_ready` — the finished video exists."""
    from . import runsignal
    from .asks import VERSION_REMINDER
    folder = _text_folder(folder)
    with runsignal.step(folder, "master"):
        res = _master(folder, version, chat_max_bytes)
        done = res.get("version") or version
        if done:                      # first successful master of this version = its finish time
            stamp = folder / "renders" / f"{done}.timing.json"
            if not stamp.exists():
                write_json(stamp, {"finished_at": now_iso()})
    runsignal.video_ready(folder)
    return {**res, "asks_reminder": VERSION_REMINDER}


def _master(folder: Path, version: str | None, chat_max_bytes: int) -> dict[str, Any]:
    from .adapters import media
    have = render_versions(folder)
    version = version or (have[-1] if have else None)
    if not version:
        raise VPError("data", f"no render with sound in {folder / 'renders'} (renders/vN.mp4)")
    if not re.fullmatch(r"v\d+", version):
        raise VPError("data", f"version must look like v1, v2 …, got {version!r}")
    src = folder / "renders" / f"{version}.mp4"
    if not src.is_file():
        raise VPError("data", f"no renders/{version}.mp4 (the render with voice and music)")
    info = media.probe(src)
    if not info.get("has_audio"):
        raise VPError("data", f"renders/{version}.mp4 has no sound: render voice and music into it")
    m = media.loudnorm_measure(src, MASTER_LUFS, MASTER_TP, MASTER_LRA)
    before, peak = float(m["input_i"]), float(m["input_tp"])
    if not math.isfinite(before) or before < -70:
        raise VPError("data", f"renders/{version}.mp4 is silent: the voice and music did not get "
                              f"into the render")
    after, mastered = before, False
    if abs(before - MASTER_LUFS) > MASTER_TOLERANCE or peak > MASTER_TP:
        tmp = src.with_name(f".{src.stem}.master{src.suffix}")
        try:
            media.loudnorm_apply(src, tmp, m, MASTER_LUFS, MASTER_TP, MASTER_LRA)
            os.replace(tmp, src)
        finally:
            tmp.unlink(missing_ok=True)
        after = float(media.loudnorm_measure(src, MASTER_LUFS, MASTER_TP, MASTER_LRA)["input_i"])
        mastered = True
    picture = folder / "renders" / f"{version}-picture.mp4"
    if not picture.is_file():
        media._render_to(picture, ["-i", str(src), "-map", "0:v:0", "-c:v", "copy", "-an",
                                   "-movflags", "+faststart"])
    chat_file = folder / "renders" / f"{version}-chat.mp4"
    size = src.stat().st_size
    if size <= chat_max_bytes:
        chat_file.unlink(missing_ok=True)       # a copy of an earlier take of this version
        chat, fits = src, True
    else:
        chat, fits = chat_copy(src, chat_file, chat_max_bytes, info)
    return {"version": version, "render": str(src), "picture": str(picture),
            "lufs_before": round(before, 1), "lufs": round(after, 1), "target_lufs": MASTER_LUFS,
            "mastered": mastered, "chat": str(chat), "chat_mb": round(chat.stat().st_size / 1e6, 1),
            "fits_chat": fits}


def link_node_modules(project: Path, target: Path) -> str:
    """The job's Remotion project uses the engine's installed packages: node_modules -> the
    workspace's (no npm install in the job). A project with its own real node_modules is left."""
    nm = project / "node_modules"
    if nm.is_symlink():
        if nm.resolve() == target.resolve():
            return "linked"
        nm.unlink()
    elif nm.exists():
        return "own"
    nm.symlink_to(target, target_is_directory=True)
    return "linked"


def render(folder: str | Path, composition: str, version: str | None = None,
           project: str | Path | None = None, entry: str = REMOTION_ENTRY, props: bool = True,
           force: bool = False, workspace: Any = None,
           master_fn: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Render a composition of the job's own Remotion project into renders/<v>.mp4 with the
    engine's Node, Chrome and Remotion (the permitted way: never `npx`), with timeline.json as
    the props, then `master`. A version is never overwritten without `force`."""
    folder = _text_folder(folder)
    if not _COMPOSITION.fullmatch(composition or ""):
        raise VPError("data", f"composition id must be letters, digits, - or _: {composition!r}")
    version = version or next_render_version(folder)
    if not re.fullmatch(r"v\d+", version):
        raise VPError("data", f"version must look like v1, v2 …, got {version!r}")
    out = folder / "renders" / f"{version}.mp4"
    if out.exists() and not force:
        raise VPError("data", f"renders/{version}.mp4 already exists: a version is never "
                              f"overwritten — render {next_render_version(folder)} "
                              f"(or --force to replace {version})")
    proj = Path(project).expanduser() if project else folder / REMOTION_PROJECT
    proj = (proj if proj.is_absolute() else folder / proj).resolve()
    if not (proj / entry).is_file():
        raise VPError("data", f"no Remotion project: {proj / entry} is missing (write the "
                              f"compositions there; src/index.ts calls registerRoot)")
    from . import runsignal
    from .core import progress
    with runsignal.step(folder, "render"):      # run signal: the engine's part of the stage
        if workspace is None:
            from .adapters.render_remotion import RemotionWorkspace
            from .core.config import load_config
            workspace = RemotionWorkspace(load_config())
        workspace.ensure(log=lambda s: print(s, file=sys.stderr))      # stdout stays the result
        link_node_modules(proj, Path(workspace.dir) / "node_modules")
        tmp = folder / "renders" / f".{version}.rendering.mp4"
        settings = getattr(workspace, "settings", {}) or {}
        args = ["render", entry, composition, str(tmp), "--log=info",
                f"--crf={int(settings.get('crf') or 16)}", "--enforce-audio-track"]
        frames = None
        tl = folder / "timeline.json"
        if props and tl.is_file():
            args.append(f"--props={tl}")
            frames = (read_json(tl) or {}).get("durationInFrames")
        if settings.get("concurrency"):
            args.append(f"--concurrency={settings['concurrency']}")
        if settings.get("gl"):
            args.append(f"--gl={settings['gl']}")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            with progress.tracking(folder, "render", version, total=float(frames or 1),
                                   unit="frames"):
                workspace.run(args, frames=int(frames) if frames else None, cwd=proj)
            if not tmp.is_file():
                raise VPError("data", f"remotion wrote no {out.name}")
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
    for stale in (f"{version}-picture.mp4", f"{version}-chat.mp4"):   # of an earlier take
        (folder / "renders" / stale).unlink(missing_ok=True)
    with progress.tracking(folder, "master", version, total=1, unit="files"):
        res = (master_fn or master)(folder, version)
    return {"composition": composition, "project": str(proj), **res}


# ---- CLI ------------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp text", description="text-only jobs (text -> video)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="create a text job from a text file")
    n.add_argument("job")
    n.add_argument("--file", required=True)
    n.add_argument("--title")
    n.add_argument("--scenario", help="the scenario letter of the route (vp scenario plan), "
                                      "remembered for the job")
    n.add_argument("--json", action="store_true")
    for name, hlp in (("status", "where the text path stands"),
                      ("estimate", "length and ElevenLabs credits before generation"),
                      ("timing", "time from job creation to each finished render"),
                      ("timeline", "measure the voice files, write timeline.json")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("job")
        s.add_argument("--json", action="store_true")
    ap_ = sub.add_parser("approve", help="record the person's approval of a stage (e.g. sketches)")
    ap_.add_argument("job")
    ap_.add_argument("what", choices=APPROVABLE)
    ap_.add_argument("--note")
    ap_.add_argument("--json", action="store_true")
    r = sub.add_parser("render", help="render the job's Remotion composition with the engine "
                                      "(never npx), then master it")
    r.add_argument("job")
    r.add_argument("--composition", required=True, help="the composition id in src/index.ts")
    r.add_argument("--version", help="vN (default: the next free version)")
    r.add_argument("--project", help=f"the Remotion project folder (default <job>/{REMOTION_PROJECT})")
    r.add_argument("--entry", default=REMOTION_ENTRY)
    r.add_argument("--no-props", action="store_true", help="do not pass timeline.json as props")
    r.add_argument("--force", action="store_true", help="replace an existing renders/<vN>.mp4")
    r.add_argument("--json", action="store_true")
    m = sub.add_parser("master", help="loudness −14 LUFS, the picture-only file and the chat copy")
    m.add_argument("job")
    m.add_argument("--version", help="vN (default: the newest render)")
    m.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "timing":
        res = timing(a.job)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(f"started: {res['started_at'] or 'not recorded'}")
            if not res["versions"]:
                print("no render versions yet")
            for row in res["versions"]:
                elapsed = row["elapsed_s"]
                duration = f"{elapsed} s ({_mmss(elapsed)})" if elapsed is not None else "unknown"
                print(f"{row['version']}: {row['finished_at'] or 'finish not recorded'}"
                      f" — elapsed: {duration}")
        return 0
    from . import runsignal
    try:
        return _main(a)
    finally:
        runsignal.observe_text(a.job)       # run signal: the stages that appeared on disk


def _main(a: argparse.Namespace) -> int:
    from . import runsignal
    if a.cmd in ("render", "master"):
        if a.cmd == "render":
            res = render(a.job, a.composition, a.version, a.project, a.entry,
                         props=not a.no_props, force=a.force)
        else:
            res = master(a.job, a.version)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(f"{'RENDERED' if a.cmd == 'render' else 'MASTERED'} {res['render']}")
            print(f"loudness: {res['lufs_before']:g} -> {res['lufs']:g} LUFS "
                  f"(target {res['target_lufs']:g})")
            print(f"picture only: {res['picture']}")
            print(f"send into the chat: {res['chat']} ({res['chat_mb']:g} MB"
                  + ("" if res["fits_chat"] else ", STILL TOO BIG for the chat: open it instead")
                  + ")")
            print(res["asks_reminder"])
        return 0
    if a.cmd == "approve":
        with runsignal.step(a.job, "approve_sketches") if a.what == "sketches" \
                else contextlib.nullcontext():
            res = approve(a.job, a.what, a.note)
        print(json.dumps(res, ensure_ascii=False) if a.json else f"APPROVED {a.what}")
        return 0

    if a.cmd == "new":
        res = new_job(a.job, a.file, a.title)
        if a.scenario:
            res["scenario_stored"] = runsignal.set_scenario(res["folder"], a.scenario)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(f"CREATED {res['folder']}\ttitle: {res['title']}")
            for line in estimate_lines(res["estimate"]):
                print(line)
        return 0
    if a.cmd == "status":
        res = status(a.job)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(f"job: {res['title']}\t{res['folder']}")
            for s in STAGES:
                print(f"  {'done' if res['stages'][s] else '-':<5} {s}")
            d = res["detail"]
            if "scenes" in d:
                print(f"scenes: {d['scenes']}, voiced: {d['voiced_scenes']}")
            if d.get("scenes_error"):
                print(f"WARNING {d['scenes_error']}")
            if d["capcut_versions"]:
                print("CapCut versions: " + ", ".join(d["capcut_versions"]))
            if res["approved"]:
                print("approved: " + ", ".join(res["approved"]))
            pos = res["position"]
            nxt = res["next"]
            if nxt is None:
                nxt = "all stages done (new versions on request)" if pos["state"] == "done" else "unknown"
            print(f"next: {nxt} — {res['next_step']['what']}")
            print(f"command: {res['next_step']['command'] or '—'}")
            place = f" ({pos['index']}/{pos['total']})" if pos["index"] is not None else ""
            title = pos["title"]["en"] if pos["title"] else "unknown step"
            focus = pos["focus"]
            count = ""
            if pos["step"] in ("sketches", "sketch_gate", "voice"):
                count = f" · {focus['items_at_step']}/{focus['items_total']} scenes"
            print(f"position: {focus['version']} · {title}{place} · {pos['state']}{count}")
            if pos["waiting_for"]:
                print(f"waiting for: {pos['waiting_for']['text']}")
            action = pos["next"]
            print(f"next action: {action['action']} — {action['text']}")
        return 0
    if a.cmd == "estimate":
        res = estimate_job(a.job)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            for line in estimate_lines(res):
                print(line)
        return 0
    with runsignal.step(a.job, "animation"):
        tl = make_timeline(a.job)
    if a.json:
        print(json.dumps(tl, ensure_ascii=False, indent=1))
    else:
        print(f"timeline.json: {len(tl['scenes'])} scenes, {tl['durationInFrames']} frames = "
              f"{tl['durationInFrames'] / tl['fps']:.1f} s")
    return 0
