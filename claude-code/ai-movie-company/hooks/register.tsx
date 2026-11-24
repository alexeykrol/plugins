// The editing director's presence in the interface.
//
// While the director is "on stage" in a session, the person can see it: replies open with her
// name and picture, her first reply opens with a welcome splash, a band above the prompt says
// she is here (her face moving and showing her mood, a progress bar while she works) and holds
// a Leave button, the work indicator speaks her words, and the app takes the warm "Elsa's room"
// look. The model gets the one-page contract (persona/contract.md) with the first command that
// calls her, and in the system prompt after a compaction.
//
// The words on the interface are Russian or English: the system language the first time the
// director is called, then the language of the conversation (see noteLang).
//
// The stage is per session. It is entered by the plugin's conversational commands and skills
// (persona.ts: STAGE_COMMANDS), by /director, by /elsa (which also runs the guided start) or by a
// message that only calls her by name ("Эльза", "Привет, Эльза"), which turns into the guided
// start too. It is left by the Leave button or /director-off. The technical commands (doctor,
// learn) neither enter nor leave it. Other sessions stay plain.
//
// A document the person is asked to approve (a brief, a screenplay, a plan) is shown in a pane
// beside the chat: the model calls the mod's tool `show_document` before it asks, the pane draws
// one large line saying what to do, the document, and the answer buttons; a press goes to the
// chat as the person's own message, so the skills receive «Утверждаю» exactly as if typed.
//
// The channel to the person: at a session's start the mod sends the engine's telemetry in the
// background (`vp telemetry flush --detach`) and asks for news (`vp messages poll --json`): messages
// from the plugin's authors, a newer version already downloaded by an update ("quit Claude and open
// it again"), the reminder to turn auto-update on. A notice is shown — a toast and a line above the
// prompt — only while nothing runs: no turn, no document waiting for an answer, no open question,
// and `vp busy` false (no render, upload or generation; a restart would kill it). Otherwise it waits
// and is looked at again when a turn ends.
//
// A hand step outside the chat (quit and reopen Claude, quit CapCut, open a site, copy a key) is
// never plain chat text: the model calls the mod's tool `show_action`, and a framed card leads the
// band above the prompt (a pane where no band is drawn) with a «👉 ОДНО ДЕЙСТВИЕ» mark, the one
// action, how to do it, the look-alike not to touch (❌), what comes after, the check «Вы поняли,
// что нужно сделать именно это?» and its buttons. A restart card is kept in `$.store`; when Claude
// opens again within a day a welcome-back card picks the work up from that place.
//
// The mod never touches the engine's rules: gates, refusals and the human-answer guard live in
// the skills, the commands and src/vp.

import { atom, read, update } from 'claude-code'
import type {
  CommandRunResult,
  EngineInterface,
  Register,
  ThemeSetArgs,
  Timer,
  ToolCallResult,
  UiOpenResult,
} from 'claude-code'

import type {
  ActionCard,
  BackgroundTask,
  ChannelNotice,
  DirectorLang,
  DirectorMood,
  LiveProgress,
  PendingRestart,
  TurnWork,
  WelcomeCard,
  WorkTask,
} from '../types'

import { faceAt, FRAME_MS, MOOD_WORDS } from './faces'
import { makeColors } from './make-colors.js'
import {
  ACCENT,
  ACTION_UNCLEAR,
  ACTION_CHECK,
  ACTION_MARK,
  ACTION_NOT_MARK,
  ACTION_RESTART_NOTE,
  AVATAR,
  AVATAR_TEXT,
  BACK_NOTE,
  BAND_EDGE,
  BAND_INK,
  BAND_INK_SOFT,
  bandColor,
  buildInfoOf,
  CALLED_NOTE,
  CONTINUE,
  continueText,
  CONTRACT_LEAD,
  DOC_BUTTONS,
  DOC_EMPTY,
  DOC_GLANCE,
  docInstructionText,
  docSentText,
  backgroundText,
  clockText,
  ELSA_TEXT,
  etaText,
  HERE_TEXT,
  isNameCall,
  LEAVE,
  LEFT_NOTE,
  LEFT_TEXT,
  LEFT_TOAST,
  LOOK_INPUT,
  NAME,
  NAME_CALL_NOTE,
  NOT_ON_STAGE_TEXT,
  NOTICE_DISMISS,
  NO_BUILD,
  ON_STAGE,
  releaseLabel,
  REMINDER_LATER,
  REMINDER_ON,
  REMINDER_TEXT,
  RESTART_LATER,
  RESTART_NOW,
  restartAskNote,
  resumeNote,
  SPINNER_WORDS,
  STAGE_COMMANDS,
  START_FALLBACK,
  STEP_WORDS,
  stepOfText,
  SWITCHED_OFF_NOTE,
  SWITCHED_OFF_TEXT,
  THINKING_LIVE,
  updateReadyText,
  VERSION,
  warmHue,
  WARM_HUES,
  WELCOME_BACK,
} from './persona'
import type { BuildInfo } from './persona'
import { SPLASH, SPLASH_ALT, SPLASH_HEIGHT, SPLASH_WIDTH } from './splash'

const IDLE_WORK: TurnWork = { isRunning: false, startedAt: null, label: null, tasks: [] }

const onStage = atom({ plugin: 'ai-movie-company', key: 'onStage' } as const, false)
const lang = atom({ plugin: 'ai-movie-company', key: 'lang' } as const, 'en')
const tint = atom({ plugin: 'ai-movie-company', key: 'tint' } as const, WARM_HUES.from)
const progress = atom({ plugin: 'ai-movie-company', key: 'progress' } as const, null)
const mood = atom({ plugin: 'ai-movie-company', key: 'mood' } as const, 'idle')
const beat = atom({ plugin: 'ai-movie-company', key: 'beat' } as const, 0)
const work = atom({ plugin: 'ai-movie-company', key: 'work' } as const, IDLE_WORK)
const background = atom({ plugin: 'ai-movie-company', key: 'background' } as const, [] as BackgroundTask[])
const doc = atom({ plugin: 'ai-movie-company', key: 'doc' } as const, null)
const notice = atom({ plugin: 'ai-movie-company', key: 'notice' } as const, null)
const action = atom({ plugin: 'ai-movie-company', key: 'action' } as const, null)
const welcome = atom({ plugin: 'ai-movie-company', key: 'welcome' } as const, null)

/** `$.store` keys: what outlives the session's process. */
const STORE_SESSIONS = 'on-stage-sessions'
const STORE_LANG = 'lang'
/** The restart card kept across the restart it asks for (PendingRestart). */
const STORE_RESTART = 'pending-restart'
/** How many sessions' "on stage" marks are kept; the oldest go first. */
const MAX_SESSIONS = 50
/** How many replies' first sightings are kept in this process. */
const MAX_SEEN = 2000
/** How long a task or tool label may be on the bar. */
const MAX_LABEL = 80

const LOOK: ThemeSetArgs = { colors: makeColors(LOOK_INPUT).theme }

// This process's own notes; a hot reload starts them over, which costs at most one more
// copy of the contract in the conversation (and one more splash).
let contractText: string | undefined
let buildHeld: BuildInfo | undefined
let isContractSent = false
/** False until the words' language came from the store, the system or the conversation. */
let isLangKnown = false
/** Reply id -> whether the director was on stage when the reply was first drawn. */
const seenReplies = new Map<string, boolean>()
/** The band's clock while the director is on stage: colour drift and the engine's progress. */
let ticker: Timer | undefined
/** The face's clock while the director is on stage: one frame every FRAME_MS. */
let faceClock: Timer | undefined
/** True from the moment she is called until her next reply is drawn: that reply gets the splash. */
let isSplashDue = false
/** True once the splash was given in this session: it is shown once. */
let isSplashGiven = false
/** The reply that carries the splash (it keeps it on every redraw). */
let splashReply: string | undefined

/** How long after /elsa answers the guided start is queued. */
const START_DELAY_MS = 30
/** How often the band looks at the engine's progress and moves its colour. */
const TICK_MS = 2000
/** How long after a session starts the channel is asked (the session settles first). */
const CHANNEL_DELAY_MS = 1500
/** How often a waiting notice is looked at again (each look may ask `vp busy`). */
const RECHECK_MS = 30_000
/** How often the mod asks for news; the engine itself asks the network at most every 6 hours. */
const REPOLL_MS = 30 * 60_000
/** `$.store` key: the version whose "restart Claude" notice was shown (once per version). */
const STORE_RESTART_SHOWN = 'restart-shown'
/** The engine's mirror of the running step, in the pipeline home. */
const LIVE_FILE = 'progress-live.json'
/** A mirror older than this belongs to a run that died: nothing is running. */
const STALE_MS = 120_000

/** The pane beside the chat that shows a document to read and answer. */
const DOC_PANE = 'doc'
/** The model's tool that fills that pane: `mcp__<plugin>__show_document`. */
const DOC_TOOL = 'show_document'
/** The width the pane asks for when docked beside the transcript (the surface caps it). */
const DOC_COLUMNS = 80
/** How much of a document the pane draws (one drawing holds 100000 characters in all). */
const MAX_DOC_CHARS = 90_000
const MAX_DOC_BUTTONS = 4
const MAX_BUTTON_LABEL = 40
const MAX_DOC_TITLE = 60

const DOC_TOOL_DESCRIPTION = [
  'Shows a document the person must read (a brief, a screenplay, a storyboard plan, any .md or .txt file)',
  'in a pane beside the chat, with answer buttons inside the pane. A press arrives in the chat as the',
  "person's own message carrying the button's label, exactly as if they typed it.",
  'Call it EVERY time before you ask the person to approve or review a document; never ask "approve?"',
  'about a document they cannot see. When the result says the pane is shown, write ONE line in the chat',
  'as a markdown heading that says where to look and what to press, e.g.',
  '«## Бриф открыт справа → прочитайте и нажмите «Утверждаю» внизу панели», and do not paste the document.',
  'When the result says the pane is NOT shown, paste the document into the chat yourself.',
].join(' ')

const DOC_TOOL_SCHEMA = {
  type: 'object',
  properties: {
    file_path: { type: 'string', description: 'Absolute path of the .md or .txt document to show.' },
    title: {
      type: 'string',
      description: "Short pane title in the person's language, e.g. «Бриф» or «Сценарий».",
    },
    buttons: {
      type: 'array',
      items: { type: 'string' },
      minItems: 1,
      maxItems: MAX_DOC_BUTTONS,
      description:
        'Answer button labels, the approving one first. Default: «Утверждаю», «Хочу поправить» ' +
        '(English conversation: "I approve", "I want changes").',
    },
  },
  required: ['file_path', 'title'],
  additionalProperties: false,
} as const

