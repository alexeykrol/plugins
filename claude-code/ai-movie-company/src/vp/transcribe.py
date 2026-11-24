"""`vp transcribe` — speech recognition for a recording that came without subtitles (owner
decision 2026-11-20): ElevenLabs Speech-to-Text (Scribe) with word timestamps -> the job contract
`_cut.json` (Whisper verbose_json-like word list, read by S0/S2 `load_words`) and `_cut.srt`
(phrases built from the words by code). The raw text then goes through S1 proofreading as usual:
Claude cleans the text, the timings stay the recognizer's.

  vp transcribe estimate <job> [--video F] [--json]      duration x price, before anything is sent
  vp transcribe run <job> --quote ID [--video F] [--language ru] [--diarize] [--force] [--retry] [--json]
  vp transcribe import <job> <file.srt> [--json]         the person's own subtitles -> <job>/_cut.srt
  vp transcribe purge <job> [--yes] [--json]             remove the job's saved recognition answers
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
import fcntl
import hashlib
import http.client
import json
import math
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator

from .core import io, progress
from .core.errors import VPError
from .core.io import atomic_write, now_iso, read_json, read_yaml, sha256_bytes, sha256_obj, write_json, write_yaml
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
APPROVAL_NAME = "recognition.approval.json"
LEDGER_NAME = "recognition.sqlite3"
ANSWERS_NAME = "recognition-answers.sqlite3"
CHECKS_SHARE = 0.25
CHECKS_CAP_S = 900
# At $0.20/h, one cent buys 180 seconds. Use integer billing seconds and cents for authorization.
SECONDS_PER_CENT = 3600 // round(PRICE_USD_PER_HOUR * 100)
TIMEOUT_S = 3600                    # one socket wait; a 2 h recording takes minutes
CHUNK = 1 << 20
LEGACY_DURATION_TOLERANCE_S = 2.0


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
    return _cost_cents(math.ceil(duration_s)) / 100 if duration_s > 0 else 0.0


def _cost_cents(seconds: int) -> int:
    return (seconds + SECONDS_PER_CENT - 1) // SECONDS_PER_CENT


def _at_rate(seconds: int) -> float:
    return round(seconds * PRICE_USD_PER_HOUR / 3600, 8)


def _video_path(job: Job, video: str | os.PathLike | None) -> Path:
    if video:
        p = Path(video).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p)
    return job.source_video


def outputs(job: Job) -> dict[str, Path]:
    return {"srt": job.source_srt, "words": job.source_words}


def _recording_info(job: Job, video: str | os.PathLike | None = None,
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
        "service": SERVICE, "model": MODEL_ID, "video": str(vp), "duration_s": dur_s,
        "size_bytes": size, "fps": info.get("fps"), "upload": "audio",   # only the sound ever leaves, never the picture
        "too_long": dur_s > MAX_DURATION_S,
        "price_usd_per_hour": PRICE_USD_PER_HOUR, "price_source": PRICE_SOURCE,
        "key": key_status()["present"],
        "outputs_exist": [k for k, p in outs.items() if p.exists()],
    }


def estimate(job: Job, video: str | os.PathLike | None = None,
             probe: Callable[[Path], dict[str, Any]] | None = None, *,
             language: str | None = None, diarize: bool = False) -> dict[str, Any]:
    """Quote the source upload and subsequent reel checks, without sending anything."""
    from .design import job_config
    guard_inputs(job, *quote_targets(job), video=video)
    with job_lock(job) as got:
        if not got:
            raise VPError("data", "the job is busy (another run holds job.lock): try again when it ends")
        with source_quote(job, video, language, diarize, probe, job_config(job), "transcribe") as (quote, _):
            return quote


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


class RejectedRecognition(VPError):
    """The provider explicitly rejected the upload before recognition."""


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
            raise RejectedRecognition("auth", f"ElevenLabs refused the key (HTTP {e.code}): {detail}") from None
        if e.code in (408, 429) or e.code >= 500:
            raise VPError("transport", f"ElevenLabs is busy or failed (HTTP {e.code}): {detail}") from None
        raise RejectedRecognition("data", f"ElevenLabs rejected the request (HTTP {e.code}): {detail}") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
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

def _timestamp_ms(value: Any) -> int | None:
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return round(value * 1000)
    return None


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
        s, e = (_timestamp_ms(t) for t in (s, e))
        if out and not _HAS_WORD.search(text):
            out[-1]["text"] += text
            if e is not None:
                out[-1]["end"] = max(out[-1]["end"] or 0, e)
            continue
        measured = s is not None and e is not None and s < e and len(text.split()) == 1
        provenance = "measured" if measured else "inferred"
        out.append({"text": text, "start": s, "end": e, "spk": w.get("speaker_id"),
                    "provenance": provenance})
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
            w["provenance"] = "inferred"
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
               "words": [{"word": w["text"], "start": sec(w["start"]), "end": sec(w["end"]),
                          "provenance": w["provenance"]} for w in g]}
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


def media_digest(path: Path) -> str:
    """Hash stable media once per process, including the timeline and all tracks."""
    try:
        return io.sha256_file(path, missing_ok=False)
    except io.FileChangedError as exc:
        raise RecognitionBlocked(NO_APPROVAL, exc.msg) from exc


@contextmanager
def _hash_progress(job: Job, path: Path):
    if progress.current() is None and path.stat().st_size > io.HASH_PROGRESS_BYTES:
        with progress.tracking(job.root, "verify recording", None, total=path.stat().st_size, unit="bytes"):
            yield
    else:
        yield


def _verified_audio(path: Path, dst: Path, expected: str, changed: str,
                    *, reason: str | None = None) -> Path:
    try:
        identity = io.file_identity(path)
        if media_digest(path) == expected and io.file_identity(path) == identity:
            audio = _extract_audio(identity[0], dst)
            if io.file_identity(path) == identity:
                return audio
    except (FileNotFoundError, RecognitionBlocked):
        pass
    raise RecognitionBlocked(reason or NO_APPROVAL, changed)


def _extract_audio(video: Path, dst_dir: Path) -> Path:
    """Mono AAC 64 kbit/s (~29 MB per hour): the only thing sent for transcription.

    `first_pts=0` pads a sound track that starts later than the picture, so word times stay on the
    recording's own clock."""
    from .adapters.media import run_ffmpeg
    out = dst_dir / "audio.m4a"
    # the same sound track the cut uses (0:a:0), and none of the file's tags, titles or chapters
    run_ffmpeg(["-i", str(video), "-vn", "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1",
                "-ac", "1", "-af", "aresample=async=1:first_pts=0",
                "-c:a", "aac", "-b:a", "64k", "-fflags", "+bitexact", str(out)])
    return out


# ---- persistent approval and recognition -------------------------------------------------------

NO_APPROVAL = "no_approval_for_this_recording"
AMOUNT_USED = "approved_amount_used_up"
ATTEMPT_UNCLEAR = "earlier_attempt_unclear"
REEL_RETRY_REQUIRED = "reel_retry_required_after_rejection"
REEL_RETRY_NOTE = "the provider rejected the earlier reel check. Approve a fresh retry quote"


class RecognitionBlocked(VPError):
    retryable_transport = False

    def __init__(self, reason: str, message: str, kind: str = "data", *, attempted: bool = False):
        super().__init__(kind, message, reason=reason, attempted=attempted)
        self.reason = reason
        self.attempted = attempted

    def result(self) -> dict[str, Any]:
        return {"status": "not_run", "reason": self.reason, "sent": None if self.attempted else False,
                "note": self.msg}


def approval_path(job: Job) -> Path:
    return job.root / APPROVAL_NAME


def guard_inputs(job: Job, *paths: Path, generated: tuple[str, ...] = (),
                 manifest_dir: Path | None = None,
                 video: str | os.PathLike | None = None, other_input: Path | None = None) -> None:
    """Refuse output/cleanup paths that resolve to inputs. Implicit defaults may be generated."""
    configured = job.request().get("source") or {}
    if not isinstance(configured, dict):
        configured = {}
    inputs = (("configured source video", job.source_video, "video"),
              ("configured source subtitle", job.source_srt, "srt"),
              ("configured source words", job.source_words, "words"),
              ("recording", _video_path(job, video) if video else None, None),
              ("imported subtitle", other_input, None))
    try:
        targets = {p.resolve() for p in paths}
        manifests = manifest_dir.resolve() if manifest_dir is not None else None
        for label, path, key in inputs:
            if path is None:
                continue
            if key in generated and not configured.get(key) and path == job.root / path.name and not path.is_symlink():
                continue
            resolved = path.resolve()
            if resolved in targets or (resolved.parent == manifests and resolved.suffix == ".yaml"):
                raise VPError("data", f"the {label} path resolves to a file this command writes or deletes")
    except (OSError, RuntimeError) as e:
        raise VPError("data", "cannot resolve the input and output paths safely") from e


def quote_targets(job: Job) -> tuple[Path, ...]:
    home = vp_home()
    return (job.lock_file, job.root / progress.FILE_NAME, home / progress.LIVE_NAME,
            *(home / (name + suffix) for name in (LEDGER_NAME, ANSWERS_NAME)
              for suffix in ("", "-journal", "-wal", "-shm")))


def recognition_targets(job: Job) -> tuple[Path, ...]:
    return (*quote_targets(job), approval_path(job), job.root / RAW_NAME, vp_home() / "costs.jsonl")


