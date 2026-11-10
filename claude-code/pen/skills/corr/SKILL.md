---
name: corr
description: Proofreads a Russian draft or speech transcript into a clean written post without changing its meaning. Use when the user says "/corr", "откорректируй", "вычитай", "поправь расшифровку", "сделай из расшифровки текст", "proofread this", or pastes dictated/transcribed Russian text for cleanup. Also serves as stage 1 of /tren.
---

# corr — proofread a Russian draft or transcript

Input: Russian text, often a transcript of speech, pasted into the message or attached as a file.

## Clean the input
Drop copy-paste debris: "Facebook", "Shared post", "No comments", "Be the first to comment", buttons, counters, social-network captions.

## Proofreading rules
- Turn speech into written text: remove filler and verbal clutter («то есть», «да?», «вот», «как бы», «вот эта всякая фигня»), repetitions, broken and restarted sentences.
- Fix spelling, punctuation, agreement, and speech-recognition errors.
- Never change: meaning, theses, order of arguments, numbers, the author's structure (numbered points), the author's emphasis (CAPS, emoji), or how strongly a claim is made.
- Preserve the author's voice. Keep colloquial or figurative phrases («как Тузик грелку») when they carry tone. Remove only what adds nothing.
- Add nothing of your own: no thoughts, examples, conclusions, headings, or hashtags.
- If a phrase in the transcript is ambiguous, pick the most likely reading and note it in the edit list.

## Length
Count characters with a script (python `len()` or `wc -m`), never by eye.

## Output format
Chat renders lines like "1. ..." as an HTML list, and the numbers are lost when the text is copied into social networks. Therefore:
- Deliver the text inside a ``` plain-text block, so it is copied with the Copy button.
- No markdown inside the block (**, #, -, >). Write numbering as plain text "1.", with a blank line between paragraphs and between points.

Reply in this order:
1. "RU — N знаков", then the text block.
2. 2–5 short lines in the user's language: what was removed, how ambiguities were resolved.

## Language
Write everything addressed to the user (notes, questions, errors, labels outside the text blocks) in the language the user writes in. The text blocks themselves follow the artifact rules above.

No preamble.

When run as stage 1 of /tren, this output becomes the first artifact; its edit notes go into /tren's combined notes instead of a separate list.
