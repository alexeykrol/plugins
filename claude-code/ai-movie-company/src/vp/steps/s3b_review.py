"""S3b — human review gate of proposed fragments (SPEC S3b).

Code writes `script.md` (proofread text in paragraphs: first subtitle number + hh:mm:ss, fragment
markers, S1 meaning-risk list) and a pre-filled `fragments.review.yaml` — only if that file is
absent (human edits are never overwritten). Then:

  * file not closed (some fragment without verdict)     -> WaitingHuman, one notification;
  * file invalid                                        -> fragments.errors.yaml + WaitingHuman
                                                           (the step stays waiting, not failed);
  * file closed and valid -> fragments.yaml (accepted + adjusted + added, normalized with the same
    rules as S3a, ids stable) and verdict lines appended to the knowledge store's verdicts.jsonl
    (idempotent: key = recording + fragment id + review file sha256).

Each new verdict carries `briefed` and `brief_sha` from the S3a manifest. Legacy lines without
these fields have unknown brief provenance.

Review file format (human-facing, see `review_template`):

    full_scan: true|false        # true = the human searched the whole recording for misses
    started_at: <iso>|null       # optional: human time (reported); else proxy from the gate
    finished_at: <iso>|null
    fragments:
    - id: r01
      verdict: accept|reject|adjust
      reason_class: weak_hook|no_closure|needs_context|boring|cut_mid_thought|wrong_length|
                    duplicate|off_topic|other          # optional
      reason: "..."                                     # required for reject and adjust
      start: "#412" | "1:24:20" | "quote"               # adjust only; a missing side is kept
      end: ...
    added:
    - {type: reel|long, start: ..., end: ..., reason: "...", title?: "...", reason_class?: ...}
    no_fragments_confirmed: true # only when there were no proposals and nothing is added
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..core.io import (atomic_write, dump_yaml, now_iso, parse_iso, read_json, read_yaml,
                       sha256_bytes, sha256_file, sha256_obj, write_yaml)
from ..core.manifest import read_manifest
from ..core.paths import Job
from ..core.srt import read_srt
from ..core.status import Status
from ..knowledge.store import KnowledgeStore, verdict_key
from . import _fragments as F
from .base import Ctx, StepSpec, VPError, WaitingHuman

GATE = "S3b.review"
VERDICTS = ("accept", "reject", "adjust")
REASON_CLASSES = ("weak_hook", "no_closure", "needs_context", "boring", "cut_mid_thought",
                  "wrong_length", "duplicate", "off_topic", "other")
PARA_GAP_MS = 2000
PARA_CHARS = 700

REVIEW_HEADER = """\
# Fragment review (S3b). Fill in a verdict for EVERY fragment, save the file, run the pipeline again.
#   verdict:      accept | reject | adjust
#   reason:       required for reject and adjust (free text: why)
#   reason_class: optional, one of: weak_hook no_closure needs_context boring cut_mid_thought
#                 wrong_length duplicate off_topic other
#   start / end:  only for adjust, any of: "#412" (subtitle number), "1:24:20" (time), or a quote
#                 copied from script.md; leave a side empty to keep the proposed boundary
#   added:        fragments the model missed: {type: reel|long, start, end, reason, title}
#   full_scan:    true if you searched the WHOLE recording for missed fragments
#   started_at / finished_at: optional, ISO time of your work (e.g. 2026-11-17T10:00:00+00:00)
# Text and subtitle numbers: script.md. Errors, if any, appear in fragments.errors.yaml.
"""


# ---- helpers ------------------------------------------------------------------------------------

def _iso(value: Any) -> str | None:
    """Normalize a human timestamp (string or YAML datetime) to ISO; None if empty.
    Raises ValueError if unparseable."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec="seconds")
    if isinstance(value, date):
        raise ValueError("a date without time")
    dt = parse_iso(str(value))
    return dt.isoformat(timespec="seconds")


