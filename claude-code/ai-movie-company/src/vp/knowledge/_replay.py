"""S3c replay (SPEC S3c п. 4): recordings archive, verdict places, measurement of a version.

Recordings archive `<vp_home>/selection/recordings/<slug>/` (written by `learn snapshot <job>`):
    subtitles.srt    proofread subtitles of the recording as reviewed
    words.json       word timings (only for boundary mode `words`)
    recording.yaml   {recording_id, slug, job_root, snapshot_at, subtitles_sha256, duration_ms,
                      end_limit_ms, boundary_mode, profile: {reel, long, selection_margin, ...},
                      human_time: [status.yaml human_time entries], live: {knowledge_version,
                      prompt_chars: {kind: n}, cost_usd: {kind: usd}}}

Replay of version V on recording R, type T: judge task `s3c.replay.<slug>.<T>`, same prompt
builder and the same S3a validator/placement code; result in
`replay/<V>/<slug>/<T>.yaml` (cached by knowledge content hash + subtitles hash).

Measurement of V on a recording set: proposals come from the replay file, or — if V itself ran
live on the recording — from the proposals of its live review (no model call needed). Verdict
inheritance and coverage per `_matching`; unevaluated proposals count as reject; human time is
NOT inherited (load = proxy only).
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from ..core.config import load_config, resolve_job_config
from ..core.costs import CostJournal
from ..core.ctx import Ctx
from ..core.errors import VPError
from ..core.io import now_iso, read_yaml, sha256_bytes, sha256_file, write_yaml
from ..core.paths import Job
from ..core.srt import read_srt, srt_duration_ms
from ..core.status import Status
from ..steps import _fragments as F
from ..steps.s3a_select import parse_output, place_fragments, transcript_text
from . import _matching as M
from .prompt_builder import build_prompt
from .store import KnowledgeStore

KINDS = F.TYPES
PROPOSAL_FIELDS = ("start_ms", "end_ms", "first_cue", "last_cue", "boundary", "title", "score",
                   "applied", "text")


def rec_slug(recording_id: str) -> str:
    """File/task-safe name of a recording id (ids may hold '/')."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(recording_id)).strip("._-")[:40] or "rec"
    return f"{base}-{sha256_bytes(str(recording_id))[:8]}"


def boundary_of_mode(mode: str | None) -> str:
    return "subtitle" if mode == "srt_only" else "words"


# ---- archive ----------------------------------------------------------------------------------

