"""S9 — approval package, formal review by code, owner approval (SPEC S9). Job scope, human gate.

1. Package (code): package/<item>.yaml = hashes of render.mp4, final.srt, thumb.png + the final
   title / description (with the S10 links block) / tags (with the vp tag) / category / language +
   video id, channel, playlist -> package_hash (canonical JSON). The schedule is NOT in the package.
2. Formal review (code), per item -> review.yaml {ok | violation codes, package_hash}:
   processed; private (unless already published); title/description/tags limits and forbidden
   characters; chapters (00:00, ≥ 10 s, ascending, ≥ 3 — if there are chapters); captions on the
   platform == final.srt (recorded upload sha + the platform's caption flag, no download —
   `captions_verified`); thumbnail set, 1280×720, < 2 MB; playlist assigned; links block present
   and every link points to a video of this series; language; author fields; and "what is on
   the platform now == package". Optional model nuance check (task s9.nuance.<item>, config
   s9.nuance, off by default) only adds notes, never clears a violation.
3. Approval: items with violations are not offered. For OK items without a valid decision the
   owner gets the series summary + one message per item (title, thesis, description; buttons:
   approve now / approve at a slot / reject) — once per (item, package_hash); then the gate stays
   closed (WaitingHuman, kind review). `approve-ingest` turns the answers into approval.yaml.
   The gate closes when every OK item has an approve/reject on its CURRENT package hash.

Config: approval.timezone ("UTC"), approval.schedule_slots ([{label, day_offset, time}]),
s9.nuance (false), youtube.language ("ru").
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.io import read_yaml, sha256_bytes, write_yaml, now_iso
from . import _limits
from ._chapters import chapter_violations
from ._package import (DEFAULT_CATEGORY, DEFAULT_LANGUAGE, build_package, captions_verified,
                       current_decision, extract_links_block, final_snippet, fragment_info,
                       item_meta, last_revoked_at, link_video_id, load_approval, load_uploads,
                       parse_chapters, platform_mismatches, png_size, read_package,
                       series_items, strip_links_block, verify_derived, write_if_changed)
from .base import Ctx, StepSpec, VPError, WaitingHuman

try:  # written by the S6 worker; S9 must not silently skip the author check
    from ._author_fields import check_author_fields
except ImportError:  # pragma: no cover
    check_author_fields = None

NUANCE_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "s9_nuance.md"
THUMB_SIZE = (1280, 720)
THUMB_MAX_BYTES = 2 * 1024 * 1024
MSG_CHUNK = 3800
DEFAULT_SLOTS = [{"label": "завтра 10:00", "day_offset": 1, "time": "10:00"}]


def approval_cfg(ctx: Ctx) -> dict[str, Any]:
    a = ctx.config.get("approval") or {}
    slots = a.get("schedule_slots")
    return {"timezone": str(a.get("timezone") or "UTC"),
            "schedule_slots": list(slots) if isinstance(slots, list) else list(DEFAULT_SLOTS)}


def _language(ctx: Ctx) -> str:
    return str((ctx.config.get("youtube") or {}).get("language") or DEFAULT_LANGUAGE)


def _category(ctx: Ctx) -> str:
    return str((ctx.config.get("youtube") or {}).get("category_id") or DEFAULT_CATEGORY)


def package_metadata(ctx: Ctx, item: str, entry: dict[str, Any],
                     crosslinks: dict[str, Any]) -> dict[str, Any]:
    """Final metadata of an item: S8's snippet (meta + vp tag) with the S10 description."""
    meta = item_meta(ctx.job, item)
    snip = final_snippet(meta, entry["tag"], _category(ctx), _language(ctx))
    desc = ((crosslinks.get("descriptions") or {}).get(item) or {}).get("description")
    if not isinstance(desc, str):
        raise VPError("data", f"S9: crosslinks.yaml has no description for {item}")
    snip["description"] = desc
    return snip


