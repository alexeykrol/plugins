#!/bin/bash
# Publishes the ai-movie-company plugin to the store.
# Source: the BUILT release folder of the development repository,
# ~/Code/AI_MovieCompany_Plugin/dist/ai-movie-company (override with AMC_SRC). That folder is made by
# the development repository's scripts/release.sh: an allowlist copy (no tests, docs, notes or
# Legacy) that passed validation and the mod's tests. Run release.sh there first.
#
#   scripts/sync-ai-movie-company.sh            publish
#   scripts/sync-ai-movie-company.sh --dry-run  only show what would change
set -euo pipefail

SRC="${AMC_SRC:-$HOME/Code/AI_MovieCompany_Plugin/dist/ai-movie-company}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST="$ROOT/claude-code/ai-movie-company"
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="-n"

test -f "$SRC/.claude-plugin/plugin.json" || { echo "no built plugin in $SRC (run scripts/release.sh there)" >&2; exit 1; }
test -f "$SRC/LICENSE" || { echo "no LICENSE in $SRC" >&2; exit 1; }

mkdir -p "$DST"
rsync -rlc --delete $DRY -i \
  --exclude '.DS_Store' --exclude '__pycache__/' \
  "$SRC/" "$DST/"

if [ -z "$DRY" ]; then
  claude plugin validate "$DST/.claude-plugin/plugin.json" --profile directory --strict
fi

echo "ai-movie-company $(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$SRC/.claude-plugin/plugin.json") → $DST ${DRY:+(dry run, nothing changed)}"
echo "source: $(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo 'no git')"
