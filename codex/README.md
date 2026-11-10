[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](README.ru.md)

# Plugins for Codex

Empty for now. The first plugin here will be `kb`, once its Claude Code version passes the newcomer test.

The Codex marketplace catalog will live in `.agents/plugins/marketplace.json` at the repository root (Claude's is in `.claude-plugin/`, so both catalogs coexist in one repository). The plugin core (base builder, search, MCP server) is shared by both systems; only the wrappers differ: model instructions and service descriptions.
