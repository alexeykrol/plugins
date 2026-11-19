"""S8 — upload as private drafts, idempotent (SPEC S8). Code. One instance per item.

uploads.yaml entry per item (state machine; every transition is written atomically BEFORE the
next action):
  intent     decided to upload, no session yet            -> start_upload
  uploading  session_uri saved                             -> upload_status:
                complete   -> uploaded (video_id from the session)
                incomplete -> resume_upload (UploadOutcomeUnknown -> ask the session again)
                invalid    -> find_by_key (with pauses for processing): found -> uploaded,
                              not found -> uncertain + ONE notification
  uncertain  blocks any new upload of this item; only `resolve-upload` lifts it
  uploaded   video_id known -> captions (final.srt), thumbnail, series playlist, metadata

vp_key = make_vp_key(job_id, item, sha256(render.mp4)); tag vp-<12> in the tags. Privacy is
private ALWAYS. Every later snippet change is a read-modify-write (Publisher.update_snippet).
Then acceptance: the platform reports private (unless already published by PUB) and processed;
the vp tag is present.

Output: none declared — uploads.yaml is a job-level journal shared by all item instances and by
PUB (publish times, SPEC §11); S8 itself verifies it. Downstream steps hash the files that
determine the upload (render, srt, thumbnail, meta) instead.

Config keys (all optional): youtube.category_id ("27"), youtube.language ("ru"),
youtube.channel_id (expected channel; mismatch = auth error), upload.find_attempts (3),
upload.find_pause_s (30), upload.processing_polls (40), upload.processing_pause_s (15).

Also: resolve_upload(ctx, item, decision=None) — CLI `resolve-upload <job> <item>`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..adapters.publisher import PublisherError, UploadOutcomeUnknown, make_vp_key, vp_tag
from ..core.io import MISSING, now_iso, sha256_file, sha256_obj
from ..core.status import Status
from ._package import (DEFAULT_CATEGORY, DEFAULT_LANGUAGE, final_snippet, fragment_info,
                       get_entry, item_meta, load_uploads, platform_video_url, playlist_title,
                       save_entry)
from . import _quota
from .base import Ctx, StepSpec, VPError

PRIVACY = "private"


def _upload_cfg(ctx: Ctx) -> dict[str, Any]:
    u = ctx.config.get("upload") or {}
    return {"find_attempts": int(u.get("find_attempts", 3)),
            "find_pause_s": float(u.get("find_pause_s", 30)),
            "processing_polls": int(u.get("processing_polls", 40)),
            "processing_pause_s": float(u.get("processing_pause_s", 15))}


def _yt_cfg(ctx: Ctx) -> dict[str, Any]:
    y = ctx.config.get("youtube") or {}
    return {"category_id": str(y.get("category_id") or DEFAULT_CATEGORY),
            "language": str(y.get("language") or DEFAULT_LANGUAGE),
            "channel_id": y.get("channel_id") if ctx.config.get("publisher", "youtube") == "youtube" else None}


def search_key(ctx: Ctx, vp_key: str) -> str | None:
    """find_by_key with pauses: a fresh upload may not be listed yet (SPEC S8)."""
    cfg = _upload_cfg(ctx)
    for attempt in range(max(1, cfg["find_attempts"])):
        if attempt:
            ctx.sleep(cfg["find_pause_s"])
        vid = ctx.retry(lambda: ctx.publisher.find_by_key(vp_key))
        if vid:
            return str(vid)
    return None


def _check_channel(ctx: Ctx) -> str:
    """The channel the credentials act on; must equal youtube.channel_id when configured."""
    cache = ctx._shared.setdefault("s8_channel", {})
    if "id" not in cache:
        cache["id"] = str(ctx.retry(ctx.publisher.channel_id))
    expected = _yt_cfg(ctx)["channel_id"]
    if expected and cache["id"] != expected:
        raise VPError("auth", "credentials act on another channel than youtube.channel_id")
    return cache["id"]


def _mark_uncertain(ctx: Ctx, item: str, entry: dict[str, Any], why: str) -> None:
    entry["state"] = "uncertain"
    entry["uncertain_reason"] = why
    first = not entry.get("uncertain_notified")
    entry["uncertain_notified"] = True
    save_entry(ctx.job, item, entry, state_note=why)
    if first:
        try:
            ctx.notifier.send(f"vp: {ctx.job.job_id} {item}: upload outcome uncertain ({why}). "
                              f"New uploads of this item are blocked. Check the channel, then run "
                              f"`python -m vp resolve-upload <job> {item}`.")
        except Exception as e:  # a failed notification must not hide the state
            ctx.log.event("notify_failed", step="S8", item=item, error=type(e).__name__)


def _drive_upload(ctx: Ctx, item: str, entry: dict[str, Any], render: Path,
                  snippet: dict[str, Any]) -> dict[str, Any]:
    """Run the state machine until `uploaded` (or raise)."""
    pub = ctx.publisher
    unknown_seen = 0
    for _ in range(12):
        state = entry["state"]
        if state == "uploaded":
            return entry
        if state == "uncertain":
            raise VPError("data", f"{item}: upload outcome uncertain; new upload blocked — run "
                                  f"`resolve-upload` after checking the channel")
        if state == "intent":
            uri = ctx.retry(lambda: pub.start_upload(render, snippet, privacy=PRIVACY))
            entry["state"] = "uploading"
            entry["session_uri"] = uri
            save_entry(ctx.job, item, entry)
            continue
        if state == "uploading":
            uri = entry.get("session_uri")
            st = ctx.retry(lambda: pub.upload_status(uri)) if uri else {"state": "invalid"}
            s = st.get("state")
            if s == "complete" and st.get("video_id"):
                _set_uploaded(ctx, item, entry, str(st["video_id"]), "session complete")
                continue
            if s == "incomplete":
                if unknown_seen >= 2:
                    raise VPError("transport", f"{item}: upload session still incomplete after "
                                               f"an unknown outcome; will re-check next run")
                try:
                    vid = ctx.retry(lambda: pub.resume_upload(uri, render))
                except UploadOutcomeUnknown:
                    unknown_seen += 1
                    ctx.log.event("upload_outcome_unknown", step="S8", item=item)
                    continue  # ask the session (state stays `uploading`)
                if vid:
                    _set_uploaded(ctx, item, entry, str(vid), "upload finished")
                continue
            # invalid session: the server may still hold the video -> search by key
            vid = search_key(ctx, entry["vp_key"])
            if vid:
                _set_uploaded(ctx, item, entry, vid, "found by vp key")
                continue
            _mark_uncertain(ctx, item, entry, "session invalid and vp key not found")
            continue
        raise VPError("internal", f"{item}: bad upload state {state!r}")
    raise VPError("internal", f"{item}: upload state machine did not settle")


def _set_uploaded(ctx: Ctx, item: str, entry: dict[str, Any], video_id: str, note: str) -> None:
    entry["state"] = "uploaded"
    entry["video_id"] = video_id
    entry["url"] = platform_video_url(ctx.publisher, video_id)
    entry["privacy"] = PRIVACY
    entry["uploaded_at"] = now_iso()
    save_entry(ctx.job, item, entry, state_note=note)


def _needs_new_upload(entry: dict[str, Any] | None, render_sha: str) -> bool:
    if not entry or entry.get("state") in (None, "intent"):
        return True
    return entry.get("state") == "uploaded" and entry.get("render_sha") != render_sha \
        and not entry.get("published")


def quota_precheck(ctx: Ctx) -> dict[str, int] | None:
    """Once per run, before the first new upload: do this job's remaining new uploads fit into
    today's YouTube quota? Raises WaitingQuota otherwise (nothing uploaded). Real YouTube only."""
    cache = ctx._shared.setdefault("s8_quota", {})
    if "info" in cache:
        return cache["info"]
    if ctx.publisher_kind != "youtube":
        cache["info"] = None
        return None
    job = ctx.job
    uploads = load_uploads(job)["items"]
    new = sum(1 for it in job.items() or []
              if _needs_new_upload(uploads.get(it), sha256_file(job.render_mp4(it))))
    daily = int((ctx.config.get("budget") or {}).get("youtube_quota_per_day", 10000))
    info = _quota.check(new, daily, ctx.costs)
    ctx.log.event("quota_precheck", **info)
    cache["info"] = info
    return info


