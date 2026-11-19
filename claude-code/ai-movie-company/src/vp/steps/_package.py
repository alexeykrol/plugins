"""Distribution helpers shared by S8, S10, S9, PUB and S11 (SPEC §5, S8–S11). Pure code, no model.

  uploads.yaml        load_uploads / save_entry (read-modify-write of one item entry, atomic)
  series facts        fragment_info, series_items, playlist_title, item_meta, final_snippet
  links block         LINKS_START/LINKS_END, extract_links_block, replace_links_block,
                      strip_links_block, remove_link_lines, video_url, link_video_id
  approval package    build_package, package_hash, read_package, write_package (stable bytes)
  derived package     build_derived, verify_derived (only `remove_links` is allowed)
  approval.yaml       load_approval, append_approval, current_decision
  platform check      platform_mismatches (what is on the platform now == package)
  chapters            parse_chapters (description lines "MM:SS Title")
  png                 png_size (IHDR) for the thumbnail check

A model never sees anything from here: video ids and URLs are built by code only.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from ..core.errors import VPError
from ..core.io import (MISSING, dump_yaml, now_iso, read_yaml, sha256_file, sha256_obj,
                       atomic_write, write_yaml)
from ..core.paths import FULL, Job
from ..core.srt import parse_srt
from ..core.timefmt import parse_time

LINKS_START = "⟦vp-links⟧"
LINKS_END = "⟦/vp-links⟧"
_BLOCK_RE = re.compile(re.escape(LINKS_START) + r"(.*?)" + re.escape(LINKS_END), re.S)
_URL_RE = re.compile(r"(?:https://youtu\.be/|fake://video/)([A-Za-z0-9_-]+)")
_CHAPTER_RE = re.compile(r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s+(\S.*)$")

UPLOAD_STATES = ("intent", "uploading", "uncertain", "uploaded")
DECISIONS = ("approve", "reject", "revoked")
DEFAULT_CATEGORY = "27"  # Education
DEFAULT_LANGUAGE = "ru"


# ---- uploads.yaml -------------------------------------------------------------------------

def load_uploads(job: Job) -> dict[str, Any]:
    """uploads.yaml as {"items": {item: entry}} (empty structure if absent)."""
    data = read_yaml(job.uploads_yaml, default=None)
    if not isinstance(data, dict):
        data = {}
    items = data.get("items")
    data["items"] = items if isinstance(items, dict) else {}
    return data


def get_entry(job: Job, item: str) -> dict[str, Any] | None:
    e = load_uploads(job)["items"].get(item)
    return dict(e) if isinstance(e, dict) else None


def save_entry(job: Job, item: str, entry: dict[str, Any], state_note: str | None = None) -> None:
    """Write one item entry (re-reading the file first). Appends a history line when the state
    changed. Every S8 transition goes through here BEFORE the next action (SPEC S8)."""
    data = load_uploads(job)
    old = data["items"].get(item) or {}
    if entry.get("state") not in UPLOAD_STATES:
        raise VPError("internal", f"uploads.yaml: bad state {entry.get('state')!r}")
    hist = list(old.get("history") or entry.get("history") or [])  # the file is authoritative
    if old.get("state") != entry["state"] or state_note:
        rec = {"state": entry["state"], "at": now_iso()}
        if state_note:
            rec["note"] = state_note
        hist.append(rec)
    entry["history"] = hist  # the caller's dict stays in sync with the file
    data["items"][item] = dict(entry)
    data.setdefault("version", 1)
    write_yaml(job.uploads_yaml, {"version": data["version"], "items": data["items"]})


def uploaded_ids(job: Job) -> dict[str, str]:
    """item -> video_id for every uploaded item."""
    out = {}
    for item, e in load_uploads(job)["items"].items():
        if isinstance(e, dict) and e.get("state") == "uploaded" and e.get("video_id"):
            out[item] = str(e["video_id"])
    return out


# ---- series facts -------------------------------------------------------------------------

def _ms(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value) * 1000))
    try:
        return parse_time(str(value))
    except ValueError:
        return None


def fragment_info(job: Job) -> dict[str, dict[str, Any]]:
    """item -> {id, kind, start_ms, end_ms, title, thesis, knowledge_version, order} for every item
    (fragments in start order, then "full"). Tolerant of the field spellings S4 accepts; no ffprobe."""
    data = read_yaml(job.fragments_yaml, default={}) or {}
    frags = []
    for f in (data.get("fragments") if isinstance(data, dict) else None) or []:
        if "start_ms" in f:
            start, end = f.get("start_ms"), f.get("end_ms")
            start = int(start) if start is not None else None
            end = int(end) if end is not None else None
        elif "in_ms" in f:
            start, end = int(f["in_ms"]), int(f.get("out_ms") or 0)
        else:
            start, end = _ms(f.get("start")), _ms(f.get("end"))
        frags.append({"id": str(f["id"]), "kind": f.get("type") or f.get("kind") or "reel",
                      "start_ms": start if start is not None else 0, "end_ms": end,
                      "title": str(f.get("title") or f.get("name") or ""),
                      "thesis": str(f.get("thesis") or ""),
                      "knowledge_version": f.get("knowledge_version")})
    frags.sort(key=lambda r: (r["start_ms"], r["id"]))
    out: dict[str, dict[str, Any]] = {}
    for n, r in enumerate(frags, 1):
        r["order"] = n
        out[r["id"]] = r
    req = job.request()
    out[FULL] = {"id": FULL, "kind": "full", "start_ms": 0, "end_ms": None,
                 "title": str(req.get("title") or ""), "thesis": str(req.get("thesis") or ""),
                 "knowledge_version": None, "order": 0}
    return out


def series_items(job: Job) -> list[str]:
    """Items in series order: fragments by start time, then "full"."""
    info = fragment_info(job)
    items = job.items() or []
    return sorted(items, key=lambda i: (i == FULL, (info.get(i) or {}).get("order", 0), i))


def playlist_title(job: Job) -> str:
    """Series playlist title: request `playlist` | `series.title` | `title`, else the job id."""
    req = job.request()
    series = req.get("series") if isinstance(req.get("series"), dict) else {}
    for v in (req.get("playlist"), series.get("title"), req.get("title")):
        if isinstance(v, str) and v.strip():
            return v.strip()[:150]
    return f"vp {job.job_id}"[:150]


def item_meta(job: Job, item: str) -> dict[str, Any]:
    """meta.yaml (S6b) normalised to {title, description, tags, categoryId, defaultLanguage,
    chapters?, hashtags?}. Accepts `title` or `titles` (+ `chosen` index)."""
    meta = read_yaml(job.meta_yaml(item), default=None)
    if not isinstance(meta, dict):
        raise VPError("data", f"{item}: meta.yaml missing or not a mapping")
    title = meta.get("title")
    if not title and isinstance(meta.get("titles"), list) and meta["titles"]:
        idx = int(meta.get("chosen") or 0)
        t = meta["titles"][idx]
        title = t.get("title") if isinstance(t, dict) else t
    if not isinstance(title, str) or not title.strip():
        raise VPError("data", f"{item}: meta.yaml has no title")
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        raise VPError("data", f"{item}: meta.yaml tags must be a list")
    out = {"title": title, "description": str(meta.get("description") or ""),
           "tags": [str(t) for t in tags],
           "categoryId": str(meta.get("categoryId") or meta.get("category_id") or ""),
           "defaultLanguage": str(meta.get("defaultLanguage") or meta.get("language") or "")}
    if isinstance(meta.get("chapters"), list):
        out["chapters"] = meta["chapters"]
    if isinstance(meta.get("hashtags"), list):
        out["hashtags"] = meta["hashtags"]
    return out


def final_snippet(meta: dict[str, Any], tag: str, category: str, language: str) -> dict[str, Any]:
    """Snippet S8 sends: meta texts, meta tags + the vp tag (last, never dropped)."""
    tags = [t for t in meta["tags"] if not t.startswith("vp-")] + [tag]
    return {"title": meta["title"], "description": meta["description"], "tags": tags,
            "categoryId": meta.get("categoryId") or category,
            "defaultLanguage": meta.get("defaultLanguage") or language}


# ---- links block --------------------------------------------------------------------------

def video_url(video_id: str, t_ms: int | None = None) -> str:
    """https://youtu.be/<id> (+ ?t=<seconds> for a place in the video)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(video_id)):
        raise VPError("internal", f"bad video id {video_id!r}")
    url = f"https://youtu.be/{video_id}"
    if t_ms:
        url += f"?t={int(t_ms) // 1000}"
    return url


