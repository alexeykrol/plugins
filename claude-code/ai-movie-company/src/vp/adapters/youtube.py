"""YouTubePublisher — the Publisher interface on YouTube Data API v3 (BUILD §6, SPEC S8–S10).

Google client libraries are imported lazily (only when this adapter is used).

Credentials: an authorized-user JSON token at config `youtube.token_path` (refreshed when
expired; the refreshed token is written back atomically with mode 0600). Contents of token and
client-secrets files are never logged, printed or put into errors. There is no interactive
login at runtime: a missing/invalid token is an `auth` error. One-time setup (owner, terminal):
    python -m vp.adapters.youtube authorize            (browser login, writes token_path)
    python -m vp.adapters.youtube convert-token <legacy.pickle>   (Legacy pickle -> JSON token)

Uploads: our own resumable protocol on the authorized HTTP object, so that opening the session
(start_upload -> session_uri, saved by S8 BEFORE any byte is sent) and sending bytes
(resume_upload) are separate steps:
  start   POST upload URL (X-Upload-Content-*) -> Location = session_uri
  status  PUT session_uri, Content-Range: bytes */<size|*>
          308 (+Range) incomplete | 200/201 complete (video id) | 404/410 invalid
  chunks  PUT session_uri, Content-Range: bytes a-b/size; a failure (exception or 5xx) while the
          FINAL chunk is in flight raises UploadOutcomeUnknown (the server may have the video).

Every snippet/status change is read-modify-write (videos.update deletes fields not passed);
after a snippet update the vp-… tags are verified. Errors: PublisherError(auth|transport|data);
quota exhaustion = non-retryable transport (QuotaExceeded).

Config (youtube.*): token_path, client_secrets_path, channel_id, chunk_size_mb (8),
uploads_scan_pages (4), playlist_scan_pages (10), timeout_s (300).
"""
from __future__ import annotations

import json
import os
import stat
import sys
from datetime import timezone
from pathlib import Path
from typing import Any, Callable

from ..core import progress
from ..core.io import atomic_write, parse_iso
from .publisher import (API_COSTS, Publisher, PublisherError, UploadOutcomeUnknown,
                        SNIPPET_FIELDS)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube",
]
SNIPPET_WRITABLE = SNIPPET_FIELDS + ("defaultAudioLanguage",)
STATUS_WRITABLE = ("privacyStatus", "embeddable", "license", "publicStatsViewable", "publishAt",
                   "selfDeclaredMadeForKids", "containsSyntheticMedia")
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
_AUTH_REASONS = {"authError", "insufficientPermissions", "forbidden", "youtubeSignupRequired",
                 "channelNotFound" }
UNIT = 256 * 1024
# Google OAuth error codes the owner can fix by hand (explain_auth_problem)
AUTH_PROBLEMS = ("invalid_grant", "access_denied")


class QuotaExceeded(PublisherError):
    """Daily quota exhausted: stop now, continue tomorrow (not retried inside the step)."""

    retryable_transport = False

    def __init__(self, msg: str):
        super().__init__("transport", msg)


# ---- error classification -------------------------------------------------------------------

def _reason(content: Any) -> str:
    try:
        data = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
        err = data.get("error") or {}
        errs = err.get("errors") or []
        if errs and errs[0].get("reason"):
            return str(errs[0]["reason"])
        return str(err.get("status") or "")
    except Exception:
        return ""


def classify_status(status: int, content: Any, op: str) -> PublisherError:
    """PublisherError for an HTTP status (+ API error reason)."""
    reason = _reason(content)
    msg = f"youtube {op}: HTTP {status}" + (f" {reason}" if reason else "")
    if status == 401:
        return PublisherError("auth", msg)
    if status == 403:
        if reason in _QUOTA_REASONS:
            return QuotaExceeded(msg)
        if reason in _RATE_REASONS:
            return PublisherError("transport", msg)
        if reason in _AUTH_REASONS:
            return PublisherError("auth", msg)
        return PublisherError("data", msg)
    if status == 429 or status >= 500:
        return PublisherError("transport", msg)
    return PublisherError("data", msg)


def classify_exception(e: BaseException, op: str) -> PublisherError:
    """PublisherError for any exception raised by the client stack (never leaks contents)."""
    if isinstance(e, PublisherError):
        return e
    try:
        from googleapiclient.errors import HttpError
        if isinstance(e, HttpError):
            return classify_status(int(e.resp.status), e.content, op)
    except ImportError:  # pragma: no cover
        pass
    try:
        from google.auth import exceptions as gae
        if isinstance(e, gae.RefreshError):
            first = str(e.args[0]) if e.args else ""
            problem = next((p for p in AUTH_PROBLEMS if p in first), None)
            return PublisherError("auth", f"youtube {op}: token refresh failed"
                                  + (f" ({problem})" if problem else ""))
        if isinstance(e, gae.TransportError):
            return PublisherError("transport", f"youtube {op}: {type(e).__name__}")
    except ImportError:  # pragma: no cover
        pass
    try:
        import httplib2
        if isinstance(e, httplib2.HttpLib2Error):
            return PublisherError("transport", f"youtube {op}: {type(e).__name__}")
    except ImportError:  # pragma: no cover
        pass
    if isinstance(e, (OSError, TimeoutError, ConnectionError)):
        return PublisherError("transport", f"youtube {op}: {type(e).__name__}")
    return PublisherError("data", f"youtube {op}: {type(e).__name__}")


