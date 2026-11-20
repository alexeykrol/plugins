"""API keys of the person's own accounts (ElevenLabs, stock, image generation): where they live,
how they are looked up, how they get there without passing through the chat.

  vp keys status [--json]                         which services have a key (never the key itself)
  vp keys set <service> --from-clipboard|--stdin  store a key the person copied; the clipboard is
                                                  cleared afterwards (--keep-clipboard to skip)
  vp keys check <service> [--json]                one free request: does the service accept the key?
  vp keys forget <service>                        delete the stored key file

Services: elevenlabs (speech recognition, voice and music — one key for all three), pexels,
pixabay (stock), gemini, openai (image generation).

Lookup order (the same pattern the Telegram notifier uses — env var first): the environment
variable (ELEVENLABS_API_KEY or XI_API_KEY, PEXELS_API_KEY, PIXABAY_API_KEY, GEMINI_API_KEY or
GOOGLE_API_KEY, OPENAI_API_KEY), then the file <vp_home>/keys/<service>.key (folder 0700, file
0600, outside every job folder and outside the repository). A key kept in an older place
(ElevenLabs: <vp_home>/elevenlabs_api_key, written by `vp transcribe key set` before 2026-11-21)
is moved into keys/ on its first read. Keys are never printed, logged or put into errors: status shows only where a key
comes from and an 8-character sha256 fingerprint.

A missing key is a reason code, not a crash: `NEED_KEY:<service>` (images: `NEED_KEY:images`) — the
dialog layer turns it into the walk-through of the `connect` skill.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from .core.errors import VPError
from .core.paths import vp_home

SERVICES: dict[str, dict[str, Any]] = {
    "elevenlabs": {"env": ("ELEVENLABS_API_KEY", "XI_API_KEY"),
                   "what": "speech recognition, voice and music (ElevenLabs)"},
    "pexels": {"env": ("PEXELS_API_KEY",), "what": "stock video and photos (Pexels)"},
    "pixabay": {"env": ("PIXABAY_API_KEY",), "what": "stock video and photos (Pixabay)"},
    "gemini": {"env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "what": "image generation (Google Gemini)"},
    "openai": {"env": ("OPENAI_API_KEY",), "what": "image generation (OpenAI)"},
}

MIN_LEN, MAX_LEN = 16, 400

# older places a key was kept, relative to <vp_home>; moved into keys/ on the first read
LEGACY_FILES: dict[str, str] = {"elevenlabs": "elevenlabs_api_key"}


class NeedKey(VPError):
    """No key for a service. `code` is the reason code the dialog layer reacts to."""

    def __init__(self, service: str, msg: str | None = None):
        self.code = f"NEED_KEY:{service}"
        super().__init__("auth", f"{self.code} — " + (msg or f"no API key for {service} yet "
                                                       f"(`vp keys set {service} --from-clipboard`)"))
        self.service = service


def _check_service(service: str) -> str:
    if service not in SERVICES:
        raise VPError("data", f"unknown service {service!r}; known: {', '.join(SERVICES)}")
    return service


def keys_dir(home: str | os.PathLike | None = None) -> Path:
    return (Path(home).expanduser() if home else vp_home()) / "keys"


def key_file(service: str, home: str | os.PathLike | None = None) -> Path:
    return keys_dir(home) / f"{_check_service(service)}.key"


def lookup(service: str, home: str | os.PathLike | None = None,
           environ: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """(key, source) — source is "env:NAME" or "file"; (None, None) when there is no key."""
    env = os.environ if environ is None else environ
    for name in SERVICES[_check_service(service)]["env"]:
        v = (env.get(name) or "").strip()
        if v:
            return v, f"env:{name}"
    p = key_file(service, home)
    if p.is_file():
        v = p.read_text(encoding="utf-8").strip()
        if v:
            return v, "file"
    return _legacy(service, home)


def legacy_file(service: str, home: str | os.PathLike | None = None) -> Path | None:
    name = LEGACY_FILES.get(service)
    return ((Path(home).expanduser() if home else vp_home()) / name) if name else None


def _legacy(service: str, home: str | os.PathLike | None) -> tuple[str | None, str | None]:
    """A key in its older place: moved into keys/ (0600) and the old file removed. If the move
    fails (read-only home, a value that does not look like a key) the old file is still used."""
    old = legacy_file(service, home)
    if old is None or not old.is_file():
        return None, None
    try:
        v = old.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None, None
    if not v:
        return None, None
    try:
        save_key(service, v, home)
        old.unlink()
    except (OSError, VPError):
        pass
    return v, "file"


def get_key(service: str, home: str | os.PathLike | None = None,
            environ: Mapping[str, str] | None = None) -> str | None:
    return lookup(service, home, environ)[0]


def require_key(service: str, home: str | os.PathLike | None = None,
                environ: Mapping[str, str] | None = None) -> str:
    key = get_key(service, home, environ)
    if not key:
        raise NeedKey(service)
    return key


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def validate(value: str) -> str:
    """Plausibility only (a pasted sentence, a URL or an empty clipboard is caught here); whether
    the service accepts it is `check`. The value never appears in the message."""
    v = (value or "").strip()
    if not v:
        raise VPError("data", "the clipboard (input) is empty — copy the key first")
    if any(c.isspace() for c in v):
        raise VPError("data", "this does not look like a key: it contains spaces or line breaks "
                              "(copy only the key itself)")
    if not v.isascii() or not v.isprintable():
        raise VPError("data", "this does not look like a key: it contains unusual characters")
    if v.lower().startswith(("http://", "https://")):
        raise VPError("data", "this is a link, not a key — copy the key itself")
    if not (MIN_LEN <= len(v) <= MAX_LEN):
        raise VPError("data", f"this does not look like a key: {len(v)} characters "
                              f"(keys are {MIN_LEN}–{MAX_LEN})")
    return v


def save_key(service: str, value: str, home: str | os.PathLike | None = None) -> dict[str, Any]:
    v = validate(value)
    d = keys_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = key_file(service, home)
    tmp = p.with_name(f".{p.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(v + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return {"service": service, "stored": str(p), "fingerprint": fingerprint(v)}


def forget_key(service: str, home: str | os.PathLike | None = None) -> bool:
    gone = False
    for p in (key_file(service, home), legacy_file(service, home)):
        if p is not None and p.is_file():
            p.unlink()
            gone = True
    return gone


def status(home: str | os.PathLike | None = None,
           environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    out = []
    for s, meta in SERVICES.items():
        key, src = lookup(s, home, environ)
        out.append({"service": s, "what": meta["what"], "present": bool(key), "source": src,
                    "fingerprint": fingerprint(key) if key else None})
    return out


# ---- check: one free request per service ---------------------------------------------------

# Free, read-only endpoints (no generation, no charge):
#   elevenlabs GET https://api.elevenlabs.io/v2/voices?page_size=1 (voice list; needs the key's
#              "Voices: read" permission, which voice selection needs anyway)
#   pexels  GET https://api.pexels.com/v1/curated?per_page=1   (counts 1 of 200 requests/hour)
#   pixabay GET https://pixabay.com/api/?key=…&per_page=3
#   gemini  GET https://generativelanguage.googleapis.com/v1beta/models  (model list)
#   openai  GET https://api.openai.com/v1/models                         (model list)
CHECK_URLS = {
    "elevenlabs": "https://api.elevenlabs.io/v2/voices?page_size=1&include_total_count=false",
    "pexels": "https://api.pexels.com/v1/curated?per_page=1",
    "pixabay": "https://pixabay.com/api/?per_page=3",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
    "openai": "https://api.openai.com/v1/models",
}


def auth_request(service: str, url: str, key: str) -> tuple[str, dict[str, str]]:
    """URL and headers carrying the key the way each service wants it."""
    if service == "elevenlabs":
        return url, {"xi-api-key": key}
    if service == "pexels":
        return url, {"Authorization": key}
    if service == "pixabay":   # Pixabay accepts the key only as a query parameter
        sep = "&" if "?" in url else "?"
        from urllib.parse import quote
        return f"{url}{sep}key={quote(key, safe='')}", {}
    if service == "gemini":
        return url, {"x-goog-api-key": key}
    if service == "openai":
        return url, {"Authorization": f"Bearer {key}"}
    raise VPError("internal", f"no auth scheme for {service}")


def check(service: str, home: str | os.PathLike | None = None, opener: Callable | None = None,
          environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """{"service", "result": ok|bad_key|no_key|network|unexpected, "detail"}."""
    from .adapters import webapi
    key = get_key(service, home, environ)
    if not key:
        return {"service": service, "result": "no_key", "code": f"NEED_KEY:{service}"}
    url, headers = auth_request(service, CHECK_URLS[service], key)
    try:
        webapi.request_json("GET", url, headers=headers, opener=opener, secrets=(key,),
                            service=service)
    except VPError as e:
        if e.kind == "auth":
            return {"service": service, "result": "bad_key", "code": f"BAD_KEY:{service}",
                    "detail": e.msg}
        if e.kind == "transport":
            return {"service": service, "result": "network", "detail": e.msg}
        return {"service": service, "result": "unexpected", "detail": e.msg}
    return {"service": service, "result": "ok"}


# ---- clipboard ------------------------------------------------------------------------------

def read_clipboard() -> str:
    try:
        return subprocess.run(["pbpaste"], capture_output=True, timeout=10,
                              check=True).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as e:
        raise VPError("data", f"could not read the clipboard ({type(e).__name__}); "
                              f"use --stdin instead") from None


def clear_clipboard() -> bool:
    try:
        subprocess.run(["pbcopy"], input=b"", timeout=10, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---- CLI ------------------------------------------------------------------------------------

def main(argv: list[str] | None = None, *, opener: Callable | None = None,
         clipboard: Callable[[], str] | None = None,
         clear: Callable[[], bool] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vp keys")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="which services have a key (never the key)")
    st.add_argument("--json", action="store_true")
    se = sub.add_parser("set", help="store a key for a service")
    se.add_argument("service", choices=list(SERVICES))
    src = se.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-clipboard", action="store_true")
    src.add_argument("--stdin", action="store_true")
    se.add_argument("--keep-clipboard", action="store_true")
    ch = sub.add_parser("check", help="one free request: is the key accepted?")
    ch.add_argument("service", choices=list(SERVICES))
    ch.add_argument("--json", action="store_true")
    fo = sub.add_parser("forget", help="delete the stored key file")
    fo.add_argument("service", choices=list(SERVICES))
    a = ap.parse_args(argv)

    if a.cmd == "status":
        rows = status()
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for r in rows:
                print(f"{r['service']}: " + (f"set ({r['source']}, fingerprint {r['fingerprint']})"
                                             if r["present"] else f"NEED_KEY:{r['service']}"))
        return 0
    if a.cmd == "set":
        raw = (clipboard or read_clipboard)() if a.from_clipboard else sys.stdin.read()
        try:
            res = save_key(a.service, raw)
        except VPError as e:
            print(f"NOT SAVED: {e.msg}")
            return 2
        cleared = False
        if a.from_clipboard and not a.keep_clipboard:
            cleared = (clear or clear_clipboard)()
        env_name = next((n for n in SERVICES[a.service]["env"] if os.environ.get(n)), None)
        print(f"saved: {a.service} key -> {res['stored']} (fingerprint {res['fingerprint']})"
              + ("; clipboard cleared" if cleared else ""))
        if env_name:
            print(f"note: env {env_name} is also set and takes precedence over the file")
        return 0
    if a.cmd == "check":
        res = check(a.service, opener=opener)
        if a.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print(f"{a.service}: {res['result']}" + (f" — {res['detail']}" if res.get("detail") else ""))
        return 0 if res["result"] == "ok" else 2
    if a.cmd == "forget":
        print(f"{a.service}: " + ("key file deleted" if forget_key(a.service) else "no key file"))
        return 0
    return 3  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
