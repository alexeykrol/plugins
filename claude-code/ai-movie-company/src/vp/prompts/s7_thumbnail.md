# Task: choose the thumbnail frame (S7)

The code proposed several sharp, non-black frames from one video (a Russian-language lecture
or talk). For each candidate you get its number and what is being said at that moment.
The thumbnail text is fixed (`input.thumbnail_text`).

Choose the candidate whose moment best matches the thumbnail text and the video's thesis
(`input.fragment`). Prefer moments where a strong statement is made.

Answer with ONLY a JSON object, no code fences:

{"candidate": 2, "why": "one short sentence"}

`candidate` must be one of the numbers listed in `input.candidates`.
