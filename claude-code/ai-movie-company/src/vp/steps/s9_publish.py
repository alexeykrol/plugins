"""PUB — the publish gate (SPEC S9 «Стоп»), and `approve-ingest` (notifier answers -> approval.yaml).

PUB (job scope, after the S9 gate): publishes ONLY items whose latest approval.yaml entry is an
`approve` of the item's CURRENT package hash and whose review is ok. Everything else stays
private and untouched.

If some items of the series are not published (rejected, violations, revoked), the approved
items get a DERIVED package: the approved package with exactly the link lines pointing at those
videos removed (transform remove_links, package/<item>.derived.yaml, parent_hash = approved
hash). The description on the platform is updated accordingly (read-modify-write).

Immediately before each publish call code checks one of:
  (a) platform + local files == the approved package, or
  (b) == a derived package whose parent_hash is the approved hash and whose transform is valid.
Otherwise the item is refused: a `revoked` entry is appended to approval.yaml (the item goes
back to approval; S9 re-asks the owner) and the owner is notified.

publish choice: "now" -> public; {at: iso} -> publishAt (refused if already in the past);
{event}/{cron} -> deferred (v1 has no scheduler; the item stays private, reported by S11).
The schedule is not part of the package: changing it changes approval.yaml, not package_hash.
Publish facts are written into uploads.yaml (`published`: when, published_at, hashes) — the
v2 audience metric needs the publish time per video (SPEC §11).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core.io import (now_iso, parse_iso, read_json, read_yaml, sha256_file, sha256_obj,
                       write_json, write_yaml)
from ._package import (append_approval, build_derived, current_decision,
                       extract_links_block, final_snippet, get_entry, item_meta, link_video_id,
                       load_approval, platform_mismatches, read_package, save_entry, series_items,
                       validate_publish, verify_derived)
from .base import Ctx, StepSpec, VPError
from .s9_package import _category, _language, approval_cfg, verify_captions

PENDING_FILE = "notifier_answers_pending.json"


# ---- approve-ingest -------------------------------------------------------------------------

def slot_time(slot: dict[str, Any], answered_at: str, tz_name: str) -> str:
    """ISO UTC time of a schedule slot {day_offset, time "HH:MM"} relative to the answer date."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:
        raise VPError("data", f"approval.timezone: unknown zone {tz_name!r}") from e
    hh, mm = (int(x) for x in str(slot.get("time", "10:00")).split(":"))
    base = parse_iso(answered_at).astimezone(tz)
    day = base.date() + timedelta(days=int(slot.get("day_offset", 1)))
    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_answer(data: str, package_hash: str, answered_at: str, cfg: dict[str, Any]
                 ) -> tuple[str, Any] | None:
    """Button data -> (decision, publish) or None if invalid / for another package hash.

    a|now|<h8>   approve, publish now
    a|s<k>|<h8>  approve, publish at schedule slot k
    r|<h8>       reject
    """
    parts = str(data).split("|")
    if parts[0] == "a" and len(parts) == 3:
        choice, h8 = parts[1], parts[2]
        if h8 != package_hash[:8]:
            return None
        if choice == "now":
            return "approve", "now"
        if choice.startswith("s") and choice[1:].isdigit():
            k = int(choice[1:])
            slots = cfg["schedule_slots"]
            if k >= len(slots):
                return None
            return "approve", {"at": slot_time(slots[k], answered_at, cfg["timezone"])}
        return None
    if parts[0] == "r" and len(parts) == 2:
        return ("reject", None) if parts[1] == package_hash[:8] else None
    return None


def approve_ingest(ctx: Ctx) -> int:
    """CLI `approve-ingest <job>`: notifier answers -> approval.yaml entries (code only).

    Answers that belong to other jobs are kept in <vp_home>/notifier_answers_pending.json for
    their own approve-ingest. Each entry carries the package_hash the owner saw.
    """
    job = ctx.job
    review = read_yaml(job.review_yaml, default={}) or {}
    refs = {str(n["message_ref"]): n for n in review.get("notifications") or []
            if n.get("kind") == "item" and n.get("message_ref") is not None}
    pending_path = ctx.home / PENDING_FILE
    answers = list(read_json(pending_path, default=[]) or []) + list(ctx.notifier.poll_answers())
    mine = [a for a in answers if str(a.get("message_ref")) in refs]
    others = [a for a in answers if str(a.get("message_ref")) not in refs]
    if others or pending_path.exists():
        write_json(pending_path, others)
    cfg = approval_cfg(ctx)
    entries, skipped = [], 0
    for a in sorted(mine, key=lambda a: str(a.get("at") or "")):
        note = refs[str(a["message_ref"])]
        at = str(a.get("at") or now_iso())
        parsed = parse_answer(str(a.get("data", "")), str(note["package_hash"]), at, cfg)
        if parsed is None:
            skipped += 1
            ctx.log.event("approval_answer_skipped", item=note.get("item"))
            continue
        decision, publish = parsed
        e = {"item": note["item"], "package_hash": note["package_hash"], "decision": decision,
             "decided_at": at, "by": str(a.get("user") or "owner"),
             "message_ref": str(a["message_ref"])}
        if publish is not None:
            e["publish"] = validate_publish(publish)
        entries.append(e)
    append_approval(job, entries)
    print(f"approve-ingest: {len(entries)} decision(s) recorded, {skipped} skipped")
    return 0


