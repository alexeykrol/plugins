"""Stock adapter (capability С, docs/SCENARIOS.md): Pexels and Pixabay search + download into the
job's material library with a provenance sidecar (SPEC §0, principle 3: material carries its
source and license).

  search(query, kind="video"|"photo", orientation=None|"portrait"|"landscape"|"square",
         min_duration=None, source=None, per_page=10, resolution=1080) -> [result]
  get(item_id, job_dir, resolution=1080, query=None) -> {"file", "provenance_file", "provenance"}

Result (normalized, both sources):
  id ("pexels:video:123"), source, kind, url (file at the chosen resolution), width, height,
  duration (s, video), size (bytes or None), author, author_url, license, license_url, page_url,
  preview_url, tags

Files go to <job>/library/stock/<source>-<kind>-<id>.<ext>, the sidecar next to it:
<file>.provenance.json = {source, kind, id, author, author_url, license, license_url, page_url,
query, file_url (without query string), width, height, duration, retrieved_at, attribution}.

Official sources (checked 2026-11-20):
  Pexels API   https://www.pexels.com/api/documentation/  — header `Authorization: <key>`;
               photos GET https://api.pexels.com/v1/search, GET /v1/photos/:id; videos moved to
               GET https://api.pexels.com/v1/videos/search and /v1/videos/videos/:id (the old
               https://api.pexels.com/videos/ paths "will be deprecated"); orientation
               landscape|portrait|square; per_page max 80; no duration filter (done here);
               200 requests/hour, 20 000/month by default; show a link to Pexels and credit the
               photographer when possible.
  Pexels license https://www.pexels.com/license/
  Pixabay API  https://pixabay.com/api/docs/  — key as the `key` query parameter (so URLs are
               never printed); images GET https://pixabay.com/api/ (orientation
               horizontal|vertical), videos GET https://pixabay.com/api/videos/ (no orientation
               filter — done here by width/height; renditions large/medium/small/tiny);
               100 requests/60 s; cache results 24 h; no permanent hotlinking — download first;
               show where results come from.
  Pixabay license https://pixabay.com/service/license-summary/
Models never see these URLs or ids (AGENTS.md): this is code-only bookkeeping.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.errors import VPError, note_failure
from ..core.io import now_iso, write_json
from .. import keys as keystore
from . import webapi

SOURCES = ("pexels", "pixabay")
KINDS = ("video", "photo")
ORIENTATIONS = ("portrait", "landscape", "square")

PEXELS_API = "https://api.pexels.com/v1"
PIXABAY_API = "https://pixabay.com/api"

LICENSES = {
    "pexels": {
        "url": "https://www.pexels.com/license/",
        "text": ("Pexels License: free to use, including commercial use; attribution not required "
                 "but appreciated. Not allowed: showing identifiable people in a bad or offensive "
                 "light; selling unaltered copies (posters, prints, products); implying "
                 "endorsement by people or brands shown; redistributing or selling on other stock "
                 "or wallpaper platforms."),
    },
    "pixabay": {
        "url": "https://pixabay.com/service/license-summary/",
        "text": ("Pixabay Content License: free to use, attribution not required. Not allowed: "
                 "selling or distributing the content standalone (without creative effort); "
                 "commercial use of content with recognisable trademarks, logos or brands for "
                 "goods and services; immoral, illegal, misleading or deceptive use (especially "
                 "with recognisable people); use as a trademark or business name. Third-party "
                 "rights (people, property, brands) may still apply."),
    },
}

ID_RE = re.compile(r"^(pexels|pixabay):(video|photo):(\d+)$")


# ---- ids ------------------------------------------------------------------------------------

def make_id(source: str, kind: str, raw_id: Any) -> str:
    return f"{source}:{kind}:{int(raw_id)}"


def parse_id(item_id: str) -> tuple[str, str, int]:
    m = ID_RE.match(str(item_id).strip())
    if not m:
        raise VPError("data", f"not a stock id: {item_id!r} (expected e.g. pexels:video:123)")
    return m.group(1), m.group(2), int(m.group(3))


# ---- choosing a rendition -------------------------------------------------------------------

def _short(w: Any, h: Any) -> int:
    try:
        return min(int(w), int(h))
    except (TypeError, ValueError):
        return 0


def pick_rendition(files: list[dict[str, Any]], resolution: int) -> dict[str, Any] | None:
    """The largest rendition whose short side is <= resolution; if all are larger, the smallest.
    Each file: {url, width, height, ...}. Files without a URL or size are skipped."""
    ok = [f for f in files if f.get("url") and _short(f.get("width"), f.get("height")) > 0]
    if not ok:
        return None
    below = [f for f in ok if _short(f["width"], f["height"]) <= resolution]
    if below:
        return max(below, key=lambda f: _short(f["width"], f["height"]))
    return min(ok, key=lambda f: _short(f["width"], f["height"]))


def orientation_of(w: Any, h: Any) -> str | None:
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    r = w / h
    return "square" if 0.9 <= r <= 1.1 else ("landscape" if r > 1 else "portrait")


# ---- normalization: Pexels ------------------------------------------------------------------

def _license(source: str) -> dict[str, str]:
    return {"license": LICENSES[source]["text"], "license_url": LICENSES[source]["url"]}


def normalize_pexels_video(v: dict[str, Any], resolution: int = 1080) -> dict[str, Any]:
    files = [{"url": f.get("link"), "width": f.get("width"), "height": f.get("height"),
              "quality": f.get("quality"), "fps": f.get("fps")}
             for f in v.get("video_files") or []
             if f.get("quality") != "hls" and (f.get("file_type") or "video/mp4") == "video/mp4"]
    best = pick_rendition(files, resolution) or {}
    user = v.get("user") or {}
    return {"id": make_id("pexels", "video", v["id"]), "source": "pexels", "kind": "video",
            "url": best.get("url"), "width": best.get("width"), "height": best.get("height"),
            "duration": v.get("duration"), "size": None,
            "author": user.get("name"), "author_url": user.get("url"),
            "page_url": v.get("url"), "preview_url": v.get("image"),
            "tags": list(v.get("tags") or []), **_license("pexels")}


def _pexels_photo_src(src: dict[str, Any], resolution: int) -> tuple[str | None, str]:
    # documented renditions: large (h=650,w=940), large2x (the same at dpr=2), original
    if resolution <= 650 and src.get("large"):
        return src["large"], "large"
    if resolution <= 1300 and src.get("large2x"):
        return src["large2x"], "large2x"
    return src.get("original"), "original"


def normalize_pexels_photo(p: dict[str, Any], resolution: int = 1080) -> dict[str, Any]:
    url, which = _pexels_photo_src(p.get("src") or {}, resolution)
    w, h = p.get("width"), p.get("height")
    if which != "original":   # the rendition is scaled; only the original size is published
        w = h = None
    return {"id": make_id("pexels", "photo", p["id"]), "source": "pexels", "kind": "photo",
            "url": url, "width": w, "height": h, "duration": None, "size": None,
            "author": p.get("photographer"), "author_url": p.get("photographer_url"),
            "page_url": p.get("url"), "preview_url": (p.get("src") or {}).get("medium"),
            "tags": [p["alt"]] if p.get("alt") else [], "original_width": p.get("width"),
            "original_height": p.get("height"), **_license("pexels")}


# ---- normalization: Pixabay -----------------------------------------------------------------

def _pixabay_user_url(hit: dict[str, Any]) -> str | None:
    if hit.get("user") and hit.get("user_id"):
        return f"https://pixabay.com/users/{hit['user']}-{hit['user_id']}/"
    return None


def _tags(s: Any) -> list[str]:
    return [t.strip() for t in str(s or "").split(",") if t.strip()]


def normalize_pixabay_video(hit: dict[str, Any], resolution: int = 1080) -> dict[str, Any]:
    vids = hit.get("videos") or {}
    files = [{"url": f.get("url"), "width": f.get("width"), "height": f.get("height"),
              "size": f.get("size"), "rendition": name}
             for name, f in vids.items() if isinstance(f, dict)]
    best = pick_rendition(files, resolution) or {}
    preview = (vids.get("small") or vids.get("tiny") or {}).get("thumbnail")
    return {"id": make_id("pixabay", "video", hit["id"]), "source": "pixabay", "kind": "video",
            "url": best.get("url"), "width": best.get("width"), "height": best.get("height"),
            "duration": hit.get("duration"), "size": best.get("size") or None,
            "author": hit.get("user"), "author_url": _pixabay_user_url(hit),
            "page_url": hit.get("pageURL"), "preview_url": preview,
            "tags": _tags(hit.get("tags")), **_license("pixabay")}


def normalize_pixabay_photo(hit: dict[str, Any], resolution: int = 1080) -> dict[str, Any]:
    iw, ih = hit.get("imageWidth"), hit.get("imageHeight")

    def scaled(max_side: int) -> tuple[int | None, int | None]:
        try:
            w, h = int(iw), int(ih)
        except (TypeError, ValueError):
            return None, None
        k = min(1.0, max_side / max(w, h))
        return round(w * k), round(h * k)

    cands = []
    if hit.get("webformatURL"):
        cands.append({"url": hit["webformatURL"], **dict(zip(("width", "height"), scaled(640)))})
    if hit.get("largeImageURL"):
        cands.append({"url": hit["largeImageURL"], **dict(zip(("width", "height"), scaled(1280)))})
    if hit.get("fullHDURL"):      # only with "full API access"
        cands.append({"url": hit["fullHDURL"], **dict(zip(("width", "height"), scaled(1920)))})
    if hit.get("imageURL"):
        cands.append({"url": hit["imageURL"], "width": iw, "height": ih})
    best = pick_rendition(cands, resolution) or {}
    return {"id": make_id("pixabay", "photo", hit["id"]), "source": "pixabay", "kind": "photo",
            "url": best.get("url"), "width": best.get("width"), "height": best.get("height"),
            "duration": None, "size": hit.get("imageSize") if best.get("url") == hit.get("imageURL") else None,
            "author": hit.get("user"), "author_url": _pixabay_user_url(hit),
            "page_url": hit.get("pageURL"), "preview_url": hit.get("previewURL"),
            "tags": _tags(hit.get("tags")), "original_width": iw, "original_height": ih,
            **_license("pixabay")}


NORMALIZE = {("pexels", "video"): normalize_pexels_video, ("pexels", "photo"): normalize_pexels_photo,
             ("pixabay", "video"): normalize_pixabay_video, ("pixabay", "photo"): normalize_pixabay_photo}


# ---- request building -----------------------------------------------------------------------

def _q(params: dict[str, Any]) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})


def pexels_search_url(query: str, kind: str, orientation: str | None, per_page: int) -> str:
    base = f"{PEXELS_API}/videos/search" if kind == "video" else f"{PEXELS_API}/search"
    return f"{base}?{_q({'query': query, 'orientation': orientation, 'per_page': per_page})}"


def pexels_item_url(kind: str, raw_id: int) -> str:
    return f"{PEXELS_API}/videos/videos/{raw_id}" if kind == "video" else f"{PEXELS_API}/photos/{raw_id}"


PIXABAY_ORIENT = {"portrait": "vertical", "landscape": "horizontal"}


def pixabay_search_url(query: str, kind: str, orientation: str | None, per_page: int) -> str:
    """Without the key — `keys.auth_request` adds it right before the call."""
    if kind == "video":
        return f"{PIXABAY_API}/videos/?{_q({'q': query[:100], 'per_page': max(3, per_page), 'safesearch': 'true'})}"
    return (f"{PIXABAY_API}/?{_q({'q': query[:100], 'image_type': 'photo', 'per_page': max(3, per_page), 'safesearch': 'true', 'orientation': PIXABAY_ORIENT.get(orientation or '')})}")


def pixabay_item_url(kind: str, raw_id: int) -> str:
    return f"{PIXABAY_API}/videos/?id={raw_id}" if kind == "video" else f"{PIXABAY_API}/?id={raw_id}"


# ---- search / get ---------------------------------------------------------------------------

def _validate(kind: str, orientation: str | None) -> None:
    if kind not in KINDS:
        raise VPError("data", f"kind must be one of {KINDS}, got {kind!r}")
    if orientation and orientation not in ORIENTATIONS:
        raise VPError("data", f"orientation must be one of {ORIENTATIONS}, got {orientation!r}")


def _sources(source: str | None, home: Any, environ: Any) -> list[tuple[str, str]]:
    """[(source, key)] to query. An explicit source without a key -> NEED_KEY:<source>; no key at
    all -> NEED_KEY:pexels (the default suggestion; Pixabay works too)."""
    if source:
        if source not in SOURCES:
            raise VPError("data", f"source must be one of {SOURCES}, got {source!r}")
        return [(source, keystore.require_key(source, home, environ))]
    found = [(s, k) for s in SOURCES if (k := keystore.get_key(s, home, environ))]
    if not found:
        raise keystore.NeedKey("pexels", "no stock key yet: a free Pexels key (or a Pixabay key) "
                                         "is needed — `vp keys set pexels --from-clipboard`")
    return found


def _call(source: str, url: str, key: str, opener: Any) -> Any:
    full, headers = keystore.auth_request(source, url, key)
    return webapi.request_json("GET", full, headers=headers, opener=opener, secrets=(key,),
                               service=source)


def _keep(r: dict[str, Any], orientation: str | None, min_duration: float | None) -> bool:
    if not r.get("url"):
        return False
    if min_duration and r["kind"] == "video" and (r.get("duration") or 0) < min_duration:
        return False
    if orientation and r["source"] == "pixabay":
        w, h = r.get("original_width") or r.get("width"), r.get("original_height") or r.get("height")
        o = orientation_of(w, h)
        if o and o != orientation:
            return False
    return True


def search(query: str, kind: str = "video", orientation: str | None = None,
           min_duration: float | None = None, source: str | None = None, per_page: int = 10,
           resolution: int = 1080, *, opener: Any = None, home: Any = None,
           environ: Any = None) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        raise VPError("data", "empty search query")
    _validate(kind, orientation)
    per_page = max(1, min(int(per_page), 80))
    # ask for more than needed when filtering by duration, so the page is not empty
    ask = min(80, per_page * 3) if min_duration else per_page
    by_source: list[list[dict[str, Any]]] = []
    for src, key in _sources(source, home, environ):
        if src == "pexels":
            data = _call(src, pexels_search_url(query, kind, orientation, ask), key, opener)
            items = (data or {}).get("videos" if kind == "video" else "photos") or []
        else:
            data = _call(src, pixabay_search_url(query, kind, orientation, ask), key, opener)
            items = (data or {}).get("hits") or []
        norm = NORMALIZE[(src, kind)]
        rows = []
        for it in items:
            try:
                r = norm(it, resolution)
            except (KeyError, TypeError, ValueError):
                continue           # one malformed item does not spoil the page
            if _keep(r, orientation, min_duration):
                rows.append(r)
        by_source.append(rows)
    # interleave sources so both are visible in the first results
    out: list[dict[str, Any]] = []
    for i in range(max((len(r) for r in by_source), default=0)):
        for rows in by_source:
            if i < len(rows):
                out.append(rows[i])
    return out[:per_page]


def fetch_item(item_id: str, resolution: int = 1080, *, opener: Any = None, home: Any = None,
               environ: Any = None) -> dict[str, Any]:
    source, kind, raw_id = parse_id(item_id)
    key = keystore.require_key(source, home, environ)
    if source == "pexels":
        data = _call(source, pexels_item_url(kind, raw_id), key, opener)
    else:
        hits = (_call(source, pixabay_item_url(kind, raw_id), key, opener) or {}).get("hits") or []
        if not hits:
            raise VPError("data", f"{item_id}: not found on Pixabay")
        data = hits[0]
    try:
        r = NORMALIZE[(source, kind)](data, resolution)
    except (KeyError, TypeError, ValueError):
        raise VPError("contract", f"{item_id}: unexpected answer from {source}") from None
    if not r.get("url"):
        raise VPError("data", f"{item_id}: no downloadable file in the answer")
    return r


def library_dir(job_dir: str | os.PathLike) -> Path:
    return Path(job_dir).expanduser().resolve() / "library" / "stock"


def _ext(url: str, kind: str) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    m = re.search(r"\.(mp4|mov|webm|jpe?g|png|webp)$", path)
    if m:
        return ".jpg" if m.group(1) == "jpeg" else f".{m.group(1)}"
    return ".mp4" if kind == "video" else ".jpg"


def attribution(r: dict[str, Any]) -> str:
    what = "Video" if r["kind"] == "video" else "Photo"
    site = "Pexels" if r["source"] == "pexels" else "Pixabay"
    return f"{what} by {r.get('author') or 'unknown'} on {site}"


def provenance(r: dict[str, Any], query: str | None) -> dict[str, Any]:
    return {"source": r["source"], "kind": r["kind"], "id": r["id"],
            "author": r.get("author"), "author_url": r.get("author_url"),
            "license": r["license"], "license_url": r["license_url"],
            "page_url": r.get("page_url"), "query": query,
            "file_url": webapi.safe_url(r["url"]) if r.get("url") else None,
            "width": r.get("width"), "height": r.get("height"), "duration": r.get("duration"),
            "retrieved_at": now_iso(), "attribution": attribution(r)}


def get(item_id: str, job_dir: str | os.PathLike, resolution: int = 1080,
        query: str | None = None, *, opener: Any = None, home: Any = None,
        environ: Any = None) -> dict[str, Any]:
    job = Path(job_dir).expanduser()
    if not job.is_dir():
        raise VPError("data", f"job folder not found: {job}")
    r = fetch_item(item_id, resolution, opener=opener, home=home, environ=environ)
    source, kind, raw_id = parse_id(item_id)
    dst = library_dir(job) / f"{source}-{kind}-{raw_id}{_ext(r['url'], kind)}"
    key = keystore.get_key(source, home, environ) or ""
    nbytes = webapi.download(r["url"], dst, opener=opener, secrets=(key,), service=source)
    prov = provenance(r, query)
    prov["bytes"] = nbytes
    prov["file"] = dst.name
    side = dst.with_name(dst.name + ".provenance.json")
    write_json(side, prov)
    return {"file": str(dst), "provenance_file": str(side), "provenance": prov}


# ---- CLI ------------------------------------------------------------------------------------

def _public(r: dict[str, Any]) -> dict[str, Any]:
    """What the dialog sees: no raw file URL (it may be signed), everything else."""
    return {k: v for k, v in r.items() if k != "url"} | {"has_file": bool(r.get("url"))}


def main(argv: list[str] | None = None, *, opener: Any = None) -> int:
    ap = argparse.ArgumentParser(prog="vp stock")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="search stock (Pexels / Pixabay)")
    s.add_argument("query", nargs="+")
    s.add_argument("--kind", choices=KINDS, default="video")
    s.add_argument("--orientation", choices=ORIENTATIONS)
    s.add_argument("--min-duration", type=float)
    s.add_argument("--source", choices=SOURCES)
    s.add_argument("--per-page", type=int, default=10)
    s.add_argument("--resolution", type=int, default=1080, help="target short side, px")
    s.add_argument("--json", action="store_true")
    g = sub.add_parser("get", help="download one result into <job>/library/stock")
    g.add_argument("id")
    g.add_argument("job")
    g.add_argument("--resolution", type=int, default=1080)
    g.add_argument("--query", help="the search that found it (kept in the provenance)")
    g.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "search":
            rows = search(" ".join(a.query), a.kind, a.orientation, a.min_duration, a.source,
                          a.per_page, a.resolution, opener=opener)
            if a.json:
                print(json.dumps([_public(r) for r in rows], ensure_ascii=False, indent=1))
            else:
                for r in rows:
                    dur = f" {r['duration']}s" if r.get("duration") else ""
                    size = f" {r['width']}x{r['height']}" if r.get("width") else ""
                    print(f"{r['id']}{size}{dur}  {attribution(r)}  {r.get('page_url') or ''}")
                if not rows:
                    print("nothing found")
            return 0
        res = get(a.id, a.job, a.resolution, a.query, opener=opener)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(f"saved: {res['file']}\nprovenance: {res['provenance_file']}\n"
                  f"{res['provenance']['attribution']} — {res['provenance']['license_url']}")
        return 0
    except keystore.NeedKey as e:
        note_failure(e, "need_key")
        print(e.code + " — " + e.msg.split(" — ", 1)[-1])
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