class Archive:
    """Per-recording subtitles snapshots under `<root>/recordings/`."""

    def __init__(self, store: KnowledgeStore):
        self.store = store
        self.dir = store.root / "recordings"

    def path(self, recording_id: str) -> Path:
        return self.dir / rec_slug(recording_id)

    def snapshot(self, job: Job, home: Path) -> dict[str, Any]:
        """Copy the job's subtitles (+ words) and the facts S3c needs. Idempotent (overwrites)."""
        if not job.subtitles_srt.exists():
            raise VPError("data", f"{job.subtitles_srt} missing: nothing to snapshot (run S2 first)")
        rid = job.job_id
        d = self.path(rid)
        d.mkdir(parents=True, exist_ok=True)
        jy = read_yaml(job.job_yaml, default={}) or {}
        proposed = read_yaml(job.fragments_proposed, default={}) or {}
        mode = proposed.get("boundary_mode") or (jy.get("boundary_mode") if isinstance(jy, dict) else None)
        if mode is None:
            mode = "words" if job.source_words.exists() else "srt_only"
        shutil.copyfile(job.subtitles_srt, d / "subtitles.srt.tmp")
        (d / "subtitles.srt.tmp").replace(d / "subtitles.srt")
        if mode == "words" and job.source_words.exists():
            shutil.copyfile(job.source_words, d / "words.json.tmp")
            (d / "words.json.tmp").replace(d / "words.json")
        elif (d / "words.json").exists():
            (d / "words.json").unlink()
        cfg = resolve_job_config(load_config(home), job.request())
        prof = cfg["profile"]
        cues = read_srt(job.subtitles_srt)
        human = []
        for e in Status(job).history():
            ht = (e.get("metadata") or {}).get("human_time")
            if isinstance(ht, dict) and e.get("stage") == "S3b":
                human.append({k: ht.get(k) for k in ("kind", "minutes", "source", "opened_at", "closed_at")})
        cost: dict[str, float] = {}
        for r in CostJournal(home).records(job=rid):
            task = str(r.get("task") or "")
            if task.startswith("s3a."):
                k = task.split(".", 1)[1]
                cost[k] = round(cost.get(k, 0.0) + float(r.get("usd") or 0.0), 6)
        prompts = proposed.get("prompts") or {}
        meta = {
            "recording_id": rid, "slug": rec_slug(rid), "job_root": str(job.root),
            "snapshot_at": now_iso(), "subtitles_sha256": sha256_file(d / "subtitles.srt"),
            "duration_ms": srt_duration_ms(cues), "end_limit_ms": F._video_end_ms(job),
            "boundary_mode": mode,
            "profile": {k: prof.get(k) for k in ("reel", "long", "selection_margin",
                                                 "selection_expected_per_hour") if k in prof},
            "selection": dict(cfg.get("selection") or {}),
            "human_time": human,
            "live": {"knowledge_version": proposed.get("knowledge_version"),
                     "prompt_chars": {k: (prompts.get(k) or {}).get("chars") for k in KINDS
                                      if isinstance(prompts.get(k), dict)},
                     "cost_usd": cost},
        }
        write_yaml(d / "recording.yaml", meta)
        return meta

    def load(self, recording_id: str) -> dict[str, Any] | None:
        m = read_yaml(self.path(recording_id) / "recording.yaml", default=None)
        return m if isinstance(m, dict) else None

    def recordings(self) -> list[str]:
        if not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.iterdir()):
            m = read_yaml(p / "recording.yaml", default=None)
            if isinstance(m, dict) and m.get("recording_id"):
                out.append(str(m["recording_id"]))
        return sorted(out)

    def timeline(self, recording_id: str) -> F.Timeline:
        meta = self.load(recording_id)
        if meta is None:
            raise VPError("data", f"recording {recording_id} is not in the archive (run `learn snapshot`)")
        d = self.path(recording_id)
        words = F.load_words(d / "words.json") if meta.get("boundary_mode") == "words" else None
        return F.Timeline(read_srt(d / "subtitles.srt"), words, end_limit_ms=meta.get("end_limit_ms"))

    def minutes(self, recording_id: str) -> dict[str, Any]:
        """Per-recording info for metrics: duration, review / full_scan minutes."""
        meta = self.load(recording_id) or {}
        by: dict[str, float] = {}
        for h in meta.get("human_time") or []:
            if h.get("minutes") is not None:
                by[h.get("kind") or "review"] = by.get(h.get("kind") or "review", 0.0) + float(h["minutes"])
        return {"duration_ms": meta.get("duration_ms") or 0, "review_minutes": by.get("review"),
                "full_scan_minutes": by.get("full_scan")}


# ---- verdict places -----------------------------------------------------------------------------

def _error_s(v: dict[str, Any]) -> float:
    sh = v.get("shift_ms") or {}
    return (abs(int(sh.get("start") or 0)) + abs(int(sh.get("end") or 0))) / 2000.0