/** The pane that holds the action card where the surface draws no band (vscode). */
const ACTION_PANE = 'action'
/** The model's tools that put up and take down the action card. */
const ACTION_TOOL = 'show_action'
const CLEAR_ACTION_TOOL = 'clear_action'
const ACTION_COLUMNS = 60
const MAX_ACTION_TITLE = 80
const MAX_ACTION_HOW = 300
const MAX_ACTION_NOTE = 200
/** The model's own buttons, each naming the action; the mod adds «Не понял(а), покажите» after them. */
const MAX_ACTION_BUTTONS = 2
/** A key combination in a step's words (⌘Q, Cmd + Q, Ctrl+W, Command-Tab): never a way for a novice. */
const KEY_COMBO = /[⌘⌃⌥]|\b(cmd|command|ctrl|control|option|alt)\s*[+\-]\s*\S/i
/** How long a restart card waits for Claude to be opened again. */
const RESTART_TTL_MS = 24 * 60 * 60 * 1000

const ACTION_TOOL_DESCRIPTION = [
  'Shows ONE hand step the person must do outside this chat on a framed, highlighted card above the',
  'prompt (where their eye is): a large «👉 ОДНО ДЕЙСТВИЕ» mark, the action in bold, how to do it, the',
  'most likely mistake marked ❌, what comes after, the check «Вы поняли, что нужно сделать именно',
  'это?», your buttons and «Не понял(а), покажите». A press arrives in the chat as the person\'s own',
  'message carrying the label. Call it EVERY time before you ask the person to do anything by hand',
  'outside the chat: opening a site, signing in, copying a key or a code, finding a file, closing a',
  'window, and anything that can break the session or lose work. A chat message alone is NEVER enough',
  'for those: a novice told to quit Claude with a key combination quit her web browser and lost',
  'everything. Rules: `how` points at something VISIBLE on screen (a button, a field, a menu by its',
  'words), NEVER at a key combination; `not_this` names in advance the most likely mistake for this',
  'very step; every button names the action («Понятно: копирую ключ»), never a bare «OK». Restarting',
  'Claude and quitting CapCut are done by the engine on a button, not by the person\'s keys. One card =',
  'one action; never chain two actions in one card or one message. Use kind "restart" for a step after',
  'which Claude closes and opens again: the card is remembered and a welcome-back card lets the person',
  'continue from this place. After calling it, do not give the next step until the person answers.',
].join(' ')

const ACTION_TOOL_SCHEMA = {
  type: 'object',
  properties: {
    title: {
      type: 'string',
      description: "The one action, short, in the person's language, e.g. «Скопируйте ключ ElevenLabs».",
    },
    how: {
      type: 'string',
      description:
        'How exactly to do it, pointing at something visible on screen, e.g. «Нажмите кнопку «Copy» справа ' +
        'от ключа на открытой странице». Never a key combination.',
    },
    not_this: {
      type: 'string',
      description:
        'The most likely mistake for this step, named in advance, e.g. «Не нажимайте «Delete» рядом с ' +
        'ключом» or «Окно браузера не закрывайте». Give it whenever something nearby can be mistaken ' +
        'for the target (another app, another window, another button).',
    },
    after: {
      type: 'string',
      description: 'What the person will see or do afterwards, e.g. «Вставьте ключ сюда, в чат — дальше я сама».',
    },
    kind: {
      type: 'string',
      enum: ['restart', 'step'],
      description:
        '"restart" when Claude closes and opens again after this step (it is remembered across the ' +
        'restart); "step" otherwise (default).',
    },
    buttons: {
      type: 'array',
      items: { type: 'string' },
      minItems: 1,
      maxItems: MAX_ACTION_BUTTONS,
      description:
        "Button labels in the person's language that name the action, e.g. «Понятно: копирую ключ» " +
        '(English: "Got it: copying the key"). The card adds «Не понял(а), покажите» after them by itself.',
    },
  },
  required: ['title', 'how', 'buttons'],
  additionalProperties: false,
} as const

const CLEAR_ACTION_TOOL_DESCRIPTION =
  'Takes the action card (show_action) down from above the prompt, e.g. once the step is done or no ' +
  'longer needed. A press on one of its buttons takes it down by itself.'

const CLEAR_ACTION_TOOL_SCHEMA = { type: 'object', properties: {}, additionalProperties: false } as const

/** The time now, or null on a host that keeps no clock. */
async function nowOf($: EngineInterface): Promise<number | null> {
  try {
    return await $.clock.now()
  } catch {
    return null
  }
}

function isLang(value: unknown): value is DirectorLang {
  return value === 'ru' || value === 'en'
}

/**
 * The language a text is written in, or undefined when it has too few letters to say.
 * Code, paths and links are not prose; a Russian text keeps Latin names (YouTube, Claude),
 * so Cyrillic wins from a third of the letters up.
 */
