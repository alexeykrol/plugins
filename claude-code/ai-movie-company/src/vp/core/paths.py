"""Paths of a job folder (SPEC §5, BUILD §2) and of the pipeline home directory.

All code addresses job files through `Job`; no step builds job paths by hand.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .io import read_yaml

FULL = "full"  # item id of the full video


def vp_home() -> Path:
    """Pipeline home: env VP_HOME, default ~/.ai_video_pipeline. Not created here."""
    env = os.environ.get("VP_HOME")
    return Path(env).expanduser() if env else Path.home() / ".ai_video_pipeline"


class Job:
    """A job folder. `root` is the folder containing request.yaml."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).expanduser().resolve()

    def __repr__(self) -> str:
        return f"Job({str(self.root)!r})"

    # ---- identity -------------------------------------------------------------------------
    @property
    def job_id(self) -> str:
        """request.yaml `job_id` if set, else "<parent folder>/<folder>" (ai_lms jobs are all
        named `_video`, so the parent — the session — makes the id unique)."""
        req = self.request()
        jid = req.get("job_id") if isinstance(req, dict) else None
        if jid:
            return str(jid)
        return f"{self.root.parent.name}/{self.root.name}"

    # ---- top-level files ------------------------------------------------------------------
    @property
    def request_yaml(self) -> Path: return self.root / "request.yaml"
    @property
    def job_yaml(self) -> Path: return self.root / "job.yaml"
    @property
    def status_yaml(self) -> Path: return self.root / "status.yaml"
    @property
    def lock_file(self) -> Path: return self.root / "job.lock"
    @property
    def s1_dir(self) -> Path: return self.root / "s1_proofread"
    @property
    def s1_cues(self) -> Path: return self.s1_dir / "cues.json"
    @property
    def s1_report(self) -> Path: return self.s1_dir / "report.json"
    def s1_in(self, n: int) -> Path: return self.s1_dir / f"in_{n:02d}.txt"
    def s1_out(self, n: int) -> Path: return self.s1_dir / f"out_{n:02d}.txt"
    @property
    def subtitles_srt(self) -> Path: return self.root / "subtitles.srt"
    @property
    def subtitles_debug(self) -> Path: return self.root / "subtitles.debug.json"
    @property
    def subtitles_rejected(self) -> Path: return self.root / "subtitles.srt.rejected.json"
    @property
    def script_md(self) -> Path: return self.root / "script.md"
    @property
    def fragments_proposed(self) -> Path: return self.root / "fragments.proposed.yaml"
    @property
    def fragments_review(self) -> Path: return self.root / "fragments.review.yaml"
    @property
    def fragments_errors(self) -> Path: return self.root / "fragments.errors.yaml"
    @property
    def fragments_yaml(self) -> Path: return self.root / "fragments.yaml"
    @property
    def uploads_yaml(self) -> Path: return self.root / "uploads.yaml"
    @property
    def crosslinks_yaml(self) -> Path: return self.root / "crosslinks.yaml"
    @property
    def package_dir(self) -> Path: return self.root / "package"
    def package(self, item: str) -> Path: return self.package_dir / f"{item}.yaml"
    def package_derived(self, item: str) -> Path: return self.package_dir / f"{item}.derived.yaml"
    @property
    def review_yaml(self) -> Path: return self.root / "review.yaml"
    @property
    def approval_yaml(self) -> Path: return self.root / "approval.yaml"
    @property
    def report_md(self) -> Path: return self.root / "report.md"
    @property
    def asks_yaml(self) -> Path: return self.root / "asks.yaml"
    @property
    def asks_md(self) -> Path: return self.root / "asks.md"
    @property
    def approval_shown(self) -> Path: return self.root / "approval.shown.yaml"
    @property
    def review_shown(self) -> Path: return self.root / "fragments.review.shown.yaml"
    @property
    def previews_dir(self) -> Path: return self.root / "previews"
    def preview_mp4(self, frag: str) -> Path: return self.previews_dir / f"{frag}.mp4"
    @property
    def stages_dir(self) -> Path: return self.root / "stages"
    @property
    def judge_root(self) -> Path: return self.root / "judge"
    def judge_dir(self, task: str) -> Path: return self.judge_root / task

    # ---- per-item files (item = fragment id or "full") -----------------------------------
    def item_dir(self, item: str) -> Path:
        """clips/<frag_id>/ for fragments, full/ for the full video."""
        if not item or "/" in item or item.startswith("."):
            raise ValueError(f"bad item id {item!r}")
        return self.root / FULL if item == FULL else self.root / "clips" / item
    def cut_mp4(self, item: str) -> Path: return self.item_dir(item) / "cut.mp4"
    def clip_srt(self, item: str) -> Path: return self.item_dir(item) / "clip.srt"
    def design_texts(self, item: str) -> Path: return self.item_dir(item) / "design_texts.yaml"
    def render_mp4(self, item: str) -> Path: return self.item_dir(item) / "render.mp4"
    def render_manifest(self, item: str) -> Path: return self.item_dir(item) / "render.manifest.yaml"
    def final_srt(self, item: str) -> Path: return self.item_dir(item) / "final.srt"
    def meta_yaml(self, item: str) -> Path: return self.item_dir(item) / "meta.yaml"
    def thumb_png(self, item: str) -> Path: return self.item_dir(item) / "thumb.png"

    def manifest_path(self, stage: str, item: str | None = None) -> Path:
        """stages/<stage>.yaml or stages/<stage>.<item>.yaml."""
        name = f"{stage}.{item}.yaml" if item else f"{stage}.yaml"
        return self.stages_dir / name

    # ---- request and sources --------------------------------------------------------------
    def request(self) -> dict[str, Any]:
        """Parsed request.yaml ({} if missing). Never modified by the pipeline."""
        data = read_yaml(self.request_yaml, default={})
        return data if isinstance(data, dict) else {}

    @property
    def design_yaml(self) -> Path: return self.root / "design.yaml"

    def design(self) -> dict[str, Any]:
        """Parsed design.yaml ({} if missing): the look the owner chose in the dialog
        (renderer, caption style), written by `vp design set`. See vp.design."""
        data = read_yaml(self.design_yaml, default={})
        return data if isinstance(data, dict) else {}

    def _source(self, key: str, default_name: str) -> Path:
        src = self.request().get("source") or {}
        value = src.get(key) if isinstance(src, dict) else None
        p = Path(value).expanduser() if value else Path(default_name)
        return p if p.is_absolute() else (self.root / p)

    @property
    def source_video(self) -> Path:
        """request.yaml source.video, default <job>/_cut.mp4 (read-only)."""
        return self._source("video", "_cut.mp4")

    @property
    def source_srt(self) -> Path:
        """request.yaml source.srt, default <job>/_cut.srt (read-only)."""
        return self._source("srt", "_cut.srt")

    @property
    def source_words(self) -> Path:
        """request.yaml source.words, default <job>/_cut.json (optional, read-only)."""
        return self._source("words", "_cut.json")

    # ---- items ----------------------------------------------------------------------------
    def items(self) -> list[str] | None:
        """Item ids = fragment ids of fragments.yaml + "full"; None while fragments.yaml is absent.

        fragments.yaml format: {fragments: [{id: ..., ...}, ...]}.
        """
        data = read_yaml(self.fragments_yaml, default=None)
        if data is None:
            return None
        frags = data.get("fragments") if isinstance(data, dict) else None
        ids: list[str] = []
        for f in frags or []:
            fid = str(f["id"])
            if fid == FULL or fid in ids:
                raise ValueError(f"fragments.yaml: bad or duplicate id {fid!r}")
            self.item_dir(fid)  # validates
            ids.append(fid)
        return ids + [FULL]

    # ---- relative paths -------------------------------------------------------------------
    def rel(self, path: str | os.PathLike) -> str:
        """Path relative to the job root (posix), or absolute string if outside the job."""
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(p.resolve())

    def abs(self, rel: str) -> Path:
        """Inverse of rel()."""
        p = Path(rel)
        return p if p.is_absolute() else self.root / p
