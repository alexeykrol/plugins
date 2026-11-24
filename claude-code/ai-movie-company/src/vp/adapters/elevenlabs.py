"""Voice and music with the person's own ElevenLabs key (UX audit 2026-11-20, fix 1): the same key
that speech recognition uses (vp.keys service "elevenlabs"), saved once from the clipboard — no
claude.ai connector, no Claude restart. Files land straight in the job folder.

  vp voice list [--lang ru] [--limit N] [--json]           the account's voices (GET, free)
  vp voice estimate (--text-file F | --text T | --chars N | --job J) [--model M] [--json]
  vp voice say <job> --voice <id> (--text-file F | --text T) --out <job>/voice/<name>.mp3
               [--model eleven_v4] [--language ru] [--previous-text T] [--next-text T] [--force]
  vp voice scenes <job> --voice <id> [--only S1,S3] [--model M] [--language ru] [--force]
                                                           one file per scene: voice/S01.mp3 …
  vp voice samples <job> [--last 3] [--json]               the choice form's options: exactly the
                                                           newest sampled voices (free, no network)
  vp music estimate --seconds N [--json]
  vp music make <job> (--prompt-file F | --prompt T) --seconds N --out <job>/music/<name>.mp3
               [--model music_v2_5] [--vocals] [--force]

Bookkeeping is code: voice ids come only from `vp voice list` (the dialog passes them between
commands as data, never invents them); every generated file gets a sidecar (`<stem>.alignment.json`
for voice — character and word timings, so subtitle timing is code-owned; `<file>.provenance.json`
for music); the cost estimate goes to <vp_home>/costs.jsonl. A paid generation is never retried
automatically: a call that broke off may already be charged — the person decides.

Errors (SPEC §7.1, webapi): the key never reaches a message, a file or the cost journal.
  NEED_KEY:elevenlabs      no key (`vp keys set elevenlabs --from-clipboard`)
  BAD_KEY:elevenlabs       the key was refused, or lacks a permission (named in the message)
  NEED_CREDITS:elevenlabs  the account is out of credits
  NEED_PLAN:elevenlabs-music  music needs a paid ElevenLabs plan (nothing charged)
  429 / 5xx                transport class: try again later, by the person's decision

ElevenLabs API, checked 2026-11-20 against the current docs:
  https://elevenlabs.io/docs/api-reference/authentication  header `xi-api-key`; a key can be
      restricted by endpoint scope, credit limit and IP allowlist (a refused IP gives 403).
  https://elevenlabs.io/docs/api-reference/voices/search  GET /v2/voices: page_size <= 100,
      next_page_token / has_more, search, language, gender…; voices[]: voice_id, name, category,
      labels {gender, age, accent, language, use_case, description}, description, preview_url,
      verified_languages[] {language, model_id, accent, locale, preview_url}.
  https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps
      POST /v1/text-to-speech/{voice_id}/with-timestamps?output_format=mp3_44100_128 (the default
      format; 192 kbps needs Creator, PCM/WAV 44.1 kHz needs Pro); body text, model_id (default
      eleven_multilingual_v2 — ours is set explicitly), language_code (ISO 639-1, ignored by
      multilingual_v2), previous_text, next_text, seed; answer {audio_base64, alignment,
      normalized_alignment}, alignment = {characters[], character_start_times_seconds[],
      character_end_times_seconds[]}.
  https://elevenlabs.io/docs/overview/models  eleven_v4 — "most emotionally rich, expressive",
      90+ languages, recommended for content creation; 100,000 characters per request
      (eleven_v3 5,000; multilingual_v2 10,000; flash_v2_5 40,000). Music: music_v2_5 "our most
      advanced music model", music_v2; music_v1 deprecated.
  https://elevenlabs.io/docs/api-reference/music/compose  POST /v1/music?output_format=…; body
      prompt (or composition_plan), music_length_ms 3000..600000, model_id music_v1|music_v2|
      music_v2_5 (default music_v1 — set explicitly), force_instrumental; the answer is the audio
      file itself.
  https://elevenlabs.io/docs/overview/capabilities/music  "The Music API is available for paid
      subscribers"; generated music is 3 s .. 5 min (the stricter limit is used here).
  https://elevenlabs.io/pricing/api (2026-11-20)  TTS v4 / v3 / Multilingual v2 $0.08 per 1K
      characters, v4 Turbo / Flash $0.04; Music $0.15 per minute, 5 minute limit.
  docs/research/service-costs-2026-11-20.md §1.1 (elevenlabs.io/pricing): credits — TTS 1 per
      character, Eleven Music 900 per minute.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from ..core.costs import CostJournal
from ..core.errors import VPError, note_failure
from ..core.io import now_iso, write_json
from ..core.paths import Job, vp_home
from .. import keys as keystore
from . import webapi

SERVICE = "elevenlabs"
API = "https://api.elevenlabs.io"
VOICES_URL = f"{API}/v2/voices"
TTS_URL = API + "/v1/text-to-speech/{voice_id}/with-timestamps"
MUSIC_URL = f"{API}/v1/music"
OUTPUT_FORMAT = "mp3_44100_128"       # available on every plan (TTS and music)

TTS_MODEL = "eleven_v4"
# characters per request, https://elevenlabs.io/docs/overview/models#character-limits
TTS_MAX_CHARS = {"eleven_v4": 100_000, "eleven_v3": 5_000, "eleven_multilingual_v2": 10_000,
                 "eleven_flash_v2_5": 40_000, "eleven_turbo_v2_5": 40_000}
TTS_MAX_CHARS_UNKNOWN = 5_000
# USD per 1K characters, https://elevenlabs.io/pricing/api (2026-11-20)
TTS_USD_PER_1K = {"eleven_v4": 0.08, "eleven_v3": 0.08, "eleven_multilingual_v2": 0.08,
                  "eleven_v4_turbo": 0.04, "eleven_flash_v2_5": 0.04, "eleven_turbo_v2_5": 0.04}
TTS_CREDITS_PER_CHAR = 1              # service-costs §1.1 (elevenlabs.io/pricing)
TTS_PRICE_SOURCE = "https://elevenlabs.io/pricing/api (TTS v4 $0.08/1K chars, checked 2026-11-20)"

MUSIC_MODEL = "music_v2_5"
MUSIC_MIN_S, MUSIC_MAX_S = 3, 300     # 3 s .. 5 min (capabilities page; the API reference allows 10)
MUSIC_USD_PER_MIN = 0.15              # https://elevenlabs.io/pricing/api (2026-11-20)
MUSIC_CREDITS_PER_MIN = 900           # service-costs §1.1 (elevenlabs.io/pricing)
MUSIC_PRICE_SOURCE = "https://elevenlabs.io/pricing/api (Music $0.15/min, checked 2026-11-20)"

TTS_TIMEOUT_S = 600
MUSIC_TIMEOUT_S = 900
LIST_PAGES = 3                        # up to 300 voices

_VOICE_ID = re.compile(r"[A-Za-z0-9]{8,64}")
_STATUS = re.compile(r'"status"\s*:\s*"([^"]+)"')
_MESSAGE = re.compile(r'"message"\s*:\s*"([^"]*)"')
_PERMISSION = re.compile(r"permission[s]?\s+([a-z_]+)", re.I)
_PLAN_WORDS = re.compile(r"plan|subscri|tier|paid|upgrade", re.I)
_CREDIT_WORDS = re.compile(r"quota|credit", re.I)


class NeedPlan(VPError):
    """The account's plan does not include this (music: paid plans only). Nothing was charged."""

    def __init__(self, what: str, msg: str):
        self.code = f"NEED_PLAN:{SERVICE}-{what}"
        super().__init__("data", f"{self.code} — {msg}")


