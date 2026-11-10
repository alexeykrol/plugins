"""Разметка: пачки для модели, приём её выхода по контракту, слияние словаря (SPEC §5 шаги 4–8).

Модель (субагент хоста) делает только суждение и возвращает TSV-секции. Код:
режет на пачки, собирает промпт с id и путями, проверяет полноту и закрытые списки,
отбрасывает выход целиком при любой ошибке (S4), ищет кандидатов на слияние понятий.
Разметка — файлы ``markup/*.tsv`` (переживают пересборку базы; K1).
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import guards
from .config import EDGE_CONFIDENCE, EDGE_TYPES, Base, KbError, write_private
from .ingest import load_extract
from .runlog import log_run, set_status

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
UNIT_COLS = ["id", "category", "subtopic", "genre", "practical", "summary"]
CONCEPT_COLS = ["id", "concept", "weight", "definition"]
EDGE_COLS = ["src", "dst", "type", "confidence", "note"]
VMAP_COLS = ["old", "canonical"]
SECTIONS = {"units": UNIT_COLS, "concepts": CONCEPT_COLS, "edges": EDGE_COLS, "vocab-map": VMAP_COLS}


# ---------------------------------------------------------------------------- TSV
def read_tsv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))


def write_tsv(path: Path, cols: list[str], rows: list[dict]) -> None:
    lines = ["\t".join(cols)]
    for r in rows:
        cells = ["" if r.get(c) is None else str(r.get(c)) for c in cols]
        if any(ch in cell for cell in cells for ch in "\t\n\r"):
            raise KbError(f"{path.name}: поле с табуляцией или переводом строки не записывается")
        lines.append("\t".join(cells))
    write_private(path, "\n".join(lines) + "\n")


def append_tsv(path: Path, cols: list[str], row: dict) -> None:
    rows = read_tsv(path)
    rows.append(row)
    write_tsv(path, cols, rows)


def norm_concept(s: str) -> str:
    return " ".join(s.strip().lower().split())


def fold(s: str) -> str:
    return re.sub(r"[\s\-_]+", " ", norm_concept(s).replace("ё", "е")).strip()


def all_markup(base: Base, kind: str) -> list[tuple[str, dict]]:
    """Строки всех файлов ``<kind>-*.tsv`` и ``<kind>.tsv`` с именем файла-источника."""
    out = []
    files = sorted(base.markup.glob(f"{kind}-*.tsv"))
    if (base.markup / f"{kind}.tsv").exists():
        files.append(base.markup / f"{kind}.tsv")
    for p in files:
        out.extend((p.name, r) for r in read_tsv(p))
    return out


def vocab_map(base: Base) -> dict[str, str]:
    m = {norm_concept(r["old"]): norm_concept(r["canonical"]) for r in read_tsv(base.markup / "vocab-map.tsv")}
    for k in list(m):                                    # цепочки a→b→c сворачиваются кодом
        seen = {k}
        while m[k] in m and m[k] not in seen:
            seen.add(m[k])
            m[k] = m[m[k]]
    return m


def vocabulary(base: Base) -> Counter:
    vm = vocab_map(base)
    return Counter(vm.get(norm_concept(r["concept"]), norm_concept(r["concept"]))
                   for _, r in all_markup(base, "concepts"))


def marked_ids(base: Base) -> set[str]:
    return {r["id"] for _, r in all_markup(base, "units")}


# ---------------------------------------------------------------------------- batches
def _next_name(base: Base, prefix: str) -> int:
    nums = [int(m.group(1)) for p in list(base.batches.glob(f"{prefix}*.json")) + list(base.markup.glob(f"*-{prefix}*.tsv"))
            for m in [re.search(rf"{prefix}(\d+)", p.name)] if m]
    return max(nums, default=0) + 1


def batch_plan(base: Base, redo: bool = False) -> list[dict]:
    ex = load_extract(base)
    cfg = base.config()
    cap = cfg["limits"]["batch_chars"]
    done = set() if redo else marked_ids(base)
    units = [u for u in ex["units"] if u["id"] not in done]
    units.sort(key=lambda u: (str(Path(u["path"]).parent), u["date"], u["path"], u["part_no"]))
    batches, cur, size = [], [], 0
    for u in units:
        if cur and size + u["chars"] > cap:
            batches.append(cur)
            cur, size = [], 0
        cur.append(u["id"])
        size += u["chars"]
    if cur:
        batches.append(cur)
    start = _next_name(base, "b")
    out = []
    for i, ids in enumerate(batches):
        name = f"b{start + i:02d}"
        rec = dict(name=name, units=ids, chars=sum(u["chars"] for u in units if u["id"] in set(ids)))
        write_private(base.batches / f"{name}.json", json.dumps(rec, ensure_ascii=False, indent=1))
        out.append(rec)
    set_status(base, "batch-plan", batches=len(out), pending_units=len(units))
    return out


def _load_batch(base: Base, name: str) -> dict:
    p = base.batches / f"{name}.json"
    if not p.exists() or not re.fullmatch(r"[be]\d{2,4}", name):
        raise KbError(f"нет пачки {name}")
    return json.loads(p.read_text(encoding="utf-8"))


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def batch_prompt(base: Base, name: str) -> str:
    """Полный промпт для субагента-читателя: рамка, задача, контекст пачки, контракт выхода."""
    b = _load_batch(base, name)
    cfg = base.config()
    ex = {u["id"]: u for u in load_extract(base)["units"]}
    vocab = vocabulary(base)
    defs = {norm_concept(r["concept"]): r.get("definition", "") for _, r in all_markup(base, "concepts")
            if r.get("definition")}
    lines = [_prompt("01-read.md"), "", _prompt("03-concepts.md"), "", "## Контекст пачки " + name, ""]
    cats = cfg.get("categories") or []
    lines.append("Категории (закрытый список): " + (", ".join(cats) if cats else
                 "не заданы — предложи короткую категорию (1–2 слова), одинаково для похожих документов"))
    lines.append("Жанры (закрытый список): " + ", ".join(cfg["genres"]))
    if vocab:
        lines.append(f"Действующий словарь понятий ({len(vocab)}; используй эти написания, новое — только если нет подходящего):")
        lines.extend(f"- {c}" + (f" — {defs[c]}" if defs.get(c) else "") for c, _ in sorted(vocab.items()))
    else:
        lines.append("Словарь понятий пуст — эта пачка его начинает.")
    lines.append("")
    lines.append("Единицы пачки (прочитай каждый файл инструментом Read; путь — абсолютный):")
    for uid in b["units"]:
        u = ex[uid]
        lines.append(f"- id={uid} · дата {u['date']} · {u['chars']} зн. · файл {base.units_dir / (uid + '.txt')}"
                     f" · название: {u['title']}")
    lines.append("")
    lines.append(f"Ответ — только две TSV-секции (#units, #concepts) ровно по контракту; {len(b['units'])} строк в #units.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- import
def parse_sections(text: str) -> dict[str, list[list[str]]]:
    """Выход модели → секции. Код-блоки ``` срезаются; всё вне секций — ошибка."""
    secs: dict[str, list[list[str]]] = {}
    cur = None
    for n, line in enumerate(text.splitlines(), 1):
        s = line.rstrip("\r")
        if not s.strip() or s.strip().startswith("```"):
            continue
        if s.startswith("#"):
            key = s[1:].strip().lower()
            if key not in SECTIONS:
                raise KbError(f"строка {n}: неизвестная секция {s[:40]!r}")
            cur = key
            secs.setdefault(cur, [])
            continue
        if cur is None:
            raise KbError(f"строка {n}: текст вне секции — выход не по контракту")
        secs[cur].append(s.split("\t"))
    return secs


