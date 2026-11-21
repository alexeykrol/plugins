[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](TERMS.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](TERMS.ru.md)

# SIMovie — terms of use (closed alpha)

Version: **2026-11-21-alpha1**

Full Terms of Service: https://alexeykrol.com/elsa/terms/ · Privacy Policy:
https://alexeykrol.com/elsa/privacy/

SIMovie — the AI Movie Company plugin, with editing director Elsa ("the plugin") — is published by
Alexey Krol, an individual (sole proprietor) based in California, USA. This page says what the
plugin does on your Mac and sums up the alpha terms. On the first start Elsa shows a short summary
and asks you to accept. **By accepting, you accept this page, the full Terms of Service and the
Privacy Policy at the links above**; where this page and the full Terms differ, the full Terms apply.
Until you accept, the plugin installs nothing and uploads or publishes nothing.

## 1. What the plugin does on your Mac

- It runs inside Claude Code (the Code tab of the Claude desktop app, or the terminal) on your Mac.
- It **reads** the recordings, subtitles and texts you point it to. It is designed never to change
  your source files.
- It **writes** only: the job folder you choose (subtitles, fragment lists, rendered videos,
  thumbnails, reports); `~/Movies/AI Movie Company/` (its own folder, feedback packs);
  `~/Movies/CapCut/…` (a new CapCut project, only when you ask for one); `~/.ai_video_pipeline/`
  (settings, its knowledge of your choices, journals, the design add-ons); the plugin's data folder
  inside Claude's configuration; `~/.local/bin` (the tools listed below). The full list is in the
  README, "What it writes to disk".
- It keeps a local journal of what you type in your sessions with Elsa (secrets removed) so that
  problems can be found. It covers only sessions with Elsa, stays on your Mac, and leaves it only if
  you send the feedback pack yourself.
- It **protects your Mac from outside code**: any command that would install software that is not
  on the plugin's own pinned list — a package from npm, PyPI, Homebrew and the like, a Claude
  plugin, a script downloaded and run at once — is stopped. Elsa explains what it was, checks it
  (name, version, age, known incidents) and runs it only once, and only if you say yes. You can turn
  this check off with an explicit setting (README, "Safety").
- With your agreement it can write a settings file **only in its own folder**
  (`~/Movies/AI Movie Company/.claude/settings.json`) so that Claude asks fewer permission questions
  there. It never changes Claude's global settings. "Put it back" undoes it.

## 2. What it installs and from where

At installation and when you ask for an update or for the design add-ons — every file pinned to a
version and checked against a checksum:

| What | From |
|---|---|
| Claude's command line | claude.ai (Anthropic's official installer) |
| uv and Python 3.13 | github.com (Astral) |
| ffmpeg and ffprobe 9.0.2 | ffmpeg.martin-riedl.de |
| The plugin itself | the `alexeykrol/plugins` store on github.com |
| The engine's Python packages | pypi.org, the hash-pinned list `requirements.lock` |
| Node.js 24 | nodejs.org |
| Chrome Headless Shell (drawing video) | storage.googleapis.com (Google, Chrome for Testing) |
| The Remotion project | registry.npmjs.org, the pinned lockfile, install scripts off |
| The Remotion plugin for Claude | Anthropic's plugin directory, through Claude's command line |

Python and ffmpeg that you already have are left alone. Nothing needs a password or administrator
rights.

## 3. What it sends over the network

- **Services under your own accounts and keys**, only for the task you asked for:
  - **YouTube (Google)** — only when a job uploads: private drafts, captions, thumbnails, playlists,
    and publishing of exactly what you approved. You sign in on Google's own page; the plugin never
    sees your password; the login token stays on your Mac.
  - **ElevenLabs** — the text to be voiced and the music description, only after you chose to go
    ahead at the stated price; the audio of a recording only when you press "Transcribe". Through
    your own key or your own ElevenLabs connector in Claude.
  - **Pexels, Pixabay** (stock video and photos) — search words; **Google Gemini, OpenAI** (picture
    generation) — picture descriptions. Only with your own key and on your request.
  - **Telegram** — notifications, only if you turn them on with your own bot.
  - **Anthropic (Claude)** — what you show Claude in the conversation is processed by Anthropic under
    your agreement with Anthropic; the plugin adds no other recipient.
