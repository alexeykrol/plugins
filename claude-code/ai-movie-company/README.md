[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](README.ru.md)

# AI Movie Company

**A video production team inside Claude Code.** Give it a recorded lecture or interview with
subtitles — it returns a reviewed series for YouTube: reels and long fragments with burned-in
captions, intro and outro cards, thumbnails, titles, descriptions with chapters and cross-links.
Everything is uploaded as **private** drafts; nothing is published until you approve each video
in the chat.

You do not operate a tool: you talk to **Elsa, the editing director** — a professional with a point of
view who finds the recording, agrees the task with you, proposes fragments and says where her own
proposal is weak. The decisions and the channel stay yours.

## Quick start (first time)

1. Install the plugin once (see Installation — one prompt, below), then open **Claude Desktop → Code** and start a
   local session in any folder. Restart the app if it was open during the install.
2. Put your recording in a folder together with its subtitles: a video (`.mp4`, `.mov`, `.mkv`) and
   an `.srt` file. The plugin does not transcribe: no `.srt` — no run.
3. Type `/ai-movie-company:start`.
4. The editing director looks for your recordings herself and asks which one to take. She reads
   the beginning of the subtitles, proposes a name for the series, and asks **one open question:
   what you want from this recording** — who the videos are for, what to look for, what to avoid.
   Your answer is the task the fragments are picked for; "make something" gives the average.
   Lengths, the number of fragments ("one" is fine for a test) and names to spell right — only if
   you bring them up. Then she says in two lines what will be made and asks «Запускаем?».
5. Wait. She proofreads the subtitles and picks fragments; on a long recording this takes a while.
6. **Review the fragments**: for each one you get a card and a preview video to open. Answer
   Accept / Reject / Adjust (say in words where it should start or end).
7. Claude cuts, renders, makes thumbnails, titles and descriptions.
8. **Watch and approve**: everything is uploaded to YouTube as private drafts (titles,
   descriptions, thumbnails, subtitles, playlist) and Claude opens a browser tab per video. Watch
   them there and answer in the chat: publish all, schedule, go one by one, or "not now" (they
   stay private). Nothing becomes public without your word.

YouTube is connected once: you sign in to your Google account; the director offers it at the start. Not connected or just trying? Choose a rehearsal for that job: nothing is
uploaded, you get local video files. The plugin's YouTube access gives about 10,000 API units a day, shared by all its users — roughly one job
with a few videos (a video costs ~2,200 units); the plugin checks this before uploading.

Stopped halfway? Open a new session and type `/ai-movie-company:run <the same folder>`: it continues
where it stopped. `/ai-movie-company:status <folder>` shows where a job is.

## The editing director

While you work with the plugin, Claude speaks as its editing director, Elsa («Эльза»), and the
interface shows it, so you always know whom you are talking to:

- her **name and picture** where each reply opens;
- a **band above the prompt** with her name and a **Leave** button;
- her own words in the progress indicator («Думаю над монтажом»);
- the **"Cutting room" colours** of the whole window (Claude Desktop and the mobile app).

She comes on stage when you type any conversational command of the plugin (`start`, `run`, `review`,
`approve`, `status`, `help`, `connect`) or `/director`, and **only in that session**: other sessions and
projects stay plain Claude. The technical commands (`doctor`, `learn`) do not call her. To get plain
Claude back, press **Leave** or type `/director-off`; the colours go back to yours.

What is drawn where: Claude Desktop — everything; the terminal — the name and the band (no picture,
no colours); VS Code — the name and picture and a status line (no band, no colours). The words on
the interface are Russian or English: your system language the first time, then the language you
and the director actually talk in.

The voice never outranks the rules: she still publishes only what you watched and never decides for
you. She says plainly what she has not seen (she works from the transcript, not from the picture) and
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
   designs thumbnails, writes metadata and chapters. Black bars of the recording are cropped away,
   and every video is checked twice before upload: by code (bars, size, black or frozen frames,
   silence, loudness) and on a sheet of frames (face in frame, nothing personal on screen,
   readable subtitles). A failed check stops the video and is shown to you.
5. **Uploads private drafts** to YouTube, links the videos of the series to each other.
6. **You approve in the chat** — per video: *Publish now / Schedule / Reject*. Only approved
   videos are published, and only in the exact version you saw.

Model judgment (proofreading, selection, texts) is done by your Claude session itself; code does
all the bookkeeping (timecodes, files, uploads, checks).

