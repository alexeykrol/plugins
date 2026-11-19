"""Publisher interface (SPEC §7.8, BUILD §6). YouTube is the first implementation.

Contract highlights:
  * every upload starts PRIVATE;
  * the video key `vp_key` lives in the tags as `vp-<first 12 chars>` (see vp_tag);
  * update_snippet ALWAYS does read-modify-write of the full snippet (the platform deletes snippet
    fields that are not passed) and verifies the vp tag afterwards;
  * errors are PublisherError(kind auth|transport|data); UploadOutcomeUnknown means
    "the server may have accepted the upload" — never retried blindly (SPEC S8).
"""
from __future__ import annotations

import abc
import os
from typing import Any

from ..core.errors import VPError
from ..core.io import sha256_bytes

SNIPPET_FIELDS = ("title", "description", "tags", "categoryId", "defaultLanguage")

# YouTube Data API v3 unit costs of the raw calls (quota is per project per day).
API_COSTS: dict[str, int] = {
    "videos.insert": 1600, "videos.list": 1, "videos.update": 50, "captions.insert": 400,
    "captions.list": 50, "captions.update": 450, "captions.download": 200, "captions.delete": 50,
    "thumbnails.set": 50, "playlists.list": 1, "playlists.insert": 50, "playlistItems.list": 1,
    "playlistItems.insert": 50, "channels.list": 1, "search.list": 100,
}

# Cost of each Publisher operation = sum of the raw calls it makes (upper estimate).
QUOTA_COSTS: dict[str, int] = {
    "channel_id": API_COSTS["channels.list"],
    "start_upload": API_COSTS["videos.insert"],
    "resume_upload": 0,
    "upload_status": 0,
    "find_by_key": API_COSTS["channels.list"] + API_COSTS["playlistItems.list"] + API_COSTS["videos.list"],
    "get_video": API_COSTS["videos.list"],
    "update_snippet": API_COSTS["videos.list"] * 2 + API_COSTS["videos.update"],
    # set_captions: lookup (list) + insert; with a known track id: update; known absent: insert
    "set_captions": API_COSTS["captions.list"] + API_COSTS["captions.insert"],
    "set_captions.update": API_COSTS["captions.update"],
    "set_captions.insert": API_COSTS["captions.insert"],
    "get_captions": API_COSTS["captions.list"] + API_COSTS["captions.download"],
    "has_caption_track": API_COSTS["captions.list"],
    "set_thumbnail": API_COSTS["thumbnails.set"],
    "ensure_playlist": API_COSTS["playlists.list"] + API_COSTS["playlists.insert"],
    "add_to_playlist": API_COSTS["playlistItems.list"] + API_COSTS["playlistItems.insert"],
    "publish": API_COSTS["videos.list"] + API_COSTS["videos.update"],
}


def quota_cost(op: str) -> int:
    """Quota units of one Publisher operation (0 for unknown ops)."""
    return QUOTA_COSTS.get(op, 0)


# What one new item costs from upload to publish (S8 upload + captions + thumbnail + playlist,
# S10 description update, a few processing polls, PUB publish). The pipeline no longer
# downloads captions to verify them (recorded sha + videos.list contentDetails.caption).
PER_UPLOAD_OPS = ("start_upload", "set_captions.insert", "set_thumbnail", "add_to_playlist",
                  "update_snippet", "publish")
PER_UPLOAD_POLLS = 10         # get_video calls per item (processing polls, acceptance, S10, S9 x2, PUB)
PER_RUN_OPS = ("channel_id", "ensure_playlist")


def estimate_upload_units(new_uploads: int) -> int:
    """Quota units needed to take `new_uploads` items from upload to publish (0 -> 0)."""
    if new_uploads <= 0:
        return 0
    per_item = sum(quota_cost(op) for op in PER_UPLOAD_OPS) + PER_UPLOAD_POLLS * quota_cost("get_video")
    return new_uploads * per_item + sum(quota_cost(op) for op in PER_RUN_OPS)


def youtube_video_url(video_id: str, t_ms: int | None = None) -> str:
    """https://youtu.be/<id> (+ ?t=<seconds>)."""
    url = f"https://youtu.be/{video_id}"
    if t_ms:
        url += f"?t={int(t_ms) // 1000}"
    return url


def studio_url(video_id: str) -> str:
    """YouTube Studio editor of a video (where the owner sees a private draft)."""
    return f"https://studio.youtube.com/video/{video_id}/edit"


FAKE_URL_PREFIX = "fake://video/"


def fake_video_url(video_id: str, t_ms: int | None = None) -> str:
    """Rehearsal link: obviously not a YouTube address (U10)."""
    url = f"{FAKE_URL_PREFIX}{video_id}"
    if t_ms:
        url += f"?t={int(t_ms) // 1000}"
    return url