def _rows(secs, key, errors) -> list[dict]:
    cols = SECTIONS[key]
    raw = secs.get(key, [])
    if raw and [c.strip().lower() for c in raw[0]] == cols:
        raw = raw[1:]
    out = []
    for i, cells in enumerate(raw, 1):
        if key == "concepts" and len(cells) == len(cols) - 1:
            cells = cells + [""]                 # пустое необязательное definition в конце строки модели часто срезают
        if len(cells) != len(cols):
            errors.append(f"#{key} строка {i}: {len(cells)} полей вместо {len(cols)}")
            continue
        row = dict(zip(cols, (c.strip() for c in cells)))
        for c, v in row.items():
            bad = guards.unsafe_markup_field(v)
            if bad:
                errors.append(f"#{key} строка {i}: поле {c} отклонено ({bad})")
        out.append(row)
    return out


def validate_reader(base: Base, batch: dict, text: str) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    cfg = base.config()
    errors: list[str] = []
    try:
        secs = parse_sections(text)
    except KbError as exc:
        return [], [], [], [str(exc)]
    ids = set(batch["units"])
    units = _rows(secs, "units", errors)
    concepts = _rows(secs, "concepts", errors)
    vmap = _rows(secs, "vocab-map", errors)
    if "edges" in secs:
        errors.append("#edges в выходе читателя не ожидается")
    seen = Counter(u["id"] for u in units)
    for uid in ids - set(seen):
        errors.append(f"#units: нет строки для {uid}")
    for uid, n in seen.items():
        if uid not in ids:
            errors.append(f"#units: чужой id {uid}")
        elif n > 1:
            errors.append(f"#units: id {uid} повторён {n} раз")
    cats = set(cfg.get("categories") or [])
    genres = set(cfg["genres"])
    for u in units:
        if cats and u["category"] not in cats:
            errors.append(f"#units {u['id']}: категория {u['category']!r} не из списка")
        if not cats and not (1 <= len(u["category"]) <= 40):
            errors.append(f"#units {u['id']}: категория пустая или длиннее 40")
        if len(u["subtopic"]) > 40:
            errors.append(f"#units {u['id']}: подтема длиннее 40")
        if u["genre"] not in genres:
            errors.append(f"#units {u['id']}: жанр {u['genre']!r} не из списка")
        if u["practical"] not in ("0", "1"):
            errors.append(f"#units {u['id']}: practical должен быть 0 или 1")
        if not (20 <= len(u["summary"]) <= 500):
            errors.append(f"#units {u['id']}: резюме {len(u['summary'])} зн. (нужно 20–500)")
    per_unit = Counter()
    pairs = set()
    genre_of = {u["id"]: u["genre"] for u in units}
    for c in concepts:
        c["concept"] = norm_concept(c["concept"])
        if c["id"] not in ids:
            errors.append(f"#concepts: чужой id {c['id']}")
        if not (2 <= len(c["concept"]) <= 60):
            errors.append(f"#concepts {c['id']}: понятие {c['concept']!r} длиной не 2–60")
        if c["weight"] not in ("1", "2"):
            errors.append(f"#concepts {c['id']}: вес {c['weight']!r} (нужно 1 или 2)")
        if len(c["definition"]) > 200:
            errors.append(f"#concepts {c['id']}: определение длиннее 200")
        if (c["id"], c["concept"]) in pairs:
            errors.append(f"#concepts {c['id']}: понятие {c['concept']!r} повторено")
        pairs.add((c["id"], c["concept"]))
        per_unit[c["id"]] += 1
    for uid in ids:
        n = per_unit[uid]
        if n > 6 or (n == 0 and genre_of.get(uid) != "noise"):
            errors.append(f"#concepts {uid}: понятий {n} (нужно 1–6; 0 только для noise)")
    return units, concepts, vmap, errors