**Optional design look.** Ask the director in plain words — "beautiful captions", "highlight the
words", "an intro card" — and he renders a sample of one fragment with
[Remotion](https://www.remotion.dev), shows it right in the chat and asks before switching: reels
get animated burned-in captions (the current word lights up) and drawn intro/outro cards; long
videos get the drawn cards only and keep their subtitles as a YouTube track. Needs Google Chrome;
a reel takes a minute or two instead of seconds. The cards and an optional transparent captions
track are also written as separate files for editors (`clips/<id>/layers/`, see `docs/LAYERS.md`
in the source repository). Remotion has its own license: free for individuals, companies of up to
3 people and non-profits; larger companies need a Remotion Company License
(remotion.dev/license). The plain look does not use Remotion at all.

## Commands

| Command | What it does |
|---|---|
| `/ai-movie-company:start [path]` | meet the editing director: she finds the recording, agrees the task with you, writes the request, runs the job |
| `/ai-movie-company:run <job>` | runs the pipeline to the end or to the next gate; conducts the gates in the dialog |
| `/ai-movie-company:review <job>` | fragment review (gate 1) |
| `/ai-movie-company:approve <job>` | publish approval (gate 2) |
| `/ai-movie-company:status <job>` | where the job stands |
| `/ai-movie-company:help [question]` | the director answers "how / why / what if" about her own work |
| `/ai-movie-company:connect` | connect YouTube (or switch channels) with the director, one small step at a time |
| `/director` | calls the editing director into this session |
| `/director-off` | she leaves: plain Claude and your own colours again |
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
- "Why did you take this fragment and not the one about pricing?" → she answers with her reason

## Requirements

- **Claude Code** (terminal) or the **Code tab of the Claude desktop app**, macOS or Linux.
  Not for claude.ai Chat or Cowork (the plugin works with local video files and ffmpeg).
  The director's face is a mod — an early-access part of Claude Code, tested on 2.1.325; update
  Claude Code and the desktop app if you do not see it.
- **Python 3.12 or newer** and **ffmpeg with libass and freetype** — the installer sets them up
  (see below). Intel Macs are not supported yet (no build of the pinned `cryptography`).
- A YouTube channel. Without connecting it, a job can run as a rehearsal (nothing is uploaded).
- Only for the optional design look: **Google Chrome** and **Node.js 18+**
  (nodejs.org); `/ai-movie-company:doctor` says if either is missing.

Run `/ai-movie-company:doctor` after installing: it shows what is missing.

## Installation — one prompt

You need: a Mac with an Apple processor (M1 or newer) and a Claude subscription with the **Claude
desktop app**. No Terminal, no passwords, no GitHub or Google Cloud accounts.

1. Open the **Claude** app, the **Code** tab, and start a new session in any folder (for example,
   Documents).
2. Paste this text and send it:

```
Install the AI Movie Company plugin for me. I am not a technical person; I have a Mac.
Run this command in the terminal:
curl -fsSL https://raw.githubusercontent.com/alexeykrol/plugins/main/claude-code/ai-movie-company/install.sh | bash
The last line of its output says what comes next:
- STATUS: OK — tell me to quit Claude completely (Cmd+Q), open it again, start a new session in
  the Code tab and type /ai-movie-company:start;
- STATUS: NEED_DEVTOOLS — an Apple window will appear: tell me to press "Install", wait for my
  "done" and run the command again;
- STATUS: FAILED — explain in plain words what happened and what I should do.
When you ask permission to run a command, tell me which button to press.
If something on the screen is unclear, I will send a screenshot — explain from it.
```

3. From here Claude does everything and tells you where to click. Five to ten minutes; if the Mac
   asks to install Apple's "developer tools", about ten more.

Screenshot on a Mac: Cmd+Shift+4, drag over the part you need — the picture lands on the desktop;
drag it into the chat.

**Update** — the same text again: the installer skips what is done and updates the plugin.

What gets installed (all into your own `~/.local/bin`, no password, every file checked against a
checksum): the Claude command line, Python 3.13 (via uv), ffmpeg 9.0.2 and the plugin itself from
the `alexeykrol/plugins` store. If you already have Python 3.12+ and ffmpeg with libass, the
installer leaves them alone.

If you live in a terminal: `claude plugin marketplace add alexeykrol/plugins`,
`claude plugin install ai-movie-company@krol`, then `/ai-movie-company:doctor`.

## Connecting YouTube (once)

Type `/ai-movie-company:connect` (or choose "Connect now" when the director asks). Two steps: pick
the channel and sign in to your Google account. Nothing to download or set up: the program's
access to Google ships inside the plugin.

At sign-in Google shows "Google hasn't verified this app" — normal for a small program Google has
not reviewed yet: "Advanced" → "Go to … (unsafe)", then tick the permissions and "Continue". The
director shows these screens in advance and helps from a screenshot.

**Start on a test channel.** The channel is whichever one you sign in to, any of yours. We advise
not to start on a channel with a real audience: create a small separate channel (the director shows
how), do the first jobs there and switch when you like the result. Uploads are private drafts
anyway. **To switch channels**, ask the director to connect another one: the new login replaces
the old.

The plugin never sees or stores your password: Google gives it a separate access key to your
channel, and that key stays only on your computer (`~/.ai_video_pipeline/`). Until YouTube is
connected, `doctor` shows the `youtube` lines as FAIL — expected, and a rehearsal works without
them. Advanced: your own Google client and other settings live in `~/.ai_video_pipeline/config.yaml`
(keys `youtube:` and `brand:`).

## What it writes to disk

- **The job folder** you point it at: proofread subtitles, `script.md`, fragment lists, review and
  approval files, rendered clips, thumbnails, metadata, `report.md`, a status journal. Source files
  are never modified.
- **CapCut's drafts folder** (`~/Movies/CapCut/User Data/Projects/com.lveditor.draft`), only when
  you ask for a CapCut project (`vp capcut export <job> [clip ...]`, CapCut closed): a NEW project
  folder per clip (a copy of the cut — CapCut is sandboxed and cannot read the job folder — on a
  video track, one editable text segment per subtitle) and a line in
  CapCut's project list (`root_meta_info.json`, backed up first to
  `~/.ai_video_pipeline/capcut/backups/`). Existing CapCut projects are never touched. The CapCut
  draft format is undocumented; the writer follows CapCut 9.8.0 for macOS.
