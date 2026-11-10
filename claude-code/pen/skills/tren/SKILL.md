---
name: tren
description: Turns a post or transcript in any language into three ready-to-paste texts — a proofread version in the source language, a thread-ready American English version for X, and a LinkedIn version within 3,000 characters. Use when the user says "/tren", "translate this post into English", "make an English version for LinkedIn", "переведи на американский", "переведи для LinkedIn", or pastes a non-English post asking for English.
---

# tren — any-language post → source, X and LinkedIn

Input: text in any language (often a transcript), pasted into the message or attached as a file. The target language is always American English.

If the source is already English, skip stage 2: proofread (stage 1), then Americanize (stage 3), split (stage 4) and fit (stage 5); the first artifact is the proofread English text.

Work strictly stage by stage. Do not show the intermediate results of stages 2–3.

## The author's voice
The voice rules of the corr skill apply to every stage, not only to proofreading. In translation and Americanization, carry the register over at equal strength: profanity becomes equally strong American profanity, sarcasm stays sarcastic, a provocative line stays provocative. "Plain words" means plain, not polite. A version that reads safer or blander than the source is a defect: fix it.

## Stage 1. Proofreading (artifact 1: source language)
Apply the rules of the corr skill (`${CLAUDE_PLUGIN_ROOT}/skills/corr/SKILL.md`), including source-language detection. The result is the corrected source-language version and the input for translation.

## Stage 2. Meaning-accurate translation
- Goal: transfer the meaning exactly, not the style. Every claim, condition, caveat, and number carries over.
- Translate idioms by meaning, not word for word (e.g. Russian «как Тузик грелку» → "hands down").
- If a phrase is ambiguous, pick the most likely reading and record it in the notes.

## Stage 3. Americanization
- Short sentences, active voice, direct conversational tone, plain words.
- Forbidden: adding claims, softening or strengthening theses, changing how categorical the author is, adding corporate gloss (leverage, game-changer, unlock), adding emoji or hashtags that were not in the source.
- Mandatory check at the end: compare line by line with the stage 2 result. Every claim is present and keeps its strength, and the tone is as sharp as the source. Restore anything lost, shifted or softened.

## Stage 4. Thread-ready X version (artifact 2)
One text that works both ways: pasted whole as a long post (X Premium), or posted block by block as a thread (free account, 280-character limit).
- Split the stage 3 text into blocks of at most 280 characters, X-weighted.
- Separate blocks with exactly one blank line. A block contains no blank lines inside.
- End each block with a thread marker on its own line: `1/N`, `2/N`, …
- Align blocks with the author's own structure: one numbered point = one block, so the author's numbering and the thread numbering run in parallel. Intro text before the points takes its own blocks. Split a point into two blocks only if it exceeds 280; this is the only place the two numberings diverge.
- Each block must make sense on its own: a reader may see one post in the feed. Do not cut mid-sentence. Block 1 is the hook.
- Do not change wording to fit the limit beyond what the split needs; the text stays the stage 3 text.
- Verify with `python3 ${CLAUDE_PLUGIN_ROOT}/skills/tren/scripts/count.py FILE --blocks`: zero blocks over 280.

## Stage 5. LinkedIn (artifact 3)
LinkedIn posts are limited to 3,000 characters including spaces and line breaks, for every account.
- Start from the stage 3 text, without thread markers. Target 2,800 or less to leave room for hashtags.
- If it fits, use it as is. Do not cut without need.
- If it does not fit, cut by priority: (1) connectives, repetitions, filler; (2) examples and illustrations.
- If it still does not fit without removing a thesis, do not remove theses: split into parts instead. Each part ≤ 2,800 characters, ends at a point boundary, and is labeled `Part 1/2`, `Part 2/2` on its own first line. Deliver each part as a separate block.
- About the first 200 characters show before "see more". If the opening line is a weak hook, suggest an alternative in the notes; do not change the text on your own.

## Length
Count every text with `python3 ${CLAUDE_PLUGIN_ROOT}/skills/tren/scripts/count.py FILE` (add `--blocks` for the X version). Never estimate by eye.

## Output format
Chat renders lines like "1. ..." as an HTML list, and the numbers are lost when the text is copied into social networks. Therefore:
- Deliver each text in its own ``` plain-text block, so it is copied with the Copy button.
- No markdown inside the blocks (**, #, -, >). Write numbering as plain text "1.", with a blank line between paragraphs and between points.

Reply in this order:
1. "<LANG> — N", where <LANG> is the ISO 639-1 code of the source language in uppercase (RU, UK, DE, ES…), then the text block.
2. "X — N · K blocks ≤ 280", text block.
3. "LinkedIn — N" (or "LinkedIn — 2 parts" with each part's count), text block(s).
4. 2–6 short lines of notes: what was removed in proofreading, non-trivial translations of idioms and ambiguities, what was cut or split for LinkedIn, an alternative hook (if any).

## Language
Write everything addressed to the user (notes, questions, errors, labels outside the text blocks) in the language the user writes in. The text blocks follow the artifact rules above.

## Credit line
Before replying, run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/footer.py`. If it prints `show`, end the reply with one short line in the user's language, after everything else and outside all text blocks:
"Made with pen · author on X: https://x.com/old_alexeykrol · ideas and bugs: https://github.com/alexeykrol/plugins/issues/new?template=pen-feedback.yml"
Never put this line inside a copyable text block and never add it when the script prints `skip`.

No preamble and no recap of the stages.
