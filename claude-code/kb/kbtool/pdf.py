"""PDF → текст через pypdf (опциональный extra ``kb[pdf]``; SPEC §6).

COPIED: ``_normalize_text`` и ``extract_pdf_text`` — из Saved Downloader/
bounded_contexts/source_ingest/src/adapt_pdf_supplementary.py @ d05295d (repo HEAD 61f01cb,
branch refactor/platform-only), 2026-11-06; тела без изменений, кроме приёма bytes вместо пути.
Без текстового слоя → '' (скан; OCR — v2). Без pypdf → вызывающий пишет дефицит.
"""
from __future__ import annotations

import io
import re


def load_pypdf():
    try:
        import pypdf  # noqa: PLC0415
        return pypdf
    except ImportError:
        return None


def _normalize_text(raw: str) -> str:
    s = raw.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t ]+", " ", s)
    lines = [ln.strip() for ln in s.split("\n")]
    out, blank = [], 0
    for ln in lines:
        if ln:
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank <= 1 and out:
                out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out).strip()


def extract_pdf_text(raw: bytes, pypdf_mod) -> str:
    reader = pypdf_mod.PdfReader(io.BytesIO(raw))
    pages = []
    for page in reader.pages:
        txt = page.extract_text() or ""
        if txt.strip():
            pages.append(txt)
    return _normalize_text("\n\n".join(pages))
