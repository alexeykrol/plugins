#!/usr/bin/env bash
# AI Movie Company — one-shot installer for macOS, run by Claude on the user's behalf:
#   curl -fsSL https://raw.githubusercontent.com/alexeykrol/plugins/main/claude-code/ai-movie-company/install.sh | bash
#
# One prompt = everything: Claude's command line, Python, ffmpeg, the plugin, its engine, and the
# add-ons for the design look (Node.js, Chrome Headless Shell, the Remotion project, the Remotion
# plugin for Claude). About 3–5 minutes on a normal connection.
# No password, no Homebrew, no GitHub account. Tools go into ~/.local/bin, add-ons into
# ~/.ai_video_pipeline/extras; every download is pinned and checked against a sha256.
# Safe to run again: done steps are skipped (an interrupted run just continues).
#
# The last lines are for the assistant that runs it:
#   ADDONS: OK | ADDONS: PARTIAL <steps>  — the design add-ons; PARTIAL is not an error: the plugin
#                               works in the plain look, Elsa retries later ("установи дополнения")
#   STATUS: OK                — installed; quit Claude (Cmd+Q), open it, new Code session, say «Эльза»
#   STATUS: NEED_DEVTOOLS     — Apple is still installing its developer tools (its window was
#                               opened and this run waited 6 minutes): run this again at once,
#                               without asking — it waits on and then goes on; ~30 minutes in all
#   STATUS: FAILED <step>     — the message above says what went wrong (`devtools`: Apple's window
#                               was closed; a rerun opens it again)
#
# For testing (not for users): AIMC_STORE=<owner/repo | path to a marketplace folder> installs from
# another store (default alexeykrol/plugins); AIMC_MARKETPLACE=<name> its marketplace name (default
# krol); AIMC_SKIP_ADDONS=1 skips the add-ons; AIMC_DEVTOOLS_WAIT / AIMC_DEVTOOLS_POLL (seconds)
# shorten the wait for Apple's tools.
set -uo pipefail

STORE="${AIMC_STORE:-alexeykrol/plugins}"
MKT="${AIMC_MARKETPLACE:-krol}"
PLUGIN_ID="ai-movie-company@$MKT"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
STEPS=7
T0=$(date +%s)

say()  { echo "$*"; }
step() { echo "[$1/$STEPS] $2"; }
fail() { echo "✗ $2"; echo "STATUS: FAILED $1"; exit 1; }

# fetch URL SHA256 DEST
fetch() {
  curl -fsSL --retry 3 -m 600 -o "$3" "$1" || fail download "could not download $1 — the internet connection dropped? Run the installer again; finished steps are kept."
  local got; got="$(shasum -a 256 "$3" | awk '{print $1}')"
  [ "$got" = "$2" ] || fail checksum "the downloaded file $1 is not the expected one (checksum) — nothing installed from it; try again later"
}

[ "$(uname -s)" = "Darwin" ] || fail platform "this installer is for macOS"
case "$(uname -m)" in
  arm64)  ARCH=arm64
          UV_PKG=uv-aarch64-apple-darwin
          UV_SHA=60fbd5c8a1ed681845eea28d9d622fcde03c51e91a43465b64a1e17db1ed3472
          FF_DIR=arm64/1789931890_9.0.2
          FFMPEG_SHA=c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924
          FFPROBE_SHA=fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6 ;;
  x86_64) fail platform "Macs with an Intel processor are not supported yet (only Apple M1 and newer)"
          ARCH=x86_64
          UV_PKG=uv-x86_64-apple-darwin
          UV_SHA=63ba17fdda55ad1a230d4b37fc6017ffa10c23688c83818bd3bfdf914c652a02
          FF_DIR=amd64/1789931006_9.0.2
          FFMPEG_SHA=7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4
          FFPROBE_SHA=2322438ed2f6319a691291b247d09c69dcaa3a982460d1f269a7e1af335cfdfd ;;
  *) fail platform "unknown Mac processor $(uname -m)" ;;
esac
UV_VERSION=0.13.6
FF_BASE=https://ffmpeg.martin-riedl.de/download/macos