class NeedCredits(VPError):
    def __init__(self, msg: str):
        self.code = f"NEED_CREDITS:{SERVICE}"
        super().__init__("data", f"{self.code} — {msg}")


# ---- errors ---------------------------------------------------------------------------------

def _detail(hint: str) -> tuple[str, str]:
    st = _STATUS.search(hint or "")
    me = _MESSAGE.search(hint or "")
    return (st.group(1) if st else ""), (me.group(1) if me else "")


def classify(code: int, hint: str, where: str, music: bool = False) -> VPError | None:
    """ElevenLabs answers -> our codes. `hint` is the scrubbed error body. None = webapi default."""
    status, message = _detail(hint)
    text = f"{status} {message} {hint}"
    if status == "missing_permissions" or (code in (401, 403) and "missing the permission" in text):
        m = _PERMISSION.search(message or hint)
        perm = m.group(1) if m else "the needed one"
        return VPError("auth", f"BAD_KEY:{SERVICE} (no permission: {perm}) — {where}: the key works "
                               f"but this permission is off; turn it on for the key on the "
                               f"ElevenLabs API Keys page", http_status=code)
    if status == "quota_exceeded" or (code in (401, 402) and _CREDIT_WORDS.search(text)
                                      and not (music and _PLAN_WORDS.search(text))):
        return NeedCredits(f"the ElevenLabs account has run out of credits ({where}); nothing more "
                           f"was generated — top up or wait for the monthly credits")
    if music and (code == 402 or (code in (401, 403) and _PLAN_WORDS.search(text))):
        return NeedPlan("music", "music by API needs a paid ElevenLabs plan (the free plan has "
                                 "none); nothing was charged — go on without music, or with the "
                                 "person's own track")
    if code == 402:
        return NeedPlan("voice", f"the ElevenLabs plan does not allow this request ({where}); "
                                 f"nothing was charged")
    if code == 404 and ("voice" in text.lower() or "/text-to-speech/" in where):
        return VPError("data", f"{where}: HTTP 404 — this voice is not in the account; take a "
                               f"voice from `vp voice list`", http_status=code)
    return None


