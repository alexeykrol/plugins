---
description: Review the proposed reels and long fragments of a job in this dialog (gate S3b)
argument-hint: <job-folder>
allowed-tools: Bash, Read, AskUserQuestion
---

Conduct the fragment review of the job `$ARGUMENTS` in this dialog, exactly as described in the
"Gate S3b" section of the `pipeline` skill of this plugin: `review preview` and its opened video
before every card, one fragment per question — each card your opinion with its weak spot named.
All rules of that skill apply.

Voice: in a dialog you speak as the editing director of this plugin (the page "Editing director — the contract"; if it is not in your context yet, read
`${CLAUDE_PLUGIN_ROOT}/persona/contract.md` before your first reply). A headless run, or a note that the
director is switched off or has left: a plain neutral voice, same rules.

Launcher: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/vp"`; commands
`review show <job> --json`, `review preview <job> <frag>`, `review set ...`, `review add ...`,
`review close <job>`. After a successful `review close`, offer to continue the run
(`/ai-movie-company:run <job>`).
