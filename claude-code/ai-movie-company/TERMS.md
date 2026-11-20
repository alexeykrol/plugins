[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](TERMS.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](TERMS.ru.md)

# AI Movie Company — terms of use

> **DRAFT for the owner, version `2026-11-21-draft`.** Written by the development agent from the
> plugin's code and the MIT licence; not reviewed by a lawyer. Have a lawyer read it before
> relying on it, in particular for the jurisdictions where the plugin is offered. Nothing here adds
> to or limits the [LICENSE](LICENSE); where they differ, the LICENSE prevails.

These terms describe what the AI Movie Company plugin ("the plugin") does on your computer and
what you and its authors commit to. On the first start Elsa, the plugin's editing director, shows
a short summary and asks you to accept it. Until you accept, the plugin does not install anything
and does not upload or publish anything.

## 1. What the plugin does on your computer

- It runs inside Claude Code (the Code tab of the Claude desktop app, or the terminal) on your Mac.
- It **reads** the recordings, subtitles and texts you point it to. It never changes your source
  files.
- It **writes** only: the job folder you choose (subtitles, fragment lists, rendered videos,
  thumbnails, reports); `~/Movies/AI Movie Company/` (its own folder, feedback reports);
  `~/Movies/CapCut/…` (a new CapCut project, only when you ask for one); `~/.ai_video_pipeline/`
  (settings, its knowledge of your choices, journals, the design add-ons); the plugin's data folder
  inside Claude's configuration; `~/.local/bin` (the tools listed below). The full list is in the
  README, "What it writes to disk".
- It keeps a local journal of what you type to Elsa (secrets removed) so that problems can be
  found. It stays on your computer; it leaves only if you send the feedback file yourself.
- It **protects your computer from outside code**: any command that would install software that
  is not on the plugin's own pinned list — a package from npm, PyPI, Homebrew and the like, a
  Claude plugin, a script downloaded and run at once — is stopped. Elsa explains what it was,
  checks it (name, version, age, known incidents) and runs it only once, and only if you say
  yes. You can turn this check off with an explicit setting (README, "Safety").
- With your agreement it can write a settings file **only in its own folder**
  (`~/Movies/AI Movie Company/.claude/settings.json`) so that Claude asks fewer permission
  questions there. It never changes Claude's global settings. "Put it back" undoes it.

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

Checked against the plugin's code on 2026-11-19:

- **YouTube (Google)** — only when a job uploads: private drafts, captions, thumbnails,
  playlists, and publishing of exactly what you approved. Signing in happens on Google's own page;
  the plugin never sees your password; the access key stays on your computer.
- **ElevenLabs** — voice and music for a video made from a text go through **your own**
  ElevenLabs key (or, if you connected it instead, your own ElevenLabs connector in Claude), under
  your ElevenLabs account and its terms: the plugin's code sends the text to be voiced and the
  music description, and only after you chose to go ahead at the stated price. **Transcription** of
  a recording without subtitles happens only when you press "Transcribe": the plugin's code sends
  the recording's audio to ElevenLabs with the same key. The key is kept only on your computer.
- **Stock and pictures** — Pexels, Pixabay (ready-made video and photos), Google Gemini and OpenAI
  (picture generation): only if you give your own key, and only on your request; what is sent is
  the search words or the picture description, under your account and the service's terms.
- **The installation sources** in section 2, when something is installed or updated.
- **Telegram** — only if you switch notifications to Telegram yourself (off by default).
- **Test-period telemetry — the authors' receiver** (`api.alexeykrol.com`): while the plugin is
  being tested, usage logs and errors (the installer's log, the engine's steps, errors and timings,
  Elsa's notes on dead ends) are recorded on your computer in `~/.ai_video_pipeline/telemetry/` and
  sent to the author to improve the plugin; keys and tokens are removed first. The plugin also asks
  the same address for messages from the authors (for example "a new version is out"). To turn the
  sending off, set `AIMC_TELEMETRY=off` in Claude's environment (settings `env`); the messages are
  then asked without the installation's id.
- Nothing else: no analytics, no advertising, no hidden recipients. The animation
  tools' telemetry is switched off and their local servers listen on your computer only
  (127.0.0.1).

What you show Claude in the conversation (subtitles, texts, frames, your messages) is processed by
Claude under **your agreement with Anthropic**; the plugin adds no other recipient.

## 4. What the authors commit to

- Pinned and audited dependencies: exact versions, checksums or hashes, at least 14 days old
  when pinned, install scripts off where possible; audits are kept in the source repository.
- No hidden data collection; the network list in section 3 is complete for the plugin's code.
- Publishing only on your explicit "ok" in the dialog; uploads are private drafts until then.
- A support address: **support@alexeykrol.com**.

## 5. What you accept

- **Your content is yours.** Your recordings, texts, videos and channels remain yours. You are
  responsible for having the rights to what you process and publish (people in the frame, music,
  quotes) and for following the rules of YouTube and of the services you connect.
- **You decide what is published.** The plugin uploads private drafts; it publishes only what you
  approve. The decision and the responsibility for publishing are yours.
- **You keep your system updated**: macOS, the Claude app and the plugin ("update the plugin").
- **Third-party services** (Google/YouTube, ElevenLabs, Anthropic, Telegram) are used under your own
  accounts and their own terms and prices (for example ElevenLabs credits).
- **Third-party components** keep their own licences. Remotion (the design look) is free for
  individuals, companies of up to 3 people and non-profits; larger companies need a Remotion
  licence (remotion.dev/license). The plain look does not depend on it.
- **No warranty.** The plugin is free and open software under the MIT licence. It is provided
  "as is", without warranty of any kind, express or implied, including but not limited to the
  warranties of merchantability, fitness for a particular purpose and noninfringement. In no event
  shall the authors or copyright holders be liable for any claim, damages or other liability,
  whether in an action of contract, tort or otherwise, arising from, out of or in connection with
  the software or the use or other dealings in the software.

## 6. Accepting, declining, changing

- Elsa shows the summary with three buttons: «Принимаю» (I accept), «Прочитать полностью» (read
  in full) and «Не сейчас» (not now). Your acceptance — the version and the date — is stored on
  your computer in `~/.ai_video_pipeline/consent.json`. Nothing is sent anywhere.
- «Не сейчас»: Elsa can still talk and explain, but installs nothing and uploads or publishes
  nothing until you accept.
- When these terms change, the version changes and Elsa asks again.
- You can take your acceptance back at any time ("withdraw the terms"); installs and publishing
  stop until you accept again. Uninstalling the plugin removes it from Claude; your files stay.

Contact: support@alexeykrol.com
