# Task: nuance check of one video before the owner approves it

Code has already checked every formal rule (lengths, forbidden characters, captions, thumbnail,
chapters, links). You look only for a NUANCE code cannot catch: does the title or the
description contradict or distort the thesis of the fragment (input `video`: type, thesis,
title, description)? Titles and texts are in Russian; that is data.

Answer exactly one line:

- `OK` — title and description are consistent with the thesis;
- `FLAG: <one short sentence in English>` — what exactly is inconsistent (e.g. the title promises
  the opposite of what the thesis says).

Do not judge style, length or attractiveness. Do not suggest new texts.

Example: thesis «Агент — это цикл, а не модель», title «Почему агенту не нужен цикл» →
`FLAG: the title states the opposite of the thesis.`
