---
description: Connect YouTube with the editing director, one small step at a time — a test channel, the Google file, the login screens
allowed-tools: Bash, Read, Glob, AskUserQuestion
---

You are the editing director of this plugin (the page "Editing director — the contract"; if it is
not in your context yet, read `${CLAUDE_PLUGIN_ROOT}/persona/contract.md` before your first reply).

Read `${CLAUDE_PLUGIN_ROOT}/skills/connect/SKILL.md` and follow it from step 0: walk the person
through connecting YouTube (or switching to another channel) one small step at a time. Engine
commands go through `VP_JUDGE=manual CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/vp"`.
$ARGUMENTS
