"""S3c — the learning loop of fragment selection (`/vp-learn`), OUTSIDE production (SPEC S3c).

Roles (llm-as-component): every count, match, comparison and decision is code; the critic and the
analyst are judge tasks (`s3c.critic`, `s3c.analyst`) whose output is validated; the replay reuses
the S3a judge task shape (`s3c.replay.<rec>.<type>`) and the S3a validator/placement code.

    python -m vp learn snapshot <job>          archive the job's subtitles (+ words, human time, cost)
    python -m vp learn splits show|set ...     fix-set / holdout by recording; yardstick pinned
    python -m vp learn eval                    live metrics per version, per type, three groups
    python -m vp learn curve                   version series on the SAME recordings -> yaml + table
    python -m vp learn critic                  recurring error patterns (fix-set verdicts only)
    python -m vp learn analyst                 candidate versions from the critic's findings
    python -m vp learn replay [V]              replay candidate V (and the active baseline) on archive
    python -m vp learn export-eval V           batch of V's unevaluated proposals for a human
    python -m vp learn import-eval V           verdicts of that batch -> verdicts.jsonl
    python -m vp learn decide [V]              activate (elitism) or reject + rejected_hypotheses
    python -m vp learn observe [--apply]       live metrics of active vs previous -> rollback advice

Files under `<vp_home>/selection/` owned by S3c: recordings/ (archive), replay/<V>/..., learn/
(eval.yaml, curve.yaml, curve.txt, critic/, analyst/, pending.jsonl, decisions.jsonl,
human_time.jsonl, observe.yaml), candidate_eval/<V>.review.yaml (human-facing batch),
learn/candidate_eval/<V>.batch.json (code sidecar), work/ (judge task folders).

Config `learn:` in `<vp_home>/config.yaml` (defaults in LEARN_DEFAULTS).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from ..core.config import deep_merge, load_config
from ..core.errors import VPError
from ..core.io import (append_line, atomic_write, now_iso, parse_iso, read_json, read_yaml,
                       write_json, write_yaml)
from ..core.paths import Job
from ..steps import _fragments as F
from . import _candidates as C
from . import _metrics as MX
from . import _replay as R
from .store import HoldoutLeak, KnowledgeStore, verdict_key

LEARN_DEFAULTS: dict[str, Any] = {
    "min_independent_cases": 3,          # a generalisation (rule/pattern/criteria/merge) waits below this
    "min_recordings_for_generalisation": 2,   # «подтверждение на чужой записи»
    "min_fix_recordings": 1,
    "min_holdout_recordings": 1,
    "candidate_eval_threshold": 0.10,    # unevaluated share above which a batch is recommended
    "observe_min_recordings": 2,
    "tolerance": {"precision": 0.0, "boundary_error_s": 0.5, "misses_per_recording": 0.0,
                  "candidates_per_recording": 0.0, "adjust_share": 0.0,
                  "review_minutes_per_recording": 1.0},
    "budget": {"max_prompt_chars": 60000, "max_prompt_growth": 0.25,
               "max_cost_per_recording_min_usd": None, "max_cost_growth": 0.25},
    "critic": {"max_groups": 30, "examples_per_group": 3, "text_chars": 300, "max_findings": 8},
    "analyst": {"max_candidates": 5, "rejected_tail": 20, "history_versions": 6, "text_chars": 300},
}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _csv(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


class Learner:
    """One S3c session over the knowledge store at `<home>/selection`."""

    def __init__(self, home: str | Path | None = None, judge: Any = None,
                 config: dict[str, Any] | None = None, store: KnowledgeStore | None = None):
        self.config = config or load_config(home)
        self.home = Path(self.config["vp_home"])
        self.store = store or KnowledgeStore(self.home)
        self.settings = deep_merge(LEARN_DEFAULTS, self.config.get("learn") or {})
        self._judge = judge
        self.archive = R.Archive(self.store)

    # ---- plumbing ------------------------------------------------------------------------------
    @property
    def judge(self) -> Any:
        if self._judge is None:
            from ..core.judge import make_judge
            self._judge = make_judge(self.config)
        return self._judge

    @property
    def dir(self) -> Path:
        return self.store.root / "learn"

    def vindex(self) -> R.VerdictIndex:
        return R.VerdictIndex(self.store.verdicts())

    def splits(self) -> dict[str, Any]:
        return self.store.read_splits()

    def split_of(self, rid: str) -> str:
        s = self.splits()
        if rid in set(map(str, s.get("holdout") or [])):
            return "holdout"
        if rid in set(map(str, s.get("fix_set") or [])):
            return "fix_set"
        return "unassigned"

    def _check_yardstick(self) -> None:
        y = self.splits().get("yardstick") or {}
        if not y:
            raise VPError("data", "splits.yaml has no pinned yardstick: run `learn splits set` first")
        if y.get("hash") != MX.yardstick_hash():
            raise VPError("data", f"the yardstick changed since the splits were fixed "
                                  f"(pinned {y.get('version')}, code {MX.YARDSTICK['version']}); "
                                  f"measurements are not comparable — owner decision required")

    # ---- snapshot ------------------------------------------------------------------------------
    def snapshot(self, job_dir: str | Path) -> dict[str, Any]:
        self.store.ensure_seed()
        return self.archive.snapshot(Job(job_dir), self.home)

    # ---- splits --------------------------------------------------------------------------------
    def _knowledge_recordings(self) -> set[str]:
        """Recordings whose verdicts already shaped a (non-seed) version: cases + manifest sources."""
        keys = self.vindex().keys
        out: set[str] = set()
        for v in self.store.list_versions():
            man = self.store.manifest(v)
            if man.get("author") == "seed":
                continue
            for s in man.get("sources") or []:
                if s in keys:
                    out.add(str(keys[s]["recording_id"]))
            for c in self.store.load_version(v).cases:
                out.add(str(c["recording"]))
        return out

    def splits_set(self, fix_set: Iterable[str] = (), holdout: Iterable[str] = ()) -> dict[str, Any]:
        """Add recordings to the fix-set / holdout. Splits are fixed: a recording never moves, and a
        recording that already shaped knowledge can never become holdout."""
        cur = self.splits()
        y = cur.get("yardstick")
        if y and y.get("hash") != MX.yardstick_hash():
            self._check_yardstick()
        fs = list(map(str, cur.get("fix_set") or []))
        ho = list(map(str, cur.get("holdout") or []))
        used = self._knowledge_recordings()
        for r in map(str, fix_set):
            if r in ho:
                raise VPError("data", f"{r} is in the holdout; splits are fixed, a recording never moves")
            if r not in fs:
                fs.append(r)
        for r in map(str, holdout):
            if r in fs:
                raise VPError("data", f"{r} is in the fix-set; splits are fixed, a recording never moves")
            if r in used:
                raise VPError("data", f"{r} already shaped the knowledge; holdout must be fixed before "
                                      f"knowledge building")
            if r not in ho:
                ho.append(r)
        doc = {"version": int(cur.get("version") or 1),
               "yardstick": y or {"version": MX.YARDSTICK["version"], "hash": MX.yardstick_hash(),
                                  "pinned_at": now_iso()},
               "created_at": cur.get("created_at") or now_iso(), "updated_at": now_iso(),
               "fix_set": sorted(fs), "holdout": sorted(ho)}
        self.store.write_splits(doc)
        return doc

    # ---- eval (live) ---------------------------------------------------------------------------
    def eval(self) -> dict[str, Any]:
        vi = self.vindex()
        versions: dict[str, Any] = {}
        for v, _ in vi.live_versions().items():
            units, recs, rids = R.live_units(vi, self.archive, v)
            versions[v] = {"recordings": rids,
                           "splits": {s: sum(1 for r in rids if self.split_of(r) == s)
                                      for s in ("fix_set", "holdout", "unassigned")},
                           "metrics": MX.aggregate(units, recs)}
        ce = _read_jsonl(self.dir / "human_time.jsonl")
        doc = {"generated_at": now_iso(), "yardstick": MX.YARDSTICK["version"],
               "note": "live metrics: each version on the recordings it actually reviewed; NOT "
                       "comparable across versions unless the recording sets match — use `learn curve`",
               "candidate_eval_minutes": round(sum(float(r.get("minutes") or 0) for r in ce), 2),
               "versions": versions}
        write_yaml(self.dir / "eval.yaml", doc)
        doc["table"] = MX.table([(v, d["metrics"]) for v, d in versions.items()])
        return doc

    # ---- curve -----------------------------------------------------------------------------------
    def curve(self, versions: list[str] | None = None, recordings: list[str] | None = None) -> dict[str, Any]:
        """Version series on ONE common recording set (versions lacking it are excluded)."""
        vi = self.vindex()
        versions = versions or self.store.list_versions()
        rids = recordings or sorted(set(self.archive.recordings()) & set(vi.recordings()))
        cover: dict[str, set[str]] = {}
        for v in versions:
            m = R.measure(self.store, self.archive, vi, v, rids)
            miss = {r for r, _ in m["missing"]}
            cover[v] = {r for r in rids if r not in miss}
        common: set[str] | None = None
        included, excluded = [], {}
        for v in sorted(versions, key=lambda x: (-len(cover[x]), x)):
            if not cover[v]:
                excluded[v] = "no measurement (run `learn replay`)"
                continue
            new = cover[v] if common is None else common & cover[v]
            if new:
                common = new
                included.append(v)
            else:
                excluded[v] = "no recordings in common with the series"
        common_list = sorted(common or [])
        series = []
        for v in sorted(included):
            m = R.measure(self.store, self.archive, vi, v, common_list)
            by_split = {}
            for s in ("fix_set", "holdout"):
                us = [u for u in m["units"] if self.split_of(u["recording_id"]) == s]
                if us:
                    by_split[s] = MX.aggregate(us, m["recs"])
            info = self.store.version_info(v)
            series.append({"version": v, "status": info["state"].get("status"),
                           "parent": info.get("parent"),
                           "metrics": {"all": MX.aggregate(m["units"], m["recs"]), **by_split}})
        doc = {"generated_at": now_iso(), "yardstick": MX.YARDSTICK["version"],
               "recordings": common_list, "excluded": excluded, "series": series}
        write_yaml(self.dir / "curve.yaml", doc)
        text = MX.table([(s["version"], s["metrics"]["all"]) for s in series])
        atomic_write(self.dir / "curve.txt", f"recordings: {len(common_list)}\n{text}\n")
        doc["table"] = text
        return doc

    # ---- critic -----------------------------------------------------------------------------------
    def critic(self) -> dict[str, Any]:
        vi = self.vindex()
        verdicts = C.learning_verdicts(self.store, vi.all)
        if not verdicts:
            raise VPError("data", "no fix-set verdicts to criticise (snapshot jobs and set splits first)")
        return self.critic_on(verdicts)

    def critic_on(self, verdicts: list[dict[str, Any]]) -> dict[str, Any]:
        """Critic over the given verdicts (entry point; holdout verdicts are refused)."""
        active = self.store.active_version()
        kv = self.store.load_version(active)
        prompt, text, keys = C.critic_input(self.store, verdicts, self.settings["critic"])
        ctx = R.work_ctx(self.store, "learn", self.config, self.judge)
        raw = self.judge.run("s3c.critic", prompt, {"verdict_groups.yaml": text}, "output.yaml", ctx)
        findings = C.parse_critic(raw, keys, kv.ref_ids())
        doc = {"at": now_iso(), "active_version": active, "verdicts": len(verdicts),
               "findings": findings}
        write_yaml(self.dir / "critic" / f"{_stamp()}.yaml", doc)
        write_yaml(self.dir / "critic" / "latest.yaml", doc)
        return doc

    # ---- analyst ------------------------------------------------------------------------------------
    def history(self) -> dict[str, Any]:
        n = int(self.settings["analyst"]["history_versions"])
        vs = []
        for v in self.store.list_versions()[-n:]:
            info = self.store.version_info(v)
            p = info["state"].get("passport") or {}
            vs.append({"id": v, "parent": info.get("parent"), "status": info["state"].get("status"),
                       "hypothesis": info.get("hypothesis"),
                       "decision": (p.get("decision") or {}).get("result"),
                       "groups_fix": (p.get("decision") or {}).get("fix_set"),
                       "groups_holdout": (p.get("decision") or {}).get("holdout")})
        tail = int(self.settings["analyst"]["rejected_tail"])
        rej = [{k: r.get(k) for k in ("at", "version", "hypothesis", "why")}
               for r in self.store.rejected_hypotheses()[-tail:]]
        return {"versions": vs, "rejected_hypotheses": rej}

    def analyst(self, findings_doc: dict[str, Any] | None = None) -> dict[str, Any]:
        doc = findings_doc or read_yaml(self.dir / "critic" / "latest.yaml", default=None)
        if not isinstance(doc, dict):
            raise VPError("data", "no critic findings: run `learn critic` first")
        findings = doc.get("findings") or []
        vi = self.vindex()
        ev_keys = list(dict.fromkeys(k for f in findings for k in f.get("evidence") or []))
        unknown = [k for k in ev_keys if k not in vi.keys]
        if unknown:
            raise VPError("data", f"critic evidence references unknown verdicts: {unknown}")
        verdicts = [vi.keys[k] for k in ev_keys]
        return self.analyst_on(findings, verdicts)

    def analyst_on(self, findings: list[dict[str, Any]], verdicts: list[dict[str, Any]]) -> dict[str, Any]:
        """Analyst over the given findings + evidence verdicts (entry point; holdout refused)."""
        active = self.store.active_version()
        kv = self.store.load_version(active)
        st = self.settings["analyst"]
        prompt, inputs, _ = C.analyst_input(self.store, findings, verdicts, kv, self.history(), st)
        ctx = R.work_ctx(self.store, "learn", self.config, self.judge)
        raw = self.judge.run("s3c.analyst", prompt, inputs, "output.yaml", ctx)
        items = C.parse_analyst(raw, int(st["max_candidates"]))
        all_keys = self.vindex().keys
        created, pending, refused = [], [], []
        for i, item in enumerate(items, 1):
            try:
                cand = C.check_candidate(self.store, item, all_keys, kv)
                wait = C.evidence_gate(cand, all_keys, int(self.settings["min_independent_cases"]),
                                       int(self.settings["min_recordings_for_generalisation"]))
                if wait:
                    rec = {"at": now_iso(), "parent": active, "reason": wait, **cand}
                    append_line(self.dir / "pending.jsonl", json.dumps(rec, ensure_ascii=False))
                    pending.append({"n": i, "kind": cand["kind"], "reason": wait})
                    continue
                changes, removals = C.build_changes(cand, self.store.read_files(active), kv, all_keys)
                vid = self.store.derive_version(active, changes, removals, author="analyst",
                                                hypothesis=cand["hypothesis"], sources=cand["sources"])
                cases = {(str(all_keys[s]["recording_id"]), str(all_keys[s].get("fragment_id")))
                         for s in cand["sources"]}
                self.store.set_state(vid, passport={"proposal": {
                    "kind": cand["kind"], "expected_effect": cand["expected_effect"],
                    "edit": cand["edit"], "independent_cases": len(cases),
                    "recordings": sorted({r for r, _ in cases})}}, note="analyst proposal")
                created.append({"n": i, "version": vid, "kind": cand["kind"]})
            except HoldoutLeak:
                raise
            except C.CandidateRefused as e:
                refused.append({"n": i, "kind": item.get("kind"), "reason": e.reason})
            except VPError as e:
                if e.kind not in ("data",):
                    raise
                refused.append({"n": i, "kind": item.get("kind"), "reason": e.msg})
        doc = {"at": now_iso(), "parent": active, "created": created, "pending": pending,
               "refused": refused}
        write_yaml(self.dir / "analyst" / f"{_stamp()}.yaml", doc)
        return doc

    # ---- replay -------------------------------------------------------------------------------------
    def measured_recordings(self) -> tuple[list[str], list[str]]:
        """(fix-set, holdout) recordings that are archived and have verdicts."""
        s = self.splits()
        have = set(self.archive.recordings()) & set(self.vindex().recordings())
        fix = [r for r in map(str, s.get("fix_set") or []) if r in have]
        hold = [r for r in map(str, s.get("holdout") or []) if r in have]
        return sorted(fix), sorted(hold)

    def default_candidate(self) -> str:
        cands = [v for v in self.store.list_versions()
                 if self.store.state(v).get("status") == "candidate"]
        if not cands:
            raise VPError("data", "no candidate versions (run `learn analyst`)")
        return cands[0]

    def replay(self, version: str | None = None, recordings: list[str] | None = None,
               force: bool = False, baseline: bool = True) -> dict[str, Any]:
        version = version or self.default_candidate()
        fix, hold = self.measured_recordings()
        rids = recordings or (fix + hold)
        if not rids:
            raise VPError("data", "no archived recordings with verdicts in the splits")
        out = {"version": version, "recordings": rids,
               "candidate": R.run_replay(self.store, self.archive, version, rids, self.config,
                                         self.judge, force=force)}
        if baseline:
            active = self.store.active_version()
            if active != version:
                vi = self.vindex()
                need = sorted({r for r in rids for k in R.KINDS
                               if R.proposals_for(self.store, self.archive, vi, active, r, k) is None})
                out["baseline"] = {"version": active, "replayed": need and R.run_replay(
                    self.store, self.archive, active, need, self.config, self.judge) or []}
        return out

    # ---- candidate_eval batch ------------------------------------------------------------------------
    def _eval_paths(self, version: str) -> tuple[Path, Path]:
        return (self.store.root / "candidate_eval" / f"{version}.review.yaml",
                self.dir / "candidate_eval" / f"{version}.batch.json")

    def export_eval(self, version: str, force: bool = False) -> dict[str, Any]:
        fix, hold = self.measured_recordings()
        m = R.measure(self.store, self.archive, self.vindex(), version, fix + hold)
        if m["missing"]:
            raise VPError("data", f"{version} has no replay on {sorted({r for r, _ in m['missing']})}")
        items, side = [], []
        for n, u in enumerate(m["unevaluated"], 1):
            p = u["proposal"]
            iid = f"e{n:03d}"
            items.append({"id": iid, "recording_id": u["recording_id"], "type": u["type"],
                          "subtitles": f"#{p['first_cue']} – #{p['last_cue']}",
                          "time": f"{F.clock(p['start_ms'])} – {F.clock(p['end_ms'])}",
                          "title": p.get("title"), "text": C._short(p.get("text"), 600),
                          "verdict": None, "reason_class": None, "reason": None,
                          "start": None, "end": None})
            side.append({"id": iid, "recording_id": u["recording_id"], "type": u["type"],
                         "proposal": {k: p.get(k) for k in R.PROPOSAL_FIELDS}})
        review, sidecar = self._eval_paths(version)
        bid = C.batch_id(side)
        old = read_json(sidecar, default=None)
        if review.exists() and not force and isinstance(old, dict) and old.get("batch_id") != bid:
            raise VPError("data", f"{review} holds another batch; import it or use --force")
        if not (review.exists() and isinstance(old, dict) and old.get("batch_id") == bid):
            atomic_write(review, C.eval_template(version, items, bid))
            write_json(sidecar, {"version": version, "batch_id": bid, "created_at": now_iso(),
                                 "items": side})
        total = sum(len(u["proposals"]) for u in m["units"])
        return {"version": version, "file": str(review), "items": len(items), "batch_id": bid,
                "unevaluated_share": round(len(items) / total, 4) if total else None}

    def import_eval(self, version: str) -> dict[str, Any]:
        review, sidecar = self._eval_paths(version)
        side = read_json(sidecar, default=None)
        if not isinstance(side, dict) or not review.exists():
            raise VPError("data", f"no candidate_eval batch for {version} (run `learn export-eval`)")
        try:
            doc = yaml.safe_load(review.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise VPError("data", f"{review.name}: not valid YAML: {str(e)[:200]}") from e
        if not isinstance(doc, dict) or doc.get("batch_id") != side["batch_id"]:
            raise VPError("data", f"{review.name} does not belong to batch {side['batch_id']}")
        by_id = {it["id"]: it for it in side["items"]}
        errors, open_, recs = [], [], []
        seen = set()
        for it in doc.get("items") or []:
            iid = str((it or {}).get("id"))
            if iid not in by_id or iid in seen:
                errors.append({"where": iid, "message": "unknown or repeated item id"})
                continue
            seen.add(iid)
            verdict = it.get("verdict")
            if verdict in (None, ""):
                open_.append(iid)
                continue
            if verdict not in ("accept", "reject", "adjust"):
                errors.append({"where": iid, "message": "verdict must be accept, reject or adjust"})
                continue
            rc = it.get("reason_class")
            if rc not in (None, "") and rc not in MX.REASON_CLASSES:
                errors.append({"where": iid, "message": "reason_class is not in the vocabulary"})
                continue
            reason = it.get("reason")
            if verdict != "accept" and not (isinstance(reason, str) and reason.strip()):
                errors.append({"where": iid, "message": f"reason is required for {verdict}"})
                continue
            s = by_id[iid]
            p = s["proposal"]
            final = None
            if verdict == "adjust":
                st, en = it.get("start") or None, it.get("end") or None
                if st is None and en is None:
                    errors.append({"where": iid, "message": "adjust needs start and/or end"})
                    continue
                try:
                    final = self.archive.timeline(s["recording_id"]).pair(st, en, base=p)
                except F.BoundaryError as e:
                    errors.append({"where": iid, "message": e.msg})
                    continue
            elif it.get("start") or it.get("end"):
                errors.append({"where": iid, "message": "start/end are only for adjust"})
                continue
            elif verdict == "accept":
                final = p
            meta = self.archive.load(s["recording_id"]) or {}
            fid = f"ce-{version}-{iid}"
            rec = {"key": verdict_key(s["recording_id"], fid, side["batch_id"]),
                   "recording_id": s["recording_id"], "fragment_id": fid, "type": s["type"],
                   "verdict": verdict, "reason_class": rc or None,
                   "reason": reason.strip() if isinstance(reason, str) else None,
                   "proposed": {k: p.get(k) for k in ("start_ms", "end_ms", "first_cue", "last_cue",
                                                     "title", "score", "applied")},
                   "final": None, "text": p.get("text"), "knowledge_version": version,
                   "boundary_mode": meta.get("boundary_mode"), "full_scan": False,
                   "source": "candidate_eval", "review_sha256": side["batch_id"],
                   "recorded_at": now_iso()}
            if final is not None:
                rec["final"] = {k: final[k] for k in ("start_ms", "end_ms", "first_cue", "last_cue")}
                rec["text"] = final.get("text") or rec["text"]
                if verdict == "adjust":
                    rec["shift_ms"] = {"start": final["start_ms"] - p["start_ms"],
                                       "end": final["end_ms"] - p["end_ms"]}
            recs.append(rec)
        open_ += [i for i in by_id if i not in seen and i not in open_]
        err_path = review.with_name(f"{version}.errors.yaml")
        if errors or open_:
            write_yaml(err_path, {"at": now_iso(), "errors": errors, "open": open_})
            return {"version": version, "imported": 0, "errors": errors, "open": open_}
        if err_path.exists():
            err_path.unlink()
        n = self.store.append_verdicts(recs)
        ht_path = self.dir / "human_time.jsonl"
        if not any(r.get("batch_id") == side["batch_id"] for r in _read_jsonl(ht_path)):
            try:
                st, fi = doc.get("started_at"), doc.get("finished_at")
                if st and fi:
                    a, b, src = parse_iso(str(st)), parse_iso(str(fi)), "reported"
                else:
                    a = parse_iso(side["created_at"])
                    b = datetime.fromtimestamp(review.stat().st_mtime, tz=timezone.utc)
                    src = "proxy"
                minutes = round(max(0.0, (b - a).total_seconds() / 60.0), 2)
            except (ValueError, TypeError):
                minutes, src = None, "unknown"
            append_line(ht_path, json.dumps({"at": now_iso(), "kind": "candidate_eval",
                                             "version": version, "batch_id": side["batch_id"],
                                             "minutes": minutes, "source": src}))
        return {"version": version, "imported": n, "errors": [], "open": []}

    # ---- decide -------------------------------------------------------------------------------------
    def _split_aggregate(self, m: dict[str, Any], rids: list[str]) -> dict[str, Any]:
        rs = set(rids)
        return MX.aggregate([u for u in m["units"] if u["recording_id"] in rs], m["recs"])

    def _budget(self, cand: dict[str, Any], base: dict[str, Any]) -> list[str]:
        b = self.settings["budget"]
        problems = []
        cp, bp = cand["overall"].get("prompt_chars"), base["overall"].get("prompt_chars")
        if cp and b.get("max_prompt_chars") and cp > float(b["max_prompt_chars"]):
            problems.append(f"prompt {cp} chars > {b['max_prompt_chars']}")
        if cp and bp and b.get("max_prompt_growth") is not None and cp > bp * (1 + float(b["max_prompt_growth"])):
            problems.append(f"prompt grows {cp} vs {bp} (> {b['max_prompt_growth']:.0%})")
        cc, bc = cand["overall"].get("cost_per_recording_min_usd"), base["overall"].get("cost_per_recording_min_usd")
        if cc and b.get("max_cost_per_recording_min_usd") and cc > float(b["max_cost_per_recording_min_usd"]):
            problems.append(f"cost {cc} $/min > {b['max_cost_per_recording_min_usd']}")
        if cc and bc and b.get("max_cost_growth") is not None and cc > bc * (1 + float(b["max_cost_growth"])):
            problems.append(f"cost grows {cc} vs {bc} $/min (> {b['max_cost_growth']:.0%})")
        return problems

    def decide(self, version: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Elitism: the candidate becomes active only if better on the fix-set and not worse on the
        holdout in ALL three groups, within budget. Otherwise rejected + rejected_hypotheses."""
        self._check_yardstick()
        version = version or self.default_candidate()
        st = self.store.state(version)
        if st.get("status") != "candidate":
            raise VPError("data", f"{version} is {st.get('status')}, not a candidate")
        active = self.store.active_version()
        fix, hold = self.measured_recordings()
        if len(fix) < int(self.settings["min_fix_recordings"]) or len(hold) < int(self.settings["min_holdout_recordings"]):
            raise VPError("data", f"not enough measured recordings: fix-set {len(fix)}, holdout {len(hold)}")
        rids = fix + hold
        vi = self.vindex()
        mc = R.measure(self.store, self.archive, vi, version, rids)
        mb = R.measure(self.store, self.archive, vi, active, rids)
        missing = sorted({f"{v}:{r}" for v, m in ((version, mc), (active, mb)) for r, _ in m["missing"]})
        if missing:
            raise VPError("data", f"replay missing for {missing}: run `learn replay {version}` "
                                  f"(versions are compared only on identical recordings)")
        tol = self.settings["tolerance"]
        agg = {"candidate": {"fix_set": self._split_aggregate(mc, fix), "holdout": self._split_aggregate(mc, hold),
                             "all": MX.aggregate(mc["units"], mc["recs"])},
               "baseline": {"fix_set": self._split_aggregate(mb, fix), "holdout": self._split_aggregate(mb, hold),
                            "all": MX.aggregate(mb["units"], mb["recs"])}}
        cmp_fix = MX.compare(agg["candidate"]["fix_set"], agg["baseline"]["fix_set"], tol)
        cmp_hold = MX.compare(agg["candidate"]["holdout"], agg["baseline"]["holdout"], tol)
        reasons = []
        worse_fix = [g for g, v in cmp_fix["groups"].items() if v == "worse"]
        if worse_fix:
            reasons.append(f"worse on the fix-set in {worse_fix}")
        elif "better" not in cmp_fix["groups"].values():
            reasons.append("not better on the fix-set in any group")
        worse_hold = [g for g, v in cmp_hold["groups"].items() if v == "worse"]
        if worse_hold:
            reasons.append(f"worse on the holdout in {worse_hold}")
        reasons += self._budget(agg["candidate"]["all"], agg["baseline"]["all"])
        accepted = not reasons
        uneval = agg["candidate"]["all"]["overall"]["unevaluated_share"]
        thr = float(self.settings["candidate_eval_threshold"])
        decision = {
            "result": "activated" if accepted else "rejected", "decided_at": now_iso(),
            "baseline": active, "yardstick": MX.YARDSTICK["version"],
            "recordings": {"fix_set": fix, "holdout": hold},
            "fix_set": cmp_fix["groups"], "holdout": cmp_hold["groups"], "reasons": reasons,
            "unevaluated_share": uneval,
            "candidate_eval_recommended": bool(uneval and uneval > thr),
        }
        passport = {"metrics": {"fix_set": agg["candidate"]["fix_set"], "holdout": agg["candidate"]["holdout"]},
                    "baseline_metrics": {"fix_set": agg["baseline"]["fix_set"],
                                         "holdout": agg["baseline"]["holdout"]},
                    "price": {"cost_per_recording_min_usd": agg["candidate"]["all"]["overall"]["cost_per_recording_min_usd"]},
                    "prompt_size": agg["candidate"]["all"]["overall"]["prompt_chars"],
                    "unevaluated_share": uneval, "decision": decision,
                    "comparison_details": {"fix_set": cmp_fix["details"], "holdout": cmp_hold["details"]}}
        out = {"version": version, **decision}
        if dry_run:
            return out
        self.store.set_state(version, passport=passport, note=f"S3c decision: {decision['result']}")
        if accepted:
            self.store.activate(version, by="s3c", reason="better on fix-set, not worse on holdout")
        else:
            self.store.set_state(version, status="rejected", note="; ".join(reasons))
            man = self.store.manifest(version)
            self.store.append_rejected_hypothesis({
                "version": version, "parent": man.get("parent"), "hypothesis": man.get("hypothesis"),
                "why": "; ".join(reasons), "sources": man.get("sources"),
                "fix_set": cmp_fix["groups"], "holdout": cmp_hold["groups"],
                "unevaluated_share": uneval})
        append_line(self.dir / "decisions.jsonl", json.dumps(out, ensure_ascii=False))
        return out

    # ---- observe ------------------------------------------------------------------------------------
    def observe(self, apply: bool = False) -> dict[str, Any]:
        active = self.store.active_version()
        prev = None
        for a in reversed(self.store.activations()):
            if a.get("action") == "activate" and a.get("to") == active:
                prev = a.get("from")
                break
        doc: dict[str, Any] = {"at": now_iso(), "active": active, "previous": prev}
        if not prev:
            doc.update({"recommendation": "none", "why": "no previous version"})
            write_yaml(self.dir / "observe.yaml", doc)
            return doc
        vi = self.vindex()
        ua, ra, rids_a = R.live_units(vi, self.archive, active)
        up, rp, rids_p = R.live_units(vi, self.archive, prev)
        need = int(self.settings["observe_min_recordings"])
        doc["recordings"] = {"active": rids_a, "previous": rids_p}
        if len(rids_a) < need or len(rids_p) < 1:
            doc.update({"recommendation": "wait", "why": f"too few live recordings "
                                                         f"({len(rids_a)} with {active}, need {need})"})
            write_yaml(self.dir / "observe.yaml", doc)
            return doc
        ma, mp = MX.aggregate(ua, ra), MX.aggregate(up, rp)
        cmp = MX.compare(ma, mp, self.settings["tolerance"], include_overall_load=True)
        worse = [g for g, v in cmp["groups"].items() if v == "worse"]
        doc.update({"groups": cmp["groups"], "metrics": {"active": ma, "previous": mp},
                    "recommendation": "rollback" if worse else "keep",
                    "why": f"live metrics worse in {worse}" if worse else "not worse on live verdicts"})
        if worse and apply:
            if self.store.manifest(active).get("parent") != prev:
                raise VPError("data", f"{active}'s parent is not {prev}; roll back manually")
            self.store.rollback(by="s3c.observe", reason=doc["why"])
            man = self.store.manifest(active)
            self.store.append_rejected_hypothesis({"version": active, "parent": prev,
                                                   "hypothesis": man.get("hypothesis"),
                                                   "why": f"observe: {doc['why']}", "groups": cmp["groups"]})
            doc["applied"] = True
        write_yaml(self.dir / "observe.yaml", doc)
        return doc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---- CLI ------------------------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vp learn", description="S3c learning loop of fragment selection")
    p.add_argument("--home", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="archive a reviewed job's subtitles for replay")
    s.add_argument("job")
    sub.add_parser("eval", help="live metrics per version")
    s = sub.add_parser("curve", help="version series on the same recordings")
    s.add_argument("--versions")
    s.add_argument("--recordings")
    sub.add_parser("critic")
    sub.add_parser("analyst")
    s = sub.add_parser("replay")
    s.add_argument("version", nargs="?")
    s.add_argument("--recordings")
    s.add_argument("--force", action="store_true")
    s.add_argument("--no-baseline", action="store_true")
    for name in ("export-eval", "import-eval"):
        s = sub.add_parser(name)
        s.add_argument("version")
        if name == "export-eval":
            s.add_argument("--force", action="store_true")
    s = sub.add_parser("decide")
    s.add_argument("version", nargs="?")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("observe")
    s.add_argument("--apply", action="store_true")
    s = sub.add_parser("splits")
    ss = s.add_subparsers(dest="splits_cmd", required=True)
    ss.add_parser("show")
    s2 = ss.add_parser("set")
    s2.add_argument("--fix-set", default="")
    s2.add_argument("--holdout", default="")
    return p


