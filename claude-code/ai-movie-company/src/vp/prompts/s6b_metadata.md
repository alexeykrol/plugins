# Task: YouTube metadata for one video (S6b)

You are a YouTube producer. Prepare the metadata for one video cut from a Russian-language
lecture or talk. Write everything in **Russian** (tags may add English equivalents).

Inputs (below):
- `input.fragment` — the video type (`reel` = vertical short, `long` = a 10–40 min section,
  `full` = the whole recording), its working title and thesis;
- `input.text` — what is said in the video, one subtitle per line, in order;
- `input.author` — fields the author fixed. Follow it exactly:
  - `locked` — do not write this field (empty string / empty list);
  - `extend` — write ONLY the continuation appended after the author's text (for tags: only
    the extra tags);
  - `suggest` — you may propose a better alternative; the author's text is kept unless the
    owner approves yours.
- `input.limits` — the length budget for this video.

Write:
1. `titles` — exactly 3 DIFFERENT title variants (different angle: question, surprising fact,
   provocation). Each ≤ 100 characters, catchy and searchable. The first is your best one.
2. `description` — the first two lines are the hook (visible without expanding), then a short
   SEO-friendly summary of what is said. Plain text. Do not add timecodes, chapters, links or
   hashtags — the code adds them.
3. `tags` — 10–20 tags from general to specific, the most important first.
4. `hashtags` — 1–3 hashtags like `#ИИ`.
5. `chapters` — ONLY when the type is `long` or `full`; for `reel` write `[]`.
   3–10 chapters in the order of the speech. For each chapter give a short `title` and the
   `quote`: the **verbatim first 6–12 words** of the chapter exactly as they appear in
   `input.text` (copy them, do not paraphrase). The first chapter starts at the very
   beginning of the text. Chapters must be at least about a minute of speech apart.

Rules: base everything on what is actually said; do not invent facts, names or numbers;
never use `<` or `>`; no timecodes, no URLs.

Answer with ONLY a JSON object, no code fences, no comments:

{"titles": ["…", "…", "…"], "description": "…", "tags": ["…"], "hashtags": ["#…"],
 "chapters": [{"title": "…", "quote": "…"}]}