def build_all_packages(ctx: Ctx, channel: str) -> dict[str, dict[str, Any]]:
    """Build and write package/<item>.yaml for every item (bytes kept when unchanged)."""
    job = ctx.job
    uploads = load_uploads(job)["items"]
    crosslinks = read_yaml(job.crosslinks_yaml, default={}) or {}
    info = fragment_info(job)
    out = {}
    for it in series_items(job):
        entry = uploads.get(it) or {}
        if entry.get("state") != "uploaded":
            raise VPError("data", f"S9: {it} is not uploaded (state {entry.get('state')})")
        pkg = build_package(job, it, video_id=entry["video_id"], channel=channel,
                            playlist=entry.get("playlist") or {},
                            metadata=package_metadata(ctx, it, entry, crosslinks),
                            kind=(info.get(it) or {}).get("kind", "reel"))
        write_if_changed(job.package(it), pkg)
        out[it] = pkg
    return out


def _author_codes(ctx: Ctx, item: str, meta: dict[str, Any]) -> list[str]:
    if check_author_fields is None:
        return ["author_check_unavailable"]
    values = {"title": meta["title"], "description": meta["description"],
              "tags": [t for t in meta["tags"] if not t.startswith("vp-")]}
    src = item_meta(ctx.job, item)
    if "hashtags" in src:
        values["hashtags"] = src["hashtags"]
    out = [f"author_{v['field']}_{v['code']}"
           for v in check_author_fields(values, ctx.job.request(), item)]
    design = read_yaml(ctx.job.design_texts(item), default=None)
    if isinstance(design, dict):
        dvals = {k: design[k] for k in ("intro_text", "announce_text", "thumbnail_text")
                 if isinstance(design.get(k), str)}
        if dvals:
            out += [f"author_{v['field']}_{v['code']}"
                    for v in check_author_fields(dvals, ctx.job.request(), item)]
    return out


def _chapters(ctx: Ctx, item: str, meta_src: dict[str, Any], description: str) -> list[dict[str, Any]]:
    ch = meta_src.get("chapters")
    if isinstance(ch, list) and all(isinstance(c, dict) and isinstance(c.get("start_ms"), int) for c in ch):
        return ch
    return parse_chapters(description)


# platform differences a published item may carry while a changed package waits for approval
HELD_UPDATABLE = ("platform_title_mismatch", "platform_description_mismatch", "platform_tags_mismatch",
                  "platform_categoryId_mismatch", "platform_defaultLanguage_mismatch",
                  "captions_mismatch", "thumbnail_mismatch", "playlist_mismatch")


def verify_captions(ctx: Ctx, item: str, vid: str, entry: dict[str, Any],
                    video: dict[str, Any] | None) -> str | None:
    """None if the platform has the approved captions (see _package.captions_verified)."""
    lang = _language(ctx)
    cap_id = ((entry or {}).get("captions") or {}).get("id")
    return captions_verified(entry, ctx.job.final_srt(item), video,
                             lambda: ctx.retry(lambda: ctx.publisher.has_caption_track(
                                 vid, cap_id, language=lang)))


def review_item(ctx: Ctx, pkg: dict[str, Any], entry: dict[str, Any], series_ids: set[str],
                channel: str) -> dict[str, Any]:
    """Formal check of one item. Returns {status, violations, notes, package_hash}."""
    job, item = ctx.job, pkg["item"]
    pub = ctx.publisher
    meta = pkg["metadata"]
    vid = pkg["video_id"]
    v: list[str] = []
    notes: list[str] = []
    video = ctx.retry(lambda: pub.get_video(vid))
    cap_code = verify_captions(ctx, item, vid, entry, video)
    published = bool(entry.get("published"))
    if video is None:
        v.append("video_missing")
    else:
        if video.get("processing") != "succeeded":
            v.append("not_processed")
        if not published and (video.get("status") or {}).get("privacyStatus") != "private":
            v.append("not_private")
    v += _limits.metadata_violations({"title": meta["title"], "description": meta["description"],
                                      "tags": meta["tags"]})
    src = item_meta(job, item)
    man = read_yaml(job.render_manifest(item), default={}) or {}
    duration = int(man.get("duration_ms") or 10 ** 9)
    v += chapter_violations(_chapters(ctx, item, src, meta["description"]), duration)
    if cap_code:
        v.append(cap_code)
    if (entry.get("thumbnail") or {}).get("sha") is None:
        v.append("thumbnail_not_set")
    if png_size(job.thumb_png(item)) != THUMB_SIZE:
        v.append("thumbnail_size")
    if job.thumb_png(item).exists() and job.thumb_png(item).stat().st_size >= THUMB_MAX_BYTES:
        v.append("thumbnail_too_big")
    if not (entry.get("playlist") or {}).get("id"):
        v.append("playlist_missing")
    block = extract_links_block(meta["description"])
    if block is None:
        v.append("links_block_missing")
    else:
        for line in block:
            tid = link_video_id(line)
            if tid is None or tid not in series_ids:
                v.append("link_foreign")
                break
    if meta.get("defaultLanguage") != "ru":
        v.append("language_not_ru")
    v += _author_codes(ctx, item, meta)

    platform = platform_mismatches(job, pkg, video, cap_code is None, entry, channel)
    if platform and published:
        derived = read_package(job.package_derived(item))
        if derived and derived.get("parent_hash") == pkg["package_hash"] \
                and not verify_derived(pkg, derived):
            platform = platform_mismatches(job, derived, video, cap_code is None, entry, channel)
            notes.append("published_with_derived_package")
    if platform and published and all(c in HELD_UPDATABLE for c in platform):
        # a published video keeps its approved state on the platform; the local change is held
        # (S8/S10 never touch a published video) and is applied by PUB only after the owner
        # approves this new package
        notes.append("update_held_for_approval: " + ", ".join(platform))
        v = [c for c in v if c != "captions_mismatch"]
        platform = []
    v += [c for c in platform if c not in v]

    if (ctx.config.get("s9") or {}).get("nuance"):
        notes += _nuance(ctx, item, meta)
    v = list(dict.fromkeys(v))
    return {"status": "ok" if not v else "violations", "violations": v, "notes": notes,
            "package_hash": pkg["package_hash"], "video_id": vid}


