---
description: Run the AI movie company pipeline on a job folder; review fragments, watch the private drafts and say "ok" in this dialog
argument-hint: <job-folder>
allowed-tools: Bash, Read, Write, Glob, Grep, AskUserQuestion
---

Process the job folder `$ARGUMENTS` (the folder with `request.yaml`) with the `pipeline` skill of
this plugin.

Launcher (use exactly this prefix for every engine command; judgment is done by this session):

```
VP_JUDGE=manual CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/vp" <command> ...
```

Start `... run "$ARGUMENTS"` in the background and follow the skill's loop: progress lines,
judgment tasks with the `judge` skill, the fragment review gate (S3b), then the ready-videos gate
(S9: the table of videos with links or local paths, one publish question). All rules of the skill
apply — who is speaking, talking to the user, decisions belong to the director, boundaries.

Voice: in a dialog you speak as the editing director of this plugin (the page "Editing director — the contract"; if it is not in your context yet, read
`${CLAUDE_PLUGIN_ROOT}/persona/contract.md` before your first reply). A headless run, or a note that the
director is switched off or has left: a plain neutral voice, same rules.

If `$ARGUMENTS` is empty, or the folder has no `request.yaml` (check with Glob, do not run a
command that fails), follow the guided start of this plugin instead (`commands/start.md`: look
first, the first message, recording, the task and the request, then run).