def _set_captions(ctx: Ctx, item: str, entry: dict[str, Any], vid: str, srt: Path,
                  language: str) -> str:
    """Upload captions with the fewest units: a known track -> update (450); a first upload ->
    insert without listing (400; a `pending` mark written first, so after a crash the next try
    lists the tracks and never creates a duplicate)."""
    pub = ctx.publisher
    cap = entry.get("captions") or {}
    if cap.get("id"):
        try:
            return ctx.retry(lambda: pub.set_captions(vid, srt, language=language,
                                                      track_id=cap["id"]))
        except PublisherError as e:
            if e.kind != "data":
                raise
            ctx.log.event("caption_track_gone", step="S8", item=item)
            return ctx.retry(lambda: pub.set_captions(vid, srt, language=language))
    if cap.get("pending"):
        return ctx.retry(lambda: pub.set_captions(vid, srt, language=language))
    entry["captions"] = {"pending": True, "language": language}
    save_entry(ctx.job, item, entry)
    return ctx.retry(lambda: pub.set_captions(vid, srt, language=language, lookup=False))


def _wait_processed(ctx: Ctx, item: str, video_id: str) -> dict[str, Any]:
    cfg = _upload_cfg(ctx)
    v = None
    for attempt in range(max(1, cfg["processing_polls"])):
        if attempt:
            ctx.sleep(cfg["processing_pause_s"])
        v = ctx.retry(lambda: ctx.publisher.get_video(video_id))
        if v and v.get("processing") == "succeeded":
            return v
    state = (v or {}).get("processing") if v else "not listed"
    raise VPError("transport", f"{item}: video still {state} after "
                               f"{cfg['processing_polls']} polls; the next run continues")


