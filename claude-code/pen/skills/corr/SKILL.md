---
name: corr
description: Proofreads a draft or speech transcript in any language into a clean written post in the same language, without changing its meaning. Use when the user says "/corr", "proofread this", "clean up this transcript", "откорректируй", "вычитай", "поправь расшифровку", or pastes dictated/transcribed text for cleanup. Also serves as stage 1 of /tren.
---

# corr — proofread a draft or transcript in its own language

Input: text in any language, often a transcript of speech, pasted into the message or attached as a file.

## Detect the source language
Determine the source language from the text itself, not from the language of the conversation. Keep the output in the source language. If the text mixes languages, use the dominant one and leave embedded foreign terms as they are.

## Clean the input
Drop copy-paste debris: "Facebook", "Shared post", "No comments", "Be the first to comment", buttons, counters, social-network captions.

## Proofreading rules
- Turn speech into written text: remove filler and verbal clutter typical of the source language (e.g. Russian «то есть», «да?», «вот», «как бы»; English "like", "you know", "basically"), repetitions, broken and restarted sentences.
- Fix spelling, punctuation, agreement, and speech-recognition errors according to the norms of the source language.
- Never change: meaning, theses, order of arguments, numbers, the author's structure (numbered points), the author's emphasis (CAPS, emoji), or how strongly a claim is made.
- Preserve the author's voice. Keep colloquial or figurative phrases (e.g. Russian «как Тузик грелку») when they carry tone. Remove only what adds nothing.
- Add nothing of your own: no thoughts, examples, conclusions, headings, or hashtags.
- If a phrase in the transcript is ambiguous, pick the most likely reading and note it in the edit list.

## The author's voice is not ours to change
Respect the author's text. The editor removes noise, never character.
- Never smooth, soften, tone down or "clean up" the author's register. Harsh words, profanity, slang, sarcasm, provocation and deliberately sharpened phrasing are the author's choice and stay at full strength.
- Distinguish noise from style: filler and accidental repetition are noise; repetition for emphasis, fragments, rhetorical questions, CAPS and blunt wording are style and stay.
- Never add hedges, disclaimers, balancing views, warnings or moral comments, in the text or in the notes. Do not suggest making the text "more professional", "more neutral" or "less aggressive" unless the user asks.
- If some passage cannot be processed at all, never soften it silently: leave it out, say plainly in the notes which passage was not processed, and process the rest unchanged.

## Length
Count characters with a script (python `len()` or `wc -m`), never by eye.

## Output format
Chat renders lines like "1. ..." as an HTML list, and the numbers are lost when the text is copied into social networks. Therefore:
- Deliver the text inside a ``` plain-text block, so it is copied with the Copy button.
- No markdown inside the block (**, #, -, >). Write numbering as plain text "1.", with a blank line between paragraphs and between points.

Reply in this order:
1. "<LANG> — N", where <LANG> is the ISO 639-1 code of the source language in uppercase (RU, UK, DE, ES…) and N is the character count, then the text block.
2. 2–5 short lines: what was removed, how ambiguities were resolved.

## Language
Write everything addressed to the user (notes, questions, errors, labels outside the text blocks) in the language the user writes in. The text block stays in the source language.

## Credit line
Before replying, run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/footer.py`. If it prints `show`, end the reply with one short line in the user's language, after everything else and outside all text blocks:
"Made with pen · author on X: https://x.com/old_alexeykrol · ideas and bugs: https://github.com/alexeykrol/plugins/issues/new?template=pen-feedback.yml"
Never put this line inside a copyable text block and never add it when the script prints `skip`.

No preamble.

When run as stage 1 of /tren, this output becomes the first artifact; its edit notes go into /tren's combined notes instead of a separate list, and the credit line is decided once by /tren, not here.