# 0. This Mac — silent when it is fine; one plain sentence + STATUS: FAILED when it is not.
#    macOS minimums of what we install (measured = LC_BUILD_VERSION minos of the pinned binary,
#    `otool -l`, 2026-11-19; docs = the project's own page):
#      Claude Code (CLI)              13.0  docs code.claude.com/docs/en/setup ("macOS 13.0+"); measured 13.0
#      Claude desktop app             13.0  measured: LSMinimumSystemVersion in Claude.app/Contents/Info.plist
#      Chrome Headless Shell 158      13.0  measured (chrome-headless-shell-mac-arm64, 158.0.8127.2)
#      ffmpeg 9.0.2 (martin-riedl)    12.0  measured
#      uv 0.13.6                      11.0  measured
#      Node.js 24.22.2                13.5  docs github.com/nodejs/node/blob/v24.x/BUILDING.md
#                                           ("macOS arm64 >= 13.5"); measured 13.5 — add-ons only
#    So: below 13.0 nothing works (FAILED); 13.0–13.4 installs everything but the add-ons.
#    Free space, measured on the 2026-11-19 clean install: Claude CLI 337 MB, Python venv 156 MB,
#    Python 3.13 (uv) ~100 MB, ffmpeg+ffprobe 126 MB -> base ~0.75 GB + downloads in flight;
#    add-ons: Node 199 + Chrome 205 + Remotion 266 MB = 0.7 GB + ~0.25 GB archives while unpacking.
#    So: under 1 GB free the install cannot finish (FAILED); under 2 GB the add-ons wait.
MACOS_MIN=1300; ADDONS_MACOS_MIN=1305; DISK_MIN_MB=1024; ADDONS_DISK_MIN_MB=2048
MACOS_VER="$(sw_vers -productVersion 2>/dev/null || echo 0)"
MACOS_NUM="$(echo "$MACOS_VER" | awk -F. '{ printf "%d", $1 * 100 + $2 }')"
FREE_MB="$(df -Pk "$HOME" 2>/dev/null | awk 'NR == 2 { printf "%d", $4 / 1024 }')"
[ "${MACOS_NUM:-0}" -ge "$MACOS_MIN" ] \
  || fail macos "this Mac has macOS $MACOS_VER; the plugin and Claude itself need macOS 13 (Ventura) or newer — update macOS in System Settings → General → Software Update, then run the installer again"
[ "${FREE_MB:-0}" -ge "$DISK_MIN_MB" ] \
  || fail disk "the Mac has only ${FREE_MB:-0} MB free; the installation needs about 1 GB (2 GB with the design add-ons) — free some space (empty the Bin, delete old downloads) and run the installer again"
ADDONS_HOLD=""
[ "$MACOS_NUM" -ge "$ADDONS_MACOS_MIN" ] || ADDONS_HOLD="macos"
[ "$FREE_MB" -ge "$ADDONS_DISK_MIN_MB" ] || ADDONS_HOLD="${ADDONS_HOLD:+$ADDONS_HOLD,}disk"

say "Installing AI Movie Company: $STEPS steps, about 3–5 minutes. Please wait until the last line."

# 1. Apple's developer tools (git) — Claude fetches the plugin store with git. macOS asks once; no
#    password. Nothing above this point and nothing in this step calls a macOS stub (/usr/bin/git,
#    python3, pip3, make, clang, …): a stub called before the tools exist pops the same Apple
#    window. Python is looked for only after this step.
#    No tools: open Apple's window once (a marker keeps a rerun from opening it again), then wait
#    here for the tools — up to AIMC_DEVTOOLS_WAIT seconds (default 6 minutes, so one run stays
#    under the 10 minutes of one Claude command) — and go on with the install in the same run.
#    Still installing when the time is up: STATUS: NEED_DEVTOOLS (the assistant runs this again at
#    once, without asking; about 30 minutes in all). Apple's window closed without installing:
#    STATUS: FAILED devtools (the assistant asks with buttons; a rerun opens the window again).
DEVTOOLS_MARK="$HOME/.ai_video_pipeline/install/devtools-requested"
DEVTOOLS_WAIT="${AIMC_DEVTOOLS_WAIT:-360}"
DEVTOOLS_POLL="${AIMC_DEVTOOLS_POLL:-10}"
devtools_ok() { xcode-select -p >/dev/null 2>&1; }
# Apple's installer app (verified on disk: /System/Library/CoreServices/Install Command Line
# Developer Tools.app, executable "Install Command Line Developer Tools"). -f: the process name
# itself is cut to 16 characters by macOS.
apple_window_open() { pgrep -f "Install Command Line Developer Tools" >/dev/null 2>&1; }
if ! devtools_ok; then
  mkdir -p "$(dirname "$DEVTOOLS_MARK")"
  asked="$(cat "$DEVTOOLS_MARK" 2>/dev/null || echo 0)"
  case "$asked" in ''|*[!0-9]*) asked=0 ;; esac
  if [ $(( $(date +%s) - asked )) -gt 2400 ]; then        # not asked in the last 40 minutes
    xcode-select --install >/dev/null 2>&1 || true
    date +%s >"$DEVTOOLS_MARK"
    say "      Apple's window \"install the command line developer tools\" is open: press Install."
  fi
  say "      waiting for Apple's developer tools (up to $(( DEVTOOLS_WAIT / 60 )) minutes)…"
  waited=0; seen=0; gone=0
  until devtools_ok; do
    if [ "$waited" -ge "$DEVTOOLS_WAIT" ]; then
      say "Apple is still installing its developer tools."
      echo "STATUS: NEED_DEVTOOLS"
      exit 0
    fi
    if apple_window_open; then seen=1; gone=0
    elif [ "$seen" = 1 ]; then
      gone=$((gone + 1))
      if [ "$gone" -ge 6 ]; then                 # a minute without the window
        rm -f "$DEVTOOLS_MARK"
        fail devtools "Apple's window closed before the developer tools were installed (\"Not Now\" or an error) — run the installer again and it opens the window again"
      fi
    fi
    sleep "$DEVTOOLS_POLL"; waited=$((waited + DEVTOOLS_POLL))
  done