def _num(fid: str) -> int:
    digits = "".join(ch for ch in fid if ch.isdigit())
    return int(digits) if digits else 0


FINGERPRINT_FIELDS = ("id", "type", "start_ms", "end_ms", "first_cue", "last_cue")


def selection_brief_sha(job: Job) -> str | None:
    """Fingerprint the brief used by S3a, even if request.yaml has since changed."""
    manifest = read_manifest(job, "S3a") or {}
    brief = (manifest.get("config") or {}).get("brief") or {}
    text = brief.get("text")
    return sha256_bytes(text)[:16] if isinstance(text, str) and text else None


def proposals_fingerprint(proposed: dict[str, Any]) -> str:
    """Hash of what the human actually judges: fragment ids, types and boundaries.

    A re-run of S3a that yields the same fragments at the same boundaries (e.g. after a
    proofreading fix changed only subtitle text) keeps a closed review valid: the owner is not
    asked to review the same segments twice. Any change of a boundary, type or id = new review.
    """
    return sha256_obj([{k: f.get(k) for k in FINGERPRINT_FIELDS}
                       for f in proposed.get("fragments") or []])


def review_template(proposed: dict[str, Any], proposed_sha: str, full_scan: bool) -> str:
    """Pre-filled review file text (header comment + YAML)."""
    frags = []
    for f in proposed.get("fragments") or []:
        frags.append({
            "id": f["id"], "type": f["type"], "title": f["title"],
            "time": f"{f.get('start') or F.clock(f['start_ms'])} – {f.get('end') or F.clock(f['end_ms'])}",
            "duration_s": round(f["duration_ms"] / 1000, 1),
            "subtitles": f"#{f['first_cue']} – #{f['last_cue']}",
            "score": f.get("score"), "thesis": f.get("thesis"), "why": f.get("why"),
            "start_quote": f.get("start_quote"), "end_quote": f.get("end_quote"),
            "verdict": None, "reason_class": None, "reason": None, "start": None, "end": None,
        })
    doc = {"job_id": proposed.get("job_id"), "knowledge_version": proposed.get("knowledge_version"),
           "proposed_sha256": proposed_sha, "proposed_fingerprint": proposals_fingerprint(proposed),
           "full_scan": bool(full_scan),
           "started_at": None, "finished_at": None, "fragments": frags, "added": []}
    header = REVIEW_HEADER
    if not frags:
        # nothing to give a verdict on: the human closes the gate explicitly (or adds fragments)
        doc["no_fragments_confirmed"] = False
        header += "# No proposals this time: add missed fragments, or set no_fragments_confirmed: true.\n"
    return header + dump_yaml(doc)


