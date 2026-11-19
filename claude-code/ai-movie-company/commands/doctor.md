---
description: Check that this machine is ready for the AI movie company pipeline (Python, venv, ffmpeg, config)
allowed-tools: Bash
---

Run `VP_JUDGE=manual CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/vp" doctor` and report
in the user's language. The first run may take a minute: it creates the plugin's Python
environment and installs pinned packages from PyPI (progress lines on stderr).

For every `FAIL` line give the fix in one line, e.g.: Python 3.12+ missing → `brew install
python@3.13`; ffmpeg missing or without the `subtitles` / `drawtext` filter → `brew install ffmpeg`
(Homebrew's build includes libass and freetype); YouTube lines → not connected yet: the README section «Connecting YouTube» (the Google client
file, then `/ai-movie-company:start` → Connect now); a rehearsal works without it. Never edit
`config.yaml` yourself. Never print, open or search for
credential files, and never print environment variable values.