function langOf(text: string): DirectorLang | undefined {
  const prose = text.replace(/```[\s\S]*?```|`[^`]*`|\S*[\/\\]\S*/g, ' ')
  const cyrillic = prose.match(/[а-яё]/gi)?.length ?? 0
  const latin = prose.match(/[a-z]/gi)?.length ?? 0

  if (cyrillic >= 3 && cyrillic * 2 >= latin) {
    return 'ru'
  }

  return latin >= 3 ? 'en' : undefined
}

/** `ai-movie-company:start` -> `start`, for a name that is this plugin's own. */
function ownShortName(name: string, plugin: string): string | undefined {
  return name.startsWith(`${plugin}:`) ? name.slice(plugin.length + 1) : undefined
}

function isStageName(shortName: string | undefined): boolean {
  return shortName !== undefined && STAGE_COMMANDS.includes(shortName)
}

function hashOf(text: string): number {
  let hash = 0

  for (const char of text) {
    hash = (hash * 31 + (char.codePointAt(0) ?? 0)) % 100003
  }

  return hash
}

function clip(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim()

  return flat.length > MAX_LABEL ? `${flat.slice(0, MAX_LABEL - 1)}…` : flat
}

async function isSwitchedOff($: EngineInterface): Promise<boolean> {
  const value = (await $.env.get('AI_MOVIE_COMPANY_DIRECTOR'))?.trim().toLowerCase()

  return value === 'off' || value === '0' || value === 'false'
}

/**
 * The plugin's build.json (stage, build number, release time), read once. No file, a broken one or
 * an install older than build.json: the release line is the version and "alpha" alone.
 */
async function buildOf($: EngineInterface): Promise<BuildInfo> {
  if (buildHeld === undefined) {
    try {
      buildHeld = buildInfoOf(JSON.parse(await $.fs.read(`${$.plugin.root}/build.json`)))
    } catch {
      buildHeld = NO_BUILD
    }
  }

  return buildHeld
}

/** The contract page, read once; empty when the file is not there. */
async function contractOf($: EngineInterface): Promise<string> {
  if (contractText === undefined) {
    try {
      contractText = (await $.fs.read(`${$.plugin.root}/persona/contract.md`)).trim()
    } catch {
      contractText = ''
    }
  }

  return contractText
}

async function storedSessions($: EngineInterface): Promise<string[]> {
  const held = await $.store.get(STORE_SESSIONS)

  return Array.isArray(held) ? held.filter(one => typeof one === 'string') : []
}

/** Keeps or drops this session's "on stage" mark, so a restarted session comes back as it was. */
async function remember($: EngineInterface, isOn: boolean): Promise<void> {
  const id = await $.session.id()
  const others = (await storedSessions($)).filter(one => one !== id)
  await $.store.set(STORE_SESSIONS, (isOn ? [...others, id] : others).slice(-MAX_SESSIONS))
}

async function isRemembered($: EngineInterface): Promise<boolean> {
  return (await storedSessions($)).includes(await $.session.id())
}

/** The look and, where no band is drawn, a status line with the name. */
async function paint($: EngineInterface): Promise<void> {
  startTicker($)
  await $.theme.set(LOOK)
  const surfaces = await $.session.surfaces()
  const hasBand = surfaces.some(one => one === 'terminal' || one === 'desktop' || one === 'mobile')

  if (surfaces.length > 0 && !hasBand) {
    $.ui.status(NAME[await read($, lang)])
  }
}

/** The pipeline home, as the engine finds it: VP_HOME, else ~/.ai_video_pipeline. */
async function pipelineHome($: EngineInterface): Promise<string | undefined> {
  const set = (await $.env.get('VP_HOME'))?.trim()

  if (set !== undefined && set !== '') {
    return set
  }
  const home = (await $.env.get('HOME'))?.trim()

  return home === undefined || home === '' ? undefined : `${home}/.ai_video_pipeline`
}

/** The engine's running step, or null: no file, a broken one, or one of a run that died. */
async function readProgress($: EngineInterface): Promise<LiveProgress | null> {
  const home = await pipelineHome($)

  if (home === undefined) {
    return null
  }

  try {
    const held: unknown = JSON.parse(await $.fs.read(`${home}/${LIVE_FILE}`))

    if (typeof held !== 'object' || held === null) {
      return null
    }
    const rec = held as Record<string, unknown>
    const updated = typeof rec.updated_at === 'string' ? Date.parse(rec.updated_at) : NaN

    if (typeof rec.step !== 'string' || !Number.isFinite(updated) || (await $.clock.now()) - updated > STALE_MS) {
      return null
    }
    const percent = typeof rec.percent === 'number' && Number.isFinite(rec.percent) ? rec.percent : 0
    const etaS = typeof rec.eta_s === 'number' && Number.isFinite(rec.eta_s) ? rec.eta_s : null

    return { step: rec.step, percent: Math.min(Math.max(percent, 0), 100), etaS }
  } catch {
    return null
  }
}

/** One beat of the band: its colour drifts within the warm range, the bar follows the engine. */
async function tick($: EngineInterface): Promise<void> {
  if (!(await read($, onStage))) {
    return
  }
  const hue = warmHue((await $.clock.now()) / 1000)

  if (hue !== (await read($, tint))) {
    await update($, tint, () => hue)
  }
  try {
    await pruneBackground($)
  } catch {
    // Bookkeeping only: the engine's bar is looked at all the same.
  }
  const next = await readProgress($)
  const now = await read($, progress)

  if (JSON.stringify(next) !== JSON.stringify(now)) {
    await update($, progress, () => next)
  }
}

/** One beat of the face: the next frame of her current mood. */
async function faceTick($: EngineInterface): Promise<void> {
  if (await read($, onStage)) {
    await update($, beat, value => (value + 1) % 840)
  }
}

/** Starts the band's and the face's clocks once per process; a hot reload starts them again. */
function startTicker($: EngineInterface): void {
  if (ticker === undefined) {
    try {
      ticker = $.clock.every(TICK_MS, () => {
        tick($).catch(() => undefined)
      })
    } catch {
      ticker = undefined
    }
  }

  if (faceClock === undefined) {
    try {
      faceClock = $.clock.every(FRAME_MS, () => {
        faceTick($).catch(() => undefined)
      })
    } catch {
      faceClock = undefined
    }
  }
}

function stopTicker(): void {
  ticker?.cancel()
  ticker = undefined
  faceClock?.cancel()
  faceClock = undefined
}

/** Sets the language of the interface words and keeps it for the next sessions. */
async function setLang($: EngineInterface, next: DirectorLang): Promise<void> {
  isLangKnown = true

  if (next !== (await read($, lang))) {
    await update($, lang, () => next)
  }

  if ((await $.store.get(STORE_LANG)) !== next) {
    await $.store.set(STORE_LANG, next)
  }
}

/**
 * The person's system language: macOS preferences first, the locale variables next.
 * Asked once per computer, the first time the director is called with no language kept.
 */
async function systemLang($: EngineInterface): Promise<DirectorLang | undefined> {
  try {
    const asked = await $.process.run(['defaults', 'read', '-g', 'AppleLanguages'], { timeoutMs: 3000 })
    const first = asked.exitCode === 0 ? /[a-z]{2}/i.exec(asked.stdout)?.[0] : undefined

    if (first !== undefined) {
      return first.toLowerCase() === 'ru' ? 'ru' : 'en'
    }
  } catch {
    // Not macOS, or a host without commands: the locale variables are next.
  }

  // Literal names: the engine lists the variables a module reads.
  const locales = [await $.env.get('LC_ALL'), await $.env.get('LC_MESSAGES'), await $.env.get('LANG')]

  for (const locale of locales) {
    const value = locale?.trim().toLowerCase()

    if (value !== undefined && /^[a-z]{2}([_.-]|$)/.test(value)) {
      return value.startsWith('ru') ? 'ru' : 'en'
    }
  }

  return undefined
}

/** Puts the director on stage; false when she already was, or the switch holds her off. */
async function enter($: EngineInterface): Promise<boolean> {
  if ((await read($, onStage)) || (await isSwitchedOff($))) {
    return false
  }

  if (!isLangKnown) {
    isLangKnown = true
    const system = await systemLang($)

    if (system !== undefined) {
      await setLang($, system)
    }
  }
  await update($, onStage, () => true)
  await remember($, true)
  await paint($)

  // Called now, by a command or by name: her next reply opens with the welcome splash, once.
  if (!isSplashGiven) {
    isSplashDue = true
  }

  return true
}

/** Sends the director away, on the person's ask alone; false when she was not on stage. */
async function leave($: EngineInterface): Promise<boolean> {
  if (!(await read($, onStage))) {
    return false
  }
  await update($, onStage, () => false)
  await remember($, false)
  // The session has one theme and this clears it whoever set it: only here, on the person's ask.
  await $.theme.clear()
  $.ui.status(undefined)
  stopTicker()
  isSplashDue = false
  await update($, progress, () => null)

  return true
}

/** The Leave button: the model learns of it from a row the person does not see. */
async function leaveByButton($: EngineInterface): Promise<void> {
  const words = await read($, lang)

  if (await leave($)) {
    $.ui.toast(LEFT_TOAST[words])
    await $.session.append({
      message: { type: 'user', content: [{ type: 'text', text: LEFT_NOTE }] },
    })
  }
}

/** What the model must be told now, once the director is (or stays) on stage. */
async function noteForModel($: EngineInterface, hasEnteredNow: boolean): Promise<string | undefined> {
  if (!isContractSent) {
    const contract = await contractOf($)

    if (contract !== '') {
      isContractSent = true

      return `${CONTRACT_LEAD}\n\n${contract}`
    }
  }

  return hasEnteredNow ? BACK_NOTE : undefined
}

/**
 * The interface words follow the conversation. What the person types moves them to Russian at
 * once and never to English ("ok", "yes", a path say nothing); the director's own answer, which
 * is in the person's language by her contract, settles it either way.
 */
async function noteLang($: EngineInterface, text: string, isAnswer: boolean): Promise<void> {
  const seen = langOf(text)

  if (seen === 'ru' || (seen === 'en' && isAnswer)) {
    await setLang($, seen)
  }
}

/**
 * Runs the plugin's guided start as if the person typed it, from a timer: the engine refuses a
 * `$.command.run` made inside a `command.run` hook (it would wait on the turn that hook holds),
 * and runs one made after the hook returned once the session is idle, after /elsa's own line.
 * A headless run (`claude -p "/elsa"`) ends before the timer fires; /elsa is for a person.
 * When the command cannot be queued, the model is asked to run the start itself.
 */
function queueStart($: EngineInterface, args: string): void {
  const fallback = () => {
    $.prompt.submit({ text: START_FALLBACK }).catch(() => undefined)
  }

  try {
    $.clock.after(START_DELAY_MS, () => {
      $.command.run({ command: `${$.plugin.name}:start`, args }).catch(fallback)
    })
  } catch {
    fallback()
  }
}

/** /elsa: Elsa comes on stage and the guided start begins. */
async function callElsa($: EngineInterface, args: string): Promise<CommandRunResult> {
  if (await isSwitchedOff($)) {
    return { text: SWITCHED_OFF_TEXT[await read($, lang)] }
  }
  await enter($)
  queueStart($, args)

  return { text: ELSA_TEXT[await read($, lang)] }
}

async function setMood($: EngineInterface, next: DirectorMood): Promise<void> {
  if ((await read($, mood)) !== next) {
    await update($, mood, () => next)
  }
}

/** A tool call as the bar names it: its own description, else the tool and what it works on. */
function toolLabel(tool: string, input: Record<string, unknown>): string {
  const isOwnTitled = tool.endsWith(`__${DOC_TOOL}`) || tool.endsWith(`__${ACTION_TOOL}`)

  if (isOwnTitled && typeof input.title === 'string' && input.title.trim() !== '') {
    return clip(input.title)
  }

  for (const key of ['description', 'activeForm', 'subject', 'prompt']) {
    const value = input[key]

    if (typeof value === 'string' && value.trim() !== '') {
      return clip(value)
    }
  }

  if (tool === 'Skill' && typeof input.skill === 'string') {
    return clip(input.skill)
  }

  for (const key of ['file_path', 'path', 'notebook_path']) {
    const value = input[key]

    if (typeof value === 'string' && value !== '') {
      return clip(`${tool} ${value.split('/').pop() ?? value}`)
    }
  }
  // mcp__elevenlabs__creative_generate_speech -> "elevenlabs: creative generate speech"
  const mcp = /^mcp__([^_]+(?:_[^_]+)*?)__(.+)$/.exec(tool)

  return clip(mcp === null ? tool : `${mcp[1]}: ${(mcp[2] ?? '').replace(/_/g, ' ')}`)
}

function taskStatus(value: unknown): WorkTask['status'] | 'deleted' | undefined {
  return value === 'pending' || value === 'in_progress' || value === 'completed' || value === 'deleted'
    ? value
    : undefined
}

/** The model's task list after one of its TodoWrite, TaskCreate or TaskUpdate calls. */
function tasksAfter(
  tasks: readonly WorkTask[],
  tool: string,
  input: Record<string, unknown>,
  resultText: string,
): WorkTask[] | undefined {
  if (tool === 'TodoWrite' && Array.isArray(input.todos)) {
    return input.todos.flatMap((one: unknown, i: number) => {
      const rec = (typeof one === 'object' && one !== null ? one : {}) as Record<string, unknown>
      const status = taskStatus(rec.status)

      if (typeof rec.content !== 'string' || status === undefined || status === 'deleted') {
        return []
      }

      return [
        {
          id: String(i + 1),
          subject: clip(rec.content),
          activeForm: typeof rec.activeForm === 'string' ? clip(rec.activeForm) : null,
          status,
        },
      ]
    })
  }

  if (tool === 'TaskCreate' && typeof input.subject === 'string') {
    const id = /#(\d+)/.exec(resultText)?.[1] ?? String(tasks.length + 1)

    return [
      ...tasks.filter(one => one.id !== id),
      {
        id,
        subject: clip(input.subject),
        activeForm: typeof input.activeForm === 'string' ? clip(input.activeForm) : null,
        status: 'pending',
      },
    ]
  }

  if (tool === 'TaskUpdate' && (typeof input.taskId === 'string' || typeof input.taskId === 'number')) {
    const id = String(input.taskId)
    const status = taskStatus(input.status)

    if (status === 'deleted') {
      return tasks.filter(one => one.id !== id)
    }

    return tasks.map(one =>
      one.id !== id
        ? one
        : {
            ...one,
            status: status ?? one.status,
            subject: typeof input.subject === 'string' ? clip(input.subject) : one.subject,
            activeForm: typeof input.activeForm === 'string' ? clip(input.activeForm) : one.activeForm,
          },
    )
  }

  return undefined
}

/** Before a tool runs: what she is doing now, and whether she waits for the person. */
async function noteToolStart($: EngineInterface, tool: string, input: Record<string, unknown>, isMain: boolean) {
  const label = toolLabel(tool, input)
  await update($, work, held => ({ ...held, label }))

  if (isMain) {
    await setMood($, tool === 'AskUserQuestion' ? 'waiting' : 'working')
  }
}

/** After a tool ran: the task list, and her mood if it failed. */
async function noteToolEnd(
  $: EngineInterface,
  tool: string,
  input: Record<string, unknown>,
  result: ToolCallResult,
  isMain: boolean,
) {
  const failed = result.isError === true
  const text = typeof result.text === 'string' ? result.text : ''

  if (!failed && result.deny === undefined) {
    const held = await read($, work)
    const tasks = tasksAfter(held.tasks, tool, input, text)

    if (tasks !== undefined) {
      await update($, work, now => ({ ...now, tasks }))
    }
  }

  if (isMain) {
    await setMood($, failed ? 'trouble' : 'working')
  }
}

/** The bar for the model's own work in a running turn, or null while she is idle. */
function workBar(
  words: DirectorLang,
  held: TurnWork,
  isWorking: boolean,
  now: number | null,
): { label: string; value?: number; max?: number; valueText: string } | null {
  if (!held.isRunning && !isWorking) {
    return null
  }
  const elapsed = held.startedAt === null || now === null ? '' : clockText(now - held.startedAt)
  const open = held.tasks.filter(one => one.status !== 'completed')

  if (held.tasks.length > 0 && open.length > 0) {
    const current = held.tasks.find(one => one.status === 'in_progress') ?? open[0]
    const step = current === undefined ? held.tasks.length : held.tasks.indexOf(current) + 1
    const task = current === undefined ? '' : (current.status === 'in_progress' ? current.activeForm : null) ?? current.subject

    return {
      label: stepOfText(words, step, held.tasks.length, task),
      value: step,
      max: held.tasks.length,
      valueText: elapsed === '' ? `${step}/${held.tasks.length}` : `${step}/${held.tasks.length} · ${elapsed}`,
    }
  }

  return { label: held.label ?? THINKING_LIVE[words], valueText: elapsed === '' ? '…' : elapsed }
}

// Background work: what Elsa left running after her turn (a subagent launched with
// run_in_background, a background Bash command, a Monitor). A turn's bar goes with the turn, so
// without this the person saw nothing for minutes and asked «ты не заснул?» (test signals m5).
//
// Seen start: the Agent call's `async_launched` record (agentId), a subagent's `turn.start`, a
// Bash record's `backgroundTaskId`, a Monitor record's `taskId`.
// Seen end: the subagent's `turn.complete` and `classic.SubagentStop`, the task notification
// (`<task-id>` with an ended `<status>`), TaskStop, the main loop's `classic.Stop` listing every
// task still in flight (the engine's own word: what it does not list has ended), `$.agent.list()`
// on the band's clock, and the age limit, for an end missed while the mod reloaded.

/** A background entry older than this is a run whose end was missed (a reload): it is dropped. */
const BACKGROUND_STALE_MS = 3 * 60 * 60_000
/** An agent `$.agent.list()` has not listed for this long has ended and been dropped by the engine. */
const AGENT_UNLISTED_MS = 30_000
/** `$.store` key: this session's background work, so a reloaded mod still shows it. */
const STORE_BACKGROUND = 'background-work'
/** How many sessions' background lists the store keeps. */
const MAX_BACKGROUND_SESSIONS = 20
/** The words a task notification ends a task with. */
const ENDED_STATUSES = new Set(['completed', 'failed', 'killed', 'stopped', 'cancelled', 'canceled', 'error'])
/** Width of the moving bar the terminal draws while she thinks or works in the background. */
const SWEEP_WIDTH = 18
const SWEEP_SPAN = 4

function isBackgroundTask(value: unknown): value is BackgroundTask {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const one = value as Record<string, unknown>

  return (
    typeof one.id === 'string' &&
    one.id !== '' &&
    typeof one.kind === 'string' &&
    typeof one.label === 'string' &&
    typeof one.startedAt === 'number' &&
    Number.isFinite(one.startedAt)
  )
}

/** The kind a BackgroundTaskSummary's `type` names. */
function kindOf(type: unknown): BackgroundTask['kind'] {
  const word = typeof type === 'string' ? type.toLowerCase() : ''

  if (word.includes('agent')) {
    return 'agent'
  }

  if (word.includes('shell') || word.includes('bash')) {
    return 'shell'
  }

  if (word.includes('monitor')) {
    return 'monitor'
  }

  return word.includes('workflow') ? 'workflow' : 'other'
}

/** The store's background lists, by session id. */
async function storedBackground($: EngineInterface): Promise<Record<string, BackgroundTask[]>> {
  const held = await $.store.get(STORE_BACKGROUND)

  if (typeof held !== 'object' || held === null || Array.isArray(held)) {
    return {}
  }
  const out: Record<string, BackgroundTask[]> = {}

  for (const [id, list] of Object.entries(held as Record<string, unknown>)) {
    if (Array.isArray(list)) {
      out[id] = list.filter(isBackgroundTask)
    }
  }

  return out
}

/** Sets the background list, and keeps it in the store under this session for a reloaded mod. */
async function setBackground($: EngineInterface, next: BackgroundTask[]): Promise<void> {
  const now = await read($, background)

  if (JSON.stringify(now) === JSON.stringify(next)) {
    return
  }
  await update($, background, () => next)

  try {
    const id = await $.session.id()
    const all = await storedBackground($)
    delete all[id]

    if (next.length > 0) {
      all[id] = next
    }
    const kept = Object.entries(all).slice(-MAX_BACKGROUND_SESSIONS)
    await $.store.set(STORE_BACKGROUND, Object.fromEntries(kept))
  } catch {
    // A host without a store: the list lives in this process only.
  }
}

/** Notes a background task as started; one already known keeps its start. */
async function addBackground(
  $: EngineInterface,
  id: string,
  kind: BackgroundTask['kind'],
  label: string,
): Promise<void> {
  if (id === '') {
    return
  }
  const held = await read($, background)
  const known = held.find(one => one.id === id)

  if (known !== undefined) {
    if (known.label === '' && label !== '') {
      await setBackground($, held.map(one => (one.id === id ? { ...one, label: clip(label) } : one)))
    }

    return
  }
  const startedAt = (await nowOf($)) ?? 0
  await setBackground($, [...held, { id, kind, label: clip(label), startedAt }])
}

async function dropBackground($: EngineInterface, id: string): Promise<void> {
  const held = await read($, background)

  if (held.some(one => one.id === id)) {
    await setBackground($, held.filter(one => one.id !== id))
  }
}

/** After a tool ran: a background launch starts an entry, a TaskStop ends one. */
async function noteBackgroundTool(
  $: EngineInterface,
  tool: string,
  input: Record<string, unknown>,
  result: ToolCallResult,
): Promise<void> {
  if (result.deny !== undefined || result.isError === true) {
    return
  }
  const record = (typeof result.result === 'object' && result.result !== null ? result.result : {}) as Record<
    string,
    unknown
  >
  const described = typeof input.description === 'string' ? input.description : ''

  if (tool === 'Agent' || tool === 'Task') {
    if (record.status === 'async_launched' && typeof record.agentId === 'string') {
      await addBackground($, record.agentId, 'agent', typeof record.description === 'string' ? record.description : described)
    } else if (record.status === 'remote_launched' && typeof record.taskId === 'string') {
      await addBackground($, record.taskId, 'agent', typeof record.description === 'string' ? record.description : described)
    }

    return
  }

  if (tool === 'Bash' && typeof record.backgroundTaskId === 'string') {
    await addBackground($, record.backgroundTaskId, 'shell', described)

    return
  }

  if (tool === 'Monitor' && typeof record.taskId === 'string') {
    await addBackground($, record.taskId, 'monitor', described)

    return
  }

  if (tool === 'TaskStop' || tool === 'KillShell') {
    const id = typeof input.task_id === 'string' ? input.task_id : typeof input.shell_id === 'string' ? input.shell_id : ''
    await dropBackground($, id)
  }
}

/** A task notification ends the task it names, when its status is an end. */
async function noteTaskNotification($: EngineInterface, text: string): Promise<void> {
  const pattern = /<task-notification>([\s\S]*?)<\/task-notification>/g

  for (const match of text.matchAll(pattern)) {
    const body = match[1] ?? ''
    const id = /<task-id>\s*([^<\s]+)\s*<\/task-id>/.exec(body)?.[1]
    const status = /<status>\s*([^<]+?)\s*<\/status>/.exec(body)?.[1]?.toLowerCase()

    if (id !== undefined && status !== undefined && ENDED_STATUSES.has(status)) {
      await dropBackground($, id)
    }
  }
}

/** The main loop stopped: the engine lists every task still in flight; the rest have ended. */
async function reconcileBackground($: EngineInterface, listed: unknown): Promise<void> {
  if (!Array.isArray(listed)) {
    return
  }
  const held = await read($, background)
  const now = (await nowOf($)) ?? 0
  const next: BackgroundTask[] = []

  for (const one of listed) {
    if (typeof one !== 'object' || one === null) {
      continue
    }
    const task = one as Record<string, unknown>

    if (typeof task.id !== 'string' || task.id === '') {
      continue
    }
    const status = typeof task.status === 'string' ? task.status.toLowerCase() : ''

    if (ENDED_STATUSES.has(status)) {
      continue
    }
    const known = held.find(mine => mine.id === task.id)
    const label = typeof task.description === 'string' ? clip(task.description) : ''
    next.push(known ?? { id: task.id, kind: kindOf(task.type), label, startedAt: now })
  }
  await setBackground($, next)
}

/** On the band's clock: old entries go, and agents the engine says ended (or no longer lists). */
async function pruneBackground($: EngineInterface): Promise<void> {
  const held = await read($, background)

  if (held.length === 0) {
    return
  }
  const now = await nowOf($)

  if (now === null) {
    return
  }
  let next = held.filter(one => now - one.startedAt <= BACKGROUND_STALE_MS)

  if (next.some(one => one.kind === 'agent')) {
    try {
      const agents = await $.agent.list()
      const status = new Map(agents.map(one => [one.id, one.status]))
      next = next.filter(one => {
        if (one.kind !== 'agent') {
          return true
        }
        const stands = status.get(one.id)

        if (stands === undefined) {
          return now - one.startedAt <= AGENT_UNLISTED_MS
        }

        return stands === 'pending' || stands === 'running' || stands === 'waiting'
      })
    } catch {
      // A host without the agent list: the end events and the age limit decide.
    }
  }

  if (next.length !== held.length) {
    await setBackground($, next)
  }
}

/** A session starts or the mod reloads: this session's background work comes back, the stale left out. */
async function restoreBackground($: EngineInterface): Promise<void> {
  try {
    const id = await $.session.id()
    const kept = (await storedBackground($))[id] ?? []
    const now = await nowOf($)
    const fresh = kept.filter(one => now === null || now - one.startedAt <= BACKGROUND_STALE_MS)
    const held = await read($, background)
    const merged = [...held, ...fresh.filter(one => !held.some(mine => mine.id === one.id))]
    await setBackground($, merged)
  } catch {
    // A host without a store: nothing to bring back.
  }
}

/** The background row, or null while nothing runs after her turn. */
function backgroundBar(
  words: DirectorLang,
  held: readonly BackgroundTask[],
  now: number | null,
): { label: string; valueText: string } | null {
  if (held.length === 0) {
    return null
  }
  const first = Math.min(...held.map(one => one.startedAt))

  return { label: backgroundText(words, held.length), valueText: now === null ? '…' : clockText(now - first) }
}

/** A bar that moves: a lit span sweeping back and forth across a dim track, one cell a beat. */
function sweepParts(beat: number): [string, string, string] {
  const travel = SWEEP_WIDTH - SWEEP_SPAN
  const cycle = travel * 2
  const step = ((beat % cycle) + cycle) % cycle
  const at = step <= travel ? step : cycle - step

  return ['━'.repeat(at), '█'.repeat(SWEEP_SPAN), '━'.repeat(travel - at)]
}

/** The full name the model calls the document tool by. */
function docToolName(plugin: string): string {
  return `mcp__${plugin}__${DOC_TOOL}`
}

/** Declares the document tool; without it the skills paste documents into the chat. */
async function registerDocTool($: EngineInterface): Promise<void> {
  try {
    await $.tool.register({
      name: DOC_TOOL,
      description: DOC_TOOL_DESCRIPTION,
      inputSchema: DOC_TOOL_SCHEMA,
      isDeferred: false,
    })
  } catch {
    // A host without model tools: the document goes into the chat, as the skills say.
  }
}

/** Declares the action card's tools; without them the hand steps go into the chat, framed. */
async function registerActionTools($: EngineInterface): Promise<void> {
  try {
    await $.tool.register({
      name: ACTION_TOOL,
      description: ACTION_TOOL_DESCRIPTION,
      inputSchema: ACTION_TOOL_SCHEMA,
      isDeferred: false,
    })
    await $.tool.register({
      name: CLEAR_ACTION_TOOL,
      description: CLEAR_ACTION_TOOL_DESCRIPTION,
      inputSchema: CLEAR_ACTION_TOOL_SCHEMA,
      isDeferred: false,
    })
  } catch {
    // A host without model tools: the contract says how to frame a hand step in the chat.
  }
}

/** The button labels the model asked for, else the defaults; a reason when they are unusable. */
function docButtonsOf(
  value: unknown,
  words: DirectorLang,
  defaults: Record<DirectorLang, readonly string[]> = DOC_BUTTONS,
  most: number = MAX_DOC_BUTTONS,
): string[] | { error: string } {
  if (value === undefined) {
    return [...defaults[words]]
  }

  if (!Array.isArray(value)) {
    return { error: 'buttons must be a list of labels' }
  }
  const labels = value.map(one => (typeof one === 'string' ? one.replace(/\s+/g, ' ').trim() : ''))

  if (labels.length === 0 || labels.length > most || labels.some(one => one === '')) {
    return { error: `buttons must be 1 to ${most} non-empty labels` }
  }

  if (labels.some(one => one.length > MAX_BUTTON_LABEL)) {
    return { error: `a button label must be at most ${MAX_BUTTON_LABEL} characters` }
  }

  if (new Set(labels).size !== labels.length) {
    return { error: 'button labels must differ' }
  }

  return labels
}

function quoted(labels: readonly string[]): string {
  return labels.map(one => `«${one}»`).join(', ')
}

/**
 * The document tool: reads the file, puts it in the pane beside the chat and opens the pane.
 * The result tells the model whether the person can see it; a refusal says why, for the model.
 */
async function showDocument($: EngineInterface, input: Record<string, unknown>): Promise<ToolCallResult> {
  const path = typeof input.file_path === 'string' ? input.file_path.trim() : ''

  if (!path.startsWith('/')) {
    return { deny: `${DOC_TOOL}: file_path must be an absolute path. Nothing was shown.` }
  }

  if (!/\.(md|markdown|txt)$/i.test(path)) {
    return { deny: `${DOC_TOOL}: only .md or .txt documents can be shown (${path}). Nothing was shown.` }
  }
  let text: string

  try {
    text = await $.fs.read(path)
  } catch (error) {
    const why = error instanceof Error ? error.message : String(error)

    return {
      deny:
        `${DOC_TOOL}: cannot read ${path} (${why}). Nothing was shown: check the path, or paste ` +
        'the document into the chat yourself.',
    }
  }
  const named = typeof input.title === 'string' ? input.title.replace(/\s+/g, ' ').trim() : ''
  const title = (named === '' ? path.split('/').pop() ?? path : named).slice(0, MAX_DOC_TITLE)
  const words = langOf(named) ?? (await read($, lang))
  const buttons = docButtonsOf(input.buttons, words)

  if (!Array.isArray(buttons)) {
    return { deny: `${DOC_TOOL}: ${buttons.error}. Nothing was shown.` }
  }
  const body = text.length > MAX_DOC_CHARS ? `${text.slice(0, MAX_DOC_CHARS)}\n\n…` : text
  await update($, doc, () => ({ path, title, text: body, lang: words, buttons, sent: null }))
  let opened: UiOpenResult

  try {
    opened = await $.ui.open({ id: DOC_PANE, title, columns: DOC_COLUMNS })
  } catch (error) {
    opened = { isPlaced: false, reason: error instanceof Error ? error.message : String(error) }
  }

  if (!opened.isPlaced) {
    return {
      result:
        `isPlaced: false. The pane beside the chat is NOT shown (${opened.reason}): the person cannot ` +
        `see "${title}". Paste the document into the chat yourself, then ask them to answer with one ` +
        `of: ${quoted(buttons)}.`,
    }
  }
  const cut = body === text ? '' : ' Only the beginning of a very long document is drawn.'

  return {
    result:
      `isPlaced: true. "${title}" is open in the pane to the right of the chat, with the buttons ` +
      `${quoted(buttons)} at its bottom.${cut} Now write ONE line in the chat as a markdown heading ` +
      `that says where to look and what to press, e.g. ` +
      `"## ${title} открыт справа → прочитайте и нажмите «${buttons[0]}» внизу панели". Do not paste ` +
      "the document. The person's press reaches you as their own message carrying the button's label.",
  }
}

/** A button of the document pane: its label goes to the chat as the person's own message. */
async function sendDocChoice($: EngineInterface, label: string): Promise<void> {
  const held = await read($, doc)

  if (held === null || held.sent !== null) {
    return
  }
  await update($, doc, now => (now === null ? now : { ...now, sent: label }))

  try {
    await $.prompt.submit({ text: label, asUser: true })
  } catch {
    // Not delivered: the buttons come back, so the person can press again or type.
    await update($, doc, now => (now === null || now.sent !== label ? now : { ...now, sent: null }))
  }
}

/** A typed answer that is one of the pane's labels settles the pane as a press would. */
async function noteTypedChoice($: EngineInterface, text: string): Promise<void> {
  const held = await read($, doc)
  const typed = text.replace(/\s+/g, ' ').trim()

  if (held !== null && held.sent === null && held.buttons.includes(typed)) {
    await update($, doc, now => (now === null ? now : { ...now, sent: typed }))
  }
}

// ---- The channel to the person ----

/** Notices found by the last poll and not shown yet, the most important first. */
let pendingNotices: ChannelNotice[] = []
let lastPollAt = Number.NEGATIVE_INFINITY
let lastCheckAt = Number.NEGATIVE_INFINITY
let isPolling = false

function vpArgv($: EngineInterface, ...args: string[]): string[] {
  return [`${$.plugin.root}/scripts/vp`, ...args]
}

/** The engine ran on this computer before (its home holds telemetry or logs): asking it now
 * never starts the first-run install of its Python environment in the middle of a session. */
async function isChannelReady($: EngineInterface): Promise<boolean> {
  const home = await pipelineHome($)

  if (home === undefined) {
    return false
  }

  try {
    const entries = await $.fs.list(home)

    return entries.some(one => one.name === 'telemetry' || one.name === 'logs')
  } catch {
    return false
  }
}

function noticesOf(answer: unknown, words: DirectorLang, restartShown: unknown): ChannelNotice[] {
  if (typeof answer !== 'object' || answer === null) {
    return []
  }
  const rec = answer as Record<string, unknown>
  const out: ChannelNotice[] = []
  const update = rec.update as Record<string, unknown> | null | undefined

  if (update && update.restart_needed === true && typeof update.installed_newest === 'string' &&
      update.installed_newest !== restartShown) {
    // The new release's whole line: its build.json was read by the engine (`vp update check`).
    const line = releaseLabel(words, update.installed_newest, buildInfoOf(update.installed_newest_build))

    out.push({ source: 'restart', id: 0, kind: update.installed_newest, text: updateReadyText(words, line), lang: words })
  }

  for (const one of Array.isArray(rec.messages) ? rec.messages : []) {
    const m = one as Record<string, unknown>

    if (typeof m.id === 'number' && typeof m.text === 'string' && m.text.trim() !== '') {
      out.push({ source: 'message', id: m.id, kind: typeof m.kind === 'string' ? m.kind : 'info', text: m.text.trim(), lang: words })
    }
  }
  const reminder = rec.reminder as Record<string, unknown> | null | undefined

  if (reminder && reminder.due === true) {
    out.push({ source: 'reminder', id: 0, kind: 'reminder', text: REMINDER_TEXT[words], lang: words })
  }

  return out
}

/** Sends the telemetry in the background and asks for news; failures are silent. */
async function pollChannel($: EngineInterface): Promise<void> {
  if (isPolling) {
    return
  }
  isPolling = true

  try {
    if (!(await isChannelReady($))) {
      return
    }
    lastPollAt = await $.clock.now()

    try {
      await $.process.run(vpArgv($, 'telemetry', 'flush', '--detach'), { timeoutMs: 15_000 })
    } catch {
      // Sent next time.
    }
    const words = await read($, lang)
    const asked = await $.process.run(vpArgv($, 'messages', 'poll', '--json', '--lang', words), { timeoutMs: 20_000 })

    if (asked.exitCode === 0) {
      pendingNotices = noticesOf(JSON.parse(asked.stdout), words, await $.store.get(STORE_RESTART_SHOWN))
    }
  } catch {
    // No engine, no network: nothing to show.
  } finally {
    isPolling = false
  }
}

/** True while nothing runs: no turn, no document or question waiting, the engine idle. */
async function isQuiet($: EngineInterface): Promise<boolean> {
  if ((await read($, work)).isRunning || (await read($, mood)) === 'waiting' || (await read($, progress)) !== null) {
    return false
  }
  const held = await read($, doc)

  if (held !== null && held.sent === null) {
    return false
  }
  // A hand step or the welcome back waiting for the person: one thing at a time above the prompt.
  const step = await read($, action)

  if ((step !== null && step.sent === null) || (await read($, welcome)) !== null) {
    return false
  }

  try {
    const asked = await $.process.run(vpArgv($, 'busy', '--json'), { timeoutMs: 15_000 })

    return asked.exitCode === 0 && (JSON.parse(asked.stdout) as { busy?: unknown }).busy === false
  } catch {
    return false
  }
}

/** Shows the first waiting notice if the moment is quiet, and records it as shown. */
async function showNotice($: EngineInterface, isForced = false): Promise<void> {
  if (pendingNotices.length === 0 || (await read($, notice)) !== null) {
    return
  }
  const now = await $.clock.now()

  if (!isForced && now - lastCheckAt < RECHECK_MS) {
    return
  }
  lastCheckAt = now

  if (!(await isQuiet($))) {
    return
  }
  const next = pendingNotices.shift()

  if (next === undefined) {
    return
  }
  await update($, notice, () => next)
  $.ui.toast(next.text)

  try {
    if (next.source === 'message') {
      await $.process.run(vpArgv($, 'messages', 'ack', String(next.id)), { timeoutMs: 15_000 })
    } else if (next.source === 'restart') {
      await $.store.set(STORE_RESTART_SHOWN, next.kind)
    } else {
      await $.process.run(vpArgv($, 'update', 'reminder', 'shown'), { timeoutMs: 15_000 })
    }
  } catch {
    // Shown; at worst it is shown once more later.
  }
}

/** A session starts: news once it settles. */
function startChannel($: EngineInterface): void {
  try {
    $.clock.after(CHANNEL_DELAY_MS, () => {
      pollChannel($)
        .then(() => showNotice($, true))
        .catch(() => undefined)
    })
  } catch {
    // No clock: the first turn's end asks.
  }
}

/** A turn ended: a waiting notice gets its chance; news are asked again now and then. */
async function channelAfterTurn($: EngineInterface): Promise<void> {
  if ((await $.clock.now()) - lastPollAt >= REPOLL_MS) {
    await pollChannel($)
    await showNotice($, true)

    return
  }
  await showNotice($)
}

/** The reminder's yes goes to the chat as the person's own words; Elsa switches it on. */
async function noticeAnswer($: EngineInterface, label: string | null): Promise<void> {
  await update($, notice, () => null)

  if (label !== null) {
    try {
      await $.prompt.submit({ text: label, asUser: true })
    } catch {
      // The person can say it in words.
    }
  }
}

/**
 * The update notice's «Перезапустить Claude»: the model is told (a row the person does not see) to
 * restart Claude with `vp app restart-claude`, which refuses while work runs; the press goes to the
 * chat as the person's own words. Never a hand step with a key combination.
 */
async function restartAnswer($: EngineInterface, version: string, label: string): Promise<void> {
  await update($, notice, () => null)

  try {
    await $.session.append({
      message: { type: 'user', content: [{ type: 'text', text: restartAskNote(version) }] },
    })
    await $.prompt.submit({ text: label, asUser: true })
  } catch {
    // The person can say it in words.
  }
}

/** The welcome-back card is up: the engine's restart marker has done its work (best effort). */
function clearRestartMarker($: EngineInterface): void {
  isChannelReady($)
    .then(isReady =>
      isReady ? $.process.run(vpArgv($, 'app', 'clear-restart'), { timeoutMs: 15_000 }) : undefined,
    )
    .catch(() => undefined)
}

/** Whether the session is drawn on a surface that has the band above the prompt. */
async function hasBand($: EngineInterface): Promise<boolean> {
  try {
    const surfaces = await $.session.surfaces()

    return surfaces.some(one => one === 'terminal' || one === 'desktop' || one === 'mobile')
  } catch {
    return false
  }
}

/** A text input of the action tool, flattened; null when absent; a reason when unusable. */
function actionTextOf(input: Record<string, unknown>, key: string, most: number): string | null | { error: string } {
  const value = input[key]

  if (value === undefined || value === null) {
    return null
  }

  if (typeof value !== 'string') {
    return { error: `${key} must be a text` }
  }
  const flat = value.replace(/\s+/g, ' ').trim()

  if (flat.length > most) {
    return { error: `${key} must be at most ${most} characters (one action, said briefly)` }
  }

  return flat === '' ? null : flat
}

/** Keeps a restart card across the restart, or forgets the one kept. */
async function keepRestart($: EngineInterface, card: ActionCard | null): Promise<void> {
  if (card === null || card.kind !== 'restart') {
    await $.store.delete(STORE_RESTART)

    return
  }
  const kept: PendingRestart = {
    title: card.title,
    how: card.how,
    after: card.after,
    lang: card.lang,
    at: await nowOf($),
  }
  await $.store.set(STORE_RESTART, kept)
}

/** The restart card kept by an earlier process, if it is one. */
function pendingOf(value: unknown): PendingRestart | undefined {
  if (typeof value !== 'object' || value === null) {
    return undefined
  }
  const rec = value as Record<string, unknown>

  if (typeof rec.title !== 'string' || typeof rec.how !== 'string' || !isLang(rec.lang)) {
    return undefined
  }

  return {
    title: rec.title,
    how: rec.how,
    after: typeof rec.after === 'string' ? rec.after : null,
    lang: rec.lang,
    at: typeof rec.at === 'number' && Number.isFinite(rec.at) ? rec.at : null,
  }
}

/**
 * At a session's start: a restart card kept within RESTART_TTL_MS becomes the welcome-back card,
 * and is dropped from the store either way, so it greets the person once.
 */
async function welcomeBack($: EngineInterface): Promise<void> {
  const held = await $.store.get(STORE_RESTART)

  if (held === undefined) {
    return
  }
  await $.store.delete(STORE_RESTART)
  const pending = pendingOf(held)
  const now = await nowOf($)

  if (pending === undefined || (pending.at !== null && now !== null && now - pending.at > RESTART_TTL_MS)) {
    return
  }
  const card: WelcomeCard = { title: pending.title, how: pending.how, after: pending.after, lang: pending.lang }
  await update($, welcome, () => card)
  clearRestartMarker($)

  if (!(await hasBand($))) {
    try {
      await $.ui.open({ id: ACTION_PANE, title: WELCOME_BACK[card.lang], columns: ACTION_COLUMNS })
    } catch {
      // Not drawn: the note still reaches the model with the person's first message.
    }
  }
}

/** The model's words after a card was put up: where it is, what to write, what not to do. */
function actionResult(card: ActionCard, where: 'band' | 'pane' | { reason: string }): string {
  const restart =
    card.kind === 'restart'
      ? ' Because kind is "restart", the step is remembered: when Claude is opened again within 24 hours ' +
        `a welcome-back card with «${CONTINUE[card.lang]}» greets the person, and their press (or first ` +
        'message) reaches you with a note to continue from this place.'
      : ''

  if (typeof where === 'object') {
    return (
      `isPlaced: false. No card can be drawn here (${where.reason}). Write the step in the chat yourself, ` +
      `framed so it cannot be missed: a blockquote opening with "**${ACTION_MARK[card.lang]}**", then the ` +
      `action in bold, how to do it${card.notThis === null ? '' : `, "${ACTION_NOT_MARK} ${card.notThis}" on its own line`}` +
      `${card.after === null ? '' : ', what comes after'}; end with the question «${ACTION_CHECK[card.lang]}» ` +
      `and the answers ${quoted(card.buttons)}, then wait for the answer before anything else.${restart}`
    )
  }
  const place = where === 'band' ? 'in a frame above the prompt' : 'in the pane to the right of the chat'
  const unclear =
    ` If they press «${ACTION_UNCLEAR[card.lang]}», say the same step more simply (what exactly they will ` +
    `see on screen) and show it again with ${ACTION_TOOL}.`

  return (
    `isPlaced: true. The card «${card.title}» is up ${place}, highlighted, with the check ` +
    `«${ACTION_CHECK[card.lang]}» and the buttons ${quoted(card.buttons)}. Now write in the chat ONE short ` +
    'line as a markdown heading that points at it, e.g. ' +
    `"## ⬇ Одно действие — в рамке над полем ввода: прочитайте и нажмите «${card.buttons[0]}»", then end ` +
    'your turn and wait. Do not repeat the step in other words and do not give the next step: their ' +
    `press reaches you as their own message carrying the label.${unclear}${restart}`
  )
}

/**
 * The action tool: checks the step, puts the card above the prompt (or in a pane where no band is
 * drawn) and keeps a restart card across the restart. A refusal says why, for the model.
 */
async function showAction($: EngineInterface, input: Record<string, unknown>): Promise<ToolCallResult> {
  const refuse = (why: string): ToolCallResult => ({ deny: `${ACTION_TOOL}: ${why}. Nothing was shown.` })
  const title = actionTextOf(input, 'title', MAX_ACTION_TITLE)
  const how = actionTextOf(input, 'how', MAX_ACTION_HOW)
  const notThis = actionTextOf(input, 'not_this', MAX_ACTION_NOTE)
  const after = actionTextOf(input, 'after', MAX_ACTION_NOTE)

  for (const one of [title, how, notThis, after]) {
    if (one !== null && typeof one === 'object') {
      return refuse(one.error)
    }
  }

  if (typeof title !== 'string') {
    return refuse('title is required: the one action, e.g. «Скопируйте ключ ElevenLabs»')
  }

  if (typeof how !== 'string') {
    return refuse('how is required: what to press, pointing at something visible on screen')
  }

  if ([title, how].some(text => KEY_COMBO.test(text))) {
    return refuse(
      'a key combination is never the way: it acts on whatever app is in front (a novice quit her ' +
        'browser that way). Point at something visible (a button, a menu by its words); restarting ' +
        'Claude or quitting CapCut is done by the engine, not by the person',
    )
  }
  const kind = input.kind ?? 'step'

  if (kind !== 'restart' && kind !== 'step') {
    return refuse('kind must be "restart" or "step"')
  }

  if (input.buttons === undefined) {
    return refuse('buttons are required: 1 or 2 labels that name the action, e.g. «Понятно: копирую ключ»')
  }
  const words = langOf(`${title} ${how}`) ?? (await read($, lang))
  const own = docButtonsOf(input.buttons, words, DOC_BUTTONS, MAX_ACTION_BUTTONS)

  if (!Array.isArray(own)) {
    return refuse(own.error)
  }
  const unclear = ACTION_UNCLEAR[words]
  const buttons = own.includes(unclear) ? own : [...own, unclear]
  const card: ActionCard = {
    title,
    how,
    notThis: typeof notThis === 'string' ? notThis : null,
    after: typeof after === 'string' ? after : null,
    kind,
    lang: words,
    buttons,
    sent: null,
  }
  await update($, action, () => card)
  // A new step stands in for a welcome-back card still up.
  await update($, welcome, () => null)
  await keepRestart($, card)

  if (await hasBand($)) {
    return { result: actionResult(card, 'band') }
  }
  let opened: UiOpenResult

  try {
    opened = await $.ui.open({ id: ACTION_PANE, title: ACTION_MARK[words], columns: ACTION_COLUMNS })
  } catch (error) {
    opened = { isPlaced: false, reason: error instanceof Error ? error.message : String(error) }
  }

  return { result: actionResult(card, opened.isPlaced ? 'pane' : { reason: opened.reason }) }
}

/** clear_action: the card goes, and a restart kept for it is forgotten. */
async function clearAction($: EngineInterface): Promise<ToolCallResult> {
  const held = await read($, action)
  await update($, action, () => null)
  await keepRestart($, null)

  return { result: held === null ? 'No action card was up.' : `The card «${held.title}» is taken down.` }
}

/** A button of the action card: its label goes to the chat as the person's own message. */
async function sendActionChoice($: EngineInterface, label: string): Promise<void> {
  const held = await read($, action)

  if (held === null || held.sent !== null) {
    return
  }
  await update($, action, now => (now === null ? now : { ...now, sent: label }))

  try {
    await $.prompt.submit({ text: label, asUser: true })
  } catch {
    // Not delivered: the card comes back, so the person can press again or type.
    await update($, action, now => (now === null || now.sent !== label ? now : { ...now, sent: null }))
  }
}

/** A typed answer that is one of the card's labels settles it as a press would. */
async function noteTypedAction($: EngineInterface, text: string): Promise<void> {
  const held = await read($, action)
  const typed = text.replace(/\s+/g, ' ').trim()

  if (held !== null && held.sent === null && held.buttons.includes(typed)) {
    await update($, action, now => (now === null ? now : { ...now, sent: typed }))
  }
}

/**
 * The welcome-back card goes, Elsa comes on stage, and the notes for the model say where the work
 * stopped (with the contract, when this conversation has not had it yet).
 */
async function takeWelcome($: EngineInterface): Promise<string[]> {
  const held = await read($, welcome)

  if (held === null) {
    return []
  }
  await update($, welcome, () => null)
  // The words go on in the language the step was given in.
  await setLang($, held.lang)
  await enter($)
  const contract = await noteForModel($, false)
  const note = resumeNote(held.title, held.how)

  return contract === undefined ? [note] : [contract, note]
}

/**
 * The welcome-back card's button: the notes go into the conversation as a row the person does not
 * see (a plugin's own prompt does not pass its own prompt.submit hook), then the person's
 * "let's continue" goes as their own message.
 */
async function sendContinue($: EngineInterface): Promise<void> {
  const held = await read($, welcome)

  if (held === null) {
    return
  }
  const text = continueText(held.lang, held.title)
  const notes = await takeWelcome($)

  try {
    await $.session.append({
      message: { type: 'user', content: [{ type: 'text', text: notes.join('\n\n') }] },
    })
    await $.prompt.submit({ text, asUser: true })
  } catch {
    // Not delivered: the card comes back, so the person can press again or type.
    await update($, welcome, now => now ?? held)
  }
}

/** The action card: the marker, the one action, how, the look-alike not to touch, after, the check. */
function actionCardTree(
  $: EngineInterface,
  ui: ReturnType<EngineInterface['ui']['resolve']>,
  card: ActionCard,
): JSX.Element {
  const { Box, Button, Text } = ui
  const words = card.lang

  return (
    <Box key="action-card" flexDirection="column" gap={1} borderStyle="bold" borderColor={ACCENT} paddingX={1}>
      <Text bold size="title" color="#ffffff" backgroundColor={ACCENT}>
        {` ${ACTION_MARK[words]} `}
      </Text>
      <Text bold size="heading">
        {card.title}
      </Text>
      <Text>{card.how}</Text>
      {card.notThis === null ? null : (
        <Text bold color="#b3392f">
          {`${ACTION_NOT_MARK} ${card.notThis}`}
        </Text>
      )}
      {card.after === null ? null : <Text dimColor>{card.after}</Text>}
      {card.kind === 'restart' ? <Text dimColor>{ACTION_RESTART_NOTE[words]}</Text> : null}
      <Text bold>{ACTION_CHECK[words]}</Text>
      <Box flexDirection="row" gap={1}>
        {card.buttons.map((label, i) => (
          <Button
            key={`action-answer-${i}`}
            label={label}
            variant={i === 0 ? 'primary' : 'secondary'}
            onPress={() => sendActionChoice($, label)}
          />
        ))}
      </Box>
    </Box>
  )
}

/** The welcome-back card: where the work stopped, and the one button that goes on from there. */
function welcomeCardTree(
  $: EngineInterface,
  ui: ReturnType<EngineInterface['ui']['resolve']>,
  card: WelcomeCard,
): JSX.Element {
  const { Box, Button, Text } = ui
  const words = card.lang

  return (
    <Box key="welcome-card" flexDirection="column" gap={1} borderStyle="bold" borderColor={ACCENT} paddingX={1}>
      <Text bold size="heading">
        {`${WELCOME_BACK[words]} «${card.title}»`}
      </Text>
      {card.after === null ? null : <Text dimColor>{card.after}</Text>}
      <Box flexDirection="row" gap={1}>
        <Button key="welcome-continue" label={CONTINUE[words]} variant="primary" onPress={() => sendContinue($)} />
      </Box>
    </Box>
  )
}

/** The card that waits for the person now, drawn; null when none does. */
async function cardNow($: EngineInterface, ui: ReturnType<EngineInterface['ui']['resolve']>): Promise<JSX.Element | null> {
  const held = await read($, action)

  if (held !== null && held.sent === null) {
    return actionCardTree($, ui, held)
  }
  const back = await read($, welcome)

  return back === null ? null : welcomeCardTree($, ui, back)
}

function isFinished(tasks: readonly WorkTask[]): boolean {
  return tasks.length > 0 && tasks.every(one => one.status === 'completed')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'director',
      description: 'Call the editing director of AI Movie Company into this conversation',
    })
    await $.command.register({
      name: 'director-off',
      description: 'Editing director: leave — plain Claude and the usual look come back',
    })
    await $.command.register({
      name: 'elsa',
      description: 'Elsa, the editing director of AI Movie Company: call her and start working together',
      argumentHint: '[folder-or-video-path]',
    })
    await registerDocTool($)
    startChannel($)
    await registerActionTools($)
    const storedLang = await $.store.get(STORE_LANG)

    if (isLang(storedLang)) {
      isLangKnown = true
      await update($, lang, () => storedLang)
    }

    // A restart or a reload: the session comes back as the person left it, look included
    // (a theme is display only and does not outlive the process).
    if (!(await isSwitchedOff($)) && ((await read($, onStage)) || (await isRemembered($)))) {
      await update($, onStage, () => true)
      await paint($)
    }

    // Work left running in the background before a reload is shown again until it ends.
    await restoreBackground($)

    // Claude opened again after a restart the director asked for: the person is met where they left.
    try {
      await welcomeBack($)
    } catch {
      // A host without a store: no welcome-back card.
    }

    return next(e)
  })

  // A typed command of the plugin calls the director before the reply starts, so the reply
  // opens with her face. A message that only calls her by name does too, and becomes the
  // guided start: the model is told beside it, the person's words stay as typed.
  on('prompt.submit', async ($, e, next) => {
    let resumed: string[] = []

    if (e.origin.kind === 'task-notification') {
      try {
        await noteTaskNotification($, e.text)
      } catch {
        // Bookkeeping only.
      }
    }

    if (e.isMeta !== true) {
      await noteLang($, e.command === undefined ? e.text : e.command.args, false)

      if (e.command === undefined && e.origin.kind !== 'plugin') {
        await noteTypedChoice($, e.text)
        await noteTypedAction($, e.text)
      }

      // The person typed instead of pressing «Продолжить»: the model is told all the same.
      if (e.origin.kind !== 'plugin') {
        resumed = await takeWelcome($)
      }
    }

    if (resumed.length > 0) {
      return next({ ...e, context: [...(e.context ?? []), ...resumed] })
    }

    if (e.command !== undefined && isStageName(ownShortName(e.command.name, $.plugin.name))) {
      await enter($)
    }

    if (e.isMeta !== true && e.command === undefined && isNameCall(e.text) && !(await read($, onStage))) {
      if (await enter($)) {
        const note = await noteForModel($, true)
        const told = note === undefined ? [NAME_CALL_NOTE] : [note, NAME_CALL_NOTE]

        return next({ ...e, context: [...(e.context ?? []), ...told] })
      }
    }

    return next(e)
  }).catch(($, e, next) => next(e))

  // The contract reaches the model inside the text of the command or skill that calls the
  // director: a system prompt is composed before the first prompt and stays as sent.
  on('skill.prompt', async ($, e, next) => {
    const computed = await next(e)

    if (e.pluginName !== $.plugin.name || !isStageName(ownShortName(e.skill, $.plugin.name))) {
      return computed
    }

    if (await isSwitchedOff($)) {
      return { text: `${computed.text}\n\n${SWITCHED_OFF_NOTE}` }
    }
    const note = await noteForModel($, await enter($))

    return note === undefined ? computed : { text: `${computed.text}\n\n---\n\n${note}` }
  })

  // A turn of the main loop starts: she is working, and the bar counts its time.
  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      const startedAt = await nowOf($)
      await update($, work, held => ({ ...held, isRunning: true, startedAt, label: null }))
      await setMood($, 'working')
    } else {
      // A subagent's run: work in her name until its turn ends (it outlives hers when backgrounded).
      await addBackground($, e.agentId, 'agent', '')
    }

    return next(e)
  }).catch(($, e, next) => next(e))

  // Every tool call says what she is doing: the bar's label, the model's task list, her mood
  // (waiting while a question to the person is open, in trouble when a tool failed). Never in
  // the way: the bookkeeping cannot stop a call.
  on('tool.call', async ($, e, next) => {
    const input = e as unknown as Record<string, unknown>
    const isMain = e.agentId === undefined

    try {
      await noteToolStart($, e.tool, input, isMain)
    } catch {
      // Bookkeeping only.
    }
    // The mod's own tool is answered here; every other call goes on to the engine.
    const result =
      e.tool === docToolName($.plugin.name)
        ? await showDocument($, input)
        : e.tool === `mcp__${$.plugin.name}__${ACTION_TOOL}`
          ? await showAction($, input)
          : e.tool === `mcp__${$.plugin.name}__${CLEAR_ACTION_TOOL}`
            ? await clearAction($)
            : await next(e)

    try {
      await noteToolEnd($, e.tool, input, result, isMain)
    } catch {
      // Bookkeeping only.
    }

    try {
      await noteBackgroundTool($, e.tool, input, result)
    } catch {
      // Bookkeeping only.
    }

    return result
  })

  // The director answers in the person's language: her answer says which words the band and
  // the work indicator use from now on. The turn's end settles her mood and stops the bar.
  on('turn.complete', async ($, e, next) => {
    if (e.agentId !== undefined) {
      await dropBackground($, e.agentId)
    }

    if (e.agentId === undefined) {
      await update($, work, held => ({
        ...held,
        isRunning: false,
        label: null,
        tasks: isFinished(held.tasks) ? [] : held.tasks,
      }))
      const current = await read($, mood)
      await setMood($, e.reason === 'error' || (e.reason === 'answer' && current === 'trouble') ? 'trouble' : 'idle')

      if (e.answer !== '' && (await read($, onStage))) {
        await noteLang($, e.answer, true)
      }

      try {
        await channelAfterTurn($)
      } catch {
        // The channel never stands in the way of a turn.
      }
    }

    return next(e)
  }).catch(($, e, next) => next(e))

  // A subagent stopped: its background entry ends.
  on('classic.SubagentStop', async ($, e, next) => {
    await dropBackground($, e.agent_id)

    return next(e)
  }).catch(($, e, next) => next(e))

  // The main loop stopped: the engine lists every task still in flight, so what it leaves out ended.
  on('classic.Stop', async ($, e, next) => {
    if (e.agent_id === undefined) {
      await reconcileBackground($, e.background_tasks)
    }

    return next(e)
  }).catch(($, e, next) => next(e))

  // After a compaction, and in a session resumed on stage, the contract rides in the system
  // prompt, where a summary cannot lose it.
  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    const isOther = e.traits.some(one => one === 'bare' || one === 'teammate' || one === 'analysis')

    if (isOther || !(await read($, onStage))) {
      return composed
    }
    const contract = await contractOf($)

    if (contract === '') {
      return composed
    }

    return {
      sections: [
        ...composed.sections,
        { id: 'ai-movie-company:editing-director', text: contract, scope: 'session' },
      ],
    }
  })

  on('command.run', { command: 'director' }, async $ => {
    if (await isSwitchedOff($)) {
      return { text: SWITCHED_OFF_TEXT[await read($, lang)] }
    }
    const note = await noteForModel($, await enter($))
    const text = HERE_TEXT[await read($, lang)]

    return note === undefined ? { text } : { text, context: [`${note}\n\n${CALLED_NOTE}`] }
  })

  on('command.run', { command: 'director-off' }, async $ => {
    const words = await read($, lang)

    return (await leave($))
      ? { text: LEFT_TEXT[words], context: [LEFT_NOTE] }
      : { text: NOT_ON_STAGE_TEXT[words] }
  })

  // /elsa: the short way in. The guided start runs right after, as if typed.
  on('command.run', { command: 'elsa' }, async ($, e) => callElsa($, e.args))

  // Name and picture where a reply opens. A reply keeps what it was first drawn with: one made
  // before the director came stays plain, one of her keeps her name after she left. Her first
  // reply after she was called opens with the welcome splash.
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    if (!e.props.isFirstOfReply || e.props.isSummary === true) {
      return next(e)
    }
    const isOn = await read($, onStage)
    const isHis = seenReplies.get(e.requestId) ?? isOn

    if (!seenReplies.has(e.requestId)) {
      if (seenReplies.size >= MAX_SEEN) {
        seenReplies.clear()
      }
      seenReplies.set(e.requestId, isOn)

      if (isOn && isSplashDue) {
        isSplashDue = false
        isSplashGiven = true
        splashReply = e.requestId
      }
    }

    if (!isHis) {
      return next(e)
    }
    // The label speaks the language of the reply it opens.
    const words = langOf(e.props.text) ?? (await read($, lang))
    const name = NAME[words]
    const row = await next(e)

    if (e.surface === 'terminal') {
      const { Box, Text } = $.ui.resolve(e)

      return (
        <Box flexDirection="column">
          <Text bold>
            {AVATAR_TEXT} {name}
          </Text>
          {row}
        </Box>
      )
    }
    const { Box, Text, Svg } = $.ui.resolve(e)
    const splash =
      splashReply === e.requestId ? (
        <Svg
          source={SPLASH[words]}
          alt={SPLASH_ALT[words]}
          width={SPLASH_WIDTH}
          height={SPLASH_HEIGHT}
          isInteractive
        />
      ) : null

    return (
      <Box flexDirection="column">
        {splash}
        <Box flexDirection="row" gap={1} alignItems="center">
          <Svg source={AVATAR} alt={name} width={20} height={20} />
          <Text bold>{name}</Text>
        </Box>
        {row}
      </Box>
    )
  })

  // The director's own words while a turn runs. A step's description ("Creating notes.md")
  // says more than any phrase of her, so only the engine's one-word verbs are replaced.
  on('ui.render', { component: 'Spinner' }, async ($, e, next) => {
    const word = e.props.word.trim()

    if (word === '' || /\s/.test(word) || !(await read($, onStage))) {
      return next(e)
    }
    const words = SPINNER_WORDS[await read($, lang)]

    return next({ ...e, props: { ...e.props, word: words[hashOf(word) % words.length] ?? word } })
  })

  // The band above the prompt: who the person is talking to (her face moving with her mood),
  // which release she is, what the engine and the model are doing now, and the way out.
  // A hand step waiting for the person (or the welcome back after a restart) leads the band, framed,
  // whether or not the director is on stage.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) {
      return next(e)
    }
    const shown = await read($, notice)
    const noticeRow = (() => {
      if (shown === null) {
        return null
      }
      const { Box, Button, Markdown } = $.ui.resolve(e)
      const buttons =
        shown.source === 'reminder' ? (
          <Box flexDirection="row" gap={1}>
            <Button key="notice-on" label={REMINDER_ON[shown.lang]} variant="primary" onPress={() => noticeAnswer($, REMINDER_ON[shown.lang])} />
            <Button key="notice-later" label={REMINDER_LATER[shown.lang]} variant="secondary" onPress={() => noticeAnswer($, null)} />
          </Box>
        ) : shown.source === 'restart' ? (
          <Box flexDirection="row" gap={1}>
            <Button
              key="notice-restart"
              label={RESTART_NOW[shown.lang]}
              variant="primary"
              onPress={() => restartAnswer($, shown.kind, RESTART_NOW[shown.lang])}
            />
            <Button key="notice-later" label={RESTART_LATER[shown.lang]} variant="secondary" onPress={() => noticeAnswer($, null)} />
          </Box>
        ) : (
          <Button key="notice-dismiss" label={NOTICE_DISMISS[shown.lang]} onPress={() => noticeAnswer($, null)} />
        )

      return (
        <Box flexDirection="column" borderStyle="round" borderColor={BAND_EDGE} paddingX={1}>
          <Markdown text={`## ${shown.text}`} />
          {buttons}
        </Box>
      )
    })()

    const card = await cardNow($, $.ui.resolve(e))

    if (!(await read($, onStage))) {
      if (noticeRow === null && card === null) {
        return next(e)
      }
      const { Box } = $.ui.resolve(e)
      // A hand step waiting for the person stands in for the engine's own row; a notice sits above it.
      const below = card ?? (await next(e))

      return (
        <Box flexDirection="column">
          {noticeRow}
          {below}
        </Box>
      )
    }
    const words = await read($, lang)
    const paper = bandColor(await read($, tint))
    const running = await read($, progress)
    const clockNow = await nowOf($)
    const turn = workBar(words, await read($, work), e.props.isWorking, clockNow)
    // The engine's bar leads; a turn's bar next; work left running after the turn only when neither shows.
    const behind = running === null && turn === null ? backgroundBar(words, await read($, background), clockNow) : null
    const release = `· v${releaseLabel(words, VERSION, await buildOf($))}`
    const { Progress, Text: LiveText } = $.ui.resolve(e)
    const engineBar =
      running === null ? null : (
        <Progress
          label={STEP_WORDS[words][running.step] ?? running.step}
          value={running.percent}
          max={100}
          valueText={`${Math.round(running.percent)}% · ${etaText(words, running.etaS)}`}
          color={BAND_EDGE}
        />
      )
    // A bar with no known end. The desktop draws Progress with no value as running; the terminal
    // gets a row that moves on the face's clock (reading the beat redraws it every frame), so the
    // clock beside it is seen ticking too.
    const openBar = async (label: string, valueText: string) => {
      if (e.surface !== 'terminal') {
        return <Progress label={label} valueText={valueText} color={BAND_EDGE} />
      }
      const [before, lit, after] = sweepParts(await read($, beat))

      return (
        <LiveText color={BAND_INK_SOFT}>
          {before}
          <LiveText color={BAND_EDGE}>{lit}</LiveText>
          {after} <LiveText color={BAND_INK}>{`${label} · ${valueText}`}</LiveText>
        </LiveText>
      )
    }
    const turnBar =
      turn === null ? null : turn.value === undefined ? (
        await openBar(turn.label, turn.valueText)
      ) : (
        <Progress label={turn.label} value={turn.value} max={turn.max} valueText={turn.valueText} color={BAND_EDGE} />
      )
    const behindBar = behind === null ? null : await openBar(behind.label, behind.valueText)

    if (e.surface === 'terminal') {
      const { Box, Button, Text } = $.ui.resolve(e)

      return (
        <Box flexDirection="column" backgroundColor={paper} paddingX={1}>
          {card}
          <Box flexDirection="row" gap={1}>
            <Text bold color={BAND_INK}>
              {AVATAR_TEXT} {NAME[words]}
            </Text>
            <Text color={BAND_INK_SOFT}>
              {ON_STAGE[words]} {release}
            </Text>
            <Button key="leave" label={LEAVE[words]} onPress={() => leaveByButton($)} />
          </Box>
          {noticeRow}
          {engineBar}
          {turnBar}
          {behindBar}
        </Box>
      )
    }
    const { Box, Button, Text, Svg } = $.ui.resolve(e)
    const held = await read($, mood)
    // Work runs in the background after her turn: her face keeps working too.
    const feeling = behind !== null && held === 'idle' ? 'working' : held

    return (
      <Box
        flexDirection="column"
        gap={1}
        backgroundColor={paper}
        borderStyle="round"
        borderColor={BAND_EDGE}
        paddingX={1}
      >
        {card}
        <Box flexDirection="row" gap={1} alignItems="center">
          <Svg
            source={faceAt(feeling, await read($, beat))}
            alt={`${NAME[words]} — ${MOOD_WORDS[words][feeling]}`}
            width={28}
            height={28}
            isInteractive
          />
          <Text bold color={BAND_INK}>
            {NAME[words]}
          </Text>
          <Text color={BAND_INK_SOFT}>
            {ON_STAGE[words]}
          </Text>
          <Text color={BAND_INK_SOFT}>{release}</Text>
          <Button key="leave" label={LEAVE[words]} onPress={() => leaveByButton($)} />
        </Box>
        {noticeRow}
        {engineBar}
        {turnBar}
        {behindBar}
      </Box>
    )
  })

  // The document pane: one large line saying what to do, the document, the answer buttons. Once a
  // button was pressed (or its label typed) the buttons give way to what was sent.
  on('ui.render', { component: 'Pane', requestId: DOC_PANE }, async ($, e) => {
    const { Box, Button, Markdown, Text } = $.ui.resolve(e)
    const held = await read($, doc)

    if (held === null) {
      return <Text dimColor>{DOC_EMPTY[await read($, lang)]}</Text>
    }
    const words = held.lang
    const status = held.sent === null ? null : <Text bold>{docSentText(words, held.sent)}</Text>

    if (e.props.placement === 'widget') {
      return (
        <Box flexDirection="column">
          <Text bold>{held.title}</Text>
          {status ?? <Text>{DOC_GLANCE[words]}</Text>}
        </Box>
      )
    }
    const answer =
      status ??
      (
        <Box flexDirection="row" gap={1}>
          {held.buttons.map((label, i) => (
            <Button
              key={`doc-answer-${i}`}
              label={label}
              variant={i === 0 ? 'primary' : 'secondary'}
              onPress={() => sendDocChoice($, label)}
            />
          ))}
        </Box>
      )

    return (
      <Box flexDirection="column" gap={1}>
        <Text bold>{held.title}</Text>
        {held.sent === null ? <Markdown text={`## ${docInstructionText(words, held.buttons[0] ?? '')}`} /> : null}
        <Markdown text={held.text} />
        {answer}
      </Box>
    )
  })

  // The action card where the surface draws no band: the same card in a pane beside the chat. Once
  // a button was pressed (or its label typed) the card gives way to what was sent.
  on('ui.render', { component: 'Pane', requestId: ACTION_PANE }, async ($, e) => {
    const ui = $.ui.resolve(e)
    const { Text } = ui
    const card = await cardNow($, ui)

    if (card !== null) {
      return card
    }
    const held = await read($, action)

    return held?.sent == null ? <Text dimColor>{'—'}</Text> : <Text bold>{docSentText(held.lang, held.sent)}</Text>
  })
}