def build_script(cues, proposed: dict[str, Any], meaning_risk: list[Any], job_id: str) -> str:
    """script.md: fragment table, S1 meaning-risk list, text in paragraphs with markers."""
    frags = proposed.get("fragments") or []
    starts: dict[str, list[str]] = {}
    ends: dict[str, list[str]] = {}
    for f in frags:
        starts.setdefault(str(f["first_cue"]), []).append(f"▶ **{f['id']}** {f['type']} «{f['title']}» starts")
        ends.setdefault(str(f["last_cue"]), []).append(f"■ **{f['id']}** ends")
    summary = ("Whole recording" if proposed.get("whole_recording") else
               f"Knowledge version {proposed.get('knowledge_version')} · boundary mode "
               f"{proposed.get('boundary_mode')} · {len(frags)} proposals")
    out = [f"# Script — {job_id}", "", summary, ""]
    if frags:
        out += ["## Proposed fragments", "", "| id | type | time | subtitles | duration | title |",
                "|---|---|---|---|---|---|"]
        for f in frags:
            out.append(f"| {f['id']} | {f['type']} | {F.clock(f['start_ms'])} – {F.clock(f['end_ms'])} | "
                       f"#{f['first_cue']} – #{f['last_cue']} | {f['duration_ms'] / 1000:.0f} s | "
                       f"{str(f['title']).replace('|', '/')} |")
        out.append("")
    if meaning_risk:
        out += ["## Meaning risk after proofreading (S1) — please check", ""]
        for r in meaning_risk:
            if isinstance(r, dict):
                cid = r.get("id") or r.get("cue") or "?"
                rest = "; ".join(f"{k}: {v}" for k, v in r.items() if k not in ("id", "cue"))
                out.append(f"- #{cid}: {rest}")
            else:
                out.append(f"- {r}")
        out.append("")
    out += ["## Text", ""]
    state: dict[str, Any] = {"para": [], "count": 0, "head": "", "force": True}

    def flush() -> None:
        if state["para"]:
            out.extend([state["head"], " ".join(state["para"]), ""])
        state["para"], state["count"] = [], 0

    prev = None
    for c in cues:
        text = " ".join(c.text.split())
        if not text:
            continue
        if (state["force"] or prev is None or c.start_ms - prev.end_ms >= PARA_GAP_MS
                or (c.speaker and prev.speaker and c.speaker != prev.speaker)
                or (state["count"] >= PARA_CHARS and prev.text.rstrip()[-1:] in ".!?…")
                or c.id in starts):
            flush()
            out.extend(f"> {m}" for m in starts.get(c.id, []))
            state["head"], state["force"] = f"**#{c.id} · {F.clock(c.start_ms)}**", False
        state["para"].append(text)
        state["count"] += len(text)
        if c.id in ends:
            flush()
            out.extend(f"> {m}" for m in ends[c.id])
            out.append("")
            state["force"] = True
        prev = c
    flush()
    return "\n".join(out).rstrip() + "\n"


class ReviewResult:
    def __init__(self):
        self.errors: list[dict[str, str]] = []
        self.open: list[str] = []            # fragment ids without verdict
        self.final: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self.full_scan = False
        self.started_at: str | None = None
        self.finished_at: str | None = None

    def err(self, where: str, msg: str) -> None:
        self.errors.append({"where": where, "message": msg})