def platform_video_url(publisher: Any, video_id: str, t_ms: int | None = None) -> str:
    """Link of a video on the publisher's platform: youtu.be for YouTube, fake://video/<id> in
    a rehearsal (U10). Objects without `video_url` get the YouTube form."""
    video_url(video_id)  # validates the id
    fn = getattr(publisher, "video_url", None)
    return str(fn(video_id, t_ms)) if callable(fn) else video_url(video_id, t_ms)


def link_video_id(line: str) -> str | None:
    m = _URL_RE.search(line)
    return m.group(1) if m else None


def extract_links_block(description: str) -> list[str] | None:
    """Non-empty lines inside the block, or None if there is no block."""
    m = _BLOCK_RE.search(description or "")
    if not m:
        return None
    return [ln for ln in m.group(1).split("\n") if ln.strip()]


def strip_links_block(description: str) -> str:
    """Description without the block and the blank lines before it."""
    return re.sub(r"\s*" + _BLOCK_RE.pattern, "", description or "", flags=re.S)


def render_block(lines: Iterable[str]) -> str:
    return LINKS_START + "\n" + "".join(ln + "\n" for ln in lines) + LINKS_END


def replace_links_block(description: str, lines: list[str]) -> str:
    """Put `lines` between the markers. An existing block is replaced in place; the rest of the
    text is untouched. Without a block, it is appended after a blank line."""
    description = description or ""
    block = render_block(lines)
    if _BLOCK_RE.search(description):
        return _BLOCK_RE.sub(lambda _m: block, description, count=1)
    if not description.strip():
        return block
    return description.rstrip("\n") + "\n\n" + block