def _nuance(ctx: Ctx, item: str, meta: dict[str, Any]) -> list[str]:
    """Optional model check: texts only, the links block (URLs) removed."""
    info = fragment_info(ctx.job).get(item) or {}
    text = (f"type: {info.get('kind')}\nthesis: {info.get('thesis', '')}\n"
            f"title: {meta['title']}\ndescription:\n{strip_links_block(meta['description'])}\n")
    raw = ctx.judge.run(f"s9.nuance.{item}", NUANCE_PROMPT.read_text(encoding="utf-8"),
                        {"video": text}, "output.txt", ctx).strip()
    if raw == "OK":
        return []
    if raw.startswith("FLAG:") and len(raw) < 500 and "\n" not in raw:
        return [f"nuance: {raw[5:].strip()}"]
    raise VPError("contract", f"s9.nuance.{item}: expected `OK` or `FLAG: ...`")


# ---- notification ---------------------------------------------------------------------------

def _chunks(text: str, size: int = MSG_CHUNK) -> list[str]:
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


def item_buttons(ctx: Ctx, h: str) -> list[list[dict[str, str]]]:
    h8 = h[:8]
    row = [{"text": "Одобрить: сейчас", "data": f"a|now|{h8}"}]
    for k, slot in enumerate(approval_cfg(ctx)["schedule_slots"]):
        row.append({"text": f"Одобрить: {slot.get('label', k)}", "data": f"a|s{k}|{h8}"})
    return [row, [{"text": "Отклонить", "data": f"r|{h8}"}]]


def item_text(ctx: Ctx, n: int, total: int, item: str, pkg: dict[str, Any]) -> str:
    info = fragment_info(ctx.job).get(item) or {}
    meta = pkg["metadata"]
    return (f"vp {ctx.job.job_id}: ролик {n}/{total} [{pkg.get('kind')}] {item}\n"
            f"Название: {meta['title']}\n"
            f"Тезис: {info.get('thesis') or '-'}\n"
            f"Описание:\n{meta['description']}\n"
            f"Пакет: {pkg['package_hash'][:12]}")


def summary_text(ctx: Ctx, review: dict[str, Any]) -> str:
    items = review["items"]
    ok = [i for i, r in items.items() if r["status"] == "ok"]
    bad = [i for i, r in items.items() if r["status"] != "ok"]
    lines = [f"vp {ctx.job.job_id}: серия готова к одобрению — {len(ok)} из {len(items)} роликов "
             f"прошли проверку."]
    if bad:
        lines.append("Не предлагаются (нарушения):")
        lines += [f"- {i}: {', '.join(items[i]['violations'])}" for i in bad]
    lines.append(f"Рекомендация: {review['series']['recommendation']}")
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "\n…"


