"""Сборка ``kb.sqlite`` из единиц и разметки (SPEC §4, §5 шаг 9).

Валидация разметки — до записи; ошибка → старая база не тронута. Запись — во временный
файл рядом, затем ``os.replace`` (атомарно; INV-1 SD: никаких DROP+CREATE на живой базе).
Права 0600. Повторная сборка без изменений даёт ту же базу (кроме меток времени в meta/runs).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime

from . import SCHEMA_VERSION
from .config import EDGE_CONFIDENCE, EDGE_TYPES, RESOLUTIONS, VERDICTS, Base, KbError
from .ingest import load_extract
from .markup import all_markup, norm_concept, read_tsv, vocab_map
from .runlog import log_run, set_status

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sources (
  path TEXT PRIMARY KEY, id TEXT, sha256 TEXT, size INTEGER, mtime INTEGER, format TEXT,
  status TEXT NOT NULL, reason TEXT          -- ok | skipped | deficit | missing
);
CREATE TABLE units (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, part_no INTEGER NOT NULL, parts INTEGER NOT NULL,
  title TEXT NOT NULL, path TEXT NOT NULL, text TEXT NOT NULL, chars INTEGER NOT NULL,
  date TEXT, date_basis TEXT
);
CREATE VIRTUAL TABLE units_fts USING fts5(id UNINDEXED, title, summary, text, tokenize='unicode61');
CREATE TABLE markup (
  unit_id TEXT PRIMARY KEY REFERENCES units(id), category TEXT, subtopic TEXT, genre TEXT,
  practical INTEGER, summary TEXT
);
CREATE TABLE concepts (name TEXT PRIMARY KEY, definition TEXT);
CREATE TABLE unit_concepts (
  unit_id TEXT REFERENCES units(id), concept TEXT REFERENCES concepts(name), weight INTEGER NOT NULL,
  PRIMARY KEY (unit_id, concept)
);
CREATE TABLE edges (
  src TEXT REFERENCES units(id), dst TEXT REFERENCES units(id), type TEXT NOT NULL,
  confidence TEXT NOT NULL, note TEXT, PRIMARY KEY (src, dst, type)
);
CREATE TABLE verdicts (unit_id TEXT PRIMARY KEY, verdict TEXT NOT NULL, date TEXT NOT NULL, note TEXT);
CREATE TABLE conflicts (
  id TEXT PRIMARY KEY, unit_id TEXT NOT NULL, claim_kb TEXT NOT NULL, claim_model TEXT,
  resolution TEXT NOT NULL, date TEXT NOT NULL
);
CREATE TABLE misses (date TEXT NOT NULL, question TEXT NOT NULL, found_id TEXT);
CREATE TABLE runs (ts TEXT, stage TEXT, units_in INTEGER, units_out INTEGER, tokens_est INTEGER,
                   duration REAL, errors TEXT);

CREATE VIEW concept_links AS
SELECT a.unit_id AS src, b.unit_id AS dst, a.concept, a.weight + b.weight AS strength
FROM unit_concepts a JOIN unit_concepts b ON a.concept = b.concept AND a.unit_id < b.unit_id;

CREATE VIEW concept_timeline AS
SELECT uc.concept, COUNT(*) AS units, MIN(u.date) AS first, MAX(u.date) AS last,
       GROUP_CONCAT(DISTINCT m.category) AS categories
FROM unit_concepts uc JOIN units u ON u.id = uc.unit_id LEFT JOIN markup m ON m.unit_id = uc.unit_id
GROUP BY uc.concept;

CREATE VIEW unit_neighbors AS
SELECT src AS unit_id, dst AS other, type AS via, 'edge' AS kind FROM edges
UNION ALL SELECT dst, src, type, 'edge' FROM edges
UNION ALL SELECT src, dst, concept, 'concept' FROM concept_links
UNION ALL SELECT dst, src, concept, 'concept' FROM concept_links;
"""


