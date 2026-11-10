---
name: tren
description: Turns a post or transcript in any language into three ready-to-paste texts — a proofread version in the source language, a full American English translation for X, and a LinkedIn-length English version. Use when the user says "/tren", "translate this post into English", "make an English version for LinkedIn", "переведи на американский", "переведи для LinkedIn", or pastes a non-English post asking for English.
---

# tren — any-language post → source, X and LinkedIn

Input: text in any language (often a transcript), pasted into the message or attached as a file. The target language is always American English.

If the source is already English, skip stage 2: proofread (stage 1), then Americanize (stage 3) and cut (stage 4); the first artifact is the proofread English text.

Work strictly stage by stage. Do not show the intermediate results of stages 2–3.

## Stage 1. Proofreading (artifact 1: source language)
Apply the rules of the corr skill (`${CLAUDE_PLUGIN_ROOT}/skills/corr/SKILL.md`), including source-language detection. The result is the corrected source-language version and the input for translation.

## Stage 2. Meaning-accurate translation
- Goal: transfer the meaning exactly, not the style. Every claim, condition, caveat, and number carries over.
- Translate idioms by meaning, not word for word (e.g. Russian «как Тузик грелку» → "hands down").
- If a phrase is ambiguous, pick the most likely reading and record it in the notes.

## Stage 3. Americanization (artifact 2: X)
- Short sentences, active voice, direct conversational tone, plain words.
- Forbidden: adding claims, softening or strengthening theses, changing how categorical the author is, adding corporate gloss (leverage, game-changer, unlock), adding emoji or hashtags that were not in the source.
- Mandatory check at the end: compare line by line with the stage 2 result. Every claim is present and keeps its strength. Restore anything lost or shifted.
- This version targets a long post on X (Premium). Build a 280-character thread only on request.

## Stage 4. LinkedIn (artifact 3)
- Limit: 3,000 characters including spaces and line breaks. Target 2,800 or less to leave room for hashtags.
- If the X version already fits, the LinkedIn version is identical. Do not cut without need.
- If it does not fit, cut by priority: (1) connectives, repetitions, filler; (2) examples and illustrations; (3) theses, only as a last resort. Name any removed thesis explicitly in the notes.
- About the first 200 characters show before "see more". If the opening line is a weak hook, suggest an alternative in the notes; do not change the text on your own.

## Length
Count all three texts with a script (python `len()` or `wc -m`), never by eye.

## Output format
Chat renders lines like "1. ..." as an HTML list, and the numbers are lost when the text is copied into social networks. Therefore:
- Deliver each text in its own ``` plain-text block, so it is copied with the Copy button.
- No markdown inside the blocks (**, #, -, >). Write numbering as plain text "1.", with a blank line between paragraphs and between points.

Reply in this order:
1. "<LANG> — N", where <LANG> is the ISO 639-1 code of the source language in uppercase (RU, UK, DE, ES…), then the text block.
2. "X — N", text block.
3. "LinkedIn — N", text block (or one line saying it is the same as X, if identical).
4. 2–6 short lines of notes: what was removed in proofreading, non-trivial translations of idioms and ambiguities, what was cut for LinkedIn, an alternative hook (if any).

## Language
Write everything addressed to the user (notes, questions, errors, labels outside the text blocks) in the language the user writes in. The text blocks follow the artifact rules above.

No preamble and no recap of the stages.
