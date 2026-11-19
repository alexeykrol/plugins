"""What came out of a job, from job files only (no API calls): the single source for `vp results`
(final chat message, browser tabs) and for S11's report.

Effective publisher of a job ("youtube" | "fake"): recorded per upload in uploads.yaml
(`publisher`, since 2026-11-17); for older entries the S8 manifest config, then the link form;
without uploads the resolved config (config.yaml + request.yaml `publisher:`).
"""
from __future__ import annotations

from typing import Any

from ..adapters.publisher import FAKE_URL_PREFIX, fake_video_url, studio_url, youtube_video_url
from ..core.manifest import read_manifest
from ..core.paths import FULL, Job
from ..core.io import read_yaml
from ._package import (current_decision, fragment_info, load_approval, load_uploads,
                       read_package, series_items)

STATES = ("published", "scheduled", "uploaded", "rejected", "violations", "revoked", "deferred",
          "not_uploaded")


def entry_publisher(job: Job, item: str, entry: dict[str, Any] | None) -> str | None:
    e = entry or {}
    if e.get("publisher") in ("youtube", "fake"):
        return str(e["publisher"])
    try:
        man = read_manifest(job, "S8", item) or {}
    except Exception:  # noqa: BLE001 — a broken manifest just gives no answer
        man = {}
    pub = (man.get("config") or {}).get("publisher")
    if pub in ("youtube", "fake"):
        return str(pub)
    if str(e.get("url") or "").startswith(FAKE_URL_PREFIX):
        return "fake"
    return None


def job_publisher(job: Job, config: dict[str, Any] | None = None) -> str:
    """"fake" if any upload of the job was a rehearsal, "youtube" if uploads were real; without
    uploads the job's configured publisher (request.yaml overrides config.yaml)."""
    uploads = load_uploads(job)["items"]
    kinds = {entry_publisher(job, it, e) for it, e in uploads.items() if isinstance(e, dict)}
    kinds.discard(None)
    if "fake" in kinds:
        return "fake"
    if kinds:
        return "youtube"
    req = job.request()
    if req.get("publisher") in ("youtube", "fake"):
        return str(req["publisher"])
    return str((config or {}).get("publisher") or "youtube")


def outcomes(job: Job) -> list[dict[str, Any]]:
    """Per item in series order: item, kind, title, state (S11 vocabulary: published, scheduled,
    rejected, violations, revoked, deferred, not_uploaded, waiting), why, url, video_id,
    published_at, knowledge_version, entry."""
    uploads = load_uploads(job)["items"]
    review = (read_yaml(job.review_yaml, default={}) or {}).get("items") or {}
    approvals = load_approval(job)
    info = fragment_info(job)
    out = []
    for it in series_items(job):
        e = uploads.get(it) or {}
        pkg = read_package(job.package(it)) or {}
        r = review.get(it) or {}
        d = current_decision(approvals, it, pkg.get("package_hash", "")) if pkg else None
        last = [a for a in approvals if a.get("item") == it]
        pub = e.get("published") or {}
        if pub:
            state = "published" if pub.get("when") == "now" else "scheduled"
            why = ""
        elif e.get("state") != "uploaded":
            state, why = "not_uploaded", f"загрузка: {e.get('state') or 'нет'}"
        elif r.get("status") == "violations":
            state, why = "violations", ", ".join(r.get("violations") or [])
        elif last and last[-1].get("decision") == "revoked":
            state, why = "revoked", ", ".join(last[-1].get("reason") or [])
        elif d and d.get("decision") == "reject":
            state, why = "rejected", "отклонено владельцем"
        elif e.get("publish_pending"):
            state, why = "deferred", f"расписание {e['publish_pending'].get('choice')} — не поддержано в v1"
        else:
            state, why = "waiting", "нет решения"
        title = ((pkg.get("metadata") or {}).get("title")) or (info.get(it) or {}).get("title", "")
        out.append({"item": it, "kind": "full" if it == FULL else (info.get(it) or {}).get("kind"),
                    "title": title, "state": state, "why": why, "url": e.get("url"),
                    "video_id": e.get("video_id"), "published_at": pub.get("published_at"),
                    "knowledge_version": e.get("knowledge_version"), "entry": e})
    return out


def _duration_s(job: Job, item: str) -> float | None:
    man = read_yaml(job.render_manifest(item), default=None)
    ms = (man or {}).get("duration_ms") if isinstance(man, dict) else None
    try:
        return round(int(ms) / 1000, 1) if ms else None
    except (TypeError, ValueError):
        return None


def results(job: Job, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The `vp results --json` document (shape fixed, relied on by the plugin skills)."""
    kind = job_publisher(job, config)
    items = []
    for o in outcomes(job):
        it, e = o["item"], o["entry"]
        vid = o["video_id"] if e.get("state") == "uploaded" else None
        fake = (entry_publisher(job, it, e) or kind) == "fake"
        url = studio = None
        if vid:
            url = fake_video_url(vid) if fake else (o["url"] or youtube_video_url(vid))
            studio = None if fake else studio_url(vid)
        pub = e.get("published") or {}
        privacy = None
        if vid:
            privacy = "public" if pub.get("when") == "now" else str(e.get("privacy") or "private")
        render, thumb = job.render_mp4(it), job.thumb_png(it)
        title = o["title"] or None
        items.append({
            "id": it, "kind": o["kind"], "title": title, "duration_s": _duration_s(job, it),
            "state": "uploaded" if o["state"] == "waiting" else o["state"],
            "privacy": privacy, "url": url, "studio_url": studio,
            "render_path": str(render.resolve()) if render.exists() else None,
            "thumb_path": str(thumb.resolve()) if thumb.exists() else None,
            "publish_at": pub.get("published_at") or None})
    return {"job": job.job_id, "publisher": kind, "items": items}


def text_table(doc: dict[str, Any]) -> str:
    """Short table for the terminal; a rehearsal says so on the first line."""
    lines = []
    if doc["publisher"] == "fake":
        lines.append("REHEARSAL — nothing was uploaded to YouTube; links below are local files.")
    lines.append(f"job {doc['job']}: {len(doc['items'])} video(s), publisher {doc['publisher']}")
    for r in doc["items"]:
        dur = f"{r['duration_s']:.0f} s" if r["duration_s"] is not None else "-"
        state = r["state"] + (" (rehearsal)" if doc["publisher"] == "fake"
                              and r["state"] in ("published", "scheduled", "uploaded") else "")
        where = (r["render_path"] if doc["publisher"] == "fake" else
                 r["studio_url"] if r["state"] == "uploaded" else r["url"]) or "-"
        when = f" at {r['publish_at']}" if r["publish_at"] and r["state"] == "scheduled" else ""
        lines.append(f"  {r['id']:<6} {r['kind'] or '-':<5} {dur:>7}  {state}{when}  "
                     f"{(r['title'] or '-')[:60]}  {where}")
    return "\n".join(lines)
