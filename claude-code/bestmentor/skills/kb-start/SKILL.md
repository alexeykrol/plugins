---
name: kb-start
description: "First run of the bestmentor plugin with the ready-made base \"Теория каст и ролей\" — check that everything works right after installation. Checks that the search skill runs and the base is in place, and shows an example question and answer. Called by /kb-start, \"check the bestmentor plugin\", \"check bestmentor\", \"does the bestmentor plugin work?\" (in Russian «проверь плагин bestmentor», «проверь bestmentor», «плагин bestmentor работает?»)."
---

# /kb-start — checking the plugin with a ready-made base

Whoever called this has most likely just installed the plugin and **does not know the terminal**.
Talk to them in the language they write in, in plain words: no "terminal", "MCP", "CLI", "skill",
"JSON", no commands or paths unless they ask. You run the commands and tell the person what happened.
Each check is one short line, e.g. "✓ The base is in place".

Nothing needs to be built: the base "Теория каст и ролей" (20 records) already lives inside the plugin.

**Where the search script is.** This skill's folder is the one that holds this SKILL.md (Claude shows
it as "Base directory for this skill"). The script is in the sibling skill:
`<this skill's folder>/../kb-ask/scripts/search.py`. Below, `S` means

```
python3 "<this skill's folder>/../kb-ask/scripts/search.py"
```

## Step 1. The search skill and the base

Run `S --info`.

- **It printed `Base "…" (…): N units … file intact.`** — "✓ The base "Теория каст и ролей" is in place: N
  records" (N — from the output, do not count yourself).
- **`file DAMAGED`** — say that the base in the plugin is damaged and the plugin should be
  reinstalled. Stop here.
- **The command did not run** — look at the response:
  - it mentions `xcode-select`, `developer tools` or `Command Line Tools` — the Mac does not have
    Apple's developer tools yet, and the system opened their installer. Say: "A window asking to
    install the developer tools should have appeared. Click "Install" and accept the license (5–20
    minutes). Then quit Claude completely (⌘Q), open it again and say "check the bestmentor plugin"
    once more." Stop here.
  - `command not found` or an old Python (below 3.9) — "You need a recent Python from python.org (the
    Download button); then restart Claude and say "check the bestmentor plugin" again".
  - anything else — pass on the last line of the error and say the plugin should be reinstalled.

## Step 2. Example question and answer

Ask the base the example question **"Как перейти из одной касты в другую?"** exactly as written: `S "Как перейти из одной касты в другую?"`. Answer the
person in the shape of the kb-ask skill (`<this skill's folder>/../kb-ask/SKILL.md`):
- a direct answer in 1–3 sentences;
- grounding: lines `id · date — «quote»` (the quote — from the "quote:" line, at most two lines);
- if records contradict each other — both sides with dates, without picking a winner.

If the results are empty, the plugin is broken (the example was checked at packing time): go to the
summary.

Add one line: "This is how the plugin answers from the base: with quotes and showing where records
disagree with each other".

If the person asks whether the plugin remembers their corrections: yes — when they say about a record
"this is outdated" or "this is valuable", the plugin writes that to their folder on the computer, and
such records are no longer offered. In Chat on claude.ai decisions are not kept between
conversations. Do not bring this up yourself and record nothing during the check.

## Summary — one sentence

- **The base is in place, the example answered:** "The plugin works. Ask about what is in the base
  "Теория каст и ролей" — for example: "Как перейти из одной касты в другую?"".
- **Something is wrong** — in one or two sentences: what does not work (in your own words, no
  tracebacks) and what to do: install the developer tools, restart Claude or reinstall the plugin.

## Do not
- Show the person commands, paths or raw output unless they ask.
- Install anything yourself (pip, brew) — only explain what to click.
- Build the base or touch the base files — it ships ready-made and is read-only.