def make_vp_key(job_id: str, item: str, render_sha256: str) -> str:
    """vp_key = sha256(job id, item id, render.mp4 hash) as hex (SPEC S8)."""
    return sha256_bytes(f"{job_id}\n{item}\n{render_sha256}")


def vp_tag(vp_key: str) -> str:
    """Tag carrying the key on the platform: 'vp-' + first 12 hex chars."""
    if vp_key.startswith("vp-"):
        return vp_key
    return "vp-" + vp_key[:12]


class PublisherError(VPError):
    """Error from a publisher. kind: auth | transport | data."""

    def __init__(self, kind: str, msg: str, **details: Any):
        if kind not in ("auth", "transport", "data", "contract"):
            raise ValueError(f"bad publisher error kind {kind!r}")
        super().__init__(kind, msg, **details)


class UploadOutcomeUnknown(PublisherError):
    """The server may have accepted the upload but the client got no answer. Not retryable:
    S8 must go through upload_status / find_by_key before any new upload."""

    retryable_transport = False

    def __init__(self, msg: str, session_uri: str | None = None):
        super().__init__("transport", msg, session_uri=session_uri)
        self.session_uri = session_uri


class Publisher(abc.ABC):
    """Platform adapter. All methods may raise PublisherError."""

    name = "abstract"

    @abc.abstractmethod
    def channel_id(self) -> str:
        """Channel the credentials act on."""

    @abc.abstractmethod
    def start_upload(self, video_path: str | os.PathLike, snippet: dict[str, Any],
                     privacy: str = "private") -> str:
        """Open a resumable upload session; returns session_uri. Nothing is uploaded yet."""

    @abc.abstractmethod
    def resume_upload(self, session_uri: str, video_path: str | os.PathLike) -> str | None:
        """Send (the rest of) the bytes. Returns video_id when complete, None if not finished.
        May raise UploadOutcomeUnknown."""

    @abc.abstractmethod
    def upload_status(self, session_uri: str) -> dict[str, Any]:
        """{"state": "complete"|"incomplete"|"invalid", "video_id"?: str}"""

    @abc.abstractmethod
    def find_by_key(self, vp_key: str) -> str | None:
        """Search the channel's uploads for the tag vp_tag(vp_key); returns video_id or None."""

    @abc.abstractmethod
    def get_video(self, video_id: str) -> dict[str, Any] | None:
        """{"snippet": {title, description, tags, categoryId, defaultLanguage},
            "status": {privacyStatus, uploadStatus, publishAt?}, "processing": str,
            "has_captions": bool | None} or None. has_captions = the platform reports a caption
            track (YouTube contentDetails.caption; None = unknown)."""

    @abc.abstractmethod
    def update_snippet(self, video_id: str, changes: dict[str, Any]) -> None:
        """Read-modify-write of the full snippet with `changes` applied; verifies the vp tag."""

    @abc.abstractmethod
    def set_captions(self, video_id: str, srt_path: str | os.PathLike, language: str = "ru", *,
                     track_id: str | None = None, lookup: bool = True) -> str:
        """Upload (replace) captions; returns caption id. track_id given -> update that track;
        lookup=False -> the caller knows there is no track yet: insert without listing."""

    @abc.abstractmethod
    def get_captions(self, video_id: str, language: str = "ru") -> bytes | None:
        """Download captions as SRT bytes, or None. Expensive on YouTube (250 units): the
        pipeline does not call it; kept for manual checks."""

    def has_caption_track(self, video_id: str, track_id: str | None = None,
                          language: str = "ru") -> bool:
        """Is there a (non-ASR) caption track in `language` (with this id, if given)?
        One captions.list (50 units) on YouTube."""
        raise NotImplementedError

    def video_url(self, video_id: str, t_ms: int | None = None) -> str:
        """Public link of a video on this platform."""
        return youtube_video_url(video_id, t_ms)

    @abc.abstractmethod
    def set_thumbnail(self, video_id: str, png_path: str | os.PathLike) -> None:
        """Set the custom thumbnail."""

    @abc.abstractmethod
    def ensure_playlist(self, title: str) -> str:
        """Find or create a (private) playlist by title; returns playlist id."""

    @abc.abstractmethod
    def add_to_playlist(self, playlist_id: str, video_id: str) -> None:
        """Idempotent: no duplicate entry."""

    @abc.abstractmethod
    def publish(self, video_id: str, when: str) -> None:
        """when = "now" (private -> public) or an ISO datetime (status.publishAt)."""

    @staticmethod
    def quota_cost(op: str) -> int:
        return quota_cost(op)