def _lang(texts) -> str:
    sample = "".join(t[:2000] for t in texts[:50])
    cyr = sum("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in sample)
    lat = sum("a" <= ch.lower() <= "z" for ch in sample)
    return "ru" if cyr >= lat else "en"


def collect(base: Base) -> dict:
    """Всё, что пойдёт в базу, с проверкой. Ошибки → KbError, предупреждения → в отчёт."""
    cfg = base.config()
    ex = load_extract(base)
    units = {u["id"]: dict(u) for u in ex["units"]}
    errors, warnings = [], []
    for uid, u in units.items():
        p = base.units_dir / f"{uid}.txt"
        if not p.exists():
            errors.append(f"нет текста единицы {uid} — повторите kbtool extract")
            continue
        u["text"] = p.read_text(encoding="utf-8")

    markup, origin = {}, {}
    cats = set(cfg.get("categories") or [])
    for fname, r in all_markup(base, "units"):
        uid = r.get("id")
        if uid not in units:
            warnings.append(f"{fname}: разметка устаревшей единицы {uid} пропущена")
            continue
        if uid in markup:
            errors.append(f"единица {uid} размечена дважды: {origin[uid]} и {fname}")
            continue
        if r.get("genre") not in cfg["genres"]:
            errors.append(f"{fname}: жанр {r.get('genre')!r} у {uid}")
        if cats and r.get("category") not in cats:
            errors.append(f"{fname}: категория {r.get('category')!r} у {uid} не из kb.yaml")
        if r.get("practical") not in ("0", "1"):
            errors.append(f"{fname}: practical {r.get('practical')!r} у {uid}")
        markup[uid], origin[uid] = r, fname

    vm = vocab_map(base)
    uc, defs = {}, {}
    for fname, r in all_markup(base, "concepts"):
        uid = r.get("id")
        if uid not in units:
            warnings.append(f"{fname}: понятия устаревшей единицы {uid} пропущены")
            continue
        raw = norm_concept(r.get("concept", ""))
        c = vm.get(raw, raw)
        if not c:
            errors.append(f"{fname}: пустое понятие у {uid}")
            continue
        if r.get("weight") not in ("1", "2"):
            errors.append(f"{fname}: вес {r.get('weight')!r} у {uid}")
            continue
        uc[(uid, c)] = max(uc.get((uid, c), 0), int(r["weight"]))
        if r.get("definition") and c not in defs and raw == c:
            defs[c] = r["definition"]
    for fname, r in all_markup(base, "concepts"):      # определение синонима — если у канона нет своего
        raw = norm_concept(r.get("concept", ""))
        c = vm.get(raw, raw)
        if r.get("definition") and c not in defs:
            defs[c] = r["definition"]

    edges = {}
    for fname, r in all_markup(base, "edges"):
        if r.get("src") not in units or r.get("dst") not in units:
            warnings.append(f"{fname}: связь {r.get('src')}→{r.get('dst')} с устаревшей единицей пропущена")
            continue
        if r.get("type") not in EDGE_TYPES:
            errors.append(f"{fname}: тип связи {r.get('type')!r}")
        if r.get("confidence") not in EDGE_CONFIDENCE:
            errors.append(f"{fname}: уверенность {r.get('confidence')!r}")
        edges[(r["src"], r["dst"], r["type"])] = (r.get("confidence"), r.get("note"))
    by_source = {}
    for u in units.values():
        by_source.setdefault(u["source_id"], []).append(u)
    for parts in by_source.values():                     # K8: части документа связаны кодом
        parts.sort(key=lambda u: u["part_no"])
        for prev, nxt in zip(parts, parts[1:]):
            edges.setdefault((nxt["id"], prev["id"], "continues"),
                             ("extracted", f"часть {nxt['part_no']}/{nxt['parts']} того же документа"))

    verdicts = {}
    for r in read_tsv(base.markup / "verdicts.tsv"):
        if r.get("verdict") not in VERDICTS:
            errors.append(f"verdicts.tsv: вердикт {r.get('verdict')!r} у {r.get('id')}")
        elif r.get("id") in units:
            verdicts[r["id"]] = r                          # последняя строка по единице побеждает
        else:
            warnings.append(f"verdicts.tsv: вердикт устаревшей единицы {r.get('id')} хранится, в базу не идёт")
    conflicts = {}
    for r in read_tsv(base.markup / "conflicts.tsv"):
        if r.get("resolution") not in RESOLUTIONS:
            errors.append(f"conflicts.tsv: решение {r.get('resolution')!r}")
        else:
            conflicts[r["id"]] = r
    misses = read_tsv(base.markup / "misses.tsv")
    if errors:
        raise KbError("ошибки разметки — база не тронута:\n  " + "\n  ".join(errors[:60]))
    return dict(ex=ex, units=units, markup=markup, uc=uc, defs=defs, edges=edges, verdicts=verdicts,
                conflicts=conflicts, misses=misses, warnings=warnings)


def _write(con: sqlite3.Connection, base: Base, d: dict) -> None:
    con.executescript(SCHEMA)
    units = d["units"]
    ordered = sorted(units.values(), key=lambda u: u["id"])
    meta = dict(schema_version=str(SCHEMA_VERSION), corpus_path=d["ex"]["corpus"],
                built=datetime.now().isoformat(timespec="seconds"), lang=_lang([u["text"] for u in ordered]))
    con.executemany("INSERT INTO meta VALUES (?,?)", sorted(meta.items()))
    con.executemany("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",
                    [(s["path"], s.get("id"), s.get("sha256"), s.get("size"), s.get("mtime"), s.get("format"),
                      s["status"], s.get("reason")) for s in sorted(d["ex"]["sources"], key=lambda s: s["path"])])
    con.executemany("INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(u["id"], u["source_id"], u["part_no"], u["parts"], u["title"], u["path"], u["text"],
                      u["chars"], u["date"], u["date_basis"]) for u in ordered])
    con.executemany("INSERT INTO units_fts VALUES (?,?,?,?)",
                    [(u["id"], u["title"], (d["markup"].get(u["id"]) or {}).get("summary", ""), u["text"])
                     for u in ordered])
    con.executemany("INSERT INTO markup VALUES (?,?,?,?,?,?)",
                    [(k, r.get("category"), r.get("subtopic"), r.get("genre"), int(r.get("practical") or 0),
                      r.get("summary")) for k, r in sorted(d["markup"].items())])
    names = sorted({c for _, c in d["uc"]})
    con.executemany("INSERT INTO concepts VALUES (?,?)", [(c, d["defs"].get(c)) for c in names])
    con.executemany("INSERT INTO unit_concepts VALUES (?,?,?)", [(u, c, w) for (u, c), w in sorted(d["uc"].items())])
    con.executemany("INSERT INTO edges VALUES (?,?,?,?,?)",
                    [(s, t, ty, cf, n) for (s, t, ty), (cf, n) in sorted(d["edges"].items())])
    con.executemany("INSERT INTO verdicts VALUES (?,?,?,?)",
                    [(k, r["verdict"], r.get("date") or "", r.get("note")) for k, r in sorted(d["verdicts"].items())])
    con.executemany("INSERT INTO conflicts VALUES (?,?,?,?,?,?)",
                    [(k, r["unit_id"], r["claim_kb"], r.get("claim_model"), r["resolution"], r["date"])
                     for k, r in sorted(d["conflicts"].items())])
    con.executemany("INSERT INTO misses VALUES (?,?,?)",
                    [(r.get("date"), r.get("question"), r.get("found_id")) for r in d["misses"]])
    rows = []
    for p in sorted(base.runs.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append((r.get("ts"), r.get("stage"), r.get("units_in"), r.get("units_out"), r.get("tokens_est"),
                     r.get("duration"), json.dumps(r.get("errors") or [], ensure_ascii=False)))
    con.executemany("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", rows)


def build(base: Base) -> dict:
    t0 = time.time()
    d = collect(base)
    tmp = base.db.with_name(base.db.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    try:
        con = sqlite3.connect(tmp)
        try:
            _write(con, base, d)
            con.commit()
        finally:
            con.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, base.db)                      # атомарная замена
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    types = Counter(t for (_, _, t) in d["edges"])
    res = dict(units=len(d["units"]), marked=len(d["markup"]), pending=len(d["units"]) - len(d["markup"]),
               concepts=len({c for _, c in d["uc"]}), bindings=len(d["uc"]), edges=len(d["edges"]),
               edge_types=dict(sorted(types.items())), contradicts=types.get("contradicts", 0),
               verdicts=len(d["verdicts"]), warnings=d["warnings"], db=str(base.db))
    set_status(base, "build", **{k: v for k, v in res.items() if isinstance(v, int)})
    log_run(base, "build", units_in=len(d["units"]), units_out=len(d["markup"]), duration=time.time() - t0,
            errors=d["warnings"])
    return res


def connect(base: Base, write: bool = False) -> sqlite3.Connection:
    if not base.db.exists():
        raise KbError(f"базы нет: {base.db} — сначала kbtool build")
    uri = f"file:{base.db}?mode={'rw' if write else 'ro'}"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not v or int(v[0]) != SCHEMA_VERSION:
        con.close()
        raise KbError("версия схемы базы не совпадает с плагином — пересоберите: kbtool build")
    return con
