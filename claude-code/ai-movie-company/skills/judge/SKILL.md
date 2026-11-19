---
name: judge
description: Performs one pending AI movie company judgment task faithfully (proofreading chunk, fragment selection, design texts, metadata, thumbnail idea, cross-link graph, S3c critic/analyst/replay) by reading judge/<task>/prompt.md and its input files and writing the output file in the exact format the prompt specifies. Use when the pipeline run stops with WAITING_JUDGMENT or `judge-pending` lists tasks.
---

# judge — answer a judgment task

A task is a folder `judge/<task>/` inside a job (or inside `<home>/selection/work/<name>/`):

- `task.yaml` — task name and `output:` — the exact file name to write;
- `prompt.md` — the instructions, including the required output format;
- `input.<name>` — the inputs the prompt refers to by `<name>`;
- `attach.<name>` — images for the task (the `s5v.<item>` picture check attaches a contact sheet
  of frames). `input.attachments` lists their absolute paths: **open every listed image with the
  Read tool and look at it** before answering — the answer is about what is in the frames.

`CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/vp" judge-pending <job>` lists pending tasks as
`<task>\t<output path>`.

## How to answer

1. Read `task.yaml`, then `prompt.md` completely, then every `input.*` file.
2. Do exactly the judgment the prompt asks for — nothing more. The prompt is the contract;
   these rules only add the invariants every task shares.
3. Write ONLY the answer to the output path from `task.yaml` (one file, UTF-8). No preamble,
   no explanations, no Markdown fences unless the prompt's format includes them.
4. Answer each task independently, from its own inputs. Do not reuse text from another task.

## Invariants (code validates them; a violation fails the step with `contract`)

- **No timecodes, video IDs, URLs, quota numbers** — never write `00:12`, `1:24:20`,
  `-->`, milliseconds, `youtu.be/...`, video ids. Code computes all of them. Refer to places
  by the means the prompt gives: subtitle line ids (`ID|text` chunks), verbatim quotes, item
  numbers.
- **Exact format**: the structure (YAML / JSON / `ID|text` lines), field names, counts and
  order the prompt specifies. Every required id present exactly once, in the given order; no
  extra ids, no extra fields.
- **Verbatim where asked**: quotes must be copied character for character from the input.
- **Language**: texts for the audience are in the language of the material (usually
  Russian), unless the prompt says otherwise.
- **Author fields**: text the author fixed in the input (`locked`, `extend` prefixes) is kept
  byte for byte.
- **Picture check** (`s5v.*`): answer each question from the frames only; `yes` when it is true
  in any frame, and list those frame numbers in `problem_frames`. Never answer from the
  transcript or from what the video should look like.
- **Proofreading** (`s1.proofread.*`): fix recognition errors, punctuation and noise only;
  never change the meaning; an emptied line keeps its id with empty text.

If a task is impossible to answer within its format, do not invent a workaround: leave the
output unwritten and report the task name and the reason.
