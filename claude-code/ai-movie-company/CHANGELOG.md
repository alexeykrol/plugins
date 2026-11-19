# Changelog

All notable changes to ai-movie-company. Versions follow [semver](https://semver.org/).

## 0.6.3 — 2026-11-19
- **The director walks newcomers through every click.** New `/ai-movie-company:connect` (and the
  "Connect now" choice in the guided start): a test channel first (how to create one), the Google
  client file, the three Google login screens ("Google hasn't verified this app" → Advanced → Go
  to…), access_denied, switching channels, the weekly re-login — one small step per message,
  button words as on screen, a screenshot whenever the person hesitates.
- `vp youtube client-find` / `client-install <path>`: the plugin finds the downloaded Google client
  file in Downloads / Desktop / Documents and puts it in place itself (owner-only permissions, its
  secret never printed); a "Web application" client is refused in plain words. The login says what
  to do when the file is missing instead of failing.
- Contract: **critical about the work, always for the person** — exact, even severe remarks about
  fragments and tasks always come with the next step; never a verdict on the person. A step the
  person got through gets a warm, specific word. New section "When the person does something by
  hand".
- README: a ready prompt to paste into Claude when stuck on any installation step; the director's
  help page on connecting YouTube rewritten.

## 0.6.2 — 2026-11-19
- **Any channel, a test channel first.** The channel is whichever the person signs in to; the
  director advises starting on a separate test channel without a real audience and switching when
  the result suits. A new login switches the channel cleanly: its @handle replaces the old one as
  the expected handle and on the plate (a plate text the user wrote is kept). README «Connecting
  YouTube» explains how to create a test channel and how to switch.

## 0.6.1 — 2026-11-19
- **Public release.** MIT license; published in the public store `alexeykrol/plugins`
  (`claude plugin marketplace add alexeykrol/plugins`, `claude plugin install ai-movie-company@krol`).
  No GitHub account is needed to install.
- **A new user gets their own channel, not the author's.** No channel handle and no plate on the
  videos by default; after the YouTube login the channel's own @handle becomes the plate and the
  expected handle (only where they are empty — a value you set is kept).
- **Connecting YouTube without editing settings**: put the Google client file at
  `~/.ai_video_pipeline/youtube_client_secret.json`; the login token is kept next to it. README
  «Connecting YouTube» walks through it; `doctor` names the place.
- `doctor` from the plugin checks the session's own judge (no separate `claude` needed).

## 0.6.0 — 2026-11-18
The plugin is now a person: the **editing director** («Режиссёр монтажа»). Same engine, same gates.
- **Voice.** One contract page (`persona/contract.md`) is the single source for every touchpoint:
  the first message, the request, the fragment card, the reject reason, the refusal to decide for
  the user, waiting, coming back, a miss and its repair, the ready videos. First person, a point of
  view with the weak spot named, no flattery; he says plainly that he works from the transcript and
  has not seen the picture. The engine's rules (gates, refusals, boundaries) stand above the voice.
- **Face — a mod inside the plugin** (`hooks/register.tsx`). While the director is on stage: his
  name and picture where a reply opens, a band above the prompt with a **Leave** button, his own
  words in the progress indicator, and the "Cutting room" colours in Claude Desktop and the mobile
  app. He comes on stage with any conversational command of the plugin (`start`, `run`, `review`,
  `approve`, `status`, `help`) or with `/director`, and only in that session; `doctor` and `learn`
  stay plain. `/director-off` or the Leave button gives plain Claude back.
  `AI_MOVIE_COMPANY_DIRECTOR=off` switches the face and the voice off (for clients' scripts).
  The interface words are Russian or English: the system language the first time, then the
  language of the conversation.
- **The director's task reaches the selection.** The guided start no longer interviews: it reads
  the beginning of the subtitles, proposes the name and the kind, and asks one open question — what
  you want from this recording. The answer is written as `brief:` in `request.yaml` (plain text, at
  most 2000 characters) and becomes its own section of the selection prompt. A job without a brief
  gets a byte-identical prompt; changing the brief re-runs the selection only.
- `help` answers in the director's own voice from the same pages, without naming them.
- Release: `persona/` and `types/` ship with the plugin; `scripts/release.sh` runs the mod's tests
  on the built release (`scripts/test_mod.sh`, 11 tests on four surfaces).
- Not yet: memory of the person between sessions, and "eyes" (the director does not see frames).
  Mods are an early-access part of Claude Code; tested on 2.1.325.

## 0.5.3 — 2026-11-17
- The fragment card says the finished video is a little longer (title and end cards).

## 0.5.2 — 2026-11-17
- From the second round of synthetic users (ideal, confused, provocateur, curious — all passed,
  no blockers): the "ready" message ends with the publish question (a rehearsal opens the video
  folders instead of asking); always the user's language, refusals included; side questions answered
  first and never dropped before a long step; a mistyped path is proposed, not substituted; a vague
  answer to "did you look through the whole recording?" is confirmed; no reading of other jobs.
- `vp review close` requires an explicit `--full-scan` / `--no-full-scan`: the template default is
  not the user's answer.

## 0.5.1 — 2026-11-17
- The human-answer guard counts only the answers given in the current Claude session
  (`CLAUDE_CODE_SESSION_ID`); a message typed in another chat no longer unlocks a decision.

## 0.5.0 — 2026-11-17
- Decisions belong to the director, enforced by the engine: plugin hooks record WHEN the user last
  answered (never what); `vp review set|close` and `vp approve set` refuse with
  `REFUSED user-input-required <id>` (exit 4) when nobody answered after the item was shown.
- Dialog rules from four synthetic users (ideal, confused, provocateur): delegating phrases are not
  decisions; the plugin never touches the source recording, settings, secrets or its decision files;
  one "ready" message with the table and one publish question; side questions answered first.
- YouTube connection: `authorize` records the channel itself; `access_denied` (Google app in
  Testing mode) and expired tokens are explained in plain words (U11, U12).
- Selection: fragments start on a whole thought, not on «Но…» or a sentence tail; the job's length
  range is the only one in the prompt.

## 0.4.0 — 2026-11-17
- Real YouTube is the default (`publisher: youtube`); a rehearsal is chosen per job
  (`publisher: fake` in `request.yaml`). `vp youtube status`; doctor says what to connect.
- New end of a run: everything uploaded as private drafts, one browser tab per video, one question
  (publish all / schedule / one by one / not now). The final message is the list of videos and
  links (`vp results`), not a report.
- Rehearsal is unmistakable: `fake://` links, a «РЕПЕТИЦИЯ» banner in `report.md`, local files.
- YouTube quota: caption re-downloads removed (12099 → 8898 units for a 4-video job); a quota check
  before uploads stops with `WAITING_QUOTA` instead of failing half-way.
- Progress: `progress.json` from ffmpeg and uploads, `vp progress [--wait N]`; the dialog posts a
  progress line about every minute.
- Guided start reworked (mentor tone, finds candidate folders, path hint, YouTube connection, plain
  words, one message per turn); previews open automatically; `/ai-movie-company:help` with a Russian
  help corpus.

## 0.3.3 — 2026-11-17
- README / README.ru: step-by-step installation from scratch on a clean Mac (GitHub access,
  Homebrew, ffmpeg, Python, Claude Code CLI, plugin, doctor) and how to update.

## 0.3.2 — 2026-11-17
- README / README.ru: quick start for first-time users.

## 0.3.1 — 2026-11-17
- `start` asks how many fragments are wanted ("at most N", e.g. one for a test) and maps it to
  the selection caps; warns about the cost of long recordings.

## 0.3.0 — 2026-11-17
- `/ai-movie-company:start [path]` — guided start: road map, environment check, finds the recording
  and subtitles, writes `request.yaml` from a short dialog, then runs the job. `run` without a job
  folder (or without `request.yaml`) goes the same way.
- Placeholder icon (`.claude-plugin/icon.png`).

## 0.2.0 — 2026-11-17
- Packaged as the Claude Code plugin `ai-movie-company`, installable privately from this
  repository as its own marketplace (`alexeykrol-private`); release folder `dist/ai-movie-company`
  built by an allowlist script with validation.
- Both human gates in the dialog: fragment review (`review show|preview|set|add|close`) and publish
  approval (`approve show|set`); the engine validates every decision exactly like the file path,
  records review time and the package hash the user saw, and refuses a decision on a changed package.
- Commands renamed: `/ai-movie-company:run`, `status`, `review`, `approve`, `learn`, `doctor`.
  Skills: `pipeline`, `judge`.
- Judgment inside the plugin session is done by the session itself (`VP_JUDGE=manual`).
- New launcher `scripts/vp` (no top-level `bin/`): finds Python ≥ 3.12, creates a private venv in
  the plugin data folder, installs the hash-pinned lock, rebuilds it when the lock changes.
- `requirements.lock` now has wheel hashes for CPython 3.12/3.13/3.14 on macOS arm64 and Linux
  x86_64/aarch64 (same versions as the 2026-11-17 supply-chain audit).
- `doctor` command: Python, venv, packages, ffmpeg filters, VP_HOME, publisher/notifier/judge settings.
- Default notifier is `session` (log only; the session talks to the user). Telegram stays optional.

## 0.1.0 — 2026-11-17
- Engine v1: steps S0–S11, learning loop S3c, approval and publishing gates, YouTube adapter,
  fake publisher, file and Telegram notifiers; first draft of the plugin layer.