def validate_edges(base: Base, group: dict, text: str) -> tuple[list[dict], list[dict], list[str]]:
    errors: list[str] = []
    try:
        secs = parse_sections(text)
    except KbError as exc:
        return [], [], [str(exc)]
    allowed = set(group["units"]) | set(group.get("neighbors", []))
    dates = {u["id"]: u["date"] for u in load_extract(base)["units"]}
    edges = _rows(secs, "edges", errors)
    vmap = _rows(secs, "vocab-map", errors)
    if set(secs) - {"edges", "vocab-map"}:
        errors.append("в выходе разметчика связей допустимы только #edges (и #vocab-map)")
    seen = set()
    for e in edges:
        key = (e["src"], e["dst"], e["type"])
        for k in ("src", "dst"):
            if e[k] not in allowed:
                errors.append(f"#edges: {k} {e[k]} не из группы")
        if e["src"] == e["dst"]:
            errors.append(f"#edges: петля {e['src']}")
        if e["type"] not in EDGE_TYPES:
            errors.append(f"#edges: тип {e['type']!r} не из списка")
        if e["confidence"] not in EDGE_CONFIDENCE:
            errors.append(f"#edges: уверенность {e['confidence']!r} не из списка")
        if not (10 <= len(e["note"]) <= 300):
            errors.append(f"#edges {e['src']}→{e['dst']}: обоснование {len(e['note'])} зн. (нужно 10–300)")
        if e["type"] in ("develops", "revisits") and dates.get(e["src"]) and dates.get(e["dst"]) \
                and dates[e["src"]] < dates[e["dst"]]:
            errors.append(f"#edges {e['src']}→{e['dst']}: {e['type']} требует, чтобы src был не раньше dst")
        if key in seen:
            errors.append(f"#edges: повтор {key}")
        seen.add(key)
    return edges, vmap, errors