fi
rm -f "$DEVTOOLS_MARK"
step 1 "Apple developer tools: ready"

# 2. Claude command line (the official installer, into ~/.local/bin)
if ! command -v claude >/dev/null 2>&1; then
  say "      installing the Claude command line…"
  curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1 || fail claude "could not install the Claude command line (internet?)"
  command -v claude >/dev/null 2>&1 || fail claude "Claude command line did not appear in $BIN"
fi
step 2 "Claude command line: ready"

# 3. Python 3.12+ (via uv, a standalone Python installer)
PY=""
find_python() {
  local c
  for c in python3.14 python3.13 python3.12 python3; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      PY="$(command -v "$c")"; return 0
    fi
  done
  return 1
}
if ! find_python; then
  if ! command -v uv >/dev/null 2>&1; then
    say "      installing uv $UV_VERSION (a Python installer)…"
    fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_PKG.tar.gz" "$UV_SHA" "$TMP/uv.tgz"
    tar -xzf "$TMP/uv.tgz" -C "$TMP" || fail uv "could not unpack uv"
    install -m 755 "$TMP/$UV_PKG/uv" "$BIN/uv"
  fi
  say "      installing Python 3.13…"
  uv python install 3.13 >/dev/null 2>&1 || fail python "could not install Python 3.13 (internet?)"
  find_python || fail python "Python 3.13 did not appear in $BIN"
fi
step 3 "Python: ready"

# 4. ffmpeg + ffprobe (static build with subtitles and text drawing)
ff_ok() {
  command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 || return 1
  local filters; filters="$(ffmpeg -hide_banner -filters 2>/dev/null)" || return 1
  ffprobe -hide_banner -version >/dev/null 2>&1 || return 1
  [[ "$filters" == *" subtitles "* && "$filters" == *" drawtext "* ]]
}
if ! ff_ok; then
  say "      downloading ffmpeg 9.0.2 (video tools, ~60 MB)…"
  fetch "$FF_BASE/$FF_DIR/ffmpeg.zip"  "$FFMPEG_SHA"  "$TMP/ffmpeg.zip"
  fetch "$FF_BASE/$FF_DIR/ffprobe.zip" "$FFPROBE_SHA" "$TMP/ffprobe.zip"
  unzip -o -q "$TMP/ffmpeg.zip" -d "$TMP/ff" && unzip -o -q "$TMP/ffprobe.zip" -d "$TMP/ff" \
    || fail ffmpeg "could not unpack ffmpeg"
  install -m 755 "$TMP/ff/ffmpeg" "$BIN/ffmpeg"
  install -m 755 "$TMP/ff/ffprobe" "$BIN/ffprobe"
  ff_ok || fail ffmpeg "ffmpeg does not run on this Mac"
fi
step 4 "ffmpeg: ready"