class VerdictIndex:
    """verdicts.jsonl indexed by recording (oldest first)."""

    def __init__(self, verdicts: Iterable[dict[str, Any]]):
        self.all = sorted(verdicts, key=lambda v: str(v.get("recorded_at") or ""))
        self.by_rec: dict[str, list[dict[str, Any]]] = {}
        for v in self.all:
            self.by_rec.setdefault(str(v["recording_id"]), []).append(v)
        self.keys = {str(v["key"]): v for v in self.all}

    def recordings(self) -> list[str]:
        return sorted(self.by_rec)

    def places(self, rid: str) -> list[dict[str, Any]]:
        """Places with a human verdict, oldest first: proposed boundaries carry the verdict;
        final boundaries of accept/adjust/added count as accept."""
        out = []
        for v in self.by_rec.get(rid, []):
            b = boundary_of_mode(v.get("boundary_mode"))
            p, f_ = v.get("proposed") or {}, v.get("final") or {}
            if v["verdict"] in ("accept", "reject", "adjust") and p.get("start_ms") is not None:
                out.append({"type": v.get("type"), "boundary": b, "outcome": v["verdict"],
                            "reason_class": v.get("reason_class"), "key": v["key"], "origin": "proposed",
                            "error_s": _error_s(v) if v["verdict"] == "adjust" else None,
                            **{k: p[k] for k in ("start_ms", "end_ms", "first_cue", "last_cue")}})
            if v["verdict"] in ("adjust", "added") and f_.get("start_ms") is not None:
                out.append({"type": v.get("type"), "boundary": b, "outcome": "accept",
                            "reason_class": None, "key": v["key"], "origin": "final", "error_s": None,
                            **{k: f_[k] for k in ("start_ms", "end_ms", "first_cue", "last_cue")}})
        return out

    def references(self, rid: str) -> list[dict[str, Any]]:
        """Strong places of the recording (accepted / corrected / added), deduplicated."""
        refs = []
        for v in self.by_rec.get(rid, []):
            f_ = v.get("final") or {}
            if v["verdict"] in ("accept", "adjust", "added") and f_.get("start_ms") is not None:
                refs.append({"type": v.get("type"), "key": v["key"],
                             **{k: f_[k] for k in ("start_ms", "end_ms", "first_cue", "last_cue")}})
        return M.dedup_places(refs)

    def full_scan(self, rid: str) -> bool:
        return any(v.get("full_scan") is True and v.get("source") != "candidate_eval"
                   for v in self.by_rec.get(rid, []))

    def live_review(self, version: str, rid: str) -> list[dict[str, Any]] | None:
        """Verdicts of the LATEST live review of `rid` made with `version` (None if none)."""
        vs = [v for v in self.by_rec.get(rid, []) if v.get("knowledge_version") == version
              and v.get("source") != "candidate_eval"]
        if not vs:
            return None
        last = vs[-1].get("review_sha256")
        return [v for v in vs if v.get("review_sha256") == last]

    def live_versions(self) -> dict[str, list[str]]:
        """{version: [recordings reviewed live with it]}"""
        out: dict[str, set[str]] = {}
        for v in self.all:
            if v.get("source") != "candidate_eval" and v.get("knowledge_version"):
                out.setdefault(str(v["knowledge_version"]), set()).add(str(v["recording_id"]))
        return {k: sorted(s) for k, s in sorted(out.items())}


# ---- proposals of a version on a recording ----------------------------------------------------------

def replay_path(store: KnowledgeStore, version: str, rid: str, kind: str) -> Path:
    return store.replay_dir / version / rec_slug(rid) / f"{kind}.yaml"


def read_replay(store: KnowledgeStore, archive: Archive, version: str, rid: str,
                kind: str) -> dict[str, Any] | None:
    """A replay result that is still valid (same knowledge content, same subtitles)."""
    doc = read_yaml(replay_path(store, version, rid, kind), default=None)
    meta = archive.load(rid)
    if not isinstance(doc, dict) or meta is None:
        return None
    if doc.get("content_hash") != store.manifest(version).get("content_hash"):
        return None
    if doc.get("subtitles_sha256") != meta.get("subtitles_sha256"):
        return None
    return doc


