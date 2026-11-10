"""Длинный документ → части по границам абзацев (SPEC §5 шаг 3, методология K8).

Контракт: ``"".join(split_text(t, n)) == t`` байт-в-байт; каждая часть ≤ n знаков; режем по
пустой строке, внутри слишком длинного абзаца — по переводу строки, затем по концу
предложения, в крайнем случае — жёстко. Идея контракта «реконструкция == источник» — из
SD scripts/_chunk.py (INV-11); код написан заново под плоскую модель «часть = единица».
"""
from __future__ import annotations

import re

_LEVELS = (re.compile(r"(\n[ \t]*\n+)"), re.compile(r"(\n)"), re.compile(r"((?<=[.!?…])\s+)"))


def _pieces(text: str, level: int) -> list[str]:
    """Куски текста с разделителем, прилипшим к левому куску; конкатенация = текст."""
    parts = _LEVELS[level].split(text)
    out = []
    for i in range(0, len(parts), 2):
        chunk = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
        if chunk:
            out.append(chunk)
    return out


def _atoms(text: str, limit: int, level: int = 0) -> list[str]:
    if len(text) <= limit:
        return [text]
    if level >= len(_LEVELS):
        return [text[i:i + limit] for i in range(0, len(text), limit)]
    out = []
    for piece in _pieces(text, level):
        out.extend(_atoms(piece, limit, level + 1) if len(piece) > limit else [piece])
    return out


def split_text(text: str, limit: int) -> list[str]:
    if limit < 1:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for atom in _atoms(text, limit):
        if cur and len(cur) + len(atom) > limit:
            parts.append(cur)
            cur = ""
        cur += atom
    if cur:
        parts.append(cur)
    assert "".join(parts) == text
    return parts
