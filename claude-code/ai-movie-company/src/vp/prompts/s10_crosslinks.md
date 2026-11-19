# Task: cross-links between the videos of one series

You plan which videos of a series should link to which, so a viewer who finishes one video
finds the next most useful one. The series is one recorded lecture (or interview): the FULL
recording plus fragments cut from it — short reels and long fragments.

You get the items of the series as a numbered list (input `items`). Each item has a number in
square brackets, its type (`full`, `reel`, `long`), its position in the recording (`order`, 1 =
earliest; the full recording has order 0), its title and its thesis. Titles and theses are in
Russian; that is data.

## What to return

Links as references to item NUMBERS. You never write addresses, identifiers or times — code
builds them.

- `kind` is one of:
  - `fragment` — from the full recording [0] to one of its fragments;
  - `next` — to the fragment that follows in the recording;
  - `prev` — to the fragment that precedes it;
  - `related` — to a fragment on a close topic (same thesis area, a continuation of the idea,
    a counter-point), wherever it is in the recording.
- Every fragment ALREADY links to the full recording automatically. Do not add links TO [0].
- At most 4 links from each fragment, at most 5 from the full recording [0].
- No link from an item to itself; no two links with the same `from` and `to`.
- Prefer quality over quantity: a link must help the viewer. Zero links from an item is allowed.
- From [0], prefer the strongest fragments (the ones whose thesis is most self-contained).

## Output format (exactly this JSON, nothing else)

```json
{"links": [
  {"from": 0, "to": 2, "kind": "fragment"},
  {"from": 1, "to": 2, "kind": "next"},
  {"from": 2, "to": 1, "kind": "prev"},
  {"from": 2, "to": 3, "kind": "related"}
]}
```

## Example of a good choice

Items: [0] full; [1] reel «Почему демо ломаются» (order 1); [2] long «Как устроен агент»
(order 2); [3] reel «Агент — это цикл, а не модель» (order 3).
[3] restates the core idea of [2] in a minute, so [3] → [2] is `related` (a viewer who liked the
short version gets the deep one), and [2] → [3] is `next`. [1] → [2] is `next`.
