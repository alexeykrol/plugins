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
                      Remotion composition is laid out on it
  renders/vN*.mp4     stage "render" (vN.mp4 = sound baked in; vN-picture.mp4 = picture only)
  capcut/vN/          stage "capcut": one folder per exported CapCut version (vp capcut)

scenes.json (written in the dialog; texts only — no times, the times come from the voice):
  {"fps": 30, "scenes": [{"id": "S1", "cues": ["subtitle line", ...],
     "voice": "voice/s01.mp3"            (optional; default voice/<id>.mp3 or voice/sNN.mp3)
     "lead_in_frames": 15, "gap_after_frames": 10   (optional; defaults below)
     "subtitles": true                    (optional)
     "anchors": {"3": 6.25}}]}            (optional, measured by code or the person: cue 3
                                           starts at 6.25 s into the voice file)

Estimate (measured in the probe, eleven_v4, voice «Алина»): ≈ 14.8 characters per second of voice;
pauses: a lead-in, a gap between scenes, a hold at the end (frame counts of the probe), plus any
ElevenLabs <break time="Xs"/> tags. Credits: voice 1 per character (v4); voice trials ≈ 4 voices
× one line of up to 200 characters (probe ≈ 800); music ≈ 900 per 80 s (probe).

CLI:  vp text new <job> --file <text.md> [--title T] [--json]
      vp text status <job> [--json]          stages, the next step and its exact command
      vp text approve <job> sketches|… [--note T]
      vp text estimate <job> [--json]
      vp text timeline <job> [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from .core.errors import VPError
from .core.io import now_iso, read_json, read_yaml, write_json, write_yaml

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
SAMPLE_VOICES = 4           # voice trials: 4 different voices, one take each (K9)
SAMPLE_CHARS = 200          # length of the trial line
MUSIC_CREDITS_PER_S = 900 / 80

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
    have = stages_present(folder)
    nxt = next((s for s in STAGES if not have[s]), None)
    detail: dict[str, Any] = {}
    try:
        sc = load_scenes(folder)
    except VPError as e:
        sc, detail["scenes_error"] = None, str(e)
    if sc:
        scenes = sc["scenes"]
        voiced = sum(1 for n, s in enumerate(scenes) if scene_voice(folder, s, n))
        detail["scenes"] = len(scenes)
        detail["voiced_scenes"] = voiced
    detail["sketches"] = len(_files(folder / "sketches", IMAGE_EXT))
    detail["renders"] = [p.name for p in renders(folder)]
    detail["capcut_versions"] = capcut_versions(folder)
    approved = approvals(folder)
    # the sketches gate: production (voice onwards) waits for the person's approval
    if have["sketches"] and "sketches" not in approved and nxt in STAGES[3:]:
        nxt = "approve_sketches"
    req = read_yaml(folder / "request.yaml", default={}) or {}
    return {"folder": str(folder), "title": req.get("title") or folder.name, "stages": have,
            "approved": approved, "next": nxt, "next_step": next_step(folder, nxt),
            "detail": detail}


APPROVALS = "approvals.yaml"
APPROVABLE = ("brief", "script", "sketches", "voice", "music", "render")


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
    if not stages_present(folder).get(what):
        raise VPError("data", f"nothing to approve yet: stage {what} has no files")
    data = approvals(folder)
    data[what] = {"at": now_iso(), **({"note": note} if note else {})}
    write_yaml(folder / APPROVALS, data)
    return data


def _q(p: Path) -> str:
    s = str(p)
    return f'"{s}"' if re.search(r"[\s'\"$`\\]", s) else s


def next_step(folder: Path, nxt: str | None) -> dict[str, str]:
    """What to do next and the exact command (or the file to write) for it."""
    j = _q(folder)
    nv = f"v{len(capcut_versions(folder)) + 1}"
    steps = {
        "brief": ("write the brief: goal, format (9:16), length, tone, sound, credit budget "
                  f"(estimate: vp text estimate {j})", f"write {_q(folder / 'brief.md')}"),
        "script": ("write the screenplay: a table `| S1 | voice text | what is on screen | ≈ s |` "
                   "(the voice text of each scene becomes its subtitles)",
                   f"write {_q(folder / 'script.md')}"),
        "sketches": ("draw a static sketch of every scene with Remotion stills and show them in the "
                     "chat", f"render stills to {_q(folder / 'sketches')}/S01.png …"),
        "approve_sketches": ("ask the person to approve the sketches; on «yes» record it",
                             f"vp text approve {j} sketches"),
        "voice": ("voice every scene with ElevenLabs (one file per scene; the voice_id comes from "
                  "`vp voice list`, chosen by the person from three samples)",
                  f"vp voice scenes {j} --voice <voice_id> ; then vp text status {j}"),
        "music": ("make one music track for the whole video (ElevenLabs, paid plans; or the "
                  "person's own file in music/)",
                  f"vp music make {j} --prompt-file {_q(folder / 'music' / 'prompt.txt')} "
                  f"--seconds <video length> --out {_q(folder / 'music')}/music.mp3"),
        "animation": ("measure the voice files and lay out the scenes (subtitle timing by code)",
                      f"vp text timeline {j}"),
        "render": ("animate the scenes in Remotion on timeline.json and render: "
                   f"{nv}-picture.mp4 (no sound) and {nv}.mp4 (voice+music); check before showing",
                   f"npx remotion render <composition> {_q(folder / 'renders')}/{nv}.mp4 ; "
                   f"vp check picture {_q(folder / 'renders')}/{nv}.mp4"),
        "capcut": ("export the CapCut project with separate tracks (picture, subtitles, voice, "
                   "music)", f"vp capcut {j} --version {nv}"),
    }
    if nxt is None:
        return {"what": "all stages done: on a change, render a new version and export it",
                "command": f"vp capcut {j} --version {nv}"}
    what, cmd = steps[nxt]
    return {"what": what, "command": cmd}


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
    files = renders(folder)
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
    from .adapters import capcut, media
    from .adapters.edit_timeline import ClipTimeline
    from .core.srt import Cue
    folder = Path(folder).expanduser().resolve()
    probe = probe or media.probe
    tl_json = load_timeline(folder)
    fps = float(tl_json.get("fps") or FPS)

    def ms(fr: float) -> int:
        return int(round(fr * 1000 / fps))

    vpath = Path(video).expanduser().resolve() if video else pick_render(folder, version)
    vinfo = probe(vpath)
    if not vinfo.get("width") or not vinfo.get("height"):
        raise VPError("data", f"{vpath.name} has no picture")
    total = ms(tl_json["durationInFrames"])
    dur = min(int(vinfo["duration_ms"]), total) if vinfo.get("duration_ms") else total
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
    w, h = int(vinfo["width"]), int(vinfo["height"])
    kind = "reel" if h > w else "long"
    req = read_yaml(folder / "request.yaml", default={}) or {}
    tl = ClipTimeline(item=version, kind=kind, title=str(req.get("title") or folder.name),
                      video_path=vpath, duration_ms=dur, width=w, height=h,
                      fps=float(vinfo.get("fps") or fps), has_audio=bool(vinfo.get("has_audio")),
                      canvas_w=w, canvas_h=h, cues=cues)
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
    inputs = {"video": str(vpath), "voice": [str(c.path) for c in voice],
              "music": str(msrc) if msrc else None, "music_bed": str(bed) if bed else None,
              "volumes": {"voice": voice_volume, "music": music_volume, "music_lift": music_lift}}
    return tl, audio, inputs


def _make_bed(src: Path, total_ms: int, out: Path) -> Path:
    from .adapters import media
    return media._render_to(out, music_bed_args(src, total_ms), timeout_s=600)


# ---- CLI ------------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp text", description="text-only jobs (text -> video)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="create a text job from a text file")
    n.add_argument("job")
    n.add_argument("--file", required=True)
    n.add_argument("--title")
    n.add_argument("--json", action="store_true")
    for name, hlp in (("status", "where the text path stands"),
                      ("estimate", "length and ElevenLabs credits before generation"),
                      ("timeline", "measure the voice files, write timeline.json")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("job")
        s.add_argument("--json", action="store_true")
    ap_ = sub.add_parser("approve", help="record the person's approval of a stage (e.g. sketches)")
    ap_.add_argument("job")
    ap_.add_argument("what", choices=APPROVABLE)
    ap_.add_argument("--note")
    ap_.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "approve":
        res = approve(a.job, a.what, a.note)
        print(json.dumps(res, ensure_ascii=False) if a.json else f"APPROVED {a.what}")
        return 0

    if a.cmd == "new":
        res = new_job(a.job, a.file, a.title)
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
            print(f"next: {res['next'] or 'all stages done (new versions on request)'} — "
                  f"{res['next_step']['what']}")
            print(f"command: {res['next_step']['command']}")
        return 0
    if a.cmd == "estimate":
        res = estimate_job(a.job)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            for line in estimate_lines(res):
                print(line)
        return 0
    tl = make_timeline(a.job)
    if a.json:
        print(json.dumps(tl, ensure_ascii=False, indent=1))
    else:
        print(f"timeline.json: {len(tl['scenes'])} scenes, {tl['durationInFrames']} frames = "
              f"{tl['durationInFrames'] / tl['fps']:.1f} s")
    return 0