- **The installation sources** in section 2, when something is installed or updated.
- **Technical telemetry to us** (`api.alexeykrol.com`) — **only after you agree to it in the plugin**:
  command and step names, durations, error types, plugin and macOS versions, with a random install
  ID. Never your dialogues, prompts, content, file or folder paths, user names, error texts or
  Elsa's notes. `vp telemetry show` shows what would be sent; `vp telemetry off` turns it off at any
  time; `vp telemetry status` shows your install ID (send it to us to have your telemetry deleted).
- **Messages from us** — the plugin asks the same address for messages such as "a new version is
  out". These requests are not stored with your install ID or IP.
- Nothing else: no analytics, no advertising, no hidden recipients. The animation tools' telemetry
  is switched off and their local servers listen on your Mac only (127.0.0.1).

Anything else reaches us only if you send it yourself: the feedback pack (your journal of sessions
with Elsa, Elsa's notes, technical checks), an email or a message.

## 4. What we try to do (not a promise)

- **Security:** pinned and audited dependencies — exact versions, checksums or hashes, at least 14
  days old when pinned, install scripts off where possible; audits are kept in the source
  repository.
- **Privacy, as far as we can actually provide it:** we want nothing of yours; the network list in
  section 3 is meant to be complete for the plugin's code.

These are efforts, not warranties or commitments. There is no support duty, no service level and no
availability guarantee. You can write to support@alexeykrol.com; we may or may not reply.

## 5. What you accept

- **It is an alpha, used at your own risk.** It may break, change, lose your work or stop at any
  time, for any reason, without notice. Keep your own copies of anything important.
- **Your content is yours.** Your recordings, texts, videos and channels remain yours. You are
  responsible for having the rights to what you process and publish (people on screen, voices,
  music, footage, quotes) and for following the rules of each platform.
- **You decide what is published.** The plugin uploads private drafts and publishes only what you
  approve. The decision and the responsibility for publishing are yours.
- **YouTube.** The plugin uses YouTube API Services. By using its YouTube features you agree to be
  bound by the [YouTube Terms of Service](https://www.youtube.com/t/terms). See the
  [Google Privacy Policy](http://www.google.com/policies/privacy) and our
  [Privacy Policy](https://alexeykrol.com/elsa/privacy/). Revoke access at any time at
  https://security.google.com/settings/security/permissions, or run `vp youtube disconnect`, which
  revokes access and deletes the login token from your Mac.
- **Third-party services** — Anthropic (Claude), Google (YouTube, Gemini), ElevenLabs, Pexels,
  Pixabay, OpenAI and Telegram — are used under your own accounts and their own terms and prices
  (for example ElevenLabs credits). We are not a party to them.
- **Third-party components** keep their own licences. Remotion (the design look) is free for
  individuals, companies of up to 3 people and non-profits; larger companies need a Remotion licence
  (remotion.dev/license). The plain look does not depend on it.
- **You keep your system updated**: macOS, the Claude app and the plugin ("update the plugin").
- **Indemnity.** As far as the law allows, you are responsible for, and will defend and indemnify
  us against, claims arising from content you create or publish with the plugin (including on
  YouTube), or from your breach of these terms or of a third-party service's terms.
- **Invites.** If you don't use your personal install link within 7 days, we may give your place to
  the next person.
- **Age.** You must be at least 18 years old.
- **No warranty, limited liability.** The plugin's code is free and open under the MIT licence
  ([LICENSE](LICENSE)), which governs your rights in the code. The plugin and the alpha are provided
  "as is", without warranty of any kind. As far as the law allows, our total liability for all claims
  is limited to the greater of US $100 and the amount you paid us for the plugin in the 12 months
  before the claim. The full Terms (link above) give the exact wording, the exclusions, California
  law and the courts.

## 6. Accepting, declining, changing

- Elsa shows the summary with three buttons: «Принимаю» (I accept), «Прочитать полностью» (read in
  full) and «Не сейчас» (not now). Your acceptance — the version and the date — is stored on your
  Mac in `~/.ai_video_pipeline/consent.json`. Nothing is sent anywhere.
- «Не сейчас»: Elsa can still talk and explain, but installs nothing and uploads or publishes
  nothing until you accept.
- When these terms change, the version changes and Elsa asks again.
- You can take your acceptance back at any time ("withdraw the terms"); installs and publishing stop
  until you accept again. Uninstalling the plugin removes it from Claude; your files stay.

Contact: support@alexeykrol.com · Alexey Krol, 1031 Crestview Dr, Mountain View, CA 94040, USA