def remove_link_lines(description: str, remove: list[str]) -> str:
    """Delete exactly the given lines from inside the block; nothing else changes."""
    m = _BLOCK_RE.search(description or "")
    if not m:
        if remove:
            raise VPError("data", "no links block to remove lines from")
        return description
    body = m.group(1)
    parts = body.split("\n")
    todo = list(remove)
    kept = []
    for p in parts:
        if p in todo:
            todo.remove(p)
            continue
        kept.append(p)
    if todo:
        raise VPError("data", f"lines not found in links block: {len(todo)}")
    new_body = "\n".join(kept)
    return description[:m.start(1)] + new_body + description[m.end(1):]


# ---- chapters / png -------------------------------------------------------------------------

def parse_chapters(description: str) -> list[dict[str, Any]]:
    """Chapter lines 'MM:SS Title' / 'H:MM:SS Title' outside the links block."""
    out = []
    for line in strip_links_block(description).split("\n"):
        m = _CHAPTER_RE.match(line)
        if m:
            try:
                out.append({"start_ms": parse_time(m.group(1)), "title": m.group(2).strip()})
            except ValueError:
                continue
    return out


def png_size(path: str | Path) -> tuple[int, int] | None:
    """(width, height) from a PNG header, None if not a PNG."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


# ---- approval package -----------------------------------------------------------------------

_HASH_EXCLUDE = ("package_hash", "derived_hash")


def package_hash(pkg: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of the package without its own hash fields."""
    return sha256_obj({k: v for k, v in pkg.items() if k not in _HASH_EXCLUDE})


def build_package(job: Job, item: str, *, video_id: str, channel: str, playlist: dict[str, Any],
                  metadata: dict[str, Any], kind: str) -> dict[str, Any]:
    """package/<item>.yaml content: file hashes + final metadata + channel + playlist + hash.
    The schedule is NOT part of the package (SPEC S9)."""
    files = {"render_mp4": sha256_file(job.render_mp4(item)),
             "final_srt": sha256_file(job.final_srt(item)),
             "thumb_png": sha256_file(job.thumb_png(item))}
    for k, v in files.items():
        if v == MISSING:
            raise VPError("data", f"{item}: {k} missing, cannot build the package")
    pkg = {"version": 1, "item": item, "kind": kind, "video_id": video_id, "channel": channel,
           "playlist": {"id": playlist.get("id"), "title": playlist.get("title")},
           "files": files,
           "metadata": {k: metadata[k] for k in ("title", "description", "tags", "categoryId",
                                                   "defaultLanguage")}}
    pkg["package_hash"] = package_hash(pkg)
    return pkg