def main(argv: list[str] | None = None, out: Callable[[str], None] | None = None,
         judge: Any = None) -> int:
    out = out or print
    try:
        args = _parser().parse_args(list(argv or []))
    except SystemExit as e:
        return int(e.code or 0)
    try:
        lr = Learner(args.home, judge=judge)
        dump = lambda d: out(yaml.safe_dump(d, allow_unicode=True, sort_keys=False).rstrip())
        if args.cmd == "snapshot":
            m = lr.snapshot(args.job)
            out(f"snapshot {m['recording_id']} -> {lr.archive.path(m['recording_id'])} "
                f"({m['duration_ms'] / 60000:.1f} min, {m['boundary_mode']})")
        elif args.cmd == "eval":
            d = lr.eval()
            out(d.pop("table"))
            out(f"written {lr.dir / 'eval.yaml'}")
        elif args.cmd == "curve":
            d = lr.curve(_csv(args.versions) or None, _csv(args.recordings) or None)
            out(f"recordings: {len(d['recordings'])}")
            out(d.pop("table"))
            for v, why in d["excluded"].items():
                out(f"excluded {v}: {why}")
        elif args.cmd == "critic":
            d = lr.critic()
            out(f"{len(d['findings'])} findings over {d['verdicts']} verdicts")
            for f in d["findings"]:
                out(f"- [{f['type']}/{f['reason_class'] or '-'}] {f['title']} ({len(f['evidence'])} verdicts)")
        elif args.cmd == "analyst":
            dump(lr.analyst())
        elif args.cmd == "replay":
            d = lr.replay(args.version, _csv(args.recordings) or None, args.force, not args.no_baseline)
            out(f"replayed {d['version']} on {len(d['recordings'])} recordings")
        elif args.cmd == "export-eval":
            dump(lr.export_eval(args.version, args.force))
        elif args.cmd == "import-eval":
            d = lr.import_eval(args.version)
            dump(d)
            return 0 if not d["errors"] and not d["open"] else 2
        elif args.cmd == "decide":
            dump(lr.decide(args.version, args.dry_run))
        elif args.cmd == "observe":
            dump(lr.observe(args.apply))
        elif args.cmd == "splits":
            if args.splits_cmd == "show":
                dump(lr.splits() or {"fix_set": [], "holdout": []})
            else:
                dump(lr.splits_set(_csv(args.fix_set), _csv(args.holdout)))
    except VPError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3 if e.kind == "internal" else 2
    return 0