- **A rough cut for DaVinci Resolve / Final Cut Pro**, only when you ask for one
  (`vp resolve export <job> [clip ...]`): a NEW folder `<job>/exports/edit/<clip>/` with an FCPXML 1.10
  timeline (intro card, cut, outro card; card texts as editable titles), the subtitles as `.srt` and
  the card backgrounds. It refers to the cut in the job folder; nothing is copied elsewhere. Import
  in Resolve: File → Import → Timeline…, then File → Import → Subtitle…. Final Cut Pro is untested.
- **`~/.ai_video_pipeline/`** (or `VP_HOME`): `config.yaml`, the selection knowledge (versions,
  your verdicts), cost and notification journals; with the design look also `remotion/` (the
  design tools, ~500 MB) and, in the job folder, `design.yaml`, `previews/design/` and
  `clips/<id>/layers/`.
- **The plugin data folder** (`${CLAUDE_PLUGIN_DATA}`, else `~/.ai_video_pipeline/runtime`): a
  Python virtual environment with the pinned packages. Deleted when you uninstall the plugin.
- **The plugin's own store** (a JSON file under your Claude Code configuration folder), written by
  the director's face: the ids of the sessions where she is on stage (at most 50) and the language
  of the interface words (`ru` / `en`). Nothing about you or your recordings. To pick that
  language the first time, it reads your system language once (on a Mac:
  `defaults read -g AppleLanguages`; elsewhere the `LANG` / `LC_*` variables).

What the director's face changes in a session, and nothing else: it adds the contract page to the
text of the plugin's own commands and to the system prompt while she is on stage; draws her name,
picture, band and progress words; sets the window colours; and, when you press Leave, adds one
hidden note to the conversation saying that she left. Your `brief` is kept in the job folder.

## What it sends to the network

- **At install**: claude.ai (the Claude command line), github.com (uv and Python 3.13),
  ffmpeg.martin-riedl.de (ffmpeg) and the `alexeykrol/plugins` store on GitHub — only what you do
  not have yet; every file is checked against a checksum.
- **PyPI** (pypi.org), on the first run only: downloads the pinned, hash-checked packages listed
  in `requirements.lock`.
- **npm registry** (registry.npmjs.org), only the first time you ask for the design look: the
  pinned Remotion packages from `remotion/package-lock.json` (integrity-checked, install scripts
  off) into `~/.ai_video_pipeline/remotion/`. Remotion's own telemetry is switched off and its
  local servers listen on 127.0.0.1 only.
- **YouTube Data API** (Google), only with `publisher: youtube`: uploads private videos,
  captions, thumbnails, playlists; publishes what you approved.
- **Telegram Bot API**, only if you switch `notifier: telegram` on (off by default).
- Nothing else. No telemetry. Model judgment runs in your own Claude session.

## Support

support@alexeykrol.com
