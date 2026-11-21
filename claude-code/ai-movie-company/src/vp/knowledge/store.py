"""Selection knowledge package store (SPEC S3 "Пакет знаний шага").

Layout under `<vp_home>/selection/`:

    active                      "vNNNN\\n" — pointer to the active version (changed only by
                                activate()/rollback())
    activations.jsonl           append-only journal of pointer changes {at, action, from, to, by, reason}
    versions/vNNNN/
      manifest.yaml             IMMUTABLE identity: id, parent, created_at, author (seed|analyst|...),
                                hypothesis, sources (verdict keys / case ids), content_hash, files
      state.yaml                MUTABLE: status (candidate|active|rejected|retired), passport
                                (metrics fix-set/holdout, price, prompt size), history of changes
      criteria/reel.md          criteria of a strong reel
      criteria/long.md          criteria of a long fragment
      rules.yaml                {rules: [{id, applies_to: [reel|long], kind: forbid|prefer, text, sources}]}
      cases/*.yaml              one case per file (see CASE_FIELDS)
      patterns.md               generalisations over cases
    verdicts.jsonl              every human verdict of every recording; append-only, deduplicated by `key`
    rejected_hypotheses.jsonl   append-only; mandatory input of the S3c analyst
    splits.yaml                 {version, holdout: [recording ids], train: [...], fix_set: [...]}
    replay/                     candidate measurements (owned by S3c)

Deviation from SPEC (documented in BUILD notes): the version's mutable part (status, passport) lives
in `state.yaml`, not in `manifest.yaml`, so that every byte of manifest + content stays immutable
after creation. `version_info(v)` returns both merged.

Immutability: content files are written once (staging dir + rename), made read-only, and their
sha256 is recorded in the manifest (`content_hash`); `verify(v)` recomputes it and `load_version`
refuses a tampered version.

Public API (for S3a/S3b and the S3c learning loop):

    store = KnowledgeStore(home)                 # home = vp_home(); root = home / "selection"
    store.ensure_seed() -> "v0001"               # installs assets/selection_seed once
    store.active_version() -> "vNNNN"            # ensures seed first
    store.list_versions() -> ["v0001", ...]
    store.version_info(v) -> {**manifest, "state": {...}}
    store.load_version(v) -> KnowledgeVersion    # criteria, rules, cases, patterns, content_hash
    store.create_version(files, parent=, author=, hypothesis=, sources=) -> "vNNNN"   # status candidate
    store.derive_version(parent, changes, removals=(), ...) -> "vNNNN"   # copy parent + edits
    store.set_state(v, status=None, passport=None, note=None)
    store.activate(v, by=, reason=) ; store.rollback(by=, reason=, status="rejected") -> parent id
    store.verify(v) -> bool
    store.append_verdicts(records) -> int (new lines) ; store.verdicts() ; store.verdict_keys()
    verdict_key(recording_id, fragment_id, review_hash) -> str
    store.append_rejected_hypothesis(record) ; store.rejected_hypotheses()
    store.read_splits() ; store.write_splits(data) ; store.holdout_recordings() -> set
    store.check_not_holdout(recording_ids, purpose)   # raises HoldoutLeak
    store.replay_dir
CLI: `python -m vp knowledge install-seed|list|show <v>|activate <v>|rollback|verify <v>` -> main(argv).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..core.errors import VPError, note_failure
from ..core.io import (append_line, atomic_write, now_iso, read_yaml, sha256_bytes, sha256_obj,
                       write_yaml)
from ..core.paths import vp_home

KINDS = ("reel", "long")
STATUSES = ("candidate", "active", "rejected", "retired")
CONTENT_FILES_REQUIRED = ("criteria/reel.md", "criteria/long.md", "rules.yaml", "patterns.md")
CASE_FIELDS = ("id", "type", "label", "text", "lesson", "recording")
# optional case fields: verdict, reason_class, reason, sources
CASE_LABELS = ("positive", "negative")
CASE_TYPES = ("reel", "long", "any")
RULE_KINDS = ("forbid", "prefer")
SEED_ID = "v0001"
_META_FILES = ("manifest.yaml", "state.yaml")

DEFAULT_SEED_DIR = Path(__file__).resolve().parents[3] / "assets" / "selection_seed"


class HoldoutLeak(VPError):
    """A verdict/case of a holdout recording reached a learning input (prompt builder, case
    library, analyst input). Always a hard stop."""

    def __init__(self, recordings: Iterable[str], purpose: str):
        recs = sorted(set(recordings))
        super().__init__("data", f"holdout recording(s) {recs} must not be used for {purpose}",
                         recordings=recs, purpose=purpose)
        self.recordings = recs
        self.purpose = purpose


def verdict_key(recording_id: str, fragment_id: str, review_hash: str) -> str:
    """Deduplication key of one verdict line: same recording + fragment + review file = same line."""
    return sha256_bytes(f"{recording_id}\x1f{fragment_id}\x1f{review_hash}")[:24]


def _vid(n: int) -> str:
    return f"v{n:04d}"


def _vnum(v: str) -> int:
    if not (isinstance(v, str) and len(v) == 5 and v[0] == "v" and v[1:].isdigit()):
        raise VPError("data", f"bad knowledge version id {v!r}")
    return int(v[1:])


def _content_files(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob("*") if p.is_file() and p.relative_to(d).as_posix() not in _META_FILES)


def content_hash(d: Path) -> str:
    """sha256 over (relative path, bytes) of every content file of a version dir."""
    items = [[p.relative_to(d).as_posix(), sha256_bytes(p.read_bytes())] for p in _content_files(d)]
    return sha256_obj(items)


@dataclass
class KnowledgeVersion:
    """Parsed content of one version."""

    id: str
    content_hash: str
    criteria: dict[str, str]                  # kind -> markdown
    rules: list[dict[str, Any]]
    cases: list[dict[str, Any]]
    patterns: str
    manifest: dict[str, Any] = field(default_factory=dict)

    def rules_for(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.rules if kind in (r.get("applies_to") or KINDS)]

    def cases_for(self, kind: str) -> list[dict[str, Any]]:
        return [c for c in self.cases if c["type"] in (kind, "any")]

    def ref_ids(self) -> set[str]:
        return {str(r["id"]) for r in self.rules} | {str(c["id"]) for c in self.cases}


def validate_content(files: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Validate version content given as {relpath: text}. Returns (rules, cases).
    Raises VPError(data) with the first problem found."""
    for req in CONTENT_FILES_REQUIRED:
        if req not in files:
            raise VPError("data", f"knowledge version lacks {req}")
    for rel in files:
        if rel.startswith("/") or ".." in Path(rel).parts or rel in _META_FILES:
            raise VPError("data", f"bad knowledge file path {rel!r}")
    try:
        rules_doc = yaml.safe_load(files["rules.yaml"]) or {}
    except yaml.YAMLError as e:
        raise VPError("data", f"rules.yaml: {e}") from e
    rules = rules_doc.get("rules") if isinstance(rules_doc, dict) else None
    if not isinstance(rules, list):
        raise VPError("data", "rules.yaml: expected {rules: [...]}")
    seen: set[str] = set()
    for r in rules:
        if not isinstance(r, dict) or not r.get("id") or not r.get("text"):
            raise VPError("data", f"rules.yaml: rule needs id and text: {r!r}")
        rid = str(r["id"])
        if rid in seen:
            raise VPError("data", f"rules.yaml: duplicate id {rid}")
        seen.add(rid)
        if r.get("kind", "forbid") not in RULE_KINDS:
            raise VPError("data", f"rule {rid}: kind must be one of {RULE_KINDS}")
        for k in r.get("applies_to") or KINDS:
            if k not in KINDS:
                raise VPError("data", f"rule {rid}: bad applies_to {k!r}")
    cases = []
    for rel in sorted(files):
        if not (rel.startswith("cases/") and rel.endswith(".yaml")):
            continue
        try:
            c = yaml.safe_load(files[rel])
        except yaml.YAMLError as e:
            raise VPError("data", f"{rel}: {e}") from e
        if not isinstance(c, dict):
            raise VPError("data", f"{rel}: case must be a mapping")
        for f_ in CASE_FIELDS:
            if c.get(f_) in (None, ""):
                raise VPError("data", f"{rel}: case lacks {f_}")
        if c["type"] not in CASE_TYPES:
            raise VPError("data", f"{rel}: type must be one of {CASE_TYPES}")
        if c["label"] not in CASE_LABELS:
            raise VPError("data", f"{rel}: label must be one of {CASE_LABELS}")
        cid = str(c["id"])
        if cid in seen:
            raise VPError("data", f"{rel}: duplicate id {cid}")
        seen.add(cid)
        c = dict(c)
        c["id"], c["recording"] = cid, str(c["recording"])
        cases.append(c)
    return rules, cases