def _secure_file(path: Path, *, create: bool) -> bool:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags | (os.O_CREAT if create else 0), 0o600)
    except FileNotFoundError:
        if not create:
            return False
        raise
    except OSError:
        if path.is_symlink():
            raise RecognitionBlocked(ATTEMPT_UNCLEAR, f"the recognition database is a symlink: {path.name}") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RecognitionBlocked(ATTEMPT_UNCLEAR,
                                     f"the recognition database must be a regular file owned by you: {path.name}")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return True


def _database_files(path: Path) -> tuple[Path, ...]:
    return (path, *(path.with_name(path.name + suffix) for suffix in ("-journal", "-wal", "-shm")))


def _secure_database(path: Path, *, create: bool) -> bool:
    if not _secure_file(path, create=create):
        return False
    for sidecar in _database_files(path)[1:]:
        _secure_file(sidecar, create=False)
    return True


def _schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY, job_key TEXT NOT NULL, recording_sha256 TEXT NOT NULL,
            audio_sha256 TEXT, language_hint TEXT, diarize INTEGER NOT NULL,
            service TEXT NOT NULL, model TEXT NOT NULL, rate_cents_per_hour INTEGER NOT NULL,
            cents INTEGER NOT NULL, source_seconds INTEGER NOT NULL, checks_seconds INTEGER NOT NULL,
            source_spent INTEGER NOT NULL DEFAULT 0, checks_spent INTEGER NOT NULL DEFAULT 0,
            approved_at TEXT NOT NULL, reel_scope TEXT NOT NULL DEFAULT '"all"'
        );
        CREATE TABLE IF NOT EXISTS paid_attempts (
            id TEXT PRIMARY KEY, sound TEXT NOT NULL, answer_key TEXT NOT NULL,
            language_hint TEXT, receipt_id TEXT, scope TEXT, seconds INTEGER, status TEXT NOT NULL,
            created_at TEXT NOT NULL, retry_of TEXT, decision_id TEXT
        );
        CREATE INDEX IF NOT EXISTS paid_attempt_sound ON paid_attempts (sound);
        CREATE TABLE IF NOT EXISTS retry_decisions (
            id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, sound TEXT NOT NULL,
            answer_key TEXT NOT NULL, previous_id TEXT NOT NULL, retry_of TEXT NOT NULL,
            seconds INTEGER NOT NULL, item TEXT NOT NULL, render_sha256 TEXT NOT NULL,
            approved_at TEXT NOT NULL, consumed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS legacy_spend (
            id TEXT PRIMARY KEY, job_key TEXT NOT NULL, cents INTEGER NOT NULL,
            spent_seconds INTEGER NOT NULL, approved_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cache.answers (
            job_key TEXT NOT NULL, answer_key TEXT NOT NULL, diarize INTEGER NOT NULL,
            response TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (job_key, answer_key)
        );
    """)
    # The unreleased v1 ledger did not record a job owner for responses. Retain payment
    # evidence and erase the unowned text. Source sound identities can be recovered from quotes.
    with db:
        db.execute("BEGIN IMMEDIATE")
        if not any(column["name"] == "reel_scope" for column in db.execute("PRAGMA table_info(approvals)")):
            db.execute("ALTER TABLE approvals ADD COLUMN reel_scope TEXT NOT NULL DEFAULT '\"all\"'")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'attempts'").fetchone():
            identities = {}
            for row in db.execute("SELECT * FROM receipts"):
                old = json.loads(row["quote"])
                db.execute("INSERT OR IGNORE INTO legacy_spend VALUES (?, ?, ?, ?, ?)",
                           (row["id"], sha256_obj(row["job"]), old["approved_cents"],
                            row["spent_seconds"], row["approved_at"]))
                sound, hint = old.get("audio_sha256"), old.get("language_hint")
                if sound:
                    identities[_request_key(sound, hint)] = (sound, hint, row["id"])
            for row in db.execute("SELECT key, id, status, created_at FROM attempts"):
                sound, hint, receipt_id = identities.get(row["key"], (row["key"], None, None))
                db.execute("""INSERT OR IGNORE INTO paid_attempts
                           (id, sound, answer_key, language_hint, receipt_id, status, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                           (row["id"], sound, row["key"], hint, receipt_id, row["status"], row["created_at"]))
            db.execute("DROP TABLE attempts")
            db.execute("DROP TABLE receipts")
        db.execute("PRAGMA user_version = 3")


@contextmanager
def _ledger(*, write: bool = False):
    # Open existing databases read/write so permissions and old-schema privacy can be
    # corrected even on a quote. A completely new quote creates neither database.
    home = vp_home().absolute()
    path, answers = home / LEDGER_NAME, home / ANSWERS_NAME
    try:
        if write:
            home.mkdir(parents=True, exist_ok=True)
        if not _secure_database(path, create=write):
            yield None
            return
        _secure_database(answers, create=True)
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("ATTACH DATABASE ? AS cache", (answers.as_uri() + "?mode=rw",))
            db.execute("PRAGMA secure_delete = ON")
            db.execute("PRAGMA cache.secure_delete = ON")
            cache_exists = db.execute("SELECT 1 FROM cache.sqlite_master WHERE name = 'answers'").fetchone()
            if db.execute("PRAGMA user_version").fetchone()[0] != 3 or not cache_exists:
                _schema(db)
            yield db
    except (sqlite3.Error, OSError, ValueError):
        raise RecognitionBlocked(ATTEMPT_UNCLEAR, "the recognition ledger could not be verified or updated") from None


def _job_key(job: Job) -> str:
    return sha256_obj(str(job.root))


def _saved(db: sqlite3.Connection | None, job: Job, key: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM cache.answers WHERE job_key = ? AND answer_key = ?",
                      (_job_key(job), key)).fetchone() if db else None


def purge_job(job: Job) -> dict[str, Any]:
    """Remove provider answers for this job. Keep payment records and converted outputs."""
    from .core.adoption import adopt

    raw = job.root / RAW_NAME
    answers = (vp_home() / ANSWERS_NAME).absolute()
    guard_inputs(job, job.lock_file, raw, *_database_files(answers))
    with job_lock(job) if job.root.is_dir() else nullcontext(True) as got:
        if not got:
            raise VPError("data", "the job is busy (another run holds job.lock): try again when it ends")
        if job.root.is_dir():
            adopt(job)
        removed = 0
        try:
            if _secure_database(answers, create=False):
                with closing(sqlite3.connect(answers.as_uri() + "?mode=rw", uri=True)) as db, db:
                    db.execute("PRAGMA secure_delete = ON")
                    removed = db.execute("DELETE FROM answers WHERE job_key = ?", (_job_key(job),)).rowcount
        except (sqlite3.Error, OSError, RecognitionBlocked):
            raise RecognitionBlocked(ATTEMPT_UNCLEAR, "the recognition answer store could not be cleaned") from None
        remove_raw = not raw.is_symlink() and _document(raw).get("service") == SERVICE
        if remove_raw:
            raw.unlink()
    return {"answers_removed": removed, "raw_removed": remove_raw}


def _attempt(db: sqlite3.Connection | None, sound: str, key: str, *, statuses: str = "all",
             scope: str | None = None) -> sqlite3.Row | None:
    if db is None:
        return None
    condition = {"all": "", "charged": "AND status != 'rejected'",
                 "ambiguous": "AND status = 'ambiguous'"}[statuses]
    params = (sound, key) if scope is None else (sound, key, scope)
    scope_condition = "" if scope is None else "AND scope = ?"
    return db.execute(f"""SELECT * FROM paid_attempts WHERE (sound = ? OR answer_key = ?)
                          {condition} {scope_condition} ORDER BY rowid DESC LIMIT 1""", params).fetchone()


def _paid_for_hint(db: sqlite3.Connection | None, key: str) -> bool:
    return bool(db and db.execute("SELECT 1 FROM paid_attempts WHERE answer_key = ? AND status = 'succeeded'",
                                  (key,)).fetchone())


def saved_for_reel(job: Job) -> bool:
    if not (vp_home() / ANSWERS_NAME).is_file():
        return False
    with _ledger() as db:
        return bool(db and db.execute("SELECT 1 FROM cache.answers WHERE job_key = ? LIMIT 1",
                                      (_job_key(job),)).fetchone())


def _request_key(audio_sha256: str, language: str | None) -> str:
    return sha256_obj([audio_sha256, MODEL_ID, language])


def _upload_seconds(audio: Path, probe: Callable[[Path], dict[str, Any]] | None = None) -> int:
    from .adapters import media
    try:
        if probe is None:
            # media.probe rounds to milliseconds. Read the duration before rounding the charge up.
            measured = subprocess.run(
                [media.FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
                capture_output=True, text=True, check=True, timeout=120)
            seconds = Decimal(measured.stdout.strip())
        else:
            seconds = Decimal(str(probe(audio)["duration_ms"])) / 1000
        if not seconds.is_finite() or not 0 < seconds <= MAX_DURATION_S:
            raise ValueError("duration")
    except (subprocess.SubprocessError, InvalidOperation, OSError, ValueError, KeyError) as exc:
        raise VPError("data", "the extracted sound has no usable duration or exceeds the service limit") from exc
    return math.ceil(seconds)


def language_hint(job: Job, language: str | None = None) -> str | None:
    return (language or str(job.request().get("language") or "")).strip().lower() or None


LOCAL_RECORDING_CHANGED = "the recording changed; its saved word times were not used"
LOCAL_UNVERIFIED = "the saved word times could not be matched to this recording"
LEGACY_TIMING_NOTE = "the saved word times match the recording's duration only"
VERIFIED = "verified"
LEGACY_UNVERIFIED = "legacy_unverified"
NO_WORD_TIMES = "the recording came without word times (subtitles only)"
OLD_APPROVAL = "the earlier approval did not cover reel checks; approve a reel-check-only quote"
MISSING_APPROVAL = "there is no approval for this recording and this check"
STALE_QUOTE = "the recording, price or recognition settings changed; use a fresh quote"
UNCLEAR_CHARGE = "the earlier recognition attempt may already have been charged"
ANSWER_MISSING = "this sound was already recognised, but this job has no saved answer"
RENDER_CHANGED = "the render changed after S5 made it; no sound was sent"
RENDER_MISMATCH = "render_changed"


def _document(path: Path, *, yaml: bool = False) -> dict[str, Any]:
    from yaml import YAMLError
    try:
        data = (read_yaml if yaml else read_json)(path, default=None)
    except (OSError, ValueError, YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def timing_status(job: Job, video: Path, doc: dict[str, Any], kind: str,
                  probe: Callable[[Path], dict[str, Any]] | None = None,
                  duration_s: float | None = None) -> str | None:
    """Validate an owned timing document. Return a plain reason if it belongs elsewhere."""
    return _timing_validation(job, video, doc, kind, probe, duration_s)[1]


def _stored_duration(doc: dict[str, Any], source: dict[str, Any], meta: dict[str, Any],
                     response: dict[str, Any]) -> float | None:
    return next((x for x in (doc.get("duration_s"), source.get("duration_s"), meta.get("duration_s"),
                             response.get("audio_duration_secs"), doc.get("duration"))
                 if isinstance(x, (int, float)) and math.isfinite(x) and x > 0), None)


def _timing_validation(job: Job, video: Path, doc: dict[str, Any], kind: str,
                       probe: Callable[[Path], dict[str, Any]] | None = None,
                       duration_s: float | None = None) -> tuple[str | None, str | None]:
    source = doc.get("source")
    source = source if isinstance(source, dict) else {}
    provider = doc.get("service") == SERVICE or source.get("service") == SERVICE
    # User-supplied timing has its own contract; enforce its digest whenever one is provided.
    digest = source.get("recording_sha256") or doc.get("recording_sha256")
    if not provider and not digest:
        return VERIFIED, None   # A timing file supplied as a job input.
    if digest:
        try:
            return (VERIFIED, None) if media_digest(video) == digest else (None, LOCAL_RECORDING_CHANGED)
        except (OSError, VPError, ValueError):
            return None, LOCAL_UNVERIFIED
    raw = _document(job.root / RAW_NAME)
    meta = _document(job.caption_choice if kind == "captions" else job.root / META_NAME, yaml=True)
    response = raw.get("response")
    response = response if isinstance(response, dict) else {}
    document_response = doc.get("response")
    document_id = source.get("transcription_id") or doc.get("transcription_id")
    if not document_id and isinstance(document_response, dict):
        document_id = document_response.get("transcription_id")
    raw_id = response.get("transcription_id")
    if document_id and raw_id and document_id != raw_id:
        raw, response = {}, {}
    if document_id and meta.get("transcription_id") and document_id != meta["transcription_id"]:
        meta = {}
    same_raw = ((document_id and document_id == raw_id) or (meta.get("created_at")
                and meta["created_at"] == raw.get("created_at")))
    duration_only = LEGACY_UNVERIFIED in (doc.get("timing_provenance"), source.get("timing_provenance"))
    if not duration_only:
        digest = meta.get("recording_sha256") or (raw.get("recording_sha256") if same_raw else None)
    try:
        if digest:
            return (VERIFIED, None) if media_digest(video) == digest else (None, LOCAL_RECORDING_CHANGED)
        identity = doc.get("video_id") or source.get("video_id")
        if not duration_only:
            identity = identity or meta.get("video_id") or raw.get("video_id")
        if identity and (not isinstance(identity, dict) or identity.get("size") != video.stat().st_size):
            return None, LOCAL_RECORDING_CHANGED
        seconds = _stored_duration(doc, source, meta, response)
        if seconds is None:
            return None, LOCAL_UNVERIFIED
        if duration_s is None:
            from .adapters import media
            duration_s = (probe or media.probe)(video)["duration_ms"] / 1000
        if abs(duration_s - seconds) <= LEGACY_DURATION_TOLERANCE_S:
            # Old video_id records carry the byte count. A copy can change the file name
            # and times, but it keeps the byte count and the recording duration.
            return (VERIFIED if identity else LEGACY_UNVERIFIED), None
        return None, LOCAL_RECORDING_CHANGED
    except (OSError, VPError, ValueError, KeyError, TypeError):
        return None, LOCAL_UNVERIFIED


def local_response(job: Job, video: Path, purpose: str, language: str | None, diarize: bool,
                   probe: Callable[[Path], dict[str, Any]] | None = None,
                   duration_s: float | None = None, *, recover: bool = False) -> dict[str, Any] | None:
    raw = _document(job.root / RAW_NAME)
    meta = _document(job.caption_choice if purpose == "captions" else job.root / META_NAME, yaml=True)
    if (not isinstance(raw.get("response"), dict) or raw.get("service") != SERVICE
            or (diarize and not raw.get("diarize"))):
        return None
    raw_id = raw["response"].get("transcription_id")
    if raw_id and meta.get("transcription_id") not in (None, raw_id):
        meta = {}
    provenance, reason = _timing_validation(job, video, raw, purpose, probe, duration_s)
    if reason:
        if recover:
            raise RecognitionBlocked(NO_APPROVAL, reason)
        return None
    hint_matches = language == raw.get("language_hint", meta.get("language_hint"))
    if not hint_matches and not (recover and "language_hint" not in raw):
        return None
    result = {**raw, "timing_provenance": provenance}
    if provenance == LEGACY_UNVERIFIED:
        result["legacy_duration_s"] = _stored_duration(raw, {}, meta, raw["response"])
    return result


@dataclass
class JobWordTimes:
    words: list[tuple[str, int, int, str]]
    provenance: str | None = None
    source: str | None = None
    reason: str | None = None
    owned: bool = False
    legacy_rows: bool = False


def legacy_word_rows(doc: dict[str, Any], source: dict[str, Any]) -> bool:
    """True for provider rows written before converted word files recorded their own binding."""
    return not (source.get("recording_sha256") or doc.get("recording_sha256")
                or source.get("recognition_attempt_id")
                or LEGACY_UNVERIFIED in (doc.get("timing_provenance"), source.get("timing_provenance")))


def _word_documents(job: Job, video: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Use a single purchased answer when one exists, otherwise offer the job's local files."""
    documents = [(name, _document(path)) for name, path in
                 (("source_words", job.source_words), ("caption_words", job.caption_words)) if path.is_file()]
    with _ledger() as db:
        purchases = db.execute("""SELECT p.*, a.recording_sha256 FROM paid_attempts p
                               JOIN approvals a ON a.id = p.receipt_id
                               WHERE a.job_key = ? AND p.scope = 'source' AND p.status = 'succeeded'
                               ORDER BY p.rowid DESC""", (_job_key(job),)) if db else ()
        purchase = None
        for candidate in purchases:
            if candidate["recording_sha256"] == media_digest(video):
                purchase = candidate
                break
        if purchase is not None:
            for name, doc in documents:
                source = doc.get("source")
                if not isinstance(source, dict) or source.get("service") != SERVICE:
                    continue
                attempt = source.get("recognition_attempt_id")
                if attempt == purchase["id"]:
                    selected = (name, doc)
                    break
                if not attempt:   # Artifacts created before successful attempt IDs were recorded.
                    meta_path = job.caption_choice if name == "caption_words" else job.root / META_NAME
                    meta = _document(meta_path, yaml=True)
                    if (meta.get("receipt_id") == purchase["receipt_id"]
                            and meta.get("language_hint") == purchase["language_hint"]
                            and meta.get("created_at") == purchase["created_at"]):
                        selected = (name, doc)
                        break
            else:
                cached = _saved(db, job, purchase["answer_key"])
                if cached is None:
                    raise RecognitionBlocked(ATTEMPT_UNCLEAR, "the latest purchased word times are missing")
                from .captions import words_json
                doc = words_json(json.loads(cached["response"]))
                doc["source"]["recording_sha256"] = purchase["recording_sha256"]
                selected = ("recognition_cache", doc)
    if purchase is not None:
        yield selected
    else:
        yield from documents


def _job_word_candidates(job: Job, video: Path, probe: Callable[[Path], dict[str, Any]] | None = None,
                         duration_s: float | None = None) -> Iterator[JobWordTimes]:
    from .analysis import align_words
    unreadable = "the word times of the recording could not be read"
    try:
        for name, doc in _word_documents(job, video):
            if not doc:
                yield JobWordTimes([], reason=unreadable)
                continue
            kind = "recording" if name == "source_words" else "captions"
            provenance, reason = _timing_validation(job, video, doc, kind, probe, duration_s)
            if reason:
                yield JobWordTimes([], reason=reason)
                continue
            source = doc["source"] if isinstance(doc.get("source"), dict) else {}
            digest = doc.get("recording_sha256") or source.get("recording_sha256")
            owned = bool(digest or SERVICE in (doc.get("service"), source.get("service")))
            try:
                words = align_words.source_words(doc, legacy=owned and not digest)
            except (TypeError, ValueError):
                yield JobWordTimes([], reason=unreadable)
                continue
            if provenance == LEGACY_UNVERIFIED:
                words = [(w, start, end, LEGACY_UNVERIFIED if origin == "measured" else origin)
                         for w, start, end, origin in words]
            yield JobWordTimes(words, provenance, name, owned=owned,
                               legacy_rows=owned and legacy_word_rows(doc, source))
    except VPError as exc:
        yield JobWordTimes([], reason=exc.msg)
    except (OSError, ValueError, TypeError):
        yield JobWordTimes([], reason=unreadable)


def read_job_word_times(job: Job, *, video: str | os.PathLike | None = None,
                        probe: Callable[[Path], dict[str, Any]] | None = None) -> JobWordTimes:
    """Read recording-relative word times in milliseconds, including per-word provenance."""
    reason = None
    for timing in _job_word_candidates(job, _video_path(job, video), probe):
        if timing.words:
            return timing
        if timing.reason == LOCAL_RECORDING_CHANGED or reason is None:
            reason = timing.reason
    return JobWordTimes([], reason=reason or NO_WORD_TIMES)


def _legacy_recognition(job: Job, video: Path, duration_s: float) -> dict[str, Any] | None:
    raw = _document(job.root / RAW_NAME)
    for purpose, path in (("captions", job.caption_choice), ("transcribe", job.root / META_NAME)):
        meta = _document(path, yaml=True)
        if (meta.get("service") == SERVICE and not meta.get("recording_sha256")
                and _timing_validation(job, video, meta, purpose, duration_s=duration_s)[0] == VERIFIED):
            at = meta.get("created_at") or meta.get("at") or raw.get("created_at") or "an unrecorded date"
            known = "language_hint" in meta or "language_hint" in raw
            return {"id": None, "created_at": str(at), "language_hint": meta.get("language_hint", raw.get("language_hint")),
                    "language_hint_known": known, "status": "succeeded"}
    if (raw.get("service") == SERVICE and not raw.get("recording_sha256")
            and _timing_validation(job, video, raw, "transcribe", duration_s=duration_s)[0] == VERIFIED):
        return {"id": None, "created_at": str(raw.get("created_at") or "an unrecorded date"),
                "language_hint": raw.get("language_hint"), "language_hint_known": "language_hint" in raw,
                "status": "succeeded"}
    return None


def _history(db: sqlite3.Connection | None, sound: str, key: str) -> list[dict[str, Any]]:
    if db is None:
        return []
    rows = db.execute("""SELECT id, created_at, language_hint, status, seconds FROM paid_attempts
                        WHERE (sound = ? OR answer_key = ?) AND status IN ('succeeded', 'ambiguous')
                        ORDER BY rowid""", (sound, key))
    return [dict(row) for row in rows]


def _hint_label(hint: str | None, known: bool = True) -> str:
    if hint:
        return hint
    return "automatic language detection" if known else "an unrecorded language hint"


def _charges(history: list[dict[str, Any]], ambiguous: sqlite3.Row | None,
             language: str | None, amount: float) -> dict[str, Any]:
    details = [{"attempt_id": entry["id"], "at": entry["created_at"], "status": entry["status"],
                "language_hint": entry["language_hint"], "language_hint_known": entry.get("language_hint_known", True),
                "seconds": entry.get("seconds")} for entry in history]
    previous = next((entry for entry in reversed(details) if entry["status"] == "succeeded"), None)
    warnings = []
    if amount and previous and previous["language_hint"] != language:
        hint = _hint_label(previous["language_hint"], previous["language_hint_known"])
        warnings.append(f"this sound was already recognised on {previous['at']} with {hint}; "
                        f"recognising it again is a new charge of up to ${amount:.8g}")
    if ambiguous:
        warnings.append(f"{UNCLEAR_CHARGE}; this retry may be charged again, up to ${amount:.8g}")
    earlier = previous or (details[-1] if details else None)
    return {"previous_recognition": earlier, "recognition_history": details,
            "ambiguous_attempt": ambiguous["id"] if ambiguous else None,
            "charge_warning": ". ".join(warnings) or None}


def _source_retry_receipt(db: sqlite3.Connection, job: Job, previous: sqlite3.Row,
                          recording_sha256: str, sound: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM approvals WHERE id = ? AND job_key = ?",
                     (previous["receipt_id"], _job_key(job))).fetchone()
    if (previous["scope"] == "source" and previous["seconds"] and row is not None
            and row["recording_sha256"] == recording_sha256 and row["audio_sha256"] == sound
            and row["source_spent"] >= previous["seconds"] and row["service"] == SERVICE
            and row["model"] == MODEL_ID and row["rate_cents_per_hour"] == round(PRICE_USD_PER_HOUR * 100)):
        return row
    raise RecognitionBlocked(ATTEMPT_UNCLEAR, f"{UNCLEAR_CHARGE}; its original source receipt could not be verified")


def _source_retry_amounts(row: sqlite3.Row, seconds: int) -> dict[str, Any]:
    prior = row["source_spent"]
    total = prior + seconds
    increment = max(0, total - row["source_seconds"])
    cents = max(0, _cost_cents(row["source_seconds"] + increment + row["checks_seconds"]) - row["cents"])
    return {"retry_source": True, "retry_receipt_id": row["id"], "retry_seconds": seconds,
            "retry_cost_usd": _at_rate(seconds), "retry_increment_seconds": increment,
            "retry_increment_usd": _at_rate(increment), "retry_increment_cents": cents,
            "prior_possible_source_spend_seconds": prior, "prior_possible_source_spend_usd": _at_rate(prior),
            "possible_source_total_seconds": total, "possible_source_total_usd": _at_rate(total)}


@contextmanager
def source_quote(job: Job, video: str | os.PathLike | None, language: str | None, diarize: bool,
                 probe: Callable[[Path], dict[str, Any]] | None, config: dict[str, Any],
                 purpose: str, *, checks_only: bool = False, info: dict[str, Any] | None = None,
                 reel_item: str | None = None):
    """Quote the exact sound handed to the caller, or price reel checks alone."""
    from .steps.s5_render import caption_qa_settings
    info = _recording_info(job, video, probe) if info is None else info
    if info["too_long"] and not checks_only:
        yield info, None
        return
    path = Path(info["video"]).resolve()
    language = language_hint(job, language)
    with _hash_progress(job, path), tempfile.TemporaryDirectory(prefix="vp-recognition-") as tmp:
        recording_sha256 = media_digest(path)
        audio, audio_sha256, previous, latest, ambiguous, saved = None, None, None, None, None, False
        source_blocked, history, retry_details = None, [], {}
        source_seconds = 0
        if not checks_only:
            changed = "the recording changed while its sound was being prepared"
            audio = _verified_audio(Path(info["video"]), Path(tmp), recording_sha256, changed)
            if _video_path(job, video).resolve() != path:
                raise RecognitionBlocked(NO_APPROVAL, changed)
            audio_sha256 = media_digest(audio)
            key = _request_key(audio_sha256, language)
            with _ledger() as db:
                previous = _attempt(db, audio_sha256, key, statuses="charged")
                latest = _attempt(db, audio_sha256, key)
                history = _history(db, audio_sha256, key)
                saved = (_saved(db, job, key) is not None
                         or local_response(job, Path(info["video"]), purpose, language, diarize,
                                           probe, info["duration_s"]) is not None)
                paid_for_hint = _paid_for_hint(db, key)
                if not saved:
                    source_seconds = _upload_seconds(audio, probe)
                    if previous and previous["status"] == "ambiguous":
                        ambiguous = previous
                        try:
                            original = _source_retry_receipt(db, job, previous, recording_sha256, audio_sha256)
                            retry_details = _source_retry_amounts(original, source_seconds)
                        except RecognitionBlocked as exc:
                            source_blocked = exc.msg
                    elif paid_for_hint:
                        source_blocked = ANSWER_MISSING
            local_prior = _legacy_recognition(job, Path(info["video"]), info["duration_s"])
            if local_prior is not None:
                history.insert(0, local_prior)
        mode = caption_qa_settings(config)["transcribe"]
        reels, reels_s, reel_billable_seconds = None, 0.0, 0
        if purpose == "captions":
            from .captions import reels as reel_spans
            reels, reels_s, reel_billable_seconds = reel_spans(job, config, info, item=reel_item)
        checks_s = min(info["duration_s"] * CHECKS_SHARE, CHECKS_CAP_S) if purpose == "transcribe" else reels_s
        checks_s = min(checks_s, CHECKS_CAP_S) if mode != "off" else 0.0
        checks_seconds = min(CHECKS_CAP_S, math.ceil(checks_s) if purpose == "transcribe" else reel_billable_seconds)
        if mode == "off" or ambiguous:
            checks_seconds = 0
        covers = (["source_retry"] if ambiguous else ["source"]) if not checks_only else []
        if checks_seconds:
            covers.append("reel_checks")
        cents = retry_details["retry_increment_cents"] if retry_details else _cost_cents(source_seconds + checks_seconds)
        receipt = {
            "schema": 2, "purpose": purpose, "job": str(job.root), "recording": str(path),
            "recording_sha256": recording_sha256, "audio_sha256": audio_sha256,
            "service": SERVICE, "model": MODEL_ID, "language_hint": language, "diarize": diarize,
            "covers": covers, "reel_scope": [reel_item] if reel_item is not None else "all",
            "checks_only": checks_only, "source_seconds": source_seconds, "checks_seconds": checks_seconds,
            "source_blocked_note": source_blocked,
            "approved_cents": cents, "cost_usd": cents / 100,
            "cost_source_usd": _at_rate(source_seconds),
            "cost_checks_max_usd": _at_rate(checks_seconds),
            "checks_share": CHECKS_SHARE, "checks_cap_s": CHECKS_CAP_S,
            "last_attempt": latest["id"] if previous and latest and not saved else None,
            **_charges(history, ambiguous, language, _at_rate(source_seconds)),
            **retry_details,
        }
        with _ledger() as db:
            approval = _active_approval(db, job)
        yield ({**info, **receipt, "quote_id": sha256_obj(receipt), "reels": reels,
                "reels_s": 0.0 if ambiguous else checks_s,
                "saved_answer": saved, **_balance_fields(approval)}, audio)


def _approve(job: Job, quote: dict[str, Any]) -> str:
    if quote.get("ambiguous_attempt"):
        raise RecognitionBlocked(ATTEMPT_UNCLEAR, "a source retry requires an attempt-bound decision")
    receipt_id = quote["quote_id"]
    if approval_path(job).exists() and not (vp_home() / LEDGER_NAME).exists():
        raise RecognitionBlocked(ATTEMPT_UNCLEAR, "the existing approval ledger could not be found")
    reel_scope = quote.get("reel_scope", "all")
    with _ledger(write=True) as db, db:
        db.execute("""INSERT OR IGNORE INTO approvals
                   (id, job_key, recording_sha256, audio_sha256, language_hint, diarize, service,
                    model, rate_cents_per_hour, cents, source_seconds, checks_seconds, approved_at, reel_scope)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (receipt_id, _job_key(job), quote["recording_sha256"], quote.get("audio_sha256"),
                    quote.get("language_hint"), quote.get("diarize", False), quote.get("service", SERVICE),
                    quote.get("model", MODEL_ID), round(PRICE_USD_PER_HOUR * 100), quote["approved_cents"],
                    quote.get("source_seconds", 0), quote.get("checks_seconds", 0), now_iso(),
                    json.dumps(reel_scope)))
    path = approval_path(job)
    marker = {"receipt_id": receipt_id, "reel_scope": reel_scope}
    if read_json(path, default=None) != marker:
        write_json(path, marker)
    return receipt_id


def _active_approval(db: sqlite3.Connection | None, job: Job) -> sqlite3.Row | None:
    marker = _document(approval_path(job))
    return db.execute("SELECT * FROM approvals WHERE id = ? AND job_key = ?",
                      (marker.get("receipt_id"), _job_key(job))).fetchone() if db else None


def _balance_fields(row: sqlite3.Row | None) -> dict[str, Any]:
    balance = {"active_receipt_id": row["id"] if row else None}
    for prefix in ("source", "checks"):
        spent = row[f"{prefix}_spent"] if row else 0
        remaining = row[f"{prefix}_seconds"] - spent if row else 0
        balance.update({f"{prefix}_spent_seconds": spent, f"{prefix}_remaining_seconds": remaining,
                        f"{prefix}_spent_usd": _at_rate(spent), f"{prefix}_remaining_usd": _at_rate(remaining)})
    return balance


def approval_reason(job: Job) -> str:
    raw = _document(job.root / RAW_NAME)
    meta = [_document(job.root / META_NAME, yaml=True), _document(job.caption_choice, yaml=True)]
    old = any(d.get("service") == SERVICE and not d.get("recording_sha256") for d in (raw, *meta))
    return OLD_APPROVAL if old else MISSING_APPROVAL


def reel_scope_covers(scope: Any, item: str | None) -> bool:
    return scope == "all" or (item is not None and isinstance(scope, list) and item in scope)


def _receipt(db: sqlite3.Connection | None, job: Job, recording_sha256: str, scope: str,
             item: str | None = None) -> sqlite3.Row:
    row = _active_approval(db, job)
    budget = "source_seconds" if scope == "source" else "checks_seconds"
    if (row is not None and row["recording_sha256"] == recording_sha256 and row[budget] > 0
            and row["service"] == SERVICE and row["model"] == MODEL_ID
            and row["rate_cents_per_hour"] == round(PRICE_USD_PER_HOUR * 100)):
        if scope == "reel_checks" and not reel_scope_covers(json.loads(row["reel_scope"]), item):
            raise RecognitionBlocked(NO_APPROVAL, "the active approval covers only the named reel checks")
        return row
    raise RecognitionBlocked(NO_APPROVAL, approval_reason(job) if row is None else MISSING_APPROVAL)


def require_reel_receipt(job: Job, recording_sha256: str, item: str | None = None) -> str | None:
    """Verify approval for this reel, or for all reels when no item is named."""
    with _ledger() as db:
        return _receipt(db, job, recording_sha256, "reel_checks", item)["language_hint"]


def reel_retry_revision(job: Job, item: str, render_sha256: str, receipt_id: str, *,
                        snapshot: bool = False) -> str | None:
    """Read the latest decision without initializing or migrating the ledger."""
    path = (vp_home() / LEDGER_NAME).absolute()
    if not path.is_file():
        return None
    # A plan cannot initialize SQLite shared-memory state even in WAL mode.
    if snapshot and path.with_name(path.name + "-wal").exists():
        raise RecognitionBlocked(ATTEMPT_UNCLEAR, "the recognition ledger is updating")
    mode = "?mode=ro&immutable=1" if snapshot else "?mode=ro"
    try:
        with closing(sqlite3.connect(path.as_uri() + mode, uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'retry_decisions'").fetchone():
                return None
            row = db.execute("""SELECT d.id FROM retry_decisions d JOIN approvals a ON a.id = d.receipt_id
                               WHERE d.receipt_id = ? AND a.job_key = ? AND d.item = ? AND d.render_sha256 = ?
                               ORDER BY d.rowid DESC LIMIT 1""",
                             (receipt_id, _job_key(job), item, render_sha256)).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        raise RecognitionBlocked(ATTEMPT_UNCLEAR, "the recognition ledger could not be verified") from None


def _require_balance(row: sqlite3.Row, seconds: int, scope: str) -> str:
    prefix = "source" if scope == "source" else "checks"
    if (row[f"{prefix}_spent"] + seconds > row[f"{prefix}_seconds"]
            or row["source_spent"] + row["checks_spent"] + seconds > row["cents"] * SECONDS_PER_CENT):
        label = "source" if scope == "source" else "reel check"
        raise RecognitionBlocked(AMOUNT_USED, f"the approved {label} amount does not cover another recognition request")
    return f"{prefix}_spent"


@contextmanager
def _attempt_lock(sound: str):
    # Locks are process-owned and released on death; the durable reservation survives a crash.
    directory = vp_home() / "recognition-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / sound).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecognitionBlocked(ATTEMPT_UNCLEAR, "an earlier recognition request is still in progress") from None
        yield


def _reel_retry_basis(db: sqlite3.Connection, sound: str, key: str,
                     previous: sqlite3.Row | None) -> sqlite3.Row | None:
    latest_reel = _attempt(db, sound, key, scope="reel_checks")
    if latest_reel and latest_reel["status"] == "rejected":
        return latest_reel
    return previous if previous and previous["status"] == "ambiguous" else None


def _recognize(job: Job, audio: Path, sound: str, recording_sha256: str, scope: str,
               seconds: int, language: str | None, diarize: bool, urlopen: Urlopen | None,
               *, approval: dict[str, Any] | None = None, item: str | None = None,
               render_sha256: str | None = None) -> tuple[dict[str, Any], bool]:
    """Return this job's saved response or reserve and perform one paid request."""
    key = _request_key(sound, language)
    source_retry = bool(approval and approval.get("retry_source"))
    with _attempt_lock(sound), _ledger(write=True) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            saved = _saved(db, job, key)
            if saved is not None:
                if diarize and not saved["diarize"]:
                    raise VPError("data", "the saved recognition does not include speaker labels")
                return dict(saved, response=json.loads(saved["response"])), False
            if seconds <= 0:
                raise RecognitionBlocked(ATTEMPT_UNCLEAR, "the quoted saved recognition is no longer available")
            previous = _attempt(db, sound, key, statuses="charged")
            latest = _attempt(db, sound, key)
            decision = None
            increment, cents_increment = 0, 0
            if source_retry:
                if (scope != "source" or not previous or previous["status"] != "ambiguous"
                        or approval["ambiguous_attempt"] != previous["id"] or approval["last_attempt"] != latest["id"]
                        or approval["language_hint"] != language or approval["diarize"] != diarize
                        or db.execute("SELECT 1 FROM retry_decisions WHERE id = ?", (approval["quote_id"],)).fetchone()):
                    raise RecognitionBlocked(ATTEMPT_UNCLEAR, "the source retry changed; use a fresh retry quote")
                row = _source_retry_receipt(db, job, previous, recording_sha256, sound)
                amounts = _source_retry_amounts(row, seconds)
                if any(approval[field] != value for field, value in amounts.items()):
                    raise RecognitionBlocked(NO_APPROVAL, STALE_QUOTE)
                increment, cents_increment = amounts["retry_increment_seconds"], amounts["retry_increment_cents"]
                if increment or cents_increment:
                    db.execute("UPDATE approvals SET source_seconds = source_seconds + ?, cents = cents + ? WHERE id = ?",
                               (increment, cents_increment, row["id"]))
                    row = db.execute("SELECT * FROM approvals WHERE id = ?", (row["id"],)).fetchone()
            else:
                row = _receipt(db, job, recording_sha256, scope, item)
                if (row["language_hint"] != language or (scope == "source"
                        and (row["audio_sha256"] != sound or row["diarize"] != diarize))):
                    raise RecognitionBlocked(NO_APPROVAL, "the sound or language hint differs from the approved quote")
            retry_basis = _reel_retry_basis(db, sound, key, previous) if scope == "reel_checks" else None
            if retry_basis:
                if retry_basis["status"] == "rejected" and _paid_for_hint(db, key):
                    raise RecognitionBlocked(ATTEMPT_UNCLEAR, ANSWER_MISSING)
                decision = db.execute("""SELECT * FROM retry_decisions WHERE receipt_id = ? AND sound = ?
                             AND answer_key = ? AND previous_id = ? AND retry_of = ? AND item = ?
                             AND render_sha256 = ? AND consumed_at IS NULL""",
                                      (row["id"], sound, key, latest["id"], retry_basis["id"],
                                       item, render_sha256)).fetchone()
                if decision is None or decision["seconds"] != seconds:
                    if retry_basis["status"] == "rejected":
                        raise RecognitionBlocked(REEL_RETRY_REQUIRED, REEL_RETRY_NOTE)
                    raise RecognitionBlocked(ATTEMPT_UNCLEAR, UNCLEAR_CHARGE)
            elif previous and previous["status"] == "ambiguous":
                if not source_retry:
                    raise RecognitionBlocked(ATTEMPT_UNCLEAR, UNCLEAR_CHARGE)
            elif previous:
                if _paid_for_hint(db, key):
                    raise RecognitionBlocked(ATTEMPT_UNCLEAR, ANSWER_MISSING)
                if not approval or approval["last_attempt"] != latest["id"]:
                    raise RecognitionBlocked(NO_APPROVAL,
                                             "this sound was already recognised; a fresh quote must show the new charge")
            elif approval and approval["last_attempt"]:
                raise RecognitionBlocked(NO_APPROVAL, STALE_QUOTE)
            spent = _require_balance(row, seconds, scope)
            api_key, _ = find_key()
            if not api_key:
                raise VPError("auth", "NEED_KEY:elevenlabs — no ElevenLabs key yet")
            if audio.stat().st_size > MAX_UPLOAD_BYTES:
                raise VPError("data", "the extracted sound exceeds the service upload limit")
            attempt_id, created = uuid.uuid4().hex, now_iso()
            if source_retry:
                db.execute("""INSERT INTO retry_decisions
                           (id, receipt_id, sound, answer_key, previous_id, retry_of, seconds, item,
                            render_sha256, approved_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?, ?)""",
                           (approval["quote_id"], row["id"], sound, key, latest["id"], previous["id"],
                            seconds, created, created))
            elif decision:
                db.execute("UPDATE retry_decisions SET consumed_at = ? WHERE id = ?", (created, decision["id"]))
            db.execute(f"UPDATE approvals SET {spent} = {spent} + ? WHERE id = ?", (seconds, row["id"]))
            db.execute("""INSERT INTO paid_attempts
                       (id, sound, answer_key, language_hint, receipt_id, scope, seconds, status,
                        created_at, retry_of, decision_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'ambiguous', ?, ?, ?)""",
                       (attempt_id, sound, key, language, row["id"], scope, seconds, created,
                        retry_basis["id"] if retry_basis else previous["id"] if source_retry else None,
                        decision["id"] if decision else approval["quote_id"] if approval else None))
        # Durable before POST. Unknown outcomes keep their reservation, even after a process dies.
        try:
            resp = call_api(audio, api_key, language, diarize, urlopen)
        except RejectedRecognition as exc:
            # Refund the reservation. The rejected attempt still requires a new reel decision.
            with db:
                db.execute(f"UPDATE approvals SET {spent} = {spent} - ? WHERE id = ?", (seconds, row["id"]))
                if increment or cents_increment:
                    db.execute("UPDATE approvals SET source_seconds = source_seconds - ?, cents = cents - ? WHERE id = ?",
                               (increment, cents_increment, row["id"]))
                db.execute("UPDATE paid_attempts SET status = 'rejected' WHERE id = ?", (attempt_id,))
            if scope == "reel_checks":
                raise RecognitionBlocked(REEL_RETRY_REQUIRED, f"{exc.msg}. {REEL_RETRY_NOTE}", exc.kind,
                                         attempted=True) from None
            raise
        except (VPError, OSError) as exc:
            kind = exc.kind if isinstance(exc, VPError) else "transport"
            raise RecognitionBlocked(ATTEMPT_UNCLEAR,
                                     "the recognition attempt may already have been charged", kind,
                                     attempted=True) from None
        try:
            with db:
                db.execute("UPDATE paid_attempts SET status = 'succeeded' WHERE id = ?", (attempt_id,))
                db.execute("""INSERT INTO cache.answers
                           (job_key, answer_key, diarize, response, created_at) VALUES (?, ?, ?, ?, ?)""",
                           (_job_key(job), key, diarize, json.dumps(resp, ensure_ascii=False), created))
        except sqlite3.Error:
            raise RecognitionBlocked(ATTEMPT_UNCLEAR, "the recognition answer could not be saved",
                                     attempted=True) from None
        return {"response": resp, "created_at": created, "attempt_id": attempt_id}, True


def reel_retry_quote(job: Job, item: str, audio: Path, recording_sha256: str, render_sha256: str,
                     approved_quote: str | None = None) -> dict[str, Any]:
    """Price or record one retry, bound to the render, sound, receipt and attempt."""
    sound, seconds = media_digest(audio), _upload_seconds(audio)
    with _attempt_lock(sound), _ledger(write=approved_quote is not None) as db:
        if db is None:
            raise RecognitionBlocked(NO_APPROVAL, MISSING_APPROVAL)
        with db:
            if approved_quote is not None:
                db.execute("BEGIN IMMEDIATE")
            row = _receipt(db, job, recording_sha256, "reel_checks", item)
            language = row["language_hint"]
            key = _request_key(sound, language)
            previous = _attempt(db, sound, key, statuses="charged")
            latest = _attempt(db, sound, key)
            retry_basis = _reel_retry_basis(db, sound, key, previous)
            if retry_basis is None:
                raise VPError("data", "there is no ambiguous or rejected recognition attempt for this reel's sound")
            if retry_basis["status"] == "rejected" and _paid_for_hint(db, key):
                raise RecognitionBlocked(ATTEMPT_UNCLEAR, ANSWER_MISSING)
            amount = _at_rate(seconds)
            ambiguous = previous if previous and previous["status"] == "ambiguous" else None
            data = {"retry_reel": item, "render_sha256": render_sha256, "audio_sha256": sound,
                    "receipt_id": row["id"], "last_attempt": latest["id"], "language_hint": language,
                    "retry_seconds": seconds, "retry_cost_usd": amount,
                    "checks_share": CHECKS_SHARE, "checks_cap_s": CHECKS_CAP_S,
                    **_charges(_history(db, sound, key), ambiguous, language, amount)}
            if retry_basis["status"] == "rejected":
                data["rejected_attempt"] = retry_basis["id"]
                charge = data["charge_warning"] or f"this new request may cost up to ${amount:.8g}"
                data["charge_warning"] = f"The provider rejected the latest reel check. {charge}"
            token = sha256_obj(data)
            if approved_quote is not None and approved_quote != token:
                raise RecognitionBlocked(NO_APPROVAL, "the reel retry changed; use a fresh retry quote")
            try:
                _require_balance(row, seconds, "reel_checks")
            except RecognitionBlocked as exc:
                if approved_quote is not None:
                    raise
                return {**data, **_balance_fields(row), **exc.result(), "quote_id": None, "offer": False}
            if approved_quote is not None:
                db.execute("""INSERT OR IGNORE INTO retry_decisions
                           (id, receipt_id, sound, answer_key, previous_id, retry_of, seconds, item,
                            render_sha256, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (token, row["id"], sound, key, latest["id"], retry_basis["id"], seconds, item,
                            render_sha256, now_iso()))
    return {**data, **_balance_fields(row), "quote_id": token,
            "offer": approved_quote is None, "sent": False, "words": None,
            "retry_approved": approved_quote is not None,
            "note": data["charge_warning"]}


def source_result(job: Job, quote: dict[str, Any], audio: Path, approved_quote: str | None,
                  urlopen: Urlopen | None, *, retry: bool = False) -> tuple[dict[str, Any], bool]:
    """Validate the shown quote and recover a local answer without a receipt."""
    if quote["source_blocked_note"]:
        raise RecognitionBlocked(ATTEMPT_UNCLEAR, quote["source_blocked_note"])
    if quote["ambiguous_attempt"] and (not retry or approved_quote != quote["quote_id"]):
        raise RecognitionBlocked(ATTEMPT_UNCLEAR, quote["charge_warning"])
    if not quote["saved_answer"] or retry:
        if retry and not quote["ambiguous_attempt"]:
            raise VPError("data", "there is no ambiguous recognition attempt for this source audio")
        if approved_quote != quote["quote_id"]:
            raise RecognitionBlocked(NO_APPROVAL, STALE_QUOTE if approved_quote else
                                     "approve the price shown for this recording with --quote")
    if approved_quote == quote["quote_id"] and not retry:
        _approve(job, quote)
    sound, hint = quote["audio_sha256"], quote["language_hint"]
    if quote["saved_answer"]:
        raw = local_response(job, Path(quote["video"]), quote["purpose"], hint, quote["diarize"],
                             duration_s=quote["duration_s"])
        if raw is not None:
            return {**raw, "created_at": raw.get("created_at"), "receipt_id": approved_quote}, False
        with _ledger() as db:
            cached = _saved(db, job, _request_key(sound, hint))
        if cached is None:
            raise RecognitionBlocked(ATTEMPT_UNCLEAR, "the quoted saved recognition is no longer available")
        if quote["diarize"] and not cached["diarize"]:
            raise VPError("data", "the saved recognition does not include speaker labels")
        return dict(cached, response=json.loads(cached["response"]), receipt_id=approved_quote), False
    try:
        result, sent = _recognize(job, audio, sound, quote["recording_sha256"], "source", quote["source_seconds"],
                                  hint, quote["diarize"], urlopen, approval=quote)
    except RecognitionBlocked as exc:
        if retry and exc.attempted and quote["charge_warning"]:
            raise RecognitionBlocked(exc.reason, f"{exc.msg}. {quote['charge_warning']}",
                                     exc.kind, attempted=True) from None
        raise
    return {**result, "receipt_id": quote["retry_receipt_id"] if retry else approved_quote}, sent


def tracked_source_result(job: Job, step: str, quote: dict[str, Any], audio: Path, approved_quote: str | None,
                          urlopen: Urlopen | None, retry: bool) -> tuple[dict[str, Any], bool]:
    tracker = (progress.tracking(job.root, step, None, total=audio.stat().st_size, unit="bytes")
               if progress.current() is None else nullcontext())
    with tracker:
        return source_result(job, quote, audio, approved_quote, urlopen, retry=retry)


def result_metadata(quote: dict[str, Any], result: dict[str, Any], sent: bool) -> dict[str, Any]:
    meta = {k: quote[k] for k in ("service", "model", "recording_sha256", "audio_sha256", "diarize",
                                   "duration_s", "language_hint", "cost_usd", "cost_source_usd",
                                   "cost_checks_max_usd", "price_source", "charge_warning")} | {
        "receipt_id": result["receipt_id"], "created_at": result["created_at"], "cost_usd_estimate": _at_rate(quote["source_seconds"]) if sent else 0.0,
        "sent": sent,
    }
    if result.get("timing_provenance") == LEGACY_UNVERIFIED:
        meta.pop("recording_sha256")
        meta["timing_provenance"] = LEGACY_UNVERIFIED
        meta["legacy_duration_s"] = result["legacy_duration_s"]
    if "timing_provenance" in result and "language_hint" not in result:
        meta.pop("language_hint")
    attempt_id = result.get("attempt_id") or result.get("recognition_attempt_id")
    if attempt_id:
        meta["recognition_attempt_id"] = attempt_id
    return meta


def _bind_words(doc: dict[str, Any], meta: dict[str, Any]) -> None:
    source = doc["source"]
    if meta.get("timing_provenance") == LEGACY_UNVERIFIED:
        source.update(timing_provenance=LEGACY_UNVERIFIED, duration_s=meta["legacy_duration_s"])
        groups = [doc, *doc.get("segments", ())]
        for group in groups:
            for word in group.get("words", ()):
                if word["provenance"] == "measured":
                    word["provenance"] = LEGACY_UNVERIFIED
    else:
        source["recording_sha256"] = meta["recording_sha256"]
    if meta.get("recognition_attempt_id"):
        source["recognition_attempt_id"] = meta["recognition_attempt_id"]


def _srt_file_marker(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_ctime_ns]


def _write_outputs(job: Job, resp: dict[str, Any], duration_ms: int | None, diarize: bool,
                   meta: dict[str, Any]) -> dict[str, Any]:
    cues, words_json = convert(resp, duration_ms, diarize)
    outs = outputs(job)
    _bind_words(words_json, meta)
    write_json(outs["words"], words_json)
    srt = format_srt(cues, with_speaker=diarize).encode("utf-8")
    atomic_write(outs["srt"], srt)   # last subtitle output: presence = done
    n_words = len(load_words(words_json))
    meta = {**meta, "cues": len(cues), "words": n_words, "srt_sha256": sha256_bytes(srt),
            "srt_file": _srt_file_marker(outs["srt"]),
            "language": resp.get("language_code"),
            "language_probability": resp.get("language_probability"),
            "transcription_id": resp.get("transcription_id"),
            "outputs": {k: job.rel(p) for k, p in outs.items()}}
    write_yaml(job.root / META_NAME, meta)
    return meta


def run(job: Job, video: str | os.PathLike | None = None, language: str | None = None,
        diarize: bool = False, force: bool = False, urlopen: Urlopen | None = None,
        probe: Callable[[Path], dict[str, Any]] | None = None, *,
        quote: str | None = None, retry: bool = False) -> dict[str, Any]:
    """Transcribe the job's recording into `_cut.srt` + `_cut.json` (see module doc). Holds the
    job lock, so it never writes under a running `vp run`."""
    from .core.adoption import adopt

    other_targets = (*recognition_targets(job), job.root / META_NAME, job.caption_words, job.caption_choice)
    guard_inputs(job, *other_targets, video=video)
    guard_inputs(job, *outputs(job).values(), generated=("srt", "words"), video=video)
    with job_lock(job) as got:
        if not got:
            raise VPError("data", "the job is busy (another run holds job.lock): try again when it ends")
        adopt(job)
        return _run_locked(job, video, language, diarize, force, urlopen, probe, quote, retry)


def _run_locked(job: Job, video: str | os.PathLike | None, language: str | None, diarize: bool,
                force: bool, urlopen: Urlopen | None,
                probe: Callable[[Path], dict[str, Any]] | None, approved_quote: str | None,
                retry: bool) -> dict[str, Any]:
    from .design import job_config
    outs = outputs(job)
    for p in outs.values():
        if not _inside(job, p):
            raise VPError("data", f"the subtitles path {p} is outside the job folder: not written")
    if any(p.resolve() == (job.root / name).resolve() for p in outs.values()
           for name in (RAW_NAME, META_NAME, APPROVAL_NAME)):
        raise VPError("data", "the configured transcription outputs overlap")
    existing = [k for k, p in outs.items() if p.exists()]
    if existing and not force:
        raise VPError("data", f"the job already has {', '.join(existing)}: nothing sent "
                              "(--force permits replacing the subtitle outputs)")
    path = _video_path(job, video)
    raw = None
    if approved_quote is None and not retry and (job.root / RAW_NAME).is_file() and path.is_file():
        with _hash_progress(job, path):
            raw = local_response(job, path, "transcribe", language_hint(job, language), diarize, probe,
                                 recover=True)
    if raw is not None:
        info = _recording_info(job, video, probe)
        with _hash_progress(job, path):
            recording = media_digest(path) if raw["timing_provenance"] == VERIFIED else None
        quote = {**info, "recording": str(path), "recording_sha256": recording,
                 "audio_sha256": raw.get("audio_sha256"), "diarize": diarize,
                 "language_hint": language_hint(job, language), "cost_usd": 0,
                 "cost_source_usd": 0, "cost_checks_max_usd": 0, "charge_warning": None}
        result, sent = {**raw, "created_at": raw.get("created_at"), "receipt_id": None}, False
    else:
        with source_quote(job, video, language, diarize, probe, job_config(job), "transcribe") as (quote, audio):
            if audio is None:
                raise VPError("data", f"the recording is longer than {MAX_DURATION_S // 3600} h")
            result, sent = tracked_source_result(job, "transcribe", quote, audio, approved_quote, urlopen, retry)
    resp = result["response"]
    meta = result_metadata(quote, result, sent)
    if sent or not (job.root / RAW_NAME).exists():
        write_json(job.root / RAW_NAME, {**meta, "response": resp})
    return _write_outputs(job, resp, round(quote["duration_s"] * 1000), diarize,
                          {**meta, "video": job.rel(quote["recording"]), "reused_saved_response": not sent})


def import_srt(job: Job, src: str | os.PathLike, force: bool = False) -> dict[str, Any]:
    """The person's own subtitles file -> <job>/_cut.srt (validated: parses, not empty). The
    source file itself is only read."""
    from .core.adoption import adopt

    p = Path(src).expanduser()
    if not p.exists():
        raise VPError("data", f"subtitles file not found: {p}")
    dst = job.source_srt
    if not _inside(job, dst):
        raise VPError("data", f"the subtitles path {dst} is outside the job folder: not written")
    if p.resolve() == dst.resolve():
        cues = read_srt(p)
        return {"srt": job.rel(dst), "cues": len(cues), "duration_ms": srt_duration_ms(cues)}
    guard_inputs(job, job.lock_file, other_input=p)
    guard_inputs(job, dst, generated=("srt",), other_input=p)
    with job_lock(job) as got:
        if not got:
            raise VPError("data", "the job is busy (another run holds job.lock): try again when it ends")
        adopt(job)
        if dst.exists():
            metadata = _document(job.root / META_NAME, yaml=True)
            digest = metadata.get("srt_sha256") if metadata.get("service") == SERVICE else None
            if (not digest or not dst.is_file() or metadata.get("srt_file") != _srt_file_marker(dst)
                    or digest != sha256_bytes(dst.read_bytes())):
                raise VPError("data", f"the existing {dst.name} is a source subtitle file. "
                              "Save the new subtitles under another name and set source.srt in request.yaml to that file.")
            if not force:
                raise VPError("data", f"the job already has {dst.name} (--force replaces engine-generated subtitles)")
        cues = read_srt(p)
        if not cues:
            raise VPError("data", "the subtitles file has no subtitles in it")
        atomic_write(dst, p.read_bytes())
    return {"srt": job.rel(dst), "cues": len(cues), "duration_ms": srt_duration_ms(cues)}


# ---- word times of any media file (render QA) --------------------------------------------------

class ScribeTranscriber:
    """Receipt-gated recognition of the finished reel's audio, with word-time provenance."""

    name = SERVICE

    def __init__(self, urlopen: Urlopen | None = None, costs: Any = None):
        self.urlopen = urlopen
        self.costs = costs

    def words(self, job: Job, path: Path, recording_sha256: str,
              language: str | None = None, *, render_sha256: str | None = None,
              item: str | None = None) -> list[dict[str, Any]]:
        if render_sha256 is None or item is None:
            raise RecognitionBlocked(NO_APPROVAL, "there is no verified S5 render for this check")
        with tempfile.TemporaryDirectory(prefix="vp-recognition-") as tmp:
            audio = _verified_audio(path, Path(tmp), render_sha256, RENDER_CHANGED, reason=RENDER_MISMATCH)
            seconds = _upload_seconds(audio)
            result, sent = _recognize(job, audio, media_digest(audio), recording_sha256, "reel_checks",
                                      seconds, language, False, self.urlopen, item=item, render_sha256=render_sha256)
        if sent and self.costs is not None:
            try:
                self.costs.add("api", provider=SERVICE, op="caption_check", model=MODEL_ID,
                               usd=_at_rate(seconds), estimate=True)
            except OSError:
                pass    # the ledger already has the spend and the response
        return response_words(result["response"])


# ---- CLI ---------------------------------------------------------------------------------------

def checks_rule(quote: dict[str, Any]) -> str:
    share, cap = quote["checks_share"], quote["checks_cap_s"]
    return (f"reel check limit: selected reels for captions, or {share:.0%} of the recording for "
            f"transcription, up to {cap} billable seconds in either case")


def recognition_quote_lines(quote: dict[str, Any]) -> list[str]:
    lines = []
    history = quote.get("recognition_history", ())
    if history or quote.get("retry_source"):
        lines.append(f"requested language is {_hint_label(quote['language_hint'])}")
    for entry in history:
        hint = _hint_label(entry["language_hint"], entry["language_hint_known"])
        if entry["status"] == "succeeded":
            lines.append(f"recognised on {entry['at']} with {hint}")
        else:
            lines.append(f"attempt on {entry['at']} with {hint} may already have been charged")
    if quote.get("retry_source"):
        lines.extend([
            f"prior possible source spend on this receipt ${quote['prior_possible_source_spend_usd']:.8g}",
            f"new source retry may cost up to ${quote['retry_cost_usd']:.8g}",
            f"possible source total after this retry ${quote['possible_source_total_usd']:.8g}",
            f"retry-only increase ${quote['retry_increment_usd']:.8g} "
            f"({quote['retry_increment_seconds']} billable seconds, "
            f"${quote['retry_increment_cents'] / 100:.2f} increase to the rounded receipt ceiling)",
            f"reel checks remaining on the active receipt {quote['checks_remaining_seconds']} billable seconds",
        ])
    elif quote.get("charge_warning") and not quote.get("source_blocked_note"):
        lines.append(quote["charge_warning"])
    return lines


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


def has_saved_recognition(job: Job) -> bool:
    """Check the purge targets without opening the spend ledger or modifying the answer store."""
    raw = job.root / RAW_NAME
    if not raw.is_symlink() and _document(raw).get("service") == SERVICE:
        return True
    answers = (vp_home() / ANSWERS_NAME).absolute()
    try:
        for path in _database_files(answers):
            try:
                info = path.lstat()
            except FileNotFoundError:
                if path == answers:
                    return False
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                return False
        with closing(sqlite3.connect(answers.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute("SELECT 1 FROM answers WHERE job_key = ? LIMIT 1",
                              (_job_key(job),)).fetchone() is not None
    except (OSError, sqlite3.Error):
        return False


def purge_command(job: Job) -> str:
    return shlex.join(["vp", "transcribe", "purge", str(job.root), "--yes"])


def _purge_cli(job: Job, *, confirmed: bool, as_json: bool) -> int:
    targets = f"Remove cached recognition answers for {job.root} and its generated {RAW_NAME}."
    warning = "Recognising this recording again will be charged again."
    res = {"job": str(job.root), "raw_file": str(job.root / RAW_NAME), "action": targets, "warning": warning}
    lines = [targets, warning]
    if not confirmed:
        command = purge_command(job)
        _print({**res, "status": "confirmation_required", "confirm_command": command}, as_json,
               [*lines, f"To confirm, run {command}"])
        return 0
    try:
        removed = purge_job(job)
    except (VPError, OSError, ValueError) as exc:
        reason = exc.msg if isinstance(exc, VPError) else str(exc)
        _print({**res, "status": "error", "reason": reason}, as_json, [*lines, f"PURGE FAILED {reason}"])
        return 2
    _print({**res, "status": "purged", **removed}, as_json,
           [*lines, f"Removed {removed['answers_removed']} cached answer(s). "
                    f"Generated {RAW_NAME} removed: {'yes' if removed['raw_removed'] else 'no'}."])
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp transcribe",
                                 description="speech recognition (ElevenLabs Scribe) for a recording without subtitles")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("estimate", "run"):
        s = sub.add_parser(name)
        s.add_argument("job")
        s.add_argument("--video", help="the recording (default: the job's source video)")
        s.add_argument("--json", action="store_true")
        s.add_argument("--language", help="ISO 639-1/3 hint, e.g. ru (default: request.yaml "
                       "`language`, else auto)")
        s.add_argument("--diarize", action="store_true", help="label speakers (off by default)")
        if name == "run":
            s.add_argument("--quote", help="quote_id from the approved transcription estimate")
            s.add_argument("--force", action="store_true", help="replace existing subtitles")
            s.add_argument("--retry", action="store_true", help="approve a retry of an ambiguous source request")
    s = sub.add_parser("import")
    s.add_argument("job")
    s.add_argument("file")
    s.add_argument("--force", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("purge", help="remove this job's saved recognition answers")
    s.add_argument("job")
    s.add_argument("--yes", action="store_true", help="confirm removal of saved recognition")
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
    if a.cmd == "purge":
        return _purge_cli(job, confirmed=a.yes, as_json=a.json)
    if a.cmd == "estimate":
        e = estimate(job, a.video, language=a.language, diarize=a.diarize)
        lines = [
            f"recording {e['video']}, {_hms(e['duration_s'])}, {e['size_bytes'] / 1e6:.0f} MB"
            f" -> would send {e['upload']} only",
            f"key: {'present' if e['key'] else 'missing'}"
            + (f"; already has: {', '.join(e['outputs_exist'])}" if e["outputs_exist"] else "")]
        if e["too_long"]:
            lines.append(f"recording exceeds the {MAX_DURATION_S // 3600} h service limit")
        else:
            lines.extend(recognition_quote_lines(e))
            if e["source_blocked_note"]:
                lines.append(e["source_blocked_note"])
            elif e.get("retry_source"):
                lines.append(f"source retry quote {e['quote_id']} (approve with --retry --quote)")
            else:
                lines.append(f"total ${e['cost_usd']:.2f}, source ${e['cost_source_usd']:.8g}, "
                             f"reel checks up to ${e['cost_checks_max_usd']:.8g}; quote {e['quote_id']}")
                lines.append(checks_rule(e))
        _print(e, a.json, lines)
        return 0
    if a.cmd == "import":
        res = import_srt(job, a.file, a.force)
        _print(res, a.json, [f"IMPORTED {res['srt']} ({res['cues']} subtitles)"])
        return 0
    t0 = time.monotonic()
    try:
        res = run(job, a.video, a.language, a.diarize, a.force, quote=a.quote, retry=a.retry)
    except RecognitionBlocked as exc:
        _print(exc.result(), a.json, [f"RECOGNITION NOT COMPLETED: {exc.msg}"])
        return 2
    res["elapsed_s"] = round(time.monotonic() - t0, 1)
    lines = [f"TRANSCRIBED {res['outputs']['srt']}: {res['cues']} subtitles, "
             f"{res['words']} words, language {res['language']}"
             + (" (saved answer reused, nothing paid)" if res.get("reused_saved_response") else "")]
    if res["charge_warning"]:
        lines.append(res["charge_warning"])
    _print(res, a.json, lines)
    return 0
