[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](README.ru.md)

# AI Movie Company

**A video production team inside Claude Code.** Give it a recorded lecture or interview with
subtitles — it returns a reviewed series for YouTube: reels and long fragments with burned-in
captions, intro and outro cards, thumbnails, titles, descriptions with chapters and cross-links.
Everything is uploaded as **private** drafts; nothing is published until you approve each video
in the chat.

You do not operate a tool: you talk to the **editing director** — a professional with a point of
view who finds the recording, agrees the task with you, proposes fragments and says where his own
proposal is weak. The decisions and the channel stay yours.

## Quick start (first time)

1. Install the plugin once (see Installation below), then open **Claude Desktop → Code** and start a
   local session in any folder. Restart the app if it was open during the install.
2. Put your recording in a folder together with its subtitles: a video (`.mp4`, `.mov`, `.mkv`) and
   an `.srt` file. The plugin does not transcribe: no `.srt` — no run.
3. Type `/ai-movie-company:start`.
4. The editing director looks for your recordings himself and asks which one to take. He reads
   the beginning of the subtitles, proposes a name for the series, and asks **one open question:
   what you want from this recording** — who the videos are for, what to look for, what to avoid.
   Your answer is the task the fragments are picked for; "make something" gives the average.
   Lengths, the number of fragments ("one" is fine for a test) and names to spell right — only if
   you bring them up. Then he says in two lines what will be made and asks «Запускаем?».
5. Wait. He proofreads the subtitles and picks fragments; on a long recording this takes a while.
6. **Review the fragments**: for each one you get a card and a preview video to open. Answer
   Accept / Reject / Adjust (say in words where it should start or end).
7. Claude cuts, renders, makes thumbnails, titles and descriptions.
8. **Watch and approve**: everything is uploaded to YouTube as private drafts (titles,
   descriptions, thumbnails, subtitles, playlist) and Claude opens a browser tab per video. Watch
   them there and answer in the chat: publish all, schedule, go one by one, or "not now" (they
   stay private). Nothing becomes public without your word.

YouTube must be connected once (see **Connecting YouTube**; Claude offers the login at the start). Not connected or just trying? Choose a rehearsal for that job: nothing is
uploaded, you get local video files. YouTube gives about 10,000 API units a day — roughly one job
with a few videos (a video costs ~2,200 units); the plugin checks this before uploading.

Stopped halfway? Open a new session and type `/ai-movie-company:run <the same folder>`: it continues
where it stopped. `/ai-movie-company:status <folder>` shows where a job is.

## The editing director

While you work with the plugin, Claude speaks as its editing director («Режиссёр монтажа»), and the
interface shows it, so you always know whom you are talking to:

- his **name and picture** where each reply opens;
- a **band above the prompt** with his name and a **Leave** button;
- his own words in the progress indicator («Думаю над монтажом»);
- the **"Cutting room" colours** of the whole window (Claude Desktop and the mobile app).

He comes on stage when you type any conversational command of the plugin (`start`, `run`, `review`,
`approve`, `status`, `help`) or `/director`, and **only in that session**: other sessions and
projects stay plain Claude. The technical commands (`doctor`, `learn`) do not call him. To get plain
Claude back, press **Leave** or type `/director-off`; the colours go back to yours.

What is drawn where: Claude Desktop — everything; the terminal — the name and the band (no picture,
no colours); VS Code — the name and picture and a status line (no band, no colours). The words on
the interface are Russian or English: your system language the first time, then the language you
and the director actually talk in.

The voice never outranks the rules: he still publishes only what you watched and never decides for
you. He says plainly what he has not seen (he works from the transcript, not from the picture) and
does not claim to remember earlier sessions beyond what the job folder shows.

For scripts and clients that call the plugin without a person, set `AI_MOVIE_COMPANY_DIRECTOR=off`:
no face, a plain neutral voice.

## What it does

1. **Proofreads the subtitles** and re-times them to the speech.
2. **Proposes fragments**: short reels (30–90 s) and long pieces (10–40 min), each with a working
   title, thesis and why it is strong.
3. **You review them in the chat** — one card per fragment, buttons *Accept / Reject / Adjust
   boundaries*, a low-res preview you can open locally. Your verdicts teach the selection: the
   plugin accumulates criteria, rules and cases (no model fine-tuning).
4. **Renders** the clips (vertical crop for reels, captions, cards, loudness normalisation),
   designs thumbnails, writes metadata and chapters.