def send_pending(ctx: Ctx, review: dict[str, Any], packages: dict[str, dict[str, Any]],
                 pending: list[str], approvals: list[dict[str, Any]]) -> int:
    """Send messages for pending items that were not yet notified on their current hash."""
    notes = review.setdefault("notifications", [])
    need = []
    for it in pending:
        h = packages[it]["package_hash"]
        revoked = last_revoked_at(approvals, it)
        sent = [n for n in notes if n.get("item") == it and n.get("package_hash") == h
                and (revoked is None or str(n.get("sent_at")) > revoked)]
        if not sent:
            need.append(it)
    if not need:
        return 0
    nt = ctx.notifier
    ref = nt.send(summary_text(ctx, review))
    notes.append({"kind": "summary", "message_ref": ref, "sent_at": now_iso()})
    order = series_items(ctx.job)
    for it in need:
        pkg = packages[it]
        parts = _chunks(item_text(ctx, order.index(it) + 1, len(order), it, pkg))
        for p in parts[:-1]:
            nt.send(p)
        ref = nt.send(parts[-1], buttons=item_buttons(ctx, pkg["package_hash"]))
        notes.append({"kind": "item", "item": it, "message_ref": ref,
                      "package_hash": pkg["package_hash"], "sent_at": now_iso()})
    return len(need)


class S9Package(StepSpec):
    id, deps, scope, version, gate = "S9", ("S10",), "job", "1", "human"

    def inputs(self, ctx: Ctx) -> list[Path]:
        j = ctx.job
        out = [j.crosslinks_yaml, j.approval_yaml, j.request_yaml, j.fragments_yaml]
        for it in j.items() or []:
            out += [j.render_mp4(it), j.final_srt(it), j.thumb_png(it), j.meta_yaml(it),
                    j.design_texts(it), j.render_manifest(it)]
        return out

    def config(self, ctx: Ctx) -> dict:
        nuance = bool((ctx.config.get("s9") or {}).get("nuance"))
        cfg = {"nuance": nuance, "language": _language(ctx), "category": _category(ctx),
               "approval": approval_cfg(ctx), "author_check": check_author_fields is not None}
        if nuance:
            cfg["nuance_prompt_sha"] = sha256_bytes(NUANCE_PROMPT.read_text(encoding="utf-8"))
        return cfg

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.package(it) for it in ctx.job.items() or []] + [ctx.job.review_yaml]

    def run(self, ctx: Ctx) -> dict:
        job = ctx.job
        channel = str(ctx.retry(ctx.publisher.channel_id))
        packages = build_all_packages(ctx, channel)
        uploads = load_uploads(job)["items"]
        series_ids = {packages[it]["video_id"] for it in packages}
        old = read_yaml(job.review_yaml, default={}) or {}
        items = {it: review_item(ctx, packages[it], uploads.get(it) or {}, series_ids, channel)
                 for it in packages}
        ok = [it for it, r in items.items() if r["status"] == "ok"]
        bad = [it for it in items if it not in ok]
        rec = ("publish" if not bad else
               "fix and rerun S6/S7/S8 for: " + ", ".join(bad) if ok else "fix before approval")
        review = {"version": 1, "items": items,
                  "series": {"ok": len(ok), "violations": len(bad), "recommendation": rec},
                  "notifications": list(old.get("notifications") or [])}
        if not ok:
            write_yaml(job.review_yaml, review)
            raise VPError("data", f"S9: no item passed the formal review ({len(bad)} with violations)")
        approvals = load_approval(job)
        decided = {it: current_decision(approvals, it, packages[it]["package_hash"]) for it in ok}
        pending = [it for it in ok if decided[it] is None]
        if pending:
            sent = send_pending(ctx, review, packages, pending, approvals)
            write_yaml(job.review_yaml, review)
            ctx.log.event("s9_waiting", pending=len(pending), sent=sent)
            raise WaitingHuman("S9.approval", f"owner decision for {len(pending)} item(s): in the "
                               f"dialog `approve show|set <job>`, or notifier answers + "
                               f"`approve-ingest <job>`", kind="review")
        write_yaml(job.review_yaml, review)
        closed = max(str(d.get("decided_at") or "") for d in decided.values())
        return {"gate_closed_at": closed or now_iso(),
                "approved": sum(1 for d in decided.values() if d["decision"] == "approve"),
                "rejected": sum(1 for d in decided.values() if d["decision"] == "reject"),
                "violations": len(bad)}


STEP = S9Package()