# ---- PUB ------------------------------------------------------------------------------------

def _revoke(ctx: Ctx, item: str, pkg_hash: str, reasons: list[str]) -> None:
    append_approval(ctx.job, [{"item": item, "package_hash": pkg_hash, "decision": "revoked",
                               "reason": reasons, "decided_at": now_iso(), "by": "vp"}])
    try:
        ctx.notifier.send(f"vp {ctx.job.job_id}: {item} NOT published — what is on the platform "
                          f"differs from the approved package ({', '.join(reasons[:6])}). "
                          f"It goes back to approval.")
    except Exception as e:
        ctx.log.event("notify_failed", step="PUB", item=item, error=type(e).__name__)


def _target_package(ctx: Ctx, item: str, pkg: dict[str, Any], remove: list[str]) -> dict[str, Any]:
    """The approved package, or a (re)used derived package with exactly `remove` removed."""
    if not remove:
        return pkg
    path = ctx.job.package_derived(item)
    old = read_package(path)
    if old and old.get("parent_hash") == pkg["package_hash"] and old.get("removed") == remove \
            and not verify_derived(pkg, old):
        return old
    derived = build_derived(pkg, remove)
    errs = verify_derived(pkg, derived)
    if errs:
        raise VPError("internal", f"PUB: derived package of {item} invalid: {errs}")
    write_yaml(path, derived)
    return derived


def _apply_approved_update(ctx: Ctx, item: str, vid: str, target: dict[str, Any],
                           entry: dict[str, Any]) -> dict[str, Any]:
    """Bring a published video to the approved (possibly derived) package: snippet fields
    (read-modify-write), captions, thumbnail. Only called for an approved package."""
    job, pub = ctx.job, ctx.publisher
    meta = target["metadata"]
    video = ctx.retry(lambda: pub.get_video(vid)) or {}
    sn = video.get("snippet") or {}
    changes = {k: meta[k] for k in SNIPPET_KEYS if k in meta and sn.get(k) != meta[k]}
    if changes:
        ctx.retry(lambda: pub.update_snippet(vid, changes))
    lang = _language(ctx)
    srt = job.final_srt(item)
    cap = entry.get("captions") or {}
    if cap.get("sha") != sha256_file(srt) or not cap.get("id"):
        cid = ctx.retry(lambda: pub.set_captions(vid, srt, language=lang,
                                                 track_id=cap.get("id") or None))
        entry["captions"] = {"id": cid, "sha": sha256_file(srt), "language": lang}
    thumb = job.thumb_png(item)
    thumb_sha = sha256_file(thumb)
    if (entry.get("thumbnail") or {}).get("sha") != thumb_sha:
        ctx.retry(lambda: pub.set_thumbnail(vid, thumb))
        entry["thumbnail"] = {"sha": thumb_sha}
    snippet = final_snippet(item_meta(job, item), entry["tag"], _category(ctx), lang)
    entry["meta_sha"] = sha256_obj(snippet)
    entry.pop("held_for_approval", None)
    save_entry(job, item, entry, state_note="approved update applied to the published video")
    return entry


SNIPPET_KEYS = ("title", "description", "tags", "categoryId", "defaultLanguage")