def _classify_music(code: int, hint: str, where: str) -> VPError | None:
    return classify(code, hint, where, music=True)


# ---- voices ---------------------------------------------------------------------------------

def _labels(v: dict[str, Any]) -> dict[str, str]:
    lab = v.get("labels") if isinstance(v.get("labels"), dict) else {}
    return {str(k).replace(" ", "_").lower(): str(val) for k, val in lab.items() if val}


def normalize_voice(v: dict[str, Any]) -> dict[str, Any]:
    """One /v2/voices entry -> the fields the dialog needs (no samples, no sharing data)."""
    lab = _labels(v)
    verified = [x for x in (v.get("verified_languages") or []) if isinstance(x, dict)]
    langs = []
    for x in verified:
        lg = str(x.get("language") or "").lower()
        if lg and lg not in langs:
            langs.append(lg)
    language = lab.get("language") or (langs[0] if langs else None)
    if language and language.lower() not in langs:
        langs.insert(0, language.lower())
    return {"voice_id": str(v.get("voice_id") or ""), "name": str(v.get("name") or ""),
            "gender": lab.get("gender"), "age": lab.get("age"), "accent": lab.get("accent"),
            "language": language, "languages": langs, "use_case": lab.get("use_case"),
            "description": v.get("description") or lab.get("description"),
            "category": v.get("category"), "preview_url": v.get("preview_url")}


def list_voices(lang: str | None = None, limit: int = 100, *, opener: Any = None, home: Any = None,
                environ: Any = None) -> dict[str, Any]:
    """The account's voices. With `lang`, voices labelled with that language come first; when none
    is labelled with it, all are returned (eleven_v4 speaks 90+ languages with any voice)."""
    key = keystore.require_key(SERVICE, home, environ)
    voices: list[dict[str, Any]] = []
    token = None
    for _ in range(LIST_PAGES):
        q = {"page_size": "100", "include_total_count": "false"}
        if token:
            q["next_page_token"] = token
        data = webapi.request_json("GET", f"{VOICES_URL}?{urllib.parse.urlencode(q)}",
                                   headers={"xi-api-key": key}, opener=opener, secrets=(key,),
                                   service=SERVICE, classify=classify)
        if not isinstance(data, dict) or not isinstance(data.get("voices"), list):
            raise VPError("contract", "ElevenLabs voice list: no `voices` in the answer")
        voices += [normalize_voice(v) for v in data["voices"] if isinstance(v, dict) and v.get("voice_id")]
        token = data.get("next_page_token")
        if not data.get("has_more") or not token:
            break
    out: dict[str, Any] = {"lang": lang, "count": 0, "voices": []}
    if lang:
        lg = lang.lower()
        match = [v for v in voices if lg in v["languages"] or (v["language"] or "").lower() == lg]
        if match:
            voices = match
        else:
            out["note"] = (f"no voice is labelled {lang!r}; every voice can speak it with "
                           f"{TTS_MODEL} (multilingual)")
    out["voices"] = voices[:max(1, int(limit))]
    out["count"] = len(out["voices"])
    return out


