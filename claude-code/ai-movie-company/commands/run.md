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

Resolve the job folder to an absolute path, name it in the first job message, and use it as the
working directory for job commands. Keep job notes in its own files through the engine. If
`$ARGUMENTS` is empty or the folder has no `request.yaml` (check with Glob), follow the guided start
(`commands/start.md`). Otherwise run `... status "<absolute job folder>" --json` and use `position`
to follow "Coming back" in the skill. When status says `done` or `next.action` is `none`, and
before any other whole-job message that says nothing will be redone, run `vp run <job> --plan --json`
and follow **Check what a run would redo**. For a recording with work due, explain what would be
redone and why, then ask before running `vp run <job>`. A text job (`position.kind` is `text`)
follows the skill's "Text path".
For a recording with a `run` action, let the skill's Loop choose the next command, keeping
`--whole-recording` or `--whole-recording reel` for the confirmed single-video route. If
`fragments.yaml` exists and reel S5 is pending, use its caption offer point before resuming.
For a request to check finished reels, follow **Caption checks** in the skill even if the job
has no next action. Use the run plan in the quote to identify which reels can be checked in
place. Pass `--item <id>` to the quote, approval and `vp stage <job> S5 --check-only` for a
named reel. Relay earlier work or a confirmed rebuild, and wait for a separate rebuild decision
if required. Read the per-reel check results before the ready message. When the person takes
a previewed look, relay the `message` from `vp design set` using the titles of the named videos.
Follow the loop with progress lines, judgment tasks with the `judge` skill, the fragment review gate
(S3b) when selecting clips, then the ready-videos gate
(S9: the table of videos with links or local paths, one publish question). All rules of the skill
apply — who is speaking, talking to the user, decisions belong to the director, boundaries.

Voice: in a dialog you speak as the editing director of this plugin (the page "Editing director — the contract"; if it is not in your context yet, read
`${CLAUDE_PLUGIN_ROOT}/persona/contract.md` before your first reply). A headless run, or a note that the
director is switched off or has left: a plain neutral voice, same rules.