class KnowledgeStore:
    """The selection knowledge package at `<home>/selection` (see module docstring)."""

    def __init__(self, home: str | os.PathLike | None = None, seed_dir: str | os.PathLike | None = None):
        self.home = Path(home).expanduser() if home else vp_home()
        self.root = self.home / "selection"
        self.seed_dir = Path(seed_dir) if seed_dir else DEFAULT_SEED_DIR

    # ---- paths ----------------------------------------------------------------------------
    @property
    def versions_dir(self) -> Path: return self.root / "versions"
    @property
    def active_file(self) -> Path: return self.root / "active"
    @property
    def activations_path(self) -> Path: return self.root / "activations.jsonl"
    @property
    def verdicts_path(self) -> Path: return self.root / "verdicts.jsonl"
    @property
    def rejected_path(self) -> Path: return self.root / "rejected_hypotheses.jsonl"
    @property
    def splits_path(self) -> Path: return self.root / "splits.yaml"
    @property
    def replay_dir(self) -> Path: return self.root / "replay"

    def version_dir(self, v: str) -> Path:
        _vnum(v)
        return self.versions_dir / v

    # ---- versions -------------------------------------------------------------------------
    def list_versions(self) -> list[str]:
        if not self.versions_dir.exists():
            return []
        out = []
        for p in self.versions_dir.iterdir():
            if p.is_dir() and (p / "manifest.yaml").exists():
                try:
                    _vnum(p.name)
                except VPError:
                    continue
                out.append(p.name)
        return sorted(out)

    def ensure_seed(self) -> str:
        """Install v0001 from the seed assets if the store has no versions; set `active` if unset.
        Idempotent. Returns the seed id."""
        if not self.list_versions():
            if not self.seed_dir.is_dir():
                raise VPError("internal", f"selection seed not found: {self.seed_dir}")
            files = {p.relative_to(self.seed_dir).as_posix(): p.read_text(encoding="utf-8")
                     for p in sorted(self.seed_dir.rglob("*")) if p.is_file() and p.name != ".DS_Store"}
            self._create(SEED_ID, files, parent=None, author="seed",
                         hypothesis="seed: owner's words 2026-11-16 («вкусный, эмоционально сильный "
                                    "кусочек») + donor prompt step1_select_segments.md",
                         sources=["SPEC R6", "process_transcript/prompts/step1_select_segments.md"],
                         status="active")
        if not self.active_file.exists():
            first = self.list_versions()[0]
            atomic_write(self.active_file, first + "\n")
            self._journal("install", None, first, "store", "seed installed")
        for d in (self.replay_dir,):
            d.mkdir(parents=True, exist_ok=True)
        return SEED_ID

    def active_version(self) -> str:
        """Id of the active version (installs the seed on first use)."""
        if not self.active_file.exists():
            self.ensure_seed()
        v = self.active_file.read_text(encoding="utf-8").strip()
        if v not in self.list_versions():
            raise VPError("data", f"active pointer names a missing version {v!r}")
        return v

    def manifest(self, v: str) -> dict[str, Any]:
        m = read_yaml(self.version_dir(v) / "manifest.yaml", default=None)
        if not isinstance(m, dict):
            raise VPError("data", f"knowledge version {v} not found")
        return m

    def state(self, v: str) -> dict[str, Any]:
        s = read_yaml(self.version_dir(v) / "state.yaml", default=None)
        if not isinstance(s, dict):
            raise VPError("data", f"knowledge version {v}: state.yaml missing")
        return s

    def version_info(self, v: str) -> dict[str, Any]:
        """Manifest + {"state": state.yaml} (status, passport, history)."""
        return {**self.manifest(v), "state": self.state(v)}

    def verify(self, v: str) -> bool:
        """True iff the content files still hash to the manifest's content_hash."""
        return content_hash(self.version_dir(v)) == self.manifest(v).get("content_hash")

    def load_version(self, v: str) -> KnowledgeVersion:
        d = self.version_dir(v)
        man = self.manifest(v)
        if not self.verify(v):
            raise VPError("data", f"knowledge version {v} was modified after creation (content hash mismatch)")
        files = {p.relative_to(d).as_posix(): p.read_text(encoding="utf-8") for p in _content_files(d)}
        rules, cases = validate_content(files)
        return KnowledgeVersion(id=v, content_hash=man["content_hash"],
                                criteria={k: files[f"criteria/{k}.md"] for k in KINDS},
                                rules=rules, cases=cases, patterns=files["patterns.md"], manifest=man)

    def read_files(self, v: str) -> dict[str, str]:
        """All content files of a version as {relpath: text} (input for derive_version)."""
        d = self.version_dir(v)
        return {p.relative_to(d).as_posix(): p.read_text(encoding="utf-8") for p in _content_files(d)}

    def create_version(self, files: dict[str, str], *, parent: str | None, author: str,
                       hypothesis: str, sources: list[str] | None = None) -> str:
        """Create a new immutable version (status `candidate`) from {relpath: text}. Returns its id.

        Cases from holdout recordings are rejected (HoldoutLeak): the case library is a learning
        input (SPEC S3c п. 4)."""
        if parent is not None and parent not in self.list_versions():
            raise VPError("data", f"parent version {parent} not found")
        _, cases = validate_content(files)
        self.check_not_holdout([c["recording"] for c in cases], "the case library")
        while True:
            existing = self.list_versions()
            vid = _vid((_vnum(existing[-1]) if existing else 0) + 1)
            try:
                self._create(vid, files, parent=parent, author=author, hypothesis=hypothesis,
                             sources=list(sources or []), status="candidate")
                return vid
            except FileExistsError:
                continue

    def derive_version(self, parent: str, changes: dict[str, str] | None = None,
                       removals: Iterable[str] = (), *, author: str, hypothesis: str,
                       sources: list[str] | None = None) -> str:
        """New version = parent's files, minus `removals`, with `changes` written over."""
        files = self.read_files(parent)
        for rel in removals:
            if rel not in files:
                raise VPError("data", f"derive_version: {rel} not in {parent}")
            del files[rel]
        files.update(changes or {})
        return self.create_version(files, parent=parent, author=author, hypothesis=hypothesis,
                                   sources=sources)

    def _create(self, vid: str, files: dict[str, str], *, parent: str | None, author: str,
                hypothesis: str, sources: list[str], status: str) -> None:
        validate_content(files)
        final = self.version_dir(vid)
        if final.exists():
            raise FileExistsError(vid)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        staging = self.versions_dir / f".{vid}.{os.getpid()}.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            for rel, text in files.items():
                p = staging / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            chash = content_hash(staging)
            write_yaml(staging / "manifest.yaml", {
                "id": vid, "parent": parent, "created_at": now_iso(), "author": author,
                "hypothesis": hypothesis, "sources": sources, "content_hash": chash,
                "files": sorted(files),
            })
            write_yaml(staging / "state.yaml", {
                "status": status, "passport": {},
                "history": [{"at": now_iso(), "status": status, "note": "created"}],
            })
            for p in staging.rglob("*"):
                if p.is_file() and p.name != "state.yaml":
                    p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            os.rename(staging, final)   # fails if another writer created it first
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            if final.exists():
                raise FileExistsError(vid) from e
            raise

    def set_state(self, v: str, status: str | None = None, passport: dict[str, Any] | None = None,
                  note: str | None = None) -> dict[str, Any]:
        """Update the mutable state of a version: status and/or passport (merged). Returns state."""
        st = self.state(v)
        if status is not None:
            if status not in STATUSES:
                raise VPError("data", f"bad status {status!r}")
            st["status"] = status
        if passport is not None:
            st["passport"] = {**(st.get("passport") or {}), **passport}
        st.setdefault("history", []).append({"at": now_iso(), "status": st["status"],
                                             "note": note or ("passport" if passport else "status")})
        write_yaml(self.version_dir(v) / "state.yaml", st)
        return st

    # ---- active pointer -------------------------------------------------------------------
    def _journal(self, action: str, frm: str | None, to: str, by: str, reason: str) -> None:
        append_line(self.activations_path, json.dumps(
            {"at": now_iso(), "action": action, "from": frm, "to": to, "by": by, "reason": reason},
            ensure_ascii=False))

    def activations(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.activations_path)

    def activate(self, v: str, by: str = "cli", reason: str = "") -> str:
        """Point `active` at `v` (must exist and verify). The previous active becomes `retired`.
        Policy (elitism, metrics) is the caller's (S3c) job. Returns the previous active id."""
        prev = self.active_version()
        if v not in self.list_versions():
            raise VPError("data", f"knowledge version {v} not found")
        if not self.verify(v):
            raise VPError("data", f"knowledge version {v} failed verification")
        if v == prev:
            return prev
        atomic_write(self.active_file, v + "\n")
        self.set_state(v, status="active", note=f"activated by {by}: {reason}")
        self.set_state(prev, status="retired", note=f"replaced by {v}")
        self._journal("activate", prev, v, by, reason)
        return prev

    def rollback(self, by: str = "cli", reason: str = "", status: str = "rejected") -> str:
        """Point `active` back at the parent of the active version (byte-for-byte pointer, the
        parent's files are untouched). The rolled-back version gets `status`. Returns the parent."""
        cur = self.active_version()
        parent = self.manifest(cur).get("parent")
        if not parent:
            raise VPError("data", f"{cur} has no parent to roll back to")
        if not self.verify(parent):
            raise VPError("data", f"parent {parent} failed verification")
        atomic_write(self.active_file, parent + "\n")
        self.set_state(parent, status="active", note=f"rollback from {cur} by {by}: {reason}")
        self.set_state(cur, status=status, note=f"rolled back by {by}: {reason}")
        self._journal("rollback", cur, parent, by, reason)
        return parent

    # ---- verdicts -------------------------------------------------------------------------
    def verdicts(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.verdicts_path)

    def verdict_keys(self) -> set[str]:
        return {str(r.get("key")) for r in self.verdicts()}

    def append_verdicts(self, records: list[dict[str, Any]]) -> int:
        """Append verdict records (each must carry `key`, `recording_id`, `knowledge_version`);
        lines whose key is already in the journal are skipped. Returns the number appended."""
        keys = self.verdict_keys()
        n = 0
        for r in records:
            for f_ in ("key", "recording_id", "knowledge_version", "fragment_id", "verdict"):
                if not r.get(f_):
                    raise VPError("internal", f"verdict record lacks {f_}")
            if r["key"] in keys:
                continue
            append_line(self.verdicts_path, json.dumps(r, ensure_ascii=False, sort_keys=False))
            keys.add(r["key"])
            n += 1
        return n

    # ---- rejected hypotheses --------------------------------------------------------------
    def append_rejected_hypothesis(self, record: dict[str, Any]) -> None:
        """Append {version?, hypothesis, why, ...}; `at` is added if absent."""
        if not record.get("hypothesis"):
            raise VPError("internal", "rejected hypothesis needs `hypothesis`")
        rec = {"at": now_iso(), **record}
        append_line(self.rejected_path, json.dumps(rec, ensure_ascii=False))

    def rejected_hypotheses(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.rejected_path)

    # ---- splits / holdout -----------------------------------------------------------------
    def read_splits(self) -> dict[str, Any]:
        data = read_yaml(self.splits_path, default={}) or {}
        if not isinstance(data, dict):
            raise VPError("data", "splits.yaml must be a mapping")
        return data

    def write_splits(self, data: dict[str, Any]) -> None:
        """Write splits.yaml. Lists of recording ids; holdout must not intersect any other list."""
        holdout = set(map(str, data.get("holdout") or []))
        for k, v in data.items():
            if isinstance(v, list) and k != "holdout":
                inter = holdout & set(map(str, v))
                if inter:
                    raise VPError("data", f"splits: holdout intersects {k}: {sorted(inter)}")
        write_yaml(self.splits_path, data)

    def holdout_recordings(self) -> set[str]:
        return set(map(str, self.read_splits().get("holdout") or []))

    def check_not_holdout(self, recording_ids: Iterable[str], purpose: str) -> None:
        """Raise HoldoutLeak if any id is a holdout recording."""
        leak = self.holdout_recordings() & set(map(str, recording_ids))
        if leak:
            raise HoldoutLeak(leak, purpose)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise VPError("data", f"{path.name}:{i}: bad JSON line") from e
    return out


