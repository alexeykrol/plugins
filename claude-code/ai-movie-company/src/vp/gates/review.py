"""`review ...` — the S3b fragment review in the dialog (UX v1, gate 1).

  review show <job> [--json]                 candidates + current verdicts; the first call stamps
                                             started_at (human time of the review)
  review preview <job> <frag>                low-res preview.mp4 of a fragment (current boundaries)
  review set <job> <frag> --verdict accept|reject|adjust [--reason R] [--reason-class C]
                                             [--start B] [--end B]
  review add <job> --type reel|long --start B --end B --reason R [--title T] [--reason-class C]
  review close <job> [--full-scan|--no-full-scan] [--no-fragments-confirmed]
                                             stamps finished_at; refuses while anything is open
                                             or invalid

`show` and `preview` stamp per-fragment `shown_at` in fragments.review.shown.yaml; `set` needs a
real user input after that fragment was shown, `add` and `close` after the last show of the review
(vp.gates.human; exit 4 otherwise). Editing fragments.review.yaml by hand is not guarded.

Every write goes through `s3b_review.check_review` — the exact validation of the file path. A
change that adds a validation error is refused and nothing is written. The file keeps the human
format (header comment + YAML), so the owner can still edit it by hand.
"""
from __future__ import annotations

import argparse
import copy
from pathlib import Path
from typing import Any

import yaml

from ..adapters import media
from ..core.io import atomic_write, dump_yaml, now_iso, read_json, read_yaml, sha256_file, write_json
from ..core.lock import job_lock
from ..core.paths import Job
from ..steps import _fragments as F
from ..steps.s3b_review import REASON_CLASSES, REVIEW_HEADER, VERDICTS, ReviewResult, check_review
from . import human
from ._common import RC_OK, RC_REFUSED, Refused, guarded, job_ctx, locked, print_json

PREVIEW_HEIGHT = 360


# ---- loading ------------------------------------------------------------------------------------

