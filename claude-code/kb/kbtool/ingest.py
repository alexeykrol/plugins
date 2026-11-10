"""Опись (scan) и извлечение текста в единицы (extract + split) — SPEC §5 шаги 1–3, §6, §8.

Папка документов только читается. Всё производное — в ``$KB_HOME/<slug>/``:
``scan.json`` (опись), ``extract.json`` (источники и единицы без текста), ``units/<id>.txt``.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import documents, guards, pdf
from .config import Base, KbError, write_private
from .runlog import log_run, set_status
from .split import split_text

TEXT_FORMATS = {".md", ".markdown", ".txt"}
MVP_FORMATS = TEXT_FORMATS | {".html", ".htm", ".docx", ".pdf"}
LATER_FORMATS = {".xlsx", ".csv", ".tsv", ".json", ".yaml", ".yml", ".pptx", ".epub", ".rtf"}
_FNAME_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?:[ _T](\d{2})[:.]?(\d{2}))?(?!\d)")
_FRONT_DATE = re.compile(r"\A---\s*\n(?:.*\n){0,40}?date:\s*[\"']?(\d{4}-\d{2}-\d{2})", re.M)


NO_PDF_READER = "no-pdf-reader"
# Причины исключения простыми словами — для описи и подтверждения перед сборкой (U3).
REASON_RU = {
    "hidden": "скрытые файлы и папки",
    "symlink": "ссылки на другие файлы",
    "symlink-outside": "ссылки за пределы папки",
    "outside-corpus": "файлы за пределами папки",
    "secret-like": "похожи на пароли или ключи — не читаются ради безопасности",
    "excluded": "исключены настройкой",
    "unsupported": "формат не поддерживается",
    "unsupported-v1": "формат будет поддержан позже",
    NO_PDF_READER: "PDF пока не читаются",
    "no_pdf_extractor": "PDF пока не читаются",
    "no_text_layer": "PDF без текста (сканы)",
    "over-limit": "слишком большие",
    "duplicate-of": "повторяют другой файл",
    "injection-like": "содержат похожие на команды для ИИ фразы — не размечаются ради безопасности",
    "empty": "пустые",
    "binary": "не текст",
    "read_failed": "не удалось прочитать",
}
CONFIRM_QUESTION = "Сборка прочитает эти документы моделью — это тратит лимит вашей подписки. Продолжать?"
MAX_LISTED = 10


def human_bytes(n: int) -> str:
    """Объём простыми словами: «12 КБ», «3,4 МБ»."""
    if n < 1024:
        return f"{n} байт"
    if n < 1024 * 1024:
        return f"{round(n / 1024)} КБ"
    return f"{n / 1024 / 1024:.1f} МБ".replace(".", ",")


def _reason_key(reason: str) -> str:
    return (reason or "").split(":", 1)[0].strip()


def excluded_summary(rows: list) -> list:
    """Сколько исключено и почему: [{reason, label, count}] — без содержимого файлов."""
    counts: dict = {}
    for r in rows:
        if r.get("path") == "(корпус)":
            continue
        key = _reason_key(r.get("reason", ""))
        counts[key] = counts.get(key, 0) + 1
    return [dict(reason=k, label=REASON_RU.get(k, k), count=v)
            for k, v in sorted(counts.items(), key=lambda x: -x[1])]


def pdf_notice(paths: list) -> str:
    """«PDF пока не читаются — пропущено N файлов: a.pdf, b.pdf и ещё K» (не больше 10 имён)."""
    if not paths:
        return ""
    shown = ", ".join(paths[:MAX_LISTED])
    rest = len(paths) - MAX_LISTED
    tail = f" и ещё {rest}" if rest > 0 else ""
    return f"PDF пока не читаются — пропущено {len(paths)} {files_word(len(paths))}: {shown}{tail}"


def files_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "файл"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "файла"
    return "файлов"


def notes_for(rows: list) -> list:
    """Сообщения пользователю простыми словами (сейчас — только про PDF)."""
    pdfs = [r["path"] for r in rows if r.get("reason") in (NO_PDF_READER, "no_pdf_extractor")]
    return [pdf_notice(pdfs)] if pdfs else []


def _is_inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _forced(cfg: dict, corpus: str) -> set[str]:
    out = set()
    for rel in cfg.get("include_forced") or []:
        full = os.path.normpath(os.path.join(corpus, rel))
        if not _is_inside(full, corpus) or ".." in Path(rel).parts or os.path.isabs(rel):
            raise KbError(f"include_forced: путь вне корпуса не принимается: {rel}")
        out.add(os.path.relpath(full, corpus))
    return out


def scan(base: Base) -> dict:
    """Опись папки: что войдёт, что исключено и почему, оценка объёма (U3)."""
    t0 = time.time()
    cfg = base.config()
    limits = cfg["limits"]
    corpus = str(base.corpus())
    if not os.path.isdir(corpus):
        raise KbError(f"папки корпуса больше нет: {corpus}")
    forced = _forced(cfg, corpus)
    excl = cfg.get("exclude") or []
    has_pdf = pdf.load_pypdf() is not None
    rows = []

    for dirpath, dirnames, filenames in os.walk(corpus, followlinks=False):
        rel_dir = os.path.relpath(dirpath, corpus)
        keep = []
        for d in sorted(dirnames):
            rel = os.path.normpath(os.path.join(rel_dir, d))
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                target = os.path.realpath(full)
                rows.append(dict(path=rel + "/", status="skipped",
                                 reason="symlink-outside" if not _is_inside(target, corpus) else "symlink"))
            elif d.startswith(".") and not any(f.startswith(rel + os.sep) for f in forced):
                rows.append(dict(path=rel + "/", status="skipped", reason="hidden"))
            elif any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel + "/", p) for p in excl):
                rows.append(dict(path=rel + "/", status="skipped", reason="excluded"))
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            rel = os.path.normpath(os.path.join(rel_dir, fn))
            full = os.path.join(dirpath, fn)
            row = dict(path=rel, format=Path(fn).suffix.lower() or "(нет)")
            is_forced = rel in forced
            if os.path.islink(full):
                target = os.path.realpath(full)
                row.update(status="skipped",
                           reason="symlink-outside" if not _is_inside(target, corpus) else "symlink")
                rows.append(row)
                continue
            real = os.path.realpath(full)
            if not _is_inside(real, corpus):           # S1: на всякий случай после realpath
                row.update(status="skipped", reason="outside-corpus")
                rows.append(row)
                continue
            st = os.stat(full)
            row.update(size=st.st_size, mtime=int(st.st_mtime))
            if fn.startswith(".") and not is_forced and not guards.secret_like_name(rel):
                row.update(status="skipped", reason="hidden")
            elif guards.secret_like_name(rel) and not is_forced:
                row.update(status="skipped", reason="secret-like")
            elif any(fnmatch.fnmatch(rel, p) for p in excl) and not is_forced:
                row.update(status="skipped", reason="excluded")
            elif row["format"] not in MVP_FORMATS:
                row.update(status="skipped",
                           reason="unsupported-v1" if row["format"] in LATER_FORMATS else "unsupported")
            elif row["format"] == ".pdf" and not has_pdf:
                row.update(status="skipped", reason=NO_PDF_READER)
            elif st.st_size > limits["max_file_bytes"]:
                row.update(status="blocked", reason=f"over-limit: файл > {limits['max_file_bytes']} байт")
            else:
                row.update(status="candidate", reason="forced" if is_forced else "")
            rows.append(row)

    cands = [r for r in rows if r["status"] == "candidate"]
    blocked = [r for r in rows if r["status"] == "blocked"]
    total = sum(r["size"] for r in cands)
    if len(cands) > limits["max_files"]:
        blocked.append(dict(path="(корпус)", status="blocked",
                            reason=f"over-limit: файлов {len(cands)} > {limits['max_files']}"))
    if total > limits["max_text_bytes"]:
        blocked.append(dict(path="(корпус)", status="blocked",
                            reason=f"over-limit: объём {total} > {limits['max_text_bytes']} байт"))
    by_format = {}
    for r in rows:
        f = by_format.setdefault(r.get("format", "(папка)"), {"files": 0, "bytes": 0, "accepted": 0})
        f["files"] += 1
        f["bytes"] += r.get("size", 0)
        f["accepted"] += r["status"] == "candidate"
    est_chars = sum(r["size"] if r["format"] in TEXT_FORMATS | {".html", ".htm"} else r["size"] // 4
                    for r in cands)
    skipped = [r for r in rows if r["status"] == "skipped"]
    result = dict(corpus=corpus, ts=datetime.now().isoformat(timespec="seconds"), files=rows,
                  by_format=by_format, candidates=len(cands), bytes=total, blocked=blocked,
                  bytes_human=human_bytes(total), excluded=excluded_summary(skipped + blocked),
                  notes=notes_for(rows),
                  estimate=dict(chars_upper=est_chars, tokens=int(est_chars / 3 * 1.3),
                                batches=max(1, -(-est_chars // limits["batch_chars"])) if cands else 0))
    _dump(base.scan_json, result)
    set_status(base, "scan", candidates=len(cands), blocked=len(blocked),
               skipped=sum(r["status"] == "skipped" for r in rows))
    log_run(base, "scan", units_in=len(rows), units_out=len(cands), duration=time.time() - t0,
            errors=[b["reason"] for b in blocked])
    return result


def _decode(raw: bytes) -> tuple[str | None, str]:
    head = raw[:8192]
    if b"\x00" in head or (head and sum(b < 9 or 13 < b < 32 for b in head) / len(head) > 0.05):
        return None, "binary"
    for enc in ("utf-8-sig", "cp1251", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None, "binary"


def _date(rel: str, text: str, meta: dict, mtime: int) -> tuple[str, str]:
    m = _FNAME_DATE.search(Path(rel).name)
    if m:
        y, mo, d, hh, mm = m.groups()
        try:
            datetime(int(y), int(mo), int(d))
            return (f"{y}-{mo}-{d}" + (f" {hh}{mm}" if hh else "")), "filename"
        except ValueError:
            pass
    if meta.get("date"):
        return meta["date"][:10], f"document:{meta.get('date_basis')}"
    m = _FRONT_DATE.search(text[:4000])
    if m:
        return m.group(1), "frontmatter"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"), "mtime"


def _extract_one(fmt: str, raw: bytes) -> tuple[str, str, dict]:
    """→ (text, state, meta); state: ok | empty | binary | read_failed | no_text_layer | no_pdf_extractor."""
    meta: dict = {}
    if fmt in TEXT_FORMATS:
        text, enc = _decode(raw)
        if text is None:
            return "", "binary", meta
        meta["encoding"] = enc
    elif fmt in (".html", ".htm", ".docx"):
        text, state = documents.extract(fmt, raw, meta)
        if state == "read_failed":
            return "", "read_failed", meta
    elif fmt == ".pdf":
        mod = pdf.load_pypdf()
        if mod is None:
            return "", "no_pdf_extractor", meta
        try:
            text = pdf.extract_pdf_text(raw, mod)
        except Exception as exc:  # pypdf бросает разное на битых файлах — это дефицит, не падение
            meta["error"] = type(exc).__name__
            return "", "read_failed", meta
        if not text.strip():
            return "", "no_text_layer", meta
    else:
        return "", "unsupported", meta
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return (text, "ok", meta) if text.strip() else ("", "empty", meta)


def extract(base: Base) -> dict:
    """Текст → единицы (части ≤ max_unit_chars) → units/<id>.txt + extract.json (U5)."""
    t0 = time.time()
    if not base.scan_json.exists():
        scan(base)
    import json
    sc = json.loads(base.scan_json.read_text(encoding="utf-8"))
    if sc["blocked"]:
        raise KbError("опись остановлена лимитом: " + "; ".join(f"{b['path']}: {b['reason']}" for b in sc["blocked"])
                      + " — исключите файлы (exclude в kb.yaml) или поднимите лимит в kb.yaml осознанно")
    cfg = base.config()
    limit = cfg["limits"]["max_unit_chars"]
    corpus = str(base.corpus())
    forced = _forced(cfg, corpus)
    sources, units, seen = [], [], {}
    keep_ids = set()
    for row in sc["files"]:
        rel = row["path"]
        src = dict(path=rel, format=row.get("format"), size=row.get("size"), mtime=row.get("mtime"),
                   status="skipped", reason=row["reason"], id=None, sha256=None, units=[])
        sources.append(src)
        if row["status"] != "candidate":
            continue
        full = os.path.join(corpus, rel)
        if not _is_inside(os.path.realpath(full), corpus) or os.path.islink(full):
            src["reason"] = "outside-corpus"
            continue
        with open(full, "rb") as f:
            raw = f.read()
        sha = hashlib.sha256(raw).hexdigest()
        src.update(sha256=sha, id=sha[:12])
        if sha in seen:
            src.update(status="skipped", reason=f"duplicate-of: {seen[sha]}")
            continue
        seen[sha] = rel
        text, state, meta = _extract_one(row["format"], raw)
        if state != "ok":
            src.update(status="deficit", reason=state)
            continue
        if rel not in forced:
            kind = guards.secret_kind(text)
            if kind:
                src.update(status="skipped", reason=f"secret-like: {kind}")
                continue
            kind = guards.injection_kind(text)
            if kind:
                src.update(status="skipped", reason=f"injection-like: {kind}")
                continue
        date, basis = _date(rel, text, meta, row.get("mtime") or 0)
        parts = split_text(text, limit)
        stem = Path(rel).stem
        src.update(status="ok", reason=src["reason"] or "", date=date, date_basis=basis,
                   encoding=meta.get("encoding"))
        for n, part in enumerate(parts, 1):
            uid = sha[:12] if len(parts) == 1 else f"{sha[:12]}-{n:02d}"
            title = stem if len(parts) == 1 else f"{stem} (часть {n}/{len(parts)})"
            write_private(base.units_dir / f"{uid}.txt", part)
            keep_ids.add(uid)
            units.append(dict(id=uid, source_id=sha[:12], part_no=n, parts=len(parts), title=title,
                              path=rel, chars=len(part), date=date, date_basis=basis))
            src["units"].append(uid)
    for p in base.units_dir.glob("*.txt"):          # единицы исчезнувших/изменённых документов
        if p.stem not in keep_ids:
            p.unlink()
    total_chars = sum(u["chars"] for u in units)
    result = dict(corpus=corpus, ts=datetime.now().isoformat(timespec="seconds"), sources=sources,
                  units=units, chars=total_chars, tokens_est=int(total_chars / 3 * 1.3),
                  notes=notes_for(sources))
    _dump(base.extract_json, result)
    counts = {}
    for s in sources:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    set_status(base, "extract", units=len(units), sources=counts)
    log_run(base, "extract", units_in=sum(r["status"] == "candidate" for r in sc["files"]), units_out=len(units),
            tokens_est=result["tokens_est"], duration=time.time() - t0,
            errors=[f"{s['path']}: {s['reason']}" for s in sources if s["status"] == "deficit"])
    return result


def summary(base: Base) -> dict:
    """Сводка для подтверждения перед разметкой (kb-build): что войдёт, объём, что исключено и почему.

    Считает код, а не модель: модель только показывает это пользователю и задаёт вопрос.
    """
    ext = load_extract(base)
    ok = [s for s in ext["sources"] if s["status"] == "ok"]
    out = [s for s in ext["sources"] if s["status"] != "ok"]
    size = sum(s.get("size") or 0 for s in ok)
    return dict(corpus=ext["corpus"], files=len(ok), files_word=files_word(len(ok)), bytes=size,
                bytes_human=human_bytes(size), units=len(ext["units"]), chars=ext["chars"],
                tokens_est=ext["tokens_est"], excluded_total=len(out), excluded=excluded_summary(out),
                notes=ext.get("notes") or notes_for(ext["sources"]), question=CONFIRM_QUESTION)


def load_extract(base: Base) -> dict:
    import json
    if not base.extract_json.exists():
        raise KbError("нет extract.json — сначала kbtool extract")
    return json.loads(base.extract_json.read_text(encoding="utf-8"))


def _dump(path: Path, data) -> None:
    import json
    write_private(path, json.dumps(data, ensure_ascii=False, indent=1))