# ---- credentials ----------------------------------------------------------------------------

def load_credentials(token_path: str | None) -> Any:
    """Authorized-user credentials from JSON; refreshed if expired. Raises PublisherError(auth)."""
    if not token_path:
        raise PublisherError("auth", "youtube.token_path is not configured")
    p = Path(token_path).expanduser()
    if not p.exists():
        raise PublisherError("auth", "youtube token file not found at the configured path; run "
                                     "`python -m vp.adapters.youtube authorize`")
    from google.oauth2.credentials import Credentials
    try:
        creds = Credentials.from_authorized_user_file(str(p), SCOPES)
    except (ValueError, json.JSONDecodeError):
        raise PublisherError("auth", "youtube token file is not a valid authorized-user JSON "
                                     "(convert a Legacy pickle with `convert-token`)") from None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        try:
            creds.refresh(Request())
        except Exception as e:
            raise classify_exception(e, "token refresh") from None
        _write_token(p, creds.to_json())
        return creds
    raise PublisherError("auth", "youtube token is invalid and cannot be refreshed; re-authorize")


def _write_token(path: Path, text: str) -> None:
    atomic_write(path, text)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


# ---- the publisher --------------------------------------------------------------------------

class YouTubePublisher(Publisher):
    name = "youtube"

    def __init__(self, config: dict[str, Any] | None = None, costs: Any = None, *,
                 service: Any = None, http: Any = None):
        """config = the `youtube` config section. Tests pass `service` (built over an
        HttpMockSequence); `http` defaults to the service's authorized HTTP object."""
        self.cfg = dict(config or {})
        self.costs = costs
        self._service = service
        self._http_obj = http
        self._channel: str | None = None
        self._uploads_playlist: str | None = None
        self._playlists: dict[str, str] = {}
        mb = float(self.cfg.get("chunk_size_mb") or 8)
        self.chunk_size = max(UNIT, int(mb * 1024 * 1024) // UNIT * UNIT)

    # ---- plumbing ----------------------------------------------------------------------
    def svc(self) -> Any:
        if self._service is None:
            from googleapiclient.discovery import build
            creds = load_credentials(self.cfg.get("token_path"))
            try:
                self._service = build("youtube", "v3", credentials=creds, cache_discovery=False,
                                      static_discovery=True)
            except Exception as e:
                raise classify_exception(e, "build") from None
        return self._service

    def http(self) -> Any:
        return self._http_obj if self._http_obj is not None else self.svc()._http

    def _quota(self, api: str) -> None:
        units = API_COSTS.get(api, 0)
        if self.costs is not None and units:
            self.costs.add("youtube_quota", op=api, units=units, model="youtube")

    def _exec(self, api: str, make: Callable[[Any], Any]) -> Any:
        """Build a request with the service and execute it; errors classified; quota logged."""
        self._quota(api)
        try:
            return make(self.svc()).execute(num_retries=0)
        except PublisherError:
            raise
        except Exception as e:
            raise classify_exception(e, api) from None

    def _raw(self, op: str, uri: str, method: str, body: Any = None,
             headers: dict[str, str] | None = None) -> tuple[Any, bytes]:
        try:
            resp, content = self.http().request(uri, method=method, body=body, headers=headers or {})
        except Exception as e:
            raise classify_exception(e, op) from None
        return resp, content if isinstance(content, bytes) else (content or "").encode("utf-8")

    # ---- Publisher -------------------------------------------------------------------------
    def channel_id(self) -> str:
        if self._channel is None:
            data = self._exec("channels.list", lambda s: s.channels().list(part="id,contentDetails",
                                                                              mine=True))
            items = data.get("items") or []
            if not items:
                raise PublisherError("auth", "youtube: the token has no channel")
            self._channel = str(items[0]["id"])
            self._uploads_playlist = ((items[0].get("contentDetails") or {})
                                      .get("relatedPlaylists") or {}).get("uploads")
        return self._channel

    def channel_info(self) -> dict[str, Any]:
        """{"id", "title", "handle"} of the channel the token acts on (one channels.list, 1 unit)."""
        data = self._exec("channels.list", lambda s: s.channels().list(
            part="id,snippet,contentDetails", mine=True))
        items = data.get("items") or []
        if not items:
            raise PublisherError("auth", "youtube: the token has no channel")
        it = items[0]
        sn = it.get("snippet") or {}
        self._channel = str(it["id"])
        self._uploads_playlist = ((it.get("contentDetails") or {}).get("relatedPlaylists")
                                  or {}).get("uploads")
        return {"id": self._channel, "title": sn.get("title"), "handle": sn.get("customUrl")}

    def start_upload(self, video_path, snippet, privacy="private") -> str:
        if privacy != "private":
            raise PublisherError("data", "uploads are private by contract")
        from googleapiclient.http import MediaFileUpload
        path = Path(video_path)
        if not path.exists():
            raise PublisherError("data", f"video file missing: {path.name}")
        sn = {k: snippet[k] for k in SNIPPET_WRITABLE if k in snippet}
        if sn.get("defaultLanguage") and "defaultAudioLanguage" not in sn:
            sn["defaultAudioLanguage"] = sn["defaultLanguage"]
        body = {"snippet": sn,
                "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                           "embeddable": True, "license": "youtube"}}
        media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True,
                                chunksize=self.chunk_size)
        req = self.svc().videos().insert(part="snippet,status", body=body, media_body=media)
        headers = dict(req.headers)
        headers["X-Upload-Content-Type"] = "video/mp4"
        headers["X-Upload-Content-Length"] = str(path.stat().st_size)
        headers["content-length"] = str(len(req.body or ""))
        self._quota("videos.insert")
        resp, content = self._raw("videos.insert", req.uri, "POST", req.body, headers)
        status = int(resp.status)
        location = resp.get("location")
        if status == 200 and location:
            return str(location)
        raise classify_status(status, content, "videos.insert")

    def _query(self, session_uri: str, size: int | None) -> dict[str, Any]:
        total = str(size) if size is not None else "*"
        resp, content = self._raw("upload.status", session_uri, "PUT", b"",
                                  {"Content-Range": f"bytes */{total}", "Content-Length": "0"})
        status = int(resp.status)
        if status in (200, 201):
            vid = _video_id(content)
            if not vid:
                raise PublisherError("contract", "upload complete but no video id in the response")
            return {"state": "complete", "video_id": vid}
        if status == 308:
            rng = resp.get("range")
            offset = int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
            return {"state": "incomplete", "offset": offset}
        if status in (404, 410):
            return {"state": "invalid"}
        raise classify_status(status, content, "upload.status")

    def upload_status(self, session_uri) -> dict[str, Any]:
        st = self._query(str(session_uri), None)
        st.pop("offset", None)
        return st

    def resume_upload(self, session_uri, video_path) -> str | None:
        path = Path(video_path)
        size = path.stat().st_size
        st = self._query(str(session_uri), size)
        if st["state"] == "complete":
            return st["video_id"]
        if st["state"] == "invalid":
            raise PublisherError("data", "upload session invalid or expired")
        offset = int(st["offset"])
        stuck = 0
        since = progress.clock()
        progress.report(offset, size, "bytes", since=since, force=True)
        with open(path, "rb") as f:
            while True:
                f.seek(offset)
                chunk = f.read(self.chunk_size)
                end = offset + len(chunk) - 1
                final = end >= size - 1
                headers = {"Content-Range": f"bytes {offset}-{end}/{size}",
                           "Content-Length": str(len(chunk))}
                try:
                    resp, content = self._raw("upload.chunk", str(session_uri), "PUT", chunk, headers)
                except PublisherError as e:
                    if final and e.kind == "transport":
                        raise UploadOutcomeUnknown("response to the final chunk was lost",
                                                   str(session_uri)) from None
                    raise
                status = int(resp.status)
                if status in (200, 201):
                    vid = _video_id(content)
                    if not vid:
                        raise UploadOutcomeUnknown("final response has no video id", str(session_uri))
                    return vid
                if status == 308:
                    rng = resp.get("range")
                    new = int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
                    stuck = stuck + 1 if new <= offset else 0
                    if stuck >= 3:
                        raise PublisherError("transport", "upload makes no progress")
                    offset = new
                    progress.report(offset, size, "bytes", since=since)
                    continue
                if status in (404, 410):
                    raise PublisherError("data", "upload session invalid or expired")
                if final and (status >= 500 or status == 429):
                    raise UploadOutcomeUnknown(f"final chunk answered HTTP {status}", str(session_uri))
                raise classify_status(status, content, "upload.chunk")

    def find_by_key(self, vp_key) -> str | None:
        from .publisher import vp_tag
        tag = vp_tag(vp_key)
        self.channel_id()
        uploads = self._uploads_playlist
        if not uploads:
            raise PublisherError("data", "youtube: channel has no uploads playlist")
        token = None
        for _ in range(int(self.cfg.get("uploads_scan_pages") or 4)):
            kw = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
            if token:
                kw["pageToken"] = token
            page = self._exec("playlistItems.list", lambda s: s.playlistItems().list(**kw))
            ids = [it["contentDetails"]["videoId"] for it in page.get("items") or []
                   if (it.get("contentDetails") or {}).get("videoId")]
            if ids:
                vids = self._exec("videos.list", lambda s: s.videos().list(
                    part="snippet", id=",".join(ids), maxResults=50))
                for v in vids.get("items") or []:
                    if tag in ((v.get("snippet") or {}).get("tags") or []):
                        return str(v["id"])
            token = page.get("nextPageToken")
            if not token:
                break
        return None

    def get_video(self, video_id) -> dict[str, Any] | None:
        data = self._exec("videos.list", lambda s: s.videos().list(
            part="snippet,status,processingDetails,contentDetails", id=str(video_id)))
        items = data.get("items") or []
        if not items:
            return None
        v = items[0]
        sn = v.get("snippet") or {}
        st = v.get("status") or {}
        proc = (v.get("processingDetails") or {}).get("processingStatus")
        if not proc:
            proc = "succeeded" if st.get("uploadStatus") == "processed" else str(st.get("uploadStatus") or "unknown")
        snippet = {k: sn.get(k) for k in SNIPPET_FIELDS}
        snippet["tags"] = list(sn.get("tags") or [])
        snippet["description"] = sn.get("description") or ""
        status = {k: st.get(k) for k in ("privacyStatus", "uploadStatus", "publishAt") if k in st}
        cap = (v.get("contentDetails") or {}).get("caption")
        has_caps = None if cap is None else str(cap).lower() == "true"
        return {"snippet": snippet, "status": status, "processing": proc, "has_captions": has_caps}

    def _snippet(self, video_id: str) -> dict[str, Any]:
        data = self._exec("videos.list", lambda s: s.videos().list(part="snippet", id=str(video_id)))
        items = data.get("items") or []
        if not items:
            raise PublisherError("data", f"video {video_id} not found")
        return items[0].get("snippet") or {}

    def update_snippet(self, video_id, changes) -> None:
        unknown = set(changes) - set(SNIPPET_WRITABLE)
        if unknown:
            raise PublisherError("data", f"unknown snippet fields {sorted(unknown)}")
        cur = self._snippet(video_id)
        keep = [t for t in cur.get("tags") or [] if t.startswith("vp-")]
        new = {k: cur[k] for k in SNIPPET_WRITABLE if k in cur}
        new.update(changes)
        if not new.get("title") or not new.get("categoryId"):
            raise PublisherError("data", "videos.update needs title and categoryId")
        missing = [t for t in keep if t not in (new.get("tags") or [])]
        if missing:
            raise PublisherError("data", "update would drop the vp tag")
        self._exec("videos.update", lambda s: s.videos().update(
            part="snippet", body={"id": str(video_id), "snippet": new}))
        after = self._snippet(video_id)
        lost = [t for t in keep if t not in (after.get("tags") or [])]
        if lost:
            raise PublisherError("data", f"vp tag lost after update of {video_id}")

    def _caption_track(self, video_id: str, language: str) -> dict[str, Any] | None:
        data = self._exec("captions.list", lambda s: s.captions().list(part="snippet",
                                                                        videoId=str(video_id)))
        for c in data.get("items") or []:
            sn = c.get("snippet") or {}
            if sn.get("language") == language and str(sn.get("trackKind", "")).lower() != "asr":
                return c
        return None

    def set_captions(self, video_id, srt_path, language="ru", *, track_id=None, lookup=True) -> str:
        """track_id -> captions.update (450); lookup=False -> captions.insert (400) without a
        captions.list; otherwise list (50) + update/insert."""
        from googleapiclient.http import MediaFileUpload
        path = str(srt_path)
        if not Path(path).exists():
            raise PublisherError("data", "captions file missing")
        if track_id:
            track = {"id": str(track_id)}
        else:
            track = self._caption_track(video_id, language) if lookup else None
        media = MediaFileUpload(path, mimetype="application/octet-stream", resumable=False)
        if track:
            cid = str(track["id"])
            self._exec("captions.update", lambda s: s.captions().update(
                part="snippet", body={"id": cid, "snippet": {"isDraft": False}}, media_body=media))
            return cid
        res = self._exec("captions.insert", lambda s: s.captions().insert(
            part="snippet", body={"snippet": {"videoId": str(video_id), "language": language,
                                              "name": "", "isDraft": False}}, media_body=media))
        return str(res["id"])

    def has_caption_track(self, video_id, track_id=None, language="ru") -> bool:
        track = self._caption_track(video_id, language)
        return bool(track) and (track_id is None or str(track.get("id")) == str(track_id))

    def get_captions(self, video_id, language="ru") -> bytes | None:
        track = self._caption_track(video_id, language)
        if not track:
            return None
        data = self._exec("captions.download", lambda s: s.captions().download(
            id=str(track["id"]), tfmt="srt"))
        return data if isinstance(data, bytes) else str(data).encode("utf-8")

    def set_thumbnail(self, video_id, png_path) -> None:
        from googleapiclient.http import MediaFileUpload
        p = Path(png_path)
        if not p.exists():
            raise PublisherError("data", "thumbnail file missing")
        if p.stat().st_size >= 2 * 1024 * 1024:
            raise PublisherError("data", "thumbnail must be < 2 MB")
        media = MediaFileUpload(str(p), mimetype="image/png", resumable=False)
        self._exec("thumbnails.set", lambda s: s.thumbnails().set(videoId=str(video_id),
                                                                  media_body=media))

    def ensure_playlist(self, title) -> str:
        if title in self._playlists:
            return self._playlists[title]
        token = None
        for _ in range(int(self.cfg.get("playlist_scan_pages") or 10)):
            kw = {"part": "snippet", "mine": True, "maxResults": 50}
            if token:
                kw["pageToken"] = token
            page = self._exec("playlists.list", lambda s: s.playlists().list(**kw))
            for pl in page.get("items") or []:
                if (pl.get("snippet") or {}).get("title") == title:
                    self._playlists[title] = str(pl["id"])
                    return self._playlists[title]
            token = page.get("nextPageToken")
            if not token:
                break
        res = self._exec("playlists.insert", lambda s: s.playlists().insert(
            part="snippet,status", body={"snippet": {"title": title},
                                         "status": {"privacyStatus": "private"}}))
        self._playlists[title] = str(res["id"])
        return self._playlists[title]

    def add_to_playlist(self, playlist_id, video_id) -> None:
        page = self._exec("playlistItems.list", lambda s: s.playlistItems().list(
            part="snippet", playlistId=str(playlist_id), videoId=str(video_id), maxResults=5))
        if page.get("items"):
            return
        self._exec("playlistItems.insert", lambda s: s.playlistItems().insert(
            part="snippet", body={"snippet": {"playlistId": str(playlist_id),
                                              "resourceId": {"kind": "youtube#video",
                                                             "videoId": str(video_id)}}}))

    def publish(self, video_id, when) -> None:
        data = self._exec("videos.list", lambda s: s.videos().list(part="status", id=str(video_id)))
        items = data.get("items") or []
        if not items:
            raise PublisherError("data", f"video {video_id} not found")
        cur = items[0].get("status") or {}
        new = {k: cur[k] for k in STATUS_WRITABLE if k in cur}
        if when == "now":
            new["privacyStatus"] = "public"
            new.pop("publishAt", None)
        else:
            try:
                at = parse_iso(str(when)).astimezone(timezone.utc)
            except ValueError:
                raise PublisherError("data", f"bad publish time {when!r}") from None
            new["privacyStatus"] = "private"
            new["publishAt"] = at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self._exec("videos.update", lambda s: s.videos().update(
            part="status", body={"id": str(video_id), "status": new}))


def _video_id(content: bytes) -> str | None:
    try:
        data = json.loads(content.decode("utf-8"))
    except Exception:
        return None
    vid = data.get("id") if isinstance(data, dict) else None
    return str(vid) if vid else None


# ---- owner commands: authorize, convert-token, status -----------------------------------------

AUTHORIZE_HINT = ("run `vp youtube authorize` (opens a browser to log in to the channel); "
                  "youtube.client_secrets_path in <vp_home>/config.yaml must point to the Google "
                  "OAuth client file (client_secret_*.json, type Desktop app)")
CONNECTION_FILE = "youtube_connection.json"   # <vp_home>: last known login problem, for doctor
LOGIN_TIMEOUT_S = 600


def auth_problem(text: Any) -> str | None:
    """'access_denied' | 'invalid_grant' | None from an error text."""
    s = str(text or "")
    return next((p for p in AUTH_PROBLEMS if p in s), None)


def client_project_id(secrets_path: Any) -> str | None:
    """project_id from the Google OAuth client file (the only field read; nothing is printed)."""
    if not secrets_path:
        return None
    try:
        data = json.loads(Path(str(secrets_path)).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for k in ("installed", "web"):
        if isinstance(data.get(k), dict) and data[k].get("project_id"):
            return str(data[k]["project_id"])
    return None


def explain_auth_problem(problem: str, yt: dict[str, Any]) -> str:
    """Plain words: what happened and what to do. Never contains secrets."""
    from ..core.config import BUNDLED_YOUTUBE_CLIENT
    if str(yt.get("client_secrets_path") or "") == str(BUNDLED_YOUTUBE_CLIENT):
        # the plugin's own Google client: the person only signs in; no Cloud words for them
        if problem == "access_denied":
            return ("Google did not give access (access_denied): the sign-in was cancelled, or "
                    "\"Allow\" was not pressed, or Google blocked this account. Fix: run "
                    "`vp youtube authorize` again and on the Google pages press Advanced -> Go to "
                    "(unsafe), tick the permissions, Continue. If Google shows a block page, ask "
                    "the person for a screenshot.")
        if problem == "invalid_grant":
            return ("The saved YouTube login has expired or was withdrawn (invalid_grant), e.g. "
                    "after a password change or removing the app's access in the Google account. "
                    "Fix: run `vp youtube authorize` again (browser login, two clicks).")
        return f"YouTube login problem: {problem}; " + AUTHORIZE_HINT
    pid = client_project_id(yt.get("client_secrets_path"))
    project = f"Google Cloud project '{pid}'" if pid else \
        "your Google Cloud project (its name is project_id in the client_secret_*.json file)"
    if problem == "access_denied":
        return ("Google refused the login (Error 403: access_denied, \"Access blocked\"). The OAuth "
                f"app of {project} is in Testing mode and this Google account is not one of its test "
                "users. Fix: in Google Cloud Console open that project -> Google Auth Platform -> "
                "Audience -> Test users -> Add users, add the Google account that owns the YouTube "
                "channel, then run `vp youtube authorize` again. Or press \"Publish app\" on the same "
                "Audience page: Google then shows an \"unverified app\" warning, but any account can "
                "log in and the login no longer expires after 7 days.")
    if problem == "invalid_grant":
        return ("The saved YouTube login has expired or was revoked (invalid_grant). While the OAuth "
                f"app of {project} is in Testing mode, Google ends its logins after about 7 days; "
                "a password change or removing the app's access in the Google account ends them too. "
                "Fix: run `vp youtube authorize` again (browser login). To stop the weekly expiry, "
                "publish the app: Google Cloud Console -> Google Auth Platform -> Audience -> "
                "Publish app.")
    return f"YouTube login problem: {problem}; " + AUTHORIZE_HINT


def _norm_handle(h: Any) -> str:
    s = str(h or "").strip().lower()
    return s if not s or s.startswith("@") else "@" + s


def handle_warning(expected: Any, actual: Any) -> str | None:
    """Warning when the signed-in channel's handle differs from youtube.expected_channel_handle
    (compared case-insensitively, '@' optional)."""
    if not expected or _norm_handle(expected) == _norm_handle(actual):
        return None
    return (f"WARNING: signed in to {actual or 'a channel without a handle'}, but "
            f"youtube.expected_channel_handle is {expected}. If this is the wrong channel, run "
            f"`vp youtube authorize` again and pick the right channel in the browser; if it is "
            f"right, change expected_channel_handle in config.yaml")


def record_connection(home: Any, ok: bool, problem: str | None) -> None:
    """Remember the last login result for doctor (no secrets: a flag, a code, a time)."""
    if not home:
        return
    from ..core.io import now_iso, write_json
    try:
        write_json(Path(str(home)) / CONNECTION_FILE, {"ok": bool(ok), "problem": problem,
                                                      "at": now_iso()})
    except OSError:
        pass


def read_connection(home: Any) -> dict[str, Any] | None:
    if not home:
        return None
    from ..core.io import read_json
    data = read_json(Path(str(home)) / CONNECTION_FILE, default=None)
    return data if isinstance(data, dict) else None


def status_detail(cfg: dict[str, Any], publisher: "YouTubePublisher | None" = None) -> dict[str, Any]:
    """{ok, message, problem, info}. Never contains secret contents."""
    yt = cfg.get("youtube") or {}
    token = yt.get("token_path")
    if not token:
        return {"ok": False, "problem": None, "info": None,
                "message": "not connected: youtube.token_path is not set in config.yaml; " + AUTHORIZE_HINT}
    if not Path(str(token)).expanduser().is_file():
        return {"ok": False, "problem": None, "info": None,
                "message": "not connected: no token file at youtube.token_path; " + AUTHORIZE_HINT}
    pub = publisher or YouTubePublisher(yt)
    try:
        info = pub.channel_info()
    except PublisherError as e:
        problem = auth_problem(e.msg) if e.kind == "auth" else None
        if problem:
            return {"ok": False, "problem": problem, "info": None,
                    "message": "not connected: " + explain_auth_problem(problem, yt)}
        what = ("the token is rejected or expired" if e.kind == "auth" else
                "YouTube is not reachable" if e.kind == "transport" else "unexpected answer")
        return {"ok": False, "problem": None, "info": None,
                "message": f"not connected: {what} ({e.msg}); " + (
                    AUTHORIZE_HINT if e.kind == "auth" else "check the network and retry")}
    except ImportError:
        return {"ok": False, "problem": None, "info": None,
                "message": "not connected: Google client libraries are not installed in the venv"}
    expected = yt.get("channel_id")
    note = ""
    if expected and str(expected) != info["id"]:
        note = f"; WARNING: config youtube.channel_id is {expected} — uploads will be refused"
    hw = handle_warning(yt.get("expected_channel_handle"), info.get("handle"))
    if hw:
        note += "; " + hw
    handle = f" {info['handle']}" if info.get("handle") else ""
    return {"ok": True, "problem": None, "info": info,
            "message": f"connected: {info.get('title') or '?'}{handle} (channel {info['id']}){note}"}


def status(cfg: dict[str, Any], publisher: "YouTubePublisher | None" = None) -> tuple[bool, str]:
    """(connected, one-line message). Never prints or returns secret contents."""
    d = status_detail(cfg, publisher)
    return d["ok"], d["message"]


def fill_channel_id(cfg: dict[str, Any], channel_id: str, *, overwrite: bool,
                    out: Callable[[str], None] = print) -> None:
    """Write youtube.channel_id into <vp_home>/config.yaml (only that key)."""
    from ..core.config import set_config_value
    yt = cfg.get("youtube") or {}
    old = yt.get("channel_id")
    if old and (str(old) == channel_id or not overwrite):
        return
    path = set_config_value("youtube", "channel_id", channel_id, cfg.get("vp_home"))
    out(f"config: youtube.channel_id set to {channel_id} in {path}"
        + (f" (was {old})" if old else ""))


def fill_channel_handle(cfg: dict[str, Any], handle: Any, *,
                        out: Callable[[str], None] = print) -> None:
    """After a login the signed-in channel is THE channel: any channel the user chose (a test
    channel first, the real one later). Its @handle becomes youtube.expected_channel_handle, and
    the plate on the videos (brand.channel_plate) follows it while the plate is empty or still the
    previous channel's handle; a plate the user wrote themselves is kept."""
    from ..core.config import set_config_value
    h = _norm_handle(handle) if handle else None
    if not h:
        return
    old = (cfg.get("youtube") or {}).get("expected_channel_handle")
    plate = (cfg.get("brand") or {}).get("channel_plate")
    home = cfg.get("vp_home")
    if _norm_handle(old) != h:
        path = set_config_value("youtube", "expected_channel_handle", h, home)
        out(f"config: youtube.expected_channel_handle set to {h} in {path}"
            + (f" (was {old}: the channel was switched)" if old else ""))
    if not plate or (old and _norm_handle(plate) == _norm_handle(old) and _norm_handle(plate) != h):
        path = set_config_value("brand", "channel_plate", h, home)
        out(f"config: brand.channel_plate set to {h} in {path}")


# ---- the Google client file, for people who never edited a settings file -----------------------
# Code does the bookkeeping (find, check, copy, permissions); the model only talks to the person.
# The file's secret is never printed: only its kind and the (public) project id are read.

CLIENT_SEARCH_DIRS = ("Downloads", "Desktop", "Documents")
CLIENT_MAX_BYTES = 64 * 1024


def client_kind(path: Any) -> str | None:
    """'installed' (Desktop app — the right one), 'web' (wrong type), or None (not a client file)."""
    try:
        p = Path(str(path)).expanduser()
        if not p.is_file() or p.stat().st_size > CLIENT_MAX_BYTES:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for kind in ("installed", "web"):
        block = data.get(kind)
        if isinstance(block, dict) and block.get("client_id") and block.get("client_secret"):
            return kind
    return None


def find_clients(home: Path | None = None) -> list[dict[str, Any]]:
    """Google client files in ~/Downloads, ~/Desktop, ~/Documents (top level only), newest first."""
    base = home or Path.home()
    found: list[dict[str, Any]] = []
    for d in CLIENT_SEARCH_DIRS:
        folder = base / d
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for f in entries:
            if f.suffix.lower() != ".json" or "client" not in f.name.lower():
                continue
            kind = client_kind(f)
            if kind:
                found.append({"path": str(f), "kind": kind, "mtime": f.stat().st_mtime})
    return sorted(found, key=lambda x: -x["mtime"])


def install_client(cfg: dict[str, Any], src: Any, out: Callable[[str], None] = print) -> int:
    """Copy a Desktop-app client file to youtube.client_secrets_path (owner-only permissions)."""
    yt = cfg.get("youtube") or {}
    dst_s = yt.get("client_secrets_path")
    if not dst_s:
        out("youtube.client_secrets_path is not set")
        return 2
    kind = client_kind(src)
    if kind is None:
        out("not a Google OAuth client file (expected the JSON downloaded from Google Cloud: "
            "Clients -> your client -> Download JSON)")
        return 2
    if kind == "web":
        out("wrong client type: this is a 'Web application' client; YouTube login here needs a "
            "'Desktop app' client (Google Cloud -> Google Auth Platform -> Clients -> Create client "
            "-> Desktop app)")
        return 2
    srcp, dst = Path(str(src)).expanduser(), Path(str(dst_s)).expanduser()
    data = srcp.read_bytes()
    if dst.is_file() and dst.read_bytes() == data:
        out("the client file is already in place")
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(dst, data)
    os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
    pid = client_project_id(dst)
    out("client file installed (contents not shown)" + (f"; Google Cloud project {pid}" if pid else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    from ..core.config import load_config
    ap = argparse.ArgumentParser(prog="vp youtube")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("authorize", help="browser login; writes youtube.token_path and youtube.channel_id")
    cv = sub.add_parser("convert-token", help="Legacy pickle token -> JSON at youtube.token_path")
    cv.add_argument("pickle_path")
    sub.add_parser("status", help="is the channel connected? (1 quota unit)")
    cf = sub.add_parser("client-find", help="is the Google client file in place? With --scan: list "
                                            "client files in Downloads/Desktop/Documents")
    cf.add_argument("--scan", action="store_true",
                    help="also look in Downloads/Desktop/Documents (macOS asks the person for "
                         "access to each of these folders; announce it first)")
    ci = sub.add_parser("client-install", help="copy a Desktop-app client file into place")
    ci.add_argument("path")
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.cmd == "client-find":
        here = (cfg.get("youtube") or {}).get("client_secrets_path")
        if here and client_kind(here) == "installed":
            print(f"in place: {here}")
            if not args.scan:
                # The plugin ships its client: no need to look into the person's folders. Each of
                # Downloads/Desktop/Documents is privacy-protected on macOS, and a look there would
                # pop a "Claude would like to access files in ..." prompt for nothing.
                return 0
        if not args.scan:
            print("none")
            return 0
        found = find_clients()
        for f in found:
            print(f"{f['kind']}\t{f['path']}")
        if not found and not (here and client_kind(here) == "installed"):
            print("none")
        return 0
    if args.cmd == "client-install":
        return install_client(cfg, args.path)
    if args.cmd == "status":
        d = status_detail(cfg)
        print(d["message"])
        if d["ok"] or d["problem"]:
            record_connection(cfg.get("vp_home"), d["ok"], d["problem"])
        if d["ok"]:
            fill_channel_id(cfg, d["info"]["id"], overwrite=False)
        return 0 if d["ok"] else 2
    if args.cmd == "authorize":
        return authorize(cfg)
    return _convert(args, cfg.get("youtube") or {})  # pragma: no cover - owner's Legacy file


def _login(secrets: str) -> Any:  # pragma: no cover - opens a browser
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(Path(secrets).expanduser()), SCOPES)
    return flow.run_local_server(port=0, open_browser=True, timeout_seconds=LOGIN_TIMEOUT_S)


def authorize(cfg: dict[str, Any], login: Callable[[str], Any] | None = None,
              publisher: "YouTubePublisher | None" = None, out: Callable[[str], None] = print) -> int:
    """Browser login -> token file; then the channel: id written to config, title and handle
    printed, handle compared. Recognises access_denied (Testing mode, not a test user)."""
    yt = cfg.get("youtube") or {}
    home = cfg.get("vp_home")
    token_path = yt.get("token_path")
    if not token_path:
        out("set youtube.token_path in <vp_home>/config.yaml first")
        return 2
    secrets = yt.get("client_secrets_path")
    if not secrets:
        out("set youtube.client_secrets_path in <vp_home>/config.yaml first (the Google "
            "OAuth client file)")
        return 2
    if client_kind(secrets) != "installed":
        out("no Google client file in place yet: run `vp youtube client-find`, then "
            "`vp youtube client-install <path>`")
        return 2
    out("A browser window opens: log in with the Google account that owns the YouTube channel and "
        "allow access. If Google shows \"Access blocked ... Error 403: access_denied\", close it: "
        "this command then explains what to do (it waits up to "
        f"{LOGIN_TIMEOUT_S // 60} minutes).")
    try:
        creds = (login or _login)(str(secrets))
    except Exception as e:  # noqa: BLE001 - classify, never print contents
        problem = auth_problem(getattr(e, "error", None)) or auth_problem(e)
        timed_out = type(e).__name__ == "WSGITimeoutError"
        if problem or timed_out:
            if timed_out:
                out(f"no answer from the browser login within {LOGIN_TIMEOUT_S // 60} minutes. "
                    f"The usual cause:")
            out(explain_auth_problem("access_denied", yt))
            record_connection(home, False, "access_denied")
            return 2
        out(f"login failed ({type(e).__name__}); run `vp youtube authorize` again")
        return 2
    _write_token(Path(token_path).expanduser(), creds.to_json())
    out("token written (contents not shown)")
    pub = publisher or YouTubePublisher(yt)
    try:
        info = pub.channel_info()
    except PublisherError as e:
        problem = auth_problem(e.msg) if e.kind == "auth" else None
        out("the login worked, but the channel could not be read: "
            + (explain_auth_problem(problem, yt) if problem else e.msg))
        if problem:
            record_connection(home, False, problem)
        return 2
    record_connection(home, True, None)
    handle = f" {info['handle']}" if info.get("handle") else ""
    out(f"connected: {info.get('title') or '?'}{handle} (channel {info['id']})")
    fill_channel_id(cfg, info["id"], overwrite=True, out=out)
    fill_channel_handle(cfg, info.get("handle"), out=out)
    return 0


def _convert(args: Any, yt: dict[str, Any]) -> int:  # pragma: no cover - owner's Legacy file
    token_path = yt.get("token_path")
    if not token_path:
        print("set youtube.token_path in <vp_home>/config.yaml first")
        return 2
    import pickle
    with open(args.pickle_path, "rb") as f:  # the owner's own Legacy token file
        creds = pickle.load(f)
    _write_token(Path(token_path).expanduser(), creds.to_json())
    print("token written (contents not shown)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
