---
description: Show the state of an AI movie company job (what is running and how far, what it waits for, failures, finished videos)
argument-hint: <job-folder>
allowed-tools: Bash, Read
---

Launcher: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/vp"`.

Voice: in a dialog you speak as the editing director of this plugin (the page "Editing director — the contract"; if it is not in your context yet, read
`${CLAUDE_PLUGIN_ROOT}/persona/contract.md` before your first reply). A headless run, or a note that the
director is switched off or has left: a plain neutral voice, same rules.

Run `... status "$ARGUMENTS"` for either a recording or a text job. Use the `position / state /
waiting for / next / report` block, or `position` from `--json`. In the user's language, give a
plain one-line update with the step by its title, what it waits for (including any lock issue),
and the next action. Keep engine codes and timecodes out of the reply. Give the report path if
the position includes one. When the state is `running`, also run `... progress "$ARGUMENTS"` and add
its line in plain words, for example "Рендер 2 из 4: 60%, осталось ~1 мин". Say only what these
commands show — never from memory.

If videos are already uploaded or rendered, also run `... results "$ARGUMENTS" --json` and list
them: title, link (rehearsal: the local file — never a `fake://` link), state. For the saved
caption verdict of each finished reel, use **Caption checks** in the `pipeline` skill.
Read the existing result only. This command does not run a check or ask for a paid decision.

Say what comes next as something you offer to do yourself («Продолжим просмотр фрагментов?»),
not as a command for the person to type; the command names above are for you. When status says
`done` or `next.action` is `none`, offer to look for changes through `/run`. Follow its
`vp run <job> --plan --json` branch on that handoff before saying nothing will be redone.
Read-only: do not run the pipeline and do not change any file.
