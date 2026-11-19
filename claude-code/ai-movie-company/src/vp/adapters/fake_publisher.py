"""FakePublisher — a file-backed imitation of YouTube for tests and offline runs (BUILD §6).

State lives in one JSON file, re-read before and written after every call, so a new process
(crash/restart tests) sees exactly what the "server" holds.

Failure injection (persisted in the state file; set with `inject(...)` or the constructor):
  auth_error=True                every call raises PublisherError("auth")
  transport_errors=n             the next n calls raise PublisherError("transport")
  fail_after_accept=n            the next n resume_upload calls create the video on the "server",
                                 then raise UploadOutcomeUnknown (server accepted, client got nothing)
  invalid_session=True           every session reports "invalid"; resume_upload raises data error
  processing_polls=n             get_video reports processing "processing" for the next n calls
  drop_tags_on_partial_update    forces the strict contract on (kept for BUILD §6 naming)
strict_snippet_contract (default True, as on YouTube): raw_update() deletes snippet fields not
passed. update_snippet() does the correct read-modify-write on top of it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..core.io import read_json, sha256_file, write_json
from ..core import progress
from .publisher import (SNIPPET_FIELDS, Publisher, PublisherError, UploadOutcomeUnknown,
                        fake_video_url, quota_cost, vp_tag)

_KNOBS = ("auth_error", "transport_errors", "fail_after_accept", "invalid_session",
          "processing_polls", "drop_tags_on_partial_update")


def _empty_state(channel: str) -> dict[str, Any]:
    return {"channel": channel, "seq": 0, "sessions": {}, "videos": {}, "playlists": {},
            "captions": {}, "thumbnails": {}, "quota_used": 0, "calls": [],
            "injections": {k: 0 for k in _KNOBS}}


class FakePublisher(Publisher):
    """Deterministic fake: video ids FV000000001…, sessions fake://upload/<n>, links
    fake://video/<id> (a rehearsal must never look like a real upload, U10)."""

    name = "fake"

    def video_url(self, video_id: str, t_ms: int | None = None) -> str:
        return fake_video_url(video_id, t_ms)

    def __init__(self, state_path: str | os.PathLike, channel: str = "UCfakechannel000000000001",
                 strict_snippet_contract: bool = True, costs: Any = None, **injections: Any):
        self.path = Path(state_path)
        self.strict_snippet_contract = strict_snippet_contract
        self.costs = costs
        if not self.path.exists():
            write_json(self.path, _empty_state(channel))
        if injections:
            self.inject(**injections)

    # ---- state ----------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return read_json(self.path)

    def _save(self, st: dict[str, Any]) -> None:
        write_json(self.path, st)

    def inject(self, **knobs: Any) -> None:
        """Set failure injection knobs (persisted)."""
        st = self.state()
        for k, v in knobs.items():
            if k not in _KNOBS:
                raise ValueError(f"unknown injection {k!r}")
            st["injections"][k] = int(v) if not isinstance(v, bool) else v
        self._save(st)

    def _call(self, op: str) -> dict[str, Any]:
        """Common prologue: load state, account quota, apply auth/transport injections."""
        st = self.state()
        inj = st["injections"]
        st["calls"].append(op)
        if inj.get("auth_error"):
            self._save(st)
            raise PublisherError("auth", "fake: credentials rejected")
        if inj.get("transport_errors", 0) > 0:
            inj["transport_errors"] -= 1
            self._save(st)
            raise PublisherError("transport", f"fake: connection reset during {op}")
        units = quota_cost(op)
        st["quota_used"] += units
        if self.costs is not None and units:
            self.costs.add("youtube_quota", op=op, units=units, model="fake")
        return st

    def _strict(self, st: dict[str, Any]) -> bool:
        return self.strict_snippet_contract or bool(st["injections"].get("drop_tags_on_partial_update"))

    def _video(self, st: dict[str, Any], video_id: str) -> dict[str, Any]:
        v = st["videos"].get(video_id)
        if v is None:
            raise PublisherError("data", f"fake: video {video_id} not found")
        return v

    # ---- Publisher ------------------------------------------------------------------------
    def channel_id(self) -> str:
        st = self._call("channel_id")
        self._save(st)
        return st["channel"]

    def start_upload(self, video_path, snippet, privacy="private") -> str:
        st = self._call("start_upload")
        if not Path(video_path).exists():
            self._save(st)
            raise PublisherError("data", f"fake: video file missing: {video_path}")
        st["seq"] += 1
        uri = f"fake://upload/{st['seq']}"
        st["sessions"][uri] = {"snippet": {k: snippet.get(k) for k in SNIPPET_FIELDS if k in snippet},
                               "privacy": privacy, "file_sha": sha256_file(video_path),
                               "state": "incomplete", "video_id": None}
        self._save(st)
        return uri

    def _create_video(self, st: dict[str, Any], sess: dict[str, Any]) -> str:
        st["seq"] += 1
        vid = f"FV{st['seq']:09d}"
        snippet = {"title": "", "description": "", "tags": [], "categoryId": "27",
                   "defaultLanguage": "ru"}
        snippet.update(sess["snippet"])
        st["videos"][vid] = {"snippet": snippet,
                             "status": {"privacyStatus": sess["privacy"], "uploadStatus": "processed"},
                             "file_sha": sess["file_sha"], "channel": st["channel"]}
        sess["state"] = "complete"
        sess["video_id"] = vid
        return vid

    def resume_upload(self, session_uri, video_path) -> str | None:
        st = self._call("resume_upload")
        sess = st["sessions"].get(session_uri)
        if sess is None or st["injections"].get("invalid_session"):
            self._save(st)
            raise PublisherError("data", "fake: upload session invalid")
        if sess["state"] == "complete":
            self._save(st)
            return sess["video_id"]
        size = Path(video_path).stat().st_size if Path(video_path).exists() else 0
        progress.report(size, size or 1, "bytes", force=True)
        vid = self._create_video(st, sess)
        if st["injections"].get("fail_after_accept", 0) > 0:
            st["injections"]["fail_after_accept"] -= 1
            self._save(st)
            raise UploadOutcomeUnknown("fake: server accepted, response lost", session_uri)
        self._save(st)
        return vid

    def upload_status(self, session_uri) -> dict[str, Any]:
        st = self._call("upload_status")
        self._save(st)
        sess = st["sessions"].get(session_uri)
        if sess is None or st["injections"].get("invalid_session"):
            return {"state": "invalid"}
        if sess["state"] == "complete":
            return {"state": "complete", "video_id": sess["video_id"]}
        return {"state": "incomplete"}

    def find_by_key(self, vp_key) -> str | None:
        st = self._call("find_by_key")
        self._save(st)
        tag = vp_tag(vp_key)
        for vid, v in st["videos"].items():
            if v.get("channel") == st["channel"] and tag in (v["snippet"].get("tags") or []):
                return vid
        return None

    def get_video(self, video_id) -> dict[str, Any] | None:
        st = self._call("get_video")
        v = st["videos"].get(video_id)
        processing = "succeeded"
        if st["injections"].get("processing_polls", 0) > 0:
            st["injections"]["processing_polls"] -= 1
            processing = "processing"
        self._save(st)
        if v is None:
            return None
        has_caps = any(c.get("video_id") == video_id for c in st["captions"].values())
        return {"snippet": dict(v["snippet"]), "status": dict(v["status"]), "processing": processing,
                "has_captions": has_caps}

    def raw_update(self, video_id: str, snippet: dict[str, Any]) -> None:
        """Imitates videos.update(part=snippet): with the strict contract, fields not passed are
        DELETED (tags, description, defaultLanguage); title and categoryId are required."""
        st = self.state()
        st["calls"].append("videos.update")
        st["quota_used"] += 50
        v = self._video(st, video_id)
        if self._strict(st):
            if not snippet.get("title") or not snippet.get("categoryId"):
                self._save(st)
                raise PublisherError("data", "fake: videos.update requires title and categoryId")
            v["snippet"] = {k: snippet[k] for k in SNIPPET_FIELDS if k in snippet}
            v["snippet"].setdefault("tags", [])
            v["snippet"].setdefault("description", "")
        else:
            v["snippet"].update({k: snippet[k] for k in SNIPPET_FIELDS if k in snippet})
        self._save(st)

    def update_snippet(self, video_id, changes) -> None:
        unknown = set(changes) - set(SNIPPET_FIELDS)
        if unknown:
            raise PublisherError("data", f"fake: unknown snippet fields {sorted(unknown)}")
        st = self._call("update_snippet")
        self._save(st)
        cur = dict(self._video(st, video_id)["snippet"])
        before_tags = [t for t in cur.get("tags") or [] if t.startswith("vp-")]
        cur.update(changes)
        self.raw_update(video_id, cur)
        after = self._video(self.state(), video_id)["snippet"]
        for t in before_tags:
            if t not in (after.get("tags") or []):
                raise PublisherError("data", f"fake: vp tag {t} lost after update of {video_id}")

    def set_captions(self, video_id, srt_path, language="ru", *, track_id=None, lookup=True) -> str:
        op = ("set_captions.update" if track_id else
              "set_captions" if lookup else "set_captions.insert")
        st = self._call(op)
        self._video(st, video_id)
        if track_id and track_id not in st["captions"]:
            self._save(st)
            raise PublisherError("data", f"fake: caption track {track_id} not found")
        data = Path(srt_path).read_bytes()
        cid = f"CAP-{video_id}-{language}"
        st["captions"][cid] = {"video_id": video_id, "language": language,
                               "data": data.decode("utf-8")}
        self._save(st)
        return cid

    def get_captions(self, video_id, language="ru") -> bytes | None:
        st = self._call("get_captions")
        self._save(st)
        cap = st["captions"].get(f"CAP-{video_id}-{language}")
        return cap["data"].encode("utf-8") if cap else None

    def has_caption_track(self, video_id, track_id=None, language="ru") -> bool:
        st = self._call("has_caption_track")
        self._save(st)
        for cid, cap in st["captions"].items():
            if cap["video_id"] == video_id and cap["language"] == language \
                    and (track_id is None or cid == track_id):
                return True
        return False

    def delete_captions(self, video_id, language="ru") -> None:
        """Test helper: the track disappears on the platform (e.g. removed in Studio)."""
        st = self.state()
        st["captions"].pop(f"CAP-{video_id}-{language}", None)
        self._save(st)

    def set_thumbnail(self, video_id, png_path) -> None:
        st = self._call("set_thumbnail")
        self._video(st, video_id)
        p = Path(png_path)
        if not p.exists():
            self._save(st)
            raise PublisherError("data", f"fake: thumbnail missing: {p}")
        if p.stat().st_size >= 2 * 1024 * 1024:
            self._save(st)
            raise PublisherError("data", "fake: thumbnail must be < 2 MB")
        st["thumbnails"][video_id] = sha256_file(p)
        self._save(st)

    def ensure_playlist(self, title) -> str:
        st = self._call("ensure_playlist")
        for pid, pl in st["playlists"].items():
            if pl["title"] == title:
                self._save(st)
                return pid
        st["seq"] += 1
        pid = f"FPL{st['seq']:09d}"
        st["playlists"][pid] = {"title": title, "items": [], "privacy": "private"}
        self._save(st)
        return pid

    def add_to_playlist(self, playlist_id, video_id) -> None:
        st = self._call("add_to_playlist")
        pl = st["playlists"].get(playlist_id)
        if pl is None:
            self._save(st)
            raise PublisherError("data", f"fake: playlist {playlist_id} not found")
        self._video(st, video_id)
        if video_id not in pl["items"]:
            pl["items"].append(video_id)
        self._save(st)

    def publish(self, video_id, when) -> None:
        st = self._call("publish")
        v = self._video(st, video_id)
        if when == "now":
            v["status"]["privacyStatus"] = "public"
            v["status"].pop("publishAt", None)
        else:
            v["status"]["privacyStatus"] = "private"
            v["status"]["publishAt"] = str(when)
        self._save(st)

    # ---- test helpers ---------------------------------------------------------------------
    def videos(self) -> dict[str, Any]:
        """All videos on the fake channel (for assertions: duplicates, privacy, tags)."""
        return self.state()["videos"]

    def quota_used(self) -> int:
        return int(self.state()["quota_used"])
