"""TSV-файлы разметки и записей пользователя (вынесено из markup.py в 0.3.0).

Нужно и сборке, и поиску: «база в коробке» везёт только поиск, без модулей сборки.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .config import KbError, write_private


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