def read_package(path: Path) -> dict[str, Any] | None:
    data = read_yaml(path, default=None)
    return data if isinstance(data, dict) else None


def write_if_changed(path: Path, obj: Any) -> bool:
    """Write YAML only if the bytes differ (keeps downstream hashes stable). True if written."""
    text = dump_yaml(obj)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    atomic_write(path, text)
    return True


# ---- derived package ------------------------------------------------------------------------

def _derived_body(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in ("derived_hash", "created_at")}


def build_derived(parent: dict[str, Any], removed_lines: list[str]) -> dict[str, Any]:
    """Derived package after approval: the parent with exactly `removed_lines` deleted from the
    links block. Everything else is copied from the parent."""
    desc = remove_link_lines(parent["metadata"]["description"], removed_lines)
    meta = dict(parent["metadata"])
    meta["description"] = desc
    d = {"version": 1, "item": parent["item"], "parent_hash": parent["package_hash"],
         "transform": "remove_links", "removed": list(removed_lines),
         "video_id": parent["video_id"], "channel": parent["channel"],
         "playlist": parent["playlist"], "files": parent["files"], "metadata": meta}
    d["derived_hash"] = sha256_obj(_derived_body(d))
    d["created_at"] = now_iso()
    return d


def verify_derived(parent: dict[str, Any], derived: dict[str, Any]) -> list[str]:
    """Codes of everything that makes the derived package invalid (empty = valid)."""
    out = []
    if derived.get("parent_hash") != parent.get("package_hash"):
        out.append("derived_parent_mismatch")
    if package_hash(parent) != parent.get("package_hash"):
        out.append("parent_hash_invalid")
    if derived.get("transform") != "remove_links":
        out.append("derived_transform_not_allowed")
    if derived.get("derived_hash") != sha256_obj(_derived_body(derived)):
        out.append("derived_hash_invalid")
    for k in ("item", "video_id", "channel", "playlist", "files"):
        if derived.get(k) != parent.get(k):
            out.append(f"derived_{k}_changed")
    pm, dm = parent.get("metadata") or {}, derived.get("metadata") or {}
    for k in ("title", "tags", "categoryId", "defaultLanguage"):
        if pm.get(k) != dm.get(k):
            out.append(f"derived_{k}_changed")
    removed = derived.get("removed")
    if not isinstance(removed, list) or not removed:
        out.append("derived_nothing_removed")
    else:
        block = extract_links_block(pm.get("description", "")) or []
        if any(r not in block for r in removed):
            out.append("derived_removed_not_a_link")
        try:
            expect = remove_link_lines(pm.get("description", ""), removed)
        except VPError:
            expect = None
        if expect is None or expect != dm.get("description"):
            out.append("derived_description_changed")
    return out


# ---- approval.yaml ------------------------------------------------------------------------

def load_approval(job: Job) -> list[dict[str, Any]]:
    data = read_yaml(job.approval_yaml, default=None)
    entries = data.get("entries") if isinstance(data, dict) else None
    return [e for e in entries or [] if isinstance(e, dict)]