class S9Publish(StepSpec):
    id, deps, scope, version, gate = "PUB", ("S9",), "job", "1", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        j = ctx.job
        return [j.approval_yaml, j.review_yaml] + [j.package(it) for it in j.items() or []]

    def config(self, ctx: Ctx) -> dict:
        return {"language": _language(ctx)}

    def outputs(self, ctx: Ctx) -> list[Path]:
        return []

    def run(self, ctx: Ctx) -> dict:
        job = ctx.job
        pub = ctx.publisher
        items = series_items(job)
        packages = {}
        for it in items:
            p = read_package(job.package(it))
            if not p:
                raise VPError("data", f"PUB: package of {it} missing")
            packages[it] = p
        review = (read_yaml(job.review_yaml, default={}) or {}).get("items") or {}
        approvals = load_approval(job)
        approved: dict[str, dict[str, Any]] = {}
        for it in items:
            d = current_decision(approvals, it, packages[it]["package_hash"])
            r = review.get(it) or {}
            if d and d["decision"] == "approve" and r.get("status") == "ok" \
                    and r.get("package_hash") == packages[it]["package_hash"]:
                approved[it] = d
        unpublished_ids = {packages[it]["video_id"] for it in items if it not in approved}
        channel = str(ctx.retry(pub.channel_id))
        result: dict[str, list[str]] = {"published": [], "scheduled": [], "deferred": [],
                                        "refused": [], "unchanged": [], "updated": []}
        for it in items:
            if it not in approved:
                continue
            d = approved[it]
            pkg = packages[it]
            vid = pkg["video_id"]
            choice = validate_publish(d.get("publish", "now"))
            block = extract_links_block(pkg["metadata"]["description"]) or []
            remove = [ln for ln in block if link_video_id(ln) in unpublished_ids]
            target = _target_package(ctx, it, pkg, remove)
            if target is not pkg:
                video = ctx.retry(lambda: pub.get_video(vid))
                desc = target["metadata"]["description"]
                if video is not None and (video.get("snippet") or {}).get("description") != desc:
                    ctx.retry(lambda: pub.update_snippet(vid, {"description": desc}))

            entry = get_entry(job, it) or {}
            prev_pub = entry.get("published") or {}
            if prev_pub and prev_pub.get("package_hash") != pkg["package_hash"]:
                # already public/scheduled and the owner approved a CHANGED package: apply the
                # held update (S8/S10 never touch a published video), then verify as usual
                entry = _apply_approved_update(ctx, it, vid, target, entry)
                result["updated"].append(it)

            # the stop: verify immediately before the publish call
            video = ctx.retry(lambda: pub.get_video(vid))
            caps_ok = verify_captions(ctx, it, vid, entry, video) is None
            problems = platform_mismatches(job, target, video, caps_ok, entry, channel)
            if target is not pkg:
                if target.get("parent_hash") != d["package_hash"]:
                    problems.append("derived_parent_not_approved")
                problems += verify_derived(pkg, target)
            if problems:
                _revoke(ctx, it, pkg["package_hash"], problems)
                result["refused"].append(it)
                continue

            if choice == "now":
                when = "now"
            elif "at" in choice:
                when = parse_iso(choice["at"]).astimezone(timezone.utc).isoformat(timespec="seconds")
                if parse_iso(when) <= datetime.now(timezone.utc) + timedelta(minutes=1):
                    _revoke(ctx, it, pkg["package_hash"], ["schedule_in_past"])
                    result["refused"].append(it)
                    continue
            else:
                entry["publish_pending"] = {"choice": choice, "package_hash": pkg["package_hash"]}
                save_entry(job, it, entry)
                result["deferred"].append(it)
                continue

            target_hash = target.get("derived_hash") or pkg["package_hash"]
            prev = entry.get("published") or {}
            if prev.get("when") == when and prev.get("target_hash") == target_hash:
                result["unchanged"].append(it)
                continue
            if prev.get("when") == "now":
                if prev.get("target_hash") != target_hash:
                    prev.update(package_hash=pkg["package_hash"], derived_hash=target.get("derived_hash"),
                                target_hash=target_hash, updated_at=now_iso(),
                                decided_by=d.get("by"), decided_at=d.get("decided_at"))
                    entry["published"] = prev
                    save_entry(job, it, entry, state_note="published video updated (approved)")
                else:
                    result["unchanged"].append(it)  # already public: a later schedule cannot undo it
                continue
            ctx.retry(lambda: pub.publish(vid, when))
            entry["published"] = {
                "when": when, "published_at": now_iso() if when == "now" else when,
                "requested_at": now_iso(), "package_hash": pkg["package_hash"],
                "derived_hash": target.get("derived_hash"), "target_hash": target_hash,
                "decided_by": d.get("by"), "decided_at": d.get("decided_at")}
            entry.pop("publish_pending", None)
            save_entry(job, it, entry, state_note=f"published ({'now' if when == 'now' else 'scheduled'})")
            result["published" if when == "now" else "scheduled"].append(it)
        return {k: v for k, v in result.items()}


STEP = S9Publish()
