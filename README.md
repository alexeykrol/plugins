[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](README.ru.md)

# Alexey Krol's plugins

Plugins for AI assistants. Each plugin lives in its own folder with its own install and usage guide.

| Plugin | What it does | Where it works | Guide |
|---|---|---|---|
| **kb** — the book "Theory of Castes and Roles" as a knowledge base | Claude answers from the book: chapter, quote, or "the book doesn't cover this" | Chat, Cowork and Code in the Claude app, terminal, VS Code | [claude-code/kb](claude-code/kb/README.md) |
| **pen** — post editor | Proofreads drafts and transcripts in any language, translates them into American English, fits them to X and LinkedIn | Chat, Cowork and Code in the Claude app, terminal, VS Code | [claude-code/pen](claude-code/pen/README.md) |

## Sections

- [`claude-code/`](claude-code/) — plugins for the Claude app (Chat, Cowork, Code), terminal and VS Code.
- [`codex/`](codex/) — plugins for Codex (empty for now).
- [`bots/`](bots/) — bots (empty for now).

## For the maintainer

- The root `.claude-plugin/` folder is **not a plugin but the marketplace catalog** (a list of plugins pointing to their folders in `claude-code/`). Claude looks for the catalog only at the repository root, so it can't move into `claude-code/`. File: `.claude-plugin/marketplace.json`, marketplace name `krol`. Users add it with `claude plugin marketplace add alexeykrol/plugins` and install a plugin with `claude plugin install <plugin>@krol`.
- Plugins are developed in their own working repositories; only tested versions are published here (e.g. `scripts/sync-kb.sh`).
- Everything system-level — skills, manifests, catalog, file names — is in English. READMEs are in English with a Russian version next to them (`README.ru.md`).
- Storefront and plugin-page plans: [BACKLOG.md](BACKLOG.md).
