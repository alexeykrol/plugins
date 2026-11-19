# Task: design texts for one video (S6a)

You write the short on-screen texts for one video cut from a Russian-language lecture or talk.
Write every text in **Russian**.

Inputs (below):
- `input.fragment` — the video type (`reel` = vertical short 30–90 s, `long` = a 10–40 min
  section, `full` = the whole recording), its working title and thesis;
- `input.text` — what is said in the video, one subtitle per line, in order;
- `input.author` — fields the author fixed. Follow it exactly:
  - `locked` — do not write this field (leave it out or write an empty string);
  - `extend` — write ONLY the continuation that will be appended after the author's text
    (never repeat or rephrase the author's text);
  - `suggest` — you may propose a better alternative; the author's text is kept unless the
    owner approves yours.

Write:
1. `intro_text` — the opening card shown for 1.5–3 s before the video: a hook that makes the
   viewer stay. Max 80 characters. No quotes, no emoji.
2. `announce_text` — the closing card: announce the series / what to watch next
   (e.g. «Полная лекция — на канале»). Max 140 characters.
3. `thumbnail_text` — text on the thumbnail: **at most 5 words**, max 40 characters,
   readable at a glance, no final period.

Rules: base everything on what is actually said in `input.text`; do not invent facts,
names or numbers; never use the characters `<` or `>`; no timecodes, links or hashtags.

Answer with ONLY a JSON object, no code fences, no comments:

{"intro_text": "…", "announce_text": "…", "thumbnail_text": "…"}

Example of a good answer:

{"intro_text": "Почему красивые демо ломаются на первом клиенте", "announce_text": "Полная лекция про агентов в производстве — на канале", "thumbnail_text": "Демо ломается на клиенте"}