# ---- CLI ------------------------------------------------------------------------------------

def main(argv: list[str] | None = None, out=None) -> int:
    """`vp knowledge <cmd>`: install-seed | list | show <v> | activate <v> | rollback | verify <v>."""
    out = out or (lambda s: print(s))
    p = argparse.ArgumentParser(prog="vp knowledge")
    p.add_argument("--home", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install-seed")
    sub.add_parser("list")
    for name in ("show", "activate", "verify"):
        s = sub.add_parser(name)
        s.add_argument("version")
        if name == "activate":
            s.add_argument("--reason", default="manual activation")
    s = sub.add_parser("rollback")
    s.add_argument("--reason", default="manual rollback")
    try:
        args = p.parse_args(list(argv or []))
    except SystemExit as e:
        return int(e.code or 0)
    store = KnowledgeStore(args.home)
    try:
        if args.cmd == "install-seed":
            store.ensure_seed()
            out(f"installed; active {store.active_version()}")
        elif args.cmd == "list":
            active = store.active_version()
            for v in store.list_versions():
                info = store.version_info(v)
                mark = "*" if v == active else " "
                out(f"{mark} {v}  {info['state'].get('status'):9}  parent={info.get('parent') or '-'}  "
                    f"author={info.get('author')}  {str(info.get('hypothesis') or '')[:70]}")
        elif args.cmd == "show":
            out(yaml.safe_dump(store.version_info(args.version), allow_unicode=True, sort_keys=False))
        elif args.cmd == "verify":
            ok = store.verify(args.version)
            out(f"{args.version}: {'ok' if ok else 'MODIFIED'}")
            return 0 if ok else 2
        elif args.cmd == "activate":
            prev = store.activate(args.version, by="cli", reason=args.reason)
            out(f"active {args.version} (was {prev})")
        elif args.cmd == "rollback":
            parent = store.rollback(by="cli", reason=args.reason)
            out(f"active {parent} (rolled back)")
    except VPError as e:
        note_failure(e)
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
