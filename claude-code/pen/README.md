[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](README.ru.md)

# pen — post editor

**For people who think and write in their own language but need to be heard in English** — on X, LinkedIn and other social networks.

## Who it's for

Experts, founders, creators and professionals whose native language isn't English. You write best in your own language: that's where your ideas are sharp. But your audience, clients and opportunities are on English-speaking X and LinkedIn.

## The problems it solves

**1. Translation that keeps the meaning.** Machine translation produces English that is technically correct and still says something else: claims get softened, idioms turn into nonsense, your voice disappears. pen translates the meaning first, then rewrites it in natural American English, then checks line by line that no claim was lost or weakened.

**Your voice stays yours.** pen removes noise, not character. If you write sharply, provocatively or with strong language, that is your choice: pen doesn't smooth it, soften it, add disclaimers or lecture you on tone. Profanity is translated into equally strong English, sarcasm stays sarcastic.

**2. Raw drafts and voice notes.** Many people dictate their thoughts. A transcript is full of filler, repetitions and recognition errors. pen first turns it into a clean written post in your own language, which you can publish as is.

**3. LinkedIn's 3,000-character limit.** pen fits the text into the limit by cutting filler and repetitions first and your ideas last. If the text can't fit without losing an idea, it splits it into Part 1/2, Part 2/2 instead of throwing content away. You also get the full text, so you can publish it as a LinkedIn article instead.

**4. X without Premium.** A free X account limits a post to 280 characters. pen gives you one English text already divided into numbered blocks of up to 280 characters: with Premium, paste it whole; without it, post it block by block as a thread. Blocks follow your own numbered points, so the thread reads as a logical sequence.

**5. Pasting without broken formatting.** Text copied from a chat often loses its numbering. pen delivers every text as a plain-text block with a **Copy** button: numbering and line breaks survive pasting into LinkedIn and X.

## What you get

Write `/pen:tren` and paste your post (or attach a file). You get three texts:

1. **Your language** — the proofread original, ready to publish for your native audience.
2. **X** — the full American English version, split into thread blocks of up to 280 characters.
3. **LinkedIn** — the English version within 3,000 characters (or in parts, if it can't fit without losing an idea).

Plus a few notes: what was cleaned up, how tricky idioms were translated, what was shortened, and a stronger opening line if yours is weak.

Need only the cleanup, without translation? Use `/pen:corr`.

Write in any language: the source language is detected from the text, and the target is always American English. Notes and questions come in the language you write to Claude in. Proofreading is best for widely used languages; for rare ones, expect lower quality. Lengths are counted by script, using X's own rules for the 280-character limit.

**See it in action:** my English posts on X are written with pen — [@old_alexeykrol](https://x.com/old_alexeykrol). Compare the result with what you'd get from a machine translator.

**Requirements:** the Claude desktop app (<https://claude.ai/download>) and a paid Claude plan.

**Privacy:** works only with the text you give it. Sends nothing to the internet and doesn't touch your files. About once in ten runs it adds a one-line credit with links to the author and the feedback form — outside the texts you copy.

## Feedback

Something to add, improve, change or remove? [Open a feedback issue](https://github.com/alexeykrol/plugins/issues/new?template=pen-feedback.yml) — any language is fine. Please don't send pull requests: changes are made by the author. Version history: [CHANGELOG](CHANGELOG.md), plans: [BACKLOG](BACKLOG.md).

---

## Installation

Marketplace: `alexeykrol/plugins`.

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