class ReviewState:
    """The open review of a job: the review document + everything check_review needs."""

    def __init__(self, job: Job):
        self.job = job
        if not job.fragments_review.exists():
            raise Refused(f"no review is open for this job ({job.fragments_review.name} is absent): "
                          f"run the pipeline until it stops at S3b")
        proposed = read_yaml(job.fragments_proposed, default=None)
        if not isinstance(proposed, dict) or "fragments" not in proposed:
            raise Refused("fragments.proposed.yaml missing or malformed: run the pipeline first")
        self.proposed = proposed
        self.proposed_sha = sha256_file(job.fragments_proposed)
        try:
            doc = yaml.safe_load(job.fragments_review.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            raise Refused(f"{job.fragments_review.name} is not valid YAML; fix it by hand first "
                          f"({str(e)[:200]})")
        if not isinstance(doc, dict):
            raise Refused(f"{job.fragments_review.name} must be a YAML mapping")
        self.doc = doc
        self.timeline = F.load_timeline(job, proposed.get("boundary_mode"))
        self.profile = job_ctx(job).profile
        self.by_id = {str(f["id"]): f for f in proposed.get("fragments") or []}

    def check(self, doc: dict[str, Any] | None = None) -> ReviewResult:
        return check_review(doc if doc is not None else self.doc, self.proposed, self.proposed_sha,
                            self.timeline, self.profile)

    def save(self, doc: dict[str, Any]) -> None:
        atomic_write(self.job.fragments_review, REVIEW_HEADER + dump_yaml(doc))
        self.doc = doc

    def entry(self, doc: dict[str, Any], fid: str) -> dict[str, Any]:
        for it in doc.get("fragments") or []:
            if isinstance(it, dict) and str(it.get("id")) == fid:
                return it
        if fid in self.by_id:      # proposal missing from the file (hand edit): add it back
            p = self.by_id[fid]
            it = {"id": fid, "type": p["type"], "title": p.get("title"), "verdict": None}
            doc.setdefault("fragments", []).append(it)
            return it
        raise Refused(f"unknown fragment id {fid!r}; known: {', '.join(self.by_id) or '-'}")

    def change(self, doc: dict[str, Any]) -> ReviewResult:
        """Refuse `doc` if it has validation errors the current file does not have."""
        before = {(e["where"], e["message"]) for e in self.check().errors}
        res = self.check(doc)
        new = [e for e in res.errors if (e["where"], e["message"]) not in before]
        if new:
            raise Refused("the change is invalid; nothing was written", new)
        return res


def _cue_text(timeline: F.Timeline, cue_id: Any) -> str:
    for c in timeline.cues:
        if str(c.id) == str(cue_id):
            return " ".join(c.text.split())
    return ""


def _sentence(text: str, last: bool) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    marks = [i for i, ch in enumerate(text) if ch in ".!?…"]
    if last:
        cut = [i for i in marks if i < len(text) - 1]
        out = text[cut[-1] + 1:].strip() if cut else text
        return out[-300:]
    out = text[:marks[0] + 1] if marks else text
    return out[:300]


def _effective(state: ReviewState, res: ReviewResult) -> dict[str, dict[str, Any]]:
    """id -> final boundaries for every fragment that currently has them (accepted / adjusted /
    added); proposals without a verdict keep the proposed boundaries."""
    out = {f["id"]: f for f in res.final}
    for fid, p in state.by_id.items():
        out.setdefault(fid, p)
    return out


def _range_key(eff: dict[str, Any]) -> str:
    """Content key of a fragment for the shown record: its current boundaries."""
    return f"{int(eff['start_ms'])}-{int(eff['end_ms'])}"


def _card(state: ReviewState, fid: str, item: dict[str, Any], eff: dict[str, Any]) -> dict[str, Any]:
    p = state.by_id.get(fid) or {}
    start_ms, end_ms = int(eff["start_ms"]), int(eff["end_ms"])
    moved = bool(p) and (start_ms, end_ms) != (int(p["start_ms"]), int(p["end_ms"]))
    if p and not moved:
        first = p.get("start_quote") or _cue_text(state.timeline, p.get("first_cue"))
        last = p.get("end_quote") or _cue_text(state.timeline, p.get("last_cue"))
    else:
        first = _cue_text(state.timeline, eff.get("first_cue"))
        last = _cue_text(state.timeline, eff.get("last_cue"))
    prev = state.job.preview_mp4(fid)
    meta = read_json(prev.with_suffix(".json"), default={}) or {}
    fresh = prev.exists() and (meta.get("start_ms"), meta.get("end_ms")) == (start_ms, end_ms)
    return {
        "id": fid, "type": item.get("type") or p.get("type"),
        "origin": "added" if not p else ("adjusted" if moved else "proposed"),
        "duration_s": round((end_ms - start_ms) / 1000, 1),
        "start": F.clock(start_ms), "end": F.clock(end_ms),
        "subtitles": f"#{eff.get('first_cue')} - #{eff.get('last_cue')}",
        "first_cue": eff.get("first_cue"), "last_cue": eff.get("last_cue"),
        "title": item.get("title") or p.get("title"), "thesis": item.get("thesis") or p.get("thesis"),
        "why": p.get("why"), "score": p.get("score"),
        "first_sentence": _sentence(first, last=False), "last_sentence": _sentence(last, last=True),
        "preview": str(prev) if fresh else None,
        "verdict": item.get("verdict"), "reason": item.get("reason"),
        "reason_class": item.get("reason_class"),
    }


# ---- commands -----------------------------------------------------------------------------------

def show(job: Job, as_json: bool) -> int:
    def body() -> int:
        state = ReviewState(job)
        if not state.doc.get("started_at"):
            # the human review starts now (reported human time); skipped while a runner is active
            with job_lock(job) as got:
                if got:
                    state = ReviewState(job)
                    doc = copy.deepcopy(state.doc)
                    doc["started_at"] = now_iso()
                    state.save(doc)
        res = state.check()
        eff = _effective(state, res)
        cards = []
        keys: dict[str, str] = {}
        for it in state.doc.get("fragments") or []:
            if isinstance(it, dict) and str(it.get("id")) in state.by_id:
                fid = str(it["id"])
                cards.append(_card(state, fid, it, eff.get(fid) or state.by_id[fid]))
                keys[fid] = _range_key(eff.get(fid) or state.by_id[fid])
        added = []
        for r in res.records:
            if r["verdict"] == "added" and r["fragment_id"] in eff:
                f = eff[r["fragment_id"]]
                keys[r["fragment_id"]] = _range_key(f)
                added.append(_card(state, r["fragment_id"],
                                   {"type": f["type"], "title": f.get("title"), "verdict": "added",
                                    "reason": r.get("reason"), "reason_class": r.get("reason_class")},
                                   f))
        counts = {"total": len(cards),
                  "reel": sum(1 for c in cards if c["type"] == "reel"),
                  "long": sum(1 for c in cards if c["type"] == "long"),
                  "decided": sum(1 for c in cards if c["verdict"]),
                  "added": len(added)}
        human.mark_shown(job.review_shown, keys, "range")
        out = {"job_id": job.job_id, "review_file": str(job.fragments_review),
               "script": str(job.script_md), "knowledge_version": state.proposed.get("knowledge_version"),
               "full_scan": state.doc.get("full_scan"), "started_at": state.doc.get("started_at"),
               "finished_at": state.doc.get("finished_at"), "counts": counts,
               "open": [o for o in res.open], "errors": list(res.errors),
               "candidates": cards, "added": added}
        if as_json:
            print_json(out)
            return RC_OK
        print(f"review {job.job_id}: {counts['total']} candidates ({counts['reel']} reel, "
              f"{counts['long']} long), decided {counts['decided']}, added {counts['added']}, "
              f"open {len(res.open)}; knowledge {out['knowledge_version']}")
        print(f"script: {job.script_md}")
        for c in cards + added:
            print(f"[{c['id']}] {c['type']} {c['duration_s']} s ({c['start']} - {c['end']}, "
                  f"{c['subtitles']}) {c['origin']}; verdict: {c['verdict'] or '-'}"
                  + (f" ({c['reason_class'] or '-'}: {c['reason']})" if c['reason'] else ""))
            for k in ("title", "thesis", "why"):
                if c.get(k):
                    print(f"  {k}: {c[k]}")
            print(f"  first: {c['first_sentence']}")
            print(f"  last: {c['last_sentence']}")
            print(f"  preview: {c['preview'] or '(none: review preview <job> ' + c['id'] + ')'}")
        for e in res.errors:
            print(f"ERROR {e['where']}: {e['message']}")
        return RC_OK
    return guarded(body)


def preview(job: Job, fid: str) -> int:
    def body() -> int:
        state = ReviewState(job)
        res = state.check()
        eff = _effective(state, res).get(fid)
        if eff is None:
            raise Refused(f"unknown fragment id {fid!r}")
        start_ms, end_ms = int(eff["start_ms"]), int(eff["end_ms"])
        out = job.preview_mp4(fid)
        side = out.with_suffix(".json")
        meta = read_json(side, default={}) or {}
        if not (out.exists() and (meta.get("start_ms"), meta.get("end_ms")) == (start_ms, end_ms)):
            if not media.available():
                raise Refused("ffmpeg/ffprobe not found on PATH")
            from ..core import progress
            with progress.tracking(job.root, "preview", fid, total=(end_ms - start_ms) / 1000,
                                   unit="s"):
                render_preview(job.source_video, start_ms, end_ms, out)
            write_json(side, {"start_ms": start_ms, "end_ms": end_ms, "rendered_at": now_iso(),
                              "mode": preview_mode(start_ms, end_ms)})
        human.mark_shown(job.review_shown, {fid: _range_key(eff)}, "range")
        print(str(out))
        return RC_OK
    return guarded(body)


# Re-encoding to 360p runs ~65x realtime on the owner's machine (measured 2026-11-17: 0.7 s for a
# 32 s reel, 3.0 s for a 193 s long fragment). Above this length the preview is a stream copy
# (instant, starts at the keyframe before the start — fine for watching; S4 cuts exactly).
PREVIEW_COPY_ABOVE_MS = 300_000


def preview_mode(start_ms: int, end_ms: int) -> str:
    return "copy" if end_ms - start_ms > PREVIEW_COPY_ABOVE_MS else "encode"


def render_preview(src: Path, start_ms: int, end_ms: int, out: Path) -> Path:
    """Fast preview for watching, never for publishing: 360p re-encode for short fragments,
    keyframe-snapped stream copy for long ones (preview_mode)."""
    if end_ms <= start_ms:
        raise Refused(f"empty fragment range {start_ms}..{end_ms}")
    head = ["-ss", f"{start_ms / 1000:.3f}", "-i", str(src), "-t", f"{(end_ms - start_ms) / 1000:.3f}",
            "-map", "0:v:0", "-map", "0:a:0?"]
    if preview_mode(start_ms, end_ms) == "copy":
        return media._render_to(out, [*head, "-c", "copy", "-avoid_negative_ts", "make_zero",
                                      "-movflags", "+faststart"])
    return media._render_to(out, [*head, "-vf", f"scale=-2:{PREVIEW_HEIGHT}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart"])


def _clean(v: str | None) -> str | None:
    return None if v is None or not str(v).strip() else str(v).strip()


def set_verdict(job: Job, fid: str, verdict: str, reason: str | None, reason_class: str | None,
                start: str | None, end: str | None) -> int:
    reason, reason_class, start, end = map(_clean, (reason, reason_class, start, end))

    def body() -> int:
        if verdict not in VERDICTS:
            raise Refused(f"verdict must be one of {', '.join(VERDICTS)}")
        if reason_class is not None and reason_class not in REASON_CLASSES:
            raise Refused(f"reason_class must be one of {', '.join(REASON_CLASSES)}")
        if verdict in ("reject", "adjust") and reason is None:
            raise Refused(f"a reason is required for {verdict}")
        if verdict != "adjust" and (start is not None or end is not None):
            raise Refused("--start/--end are only for verdict adjust")
        if verdict == "adjust" and start is None and end is None:
            raise Refused("adjust needs --start and/or --end")
        state = ReviewState(job)
        doc = copy.deepcopy(state.doc)
        it = state.entry(doc, fid)
        current = _effective(state, state.check()).get(fid)
        it.update(verdict=verdict, reason=reason, reason_class=reason_class,
                  start=start if verdict == "adjust" else None,
                  end=end if verdict == "adjust" else None)
        if not doc.get("started_at"):
            doc["started_at"] = now_iso()
        res = state.change(doc)
        seen = human.load_shown(job.review_shown)["items"].get(fid)
        shown_at = (seen.get("shown_at") if isinstance(seen, dict) and current is not None
                    and seen.get("range") == _range_key(current) else None)
        human.require_user_after([(fid, shown_at)])
        state.save(doc)
        f = next((x for x in res.final if x["id"] == fid), None)
        where = (f" ({F.clock(f['start_ms'])} - {F.clock(f['end_ms'])}, "
                 f"{f['duration_ms'] / 1000:.1f} s)" if f else "")
        print(f"OK {fid}: {verdict}{where}; open {len(res.open)}")
        return RC_OK
    return guarded(lambda: locked(job, body))


def add(job: Job, kind: str, start: str, end: str, reason: str, title: str | None,
        reason_class: str | None) -> int:
    reason, title, reason_class, start, end = map(_clean, (reason, title, reason_class, start, end))

    def body() -> int:
        if kind not in F.TYPES:
            raise Refused("type must be reel or long")
        if start is None or end is None:
            raise Refused("--start and --end are required")
        if reason is None:
            raise Refused("a reason is required (why this place is strong)")
        if reason_class is not None and reason_class not in REASON_CLASSES:
            raise Refused(f"reason_class must be one of {', '.join(REASON_CLASSES)}")
        state = ReviewState(job)
        doc = copy.deepcopy(state.doc)
        entry: dict[str, Any] = {"type": kind, "start": start, "end": end, "reason": reason}
        if title:
            entry["title"] = title
        if reason_class:
            entry["reason_class"] = reason_class
        added = doc.get("added")
        doc["added"] = (list(added) if isinstance(added, list) else []) + [entry]
        if not doc.get("started_at"):
            doc["started_at"] = now_iso()
        res = state.change(doc)
        human.require_user_after([("review", human.load_shown(job.review_shown).get("last_shown_at"))])
        state.save(doc)
        rec = [r for r in res.records if r["verdict"] == "added"][-1]
        f = next(x for x in res.final if x["id"] == rec["fragment_id"])
        print(f"OK added {rec['fragment_id']}: {kind} ({F.clock(f['start_ms'])} - "
              f"{F.clock(f['end_ms'])}, {f['duration_ms'] / 1000:.1f} s)")
        return RC_OK
    return guarded(lambda: locked(job, body))


def close(job: Job, full_scan: bool | None, no_fragments_confirmed: bool) -> int:
    def body() -> int:
        state = ReviewState(job)
        doc = copy.deepcopy(state.doc)
        if full_scan is None:
            # The template's default is not the user's answer: whether the whole recording was
            # searched for missed fragments is a claim only the user can make (it feeds learning).
            raise Refused("say whether the user searched the whole recording: ask the user, then "
                          "pass --full-scan or --no-full-scan; nothing was written")
        doc["full_scan"] = full_scan
        if no_fragments_confirmed:
            doc["no_fragments_confirmed"] = True
        doc["finished_at"] = now_iso()
        res = state.check(doc)
        if res.errors:
            raise Refused(f"the review has {len(res.errors)} error(s); nothing was written",
                          res.errors)
        if res.open:
            raise Refused(f"still open: {', '.join(res.open)}; nothing was written")
        human.require_user_after([("review", human.load_shown(job.review_shown).get("last_shown_at"))])
        state.save(doc)
        counts = {v: sum(1 for r in res.records if r["verdict"] == v)
                  for v in ("accept", "reject", "adjust", "added")}
        print(f"CLOSED {job.job_id}: {counts['accept']} accepted, {counts['reject']} rejected, "
              f"{counts['adjust']} adjusted, {counts['added']} added; {len(res.final)} fragment(s) "
              f"go to production; full_scan: {bool(doc.get('full_scan'))}. Run the pipeline to continue.")
        return RC_OK
    return guarded(lambda: locked(job, body))


# ---- CLI ----------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vp review", description="S3b fragment review in the dialog")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("job")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("preview")
    s.add_argument("job")
    s.add_argument("frag")
    s = sub.add_parser("set")
    s.add_argument("job")
    s.add_argument("frag")
    s.add_argument("--verdict", required=True, choices=VERDICTS)
    s.add_argument("--reason")
    s.add_argument("--reason-class", choices=REASON_CLASSES)
    s.add_argument("--start")
    s.add_argument("--end")
    s = sub.add_parser("add")
    s.add_argument("job")
    s.add_argument("--type", required=True, choices=F.TYPES, dest="kind")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--title")
    s.add_argument("--reason-class", choices=REASON_CLASSES)
    s = sub.add_parser("close")
    s.add_argument("job")
    s.add_argument("--full-scan", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--no-fragments-confirmed", action="store_true")
    return p


def main(argv: list[str]) -> int:
    a = build_parser().parse_args(argv)
    job = Job(a.job)
    if a.cmd == "show":
        return show(job, a.json)
    if a.cmd == "preview":
        return preview(job, a.frag)
    if a.cmd == "set":
        return set_verdict(job, a.frag, a.verdict, a.reason, a.reason_class, a.start, a.end)
    if a.cmd == "add":
        return add(job, a.kind, a.start, a.end, a.reason, a.title, a.reason_class)
    if a.cmd == "close":
        return close(job, a.full_scan, a.no_fragments_confirmed)
    return RC_REFUSED  # pragma: no cover
