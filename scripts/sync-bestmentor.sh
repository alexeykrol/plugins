#!/bin/bash
# Publishes the BestMentor "base in a box" plugin (id bestmentor) to the store.
# 1) the project's packer (~/Code/BestMentor_Plugin/scripts/pack-boxed.py; override with BESTMENTOR_SRC) builds a copy
#    from a finished base into a temporary folder;
# 2) the copy is moved into claude-code/bestmentor; the store's README.md and README.ru.md (user install
#    guide, maintained here in the store) are not overwritten.
#
#   scripts/sync-bestmentor.sh <built base folder> [packer arguments: --title … --topic … --example …]
#   example: scripts/sync-bestmentor.sh ~/Code/BestMentor_Plugin/assets/tcr --title "Теория каст и ролей" --example "Как перейти из одной касты в другую?"
set -euo pipefail

SRC="${BESTMENTOR_SRC:-$HOME/Code/BestMentor_Plugin}"
DST="$(cd "$(dirname "$0")/.." && pwd)/claude-code/bestmentor"
BASE="${1:?need a built base folder, e.g. ~/Code/BestMentor_Plugin/assets/tcr}"; shift

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

/usr/bin/python3 "$SRC/scripts/pack-boxed.py" "$BASE" "$TMP/kb" "$@" --force >/dev/null
mkdir -p "$DST"
rsync -a --delete --exclude 'README.md' --exclude 'README.ru.md' --exclude '.DS_Store' --exclude '__pycache__/' "$TMP/kb/" "$DST/"

echo "bestmentor $(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$DST/.claude-plugin/plugin.json") (base: $BASE) → $DST"
echo "code source: $(git -C "$SRC" rev-parse --short HEAD) ($(git -C "$SRC" rev-parse --abbrev-ref HEAD))"
