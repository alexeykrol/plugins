[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](README.ru.md)

# AI Movie Company

**How to install:** copy the text in the grey block below (the copy icon is at the block's top
right) and paste it into the Claude app on your Mac. Claude explains the rest by itself.

```
Install the AI Movie Company plugin for me. I am not a technical person; I have a Mac.
It can only be installed from the Code mode of the Claude app: there you run commands right on my Mac.
If you are in Chat or Cowork now and cannot run commands right on my Mac (your own sandbox does
not count), run nothing; explain to me simply, step by step: at the top left of the Claude window
there are tabs Chat, Cowork and Code — press Code (it needs a paid Claude plan, Pro or higher);
press "+ New"; paste this same text again (⌘V) and send it (Enter).
If you are in Code, first warn me in one sentence: you will ask permission to run commands — name
the button that allows it for good, and say not to press "Deny"; and the Mac may offer Apple's free
"developer tools" — press "Install", it is safe, and you carry on by yourself.
Then run (allow up to 10 minutes; running it again is safe, finished steps are kept), and while we
wait, write briefly that all is going well, so the window is not closed:
curl -fsSL https://raw.githubusercontent.com/alexeykrol/plugins/main/claude-code/ai-movie-company/install.sh | bash
The last line says what comes next:
- STATUS: OK — say in one line that Claude will now close for a few seconds and reopen with a new tab
  where "Elsa" is already typed — I just press Enter there; then run the command from the RESTART:
  line yourself. Don't ask me to quit anything. A line ADDONS: PARTIAL is not an error.
- STATUS: NEED_DEVTOOLS — Apple is still installing: tell me all is well and run the same command
  again yourself at once, without asking me; up to 5 times (about half an hour).
- STATUS: FAILED — explain in plain words and ask me with buttons; if it was the internet or a
  closed Apple window, run the command again. Never ask me to type or run anything.
```

Everything else on this page is for those who are curious.

**A video production team inside Claude Code.** Give it a recorded lecture or interview with
subtitles — it returns a reviewed series for YouTube: reels and long fragments with burned-in
captions, intro and outro cards, thumbnails, titles, descriptions with chapters and cross-links.
Everything is uploaded as **private** drafts; nothing is published until you approve each video
in the chat.

You do not operate a tool: you talk to **Elsa, the editing director** — a professional with a point of
view who finds the recording, agrees the task with you, proposes fragments and says where her own
proposal is weak. The decisions and the channel stay yours.

## Quick start (after the install)

Whatever did not work, Elsa writes down and puts together a report — one file you can forward to the author.

1. Open the Claude app → **Code**, start a new conversation.
2. For a video from a recording, put the recording in a folder together with its subtitles: a video
   (`.mp4`, `.mov`, `.mkv`) and an `.srt` file (the plugin does not transcribe: no `.srt` — no run).
   For a video from a text, the text is enough.
3. Write **"Elsa"** (or `/elsa`).
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
| `/ai-movie-company:vet` | checks outside code (a package, a plugin, a script) before it is installed — the guard calls it by itself |

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
- For the design look (animated captions, drawn cards, video from text) — the animation add-ons:
  Node.js, a Chrome to draw in, Remotion. The same installer sets them up; if something did not
  download, `/ai-movie-company:doctor` says what is missing and Elsa adds it when you say "install
  the add-ons".

Run `/ai-movie-company:doctor` after installing: it shows what is missing.

## Installation — details

How to install is at the top of this page ("How to install"). No Terminal, no passwords, no GitHub or
Google Cloud accounts.

**Update** — the same text again (or tell Elsa "update the plugin"): the installer skips what is
done and updates the plugin.

What gets installed (no password, every download checked against a checksum): into `~/.local/bin` —
the Claude command line, Python 3.13 (via uv), ffmpeg 9.0.2; the plugin itself from the
`alexeykrol/plugins` store and its engine; the animation add-ons into `~/.ai_video_pipeline` —
Node.js 24, Chrome Headless Shell (a Chrome without windows, for drawing video), the Remotion project
from a pinned list of versions — and the Remotion plugin for Claude from the Anthropic Directory.
Python 3.12+ and ffmpeg with libass that you already have are left alone.

If you live in a terminal: `claude plugin marketplace add alexeykrol/plugins`,
`claude plugin install ai-movie-company@krol`, then `/ai-movie-company:doctor`.

## Safety, terms and permission questions

**The install guard (on by default).** Wherever the plugin is enabled, a hook stops every command
that would bring outside code onto your computer — `npm`/`npx`/`pnpm`/`yarn`/`bun` installs,
`pip`/`pipx`/`uv`/`uvx`, `brew`, `cargo`, `gem`, `go install`, `claude plugin install`,
`claude plugin marketplace add`, `claude mcp add`, a script downloaded and piped into a shell or
run right after downloading, a macOS `installer -pkg` — whoever suggested it. Elsa says what it
was, vets it (exact name and version, a release at least 14 days old, fresh incident reports,
install scripts, look-alike names), tells you her verdict and asks **«Ставим» / «Не ставим»**.
Only after your own «Ставим» does that exact command run, once, within 30 minutes; any other
command is a new check. The plugin's own pinned installs pass without a stop: the installer from
this store, `vp extras install` and `npm ci --ignore-scripts` of its own lockfile. The guard
works in **every Claude session where the plugin is enabled** — any folder, any task, not only
while Elsa makes a video.

