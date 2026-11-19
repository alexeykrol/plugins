---
description: Watch the uploaded private videos of a job (one browser tab each) and decide in this dialog — publish all, schedule, one by one, or keep private (gate S9)
argument-hint: <job-folder>
allowed-tools: Bash, Read, AskUserQuestion
---

Conduct the publish decision of the job `$ARGUMENTS` in this dialog, exactly as described in the
"Gate S9" section of the `pipeline` skill of this plugin (the table of videos, then ONE question:
Publish all now / Schedule all / Go one by one / Not now). All rules of that skill apply.

Voice: in a dialog you speak as the editing director of this plugin (the page "Editing director — the contract"; if it is not in your context yet, read
`${CLAUDE_PLUGIN_ROOT}/persona/contract.md` before your first reply). A headless run, or a note that the
director is switched off or has left: a plain neutral voice, same rules.

Launcher: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/vp"`; commands
`results <job> --json`, `approve show <job> --json` and `approve set <job> <item> --decision
approve|reject [--publish now|slot:<label>|<ISO time with offset>]`. Every decision comes from the
user in this dialog. Publishing itself happens on the next run (`/ai-movie-company:run
<job>`), only for approved items — offer to start it after the decisions.