5. **Uploads private drafts** to YouTube, links the videos of the series to each other.
6. **You approve in the chat** — per video: *Publish now / Schedule / Reject*. Only approved
   videos are published, and only in the exact version you saw.

Model judgment (proofreading, selection, texts) is done by your Claude session itself; code does
all the bookkeeping (timecodes, files, uploads, checks).

## Commands

| Command | What it does |
|---|---|
| `/ai-movie-company:start [path]` | meet the editing director: he finds the recording, agrees the task with you, writes the request, runs the job |
| `/ai-movie-company:run <job>` | runs the pipeline to the end or to the next gate; conducts the gates in the dialog |
| `/ai-movie-company:review <job>` | fragment review (gate 1) |
| `/ai-movie-company:approve <job>` | publish approval (gate 2) |
| `/ai-movie-company:status <job>` | where the job stands |
| `/ai-movie-company:help [question]` | the director answers "how / why / what if" about his own work |
| `/director` | calls the editing director into this session |
| `/director-off` | he leaves: plain Claude and your own colours again |
| `/ai-movie-company:learn <subcommand>` | learning loop of the fragment selection |
| `/ai-movie-company:doctor` | checks Python, ffmpeg and settings |

A **job** is a folder with `request.yaml` and the sources: `_cut.mp4` (video), `_cut.srt`
(subtitles) and optionally `_cut.json` (word timings). Example `request.yaml`:

```yaml
enabled: true
platforms: [youtube]
profile: lecture
title: "Agents in production — lecture"
brief: "For people who are just starting with agents: places where the speaker explains on an example. No inside jokes."
```

`brief` is optional: the director's task in plain words (at most 2000 characters). It reaches the
selection of fragments; changing it re-runs the selection only.

## Examples

- `/ai-movie-company:run ~/Videos/2026-11-17-agents` — "make reels from this lecture"
- "Show me the fragments of yesterday's lecture one by one" → the review in the chat
- "Which videos of the agents series are waiting for my approval?" → `/ai-movie-company:approve`
- "Why did you take this fragment and not the one about pricing?" → he answers with his reason

## Requirements

- **Claude Code** (terminal) or the **Code tab of the Claude desktop app**, macOS or Linux.
  Not for claude.ai Chat or Cowork (the plugin works with local video files and ffmpeg).
  The director's face is a mod — an early-access part of Claude Code, tested on 2.1.325; update
  Claude Code and the desktop app if you do not see it.
- **Python 3.12 or newer** (`python3.12`/`3.13`/`3.14` on PATH). Supported wheels: macOS arm64,
  Linux x86_64 and aarch64. Intel Macs are not supported yet (no `cryptography` wheel at the pinned version).
- **ffmpeg with libass and freetype** (`subtitles` and `drawtext` filters). macOS: `brew install ffmpeg`.
- A YouTube channel and a Google OAuth client (YouTube Data API v3) — publishing to YouTube is
  the normal mode. Without them you can still run a rehearsal per job (nothing is uploaded).

Run `/ai-movie-company:doctor` after installing: it shows what is missing.

## Installation from scratch (Mac with Apple silicon)

You need: a Claude subscription with the **Claude desktop app** and about 20 minutes. No GitHub
account is needed. All commands go into the **Terminal** app (Applications → Utilities →
Terminal); paste one line, press Enter, wait.

