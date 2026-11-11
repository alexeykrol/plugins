#!/bin/bash
# Publishes the kb "base in a box" plugin to the store.
# 1) the kb project's packer (~/Code/Kb_Plugin/scripts/pack-boxed.py; override with KB_SRC) builds a copy
#    from a finished base into a temporary folder;
# 2) the copy is moved into claude-code/kb; the store's README.md and README.ru.md (user install
#    guide, maintained here in the store) are not overwritten.
#
#   scripts/sync-kb.sh <built base folder> [packer arguments: --title … --topic … --example …]
#   example: scripts/sync-kb.sh ~/Code/Kb_Plugin/assets/tcr --title "Теория каст и ролей" --example "Как перейти из одной касты в другую?"
set -euo pipefail

SRC="${KB_SRC:-$HOME/Code/Kb_Plugin}"
DST="$(cd "$(dirname "$0")/.." && pwd)/claude-code/kb"
BASE="${1:?need a built base folder, e.g. ~/Code/Kb_Plugin/assets/tcr}"; shift

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

/usr/bin/python3 "$SRC/scripts/pack-boxed.py" "$BASE" "$TMP/kb" "$@" --force >/dev/null
mkdir -p "$DST"
rsync -a --delete --exclude 'README.md' --exclude 'README.ru.md' --exclude '.DS_Store' --exclude '__pycache__/' "$TMP/kb/" "$DST/"

echo "kb $(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$DST/.claude-plugin/plugin.json") (base: $BASE) → $DST"
echo "code source: $(git -C "$SRC" rev-parse --short HEAD) ($(git -C "$SRC" rev-parse --abbrev-ref HEAD))"
