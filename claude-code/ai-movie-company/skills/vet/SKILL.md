---
name: vet
description: Vets outside code before it is installed on the person's computer — an npm/PyPI/Homebrew/cargo/gem/Go package, a Claude plugin or plugin store, an MCP server, a script downloaded and run — after the plugin's install guard stopped the command ("AI Movie Company install guard: STOPPED (id …)"). Checks the exact name, version and source, the release age (14 days or older), fresh incident reports, install scripts and look-alike names; gives a verdict clean / caution / do-not-install; explains it to the person in plain words and asks; records a one-time approval of exactly one command. Use whenever the guard stopped a command, or the person asks "можно это поставить?", "проверь пакет", "это безопасно установить?", "/ai-movie-company:vet".
allowed-tools: Bash, Read, Write, WebSearch, WebFetch, AskUserQuestion
---

# vet — check outside code before it reaches the person's computer

The person is not a developer. Packages and install scripts get hijacked: a maintainer's account
is stolen, a fresh version carries malware for a few hours, a look-alike name catches a typo. The
plugin's install guard (`hooks/guard.py`) therefore stops every command that would bring outside
code onto the machine and sends it here. Nothing is installed until this skill has a verdict and
the person said yes to **exactly one command**.

Engine launcher (written below as `vp`):
`CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/vp"`

In a dialog you speak as Elsa (the page "Editing director — the contract", section "Safety on
the person's computer"): plain Russian, short, no jargon unless the person uses it.

## 0. Say what happened — before any checking

One short message, in the person's language: what was about to be installed (in words: «пакет
для чтения PDF из npm», not the command), that the plugin checks every outside install first
because such packages are sometimes hijacked, and what happens next («проверю за минуту и скажу,
что нашла»). No alarm, no blame. Then check.

Did the person really need it? If the job can be done with what is already there (the plugin's
own engine, ffmpeg, macOS tools), say so and offer that instead — the cheapest safe install is no
install.

## 1. Pin down exactly what would be installed

From the stopped command (`vp guard status --json` lists the latest stops with their ids):

- the ecosystem and the **exact** names (no typos: compare with the official project page);
- the version that would be taken — a bare name means "latest" and a range like `^1.2` means
  "whatever is newest": the vet must end with an exact version;
- the source: the official registry, a GitHub repository, a web address. A script from a web
  address: read it (WebFetch, never run it) and name what it would download and run in turn.

## 2. Check — fresh, every time

Do not rely on memory: the threat picture changes weekly. Read-only commands only.

| Check | How |
|---|---|
| Release date of the chosen version (14 days or older) | npm: `npm view <pkg> time --json`; PyPI: WebFetch `https://pypi.org/pypi/<pkg>/json`; Homebrew: `brew info --json=v2 <name>`; GitHub: the releases page. Younger than 14 days → take the newest version that is 14+ days old. |
| Incidents | WebSearch `"<pkg>" compromised OR malicious OR malware <month year>` and `"<pkg>" security advisory`; the GitHub advisory database; for a whole ecosystem, the news of the last 2–4 weeks (active campaigns). |
| Install scripts | npm: `npm view <pkg>@<ver> scripts` (preinstall / install / postinstall); PyPI: a source-only package (no wheel) runs `setup.py` at install. Prefer `--ignore-scripts`, wheels only (`--only-binary=:all:`). |
| Who publishes | npm: `npm view <pkg> maintainers`; a recent change of owner or a sudden burst of releases → caution. |
| Look-alike names | name similar packages, so that exactly the right one is installed. |
| A Claude plugin or store | who publishes it (Anthropic's directory, a known company, an unknown account), what it ships (hooks and MCP servers run code; skills alone do not), stars/age of the repository. |
| A script piped into a shell | its owner and domain, what it downloads, whether it checks checksums, whether it asks for a password (sudo). |

Write the findings to `~/.ai_video_pipeline/guard/reports/<id>.md`: date, the command, the exact
name/version/source, each check with its source link, the verdict. Keep it short; it is for the
record and for Alexey's feedback report.

## 3. Verdict

- **clean** — exact version 14+ days old, no incidents, no install scripts (or they are off), a
  known publisher.
- **caution** — usable with conditions: name them and build the safer command (exact version,
  `--ignore-scripts`, `--save-exact`, wheels only).
- **do-not-install** — an incident, a fresh hijack, an unknown publisher of a script that wants a
  password, a look-alike name, or no way to pin a version. Then nothing is installed:
  `vp guard approve <id> --verdict do-not-install` (records the refusal), say so plainly and offer
  another way.

## 4. Ask the person — buttons, one question

Two or three plain sentences: what it is, what you found, your verdict and why — then the form
(AskUserQuestion): **«Ставим»** / **«Не ставим»** (+ «Расскажи подробнее» if they may want it).
Never press; "not now" is a fine answer. Never install without their own «Ставим».

## 5. One approved run

After their own «Ставим» (the engine refuses without it — exit 4):

```
vp guard approve <id> --verdict clean|caution --report ~/.ai_video_pipeline/guard/reports/<id>.md \
  [--command '<the safer exact command>']
```

Then run **exactly** the approved command once (within 30 minutes). Any other command — even one
flag different — is a new stop. Prefer installing into a private folder over a global install
(`npm install --prefix …`, a virtualenv), and never with `sudo`.

## Rules

- Never get around the guard: no rewording of the command to dodge it, no `bash -c`, no script
  file, no turning it off. If the person wants it off, that is their explicit choice: `vp guard
  off --request`, the buttons, `vp guard off --confirm` (README, "Safety").
- The terms come first: if the engine says the terms are not accepted, show them (`vp consent
  show`) before anything is installed.
- A headless run (no person in the dialog): do not approve anything; report the stop and the
  verdict and stop.
