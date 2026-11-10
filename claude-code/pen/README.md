[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](README.ru.md)

# pen — post editor

A Claude plugin that gets posts ready for publishing. It currently does two things:

- **`/pen:corr`** — proofreads a draft or a voice-note transcript in any language, keeping it in that language. Removes filler words, repetitions and speech-recognition errors and turns speech into written text. Meaning, structure, numbering and the author's voice stay intact.
- **`/pen:tren`** — the full pipeline. Returns three ready-to-paste texts:
  1. **Source** — the proofread version in the original language;
  2. **X** — a full American English translation (long post, X Premium);
  3. **LinkedIn** — the English version fitted to the 3,000-character limit.

Translation runs in stages: first an exact transfer of meaning, then American style with a line-by-line check that no claim was lost or weakened, then the LinkedIn cut — connectives and repetitions first, theses last.

Write in any language: the source language is detected from the text, and the target is always American English. Proofreading is best for widely used languages; for rare ones, expect lower quality.

Every text comes as a plain-text block with a **Copy** button, so numbering and line breaks survive pasting into LinkedIn and X. Lengths are counted by script, not estimated. Notes and questions come in the language you write in.

**How to use:** type `/pen:tren` (or `/pen:corr`) and paste the text, or attach a file. Plain words work too: "translate this post into American English for LinkedIn".

**Requirements:** the Claude desktop app (<https://claude.ai/download>) and a paid Claude plan.

**What it does and doesn't do:** works only with the text you give it. Sends nothing to the internet and doesn't touch your files.

---

## Installation

Marketplace: `alexeykrol/plugins` (same as the other plugins).

> **Don't ask Claude in chat to "run the install commands".** In chat Claude has its own temporary cloud computer: the plugin would be installed there and disappear after the conversation.

### Option 1. Chat and Cowork

1. In the Claude app, open the left menu → **Customize** → **Plugins**.
2. **Add** → **Add marketplace**, enter `alexeykrol/plugins`. Skip this if the marketplace is already added.
3. **While the marketplace is private:** the app will ask you to connect GitHub — agree and grant access to the `alexeykrol/plugins` repository.
4. Find **pen** in the plugin list and click **Install**.
5. Start a **new conversation**, type `/pen:tren`, and paste your post.

### Option 2. Code (Code tab in the app, terminal, VS Code)

When creating a session in the Code tab, choose to run **on this computer (Local)**, not in the cloud.

```
/plugin marketplace add alexeykrol/plugins
/plugin install pen@krol
```

Or from the terminal:

```
claude plugin marketplace add alexeykrol/plugins
claude plugin install pen@krol
```

Restart the session after installing.