**If the guard itself breaks, it fails safe.** Should its check crash or take too long, a command
that looks like an install is stopped anyway (with a plain reason), while ordinary commands —
reading files, git, ffmpeg, the plugin's own engine — go through, so a guard bug never stops your
normal work. If it keeps failing, the way out is to quit Claude and start it with
`AIMC_GUARD=off` (Terminal: `AIMC_GUARD=off claude`); Elsa tells you this herself when it happens.

The guard also keeps its own records (your acceptance of the terms, approvals, its settings, the
record of your answers) out of reach of Claude's file tools. **To turn the guard off** — an
explicit choice only: tell Elsa "turn off the install check"; she asks with buttons and turns it
off only on your «Отключить проверку» (`vp guard off --request`, then `--confirm`); "turn the
check back on" → `vp guard on`. For developers: the environment variable `AIMC_GUARD=off` for the
process that runs Claude. What it does not catch: code you install outside Claude, a repository
cloned and built by hand, a program that downloads more code by itself after it was approved.

**The terms.** On the first start Elsa shows a short summary of [TERMS.md](TERMS.md) (Russian:
[TERMS.ru.md](TERMS.ru.md)) with three buttons: «Принимаю» / «Прочитать полностью» / «Не сейчас».
The version and date of your acceptance are stored only on your computer
(`~/.ai_video_pipeline/consent.json`). Until you accept, the plugin installs nothing and uploads
or publishes nothing (a rehearsal works). The terms are a **draft** until a lawyer has read them.

**Fewer permission questions — only in Elsa's folder.** If your session asks before every command
(the Manual mode), Elsa offers once to stop asking for her own commands in her folder
`~/Movies/AI Movie Company`. On your yes she writes `~/Movies/AI Movie Company/.claude/settings.json`
(`defaultMode: acceptEdits` and allow rules for the plugin's engine), and the guard hook lets a
plain call of the plugin's engine through there without Claude's card. Your global
`~/.claude/settings.json` and your mode selector are never touched. "Put it back" (`vp mode undo`)
restores that file byte for byte, or removes it if it was not there. Why not Auto: Claude Code
does not accept `defaultMode: auto` from a folder's settings, only from your own user settings or
the selector — which stay your choice.

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
  your verdicts), cost and notification journals; the animation add-ons in `extras/` and `remotion/` (the
  design tools, ~500 MB) and, in the job folder, `design.yaml`, `previews/design/` and
  `clips/<id>/layers/`.
- **The feedback report**, only when you ask for it: one Markdown file in
  `~/Movies/AI Movie Company/Feedback/` (not the Desktop: macOS asks permission for the Desktop);
  a Finder window opens with the file selected. While the installer waits for Apple's developer
  tools it keeps one small marker file in `~/.ai_video_pipeline/install/` and removes it after.
- **Safety records** in `~/.ai_video_pipeline/`: `consent.json` (the version and date of the
  accepted terms), `guard/` (the install guard's stops, one-time approvals, vet reports, its
  on/off setting, a log), `mode.json` (whether "fewer questions" is on, and a byte copy of the
  settings file it replaced); with "fewer questions" on — `~/Movies/AI Movie Company/.claude/settings.json`.
  The guard hook also notes this session's permission mode (`session_modes.json` in the plugin
  data folder).
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
- **The animation add-ons — once, at install** (or when you say "install the add-ons"):
  nodejs.org (Node.js 24), storage.googleapis.com (Google's Chrome Headless Shell), the Anthropic
  plugin directory through the Claude command line (the Remotion plugin for Claude) — every file
  checksum-checked, into `~/.ai_video_pipeline/extras/`.
- **npm registry** (registry.npmjs.org), at the same time, once: the pinned Remotion packages from `remotion/package-lock.json` (integrity-checked, install scripts
  off) into `~/.ai_video_pipeline/remotion/`. Remotion's own telemetry is switched off and its
  local servers listen on 127.0.0.1 only.
- **YouTube Data API** (Google), only with `publisher: youtube`: uploads private videos,
  captions, thumbnails, playlists; publishes what you approved.
- **Telegram Bot API**, only if you switch `notifier: telegram` on (off by default).
- **The authors' receiver** (api.alexeykrol.com), during the test period: usage logs and errors
  (installer log, engine steps, errors, timings, dead-end notes; keys and tokens removed), kept in
  `~/.ai_video_pipeline/telemetry/` and sent in the background; the same address gives messages
  from the authors ("a new version is out"). `AIMC_TELEMETRY=off` turns the sending off.
- Nothing else. Model judgment runs in your own Claude session.

## Support

support@alexeykrol.com
