"""kbtool — CLI бухгалтерии плагина kb. Модель не вызывает, в сеть не ходит.

Поток сборки (скилл /kb-build):
  init ПАПКА → scan → extract → [titles → config-set categories] → batch-plan →
  batch-prompt bNN → (субагент) → markup-import bNN → vocab-candidates → markup-import (vocab-map) →
  edge-plan → edge-prompt eNN → (субагент) → markup-import eNN → build
Запрос (скилл /kb-ask, MCP): ask · unit · concept · concepts · contradictions · verdict · miss · conflict
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, ingest, markup, query, store
from .config import KbError, init_base, list_bases, load_yaml, resolve_base, write_private

SETTABLE = {"categories", "exclude", "include_forced", "notice_shown", "genres", "vocab_corridor"}


def _out(obj, as_json: bool, human=None) -> None:
    if as_json or human is None:
        print(json.dumps(obj, ensure_ascii=False, indent=1))
    else:
        human(obj)


def _print_scan(r):
    print(f"Корпус: {r['corpus']}")
    print(f"{'формат':10} {'файлов':>7} {'принято':>8} {'байт':>12}")
    for fmt, f in sorted(r["by_format"].items(), key=lambda x: -x[1]["files"]):
        print(f"{fmt:10} {f['files']:>7} {f['accepted']:>8} {f['bytes']:>12}")
    skipped = [x for x in r["files"] if x["status"] == "skipped"]
    if skipped:
        print("\nИсключено:")
        for x in skipped:
            print(f"  {x['path']}: {x['reason']}")
    for b in r["blocked"]:
        print(f"СТОП: {b['path']}: {b['reason']}")
    if r.get("excluded"):
        print("\nИсключено по причинам: " + "; ".join(f"{x['label']} — {x['count']}" for x in r["excluded"]))
    for n in r.get("notes") or []:
        print(n)
    e = r["estimate"]
    print(f"\nК разбору: {r['candidates']} {ingest.files_word(r['candidates'])}, {r.get('bytes_human') or r['bytes']} ({r['bytes']} байт); "
          f"оценка сверху ≈ {e['tokens']} токенов, пачек ≈ {e['batches']}")


def _print_extract(r):
    counts = {}
    for s in r["sources"]:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    print(f"Единиц: {len(r['units'])}, знаков: {r['chars']}, оценка ≈ {r['tokens_est']} токенов; источники: {counts}")
    for s in r["sources"]:
        if s["status"] in ("deficit",) or (s["status"] == "skipped" and s["reason"].startswith(("secret", "inj", "dup"))):
            print(f"  {s['status']}: {s['path']}: {s['reason']}")
    for n in r.get("notes") or []:
        print(n)


def _print_summary(r):
    print(f"Войдут в базу: {r['files']} {r['files_word']}, {r['bytes_human']} "
          f"(≈ {r['tokens_est']} токенов на чтение моделью).")
    if r["excluded_total"]:
        print(f"Не войдут: {r['excluded_total']} — " + "; ".join(f"{x['label']}: {x['count']}" for x in r["excluded"]) + ".")
    else:
        print("Не войдут: ничего.")
    for n in r["notes"]:
        print(n)
    print(r["question"])


def _print_ask(r):
    print(f"== Найдено по запросу: {r['question']}  (термины: {', '.join(r['terms'])})")
    if r.get("note"):
        print("  " + r["note"])
    if not r["results"]:
        print("  ничего не найдено — переформулируйте; если ответ в базе есть — kbtool miss")
    for x in r["results"]:
        v = f"  [{x['verdict']['verdict']} {x['verdict']['date']}]" if x["verdict"] else ""
        print(f"\n{x['id']} · {x['date']} · {x['category'] or '—'}/{x['subtopic'] or '—'} · {x['genre'] or '?'}{v}")
        print(f"   {x['title']}  ({x['path']}, часть {x['part']})")
        if x["summary"]:
            print(f"   {x['summary']}")
        print(f"   цитата: {x['quote']}" + ("   [есть проверяемые факты]" if x["checkable"] else ""))
    for s in r["suppressed"]:
        print(f"\n(скрыто вердиктом: {s['id']} {s['title']} — {s['verdict']} {s['verdict_date']})")
    if r["concepts"]:
        print("\n== Понятия, задетые вопросом")
        for c in r["concepts"]:
            print(f"  {c['concept']}: {c['units']} ед., {c['first']} → {c['last']} ({c['categories']})")
    if r["edges"]:
        print("\n== Связи между найденным")
        for e in r["edges"]:
            print(f"  {e['src']} → {e['dst']}  {e['type']} [{e['confidence']}]: {e['note']}")
    if r["contradictions"]:
        print("\n== Противоречия")
        for e in r["contradictions"]:
            print(f"  {e['src']} ({e['sd']}) ⟂ {e['dst']} ({e['dd']}): {e['note']}")
    if r["conflict_decisions"]:
        print("\n== Решения по конфликтам с моделью (не спрашивать повторно)")
        for c in r["conflict_decisions"]:
            print(f"  {c['unit_id']}: «{c['claim_kb']}» → {c['resolution']} ({c['date']})")


def _print_unit(d):
    v = f"  [{d['verdict']['verdict']} {d['verdict']['date']}]" if d["verdict"] else ""
    print(f"{d['id']} · {d['date']} ({d['date_basis']}) · {d['category'] or '—'}/{d['subtopic'] or '—'} · "
          f"{d['genre'] or '?'}{v}\n   {d['title']}\n   файл: {d['path']}, часть {d['part_no']}/{d['parts']}, {d['chars']} зн.")
    if d["summary"]:
        print(f"   {d['summary']}")
    print("   понятия: " + ", ".join(f"{c['concept']}({c['weight']})" for c in d["concepts"]))
    for e in d["edges_out"]:
        print(f"  → {e['id']} {e['type']} [{e['confidence']}]: {e['note']}\n      {e['title']}")
    for e in d["edges_in"]:
        print(f"  ← {e['id']} {e['type']} [{e['confidence']}]: {e['note']}\n      {e['title']}")
    if d["neighbors"]:
        print("== Соседи по понятиям: " + "; ".join(f"{n['id']} ({n['via']})" for n in d["neighbors"]))
    print("\n== Текст" + (" (начало)" if d["truncated"] else "") + "\n" + d["text"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kbtool", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"kbtool {__version__}")
    p.add_argument("--kb", help="slug базы (если баз несколько)")
    p.add_argument("--corpus", help="путь корпуса вместо slug")
    p.add_argument("--json", action="store_true", help="вывод JSON")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="базы в $KB_HOME")
    s = sub.add_parser("init", help="создать базу для папки документов")
    s.add_argument("path")
    s.add_argument("--slug", help="явное имя базы (демо: kb-demo); по умолчанию — из имени папки")
    sub.add_parser("scan", help="опись папки (U3)")
    sub.add_parser("extract", help="извлечь текст и разбить на единицы (U5)")
    sub.add_parser("titles", help="названия и даты единиц — для предложения категорий")
    sub.add_parser("summary", help="сводка перед разметкой: что войдёт, объём, что исключено и почему")
    s = sub.add_parser("config-set", help=f"поле kb.yaml: {', '.join(sorted(SETTABLE))}; значение — JSON")
    s.add_argument("key")
    s.add_argument("value")
    s = sub.add_parser("batch-plan", help="разбить неразмеченные единицы на пачки")
    s.add_argument("--redo", action="store_true", help="включить уже размеченные")
    s = sub.add_parser("batch-prompt", help="промпт для субагента-читателя пачки")
    s.add_argument("name")
    s.add_argument("--save", action="store_true", help="записать в batches/<name>.prompt.md (0600) и напечатать путь")
    s = sub.add_parser("markup-import", help="принять выход модели для bNN/eNN (файл или - для stdin)")
    s.add_argument("name")
    s.add_argument("file", nargs="?", default="-")
    sub.add_parser("vocab-candidates", help="кандидаты на слияние понятий")
    s = sub.add_parser("vocab-import", help="принять таблицу слияний (#vocab-map), файл или -")
    s.add_argument("file", nargs="?", default="-")
    sub.add_parser("edge-plan", help="группы карточек для разметки связей")
    s = sub.add_parser("edge-prompt", help="промпт для субагента связей")
    s.add_argument("name")
    s.add_argument("--save", action="store_true", help="записать в batches/<name>.prompt.md (0600) и напечатать путь")
    sub.add_parser("pending", help="единицы без разметки")
    sub.add_parser("build", help="собрать kb.sqlite (атомарно)")
    sub.add_parser("status", help="статус прогонов (без текста документов)")
    s = sub.add_parser("ask", help="поиск по базе")
    s.add_argument("question")
    s.add_argument("-n", type=int, default=8)
    s = sub.add_parser("unit", help="карточка единицы")
    s.add_argument("id")
    s.add_argument("--full", action="store_true")
    s = sub.add_parser("concept", help="линия понятия")
    s.add_argument("name")
    sub.add_parser("concepts", help="словарь понятий с хронологией")
    sub.add_parser("contradictions", help="все противоречия")
    s = sub.add_parser("verdict", help="вердикт пользователя: ценно|отвергнуто|устарело|спорно")
    s.add_argument("id")
    s.add_argument("verdict")
    s.add_argument("note", nargs="?", default="")
    s = sub.add_parser("miss", help="записать промах поиска")
    s.add_argument("question")
    s.add_argument("found_id", nargs="?", default="")
    s = sub.add_parser("conflict", help="конфликт база ↔ модель: прочитать или записать решение")
    s.add_argument("unit_id")
    s.add_argument("--claim-kb", default="")
    s.add_argument("--claim-model", default="")
    s.add_argument("--resolution", default="", help="kb | model | both | pending")
    return p


def main(argv=None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 0
    try:
        return _run(a)
    except KbError as exc:
        print(f"kbtool: {exc}", file=sys.stderr)
        return 2


def _read_input(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _run(a) -> int:
    if a.cmd == "list":
        _out([dict(slug=b.slug, corpus=b.config().get("corpus_path"), built=b.db.exists()) for b in list_bases()], True)
        return 0
    if a.cmd == "init":
        b = init_base(a.path, a.slug)
        _out(dict(slug=b.slug, root=str(b.root), corpus=b.config()["corpus_path"],
                  notice_shown=b.config().get("notice_shown")), True)
        return 0
    base = resolve_base(a.kb, a.corpus)
    j = a.json
    if a.cmd == "scan":
        r = ingest.scan(base)
        _out(r, j, _print_scan)
        return 3 if r["blocked"] else 0
    if a.cmd == "extract":
        _out(ingest.extract(base), j, _print_extract)
    elif a.cmd == "summary":
        _out(ingest.summary(base), j, _print_summary)
    elif a.cmd == "titles":
        for u in ingest.load_extract(base)["units"]:
            print(f"{u['id']}\t{u['date']}\t{u['path']}")
    elif a.cmd == "config-set":
        if a.key not in SETTABLE:
            raise KbError(f"поле {a.key!r} не меняется командой (лимиты — только правкой kb.yaml осознанно)")
        try:
            value = json.loads(a.value)
        except json.JSONDecodeError as exc:
            raise KbError(f"значение не JSON: {exc.msg}") from None
        stored = load_yaml(base.yaml.read_text(encoding="utf-8"))
        stored[a.key] = value
        base.save_config(stored)
        print(f"{a.key} = {json.dumps(value, ensure_ascii=False)}")
    elif a.cmd == "batch-plan":
        _out(markup.batch_plan(base, a.redo), True)
    elif a.cmd in ("batch-prompt", "edge-prompt"):
        text = (markup.batch_prompt if a.cmd == "batch-prompt" else markup.edge_prompt)(base, a.name)
        if a.save:
            path = base.batches / f"{a.name}.prompt.md"
            write_private(path, text)
            print(path)
        else:
            print(text)
    elif a.cmd == "markup-import":
        _out(markup.markup_import(base, a.name, _read_input(a.file)), True)
    elif a.cmd == "vocab-candidates":
        _out(markup.vocab_candidates(base), True)
    elif a.cmd == "vocab-import":
        _out(markup.vocab_import(base, _read_input(a.file)), True)
    elif a.cmd == "edge-plan":
        _out(markup.edge_plan(base), True)
    elif a.cmd == "pending":
        for u in markup.pending(base):
            print(f"{u['id']}\t{u['chars']}\t{u['path']}")
    elif a.cmd == "build":
        _out(store.build(base), True)
    elif a.cmd == "status":
        print(base.status.read_text(encoding="utf-8") if base.status.exists() else "{}")
    elif a.cmd == "ask":
        _out(query.search(base, a.question, a.n), j, _print_ask)
    elif a.cmd == "unit":
        _out(query.unit(base, a.id, a.full), j, _print_unit)
    elif a.cmd == "concept":
        _out(query.concept(base, a.name), True)
    elif a.cmd == "concepts":
        for c in query.concepts_all(base):
            print(f"{c['concept']}: {c['units']} ед., {c['first']} → {c['last']} ({c['categories']})")
    elif a.cmd == "contradictions":
        _out(query.contradictions(base), True)
    elif a.cmd == "verdict":
        _out(query.set_verdict(base, a.id, a.verdict, a.note), True)
    elif a.cmd == "miss":
        _out(query.add_miss(base, a.question, a.found_id), True)
    elif a.cmd == "conflict":
        _out(query.conflict(base, a.unit_id, a.claim_kb, a.claim_model, a.resolution), True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
