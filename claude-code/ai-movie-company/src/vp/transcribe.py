"""`vp transcribe` — speech recognition for a recording that came without subtitles (owner
decision 2026-11-20): ElevenLabs Speech-to-Text (Scribe) with word timestamps -> the job contract
`_cut.json` (Whisper verbose_json-like word list, read by S0/S2 `load_words`) and `_cut.srt`
(phrases built from the words by code). The raw text then goes through S1 proofreading as usual:
Claude cleans the text, the timings stay the recognizer's.

  vp transcribe estimate <job> [--video F] [--json]      duration x price, before anything is sent
  vp transcribe run <job> [--video F] [--language ru] [--diarize] [--force] [--json]
  vp transcribe import <job> <file.srt> [--json]         the person's own subtitles -> <job>/_cut.srt
  vp transcribe key status [--json]                      is an ElevenLabs key in place (never shown)
  vp transcribe key set --from-clipboard | --stdin       save the key; it never passes through the chat
                                                         (alias of `vp keys status|set elevenlabs`)

API (checked 2026-11-20):
  https://elevenlabs.io/docs/api-reference/speech-to-text/convert  POST /v1/speech-to-text,
      multipart: model_id, file (< 5.0 GB, audio or video), language_code (null = auto),
      diarize, timestamps_granularity=word, tag_audio_events; header xi-api-key.
      Response: {language_code, language_probability, text, words: [{text, type
      (word|spacing|audio_event), start, end (seconds, nullable), speaker_id, logprob}],
      transcription_id, audio_duration_secs}.
  https://elevenlabs.io/docs/overview/models#scribe-v3  `scribe_v3` = the current model for
      recorded audio (successor of scribe_v2, same API).
  https://elevenlabs.io/docs/capabilities/speech-to-text  limits: 5 GB per file, 15 h per file;
      billed per second of audio sent, silence included.
  https://elevenlabs.io/pricing/api  Scribe $0.20 per hour of audio (pay-as-you-go, API).

Bookkeeping is code: the key comes from the common key store (vp.keys, service "elevenlabs": env
ELEVENLABS_API_KEY / XI_API_KEY, then <vp_home>/keys/elevenlabs.key, 0600; the older
<vp_home>/elevenlabs_api_key is moved there on first read) and is never printed, logged or written
into the job folder; the model sees no timings. The same key serves `vp voice` and `vp music`.
Outputs are written atomically; `_cut.srt` is written last (its presence = done). The raw
response is kept in <job>/transcript.raw.json, so a lost conversion is rebuilt without paying
again; <job>/transcript.yaml records which service and model made the transcript.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from .core import progress
from .core.errors import VPError
from .core.io import atomic_write, now_iso, read_json, write_json, write_yaml
from .core.lock import job_lock
from .core.paths import Job, vp_home
from .core.srt import Cue, format_srt, read_srt, srt_duration_ms
from .steps._s2_retime import LINE, MAX_CHARS, MAX_SPAN, MIN_DUR, GAP, SENT_MIN, load_words, wrap

SERVICE = "elevenlabs"
API_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL_ID = "scribe_v3"              # https://elevenlabs.io/docs/overview/models#scribe-v3
PRICE_USD_PER_HOUR = 0.20           # https://elevenlabs.io/pricing/api (Scribe, pay-as-you-go), 2026-11-20
PRICE_SOURCE = "https://elevenlabs.io/pricing/api (Scribe $0.20/h, checked 2026-11-20)"
MAX_UPLOAD_BYTES = 4_900_000_000    # API: "less than 5.0GB"; a margin for the multipart envelope
MAX_DURATION_S = 15 * 3600          # API: 15 h per file (standard mode)
KEY_ENV = "ELEVENLABS_API_KEY"
KEY_FILE = "elevenlabs_api_key"
RAW_NAME = "transcript.raw.json"
META_NAME = "transcript.yaml"
TIMEOUT_S = 3600                    # one socket wait; a 2 h recording takes minutes
CHUNK = 1 << 20

# phrase building (subtitle limits are those of S2, so S2 regroups without surprises)
PAUSE_HARD_MS = 1000                # = S2 BREAK_GAP: a pause this long always ends a phrase
PAUSE_SOFT_MS = 500                 # a shorter pause ends a phrase that already has SENT_MIN chars
_SENT_END = re.compile(r'[.!?…]["»”)]?$')
_CLAUSE_END = re.compile(r'[,;:—–]["»”)]?$')
_HAS_WORD = re.compile(r"\w")

Urlopen = Callable[..., Any]


# ---- the key -----------------------------------------------------------------------------------
# One ElevenLabs key serves recognition, voice and music: it lives in the common key store
# (vp.keys, service "elevenlabs": env ELEVENLABS_API_KEY / XI_API_KEY, then
# <vp_home>/keys/elevenlabs.key). The older <vp_home>/elevenlabs_api_key is moved there on its
# first read. `vp transcribe key ...` stays as an alias of `vp keys ... elevenlabs`.

def key_path() -> Path:
    """Where a saved key lives (the common key store)."""
    from . import keys
    return keys.key_file(SERVICE)


def legacy_key_path() -> Path:
    return vp_home() / KEY_FILE


def find_key() -> tuple[str | None, str | None]:
    """(key, where) — where is "env" | "file" | None. The key itself must never reach stdout,
    logs or job files."""
    from . import keys
    key, src = keys.lookup(SERVICE)
    if not key:
        return None, None
    return key, ("env" if str(src).startswith("env") else "file")


def fingerprint(key: str) -> str:
    """First 8 hex of sha256 — enough to tell keys apart, useless to use."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def valid_key_shape(key: str) -> bool:
    """A plausible API key (shape only — the API is the real check); the common store's rule."""
    from . import keys
    try:
        keys.validate(key)
        return True
    except VPError:
        return False


