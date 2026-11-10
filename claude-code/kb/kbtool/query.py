"""Запрос к базе и записи пользователя (SPEC §7; логика поиска — перенос processed-db/ask.py).

Функции возвращают dict — их печатает CLI и отдаёт MCP. Пишут только в verdicts / misses /
conflicts: строка в ``markup/*.tsv`` (переживает пересборку) и сразу в ``kb.sqlite``.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date

from .config import RESOLUTIONS, VERDICTS, Base, KbError
from .markup import append_tsv, fold, read_tsv, write_tsv
from .store import connect

# стоп-слова: ru — из processed-db/ask.py (06.11.2026) + общие; en — общие служебные
STOP = set("""что как это для про или мне нас есть было был если чем где когда почему какие какой какая
можно надо нужно думал думаю говорил идея идеи заметки заметках заметка стоит ещё еще тоже только
очень всех всем себя свои своей свой наш наша наше них ними того этого этой этом там тут вот как-то
что-то был была были будет будут может могу хочу хотел чтобы потому именно просто уже даже который
которая которые такое такой также между после перед через более менее самый сказано написано
about after again also among another because been before being between both could does doing down
during each from further have having here into itself just more most other over same should some
such than that their them then there these they this those through under until very what when where
which while whom with would your yours what's""".split())
HIDDEN = ("отвергнуто", "устарело")
_FACT = re.compile(r"\b(?:1[5-9]|20)\d{2}\b|\d+(?:[.,]\d+)?\s*(?:%|процент|млн|млрд|тыс|км|кг|долл|\$|€|руб)"
                   r"|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|\s—\s*это\b|\bявляется\b|\bназывается\b|\bозначает\b"
                   r"|\bis (?:a|an|the) \w+|\bdefined as\b|\bmeans\b|\bbecause\b|\bпотому что\b|\bиз-за\b", re.I)


def terms_of(text: str) -> list[str]:
    words = [w.lower() for w in re.findall(r"[\w-]+", text)]
    return [w for w in dict.fromkeys(words) if len(w) >= 4 and w not in STOP]


def _stem(w: str) -> str:
    return w[:max(4, len(w) - 2)]


def checkable(text: str) -> bool:
    """Ступень 1 детектора конфликта (§7): в единице есть проверяемые факты — даты, числа, определения."""
    return bool(_FACT.search(text))


def _verdict(con, uid):
    r = con.execute("SELECT verdict, date, note FROM verdicts WHERE unit_id=?", (uid,)).fetchone()
    return dict(r) if r else None


def _edges_among(con, ids):
    if len(ids) < 2:
        return []
    q = ",".join("?" * len(ids))
    return [dict(r) for r in con.execute(
        f"SELECT * FROM edges WHERE src IN ({q}) AND dst IN ({q}) ORDER BY type, src", list(ids) * 2)]