def _validate_vmap(base: Base, vmap: list[dict], extra_concepts: set[str], errors: list[str]) -> None:
    known = set(vocabulary(base)) | extra_concepts | {norm_concept(r["concept"]) for _, r in all_markup(base, "concepts")}
    for r in vmap:
        r["old"], r["canonical"] = norm_concept(r["old"]), norm_concept(r["canonical"])
        if r["old"] == r["canonical"]:
            errors.append(f"#vocab-map: {r['old']!r} → само в себя")
        if r["old"] not in known:
            errors.append(f"#vocab-map: понятия {r['old']!r} нет в разметке")
        if not (2 <= len(r["canonical"]) <= 60):
            errors.append(f"#vocab-map: каноническое {r['canonical']!r} длиной не 2–60")


def markup_import(base: Base, name: str, text: str) -> dict:
    """Принять выход модели для пачки (bNN) или группы связей (eNN). Всё или ничего (S4)."""
    batch = _load_batch(base, name)
    if name.startswith("b"):
        units, concepts, vmap, errors = validate_reader(base, batch, text)
        _validate_vmap(base, vmap, {c["concept"] for c in concepts}, errors)
        if errors:
            log_run(base, f"import-{name}", units_in=len(batch["units"]), errors=errors)
            raise KbError(f"выход для {name} отклонён целиком ({len(errors)} ошибок):\n  " + "\n  ".join(errors[:40]))
        write_tsv(base.markup / f"units-{name}.tsv", UNIT_COLS, units)
        write_tsv(base.markup / f"concepts-{name}.tsv", CONCEPT_COLS, concepts)
        res = dict(batch=name, units=len(units), concepts=len(concepts), vocab_map=len(vmap))
    else:
        edges, vmap, errors = validate_edges(base, batch, text)
        _validate_vmap(base, vmap, set(), errors)
        if errors:
            log_run(base, f"import-{name}", units_in=len(batch["units"]), errors=errors)
            raise KbError(f"выход для {name} отклонён целиком ({len(errors)} ошибок):\n  " + "\n  ".join(errors[:40]))
        write_tsv(base.markup / f"edges-{name}.tsv", EDGE_COLS, edges)
        res = dict(group=name, edges=len(edges), vocab_map=len(vmap))
    if vmap:
        rows = {r["old"]: r for r in read_tsv(base.markup / "vocab-map.tsv")}
        rows.update({r["old"]: r for r in vmap})
        write_tsv(base.markup / "vocab-map.tsv", VMAP_COLS, list(rows.values()))
    log_run(base, f"import-{name}", units_in=len(batch["units"]), units_out=res.get("units", res.get("edges", 0)))
    set_status(base, f"import-{name}", **{k: v for k, v in res.items() if isinstance(v, int)})
    return res


def vocab_import(base: Base, text: str) -> dict:
    """Таблица слияний понятий (#vocab-map old→canonical) — решение модели/пользователя по кандидатам."""
    errors: list[str] = []
    try:
        secs = parse_sections(text)
    except KbError as exc:
        raise KbError(f"слияния отклонены: {exc}") from None
    if set(secs) - {"vocab-map"}:
        errors.append("ожидается только секция #vocab-map")
    vmap = _rows(secs, "vocab-map", errors)
    _validate_vmap(base, vmap, set(), errors)
    if errors:
        raise KbError("слияния отклонены целиком:\n  " + "\n  ".join(errors[:40]))
    rows = {r["old"]: r for r in read_tsv(base.markup / "vocab-map.tsv")}
    rows.update({r["old"]: r for r in vmap})
    write_tsv(base.markup / "vocab-map.tsv", VMAP_COLS, list(rows.values()))
    log_run(base, "vocab-import", units_in=len(vmap), units_out=len(rows))
    return dict(merged=len(vmap), total_map=len(rows), vocabulary=len(vocabulary(base)))