def save_key(key: str) -> dict[str, Any]:
    from . import keys
    try:
        res = keys.save_key(SERVICE, key)
    except VPError as e:
        raise VPError("data", f"that does not look like an ElevenLabs API key: {e.msg}") from None
    return {"saved": True, "where": "file", "fingerprint": res["fingerprint"]}


def key_status() -> dict[str, Any]:
    key, where = find_key()
    return {"present": bool(key), "where": where, "fingerprint": fingerprint(key) if key else None}


def _scrub(text: str, key: str | None) -> str:
    """Remove the key from any text that may be shown (error bodies, exception messages)."""
    if key and key in text:
        text = text.replace(key, "***")
    return text


# ---- estimate ----------------------------------------------------------------------------------

def cost_usd(duration_s: float) -> float:
    """Price of `duration_s` seconds of audio, rounded up to the cent (billing is per second)."""
    cents = duration_s / 3600 * PRICE_USD_PER_HOUR * 100
    return max(0.01, -(-int(cents * 1000) // 1000) / 100) if duration_s > 0 else 0.0


def _video_path(job: Job, video: str | os.PathLike | None) -> Path:
    if video:
        p = Path(video).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p)
    return job.source_video


def outputs(job: Job) -> dict[str, Path]:
    return {"srt": job.source_srt, "words": job.source_words}


def estimate(job: Job, video: str | os.PathLike | None = None,
             probe: Callable[[Path], dict[str, Any]] | None = None) -> dict[str, Any]:
    """What a transcription of this job's recording would cost — nothing is sent."""
    vp = _video_path(job, video)
    if not vp.exists():
        raise VPError("data", f"recording not found: {vp.name}")
    if probe is None:
        from .adapters import media   # ffprobe only when needed
        probe = media.probe
    info = probe(vp)
    if not info.get("has_audio"):
        raise VPError("data", "the recording has no sound track: nothing to transcribe")
    dur_s = (info.get("duration_ms") or 0) / 1000
    size = vp.stat().st_size
    outs = outputs(job)
    return {
        "service": SERVICE, "model": MODEL_ID, "video": str(vp), "duration_s": round(dur_s, 1),
        "size_bytes": size, "upload": "video" if size <= MAX_UPLOAD_BYTES else "audio",
        "too_long": dur_s > MAX_DURATION_S,
        "price_usd_per_hour": PRICE_USD_PER_HOUR, "price_source": PRICE_SOURCE,
        "cost_usd": cost_usd(dur_s), "key": key_status()["present"],
        "outputs_exist": [k for k, p in outs.items() if p.exists()],
    }


# ---- the API call ------------------------------------------------------------------------------

def _multipart(fields: list[tuple[str, str]], path: Path, size: int, boundary: str,
               on_bytes: Callable[[int], None] | None = None) -> tuple[Iterator[bytes], int]:
    """A streamed multipart/form-data body (the file is never read into memory whole) and its
    exact length. The upload name is neutral (`upload<ext>`): the person's file name stays home."""
    head = b"".join(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode("utf-8")
        for k, v in fields)
    ext = path.suffix.lower()
    ext = ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ".bin"
    head += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
             f'filename="upload{ext}"\r\n'
             f"Content-Type: application/octet-stream\r\n\r\n").encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")

    def gen() -> Iterator[bytes]:
        yield head
        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                sent += len(chunk)
                if on_bytes:
                    on_bytes(sent)
                yield chunk
        yield tail
    return gen(), len(head) + size + len(tail)


