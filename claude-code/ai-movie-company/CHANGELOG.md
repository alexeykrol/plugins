# Changelog

All notable changes to ai-movie-company. Versions follow [semver](https://semver.org/).

## 0.8.7 — 2026-11-21

- **You always see that Elsa is working.** While she thinks: a live bar and «Эльза думает · 1:42»
  with the time running. While work she handed off runs in the background after her reply:
  «Эльза работает в фоне: 2 задачи · 4:10» until it ends. Engine steps keep their percent bar.
- **One CapCut project per job.** Every new version is a new timeline in the same project; earlier
  timelines and your edits in them stay untouched. Every layer on its own track (picture, editable
  subtitles, voice phrase by phrase, music). Cards keep their shape; transitions, filters and
  animations load from CapCut's library. A backup before every write; nothing is written while
  CapCut is open.
- **Text to video, honest guidance.** `vp text render` renders the job's own animation with the
  engine's tools (no blocked commands); loudness is evened to −14 LUFS; a smaller copy is made when
  the video is too big for the chat; the music estimate is correct (about 15 credits per second);
  stock is allowed when you have a stock key.
- **Results always in the chat.** Elsa sends the video unasked, never only a path, and never says
  something "is open on the right" unless it really opened.
- **Voice choice matches.** Elsa lets you hear three voices and shows exactly those three as
  buttons, plus «Другие варианты».
- **Failures are diagnosable.** A failed command reports its error class (never its text).

## 0.8.6 — 2026-11-21

- **Alpha terms and privacy (version `2026-11-21-alpha1`).** Short in-plugin terms now match the
  public pages alexeykrol.com/elsa/terms/ and /elsa/privacy/: an alpha "as is", at your own risk;
  the only thing we try to do is keep it safe and private as far as we can. Everyone accepts the new
  version once.
- **Nothing leaves your Mac before you agree.** Telemetry starts only after the terms are accepted;
  the installer sends nothing. What is sent is built from a fixed list of technical fields
  (commands, steps, error classes, timings, costs, versions) — no paths, user names, error texts or
  Elsa's notes. Elsa's notes go only into the feedback pack you send yourself.
- **`vp telemetry show | off | on | status`.** See exactly what would be sent; turn it off any time.
- **`vp youtube disconnect`.** Revokes the plugin's Google access and deletes the local token.
  The plugin now asks Google for two YouTube permissions instead of three; existing logins keep
  working.
- **The prompt journal covers only sessions with Elsa** and still never leaves your Mac.

## 0.8.5 — 2026-11-20

- **Elsa restarts Claude and closes CapCut herself.** She never asks you to press ⌘Q any more (it
  quits whatever window is in front — often the browser). When a restart is needed she asks with
  two buttons, «Перезапустить Claude» / «Позже», closes Claude, opens it again and greets you back.
  She refuses while a render or an upload is running.
- **Install ends by itself.** After installing, Claude closes for a few seconds and opens a new tab
  with «Эльза» already typed in — press Enter. Running the same install text over an existing
  install updates it: projects and their progress stay, and the new tab asks Elsa to continue the
  project from where you stopped instead of starting over.
- **Steps outside the chat come as a framed card.** When you have to do something by hand (copy a
  key on a website, for example), a card above the prompt says exactly what to do, what not to do,
  and asks «Понятно, что сделать?», with a «Не понял(а), покажите» button.
- **Stock, pictures and keys.** Pexels, Pixabay, Gemini and OpenAI keys are taken from the
  clipboard, like ElevenLabs; Elsa searches stock footage and makes pictures with your own key.
- **Scenario map.** Elsa knows which starting points (recording, text, stock, storyboard…) work with
  no services, which need ElevenLabs or a pictures key, and roughly what each costs.
- **Scene and beat analysis, montage plan, take cleanup** built into the engine.
- **One ElevenLabs key for everything, connected once.** Speech recognition, the voice-over and
  the music now all use your own ElevenLabs key, copied once from the ElevenLabs site — no
  connector to add and no Claude restart. Elsa plays four different voices, you pick one, and
  every scene is voiced straight into the job folder with exact word timings; the music is made the
  same way (paid ElevenLabs plans; on the free plan Elsa says so before anything is charged). A key
  saved earlier for recognition keeps working. The terms say so (version `2026-11-21-draft`).

## 0.8.4 — 2026-11-20

- **Documents to approve open beside the chat.** When Elsa asks you to approve a brief or another
  document, it opens in a panel to the right of the chat, with «Утверждаю» / «Хочу поправить»
  buttons right under the text; pressing one answers Elsa. Where no panel can be shown, the document
  is pasted into the chat instead.
- **Every set of buttons says to press one.** A large line «Нажмите одну из кнопок ниже» comes right
  before any choice.
- **Install from the top of the README.** One sentence and one block to paste into Claude; the block
  itself guides a person from Chat or Cowork to Code.

## 0.8.3 — 2026-11-19

- **The guard fails closed.** If the guard itself breaks or runs too long, install-shaped commands
  are blocked with a plain reason and the way out (`AIMC_GUARD=off`); ordinary commands go
  through, so a broken guard never locks up the session. It works in every session where the
  plugin is enabled.
- **Terms gate headless uploads too.** A real upload or publish without accepted terms (also from
  clients like ai_lms) refuses with `REFUSED consent-required` (exit code 5) and sends nothing;
  rehearsals are not affected.
- **Security updates by exception to the 14-day hold:** Node 24.22.3 and Chrome Headless Shell
  158.0.8127.36, verified against official checksums, a real Remotion frame and video rendered;
  the exception is recorded in the audit with the owner's approval.

## 0.8.2 — 2026-11-19

Safety on the person's computer.

- **Audited install path.** Everything the install brings onto the Mac (328 items: programs,
  Python and npm packages, the Remotion plugin, the store) was checked on 2026-11-19; nothing is
  rejected. A release is refused when the audit is older than 14 days or misses a pinned item.
  Downloads are https-only; a freshly installed `claude` must carry Anthropic's code signature.

- **Outside installs are stopped and vetted.** A hook stops any command that would bring
  outside code (npm/npx/pnpm/yarn/bun, pip/uv, brew, cargo, gem, go install, `claude plugin
  install`, `marketplace add`, `mcp add`, a download run by a shell). Elsa vets it with the new
  `/ai-movie-company:vet` skill and asks «Ставим» / «Не ставим»; a yes lets through exactly that
  command once, within 30 minutes. The plugin's own pinned installs pass. Off only by the
  person's explicit choice (`vp guard off`) or `AIMC_GUARD=off`.
- **Terms of use (draft).** `TERMS.md` / `TERMS.ru.md`; on first start Elsa shows a summary with
  «Принимаю» / «Прочитать полностью» / «Не сейчас». Until accepted, nothing is installed or
  published from the dialog (a rehearsal works).
- **Fewer permission questions, only in Elsa's folder.** If the session asks before every
  command, Elsa offers once to write `~/Movies/AI Movie Company/.claude/settings.json`
  (acceptEdits plus allow rules for her own engine); «верни как было» restores it byte for byte.
  The global settings and the mode switch are never touched.

## 0.8.1 — 2026-11-19

The install a newcomer barely sees (inventory: `docs/permissions-0.8.1.md`).

- **Apple's developer tools without a rerun.** The installer opens Apple's window once, waits and
  continues in the same run; if Apple is still installing, Claude reruns it by itself. Buttons
  appear only if the window was closed without installing. No macOS stub program (`git`,
  `python3`, …) is called before the tools exist, so the window never pops up by surprise.
- **Every remaining confirmation is announced first.** A "Permissions" section in the contract:
  Elsa says what the window is, that it is safe and normal, why it is needed, and that she
  continues by herself — for Claude's command cards, Apple's tools, folder access, Google sign-in
  and ElevenLabs.
- **Fewer folder prompts.** The feedback report goes to `~/Movies/AI Movie Company/Feedback`
  (not the Desktop) and Finder shows it; the YouTube connect step no longer looks into
  Downloads/Desktop/Documents; the session is suggested in Movies.
- **Silent Mac check.** macOS version and free disk are checked at the start against the real
  minimums of what we install; nothing is printed on a normal Mac.

## 0.8.0 — 2026-11-19

Ready for a newcomer's first run.

- **Short start.** `/elsa`, or a message that is just her name, starts the guided start. The first
  reply of a session opens with an animated welcome splash.
- **Projects.** Every job is a project in a registry (`vp projects list`); the start asks «new
  project or back to one?» with a clickable list.
- **Text to video.** `vp text new|status|estimate|timeline|approve`: brief → screenplay → storyboard
  sketches approved in the chat → ElevenLabs voice and music (length and credits estimated first,
  ≈14.8 characters per second) → Remotion animation → render → CapCut. `vp text status` always
  names the next step and its exact command.
- **CapCut versions with separate tracks.** `vp capcut <job> --version vN`: picture, editable
  subtitles, voice phrase by phrase, music on its own track rising in the voice pauses; every
  change is a new version next to the earlier ones, hand edits are never overwritten.
- **Picture check.** `vp check picture` (black or empty stretches, frozen frames, frame sheet)
  runs after every render before anything is shown.
- **Elsa's face and look.** Four moods with a moving face, a progress bar for every long turn
  (from the turn's task list, or a running bar with elapsed time), a warm session look.
- **Buttons, not «готово».** Every hand step (close CapCut, confirm in Google) is a card with
  buttons. Voice choice: three different voices plus «Другие варианты».
- **Everything installs at once.** The install prompt also sets up Node, Chrome for rendering, the
  Remotion project and the Remotion plugin for Claude; `vp extras status|install`, doctor checks
  them. Clean install measured: 3 min 6 s; a rerun 4 s. Intel Macs are not supported yet.
- **Feedback from a newcomer.** Every reply the person types is journaled per project; dead ends
  and surprises are recorded with context; `vp feedback pack` puts one redacted report on the
  Desktop to send to the owner. Elsa answers every question about the plugin herself.

## 0.7.1 — 2026-11-19

- **Elsa's band, alive.** The band above the prompt now carries Elsa's face — an animated portrait
  that blinks and moves a little — her name, role and the release (`v0.7.1 · 19.11.2026`), on a
  bright pastel that drifts slowly around the colour wheel (a full turn in three minutes, never a
  jump).
- **A big progress bar.** While the engine works, the band shows the step in plain words
  («Собираю ролики»), a bar, the percent and the time left. The engine mirrors the running step to
  `~/.ai_video_pipeline/progress-live.json`; the band reads it every two seconds and hides it when
  the run ends or dies.

## 0.7.0 — 2026-11-19

- **Editor exports** — CapCut project and FCPXML rough cut, see below.
- **Elsa.** The editing director is now Elsa («Эльза»): a woman of about forty who loves working
  with the person, warmer and livelier, with a very light, friendly irony (never at the person,
  none in errors, refusals or at the publish question). The band reads «Эльза (режиссёр
  монтажа) на связи». Persona card at the top of `persona/contract.md`.
- **Asks ledger.** Every request of the person is recorded per job (`asks.yaml` / readable
  `asks.md`) with a status and evidence; open ones are named before publishing and in the report.
- **Picture QA.** No reel ships with bars: the real picture area is measured, a code check
  (bars, size, black/frozen frames, silence) runs after cut and render, a frame check by the model
  (face, personal data, subtitles) before upload; a hard gate, calibrated on real Zoom recordings.
- **Remotion design path** (optional; needs Node and Chrome): animated reel captions, cards,
  cover, editor layers; falls back to the plain look when the tools are missing.
- **Quota over several days**: a job bigger than one day's YouTube quota uploads in parts instead
  of waiting forever.
- **Your own fragment may be short**: a reel the person picks may be down to 10 s.
- **Previews in the chat**, not in a separate player.

- CapCut export: `--opener <video>` puts a title clip before the cut in the same project — fitted into the cut's frame without stretching, silent track added when it has none, 0.8 s cross-dissolve baked in (`--dissolve`), cut and subtitles shifted to stay in sync.
- **CapCut project per clip.** `vp capcut export <job> [clip ...]` makes a NEW CapCut desktop
  project for each finished clip: the cut on a video track, the proofread subtitles as editable
  text segments (one per cue; the cut is copied into the project because CapCut is sandboxed),
  a 1080×1920 canvas for reels and 1920×1080 for long videos. Only new
  folders are created; CapCut's project list is backed up before the new entry is added; nothing is
  written while CapCut is running. The draft format is undocumented and follows CapCut 9.8.0 for
  macOS (`src/vp/adapters/capcut_template.json`, re-derived by
  `scripts/capcut_template_from_draft.py`). The clip→timeline model (`edit_timeline.py`) is
  editor-neutral, ready for a DaVinci Resolve (FCPXML) writer.
- **Rough cut for DaVinci Resolve / Final Cut Pro.** `vp resolve export <job> [clip ...]` (alias
  `vp fcpxml`) writes an FCPXML 1.10 timeline per finished clip into `<job>/exports/edit/<clip>/`:
  intro card, the cut with its audio, outro card on the spine; the card texts as editable Basic
  Titles; canvas and frame rate of the clip (1080×1920 for reels). Subtitles go next to it as an
  `.srt` on the same timeline (caption import from FCPXML differs between editors).
  `--baked-cards` takes the cards from `render.mp4` instead. Final Cut Pro untested (not installed).

## 0.6.4 — 2026-11-19
- **Install with one prompt.** A newcomer opens Claude Desktop → Code, pastes one text from the
  README, and Claude runs `install.sh`: no Terminal, no password, no Homebrew, no GitHub or Google
  Cloud account. The installer puts the Claude command line, Python 3.13 (uv 0.13.6), ffmpeg 9.0.2
  (static build with libass/freetype) and the plugin into `~/.local/bin` / the store, every download
  pinned and checked against a sha256 in the script; reruns skip what is done; the last line tells
  the assistant what to say next (`STATUS: OK | NEED_DEVTOOLS | FAILED <step>`). Intel Macs stop
  early with a clear message.
- **YouTube: sign in, nothing else.** The plugin carries its own Google desktop-app client
  (`assets/youtube_client.json`; the project is in production), so connecting is "pick a channel,
  sign in, Allow". A client of your own (in `~/.ai_video_pipeline/` or `config.yaml`) still wins.
  Login errors for the bundled client are explained without any Google Cloud words.
- The launcher and `doctor` find tools in `~/.local/bin`; fix hints point to the installer instead
  of Homebrew. `/ai-movie-company:connect` is two steps now (channel, sign-in).

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