class S8Upload(StepSpec):
    id, deps, scope, version, gate = "S8", ("S6b", "S7"), "item", "1", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        j, it = ctx.job, ctx.item
        # Only this item's files: fragments.yaml / request.yaml are job-wide (a text change in
        # another fragment must not re-run this upload); what S8 takes from them is in config().
        return [j.render_mp4(it), j.final_srt(it), j.thumb_png(it), j.meta_yaml(it)]

    def config(self, ctx: Ctx) -> dict:
        y = _yt_cfg(ctx)
        info = fragment_info(ctx.job).get(ctx.item) or {}
        return {"privacy": PRIVACY, "category_id": y["category_id"], "language": y["language"],
                "playlist_title": playlist_title(ctx.job), "job_id": ctx.job.job_id,
                "fragment": {"kind": info.get("kind"), "knowledge_version": info.get("knowledge_version")},
                "publisher": ctx.publisher_kind}

    def outputs(self, ctx: Ctx) -> list[Path]:
        return []

    def run(self, ctx: Ctx) -> dict:
        job, item = ctx.job, ctx.item
        render = job.render_mp4(item)
        render_sha = sha256_file(render)
        if render_sha == MISSING:
            raise VPError("data", f"{item}: render.mp4 missing")
        y = _yt_cfg(ctx)
        meta = item_meta(job, item)
        key = make_vp_key(job.job_id, item, render_sha)
        tag = vp_tag(key)
        snippet = final_snippet(meta, tag, y["category_id"], y["language"])
        info = fragment_info(job).get(item) or {}
        entry = get_entry(job, item)
        if _needs_new_upload(entry, render_sha) and not (entry or {}).get("published"):
            quota_precheck(ctx)        # before any API call of a new upload
        _check_channel(ctx)

        if entry and entry.get("vp_key") != key and entry.get("published"):
            # The platform cannot replace the video of a published item, and a second upload
            # would be a duplicate on the channel (PRODUCT §7.7): stop, the owner decides.
            raise VPError("data", f"{item}: the render changed after the video was published "
                                  f"({entry.get('video_id')}); not re-uploading — decide manually")
        if entry and entry.get("vp_key") != key:
            if entry.get("state") in ("uploading", "uncertain"):
                raise VPError("data", f"{item}: previous upload (other render) is "
                                      f"{entry.get('state')}; resolve it before uploading a new render")
            superseded = list(entry.get("superseded") or [])
            if entry.get("state") == "uploaded":
                superseded.append({"vp_key": entry.get("vp_key"), "video_id": entry.get("video_id"),
                                   "at": now_iso()})
            entry = {"superseded": superseded, "history": entry.get("history") or []}
        if not entry or "state" not in entry:
            entry = {**(entry or {}), "vp_key": key, "tag": tag, "state": "intent",
                     "kind": info.get("kind"), "knowledge_version": info.get("knowledge_version"),
                     "render_sha": render_sha, "privacy": PRIVACY,
                     "publisher": ctx.publisher_kind,
                     "meta_sha": sha256_obj(snippet)}
            save_entry(job, item, entry)

        entry = _drive_upload(ctx, item, entry, render, snippet)
        vid = entry["video_id"]
        pub = ctx.publisher
        calls = []
        # A published (public or scheduled) video is never changed here: changes are held and
        # reach the platform only through PUB after the owner approves the new package.
        frozen = bool(entry.get("published"))
        held: list[str] = []

        # metadata: only when meta changed since it was applied (keeps the S10 block otherwise)
        meta_sha = sha256_obj(snippet)
        if frozen and entry.get("meta_sha") != meta_sha:
            held.append("metadata")
        elif entry.get("meta_sha") != meta_sha:
            ctx.retry(lambda: pub.update_snippet(vid, dict(snippet)))
            entry["meta_sha"] = meta_sha
            save_entry(job, item, entry)
            calls.append("update_snippet")

        video = _wait_processed(ctx, item, vid)

        srt_sha = sha256_file(job.final_srt(item))
        if srt_sha == MISSING:
            raise VPError("data", f"{item}: final.srt missing")
        if frozen and (entry.get("captions") or {}).get("sha") != srt_sha:
            held.append("captions")
        elif (entry.get("captions") or {}).get("sha") != srt_sha:
            cid = _set_captions(ctx, item, entry, vid, job.final_srt(item), y["language"])
            entry["captions"] = {"id": cid, "sha": srt_sha, "language": y["language"]}
            save_entry(job, item, entry)
            calls.append("set_captions")

        thumb = job.thumb_png(item)
        thumb_sha = sha256_file(thumb)
        if thumb_sha == MISSING:
            raise VPError("data", f"{item}: thumb.png missing")
        if frozen and (entry.get("thumbnail") or {}).get("sha") != thumb_sha:
            held.append("thumbnail")
        elif (entry.get("thumbnail") or {}).get("sha") != thumb_sha:
            ctx.retry(lambda: pub.set_thumbnail(vid, thumb))
            entry["thumbnail"] = {"sha": thumb_sha}
            save_entry(job, item, entry)
            calls.append("set_thumbnail")

        title = playlist_title(job)
        pl = entry.get("playlist") or {}
        if frozen and (pl.get("title") != title or not pl.get("id")):
            held.append("playlist")
        elif pl.get("title") != title or not pl.get("id"):
            cache = ctx._shared.setdefault("s8_playlists", {})
            if title not in cache:
                cache[title] = ctx.retry(lambda: pub.ensure_playlist(title))
            pid = cache[title]
            ctx.retry(lambda: pub.add_to_playlist(pid, vid))
            entry["playlist"] = {"id": pid, "title": title}
            save_entry(job, item, entry)
            calls.append("add_to_playlist")

        # acceptance
        video = ctx.retry(lambda: pub.get_video(vid)) if calls else video
        if not video:
            raise VPError("data", f"{item}: video {vid} not found on the platform after upload")
        status = video.get("status") or {}
        if not entry.get("published") and status.get("privacyStatus") != PRIVACY:
            raise VPError("data", f"{item}: video is {status.get('privacyStatus')}, expected private")
        if video.get("processing") != "succeeded":
            raise VPError("transport", f"{item}: processing {video.get('processing')}")
        if tag not in ((video.get("snippet") or {}).get("tags") or []):
            raise VPError("data", f"{item}: vp tag missing on the platform")
        entry["processing"] = "succeeded"
        entry["checked_at"] = now_iso()
        if held:
            entry["held_for_approval"] = held
        else:
            entry.pop("held_for_approval", None)
        save_entry(job, item, entry)
        out = {"video_id": vid, "state": "uploaded", "calls": calls}
        if held:
            out["held_for_approval"] = held
        return out


