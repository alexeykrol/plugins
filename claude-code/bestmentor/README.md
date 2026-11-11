[![English](https://img.shields.io/badge/lang-English-blue)](README.md) [![Русский](https://img.shields.io/badge/lang-Русский-lightgrey)](README.ru.md)

# BestMentor — the book «Теория каст и ролей» (Theory of Castes and Roles) as a knowledge base for Claude

This plugin gives Claude a ready-made knowledge base built from Alexey Krol's book «Теория каст и
ролей» (Theory of Castes and Roles, 12 chapters). Once it's installed, Claude answers questions
**from the book**: it names the chapter, gives a quote, and honestly tells you when the book has no
answer. There is nothing to build or set up — the knowledge base is already inside the plugin.

The book is in Russian, so you ask your questions in Russian. Example questions:
- Как перейти из одной касты в другую? (How do you move from one caste to another?)
- Чем отличаются роли Специалиста и Предпринимателя? (How do the roles of Specialist and Entrepreneur differ?)
- Что автор говорит о пассивном доходе? (What does the author say about passive income?)

**What you need:** the Claude desktop app (<https://claude.ai/download>) and a paid Claude plan.
The browser version works too (Option 1 below), but it was tested in the app.

**What the plugin does and doesn't do:** it searches its own knowledge base with a small script
that comes with it. It sends nothing to the internet and doesn't touch your files. The only thing
it writes is your answers about "what is correct", saved to the folder `~/.kb/tcr-book/` on your
computer.

---

## Installation

The Claude app has three modes — the **Chat**, **Cowork** and **Code** tabs. The plugin is
installed one way in Chat and Cowork and another way in Code. If you use all of them, install it
both ways.

> **Don't ask Claude in chat to "run the install commands".** In chat Claude has its own temporary
> cloud computer: the plugin would be installed there and disappear after the conversation.

### Option 1. Chat and Cowork (a regular conversation with Claude)

1. In the Claude app, open the left menu and click **Customize**.
2. Choose **Plugins**.
3. Click **Add** → **Add marketplace**.
4. Enter the marketplace address `alexeykrol/plugins` and confirm.
5. The marketplace is public, so usually there is nothing to do here. **If the app does ask** you
   to connect GitHub (**Connect GitHub**) or install the **Claude GitHub App**, agree and give
   access to the `alexeykrol/plugins` repository.
6. Find **bestmentor** (BestMentor) in the marketplace's plugin list and click **Install**.
7. Start a **new conversation** and type:

   ```
   проверь плагин bestmentor
   ```

   (This means "check the bestmentor plugin".) Claude will check that the knowledge base is in place and
   show you an example question and answer. After that, just ask questions about the book.

**If the marketplace won't add**, there is a backup
option: download the skill file [`dist/kb-ask.zip`](dist/kb-ask.zip) (on the file's page, use the
"Download raw file" button), then in the app go to **Customize → Skills → Upload** and choose that
file. It works the same way, just without the `проверь плагин bestmentor` check: start asking questions
right away.

### Option 2. Code (the Code tab in the app, the terminal, VS Code)

One installation works in all three places at once.

1. Open the **Code** tab in the Claude app (or the terminal, or Claude in VS Code).
   When creating a session in the Code tab, choose to run it **on this computer (Local)**, not in
   the cloud (Cloud). In a cloud session the plugin would be installed on a temporary machine and
   disappear along with it.
2. Send Claude this text:

   ```
   Установи плагин bestmentor из магазина alexeykrol/plugins. Для этого выполни в терминале две команды:
   claude plugin marketplace add alexeykrol/plugins
   claude plugin install bestmentor@krol
   Когда закончишь, скажи мне одной фразой, получилось ли, и что делать дальше.
   ```

   (In English: "Install the bestmentor plugin from the alexeykrol/plugins marketplace. To do this, run
   these two commands in the terminal: … When you're done, tell me in one sentence whether it
   worked and what to do next.")

   Claude will ask for permission to run the commands — click **Allow**.
3. Start a **new session** and type `проверь плагин bestmentor`.

You can also type the same thing into the terminal yourself:

```bash
claude plugin marketplace add alexeykrol/plugins
claude plugin install bestmentor@krol
```

---

## How to use it

Ask about the book in your own words. If Claude doesn't think to look in the knowledge base on its
own, add «по книге» ("from the book") or «спроси базу» ("ask the base"):

```
по книге: что такое функциональная роль?
```

(This means "from the book: what is a functional role?")

If the book says one thing and Claude "knows" something else, it will show you both versions and
ask you which one is correct. In Code and Cowork your answer is remembered; **in Chat it isn't**
(every conversation there starts in a clean sandbox).

In Code, Claude may ask for permission to run the plugin's search script — that is the search
through the book, so allow it.

## If something goes wrong

| What you see | What to do |
|---|---|
| After installing, Claude doesn't know about the plugin | Start a **new** conversation — the plugin is loaded at the start |
| The marketplace won't add in Chat/Cowork | Connect GitHub when the app asks, or use the backup option with `kb-ask.zip` |
| In Code: an "Install developer tools" window | Click "Install", wait for it to finish, and try again. This is needed only once |
| Claude answers "from memory", without chapters or quotes | Add «по книге» ("from the book") to your question |

## Uninstall

- Chat and Cowork: **Customize → Plugins** → bestmentor → **Uninstall**.
- Code: type to Claude `выполни claude plugin uninstall bestmentor@krol` (this means "run claude plugin
  uninstall bestmentor@krol").

## Support

Questions and problems: support@alexeykrol.com (any language).
