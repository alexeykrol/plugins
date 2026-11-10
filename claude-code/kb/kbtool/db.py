"""Открытие ``kb.sqlite`` (вынесено из store.py в 0.3.0 — поиску не нужны модули сборки).

База своей сборки (``~/.kb/<slug>/``) открывается на запись только для записей пользователя.
Готовая база из плагина (``skills/kb-ask/data/``) — всегда только чтение: ``mode=ro`` и
``immutable=1`` (папка плагина может быть недоступна для записи; ни журнала, ни -wal рядом).
"""
from __future__ import annotations

import sqlite3
from urllib.parse import quote

from . import SCHEMA_VERSION
from .config import KbError


def db_uri(path, readonly: bool = True, immutable: bool = False) -> str:
    uri = f"file:{quote(str(path))}?mode={'ro' if readonly else 'rw'}"
    return uri + ("&immutable=1" if immutable else "")


def connect(base, write: bool = False) -> sqlite3.Connection:
    if not base.db.exists():
        raise KbError(f"базы нет: {base.db} — сначала kbtool build")
    boxed = getattr(base, "boxed", False)
    if write and boxed:
        raise KbError("готовая база плагина открывается только для чтения")
    con = sqlite3.connect(db_uri(base.db, readonly=not write, immutable=boxed), uri=True)
    con.row_factory = sqlite3.Row
    v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not v or int(v[0]) != SCHEMA_VERSION:
        con.close()
        raise KbError("версия схемы базы не совпадает с плагином — пересоберите: kbtool build")
    return con
