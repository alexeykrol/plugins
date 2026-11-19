"""`approve ...` — the S9 publish approval in the dialog (UX v1, gate 2).

  approve show <job> [--json]
      per item: title, description, thesis, thumbnail path, private video URL, formal review
      result (review.yaml violations) and package_hash. Records the package hash shown per item in
      approval.shown.yaml — the hash the owner saw — and when it was first shown (`shown_at`).
  approve set <job> <item> --decision approve|reject [--publish now|<iso>|slot:<label>]
                           [--package-hash H]
      appends one approval.yaml entry {item, package_hash, decision, decided_at, by: dialog,
      publish?}. Refused when: the item was not shown, its package changed since `approve show`,
      --package-hash does not match, the formal review is stale or not ok, the publish choice is
      invalid or in the past, or no real user input arrived after the item was shown
      (vp.gates.human: `REFUSED user-input-required <item>: ...`, exit 4).

Code writes approval.yaml; PUB publishes only an approve of the CURRENT package hash (same rule
as the Telegram/file path).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

from ..core.io import now_iso, parse_iso, read_yaml
from ..core.paths import Job
from ..steps._package import (append_approval, current_decision, fragment_info, load_approval,
                              load_uploads, read_package, series_items, validate_publish)
from ..steps.s9_package import approval_cfg
from ..steps.s9_publish import slot_time
from . import human
from ._common import RC_OK, RC_REFUSED, Refused, guarded, job_ctx, locked, print_json

DECISIONS = ("approve", "reject")
BY = "dialog"


def _items(job: Job) -> tuple[list[str], dict[str, Any]]:
    review = read_yaml(job.review_yaml, default=None)
    if not isinstance(review, dict) or not isinstance(review.get("items"), dict):
        raise Refused("no approval is open for this job (review.yaml is absent): run the pipeline "
                      "until it stops at S9")
    order = [it for it in series_items(job) if it in review["items"]]
    order += [it for it in review["items"] if it not in order]
    return order, review


def _card(job: Job, item: str, review: dict[str, Any], approvals: list[dict[str, Any]],
          uploads: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    pkg = read_package(job.package(item)) or {}
    r = review["items"][item]
    h = pkg.get("package_hash")
    meta = pkg.get("metadata") or {}
    entry = uploads.get(item) or {}
    dec = current_decision(approvals, item, h) if h else None
    stale = bool(h) and r.get("package_hash") != h
    return {
        "item": item, "kind": pkg.get("kind") or (info.get(item) or {}).get("kind"),
        "title": meta.get("title"), "description": meta.get("description"),
        "thesis": (info.get(item) or {}).get("thesis") or None,
        "thumbnail": str(job.thumb_png(item)) if job.thumb_png(item).exists() else None,
        "video_url": entry.get("url"), "video_id": pkg.get("video_id"),
        "privacy": "public" if entry.get("published") else "private",
        "review_status": "stale" if stale else r.get("status"),
        "violations": list(r.get("violations") or []), "notes": list(r.get("notes") or []),
        "package_hash": h,
        "decision": ({k: dec.get(k) for k in ("decision", "publish", "decided_at", "by")}
                     if dec else None),
        "offered": bool(h) and not stale and r.get("status") == "ok",
    }


def show(job: Job, as_json: bool) -> int:
    def body() -> int:
        order, review = _items(job)
        approvals = load_approval(job)
        uploads = load_uploads(job)["items"]
        info = fragment_info(job)
        cards = [_card(job, it, review, approvals, uploads, info) for it in order]
        human.mark_shown(job.approval_shown,
                         {c["item"]: c["package_hash"] for c in cards if c["package_hash"]},
                         "package_hash")
        slots = [s.get("label") for s in approval_cfg(job_ctx(job))["schedule_slots"]]
        pending = [c["item"] for c in cards if c["offered"] and not c["decision"]]
        out = {"job_id": job.job_id, "items": cards, "pending": pending, "schedule_slots": slots,
               "recommendation": (review.get("series") or {}).get("recommendation")}
        if as_json:
            print_json(out)
            return RC_OK
        print(f"approve {job.job_id}: {len(cards)} item(s), {len(pending)} waiting for a decision; "
              f"slots: {', '.join(str(s) for s in slots) or '-'}")
        for c in cards:
            dec = c["decision"]
            print(f"[{c['item']}] {c['kind']} review: {c['review_status']}"
                  + (f" ({', '.join(c['violations'])})" if c["violations"] else "")
                  + f"; decision: {dec['decision'] + ' ' + str(dec.get('publish') or '') if dec else '-'}")
            print(f"  title: {c['title']}")
            if c["thesis"]:
                print(f"  thesis: {c['thesis']}")
            print(f"  thumbnail: {c['thumbnail'] or '-'}")
            print(f"  private video: {c['video_url'] or '-'}")
            print(f"  package: {c['package_hash']}")
            desc = str(c["description"] or "")
            print("  description:")
            for line in desc.splitlines() or ["-"]:
                print(f"    {line}")
        return RC_OK
    return guarded(lambda: locked(job, body))


def parse_publish(value: str | None, cfg: dict[str, Any], now: str | None = None) -> Any:
    """now | slot:<label or index> | ISO time with offset (future) -> approval `publish` value."""
    now = now or now_iso()
    if value is None or not str(value).strip():
        raise Refused("approve needs --publish now | slot:<label> | <ISO time>")
    v = str(value).strip()
    if v == "now":
        return "now"
    if v.startswith("slot:"):
        key = v[5:].strip()
        slots = cfg["schedule_slots"]
        for k, s in enumerate(slots):
            if str(s.get("label")) == key or key == str(k):
                return {"at": slot_time(s, now, cfg["timezone"])}
        raise Refused(f"unknown slot {key!r}; slots: {', '.join(str(s.get('label')) for s in slots) or '-'}")
    try:
        dt = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
    except (ValueError, TypeError):
        raise Refused(f"--publish {v!r}: expected now, slot:<label> or an ISO time "
                      f"like 2026-11-20T10:00:00+03:00")
    if dt.tzinfo is None:
        raise Refused("the publish time needs a UTC offset, e.g. +03:00")
    if dt <= parse_iso(now):
        raise Refused("the publish time is in the past")
    return {"at": dt.astimezone(timezone.utc).isoformat(timespec="seconds")}


def set_decision(job: Job, item: str, decision: str, publish: str | None,
                 package_hash: str | None) -> int:
    def body() -> int:
        if decision not in DECISIONS:
            raise Refused(f"decision must be one of {', '.join(DECISIONS)}")
        if decision == "reject" and publish:
            raise Refused("--publish is only for approve")
        order, review = _items(job)
        if item not in review["items"]:
            raise Refused(f"unknown item {item!r}; items: {', '.join(order)}")
        pkg = read_package(job.package(item))
        if not pkg or not pkg.get("package_hash"):
            raise Refused(f"{item}: no package; run the pipeline first")
        current = pkg["package_hash"]
        r = review["items"][item]
        if r.get("package_hash") != current:
            raise Refused(f"{item}: the formal review is older than the package; run the pipeline, "
                          f"then `approve show` again")
        if r.get("status") != "ok":
            raise Refused(f"{item}: not offered for approval — violations: "
                          f"{', '.join(r.get('violations') or []) or '?'}")
        shown = read_yaml(job.approval_shown, default=None)
        seen = ((shown or {}).get("items") or {}).get(item) if isinstance(shown, dict) else None
        if not isinstance(seen, dict) or not seen.get("package_hash"):
            raise Refused(f"{item}: show the item to the owner first (`approve show <job>`)")
        if seen["package_hash"] != current:
            raise Refused(f"{item}: the package changed since it was shown "
                          f"({seen['package_hash'][:12]} -> {current[:12]}); run `approve show` "
                          f"again and get a new decision")
        if package_hash and not current.startswith(package_hash.strip()):
            raise Refused(f"{item}: --package-hash {package_hash[:12]} is not the current package "
                          f"{current[:12]}")
        at = now_iso()
        entry: dict[str, Any] = {"item": item, "package_hash": current, "decision": decision,
                                 "decided_at": at, "by": BY}
        if decision == "approve":
            entry["publish"] = validate_publish(parse_publish(publish, approval_cfg(job_ctx(job)), at))
        human.require_user_after([(item, seen.get("shown_at"))])
        append_approval(job, [entry])
        print(f"OK {item}: {decision}" + (f" publish {entry['publish']}" if "publish" in entry else "")
              + f" (package {current[:12]})")
        return RC_OK
    return guarded(lambda: locked(job, body))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vp approve", description="S9 publish approval in the dialog")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("job")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("job")
    s.add_argument("item")
    s.add_argument("--decision", required=True, choices=DECISIONS)
    s.add_argument("--publish")
    s.add_argument("--package-hash")
    return p


def main(argv: list[str]) -> int:
    a = build_parser().parse_args(argv)
    job = Job(a.job)
    if a.cmd == "show":
        return show(job, a.json)
    if a.cmd == "set":
        return set_decision(job, a.item, a.decision, a.publish, a.package_hash)
    return RC_REFUSED  # pragma: no cover
