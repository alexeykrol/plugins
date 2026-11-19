---
description: S3c learning loop of fragment selection (eval, critic, analyst, replay, decide)
argument-hint: <subcommand> [args]  (snapshot | splits | eval | curve | critic | analyst | replay | export-eval | import-eval | decide | observe)
allowed-tools: Bash, Read, Write
---

Run `VP_JUDGE=manual CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/vp" learn $ARGUMENTS`
and report the result.

| Subcommand | What it does |
|---|---|
| `snapshot <job>` | archive a reviewed job's subtitles for replay |
| `splits show` / `splits set --fix-set a,b --holdout c` | fix-set and holdout recordings |
| `eval` | live metrics per knowledge version (precision, boundary error, misses, review time) |
| `curve [--versions v1,v2] [--recordings r1,r2]` | version series on the same recordings |
| `critic` | groups owner verdicts into findings |
| `analyst` | proposes candidate knowledge versions from the findings |
| `replay [version] [--recordings ...] [--force] [--no-baseline]` | re-run selection of a version on archived recordings |
| `export-eval <version> [--force]` / `import-eval <version>` | candidate evaluation batch for the owner (new proposals only) |
| `decide [version] [--dry-run]` | accept or reject a candidate by the measured metrics |
| `observe [--apply]` | watch the active version on new recordings; roll back on degradation |

All subcommands accept `--home <dir>` (default `~/.ai_video_pipeline` or `VP_HOME`).

`critic`, `analyst` and `replay` stop with pending judgment tasks under
`<home>/selection/work/<name>/judge/`: list them with `... judge-pending <home>/selection/work/<name>`,
perform them with the `judge` skill of this plugin, then repeat the command.

Activating or rolling back a knowledge version changes production behaviour: run `decide` without
`--dry-run`, `observe --apply` or `knowledge activate|rollback` only on the user's explicit request
in this dialog. Never edit the knowledge store files by hand.
