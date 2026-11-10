#!/bin/bash
# Выкладывает проверенную версию плагина kb из рабочего репозитория в магазин.
# Наружу не идут: docs/ (спека, отчёты, исследования — внутренняя кухня, местами с цитатами
# личных заметок), tests/ (опираются на личный корпус владельца), README.md разработчика
# (у магазина своя инструкция для пользователя — claude-code/kb/README.md, она не затирается).
set -euo pipefail

SRC="${KB_SRC:-$HOME/Code/Saved Downloader/plugins/kb}"
DST="$(cd "$(dirname "$0")/.." && pwd)/claude-code/kb"

[ -f "$SRC/.claude-plugin/plugin.json" ] || { echo "нет плагина в $SRC" >&2; exit 1; }
mkdir -p "$DST"

rsync -a --delete \
  --exclude 'docs/' \
  --exclude 'tests/' \
  --exclude 'README.md' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  "$SRC/" "$DST/"

echo "kb $(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$DST/.claude-plugin/plugin.json") → $DST"
echo "источник: $(git -C "$SRC" rev-parse --short HEAD) ($(git -C "$SRC" rev-parse --abbrev-ref HEAD))"
