---
description: Check that this machine is ready for the AI movie company pipeline (Python, venv, ffmpeg, config)
allowed-tools: Bash
---

Run `VP_JUDGE=manual CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/vp" doctor` and report
in the user's language. The first run may take a minute: it creates the plugin's Python
environment and installs pinned packages from PyPI (progress lines on stderr).

For every `FAIL` line give the fix in one line, e.g.: Python 3.12+ or ffmpeg (with the `subtitles` /
`drawtext` filters) missing → offer to run the plugin's installer again (`curl -fsSL
https://raw.githubusercontent.com/alexeykrol/plugins/main/claude-code/ai-movie-company/install.sh |
bash`; no password; it skips what is already there); YouTube lines → not connected yet, which is normal before the first connection: offer
`/ai-movie-company:connect` — the director walks through it one step at a time; a rehearsal works
without it. Never edit
`config.yaml` yourself. Never print, open or search for
credential files, and never print environment variable values.
