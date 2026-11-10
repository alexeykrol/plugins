#!/bin/bash
# Выкладывает плагин pen из папки разработки в магазин.
# Источник — ~/Code/Pen_Plugin (другой путь — PEN_SRC). В магазин уходит сам плагин:
# всё, кроме AGENTS.md, CLAUDE.md, docs/, dist/, git и служебного мусора.
# Форма отзыва docs/store/pen-feedback.issue-template.yml → .github/ISSUE_TEMPLATE/pen-feedback.yml.
#
#   scripts/sync-pen.sh            выложить
#   scripts/sync-pen.sh --dry-run  только показать, что изменится
set -euo pipefail

SRC="${PEN_SRC:-$HOME/Code/Pen_Plugin}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST="$ROOT/claude-code/pen"
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="-n"

test -f "$SRC/.claude-plugin/plugin.json" || { echo "нет плагина в $SRC" >&2; exit 1; }

mkdir -p "$DST" "$ROOT/.github/ISSUE_TEMPLATE"
rsync -rlc --delete $DRY -i \
  --exclude '.git/' --exclude 'AGENTS.md' --exclude 'CLAUDE.md' --exclude 'docs/' --exclude 'dist/' \
  --exclude '.gitignore' --exclude '.DS_Store' --exclude '__pycache__/' \
  "$SRC/" "$DST/"
rsync -c $DRY -i "$SRC/docs/store/pen-feedback.issue-template.yml" "$ROOT/.github/ISSUE_TEMPLATE/pen-feedback.yml"

echo "pen $(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$SRC/.claude-plugin/plugin.json") → $DST ${DRY:+(пробный прогон, ничего не изменено)}"
echo "источник: $(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo 'без git')"
