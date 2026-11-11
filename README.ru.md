[![English](https://img.shields.io/badge/lang-English-blue?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/lang-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-red?style=for-the-badge)](README.ru.md)

# Плагины Алексея Крола

Здесь лежат плагины для ИИ-ассистентов. Каждый плагин — в своей папке, у каждого своя
инструкция «как поставить и пользоваться».

| Плагин | Для чего | Где работает | Инструкция |
|---|---|---|---|
| **BestMentor** (`bestmentor`) — книга «Теория каст и ролей» как база знаний | Claude отвечает по книге: глава, цитата, «в книге этого нет» | Chat, Cowork и Code приложения Claude, терминал, VS Code | [claude-code/bestmentor](claude-code/bestmentor/README.md) |
| **pen** — редактор постов | Корректура текста и расшифровок на любом языке, перевод на американский английский, версии для X и LinkedIn | Chat, Cowork и Code приложения Claude, терминал, VS Code | [claude-code/pen](claude-code/pen/README.ru.md) |

## Разделы

- [`claude-code/`](claude-code/README.ru.md) — плагины для приложения Claude (Chat, Cowork, Code), терминала и VS Code.
- [`codex/`](codex/README.ru.md) — плагины для Codex (пока пусто).
- [`bots/`](bots/README.ru.md) — боты (пока пусто).

## Для того, кто ведёт этот репозиторий

- Папка `.claude-plugin/` в корне — **не плагин, а каталог магазина** (список плагинов со
  ссылками на их папки в `claude-code/`). Claude ищет каталог только в корне репозитория,
  поэтому перенести его в `claude-code/` нельзя. Файл — `.claude-plugin/marketplace.json`, имя магазина `krol`.
  Пользователь подключает его так: `claude plugin marketplace add alexeykrol/plugins`, а
  ставит плагин так: `claude plugin install <плагин>@krol`.
- Плагины разрабатываются в своих рабочих репозиториях; сюда выкладываются только проверенные
  версии (например, скрипт `scripts/sync-bestmentor.sh`).
- Всё системное — скиллы, манифесты, каталог, имена файлов — на английском. README — на
  английском, русская версия рядом (`README.ru.md`).
- Планы по витрине и страницам плагинов — [BACKLOG.md](BACKLOG.md).