STEP = S8Upload()


def resolve_upload(ctx: Ctx, item: str, decision: str | None = None) -> int:
    """CLI `resolve-upload <job> <item>` (runs under the job lock).

    For an `uncertain` (or stuck `uploading`) entry: search the channel by the vp key again.
    Found -> uploaded. Not found -> stays uncertain unless the owner decided `reupload`
    (argument or env VP_RESOLVE_DECISION=reupload): then the entry returns to `intent`.
    On a state change the loop guard of S8.<item> is reset so `run` continues.
    """
    decision = decision or os.environ.get("VP_RESOLVE_DECISION") or None
    entry = get_entry(ctx.job, item)
    if not entry:
        print(f"{item}: no upload entry")
        return 2
    state = entry.get("state")
    if state not in ("uncertain", "uploading"):
        print(f"{item}: state {state}, nothing to resolve")
        return 0
    vid = search_key(ctx, entry["vp_key"])
    if vid:
        _set_uploaded(ctx, item, entry, vid, "resolved: found by vp key")
        Status(ctx.job).reset("S8", item, by="resolve-upload")
        print(f"{item}: found on the channel -> uploaded")
        return 0
    if decision == "reupload":
        entry["state"] = "intent"
        entry.pop("session_uri", None)
        entry["resolved_by"] = "owner: reupload"
        save_entry(ctx.job, item, entry, state_note="resolved by owner: not on channel, re-upload")
        Status(ctx.job).reset("S8", item, by="resolve-upload")
        print(f"{item}: not on the channel; owner allowed re-upload -> intent")
        return 0
    print(f"{item}: not found on the channel; still uncertain. If the owner confirms there is no "
          f"such video, run with VP_RESOLVE_DECISION=reupload")
    return 2