def proposals_for(store: KnowledgeStore, archive: Archive, vindex: VerdictIndex, version: str,
                  rid: str, kind: str) -> dict[str, Any] | None:
    """{"source": replay|live, "proposals": [...], "prompt_chars", "cost_usd"} or None."""
    meta = archive.load(rid) or {}
    live_meta = meta.get("live") or {}
    live = vindex.live_review(version, rid)
    if live is not None or live_meta.get("knowledge_version") == version:
        b = boundary_of_mode((live or [{}])[0].get("boundary_mode") if live else meta.get("boundary_mode"))
        props = []
        for v in live or []:
            p = v.get("proposed") or {}
            if v.get("type") == kind and v["verdict"] in ("accept", "reject", "adjust") and p.get("start_ms") is not None:
                props.append({"type": kind, "boundary": b, **{k: p.get(k) for k in
                              ("start_ms", "end_ms", "first_cue", "last_cue", "title", "score", "applied")}})
        return {"source": "live", "proposals": props,
                "prompt_chars": (live_meta.get("prompt_chars") or {}).get(kind)
                if live_meta.get("knowledge_version") == version else None,
                "cost_usd": (live_meta.get("cost_usd") or {}).get(kind)
                if live_meta.get("knowledge_version") == version else None}
    doc = read_replay(store, archive, version, rid, kind)
    if doc is None:
        return None
    return {"source": "replay", "proposals": [dict(p, type=kind) for p in doc.get("proposals") or []],
            "prompt_chars": doc.get("prompt_chars"), "cost_usd": doc.get("cost_usd")}


