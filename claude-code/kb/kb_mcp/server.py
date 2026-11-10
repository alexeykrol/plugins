"""kb-mcp — MCP-сервер (stdio, JSON-RPC 2.0 построчно) над базами kb (SPEC §3, §7, Э4).

Только stdlib, без сети. Читает ``$KB_HOME/<slug>/kb.sqlite``; пишет только в verdicts /
misses / conflicts (и их TSV в markup/). Ничего не исполняет. Инструменты MVP:
kb_search, kb_unit, kb_verdict, kb_miss, kb_conflict.

«База в коробке» (0.3.0): если в плагине есть готовая база (``skills/kb-ask/data/``), сервер
берёт её по умолчанию — только на чтение, без ``~/.kb`` и без init; решения пользователя пишет
в ``$KB_HOME/<имя>/user/``. Путь к базе вычисляется от расположения кода, а не из env.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import traceback

from kbtool import __version__, query
from kbtool.config import RESOLUTIONS, VERDICTS, KbError, boxed_base, list_bases, resolve_base

PROTOCOL = "2025-06-18"
INSTRUCTIONS = """База знаний пользователя из его документов (kb). Протокол ответа:
1) На вопрос о содержимом документов сначала вызови kb_search; пусто — переформулируй один раз; снова пусто — скажи «в базе нет» и предложи записать промах (kb_miss).
2) Каждое утверждение из базы — с id единицы и короткой цитатой (≤2 строк) из поля quote или kb_unit; знание модели помечай «[модель]».
3) Единицы с вердиктом «отвергнуто/устарело» (suppressed) в ответ не идут — упомяни одной строкой.
4) Противоречия между документами (contradictions) показывай обеими сторонами с датами.
5) Конфликт база ↔ твоё знание: только для проверяемого факта (дата, число, определение, причина; флаг checkable) — не мнение, не термин, не давность. Сначала смотри conflict_decisions: решение уже есть — следуй ему, не спрашивай. Нет решения — не выбирай сам: спроси пользователя «верить базе / верить модели / показывать оба» и запиши kb_conflict(resolution=kb|model|both). Не можешь спросить — покажи оба и запиши resolution=pending.
6) Вердикт пользователя («это устарело», «ценно») — kb_verdict."""

_KB = {"type": "string", "description": "slug базы; можно не указывать, если база одна"}
TOOLS = [
    dict(name="kb_search", description="Поиск по базе документов: единицы с цитатами, понятия, связи, противоречия, "
                                       "прежние решения по конфликтам. Вызывать до ответа о содержимом документов.",
         inputSchema={"type": "object", "properties": {
             "question": {"type": "string", "description": "вопрос или ключевые слова"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}, "kb": _KB},
             "required": ["question"]}),
    dict(name="kb_unit", description="Карточка единицы: разметка, понятия, связи, соседи, текст (начало или весь).",
         inputSchema={"type": "object", "properties": {
             "id": {"type": "string"}, "full": {"type": "boolean", "default": False}, "kb": _KB},
             "required": ["id"]}),
    dict(name="kb_verdict", description="Записать вердикт пользователя о единице. Отвергнутое/устаревшее "
                                        "дальше не предлагается, но хранится.",
         inputSchema={"type": "object", "properties": {
             "id": {"type": "string"}, "verdict": {"type": "string", "enum": list(VERDICTS)},
             "note": {"type": "string"}, "kb": _KB}, "required": ["id", "verdict"]}),
    dict(name="kb_miss", description="Записать промах поиска: ответ в базе есть, а kb_search его не нашёл.",
         inputSchema={"type": "object", "properties": {
             "question": {"type": "string"}, "found_id": {"type": "string"}, "kb": _KB}, "required": ["question"]}),
    dict(name="kb_conflict", description="Конфликт утверждения базы со знанием модели. Без resolution — прочитать "
                                         "прежние решения (ask=true значит решения нет, надо спросить пользователя). "
                                         "С resolution — записать решение пользователя.",
         inputSchema={"type": "object", "properties": {
             "unit_id": {"type": "string"}, "claim_kb": {"type": "string"}, "claim_model": {"type": "string"},
             "resolution": {"type": "string", "enum": list(RESOLUTIONS)}, "kb": _KB}, "required": ["unit_id"]}),
]


def _box_line():
    """Строка о готовой базе плагина для инструкций и описания kb_search; None — её нет."""
    try:
        box = boxed_base()
    except KbError:
        return None
    if box is None:
        return None
    title, topic = box.meta.get("title") or box.slug, box.meta.get("topic") or ""
    return f"Готовая база «{title}» поставляется с плагином" + (f"; тема: {topic}" if topic else "") + "."


def instructions() -> str:
    line = _box_line()
    return INSTRUCTIONS if not line else (line + " Вопросы о её содержимом — через kb_search.\n" + INSTRUCTIONS)


def tools() -> list:
    line = _box_line()
    if not line:
        return TOOLS
    out = copy.deepcopy(TOOLS)
    out[0]["description"] += " " + line
    return out


def _need(args: dict, *keys: str) -> None:
    for k in keys:
        if not isinstance(args.get(k), str) or not args[k].strip():
            raise KbError(f"нужен непустой аргумент {k}")


def call_tool(name: str, args: dict):
    if not isinstance(args, dict):
        raise KbError("arguments должны быть объектом")
    kb = args.get("kb") if isinstance(args.get("kb"), str) else None
    if name == "kb_search":
        _need(args, "question")
        limit = args.get("limit", 8)
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise KbError("limit — целое 1..20")
        return query.search(resolve_base(kb), args["question"], limit)
    if name == "kb_unit":
        _need(args, "id")
        return query.unit(resolve_base(kb), args["id"], bool(args.get("full")))
    if name == "kb_verdict":
        _need(args, "id", "verdict")
        return query.set_verdict(resolve_base(kb), args["id"], args["verdict"], str(args.get("note") or ""))
    if name == "kb_miss":
        _need(args, "question")
        return query.add_miss(resolve_base(kb), args["question"], str(args.get("found_id") or ""))
    if name == "kb_conflict":
        _need(args, "unit_id")
        return query.conflict(resolve_base(kb), args["unit_id"], str(args.get("claim_kb") or ""),
                              str(args.get("claim_model") or ""), str(args.get("resolution") or ""))
    raise KbError(f"нет инструмента {name}")


def handle(msg: dict) -> dict | None:
    """Один JSON-RPC запрос → ответ (None для уведомлений)."""
    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:                       # уведомление (notifications/initialized и т. п.)
        return None
    try:
        if method == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
            result = {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "kb", "version": __version__}, "instructions": instructions()}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools()}
        elif method == "tools/call":
            params = msg.get("params") or {}
            try:
                data = call_tool(params.get("name"), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                          "isError": False}
            except KbError as exc:
                result = {"content": [{"type": "text", "text": f"ошибка: {exc}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"нет метода {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as exc:  # сервер не падает на одном запросе
        print(traceback.format_exc(), file=sys.stderr)
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": type(exc).__name__}}


def serve(stdin=sys.stdin, stdout=sys.stdout) -> int:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(msg) if isinstance(msg, dict) else \
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def selftest_boxed(box) -> int:
    """Готовая база плагина: открыть только на чтение, найти пример вопроса, ничего не записать."""
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r["result"]["serverInfo"]["name"] == "kb"
    names = [t["name"] for t in handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    inf = query.info(box)
    if not inf["intact"]:
        print("kb-mcp selftest: файл базы повреждён (контрольная сумма не совпадает с box.json)", file=sys.stderr)
        return 1
    q = box.meta.get("example_question") or (inf["top_concepts"] or ["база"])[0]
    r = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "kb_search", "arguments": {"question": q}}})
    if r["result"]["isError"]:
        print("kb-mcp selftest: " + r["result"]["content"][0]["text"], file=sys.stderr)
        return 1
    data = json.loads(r["result"]["content"][0]["text"])
    if not data["results"]:
        print(f"kb-mcp selftest: по примеру вопроса «{q}» ничего не найдено", file=sys.stderr)
        return 1
    print(f"kb-mcp selftest OK: база «{box.meta.get('title') or box.slug}» ({inf['units']} единиц, только чтение), "
          f"пример «{q}» → {len(data['results'])} результатов; tools={','.join(names)}")
    return 0


def selftest() -> int:
    """Временный корпус → сборка → запросы через handle(). Сеть не нужна.
    Если в плагине есть готовая база — проверяется она (в упакованной копии модулей сборки нет)."""
    box = boxed_base()
    if box is not None:
        return selftest_boxed(box)
    from kbtool import ingest, store
    from kbtool.config import init_base
    with tempfile.TemporaryDirectory(prefix="kb-selftest-") as tmp:
        os.environ["KB_HOME"] = os.path.join(tmp, "home")
        corpus = os.path.join(tmp, "docs")
        os.makedirs(corpus)
        with open(os.path.join(corpus, "note.md"), "w", encoding="utf-8") as f:
            f.write("# Проверка\n\nСамотестирование сервера базы знаний.\n")
        base = init_base(corpus)
        ingest.scan(base)
        ingest.extract(base)
        store.build(base)
        r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert r["result"]["serverInfo"]["name"] == "kb"
        r = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in r["result"]["tools"]]
        r = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "kb_search", "arguments": {"question": "самотестирование"}}})
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["results"], "поиск пуст"
    print(f"kb-mcp selftest OK: tools={','.join(names)}")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()
    if "--list" in argv:
        box = boxed_base()
        print(json.dumps(([box.slug] if box else []) + [b.slug for b in list_bases()], ensure_ascii=False))
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main())