def search(base: Base, question: str, limit: int = 8) -> dict:
    terms = terms_of(question)
    if not terms:
        return dict(question=question, terms=[], results=[], suppressed=[], concepts=[], edges=[],
                    contradictions=[], conflict_decisions=[], note="в вопросе нет значимых слов (≥4 букв)")
    con = connect(base)
    try:
        fts = " OR ".join(f'"{_stem(w)}"*' for w in terms)
        rows = con.execute(
            "SELECT u.*, m.category, m.subtopic, m.genre, m.summary, bm25(units_fts, 0, 5.0, 3.0, 1.0) AS rank "
            "FROM units_fts JOIN units u ON u.id = units_fts.id LEFT JOIN markup m ON m.unit_id = u.id "
            "WHERE units_fts MATCH ? ORDER BY rank LIMIT ?", (fts, limit * 3)).fetchall()
        results, suppressed = [], []
        for r in rows:
            v = _verdict(con, r["id"])
            if v and v["verdict"] in HIDDEN:
                suppressed.append(dict(id=r["id"], title=r["title"], date=r["date"], verdict=v["verdict"],
                                       verdict_date=v["date"]))
                continue
            if len(results) >= limit:
                continue
            snip = con.execute("SELECT snippet(units_fts, 3, '«', '»', ' … ', 18) FROM units_fts "
                               "WHERE units_fts MATCH ? AND id = ?", (fts, r["id"])).fetchone()[0]
            results.append(dict(id=r["id"], title=r["title"], path=r["path"],
                                part=f"{r['part_no']}/{r['parts']}", date=r["date"], category=r["category"],
                                subtopic=r["subtopic"], genre=r["genre"], summary=r["summary"],
                                verdict=v, quote=" ".join(snip.split()), checkable=checkable(r["text"])))
        ids = [r["id"] for r in results]
        like = " OR ".join("concept LIKE ? OR concept LIKE ?" for _ in terms)
        cps = con.execute(f"SELECT DISTINCT concept FROM unit_concepts WHERE {like}",
                          [x for w in terms for x in (f"{_stem(w)}%", f"% {_stem(w)}%")]).fetchall()
        concepts = [dict(con.execute("SELECT * FROM concept_timeline WHERE concept=?", (c[0],)).fetchone())
                    for c in cps]
        contra = []
        if ids:
            q = ",".join("?" * len(ids))
            for e in con.execute(f"SELECT e.*, a.title st, a.date sd, b.title dt, b.date dd FROM edges e "
                                 f"JOIN units a ON a.id=e.src JOIN units b ON b.id=e.dst "
                                 f"WHERE e.type='contradicts' AND (e.src IN ({q}) OR e.dst IN ({q}))", ids * 2):
                contra.append(dict(e))
        decisions = []
        if ids:
            q = ",".join("?" * len(ids))
            decisions = [dict(r) for r in con.execute(f"SELECT * FROM conflicts WHERE unit_id IN ({q})", ids)]
        return dict(question=question, terms=terms, results=results, suppressed=suppressed, concepts=concepts,
                    edges=_edges_among(con, ids), contradictions=contra, conflict_decisions=decisions)
    finally:
        con.close()


def unit(base: Base, uid: str, full: bool = False, max_chars: int = 1500) -> dict:
    con = connect(base)
    try:
        u = con.execute("SELECT u.*, m.category, m.subtopic, m.genre, m.practical, m.summary FROM units u "
                        "LEFT JOIN markup m ON m.unit_id=u.id WHERE u.id=?", (uid,)).fetchone()
        if not u:
            raise KbError(f"нет единицы {uid}")
        d = dict(u)
        text = d.pop("text")
        d["text"] = text if full else text[:max_chars]
        d["truncated"] = not full and len(text) > max_chars
        d["checkable"] = checkable(text)
        d["verdict"] = _verdict(con, uid)
        d["concepts"] = [dict(r) for r in con.execute(
            "SELECT concept, weight FROM unit_concepts WHERE unit_id=? ORDER BY weight DESC, concept", (uid,))]
        d["edges_out"] = [dict(r) for r in con.execute(
            "SELECT e.dst AS id, e.type, e.confidence, e.note, u.title, u.date FROM edges e JOIN units u ON u.id=e.dst "
            "WHERE e.src=?", (uid,))]
        d["edges_in"] = [dict(r) for r in con.execute(
            "SELECT e.src AS id, e.type, e.confidence, e.note, u.title, u.date FROM edges e JOIN units u ON u.id=e.src "
            "WHERE e.dst=?", (uid,))]
        d["neighbors"] = [dict(r) for r in con.execute(
            "SELECT CASE WHEN src=? THEN dst ELSE src END AS id, GROUP_CONCAT(concept, ', ') AS via, "
            "SUM(strength) AS strength FROM concept_links WHERE src=? OR dst=? GROUP BY 1 "
            "ORDER BY strength DESC LIMIT 8", (uid, uid, uid))]
        d["conflict_decisions"] = [dict(r) for r in con.execute("SELECT * FROM conflicts WHERE unit_id=?", (uid,))]
        return d
    finally:
        con.close()


def concept(base: Base, name: str) -> dict:
    con = connect(base)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT u.id, u.title, u.date, m.category, m.summary, uc.weight FROM unit_concepts uc "
            "JOIN units u ON u.id=uc.unit_id LEFT JOIN markup m ON m.unit_id=u.id WHERE uc.concept=? ORDER BY u.date",
            (name,))]
        if not rows:
            raise KbError(f"нет понятия «{name}»")
        return dict(concept=name, units=rows, edges=_edges_among(con, [r["id"] for r in rows]))
    finally:
        con.close()


