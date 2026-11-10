#!/bin/bash
# Выкладывает плагин kb «база в коробке» в магазин.
# 1) упаковщик исходника (Saved Downloader/plugins/kb/scripts/pack-boxed.py) собирает копию
#    из готовой базы во временную папку;
# 2) копия переносится в claude-code/kb; README.md магазина (инструкция для пользователя)
#    не затирается.
#
#   scripts/sync-kb.sh <папка собранной базы> [аргументы упаковщика: --title … --topic … --example …]
#   пример: scripts/sync-kb.sh ~/.kb/tcr --title "Теория каст и ролей" --example "Как перейти из одной касты в другую?"
set -euo pipefail

SRC="${KB_SRC:-$HOME/Code/Saved Downloader/plugins/kb}"
DST="$(cd "$(dirname "$0")/.." && pwd)/claude-code/kb"
BASE="${1:?нужна папка собранной базы, например ~/.kb/tcr}"; shift

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

/usr/bin/python3 "$SRC/scripts/pack-boxed.py" "$BASE" "$TMP/kb" "$@" --force >/dev/null
mkdir -p "$DST"
rsync -a --delete --exclude 'README.md' --exclude '.DS_Store' --exclude '__pycache__/' "$TMP/kb/" "$DST/"

echo "kb $(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$DST/.claude-plugin/plugin.json") (база: $BASE) → $DST"
echo "источник кода: $(git -C "$SRC" rev-parse --short HEAD) ($(git -C "$SRC" rev-parse --abbrev-ref HEAD))"