# ---------------------------------------------------------------------------- vocabulary
def _lev(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
        if min(prev) > cap:
            return cap + 1
    return prev[-1]


def _stem(w: str) -> str:
    return w[:max(4, len(w) - 2)]


def vocab_candidates(base: Base) -> dict:
    """Кандидаты на слияние понятий (код); решает модель/пользователь (05-merge-vocab.md)."""
    voc = vocabulary(base)
    names = sorted(voc)
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            fa, fb = fold(a), fold(b)
            why = None
            if fa == fb:
                why = "написание"
            elif min(len(fa), len(fb)) >= 6 and _lev(fa, fb) <= 2:
                why = "опечатка/форма"
            elif [_stem(w) for w in fa.split()] == [_stem(w) for w in fb.split()]:
                why = "общие основы"
            elif set(fa.split()) < set(fb.split()) or set(fb.split()) < set(fa.split()):
                why = "одно входит в другое"
            elif {_stem(w) for w in fa.split() if len(w) >= 6} & {_stem(w) for w in fb.split() if len(w) >= 6}:
                why = "общее слово"
            if why:
                pairs.append(dict(a=a, a_units=voc[a], b=b, b_units=voc[b], why=why))
    lo, hi = base.config().get("vocab_corridor", [30, 70])
    return dict(size=len(voc), corridor=[lo, hi], in_corridor=lo <= len(voc) <= hi, pairs=pairs,
                singletons=sorted(c for c, n in voc.items() if n == 1))


# ---------------------------------------------------------------------------- edges plan
def cards(base: Base) -> dict[str, dict]:
    ex = {u["id"]: u for u in load_extract(base)["units"]}
    vm = vocab_map(base)
    out = {}
    for _, r in all_markup(base, "units"):
        if r["id"] in ex:
            u = ex[r["id"]]
            out[r["id"]] = dict(id=r["id"], date=u["date"], title=u["title"], category=r["category"],
                                genre=r["genre"], summary=r["summary"], concepts=[])
    for _, r in all_markup(base, "concepts"):
        if r["id"] in out:
            c = norm_concept(r["concept"])
            out[r["id"]]["concepts"].append((vm.get(c, c), int(r["weight"] or 1)))
    return out


def _card_text(c: dict) -> str:
    cs = ", ".join(f"{n}({w})" for n, w in sorted(c["concepts"], key=lambda x: -x[1]))
    return f"- id={c['id']} · {c['date']} · {c['category']}/{c['genre']} · {c['title']}\n  резюме: {c['summary']}\n  понятия: {cs}"


def edge_plan(base: Base, neighbors_k: int = 5) -> list[dict]:
    cs = cards(base)
    cap = base.config()["limits"]["batch_chars"]
    order = sorted(cs.values(), key=lambda c: (max(c["concepts"], key=lambda x: x[1])[0] if c["concepts"] else "",
                                                c["date"]))
    groups, cur, size = [], [], 0
    for c in order:
        n = len(_card_text(c))
        if cur and size + n > cap:
            groups.append(cur)
            cur, size = [], 0
        cur.append(c["id"])
        size += n
    if cur:
        groups.append(cur)
    by_concept = defaultdict(set)
    for c in cs.values():
        for name, _ in c["concepts"]:
            by_concept[name].add(c["id"])
    start = _next_name(base, "e")
    out = []
    for i, ids in enumerate(groups):
        inside = set(ids)
        score = Counter()
        for uid in ids:
            for name, w in cs[uid]["concepts"]:
                for other in by_concept[name] - inside:
                    score[other] += w
        nb = [o for o, _ in score.most_common(neighbors_k)] if len(groups) > 1 else []
        rec = dict(name=f"e{start + i:02d}", units=ids, neighbors=nb)
        write_private(base.batches / f"{rec['name']}.json", json.dumps(rec, ensure_ascii=False, indent=1))
        out.append(rec)
    set_status(base, "edge-plan", groups=len(out), cards=len(cs))
    return out


def edge_prompt(base: Base, name: str) -> str:
    g = _load_batch(base, name)
    cs = cards(base)
    lines = [_prompt("04-edges.md"), "", f"## Карточки группы {name} ({len(g['units'])})", ""]
    lines += [_card_text(cs[u]) for u in g["units"] if u in cs]
    if g.get("neighbors"):
        lines += ["", "## Соседи из других групп (связи с ними допустимы)", ""]
        lines += [_card_text(cs[u]) for u in g["neighbors"] if u in cs]
    lines += ["", "Ответ — только секция #edges ровно по контракту."]
    return "\n".join(lines)


def pending(base: Base) -> list[dict]:
    done = marked_ids(base)
    return [u for u in load_extract(base)["units"] if u["id"] not in done]