def check_review(doc: Any, proposed: dict[str, Any], proposed_sha: str, timeline: F.Timeline,
                 profile: dict[str, Any]) -> ReviewResult:
    """Validate a review document and compute the final fragments (pure, no I/O)."""
    res = ReviewResult()
    if not isinstance(doc, dict):
        res.err("file", "the review file must be a YAML mapping")
        return res
    if (doc.get("proposed_sha256") and doc["proposed_sha256"] != proposed_sha
            and doc.get("proposed_fingerprint") != proposals_fingerprint(proposed)):
        res.err("file", "this review was prepared for another set of proposals (S3a ran again); "
                        "move the file away and run the pipeline to get a fresh one")
        return res
    fs = doc.get("full_scan", False)
    if not isinstance(fs, bool):
        res.err("full_scan", "must be true or false")
    else:
        res.full_scan = fs
    for k in ("started_at", "finished_at"):
        try:
            setattr(res, k, _iso(doc.get(k)))
        except (ValueError, TypeError):
            res.err(k, "not an ISO time, e.g. 2026-11-17T10:00:00+00:00")
    if res.started_at and res.finished_at and parse_iso(res.finished_at) < parse_iso(res.started_at):
        res.err("finished_at", "is before started_at")

    prop = {str(f["id"]): f for f in proposed.get("fragments") or []}
    items = doc.get("fragments")
    if items is None:
        items = []
    if not isinstance(items, list):
        res.err("fragments", "must be a list")
        items = []
    seen: set[str] = set()
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict) or "id" not in it:
            res.err(f"fragments[{i}]", "each entry needs an id")
            continue
        fid = str(it["id"])
        where = f"fragment {fid}"
        if fid not in prop:
            res.err(where, "unknown fragment id (not in fragments.proposed.yaml)")
            continue
        if fid in seen:
            res.err(where, "listed twice")
            continue
        seen.add(fid)
        p = prop[fid]
        verdict = it.get("verdict")
        if verdict in (None, ""):
            res.open.append(fid)
            continue
        if verdict not in VERDICTS:
            res.err(where, f"verdict must be one of {', '.join(VERDICTS)}")
            continue
        rc = it.get("reason_class")
        if rc not in (None, "") and rc not in REASON_CLASSES:
            res.err(where, f"reason_class must be one of {', '.join(REASON_CLASSES)}")
        reason = it.get("reason")
        if verdict in ("reject", "adjust") and not (isinstance(reason, str) and reason.strip()):
            res.err(where, f"reason is required for {verdict}")
        start, end = it.get("start"), it.get("end")
        start = None if start in ("", None) else start
        end = None if end in ("", None) else end
        final = None
        if verdict == "adjust":
            if start is None and end is None:
                res.err(where, "adjust needs a new start and/or end")
                continue
            try:
                final = timeline.pair(start, end, base=p)
            except F.BoundaryError as e:
                res.err(where, e.msg)
                continue
        elif start is not None or end is not None:
            res.err(where, "start/end are only for verdict adjust")
            continue
        elif verdict == "accept":
            final = {k: p[k] for k in ("start_ms", "end_ms", "duration_ms", "boundary", "first_cue",
                                      "last_cue", "text")}
        rec = {"fragment_id": fid, "type": p["type"], "verdict": verdict,
               "reason_class": rc or None, "reason": reason.strip() if isinstance(reason, str) else None,
               "proposed": {k: p.get(k) for k in ("start_ms", "end_ms", "first_cue", "last_cue",
                                                 "start_quote", "end_quote", "title", "thesis", "why",
                                                 "score", "applied")},
               "final": None, "text": p.get("text")}
        if final is not None:
            ff = {"id": fid, "type": p["type"], "origin": "adjusted" if verdict == "adjust" else "proposed",
                  "title": p["title"], "thesis": p.get("thesis"), **final}
            res.final.append(ff)
            rec["final"] = {k: final[k] for k in ("start_ms", "end_ms", "first_cue", "last_cue")}
            rec["text"] = final["text"]
            if verdict == "adjust":
                rec["shift_ms"] = {"start": final["start_ms"] - p["start_ms"],
                                   "end": final["end_ms"] - p["end_ms"]}
        res.records.append(rec)
    for fid in prop:
        if fid not in seen and fid not in res.open:
            res.open.append(fid)
    confirmed = doc.get("no_fragments_confirmed", False)
    if not isinstance(confirmed, bool):
        res.err("no_fragments_confirmed", "must be true or false")

    added = doc.get("added")
    if added is None:
        added = []
    if not isinstance(added, list):
        res.err("added", "must be a list")
        added = []
    next_num = {k: max([_num(f) for f, p in prop.items() if p["type"] == k] or [0]) for k in F.TYPES}
    for i, a in enumerate(added, 1):
        where = f"added[{i}]"
        if not isinstance(a, dict):
            res.err(where, "must be a mapping {type, start, end, reason}")
            continue
        kind = a.get("type")
        if kind not in F.TYPES:
            res.err(where, "type must be reel or long")
            continue
        next_num[kind] += 1     # numbering by position keeps ids stable while the list only grows
        fid = f"{F.TYPE_PREFIX[kind]}{next_num[kind]:02d}"
        if a.get("start") in (None, "") or a.get("end") in (None, ""):
            res.err(where, "start and end are required")
            continue
        if not (isinstance(a.get("reason"), str) and a["reason"].strip()):
            res.err(where, "reason is required (why this place is strong)")
        rc = a.get("reason_class")
        if rc not in (None, "") and rc not in REASON_CLASSES:
            res.err(where, f"reason_class must be one of {', '.join(REASON_CLASSES)}")
        try:
            final = timeline.pair(a["start"], a["end"])
        except F.BoundaryError as e:
            res.err(where, e.msg)
            continue
        title = str(a.get("title") or a.get("reason") or fid).strip()[:120]
        res.final.append({"id": fid, "type": kind, "origin": "added", "title": title,
                          "thesis": a.get("thesis"), **final})
        res.records.append({"fragment_id": fid, "type": kind, "verdict": "added",
                            "reason_class": rc or None, "reason": str(a.get("reason") or "").strip(),
                            "proposed": None,
                            "final": {k: final[k] for k in ("start_ms", "end_ms", "first_cue", "last_cue")},
                            "text": final["text"]})

    if not prop and not added and confirmed is not True:
        res.open.append("no_fragments_confirmed")
    for f in res.final:
        rng = F.duration_range(profile, f["type"], human=True)
        if not F.in_range(f["duration_ms"], rng):
            res.err(f"fragment {f['id']}", f"duration {f['duration_ms'] / 1000:.1f}s is outside "
                                           f"{rng[0] / 1000:g}-{rng[1] / 1000:g}s for {f['type']}")
    for a, b, share in F.overlapping_pairs(res.final):
        res.err(f"fragments {a} and {b}", f"overlap {share:.0%} > {F.OVERLAP_MAX:.0%} within one type")
    res.final.sort(key=lambda f: (f["start_ms"], f["id"]))
    return res