def concepts_all(base: Base) -> list[dict]:
    con = connect(base)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM concept_timeline ORDER BY units DESC, first")]
    finally:
        con.close()


def contradictions(base: Base) -> list[dict]:
    con = connect(base)
    try:
        return [dict(r) for r in con.execute(
            "SELECT e.*, a.title st, a.date sd, b.title dt, b.date dd FROM edges e JOIN units a ON a.id=e.src "
            "JOIN units b ON b.id=e.dst WHERE e.type='contradicts' ORDER BY a.date")]
    finally:
        con.close()


# ---------------------------------------------------------------------------- writes
def _clean(s: str | None, cap: int) -> str:
    s = " ".join((s or "").split())
    return s[:cap]


def _require_unit(base: Base, uid: str) -> None:
    con = connect(base)
    try:
        if not con.execute("SELECT 1 FROM units WHERE id=?", (uid,)).fetchone():
            raise KbError(f"нет единицы {uid}")
    finally:
        con.close()


def set_verdict(base: Base, uid: str, verdict: str, note: str = "") -> dict:
    if verdict not in VERDICTS:
        raise KbError(f"вердикт должен быть одним из: {', '.join(VERDICTS)}")
    _require_unit(base, uid)
    row = dict(id=uid, verdict=verdict, date=date.today().isoformat(), note=_clean(note, 300))
    append_tsv(base.markup / "verdicts.tsv", ["id", "verdict", "date", "note"], row)
    con = connect(base, write=True)
    try:
        con.execute("INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?)", (uid, verdict, row["date"], row["note"]))
        con.commit()
    finally:
        con.close()
    return row


def add_miss(base: Base, question: str, found_id: str = "") -> dict:
    row = dict(date=date.today().isoformat(), question=_clean(question, 500), found_id=_clean(found_id, 40))
    if not row["question"]:
        raise KbError("пустой вопрос")
    append_tsv(base.markup / "misses.tsv", ["date", "question", "found_id"], row)
    con = connect(base, write=True)
    try:
        con.execute("INSERT INTO misses VALUES (?,?,?)", (row["date"], row["question"], row["found_id"]))
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM misses").fetchone()[0]
    finally:
        con.close()
    return dict(row, total=total)


def conflict_id(uid: str, claim_kb: str) -> str:
    return hashlib.sha256(f"{uid}\n{fold(claim_kb)}".encode()).hexdigest()[:12]


def conflict(base: Base, unit_id: str, claim_kb: str = "", claim_model: str = "", resolution: str = "") -> dict:
    """Без resolution — прочитать прежние решения (по единице или по утверждению).
    С resolution — записать/обновить решение (kb | model | both | pending)."""
    _require_unit(base, unit_id)
    con = connect(base)
    try:
        prior = [dict(r) for r in con.execute("SELECT * FROM conflicts WHERE unit_id=?", (unit_id,))]
    finally:
        con.close()
    if not resolution:
        if claim_kb:
            cid = conflict_id(unit_id, claim_kb)
            prior = [p for p in prior if p["id"] == cid]
        # pending — «показали оба, пользователя не спросили»: в интерактивном чате спросить снова
        return dict(unit_id=unit_id, decisions=prior, ask=not any(p["resolution"] != "pending" for p in prior))
    if resolution not in RESOLUTIONS:
        raise KbError(f"решение должно быть одним из: {', '.join(RESOLUTIONS)}")
    if not claim_kb.strip():
        raise KbError("нужно утверждение базы (claim_kb)")
    row = dict(id=conflict_id(unit_id, claim_kb), unit_id=unit_id, claim_kb=_clean(claim_kb, 400),
               claim_model=_clean(claim_model, 400), resolution=resolution, date=date.today().isoformat())
    cols = ["id", "unit_id", "claim_kb", "claim_model", "resolution", "date"]
    rows = [r for r in read_tsv(base.markup / "conflicts.tsv") if r.get("id") != row["id"]] + [row]
    write_tsv(base.markup / "conflicts.tsv", cols, rows)
    con = connect(base, write=True)
    try:
        con.execute("INSERT OR REPLACE INTO conflicts VALUES (?,?,?,?,?,?)", tuple(row[c] for c in cols))
        con.commit()
    finally:
        con.close()
    return dict(recorded=row)
