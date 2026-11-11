#!/bin/bash
# Publishes the pen plugin from its development folder to the store.
# Source: ~/Code/Pen_Plugin (override with PEN_SRC). Only the plugin itself is published:
# everything except AGENTS.md, CLAUDE.md, docs/, dist/, git and housekeeping files.
# Feedback form docs/store/pen-feedback.issue-template.yml → .github/ISSUE_TEMPLATE/pen-feedback.yml.
#
#   scripts/sync-pen.sh            publish
#   scripts/sync-pen.sh --dry-run  only show what would change
set -euo pipefail

SRC="${PEN_SRC:-$HOME/Code/Pen_Plugin}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST="$ROOT/claude-code/pen"
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="-n"

test -f "$SRC/.claude-plugin/plugin.json" || { echo "no plugin found in $SRC" >&2; exit 1; }

mkdir -p "$DST" "$ROOT/.github/ISSUE_TEMPLATE"
rsync -rlc --delete $DRY -i \
  --exclude '.git/' --exclude 'AGENTS.md' --exclude 'CLAUDE.md' --exclude 'docs/' --exclude 'dist/' \
  --exclude '.gitignore' --exclude '.DS_Store' --exclude '__pycache__/' \
  "$SRC/" "$DST/"
rsync -c $DRY -i "$SRC/docs/store/pen-feedback.issue-template.yml" "$ROOT/.github/ISSUE_TEMPLATE/pen-feedback.yml"

echo "pen $(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$SRC/.claude-plugin/plugin.json") → $DST ${DRY:+(dry run, nothing changed)}"
echo "source: $(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo 'no git')"