def request_fields(language: str | None, diarize: bool) -> list[tuple[str, str]]:
    fields = [("model_id", MODEL_ID), ("timestamps_granularity", "word"),
              ("diarize", "true" if diarize else "false"), ("tag_audio_events", "false")]
    if language:
        fields.append(("language_code", language))
    return fields


def call_api(path: Path, key: str, language: str | None, diarize: bool,
             urlopen: Urlopen | None = None) -> dict[str, Any]:
    """One POST; no automatic retry (an upload that timed out may already be billed).
    401/403 -> auth; 429/5xx/network -> transport; other 4xx -> data. Messages never carry the key."""
    size = path.stat().st_size
    t0 = progress.clock()
    boundary = uuid.uuid4().hex
    body, length = _multipart(request_fields(language, diarize), path, size, boundary,
                              on_bytes=lambda n: progress.report(n, size, "bytes", since=t0))
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "xi-api-key": key, "Accept": "application/json", "User-Agent": "ai-movie-company-transcribe",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(length)})
    try:
        with (urlopen or urllib.request.urlopen)(req, timeout=TIMEOUT_S) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = _scrub(_error_detail(e), key)
        if e.code in (401, 403):
            raise VPError("auth", f"ElevenLabs refused the key (HTTP {e.code}): {detail}") from None
        if e.code == 429 or e.code >= 500:
            raise VPError("transport", f"ElevenLabs is busy or failed (HTTP {e.code}): {detail}") from None
        raise VPError("data", f"ElevenLabs rejected the request (HTTP {e.code}): {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VPError("transport", f"no answer from ElevenLabs: {_scrub(str(e), key)}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise VPError("contract", f"ElevenLabs answer is not JSON: {e}") from None
    if not isinstance(data, dict):
        raise VPError("contract", "ElevenLabs answer is not an object")
    return data


def _error_detail(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", "replace")[:600]
    except Exception:  # noqa: BLE001 — the status code is enough then
        return ""
    try:
        d = json.loads(body).get("detail")
        if isinstance(d, dict):
            return str(d.get("message") or d.get("status") or d)[:300]
        if isinstance(d, list) and d:
            return "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d)[:300]
        return str(d)[:300] if d else body[:300]
    except (json.JSONDecodeError, AttributeError):
        return body[:300]


# ---- response -> job contract ------------------------------------------------------------------

def response_words(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Words of a Scribe response -> [{"text","start","end","spk"}] (ms, sorted). Spacing and
    audio events are dropped; a punctuation-only token sticks to the previous word; a missing
    time is taken from the neighbour. Raises VPError(contract) on a malformed response."""
    raw = resp.get("words")
    if raw is None and isinstance(resp.get("transcripts"), list):  # multichannel shape: merge
        raw = [w for t in resp["transcripts"] if isinstance(t, dict) for w in (t.get("words") or [])]
    if not isinstance(raw, list):
        raise VPError("contract", "ElevenLabs answer has no word list")
    out: list[dict[str, Any]] = []
    for w in raw:
        if not isinstance(w, dict):
            raise VPError("contract", "ElevenLabs word entry is not an object")
        if w.get("type", "word") != "word":
            continue
        text = str(w.get("text") or "").strip()
        if not text:
            continue
        s, e = w.get("start"), w.get("end")
        s = int(round(float(s) * 1000)) if isinstance(s, (int, float)) else None
        e = int(round(float(e) * 1000)) if isinstance(e, (int, float)) else None
        if out and not _HAS_WORD.search(text):
            out[-1]["text"] += text
            if e is not None:
                out[-1]["end"] = max(out[-1]["end"] or 0, e)
            continue
        out.append({"text": text, "start": s, "end": e, "spk": w.get("speaker_id")})
    # fill missing times from neighbours, then make them monotonic
    for i, w in enumerate(out):
        if w["start"] is None:
            w["start"] = out[i - 1]["end"] if i and out[i - 1]["end"] is not None else (w["end"] or 0)
        if w["end"] is None or w["end"] < w["start"]:
            nxt = next((x["start"] for x in out[i + 1:] if x["start"] is not None), None)
            w["end"] = max(w["start"], nxt if nxt is not None else w["start"])
    last = 0
    for w in out:
        if w["start"] < last:
            w["start"] = last
        w["end"] = max(w["end"], w["start"])
        last = w["start"]
    return out


def _fits(text: str) -> bool:
    return len(text) <= MAX_CHARS and all(len(ln) <= LINE for ln in wrap(text).split("\n"))


def phrases(words: list[dict[str, Any]], diarize: bool = False) -> list[list[dict[str, Any]]]:
    """Group words into subtitle phrases: a long pause, a speaker change, the 2 x 42 character
    frame or MAX_SPAN ends a phrase before a word; a sentence end (>= SENT_MIN chars) or a clause
    end in a long phrase ends it after a word."""
    groups: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []

    def text(g: list[dict[str, Any]]) -> str:
        return " ".join(x["text"] for x in g)
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            brk = (gap > PAUSE_HARD_MS
                   or (gap >= PAUSE_SOFT_MS and len(text(cur)) >= SENT_MIN)
                   or not _fits(text(cur + [w]))
                   or w["end"] - cur[0]["start"] > MAX_SPAN
                   or (diarize and w.get("spk") != cur[-1].get("spk")))
            if brk:
                groups.append(cur)
                cur = []
        cur.append(w)
        t = text(cur)
        if (_SENT_END.search(w["text"]) and len(t) >= SENT_MIN) or \
                (_CLAUSE_END.search(w["text"]) and len(t) >= MAX_CHARS * 0.6):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def build_cues(groups: list[list[dict[str, Any]]], duration_ms: int | None = None,
               diarize: bool = False) -> list[Cue]:
    """Phrases -> SRT cues: start = first word, end = last word, stretched toward MIN_DUR without
    running into the next phrase (GAP kept when there is room); never past the recording end."""
    spk_ids: dict[Any, str] = {}
    cues: list[Cue] = []
    for i, g in enumerate(groups):
        start, end = g[0]["start"], max(x["end"] for x in g)
        nxt = groups[i + 1][0]["start"] if i + 1 < len(groups) else None
        if end - start < MIN_DUR:
            want = start + MIN_DUR
            if nxt is not None:
                want = min(want, max(end, nxt - GAP))
            if duration_ms:
                want = min(want, max(end, duration_ms))
            end = max(end, want)
        if nxt is not None and end > nxt:
            end = max(nxt, start + 1)
        if end <= start:
            end = start + 1
        speaker = None
        if diarize:
            sid = g[0].get("spk")
            speaker = spk_ids.setdefault(sid, str(len(spk_ids) + 1))
        cues.append(Cue(str(i + 1), start, end, " ".join(x["text"] for x in g), speaker))
    return cues


def whisper_json(resp: dict[str, Any], groups: list[list[dict[str, Any]]],
                 cues: list[Cue]) -> dict[str, Any]:
    """The `_cut.json` contract: Whisper verbose_json-like, one segment per SRT cue with its
    words in seconds (S0/S2 `load_words` reads segments[].words[]: word, start, end)."""
    def sec(ms: int) -> float:
        return round(ms / 1000, 3)
    segments = []
    for i, (g, c) in enumerate(zip(groups, cues)):
        seg = {"id": i, "start": sec(c.start_ms), "end": sec(c.end_ms), "text": c.text,
               "words": [{"word": w["text"], "start": sec(w["start"]), "end": sec(w["end"])} for w in g]}
        if c.speaker is not None:
            seg["speaker"] = c.speaker
        segments.append(seg)
    return {"text": " ".join(c.text for c in cues), "language": resp.get("language_code"),
            "duration": resp.get("audio_duration_secs"), "segments": segments,
            "source": {"service": SERVICE, "model": MODEL_ID,
                       "transcription_id": resp.get("transcription_id")}}


def convert(resp: dict[str, Any], duration_ms: int | None = None,
            diarize: bool = False) -> tuple[list[Cue], dict[str, Any]]:
    """A Scribe response -> (SRT cues, `_cut.json` object). Pure."""
    words = response_words(resp)
    if not words:
        raise VPError("data", "no speech was recognised in the recording")
    groups = phrases(words, diarize)
    cues = build_cues(groups, duration_ms, diarize)
    return cues, whisper_json(resp, groups, cues)


# ---- run ---------------------------------------------------------------------------------------

def _inside(job: Job, p: Path) -> bool:
    try:
        p.resolve().relative_to(job.root)
        return True
    except ValueError:
        return False


def _video_id(p: Path) -> dict[str, Any]:
    st = p.stat()
    return {"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)}


def _extract_audio(video: Path, dst_dir: Path) -> Path:
    """Mono AAC 64 kbit/s (~29 MB per hour) for a recording above the upload limit."""
    from .adapters.media import run_ffmpeg
    out = dst_dir / "audio.m4a"
    run_ffmpeg(["-i", str(video), "-vn", "-ac", "1", "-c:a", "aac", "-b:a", "64k", str(out)])
    return out


def _write_outputs(job: Job, resp: dict[str, Any], duration_ms: int | None, diarize: bool,
                   meta: dict[str, Any]) -> dict[str, Any]:
    cues, words_json = convert(resp, duration_ms, diarize)
    outs = outputs(job)
    write_json(outs["words"], words_json)
    atomic_write(outs["srt"], format_srt(cues, with_speaker=diarize))   # last: presence = done
    n_words = len(load_words(words_json))
    meta = {**meta, "cues": len(cues), "words": n_words,
            "language": resp.get("language_code"),
            "language_probability": resp.get("language_probability"),
            "transcription_id": resp.get("transcription_id"),
            "outputs": {k: job.rel(p) for k, p in outs.items()}}
    write_yaml(job.root / META_NAME, meta)
    return meta


def run(job: Job, video: str | os.PathLike | None = None, language: str | None = None,
        diarize: bool = False, force: bool = False, urlopen: Urlopen | None = None,
        probe: Callable[[Path], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Transcribe the job's recording into `_cut.srt` + `_cut.json` (see module doc). Holds the
    job lock, so it never writes under a running `vp run`."""
    with job_lock(job) as got:
        if not got:
            raise VPError("data", "the job is busy (another run holds job.lock): try again when it ends")
        return _run_locked(job, video, language, diarize, force, urlopen, probe)


def _run_locked(job: Job, video: str | os.PathLike | None, language: str | None, diarize: bool,
                force: bool, urlopen: Urlopen | None,
                probe: Callable[[Path], dict[str, Any]] | None) -> dict[str, Any]:
    outs = outputs(job)
    for p in outs.values():
        if not _inside(job, p):
            raise VPError("data", f"the subtitles path {p} is outside the job folder: not written")
    existing = [k for k, p in outs.items() if p.exists()]
    if existing and not force:
        raise VPError("data", f"the job already has {', '.join(existing)}: nothing sent "
                              "(--force replaces them and pays again)")
    est = estimate(job, video, probe)
    if est["too_long"]:
        raise VPError("data", f"the recording is longer than {MAX_DURATION_S // 3600} h: "
                              "ElevenLabs takes at most that per file")
    vp = Path(est["video"])
    language = language or (str(job.request().get("language") or "") or None)
    duration_ms = int(est["duration_s"] * 1000)
    vid = _video_id(vp)
    meta = {"service": SERVICE, "model": MODEL_ID, "video": job.rel(vp), "video_id": vid,
            "language_hint": language, "diarize": diarize, "duration_s": est["duration_s"],
            "cost_usd_estimate": est["cost_usd"], "price_source": PRICE_SOURCE}
    raw_path = job.root / RAW_NAME
    saved = read_json(raw_path, default=None) if raw_path.exists() and not force else None
    if isinstance(saved, dict) and saved.get("video_id") == vid and isinstance(saved.get("response"), dict) \
            and saved.get("diarize", False) == diarize:
        meta = {**meta, "created_at": saved.get("created_at"), "reused_saved_response": True}
        return _write_outputs(job, saved["response"], duration_ms, diarize, meta)

    key, _ = find_key()
    if not key:
        raise VPError("auth", "NEED_KEY:elevenlabs — no ElevenLabs key yet: copy the key, then run "
                              "`vp keys set elevenlabs --from-clipboard`")
    with progress.tracking(job.root, "transcribe", None, total=est["size_bytes"], unit="bytes"), \
            tempfile.TemporaryDirectory(prefix="vp-transcribe-") as tmp:
        src = vp if est["upload"] == "video" else _extract_audio(vp, Path(tmp))
        resp = call_api(src, key, language, diarize, urlopen)
    created = now_iso()
    write_json(raw_path, {"service": SERVICE, "model": MODEL_ID, "created_at": created,
                          "video_id": vid, "diarize": diarize, "response": resp})
    meta = {**meta, "created_at": created, "reused_saved_response": False}
    return _write_outputs(job, resp, duration_ms, diarize, meta)


def import_srt(job: Job, src: str | os.PathLike, force: bool = False) -> dict[str, Any]:
    """The person's own subtitles file -> <job>/_cut.srt (validated: parses, not empty). The
    source file itself is only read."""
    p = Path(src).expanduser()
    if not p.exists():
        raise VPError("data", f"subtitles file not found: {p}")
    dst = job.source_srt
    if not _inside(job, dst):
        raise VPError("data", f"the subtitles path {dst} is outside the job folder: not written")
    if p.resolve() == dst.resolve():
        cues = read_srt(p)
        return {"srt": job.rel(dst), "cues": len(cues), "duration_ms": srt_duration_ms(cues)}
    if dst.exists() and not force:
        raise VPError("data", f"the job already has {dst.name}: not replaced (--force replaces it)")
    cues = read_srt(p)
    if not cues:
        raise VPError("data", "the subtitles file has no subtitles in it")
    atomic_write(dst, p.read_bytes())
    return {"srt": job.rel(dst), "cues": len(cues), "duration_ms": srt_duration_ms(cues)}


# ---- word times of any media file (render QA) --------------------------------------------------

class ScribeTranscriber:
    """The same recogniser as `vp transcribe run`, for one media file and with nothing written
    into the job: the word times of what is actually in the sound. Used by the render QA to check
    burned captions against the voice (vp.steps.s5_render). It sends the file's sound to
    ElevenLabs and costs money, so the caller decides when it may run (`caption_qa.transcribe`).

      available() -> (bool, reason)      is there a key; the reason is plain words when not
      words(path, language=None) -> [(text, start_ms, end_ms)]
    """

    name = SERVICE

    def __init__(self, urlopen: Urlopen | None = None, costs: Any = None):
        self.urlopen = urlopen
        self.costs = costs

    def available(self) -> tuple[bool, str]:
        key, _ = find_key()
        return (True, "") if key else (False, "no ElevenLabs key is set")

    def words(self, path: str | os.PathLike, language: str | None = None) -> list[tuple[str, int, int]]:
        key, _ = find_key()
        if not key:
            raise VPError("auth", "NEED_KEY:elevenlabs — no ElevenLabs key")
        with tempfile.TemporaryDirectory(prefix="vp-transcribe-") as tmp:
            audio = _extract_audio(Path(path), Path(tmp))      # the sound only, never the picture
            resp = call_api(audio, key, language, False, self.urlopen)
        secs = resp.get("audio_duration_secs")
        if self.costs is not None and isinstance(secs, (int, float)):
            try:
                self.costs.add("api", provider=SERVICE, op="caption_check", model=MODEL_ID,
                               usd=round(float(secs) / 3600 * PRICE_USD_PER_HOUR, 4), estimate=True)
            except OSError:
                pass    # bookkeeping; the answer is already here
        return [(w["text"], int(w["start"]), int(w["end"])) for w in response_words(resp)]


# ---- CLI ---------------------------------------------------------------------------------------

def _print(res: dict[str, Any], as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        for ln in lines:
            print(ln)


def _hms(s: float) -> str:
    s = int(round(s))
    h, rem = divmod(s, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def _read_clipboard() -> str:
    try:
        proc = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise VPError("data", "cannot read the clipboard on this computer (use --stdin)") from e
    return proc.stdout or ""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp transcribe",
                                 description="speech recognition (ElevenLabs Scribe) for a recording without subtitles")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("estimate", "run"):
        s = sub.add_parser(name)
        s.add_argument("job")
        s.add_argument("--video", help="the recording (default: the job's source video)")
        s.add_argument("--json", action="store_true")
        if name == "run":
            s.add_argument("--language", help="ISO 639-1/3 hint, e.g. ru (default: request.yaml "
                                              "`language`, else auto)")
            s.add_argument("--diarize", action="store_true", help="label speakers (off by default)")
            s.add_argument("--force", action="store_true", help="replace existing subtitles, pay again")
    s = sub.add_parser("import")
    s.add_argument("job")
    s.add_argument("file")
    s.add_argument("--force", action="store_true")
    s.add_argument("--json", action="store_true")
    k = sub.add_parser("key")
    k.add_argument("action", choices=("status", "set"))
    k.add_argument("--from-clipboard", action="store_true")
    k.add_argument("--stdin", action="store_true")
    k.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "key":
        if a.action == "status":
            st = key_status()
            _print(st, a.json, [f"KEY present ({st['where']}, fingerprint {st['fingerprint']})"
                                if st["present"] else "KEY missing"])
            return 0 if st["present"] else 2
        if a.from_clipboard == a.stdin:
            raise VPError("data", "key set needs exactly one of --from-clipboard / --stdin")
        raw = _read_clipboard() if a.from_clipboard else sys.stdin.read()
        res = save_key(raw)
        if a.from_clipboard:
            from .keys import clear_clipboard
            res["clipboard_cleared"] = clear_clipboard()
        _print(res, a.json, [f"SAVED key (fingerprint {res['fingerprint']}) — serves recognition, "
                             "voice and music"])
        return 0

    job = Job(a.job)
    if a.cmd == "estimate":
        e = estimate(job, a.video)
        _print(e, a.json, [
            f"recording {_hms(e['duration_s'])}, {e['size_bytes'] / 1e6:.0f} MB -> sent as {e['upload']}",
            f"cost ≈ ${e['cost_usd']:.2f} ({SERVICE} {MODEL_ID}, ${PRICE_USD_PER_HOUR:.2f}/h)",
            f"key: {'present' if e['key'] else 'missing'}"
            + (f"; already has: {', '.join(e['outputs_exist'])}" if e["outputs_exist"] else "")])
        return 0
    if a.cmd == "import":
        res = import_srt(job, a.file, a.force)
        _print(res, a.json, [f"IMPORTED {res['srt']} ({res['cues']} subtitles)"])
        return 0
    t0 = time.monotonic()
    res = run(job, a.video, a.language, a.diarize, a.force)
    res["elapsed_s"] = round(time.monotonic() - t0, 1)
    _print(res, a.json, [f"TRANSCRIBED {res['outputs']['srt']}: {res['cues']} subtitles, "
                         f"{res['words']} words, language {res['language']}"
                         + (" (saved answer reused, nothing paid)" if res.get("reused_saved_response") else "")])
    return 0