class S3bReview(StepSpec):
    id, deps, scope, version, gate = "S3b", ("S3a",), "job", "1", "human"

    def inputs(self, ctx: Ctx) -> list[Path]:
        j = ctx.job
        paths = [j.fragments_proposed, j.fragments_review, j.subtitles_srt, j.source_words,
                 j.job_yaml, j.s1_report]
        if ctx.config.get("whole_recording") == "reel":
            paths.append(j.request_yaml)
        return paths

    def config(self, ctx: Ctx) -> dict:
        if ctx.config.get("whole_recording"):
            return {"whole_recording": ctx.config["whole_recording"]}
        p = ctx.profile
        return {"reel": p.get("reel"), "long": p.get("long"),
                "selection_full_scan": bool(p.get("selection_full_scan", True))}

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.fragments_yaml, ctx.job.script_md]

    def _notify_once(self, ctx: Ctx, text: str) -> None:
        if Status(ctx.job).open_wait(self.id, None) is not None:
            return
        try:
            ctx.retry(lambda: ctx.notifier.send(text))
        except VPError as e:
            if e.kind == "auth":
                raise
            ctx.log.event("s3b_notify_failed", kind=e.kind)

    def _wait(self, ctx: Ctx, full_scan: bool, what: str, n: int) -> WaitingHuman:
        self._notify_once(ctx, f"vp: {ctx.job.job_id}: {n} fragments proposed — review "
                               f"{ctx.job.fragments_review} (text: {ctx.job.script_md.name})")
        return WaitingHuman(GATE, what, kind="full_scan" if full_scan else "review")

    def run(self, ctx: Ctx) -> dict:
        job = ctx.job
        report = read_json(job.s1_report, default={}) or {}
        risk = report.get("meaning_risk") if isinstance(report, dict) else None
        risk = risk if isinstance(risk, list) else []
        whole = ctx.config.get("whole_recording")
        if whole:
            duration = F._video_end_ms(job)
            if not duration:
                raise VPError("data", "the recording duration is missing from intake (S0)")
            span = {"start_ms": 0, "end_ms": duration, "duration_ms": duration}
            fragments = []
            if whole == "reel":
                request = job.request()
                fragments.append({"id": "r01", "type": "reel", "origin": "whole_recording",
                                  "title": str(request.get("title") or ""),
                                  "thesis": str(request.get("thesis") or ""), **span})
            atomic_write(job.script_md, build_script(read_srt(job.subtitles_srt),
                                                     {"whole_recording": whole}, risk, job.job_id))
            write_yaml(job.fragments_yaml, {
                "job_id": job.job_id, "fragments": fragments, "whole_recording": {"kind": whole, **span},
            })
            return {"fragments": len(fragments), "whole_recording": whole}
        proposed = read_yaml(job.fragments_proposed, default=None)
        if not isinstance(proposed, dict) or "fragments" not in proposed:
            raise VPError("data", "fragments.proposed.yaml missing or malformed")
        proposed_sha = sha256_file(job.fragments_proposed)
        timeline = F.load_timeline(job, proposed.get("boundary_mode"))
        atomic_write(job.script_md, build_script(timeline.cues, proposed, risk, job.job_id))
        default_full_scan = bool(ctx.profile.get("selection_full_scan", True))
        n = len(proposed.get("fragments") or [])
        if not job.fragments_review.exists():
            atomic_write(job.fragments_review, review_template(proposed, proposed_sha, default_full_scan))
            raise self._wait(ctx, default_full_scan, f"fill in {job.fragments_review.name}", n)

        raw = job.fragments_review.read_bytes()
        review_sha = sha256_bytes(raw)
        try:
            doc = yaml.safe_load(raw.decode("utf-8"))
            res = check_review(doc, proposed, proposed_sha, timeline, ctx.profile)
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            res = ReviewResult()
            res.err("file", f"not valid YAML: {str(e)[:300]}")
        full_scan = res.full_scan if not res.errors else default_full_scan
        if res.errors:
            write_yaml(job.fragments_errors, {"review_sha256": review_sha, "at": now_iso(),
                                              "errors": res.errors})
            raise self._wait(ctx, full_scan, f"fix {job.fragments_review.name} "
                                             f"({len(res.errors)} errors in {job.fragments_errors.name})", n)
        if job.fragments_errors.exists():
            job.fragments_errors.unlink()
        if res.open:
            raise self._wait(ctx, full_scan, f"{len(res.open)} fragments without verdict in "
                                             f"{job.fragments_review.name}", n)

        # closed: fragments.yaml + verdicts
        brief_sha = selection_brief_sha(job)
        fragments = [{**{k: f[k] for k in ("id", "type", "origin", "start_ms", "end_ms", "duration_ms",
                                          "boundary", "first_cue", "last_cue", "title", "thesis", "text")},
                      "start": F.clock(f["start_ms"]), "end": F.clock(f["end_ms"])} for f in res.final]
        write_yaml(job.fragments_yaml, {
            "job_id": job.job_id, "knowledge_version": proposed.get("knowledge_version"),
            "boundary_mode": timeline.mode, "review_sha256": review_sha,
            "proposed_sha256": proposed_sha, "full_scan": res.full_scan, "fragments": fragments})
        kv = proposed.get("knowledge_version")
        recs = []
        for r in res.records:
            recs.append({"key": verdict_key(job.job_id, r["fragment_id"], review_sha),
                         "recording_id": job.job_id, "job_id": job.job_id, **r,
                         "knowledge_version": kv, "knowledge_hash": proposed.get("knowledge_hash"),
                         "boundary_mode": timeline.mode, "full_scan": res.full_scan,
                         "briefed": brief_sha is not None, "brief_sha": brief_sha,
                         "review_sha256": review_sha, "recorded_at": now_iso()})
        added_lines = KnowledgeStore(ctx.home).append_verdicts(recs)
        counts = {v: sum(1 for r in res.records if r["verdict"] == v)
                  for v in ("accept", "reject", "adjust", "added")}
        meta: dict[str, Any] = {"fragments": len(fragments), "verdicts": counts,
                                "verdict_lines_appended": added_lines, "knowledge_version": kv,
                                "full_scan": res.full_scan}
        kind = "full_scan" if res.full_scan else "review"
        if res.started_at and res.finished_at:
            meta["human_time_reported"] = {"opened_at": res.started_at, "closed_at": res.finished_at,
                                           "kind": kind}
        else:
            mtime = datetime.fromtimestamp(job.fragments_review.stat().st_mtime, tz=timezone.utc)
            meta["gate_closed_at"] = mtime.isoformat(timespec="seconds")
        return meta


STEP = S3bReview()