1. **Homebrew** (the installer for the tools below), if `brew --version` says "command not found":
   ```
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
   It asks for your Mac password. At the end it prints two or three lines under "Next steps" —
   copy and run them, then close and reopen Terminal.
2. **Tools**: video tools and Python:
   ```
   brew install ffmpeg python@3.13
   ```
3. **Claude Code command line** (the desktop app has its own; this one installs the plugin):
   ```
   curl -fsSL https://claude.ai/install.sh | bash
   ```
   Close and reopen Terminal afterwards.
4. **The plugin**:
   ```
   claude plugin marketplace add alexeykrol/plugins
   claude plugin install ai-movie-company@krol
   ```
   Both lines end with "Successfully …".
5. **Check**: quit the Claude desktop app completely (Cmd+Q) and open it again, go to **Code**,
   start a local session in any folder and type `/ai-movie-company:doctor`. The first time it takes
   a minute (it prepares its own Python environment). Everything except the `youtube` lines must be
   OK; for those see **Connecting YouTube** below.

Then go to **Quick start** above.

**Updating** to a new version (Terminal):
```
claude plugin marketplace update krol
claude plugin update ai-movie-company@krol
```
then restart the desktop app.

## Installation (if you already have Python 3.12+ and ffmpeg)

```
/plugin marketplace add alexeykrol/plugins
/plugin install ai-movie-company@krol
```

or from a terminal: `claude plugin marketplace add alexeykrol/plugins` and
`claude plugin install ai-movie-company@krol`. Restart the session, then run
`/ai-movie-company:doctor`.

## Connecting YouTube (once)

Until YouTube is connected, `doctor` shows three `youtube` lines as FAIL — that is expected, and a
rehearsal works without them (nothing is uploaded, you get local video files).

1. **The Google client file.** Uploading goes through a Google Cloud "OAuth client" of type
   *Desktop app* — a small `client_secret_….json` file. Get it from whoever set the plugin up for
   you, or make your own: [Google Cloud Console](https://console.cloud.google.com/) → new project →
   APIs & Services → enable **YouTube Data API v3** → Google Auth Platform → Clients → Create
   client → *Desktop app* → Download JSON.
2. **Put it here** under exactly this name (Finder: Go → Go to Folder…, paste the path; the folder
   appears after the first `doctor`):
   ```
   ~/.ai_video_pipeline/youtube_client_secret.json
   ```
3. **Allow your Google account.** While that Google project is in *Testing* mode, only its test users
   can log in: its owner adds the Google account of your channel under Google Auth Platform →
   Audience → Test users. Otherwise Google says "Access blocked … 403: access_denied" — the plugin
   then explains the same.
4. **Log in**: type `/ai-movie-company:start`; when the director asks about YouTube choose
   **Connect now**. A browser opens: sign in to the Google account of the channel you want (pick
   the channel if Google asks) and allow access. The plugin remembers the channel and puts its name
   (@handle) on your videos.

**Start on a test channel.** The channel is whichever you sign in to — any of yours. We recommend
not starting on the channel with your real audience: create a small separate channel (YouTube →
your avatar → Settings → Add or manage your channels → Create a channel), run the first jobs there,
and switch when the result suits you. Uploads are private drafts in any case, but a test channel
keeps the real one's library and playlists clean. **To switch** — ask the director to connect
another channel (or `/ai-movie-company:start` → Connect now) and sign in to the other one: its
@handle replaces the old one on the next videos.

While the Google project is in Testing mode, Google ends the login after about 7 days; the plugin
then says so and `/ai-movie-company:start` → Connect now logs in again. Secrets stay in your files;
the plugin never prints them. Advanced: other paths and settings live in
`~/.ai_video_pipeline/config.yaml` (`youtube:` and `brand:` keys).

## What it writes to disk

- **The job folder** you point it at: proofread subtitles, `script.md`, fragment lists, review and
  approval files, rendered clips, thumbnails, metadata, `report.md`, a status journal. Source files
  are never modified.
- **`~/.ai_video_pipeline/`** (or `VP_HOME`): `config.yaml`, the selection knowledge (versions,
  your verdicts), cost and notification journals.
- **The plugin data folder** (`${CLAUDE_PLUGIN_DATA}`, else `~/.ai_video_pipeline/runtime`): a
  Python virtual environment with the pinned packages. Deleted when you uninstall the plugin.
- **The plugin's own store** (a JSON file under your Claude Code configuration folder), written by
  the director's face: the ids of the sessions where he is on stage (at most 50) and the language
  of the interface words (`ru` / `en`). Nothing about you or your recordings. To pick that
  language the first time, it reads your system language once (on a Mac:
  `defaults read -g AppleLanguages`; elsewhere the `LANG` / `LC_*` variables).

What the director's face changes in a session, and nothing else: it adds the contract page to the
text of the plugin's own commands and to the system prompt while he is on stage; draws his name,
picture, band and progress words; sets the window colours; and, when you press Leave, adds one
hidden note to the conversation saying that he left. Your `brief` is kept in the job folder.

## What it sends to the network

- **PyPI** (pypi.org), on the first run only: downloads the pinned, hash-checked packages listed
  in `requirements.lock`.
- **YouTube Data API** (Google), only with `publisher: youtube`: uploads private videos,
  captions, thumbnails, playlists; publishes what you approved.
- **Telegram Bot API**, only if you switch `notifier: telegram` on (off by default).
- Nothing else. No telemetry. Model judgment runs in your own Claude session.

## Support

support@alexeykrol.com