# ---- text to speech -------------------------------------------------------------------------

def tts_estimate(chars: int, model: str = TTS_MODEL) -> dict[str, Any]:
    usd_1k = TTS_USD_PER_1K.get(model, TTS_USD_PER_1K[TTS_MODEL])
    return {"model": model, "characters": int(chars), "credits": int(chars) * TTS_CREDITS_PER_CHAR,
            "usd": round(chars / 1000 * usd_1k, 4), "usd_per_1k_chars": usd_1k,
            "max_chars_per_request": TTS_MAX_CHARS.get(model, TTS_MAX_CHARS_UNKNOWN),
            "price_source": TTS_PRICE_SOURCE}


def check_voice_id(voice_id: str) -> str:
    v = (voice_id or "").strip()
    if not _VOICE_ID.fullmatch(v):
        raise VPError("data", "this is not a voice id — take one from `vp voice list`")
    return v


def _job_dir(job_dir: Any) -> Path:
    job = Path(job_dir).expanduser().resolve()
    if not job.is_dir():
        raise VPError("data", f"job folder not found: {job}")
    return job


def _out_path(job: Path, out: Any, suffix: str = ".mp3") -> Path:
    p = Path(out).expanduser()
    p = (p if p.is_absolute() else job / p).resolve()
    try:
        p.relative_to(job)
    except ValueError:
        raise VPError("data", f"{p} is outside the job folder: not written") from None
    if p.suffix.lower() != suffix:
        raise VPError("data", f"the output must be a {suffix} file: {p.name}")
    return p


