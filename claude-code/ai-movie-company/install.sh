#!/usr/bin/env bash
# AI Movie Company — one-shot installer for macOS, run by Claude on the user's behalf:
#   curl -fsSL https://raw.githubusercontent.com/alexeykrol/plugins/main/claude-code/ai-movie-company/install.sh | bash
#
# No password, no Homebrew, no GitHub account. Everything goes into ~/.local/bin; every download is
# pinned and checked against a sha256 written here. Safe to run again: done steps are skipped.
# The last line is for the assistant that runs it:
#   STATUS: OK                — installed; restart Claude, then /ai-movie-company:start
#   STATUS: NEED_DEVTOOLS     — macOS shows "install command line developer tools": press Install,
#                               wait until it finishes, run this again
#   STATUS: FAILED <step>     — the message above says what went wrong
set -uo pipefail

BIN="$HOME/.local/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say()  { echo "• $*"; }
fail() { echo "✗ $2"; echo "STATUS: FAILED $1"; exit 1; }

# fetch URL SHA256 DEST
fetch() {
  curl -fsSL --retry 3 -m 600 -o "$3" "$1" || fail download "could not download $1 (network?)"
  local got; got="$(shasum -a 256 "$3" | awk '{print $1}')"
  [ "$got" = "$2" ] || fail checksum "checksum mismatch for $1 — not installing it"
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

# 1. Apple's developer tools (git) — needed to fetch the plugin. macOS asks once; no password.
if ! xcode-select -p >/dev/null 2>&1; then
  say "macOS needs its free developer tools once (a window will appear: press Install)."
  xcode-select --install >/dev/null 2>&1 || true
  echo "STATUS: NEED_DEVTOOLS"
  exit 0
fi
say "developer tools: ready"

# 2. Claude command line (the official installer, into ~/.local/bin)
if ! command -v claude >/dev/null 2>&1; then
  say "installing the Claude command line..."
  curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1 || fail claude "could not install the Claude command line"
  command -v claude >/dev/null 2>&1 || fail claude "Claude command line did not appear in $BIN"
fi
say "claude: $(command -v claude)"

# 3. Python 3.12+ (via uv, a standalone Python installer)
have_python() {
  local c
  for c in python3.14 python3.13 python3.12 python3; do
    command -v "$c" >/dev/null 2>&1 \
      && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null \
      && return 0
  done
  return 1
}
if ! have_python; then
  if ! command -v uv >/dev/null 2>&1; then
    say "installing uv $UV_VERSION..."
    fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_PKG.tar.gz" "$UV_SHA" "$TMP/uv.tgz"
    tar -xzf "$TMP/uv.tgz" -C "$TMP" || fail uv "could not unpack uv"
    install -m 755 "$TMP/$UV_PKG/uv" "$BIN/uv"
  fi
  say "installing Python 3.13..."
  uv python install 3.13 >/dev/null 2>&1 || fail python "could not install Python 3.13"
  have_python || fail python "Python 3.13 did not appear in $BIN"
fi
say "python: ready"

# 4. ffmpeg + ffprobe (static build with subtitles and text drawing)
ff_ok() {
  command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 || return 1
  local filters; filters="$(ffmpeg -hide_banner -filters 2>/dev/null)" || return 1
  ffprobe -hide_banner -version >/dev/null 2>&1 || return 1
  [[ "$filters" == *" subtitles "* && "$filters" == *" drawtext "* ]]
}
if ! ff_ok; then
  say "installing ffmpeg 9.0.2 ($ARCH)..."
  fetch "$FF_BASE/$FF_DIR/ffmpeg.zip"  "$FFMPEG_SHA"  "$TMP/ffmpeg.zip"
  fetch "$FF_BASE/$FF_DIR/ffprobe.zip" "$FFPROBE_SHA" "$TMP/ffprobe.zip"
  unzip -o -q "$TMP/ffmpeg.zip" -d "$TMP/ff" && unzip -o -q "$TMP/ffprobe.zip" -d "$TMP/ff" \
    || fail ffmpeg "could not unpack ffmpeg"
  install -m 755 "$TMP/ff/ffmpeg" "$BIN/ffmpeg"
  install -m 755 "$TMP/ff/ffprobe" "$BIN/ffprobe"
  ff_ok || fail ffmpeg "ffmpeg does not run on this Mac"
fi
say "ffmpeg: ready"

# 5. The plugin from the public store
if ! claude plugin marketplace list 2>/dev/null | grep -q "krol"; then
  claude plugin marketplace add alexeykrol/plugins >/dev/null 2>&1 || fail plugin "could not add the plugin store"
else
  claude plugin marketplace update krol >/dev/null 2>&1 || true
fi
if claude plugin list 2>/dev/null | grep -q "ai-movie-company@krol"; then
  claude plugin update ai-movie-company@krol >/dev/null 2>&1 || true
else
  claude plugin install ai-movie-company@krol >/dev/null 2>&1 || fail plugin "could not install the plugin"
fi
say "plugin: $(claude plugin list 2>/dev/null | grep -A2 'ai-movie-company@krol' | grep -i version | head -1 | xargs)"

echo "STATUS: OK"
