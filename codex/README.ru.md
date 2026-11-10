[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](README.ru.md)

# Плагины для Codex

Пока пусто. Первым сюда придёт плагин `kb` — после того как его версия для Claude Code пройдёт
проверку новичком.

Каталог магазина Codex будет лежать в `.agents/plugins/marketplace.json` в корне репозитория
(у Claude — в `.claude-plugin/`, поэтому оба каталога уживаются в одном репозитории).
Общее ядро плагина (сборщик базы, поиск, MCP-сервер) — одно на обе системы, различаются только
обёртки: инструкции модели и служебные описания.