def words_from_alignment(al: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Character timings -> words [{word, start, end}] in seconds (split on whitespace)."""
    if not isinstance(al, dict):
        return []
    chars = al.get("characters") or []
    st = al.get("character_start_times_seconds") or []
    en = al.get("character_end_times_seconds") or []
    if not (len(chars) == len(st) == len(en)):
        raise VPError("contract", "ElevenLabs alignment: lists of different length")
    words: list[dict[str, Any]] = []
    cur = None
    for c, s, e in zip(chars, st, en):
        if str(c).isspace() or c == "":
            cur = None
            continue
        if cur is None:
            cur = {"word": str(c), "start": round(float(s), 3), "end": round(float(e), 3)}
            words.append(cur)
        else:
            cur["word"] += str(c)
            cur["end"] = round(float(e), 3)
    return words


def _write_atomic(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.part")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def _journal(home: Any, job: Path, **fields: Any) -> None:
    try:
        CostJournal(Path(home).expanduser() if home else vp_home(), Job(job).job_id).add(
            "api", provider=SERVICE, estimate=True, **fields)
    except OSError:
        pass   # bookkeeping; the file is already saved


def say(job_dir: Any, voice_id: str, text: str, out: Any, model: str = TTS_MODEL,
        language: str | None = None, previous_text: str | None = None,
        next_text: str | None = None, force: bool = False, *, opener: Any = None,
        home: Any = None, environ: Any = None) -> dict[str, Any]:
    """One voice file (mp3) + `<stem>.alignment.json` next to it. One paid call, never retried."""
    job = _job_dir(job_dir)
    voice_id = check_voice_id(voice_id)
    text = (text or "").strip()
    if not text:
        raise VPError("data", "nothing to voice: the text is empty")
    limit = TTS_MAX_CHARS.get(model, TTS_MAX_CHARS_UNKNOWN)
    if len(text) > limit:
        raise VPError("data", f"the text is {len(text)} characters; {model} takes at most {limit} "
                              f"per file — split it into scenes")
    dst = _out_path(job, out)
    side = dst.with_name(dst.stem + ".alignment.json")
    if dst.exists() and not force:
        raise VPError("data", f"{dst.name} already exists: nothing sent (--force replaces it and "
                              f"pays again)")
    key = keystore.require_key(SERVICE, home, environ)
    body: dict[str, Any] = {"text": text, "model_id": model}
    if language and not model.startswith("eleven_multilingual_v2"):
        body["language_code"] = language
    if previous_text:
        body["previous_text"] = previous_text
    if next_text:
        body["next_text"] = next_text
    url = TTS_URL.format(voice_id=voice_id) + f"?output_format={OUTPUT_FORMAT}"
    resp = webapi.request_json("POST", url, headers={"xi-api-key": key}, body=body, opener=opener,
                               secrets=(key,), service=SERVICE, timeout_s=TTS_TIMEOUT_S,
                               classify=classify)
    if not isinstance(resp, dict) or not resp.get("audio_base64"):
        raise VPError("contract", "ElevenLabs speech: no audio in the answer")
    try:
        audio = base64.b64decode(resp["audio_base64"], validate=False)
    except (ValueError, TypeError):
        raise VPError("contract", "ElevenLabs speech: the audio is not base64") from None
    if not audio:
        raise VPError("contract", "ElevenLabs speech: empty audio")
    al = resp.get("alignment") if isinstance(resp.get("alignment"), dict) else None
    words = words_from_alignment(al)
    duration = max((float(x) for x in (al or {}).get("character_end_times_seconds") or []), default=None)
    cost = tts_estimate(len(text), model)
    _write_atomic(dst, audio)
    write_json(side, {
        "service": SERVICE, "model": model, "voice_id": voice_id, "language": language,
        "created_at": now_iso(), "file": dst.name, "bytes": len(audio), "text": text,
        "characters": len(text), "duration_s": round(duration, 3) if duration is not None else None,
        "credits_estimate": cost["credits"], "cost_estimate_usd": cost["usd"],
        "price_source": TTS_PRICE_SOURCE, "words": words, "alignment": al,
        "normalized_alignment": resp.get("normalized_alignment"),
        "note": "generated with the person's own ElevenLabs account; its terms apply"})
    _journal(home, job, op="tts", model=model, units=len(text), usd=cost["usd"],
             credits=cost["credits"])
    return {"file": str(dst), "alignment_file": str(side), "voice_id": voice_id, "model": model,
            "characters": len(text), "words": len(words),
            "duration_s": round(duration, 3) if duration is not None else None,
            "credits_estimate": cost["credits"], "cost_estimate_usd": cost["usd"]}


def scene_texts(job: Path) -> list[tuple[str, str]]:
    """[(scene id, voice text)] from scenes.json / script.md (vp.text)."""
    from .. import text as textjob
    data = textjob.load_scenes(job)
    if not data:
        raise VPError("data", "no scenes yet: write script.md (scene table) or scenes.json first")
    out = []
    for s in data["scenes"]:
        t = " ".join(c.strip() for c in s["cues"] if c.strip())
        if t:
            out.append((str(s["id"]).upper(), t))
    return out


def scene_file(sid: str) -> str:
    m = re.fullmatch(r"S(\d+)", sid.upper())
    return f"S{int(m.group(1)):02d}.mp3" if m else f"{sid}.mp3"


def scenes(job_dir: Any, voice_id: str, only: list[str] | None = None, model: str = TTS_MODEL,
           language: str | None = None, force: bool = False, *, opener: Any = None,
           home: Any = None, environ: Any = None) -> dict[str, Any]:
    """Voice every scene into voice/SNN.mp3 with its neighbours as context. Scenes that already
    have a file are skipped (unless --force / --only names them). The first failure stops the
    loop (nothing is retried); what is done stays done."""
    job = _job_dir(job_dir)
    check_voice_id(voice_id)
    items = scene_texts(job)
    want = {x.strip().upper() for x in (only or []) if x.strip()}
    unknown = want - {sid for sid, _ in items}
    if unknown:
        raise VPError("data", f"no such scene: {', '.join(sorted(unknown))}")
    done, skipped = [], []
    for i, (sid, txt) in enumerate(items):
        dst = job / "voice" / scene_file(sid)
        if want and sid not in want:
            continue
        if dst.exists() and not (force or want):
            skipped.append(sid)
            continue
        prev_t = items[i - 1][1] if i > 0 else None
        next_t = items[i + 1][1] if i + 1 < len(items) else None
        try:
            r = say(job, voice_id, txt, dst, model, language, prev_t, next_t, force=True,
                    opener=opener, home=home, environ=environ)
        except VPError as e:
            e.details["done"] = [d["scene"] for d in done]
            e.details["failed_scene"] = sid
            raise
        done.append({"scene": sid, **r})
    return {"voiced": [d["scene"] for d in done], "skipped": skipped, "files": done,
            "characters": sum(d["characters"] for d in done),
            "cost_estimate_usd": round(sum(d["cost_estimate_usd"] for d in done), 4)}


# ---- music ----------------------------------------------------------------------------------

def music_estimate(seconds: float) -> dict[str, Any]:
    s = float(seconds)
    return {"model": MUSIC_MODEL, "seconds": s, "usd": round(s / 60 * MUSIC_USD_PER_MIN, 4),
            "credits": int(math.ceil(s / 60 * MUSIC_CREDITS_PER_MIN)),
            "usd_per_min": MUSIC_USD_PER_MIN, "paid_plan_only": True,
            "price_source": MUSIC_PRICE_SOURCE}


def make_music(job_dir: Any, prompt: str, seconds: float, out: Any, model: str = MUSIC_MODEL,
               instrumental: bool = True, force: bool = False, *, opener: Any = None,
               home: Any = None, environ: Any = None) -> dict[str, Any]:
    """One music track (mp3) + `<file>.provenance.json`. One paid call, never retried."""
    job = _job_dir(job_dir)
    prompt = (prompt or "").strip()
    if not prompt:
        raise VPError("data", "the music prompt is empty")
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        raise VPError("data", "--seconds must be a number") from None
    if not (MUSIC_MIN_S <= s <= MUSIC_MAX_S):
        raise VPError("data", f"music length must be {MUSIC_MIN_S}..{MUSIC_MAX_S} s (got {s:g}); "
                              f"a longer video takes a loop or two tracks")
    dst = _out_path(job, out)
    if dst.exists() and not force:
        raise VPError("data", f"{dst.name} already exists: nothing sent (--force replaces it and "
                              f"pays again)")
    key = keystore.require_key(SERVICE, home, environ)
    body = {"prompt": prompt, "music_length_ms": int(round(s * 1000)), "model_id": model,
            "force_instrumental": bool(instrumental)}
    raw = webapi.request_bytes("POST", f"{MUSIC_URL}?output_format={OUTPUT_FORMAT}",
                               headers={"xi-api-key": key}, body=body, opener=opener,
                               secrets=(key,), service=SERVICE, timeout_s=MUSIC_TIMEOUT_S,
                               classify=_classify_music, accept="audio/mpeg")
    if not raw:
        raise VPError("contract", "ElevenLabs music: empty answer")
    if raw[:1] in (b"{", b"["):
        raise VPError("contract", "ElevenLabs music: got JSON instead of audio")
    cost = music_estimate(s)
    _write_atomic(dst, raw)
    side = dst.with_name(dst.name + ".provenance.json")
    write_json(side, {"source": "generated", "service": SERVICE, "model": model, "prompt": prompt,
                      "seconds": s, "instrumental": bool(instrumental), "created_at": now_iso(),
                      "file": dst.name, "bytes": len(raw), "credits_estimate": cost["credits"],
                      "cost_estimate_usd": cost["usd"], "price_source": MUSIC_PRICE_SOURCE,
                      "terms_url": "https://elevenlabs.io/music-terms",
                      "note": "generated with the person's own ElevenLabs account; its music "
                              "terms apply"})
    _journal(home, job, op="music", model=model, units=s, usd=cost["usd"], credits=cost["credits"])
    return {"file": str(dst), "provenance_file": str(side), "model": model, "seconds": s,
            "credits_estimate": cost["credits"], "cost_estimate_usd": cost["usd"]}


# ---- the voice choice form (test signals 2026-11-21 M1) ------------------------------------
# The form once listed names that were never sampled and dropped one that was. Now its buttons
# come from the sample files themselves: label = file name, voice_id = the take's sidecar.

FORM_MAX_OPTIONS = 4        # AskUserQuestion holds at most 4 options
SAMPLE_ROUND = 3            # voices per round: 3 buttons + «other voices»


def samples(job_dir: Any, last: int = SAMPLE_ROUND) -> dict[str, Any]:
    """The newest `last` voice samples of a job (voice/samples/*.mp3, in the order they were
    made) as the options of the choice form: {label (the file name without .mp3), file,
    voice_id (from <stem>.alignment.json)}. `other_option` = the form has room for «other
    voices»; `earlier` = samples of earlier rounds (not on this form)."""
    job = _job_dir(job_dir)
    if not 1 <= last <= FORM_MAX_OPTIONS:
        raise VPError("data", f"--last must be 1..{FORM_MAX_OPTIONS} (a form holds "
                              f"{FORM_MAX_OPTIONS} options)")
    d = job / "voice" / "samples"
    files = sorted((p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".mp3"
                    and not p.name.startswith(".")),
                   key=lambda p: (p.stat().st_mtime, p.name)) if d.is_dir() else []
    if not files:
        raise VPError("data", f"no voice samples in {d}: make them with `vp voice say` first")
    pick = files[-last:]
    options = []
    for p in pick:
        side = p.with_name(p.stem + ".alignment.json")
        try:
            meta = json.loads(side.read_text(encoding="utf-8")) if side.is_file() else {}
        except ValueError:
            meta = {}
        options.append({"label": p.stem, "file": str(p),
                        "voice_id": meta.get("voice_id") if isinstance(meta, dict) else None})
    return {"options": options, "count": len(options),
            "other_option": len(options) < FORM_MAX_OPTIONS,
            "earlier": len(files) - len(pick)}


# ---- CLI ------------------------------------------------------------------------------------

def _read_text_arg(a: argparse.Namespace, file_attr: str, text_attr: str) -> str | None:
    f, t = getattr(a, file_attr, None), getattr(a, text_attr, None)
    if f:
        p = Path(f).expanduser()
        if not p.is_file():
            raise VPError("data", f"file not found: {p}")
        return p.read_text(encoding="utf-8")
    return t


def _coded(fn) -> int:
    try:
        return fn()
    except (keystore.NeedKey, NeedPlan, NeedCredits) as e:
        note_failure(e, e.code.split(":", 1)[0].lower())
        print(e.code + " — " + e.msg.split(" — ", 1)[-1])
        return 2


def _emit(res: dict[str, Any], as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        for ln in lines:
            print(ln)


def voice_main(argv: list[str] | None = None, *, opener: Any = None) -> int:
    ap = argparse.ArgumentParser(prog="vp voice", description="voice-over with your ElevenLabs key")
    sub = ap.add_subparsers(dest="cmd", required=True)
    li = sub.add_parser("list", help="the account's voices (free)")
    li.add_argument("--lang", help="ISO 639-1, e.g. ru: voices labelled with it first")
    li.add_argument("--limit", type=int, default=100)
    li.add_argument("--json", action="store_true")
    es = sub.add_parser("estimate", help="credits and USD before generating (no network)")
    g = es.add_mutually_exclusive_group(required=True)
    g.add_argument("--text-file")
    g.add_argument("--text")
    g.add_argument("--chars", type=int)
    g.add_argument("--job", help="all scenes of a text job")
    es.add_argument("--model", default=TTS_MODEL)
    es.add_argument("--json", action="store_true")
    sa = sub.add_parser("say", help="one voice file + its timings")
    sa.add_argument("job")
    sa.add_argument("--voice", required=True, help="a voice_id from `vp voice list`")
    g = sa.add_mutually_exclusive_group(required=True)
    g.add_argument("--text-file")
    g.add_argument("--text")
    sa.add_argument("--out", required=True, help="<job>/voice/<name>.mp3")
    sa.add_argument("--model", default=TTS_MODEL)
    sa.add_argument("--language")
    sa.add_argument("--previous-text")
    sa.add_argument("--next-text")
    sa.add_argument("--force", action="store_true", help="replace an existing file, pay again")
    sa.add_argument("--json", action="store_true")
    sc = sub.add_parser("scenes", help="every scene of a text job -> voice/S01.mp3 …")
    sc.add_argument("job")
    sc.add_argument("--voice", required=True)
    sc.add_argument("--only", help="comma list of scenes to (re)voice, e.g. S3,S5")
    sc.add_argument("--model", default=TTS_MODEL)
    sc.add_argument("--language")
    sc.add_argument("--force", action="store_true", help="re-voice scenes that have a file")
    sc.add_argument("--json", action="store_true")
    sm = sub.add_parser("samples", help="the choice form: exactly the sampled voices (free)")
    sm.add_argument("job")
    sm.add_argument("--last", type=int, default=SAMPLE_ROUND,
                    help="how many of the newest samples go on the form (default 3)")
    sm.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    def run() -> int:
        if a.cmd == "samples":
            res = samples(a.job, a.last)
            _emit(res, a.json, [f"{o['label']}\t{o['voice_id'] or '-'}\t{o['file']}"
                                for o in res["options"]]
                  + (["+ the «other voices» option"] if res["other_option"] else []))
            return 0
        if a.cmd == "list":
            res = list_voices(a.lang, a.limit, opener=opener)
            lines = [f"{v['voice_id']}  {v['name']} — " + ", ".join(
                x for x in (v["gender"], v["age"], v["accent"], v["language"]) if x)
                for v in res["voices"]]
            _emit(res, a.json, lines + ([res["note"]] if res.get("note") else []))
            return 0
        if a.cmd == "estimate":
            if a.job:
                chars = sum(len(t) for _, t in scene_texts(_job_dir(a.job)))
            elif a.chars is not None:
                chars = a.chars
            else:
                chars = len((_read_text_arg(a, "text_file", "text") or "").strip())
            res = tts_estimate(chars, a.model)
            _emit(res, a.json, [f"{res['characters']} characters ≈ {res['credits']} credits ≈ "
                                f"${res['usd']:.3f} ({a.model}, ${res['usd_per_1k_chars']}/1K)"])
            return 0
        if a.cmd == "say":
            res = say(a.job, a.voice, _read_text_arg(a, "text_file", "text") or "", a.out, a.model,
                      a.language, a.previous_text, a.next_text, a.force, opener=opener)
            _emit(res, a.json, [f"saved: {res['file']} ({res['duration_s']} s, {res['words']} words)",
                                f"≈ {res['credits_estimate']} credits ≈ ${res['cost_estimate_usd']:.3f}"])
            return 0
        only = [x for x in (a.only or "").split(",") if x.strip()]
        from .. import runsignal
        with runsignal.step(a.job, "voice"):           # run signal: the text route's voice stage
            res = scenes(a.job, a.voice, only, a.model, a.language, a.force, opener=opener)
        _emit(res, a.json, [f"voiced: {', '.join(res['voiced']) or '—'}"
                            + (f"; already there: {', '.join(res['skipped'])}" if res["skipped"] else ""),
                            f"≈ {res['characters']} credits ≈ ${res['cost_estimate_usd']:.3f}"])
        return 0
    return _coded(run)


def music_main(argv: list[str] | None = None, *, opener: Any = None) -> int:
    ap = argparse.ArgumentParser(prog="vp music", description="music with your ElevenLabs key "
                                                              "(paid ElevenLabs plans)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    es = sub.add_parser("estimate", help="credits and USD before generating (no network)")
    es.add_argument("--seconds", type=float, required=True)
    es.add_argument("--json", action="store_true")
    mk = sub.add_parser("make", help="one music track")
    mk.add_argument("job")
    g = mk.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt-file")
    g.add_argument("--prompt")
    mk.add_argument("--seconds", type=float, required=True)
    mk.add_argument("--out", required=True, help="<job>/music/<name>.mp3")
    mk.add_argument("--model", default=MUSIC_MODEL)
    mk.add_argument("--vocals", action="store_true", help="allow singing (default: instrumental)")
    mk.add_argument("--force", action="store_true", help="replace an existing file, pay again")
    mk.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    def run() -> int:
        if a.cmd == "estimate":
            res = music_estimate(a.seconds)
            _emit(res, a.json, [f"{a.seconds:g} s of music ≈ {res['credits']} credits ≈ "
                                f"${res['usd']:.2f} (paid ElevenLabs plans only)"])
            return 0
        from .. import runsignal
        with runsignal.step(a.job, "music"):           # run signal: the text route's music stage
            res = make_music(a.job, _read_text_arg(a, "prompt_file", "prompt") or "", a.seconds,
                             a.out, a.model, not a.vocals, a.force, opener=opener)
        _emit(res, a.json, [f"saved: {res['file']} ({res['seconds']:g} s)",
                            f"≈ {res['credits_estimate']} credits ≈ ${res['cost_estimate_usd']:.3f}"])
        return 0
    return _coded(run)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(voice_main())
