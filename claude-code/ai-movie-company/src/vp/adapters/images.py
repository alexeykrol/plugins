"""Image generation adapter (capability Г, docs/SCENARIOS.md): one interface over Google Gemini
("Nano Banana") and OpenAI GPT Image, with the person's own key; every file lands in the job's
material library with a provenance sidecar (SPEC §0, principle 3).

  generate(job_dir, prompt, aspect="9:16"|"16:9"|"1:1", n=1, style=None, provider=None,
           quality=None) -> {"provider", "model", "files": [...], "cost_estimate_usd"}
  estimate(n, provider=None, aspect="1:1", quality=None) -> cost estimate (no network)
  choose_provider(provider=None) -> provider name; NEED_KEY:images when none has a key

Provider order: --provider, else env VP_IMAGES_ORDER ("openai,gemini"), else config.yaml
`images.order`, else DEFAULT_ORDER; the first one with a key wins.

Files: <job>/library/generated/gen-<UTC stamp>-<provider>-<i>.<ext>, sidecar <file>.provenance.json
= {source: "generated", provider, model, prompt, style, aspect, size, quality, created_at,
cost_estimate_usd, terms_url, note}. The cost estimate is also written to <vp_home>/costs.jsonl.

Official sources (read 2026-11-20):
  Gemini image generation  https://ai.google.dev/gemini-api/docs/image-generation
    (now served at https://aistudio.google.com/docs/image-generation): REST
    POST https://generativelanguage.googleapis.com/v1beta/interactions, header x-goog-api-key and
    Api-Revision: 2026-05-20, body {model, input: [{type: text, text}], response_format: {type:
    image, mime_type, aspect_ratio, image_size}}; images come back as base64 in
    steps[].content[] blocks of type "image". Models: gemini-nano-banana-2.1 (Nano Banana 2.1,
    "primary high-efficiency workhorse", recommended for new projects),
    gemini-3.1-flash-lite-image (Nano Banana 2 Lite, cheapest), gemini-3.1-flash-image
    (Nano Banana 2), gemini-3-pro-image (Nano Banana Pro). image_size needs an uppercase K.
    Every image carries a SynthID watermark.
  Gemini pricing  https://ai.google.dev/gemini-api/docs/pricing (aistudio.google.com/docs/pricing):
    image models have NO free tier (paid tier only).
  OpenAI image generation  https://developers.openai.com/api/docs/guides/image-generation:
    POST https://api.openai.com/v1/images/generations {model, prompt, n, size, quality,
    output_format} -> data[].b64_json; current models GPT-Image-2.6 Sunburst / Flare
    (gpt-image-2.6-sunburst, gpt-image-2.6-flare); custom WIDTHxHEIGHT allowed (multiples of 16,
    ratio <= 3:1, 655 360..8 294 400 px); quality low|medium|high|xhigh|max|auto; the
    organization may need "API Organization Verification" before GPT Image models work.
  OpenAI pricing  https://developers.openai.com/api/docs/pricing: gpt-image-2.6-* image output
    $30 / 1M tokens, text input $5 / 1M. Output tokens per image follow the official calculator
    of the guide (GptImageTokenCalculator, ported in `openai_output_tokens`).
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.costs import CostJournal
from ..core.errors import VPError
from ..core.io import now_iso, read_yaml, write_json
from ..core.paths import Job, vp_home
from .. import keys as keystore
from . import webapi

ASPECTS = ("9:16", "16:9", "1:1")
PROVIDERS = ("gemini", "openai")
DEFAULT_ORDER = ("gemini", "openai")   # Nano Banana 2.1 at 1K is the cheaper good-quality default

# ---- Gemini ---------------------------------------------------------------------------------

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_API_REVISION = "2026-05-20"
GEMINI_MODEL = "gemini-nano-banana-2.1"
GEMINI_IMAGE_SIZE = "1K"
# USD per output image, paid tier, standard (not batch); https://ai.google.dev/gemini-api/docs/pricing
# read 2026-11-20. Image output $30/1M tokens for nano-banana-2.1 and flash-lite (1K = 1120 tokens),
# $60/1M for 3.1-flash-image.
GEMINI_PRICE_PER_IMAGE = {
    "gemini-nano-banana-2.1": {"1K": 0.0336, "2K": 0.0504, "4K": 0.113},
    "gemini-3.1-flash-lite-image": {"1K": 0.0336},
    "gemini-3.1-flash-image": {"0.5K": 0.045, "1K": 0.067, "2K": 0.101, "4K": 0.151},
}
GEMINI_TEXT_INPUT_PER_M = {"gemini-nano-banana-2.1": 1.50, "gemini-3.1-flash-lite-image": 0.25,
                           "gemini-3.1-flash-image": 0.50}
GEMINI_TERMS_URL = "https://ai.google.dev/gemini-api/terms"

# ---- OpenAI ---------------------------------------------------------------------------------

OPENAI_URL = "https://api.openai.com/v1/images/generations"
OPENAI_MODEL = "gpt-image-2.6-flare"     # "fast, high-quality everyday image generation"
OPENAI_QUALITY = "medium"
OPENAI_IMAGE_OUTPUT_PER_M = 30.0         # USD / 1M image output tokens (gpt-image-2.6-*)
OPENAI_TEXT_INPUT_PER_M = 5.0            # USD / 1M text input tokens
OPENAI_TERMS_URL = "https://openai.com/policies/terms-of-use/"
# multiples of 16, ratio <= 3:1, pixel budget 655 360..8 294 400 (the guide, "Size and quality")
OPENAI_SIZES = {"1:1": (1024, 1024), "16:9": (1536, 864), "9:16": (864, 1536)}

# The guide's calculator (GptImageTokenCalculator, 2026-11-20): base tiles per quality, and for
# gpt-image-2.6 a multiplier per quality.
_OPENAI_TILES = {
    "gpt-image-2": {"low": 16, "medium": 48, "high": 96},
    "gpt-image-2.5": {"low": 16, "medium": 24, "high": 48, "xhigh": 64, "max": 96},
    "gpt-image-2.6": {"low": 16, "medium": 24, "high": 48, "xhigh": 64, "max": 96},
}
_OPENAI_26_MULT = {"low": 4.2, "medium": 3.3, "high": 1, "xhigh": 1, "max": 1}


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def openai_family(model: str) -> str:
    for fam in ("gpt-image-2.6", "gpt-image-2.5", "gpt-image-2"):
        if model.startswith(fam):
            return fam
    raise VPError("data", f"no cost table for OpenAI model {model!r}")


def openai_output_tokens(model: str, width: int, height: int, quality: str) -> int:
    """Port of the official calculator: output tokens for one image."""
    fam = openai_family(model)
    o = _OPENAI_TILES[fam].get(quality)
    if o is None:
        raise VPError("data", f"quality {quality!r} is not priced for {fam}")
    long_side, short_side = max(width, height), min(width, height)
    s = o / (long_side / short_side)
    c = math.floor(s)
    d = c + c % 2 if s - c == 0.5 else _js_round(s)
    f = (o if width >= height else d) * (d if width >= height else o)
    p = math.ceil(f * (2e6 + width * height) / 4e6)
    return _js_round(p * _OPENAI_26_MULT[quality]) if fam == "gpt-image-2.6" else p


# ---- estimate -------------------------------------------------------------------------------

def _prompt_tokens(prompt: str | None) -> int:
    return max(1, len(prompt or "") // 4) if prompt else 100   # rough: 4 chars per token


def per_image_cost(provider: str, aspect: str = "1:1", quality: str | None = None,
                   model: str | None = None, prompt: str | None = None) -> dict[str, Any]:
    _check_aspect(aspect)
    if provider == "gemini":
        model = model or GEMINI_MODEL
        table = GEMINI_PRICE_PER_IMAGE.get(model)
        if not table or GEMINI_IMAGE_SIZE not in table:
            raise VPError("data", f"no price for {model} at {GEMINI_IMAGE_SIZE}")
        out = table[GEMINI_IMAGE_SIZE]
        inp = _prompt_tokens(prompt) * GEMINI_TEXT_INPUT_PER_M.get(model, 0) / 1e6
        return {"provider": provider, "model": model, "size": GEMINI_IMAGE_SIZE, "quality": None,
                "usd": round(out + inp, 5),
                "basis": f"{GEMINI_IMAGE_SIZE} image ${out} + prompt text",
                "price_source": "https://ai.google.dev/gemini-api/docs/pricing (2026-11-20)"}
    if provider == "openai":
        model = model or OPENAI_MODEL
        quality = quality or OPENAI_QUALITY
        w, h = OPENAI_SIZES[aspect]
        toks = openai_output_tokens(model, w, h, quality)
        out = toks * OPENAI_IMAGE_OUTPUT_PER_M / 1e6
        inp = _prompt_tokens(prompt) * OPENAI_TEXT_INPUT_PER_M / 1e6
        return {"provider": provider, "model": model, "size": f"{w}x{h}", "quality": quality,
                "usd": round(out + inp, 5),
                "basis": f"{toks} image output tokens x ${OPENAI_IMAGE_OUTPUT_PER_M}/1M + prompt text",
                "price_source": "https://developers.openai.com/api/docs/pricing (2026-11-20)"}
    raise VPError("data", f"unknown image provider {provider!r}; known: {', '.join(PROVIDERS)}")


def estimate(n: int, provider: str | None = None, aspect: str = "1:1",
             quality: str | None = None, prompt: str | None = None, *, home: Any = None,
             environ: Any = None) -> dict[str, Any]:
    """Cost of n images. Without `provider`: the provider that would be used; if none has a key,
    every provider is listed and `code` is NEED_KEY:images."""
    n = _check_n(n)
    try:
        chosen = [choose_provider(provider, home=home, environ=environ)]
        code = None
    except keystore.NeedKey as e:
        if provider:
            chosen, code = [provider], e.code
        else:
            chosen, code = list(PROVIDERS), e.code
    rows = []
    for p in chosen:
        c = per_image_cost(p, aspect, quality, prompt=prompt)
        rows.append({**c, "n": n, "total_usd": round(c["usd"] * n, 4)})
    out: dict[str, Any] = {"n": n, "aspect": aspect, "options": rows}
    if code:
        out["code"] = code
    return out


# ---- provider choice ------------------------------------------------------------------------

def provider_order(home: Any = None, environ: Any = None) -> list[str]:
    env = os.environ if environ is None else environ
    raw = (env.get("VP_IMAGES_ORDER") or "").strip()
    if not raw:
        h = Path(home).expanduser() if home else vp_home()
        cfg = read_yaml(h / "config.yaml", default={}) or {}
        order = ((cfg.get("images") or {}) if isinstance(cfg, dict) else {}).get("order")
        if isinstance(order, (list, tuple)):
            raw = ",".join(str(x) for x in order)
    names = [x.strip() for x in raw.split(",") if x.strip()] if raw else list(DEFAULT_ORDER)
    names = [x for x in names if x in PROVIDERS]
    return names + [p for p in PROVIDERS if p not in names]


def choose_provider(provider: str | None = None, *, home: Any = None, environ: Any = None) -> str:
    if provider:
        if provider not in PROVIDERS:
            raise VPError("data", f"unknown image provider {provider!r}; known: {', '.join(PROVIDERS)}")
        if not keystore.get_key(provider, home, environ):
            raise keystore.NeedKey("images", f"no {provider} key yet — `vp keys set {provider} "
                                             f"--from-clipboard`")
        return provider
    for p in provider_order(home, environ):
        if keystore.get_key(p, home, environ):
            return p
    raise keystore.NeedKey("images", "no image-generation key yet: a Gemini key or an OpenAI key "
                                     "is needed — `vp keys set gemini --from-clipboard`")


# ---- requests -------------------------------------------------------------------------------

def _check_aspect(aspect: str) -> None:
    if aspect not in ASPECTS:
        raise VPError("data", f"aspect must be one of {ASPECTS}, got {aspect!r}")


def _check_n(n: Any) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise VPError("data", f"n must be a whole number, got {n!r}") from None
    if not 1 <= n <= 10:
        raise VPError("data", "n must be between 1 and 10")
    return n


def full_prompt(prompt: str, style: str | None) -> str:
    p = (prompt or "").strip()
    if not p:
        raise VPError("data", "empty prompt")
    return f"{p}\n\nStyle: {style.strip()}" if style and style.strip() else p


def gemini_body(prompt: str, aspect: str, model: str = GEMINI_MODEL) -> dict[str, Any]:
    _check_aspect(aspect)
    return {"model": model,
            "input": [{"type": "text", "text": prompt}],
            "response_format": {"type": "image", "mime_type": "image/png",
                                "aspect_ratio": aspect, "image_size": GEMINI_IMAGE_SIZE}}


def openai_body(prompt: str, aspect: str, n: int, quality: str | None = None,
                model: str = OPENAI_MODEL) -> dict[str, Any]:
    _check_aspect(aspect)
    w, h = OPENAI_SIZES[aspect]
    return {"model": model, "prompt": prompt, "n": n, "size": f"{w}x{h}",
            "quality": quality or OPENAI_QUALITY, "output_format": "png"}


def _images_in(obj: Any) -> list[tuple[bytes, str]]:
    """Every base64 image block in an answer, in order: Gemini interactions (steps[].content[]
    {type: image, data, mime_type}), older generateContent (inlineData), OpenAI (b64_json)."""
    found: list[tuple[bytes, str]] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            data, mime = None, None
            if x.get("type") == "image" and isinstance(x.get("data"), str):
                data, mime = x["data"], x.get("mime_type") or x.get("mimeType") or "image/png"
            elif isinstance(x.get("b64_json"), str):
                data, mime = x["b64_json"], "image/png"
            else:
                inline = x.get("inlineData") or x.get("inline_data")
                if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                    data = inline["data"]
                    mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            if data is not None:
                try:
                    found.append((base64.b64decode(data, validate=True), mime))
                except (ValueError, TypeError):
                    raise VPError("contract", "image provider: an image is not valid base64") from None
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return found


def _call_gemini(prompt: str, aspect: str, n: int, key: str, opener: Any) -> list[tuple[bytes, str]]:
    out: list[tuple[bytes, str]] = []
    for _ in range(n):     # one image per interaction
        ans = webapi.request_json("POST", GEMINI_URL, body=gemini_body(prompt, aspect),
                                  headers={"x-goog-api-key": key,
                                           "Api-Revision": GEMINI_API_REVISION},
                                  opener=opener, secrets=(key,), service="gemini",
                                  timeout_s=180)
        imgs = _images_in(ans)
        if not imgs:
            raise VPError("contract", "gemini: the answer has no image (the prompt may have been "
                                      "refused by the safety filter — rephrase it)")
        out.append(imgs[-1])
    return out


def _call_openai(prompt: str, aspect: str, n: int, quality: str | None, key: str,
                 opener: Any) -> list[tuple[bytes, str]]:
    ans = webapi.request_json("POST", OPENAI_URL, body=openai_body(prompt, aspect, n, quality),
                              headers={"Authorization": f"Bearer {key}"}, opener=opener,
                              secrets=(key,), service="openai", timeout_s=300)
    imgs = _images_in(ans)
    if not imgs:
        raise VPError("contract", "openai: the answer has no image (the prompt may have been "
                                  "refused by moderation — rephrase it)")
    return imgs[:n]


EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def library_dir(job_dir: str | os.PathLike) -> Path:
    return Path(job_dir).expanduser().resolve() / "library" / "generated"


def generate(job_dir: str | os.PathLike, prompt: str, aspect: str = "9:16", n: int = 1,
             style: str | None = None, provider: str | None = None, quality: str | None = None,
             *, opener: Any = None, home: Any = None, environ: Any = None) -> dict[str, Any]:
    job = Path(job_dir).expanduser()
    if not job.is_dir():
        raise VPError("data", f"job folder not found: {job}")
    _check_aspect(aspect)
    n = _check_n(n)
    text = full_prompt(prompt, style)
    prov = choose_provider(provider, home=home, environ=environ)
    key = keystore.require_key(prov, home, environ)
    cost = per_image_cost(prov, aspect, quality, prompt=text)
    if prov == "gemini":
        images = _call_gemini(text, aspect, n, key, opener)
    else:
        images = _call_openai(text, aspect, n, cost["quality"], key, opener)
    out_dir = library_dir(job)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    files = []
    terms = GEMINI_TERMS_URL if prov == "gemini" else OPENAI_TERMS_URL
    for i, (data, mime) in enumerate(images, 1):
        dst = out_dir / f"gen-{stamp}-{prov}-{i}{EXT.get(mime, '.png')}"
        k = 1
        while dst.exists():
            k += 1
            dst = out_dir / f"gen-{stamp}-{prov}-{i}-{k}{EXT.get(mime, '.png')}"
        tmp = dst.with_name(f".{dst.name}.part")
        tmp.write_bytes(data)
        os.replace(tmp, dst)
        side = dst.with_name(dst.name + ".provenance.json")
        record = {"source": "generated", "provider": prov, "model": cost["model"],
                  "prompt": prompt, "style": style, "aspect": aspect, "size": cost["size"],
                  "quality": cost["quality"], "created_at": now_iso(),
                  "cost_estimate_usd": cost["usd"], "terms_url": terms, "file": dst.name,
                  "bytes": len(data),
                  "note": ("generated with the person's own " + prov + " account; the provider's "
                           "terms apply" + ("; carries a SynthID watermark" if prov == "gemini" else ""))}
        write_json(side, record)
        files.append({"file": str(dst), "provenance_file": str(side)})
    total = round(cost["usd"] * len(images), 5)
    try:
        CostJournal(Path(home).expanduser() if home else vp_home(), Job(job).job_id).add(
            "api", op="image_generation", provider=prov, model=cost["model"], units=len(images),
            usd=total, estimate=True)
    except OSError:
        pass   # the journal is bookkeeping; the images are already saved
    return {"provider": prov, "model": cost["model"], "aspect": aspect, "files": files,
            "cost_estimate_usd": total}


# ---- CLI ------------------------------------------------------------------------------------

def main(argv: list[str] | None = None, *, opener: Any = None) -> int:
    ap = argparse.ArgumentParser(prog="vp images")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("estimate", help="cost of K images before generating (no network)")
    e.add_argument("--n", type=int, default=1)
    e.add_argument("--aspect", choices=ASPECTS, default="1:1")
    e.add_argument("--provider", choices=PROVIDERS)
    e.add_argument("--quality", help="OpenAI only: low|medium|high|xhigh|max")
    e.add_argument("--json", action="store_true")
    g = sub.add_parser("generate", help="generate K images into <job>/library/generated")
    g.add_argument("job")
    g.add_argument("--prompt", required=True)
    g.add_argument("--aspect", choices=ASPECTS, default="9:16")
    g.add_argument("--n", type=int, default=1)
    g.add_argument("--style")
    g.add_argument("--provider", choices=PROVIDERS)
    g.add_argument("--quality", help="OpenAI only: low|medium|high|xhigh|max")
    g.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "estimate":
            res = estimate(a.n, a.provider, a.aspect, a.quality)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
            else:
                for r in res["options"]:
                    print(f"{r['provider']} {r['model']} {r['size']}: {r['n']} x ${r['usd']:.4f} "
                          f"= ${r['total_usd']:.4f} ({r['basis']})")
                if res.get("code"):
                    print(res["code"])
            return 0
        res = generate(a.job, a.prompt, a.aspect, a.n, a.style, a.provider, a.quality,
                       opener=opener)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            for f in res["files"]:
                print(f"saved: {f['file']}")
            print(f"{res['provider']} {res['model']}: ≈ ${res['cost_estimate_usd']:.4f}")
        return 0
    except keystore.NeedKey as e:
        print(e.code + " — " + e.msg.split(" — ", 1)[-1])
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
