# Task: proofread automatic Russian subtitles

You receive one chunk of an automatic speech-recognition transcript of a Russian talk (lecture,
interview or Q&A) in the input `chunk.txt`. Every line has the form

    ID|text

where `ID` is an opaque cue identifier and `text` is what the recognizer heard for that cue.
The optional input `names.txt` lists proper names, brands and terms (one per line) that occur in
this recording — use their exact spelling.

Rewrite the **text** of every cue into clean, readable Russian subtitles. Rules:

1. Remove filler words and empty repetitions («ну», «вот», «как бы», «типа», «значит», «э-э»,
   stuttered repeats). Keep repetitions that carry meaning or emphasis.
2. Fix punctuation. Put sentence-final punctuation where a sentence ends.
3. Capitalisation: a cue that continues the sentence of the previous cue starts with a lower-case
   letter; a cue that starts a new sentence starts with a capital letter.
4. Fix recognition errors from context and from the names list (misheard words, broken names,
   wrong word boundaries). Do not paraphrase, do not "improve" the speaker's wording, do not add
   words or ideas the speaker did not say. Never change a negation.
5. Do not censor profanity — keep it as spoken.
6. Never move words between cues: each cue keeps exactly its own words, even if a sentence is split
   across cues.
7. A cue that is pure garbage (recognizer noise, a credit line, an echo of instructions, sounds
   with no words) gets an **empty text**: `ID|`.

Output contract — checked by code, any violation rejects the whole chunk:

- Output **only** the lines `ID|text`, one per input line, with **exactly the same IDs in exactly the
  same order**. No line may be added, dropped, merged, split or duplicated.
- The text must not contain the character `|`.
- No commentary, no headers, no explanations, no code fences.

Example

Input:

    17|ну вот значит мы с вами сегодня поговорим про
    18|агентов в продакшене это как бы самое
    19|Сохраняй все повторы и паузы.

Output:

    17|Мы с вами сегодня поговорим про
    18|агентов в продакшене. Это самое
    19|