# 5. The plugin from the public store
# json_has <field> <value>: does the JSON list on stdin have an item with item[field] == value?
json_has() {
  "$PY" -c '
import json, sys
try: items = json.load(sys.stdin)
except ValueError: sys.exit(1)
sys.exit(0 if any(isinstance(i, dict) and i.get(sys.argv[1]) == sys.argv[2] for i in items) else 1)
' "$1" "$2"
}
if ! claude plugin marketplace list --json 2>/dev/null | json_has name "$MKT"; then
  claude plugin marketplace add "$STORE" >/dev/null 2>&1 || fail plugin "could not add the plugin store (internet?)"
else
  claude plugin marketplace update "$MKT" >/dev/null 2>&1 || true
fi
if claude plugin list --json 2>/dev/null | json_has id "$PLUGIN_ID"; then
  claude plugin update "$PLUGIN_ID" >/dev/null 2>&1 || true
else
  claude plugin install "$PLUGIN_ID" >/dev/null 2>&1 || fail plugin "could not install the plugin"
fi
PLUGIN_DIR="$(claude plugin list --json 2>/dev/null | "$PY" -c '
import json, sys
want = sys.argv[1]
for p in json.load(sys.stdin):
    if p.get("id") == want:
        print(p.get("installPath", "")); print(p.get("version", ""))
        break
' "$PLUGIN_ID")"
PLUGIN_VER="$(echo "$PLUGIN_DIR" | sed -n 2p)"
PLUGIN_DIR="$(echo "$PLUGIN_DIR" | sed -n 1p)"
[ -n "$PLUGIN_DIR" ] && [ -x "$PLUGIN_DIR/scripts/vp" ] || fail plugin "the plugin is not where Claude says it is"
step 5 "plugin ai-movie-company $PLUGIN_VER: ready"

# 6. The engine: the plugin's private Python environment (pinned, hash-checked), built now so the
#    first session starts at once. Same folder Claude gives the plugin (CLAUDE_PLUGIN_DATA).
VP="$PLUGIN_DIR/scripts/vp"
export CLAUDE_PLUGIN_DATA="$HOME/.claude/plugins/data/$(echo "$PLUGIN_ID" | sed 's/[^A-Za-z0-9_-]/-/g')"
"$VP" extras status --json >"$TMP/engine.log" 2>&1 || {
  tail -5 "$TMP/engine.log" | sed 's/^/      /'
  fail engine "the plugin's engine did not start (see above)"
}
step 6 "engine: ready"

# 7. The add-ons for the design look. A failure here does not fail the install: the plugin works in
#    the plain look, `vp doctor` says what is missing, Elsa retries with `vp extras install`.
ADDONS="ADDONS: OK"
if [ "${AIMC_SKIP_ADDONS:-}" = "1" ]; then
  ADDONS="ADDONS: PARTIAL skipped"
  step 7 "add-ons: skipped"
elif [ -n "$ADDONS_HOLD" ] && ! grep -q '"all_installed": true' "$TMP/engine.log"; then
  # step 0 found the Mac short of what the add-ons need (macOS 13.5 for Node 24, 2 GB free)
  ADDONS="ADDONS: PARTIAL $ADDONS_HOLD"
  case "$ADDONS_HOLD" in
    macos) step 7 "add-ons: not now — Node.js 24 for the design look needs macOS 13.5 or newer (this Mac has $MACOS_VER); everything else works in the plain look" ;;
    disk)  step 7 "add-ons: not now — they need about 2 GB free (there are $FREE_MB MB); everything else works in the plain look" ;;
    *)     step 7 "add-ons: not now — they need macOS 13.5 or newer and about 2 GB free; everything else works in the plain look" ;;
  esac
else
  step 7 "add-ons for the design look (Node.js, Chrome for drawing, Remotion), ~130 MB download…"
  "$VP" extras install 2>&1 | tee "$TMP/extras.log" | grep -v '^EXTRAS:' | sed 's/^/      /'
  last="$(grep '^EXTRAS:' "$TMP/extras.log" | tail -1)"
  case "$last" in
    "EXTRAS: OK") ADDONS="ADDONS: OK" ;;
    "EXTRAS: PARTIAL "*) ADDONS="ADDONS: PARTIAL ${last#EXTRAS: PARTIAL }" ;;
    *) ADDONS="ADDONS: PARTIAL all" ;;
  esac
fi

say "Done in $(( ($(date +%s) - T0) / 60 )) min $(( ($(date +%s) - T0) % 60 )) s."
say "RESTART NEEDED: quit Claude completely (Cmd+Q), open it again, start a new session in the Code tab and write «Эльза»."
echo "$ADDONS"
echo "STATUS: OK"