def append_approval(job: Job, entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    old = load_approval(job)
    write_yaml(job.approval_yaml, {"version": 1, "entries": old + list(entries)})


def current_decision(entries: list[dict[str, Any]], item: str,
                     package_hash_: str) -> dict[str, Any] | None:
    """The latest entry for `item` if it is an approve/reject of THIS package hash; a later
    revocation (or a decision on another hash) means no valid decision."""
    mine = [e for e in entries if e.get("item") == item]
    if not mine:
        return None
    last = mine[-1]
    if last.get("decision") in ("approve", "reject") and last.get("package_hash") == package_hash_:
        return last
    return None


def last_revoked_at(entries: list[dict[str, Any]], item: str) -> str | None:
    rev = [e for e in entries if e.get("item") == item and e.get("decision") == "revoked"]
    return str(rev[-1].get("decided_at")) if rev else None


def validate_publish(value: Any) -> Any:
    """publish: "now" | {at: iso} | {event: str} | {cron: str}."""
    if value == "now":
        return value
    if isinstance(value, dict) and len(value) == 1:
        (k, v), = value.items()
        if k in ("at", "event", "cron") and isinstance(v, str) and v.strip():
            return {k: v}
    raise VPError("data", f"bad publish choice {value!r}")


# ---- platform == package --------------------------------------------------------------------

def captions_verified(entry: dict[str, Any] | None, srt_path: Path, video: dict[str, Any] | None,
                      check_track: Any) -> str | None:
    """Captions on the platform == final.srt, WITHOUT downloading them (quota: a download is
    250 units). None = verified; else a violation code.

    The guarantee: our own upload recorded the sha of the file it sent (uploads.yaml
    captions.sha); it must equal the current final.srt (whose hash is in the approved package);
    and the platform must still have the track — videos.list contentDetails.caption (`has_captions`,
    free with the get_video we do anyway), or, when that is not true, ONE captions.list
    (`check_track()` -> bool, 50 units). An edit of the track by hand in Studio is not detected.
    """
    cap = (entry or {}).get("captions") or {}
    if not cap.get("id") or not cap.get("sha"):
        return "captions_missing"
    if cap.get("sha") != sha256_file(srt_path):
        return "captions_mismatch"
    if video is not None and video.get("has_captions") is True:
        return None
    return None if check_track() else "captions_missing"


def captions_equal(platform: bytes | None, local_path: Path) -> bool:
    """Compare captions by cues (start, end, text): the platform may re-format the SRT."""
    if platform is None or not local_path.exists():
        return False
    try:
        a = parse_srt(platform.decode("utf-8-sig"), strip_speaker=False)
        b = parse_srt(local_path.read_text(encoding="utf-8-sig"), strip_speaker=False)
    except Exception:
        return False
    norm = lambda cues: [(c.start_ms, c.end_ms, " ".join(c.text.split())) for c in cues]
    return norm(a) == norm(b)


def platform_mismatches(job: Job, target: dict[str, Any], video: dict[str, Any] | None,
                        captions: bytes | bool | None, entry: dict[str, Any] | None,
                        channel: str) -> list[str]:
    """What differs between the platform/local state and a (derived) package. Empty = equal.

    `captions`: True/False = already verified (captions_verified), or the downloaded SRT bytes
    (None = no track). Compared: local file hashes (render, final.srt, thumbnail); the video id and the vp key of
    the recorded upload; snippet fields on the platform; captions on the platform vs final.srt;
    the thumbnail recorded as set; the channel; the playlist.
    """
    out = []
    item = target["item"]
    files = target.get("files") or {}
    if sha256_file(job.render_mp4(item)) != files.get("render_mp4"):
        out.append("local_render_changed")
    if sha256_file(job.final_srt(item)) != files.get("final_srt"):
        out.append("local_srt_changed")
    if sha256_file(job.thumb_png(item)) != files.get("thumb_png"):
        out.append("local_thumbnail_changed")
    if channel != target.get("channel"):
        out.append("channel_mismatch")
    if not entry or entry.get("video_id") != target.get("video_id") or entry.get("state") != "uploaded":
        out.append("upload_record_mismatch")
    else:
        if entry.get("render_sha") != files.get("render_mp4"):
            out.append("uploaded_video_mismatch")
        if (entry.get("thumbnail") or {}).get("sha") != files.get("thumb_png"):
            out.append("thumbnail_mismatch")
        if (entry.get("playlist") or {}).get("id") != (target.get("playlist") or {}).get("id"):
            out.append("playlist_mismatch")
    if video is None:
        out.append("video_missing")
    else:
        sn = video.get("snippet") or {}
        meta = target.get("metadata") or {}
        for k in ("title", "description", "tags", "categoryId", "defaultLanguage"):
            a, b = sn.get(k), meta.get(k)
            if k == "tags":
                a, b = list(a or []), list(b or [])
            if a != b:
                out.append(f"platform_{k}_mismatch")
    if isinstance(captions, bool):
        if not captions:
            out.append("captions_mismatch")
    elif not captions_equal(captions, job.final_srt(item)):
        out.append("captions_mismatch")
    return out