def evaluate_unit(rid: str, kind: str, proposals: list[dict[str, Any]], places: list[dict[str, Any]],
                  references: list[dict[str, Any]] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One evaluation unit (replay semantics). Returns (unit, unevaluated proposals)."""
    out, uneval = [], []
    kplaces = [p for p in places if p.get("type") == kind]
    for p in proposals:
        m = M.find_inherited(p, kplaces)
        if m is None:
            out.append({"outcome": "unevaluated", "reason_class": None, "error_s": None})
            uneval.append(p)
        else:
            out.append({"outcome": m["outcome"], "reason_class": m.get("reason_class"),
                        "error_s": m.get("error_s"), "key": m["key"]})
    misses = None
    if references is not None:
        misses = len(M.uncovered([r for r in references if r.get("type") == kind], proposals))
    return {"recording_id": rid, "type": kind, "proposals": out, "misses": misses}, uneval


def measure(store: KnowledgeStore, archive: Archive, vindex: VerdictIndex, version: str,
            rids: Iterable[str], kinds: Iterable[str] = KINDS) -> dict[str, Any]:
    """Units of `version` on recordings `rids` (replay semantics). Returns
    {"units", "recs", "missing": [(rid, kind)], "unevaluated": [{recording_id, type, proposal}],
     "sources": {rid: {kind: replay|live}}}."""
    units, recs, missing, uneval, sources = [], {}, [], [], {}
    for rid in rids:
        places = vindex.places(rid)
        refs = vindex.references(rid) if vindex.full_scan(rid) else None
        info = archive.minutes(rid)
        recs[rid] = {"duration_ms": info["duration_ms"], "review_minutes": None, "full_scan_minutes": None}
        for kind in kinds:
            got = proposals_for(store, archive, vindex, version, rid, kind)
            if got is None:
                missing.append((rid, kind))
                continue
            unit, ue = evaluate_unit(rid, kind, got["proposals"], places, refs)
            unit["prompt_chars"], unit["cost_usd"] = got.get("prompt_chars"), got.get("cost_usd")
            units.append(unit)
            uneval.extend({"recording_id": rid, "type": kind, "proposal": p} for p in ue)
            sources.setdefault(rid, {})[kind] = got["source"]
    return {"units": units, "recs": recs, "missing": missing, "unevaluated": uneval, "sources": sources}


def live_units(vindex: VerdictIndex, archive: Archive, version: str,
               kinds: Iterable[str] = KINDS) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Units from live verdicts of `version` (live semantics: misses = `added` on full_scan
    reviews; review minutes from the archive's human time)."""
    units, recs, rids = [], {}, []
    for rid in vindex.recordings():
        rev = vindex.live_review(version, rid)
        if rev is None:
            continue
        rids.append(rid)
        meta = archive.load(rid) or {}
        live_meta = meta.get("live") or {}
        same = live_meta.get("knowledge_version") == version
        recs[rid] = archive.minutes(rid) if same else {"duration_ms": meta.get("duration_ms") or 0,
                                                        "review_minutes": None, "full_scan_minutes": None}
        full = any(v.get("full_scan") is True for v in rev)
        for kind in kinds:
            props = [{"outcome": v["verdict"], "reason_class": v.get("reason_class"),
                      "error_s": _error_s(v) if v["verdict"] == "adjust" else None}
                     for v in rev if v.get("type") == kind and v["verdict"] in ("accept", "reject", "adjust")]
            added = sum(1 for v in rev if v.get("type") == kind and v["verdict"] == "added")
            units.append({"recording_id": rid, "type": kind, "proposals": props,
                          "misses": added if full else None,
                          "prompt_chars": (live_meta.get("prompt_chars") or {}).get(kind) if same else None,
                          "cost_usd": (live_meta.get("cost_usd") or {}).get(kind) if same else None})
    return units, recs, rids


# ---- running a replay ------------------------------------------------------------------------------

def work_ctx(store: KnowledgeStore, name: str, config: dict[str, Any], judge: Any) -> Ctx:
    """A context whose job folder is a work dir inside the knowledge store (judge tasks live there)."""
    root = store.root / "work" / name
    root.mkdir(parents=True, exist_ok=True)
    req = root / "request.yaml"
    if not req.exists():
        write_yaml(req, {"job_id": f"s3c-{name}"})
    return Ctx(Job(root), None, config, judge=judge)


def run_replay(store: KnowledgeStore, archive: Archive, version: str, rids: Iterable[str],
               config: dict[str, Any], judge: Any, kinds: Iterable[str] = KINDS,
               force: bool = False) -> list[dict[str, Any]]:
    """Replay `version` on recordings `rids`; returns one summary per (rid, kind)."""
    kv = store.load_version(version)
    content_hash = kv.content_hash
    ctx = work_ctx(store, f"replay-{version}", config, judge)
    out = []
    for rid in rids:
        meta = archive.load(rid)
        if meta is None:
            raise VPError("data", f"recording {rid} is not in the archive (run `learn snapshot <job>`)")
        tl = archive.timeline(rid)
        transcript = transcript_text(tl.cues)
        rec_ms = srt_duration_ms(tl.cues)
        prof = meta.get("profile") or {}
        for kind in kinds:
            path = replay_path(store, version, rid, kind)
            if not force and read_replay(store, archive, version, rid, kind) is not None:
                out.append({"recording_id": rid, "type": kind, "status": "cached"})
                continue
            rng = F.duration_range(prof, kind)
            cap = F.proposal_cap(kind, rec_ms, prof)
            built = build_prompt(store, version, kind, transcript, cap=cap, min_s=rng[0] / 1000,
                                 max_s=rng[1] / 1000, settings=meta.get("selection") or {})
            task = f"s3c.replay.{rec_slug(rid)}.{kind}"
            started = now_iso()
            raw = judge.run(task, built.text, {"subtitles.txt": transcript}, "output.yaml", ctx)
            items = parse_output(raw)
            kept, dropped = place_fragments(kind, items, tl, rng, cap, kv.ref_ids())
            usd = sum(float(r.get("usd") or 0.0) for r in ctx.costs.records(job=ctx.job.job_id)
                      if r.get("task") == task and str(r.get("at") or "") >= started)
            doc = {"version": version, "content_hash": content_hash, "recording_id": rid,
                   "type": kind, "subtitles_sha256": meta.get("subtitles_sha256"),
                   "created_at": now_iso(), "cap": cap, "prompt_chars": built.chars,
                   "prompt_sha256": built.sha256, "cases": built.cases, "cost_usd": round(usd, 6),
                   "returned": len(items), "discarded": len(dropped),
                   "proposals": [{k: f_.get(k) for k in PROPOSAL_FIELDS} for f_ in kept]}
            write_yaml(path, doc)
            out.append({"recording_id": rid, "type": kind, "status": "replayed",
                        "proposals": len(kept), "discarded": len(dropped)})
    return out
