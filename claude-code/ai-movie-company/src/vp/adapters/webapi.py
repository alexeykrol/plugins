"""Small HTTPS helpers for key-based web APIs (stock, image generation, ElevenLabs). urllib only.

  request_json(method, url, headers, body=None, opener=None, secrets=(), service="")
  request_bytes(method, url, headers, body=None, opener=None, secrets=(), service="")  raw body
  download(url, dst, opener=None, headers=None, secrets=())

`classify(code, hint, where)` (optional, request_json / request_bytes) lets an adapter turn an
HTTP error into its own VPError (e.g. ElevenLabs "no permission" or "paid plan only"); returning
None falls back to the classes below. The hint it gets is already scrubbed of the secrets.

The opener is injectable (same pattern as the Telegram notifier): `opener(req, timeout=...)` returns
a context manager whose `.read()` gives the body. Tests pass a fake; nothing touches the network.

Error classes (SPEC §7.1): HTTP 401/403 -> auth (a wrong or unpaid key; never retried), 429 and 5xx
-> transport, other 4xx -> data, bad JSON -> contract. Keys never reach a message: every string
that goes into an error is scrubbed of the given `secrets`, and URLs are shown without their query
(Pixabay carries its key in the query).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.errors import VPError

Opener = Callable[..., Any]
USER_AGENT = "ai-movie-company"
TIMEOUT_S = 60
MAX_ERROR_BODY = 300


def scrub(text: str, secrets: Iterable[str]) -> str:
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "[key]").replace(urllib.parse.quote(s, safe=""), "[key]")
    return out


def safe_url(url: str) -> str:
    """scheme://host/path — no query, no fragment (a query may hold a key)."""
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _https_only(url: str) -> None:
    if not url.startswith("https://"):
        raise VPError("internal", f"refusing a non-https request: {safe_url(url)}")


Classify = Callable[[int, str, str], "VPError | None"]


def _http_error(e: urllib.error.HTTPError, where: str, secrets: Iterable[str],
                service: str, classify: Classify | None = None) -> VPError:
    code = e.code
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — the body is only a hint
        body = ""
    hint = scrub(body.strip().replace("\n", " "), secrets)[:MAX_ERROR_BODY]
    if classify is not None:
        mine = classify(code, hint, where)
        if mine is not None:
            return mine
    if code in (401, 403):
        tag = f"BAD_KEY:{service} " if service else ""
        return VPError("auth", f"{tag}{where}: HTTP {code} — the service refused the key"
                               + (f" ({hint})" if hint else ""), http_status=code)
    if code == 429:
        return VPError("transport", f"{where}: HTTP 429 — rate limit reached, try again later",
                       http_status=code)
    if code >= 500:
        return VPError("transport", f"{where}: HTTP {code} — the service is unwell",
                       http_status=code)
    return VPError("data", f"{where}: HTTP {code}" + (f" — {hint}" if hint else ""),
                   http_status=code)


def request_bytes(method: str, url: str, headers: dict[str, str] | None = None,
                  body: Any = None, opener: Opener | None = None, secrets: Iterable[str] = (),
                  service: str = "", timeout_s: float = TIMEOUT_S,
                  classify: Classify | None = None, accept: str = "application/json") -> bytes:
    """One request, the raw answer body. Never retried here: a paid generation that timed out
    may already be charged, so retrying is the caller's (the person's) decision."""
    _https_only(url)
    secrets = tuple(secrets)
    where = f"{service or 'web'} {method} {safe_url(url)}"
    data = None
    h = {"User-Agent": USER_AGENT, "Accept": accept}
    h.update(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout_s) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise _http_error(e, where, secrets, service, classify) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise VPError("transport", f"{where}: no answer ({type(e).__name__}) — check the "
                                   f"internet connection") from None


def request_json(method: str, url: str, headers: dict[str, str] | None = None,
                 body: Any = None, opener: Opener | None = None, secrets: Iterable[str] = (),
                 service: str = "", timeout_s: float = TIMEOUT_S,
                 classify: Classify | None = None) -> Any:
    raw = request_bytes(method, url, headers, body, opener, secrets, service, timeout_s, classify)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VPError("contract", f"{service or 'web'} {method} {safe_url(url)}: the answer is "
                                  f"not JSON") from None


def download(url: str, dst: str | os.PathLike, opener: Opener | None = None,
             headers: dict[str, str] | None = None, secrets: Iterable[str] = (),
             service: str = "", timeout_s: float = TIMEOUT_S) -> int:
    """Stream an https URL into dst (tmp file + rename). Returns the byte count."""
    _https_only(url)
    secrets = tuple(secrets)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.part")
    where = f"{service or 'web'} download {safe_url(url)}"
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    got = 0
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout_s) as resp:
            final = getattr(resp, "geturl", lambda: url)()
            if final and not str(final).startswith("https://"):
                raise VPError("internal", f"{where}: refusing a redirect away from https")
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        raise _http_error(e, where, secrets, service) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        tmp.unlink(missing_ok=True)
        raise VPError("transport", f"{where}: the download broke off ({type(e).__name__}) — "
                                   f"try again later") from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if got == 0:
        tmp.unlink(missing_ok=True)
        raise VPError("data", f"{where}: empty file")
    os.replace(tmp, dst)
    return got
