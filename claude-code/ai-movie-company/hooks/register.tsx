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
// The mod never touches the engine's rules: gates, refusals and the human-answer guard live in
// the skills, the commands and src/vp.

import { atom, read, update } from 'claude-code'
import type { CommandRunResult, EngineInterface, Register, ThemeSetArgs, Timer, ToolCallResult } from 'claude-code'

import type { DirectorLang, DirectorMood, LiveProgress, TurnWork, WorkTask } from '../types'

import { faceAt, FRAME_MS, MOOD_WORDS } from './faces'
import { makeColors } from './make-colors.js'
import {
  AVATAR,
  AVATAR_TEXT,
  BACK_NOTE,
  BAND_EDGE,
  BAND_INK,
  BAND_INK_SOFT,
  bandColor,
  CALLED_NOTE,
  CONTRACT_LEAD,
  elapsedText,
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
  ON_STAGE,
  releasedText,
  ROLE,
  SPINNER_WORDS,
  STAGE_COMMANDS,
  START_FALLBACK,
  STEP_WORDS,
  stepOfText,
  SWITCHED_OFF_NOTE,
  SWITCHED_OFF_TEXT,
  THINKING,
  VERSION,
  warmHue,
  WARM_HUES,
} from './persona'
import { SPLASH, SPLASH_ALT, SPLASH_HEIGHT, SPLASH_WIDTH } from './splash'

const IDLE_WORK: TurnWork = { isRunning: false, startedAt: null, label: null, tasks: [] }

const onStage = atom({ plugin: 'ai-movie-company', key: 'onStage' } as const, false)
const lang = atom({ plugin: 'ai-movie-company', key: 'lang' } as const, 'en')
const tint = atom({ plugin: 'ai-movie-company', key: 'tint' } as const, WARM_HUES.from)
const progress = atom({ plugin: 'ai-movie-company', key: 'progress' } as const, null)
const mood = atom({ plugin: 'ai-movie-company', key: 'mood' } as const, 'idle')
const beat = atom({ plugin: 'ai-movie-company', key: 'beat' } as const, 0)
const work = atom({ plugin: 'ai-movie-company', key: 'work' } as const, IDLE_WORK)

/** `$.store` keys: what outlives the session's process. */
const STORE_SESSIONS = 'on-stage-sessions'
const STORE_LANG = 'lang'
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
/** The engine's mirror of the running step, in the pipeline home. */
const LIVE_FILE = 'progress-live.json'
/** A mirror older than this belongs to a run that died: nothing is running. */
const STALE_MS = 120_000

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
  const elapsed = held.startedAt === null || now === null ? '' : elapsedText(words, now - held.startedAt)
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

  return { label: held.label ?? THINKING[words], valueText: elapsed === '' ? '…' : elapsed }
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

    return next(e)
  })

  // A typed command of the plugin calls the director before the reply starts, so the reply
  // opens with her face. A message that only calls her by name does too, and becomes the
  // guided start: the model is told beside it, the person's words stay as typed.
  on('prompt.submit', async ($, e, next) => {
    if (e.isMeta !== true) {
      await noteLang($, e.command === undefined ? e.text : e.command.args, false)
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
    const result = await next(e)

    try {
      await noteToolEnd($, e.tool, input, result, isMain)
    } catch {
      // Bookkeeping only.
    }

    return result
  })

  // The director answers in the person's language: her answer says which words the band and
  // the work indicator use from now on. The turn's end settles her mood and stops the bar.
  on('turn.complete', async ($, e, next) => {
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
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || !(await read($, onStage))) {
      return next(e)
    }
    const words = await read($, lang)
    const paper = bandColor(await read($, tint))
    const running = await read($, progress)
    const turn = workBar(words, await read($, work), e.props.isWorking, await nowOf($))
    const release = `· v${VERSION} · ${releasedText(words)}`
    const { Progress } = $.ui.resolve(e)
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
    const turnBar =
      turn === null ? null : (
        <Progress label={turn.label} value={turn.value} max={turn.max} valueText={turn.valueText} color={BAND_EDGE} />
      )

    if (e.surface === 'terminal') {
      const { Box, Button, Text } = $.ui.resolve(e)

      return (
        <Box flexDirection="column" backgroundColor={paper} paddingX={1}>
          <Box flexDirection="row" gap={1}>
            <Text bold color={BAND_INK}>
              {AVATAR_TEXT} {NAME[words]}
            </Text>
            <Text color={BAND_INK_SOFT}>
              {ROLE[words]} {ON_STAGE[words]} {release}
            </Text>
            <Button key="leave" label={LEAVE[words]} onPress={() => leaveByButton($)} />
          </Box>
          {engineBar}
          {turnBar}
        </Box>
      )
    }
    const { Box, Button, Text, Svg } = $.ui.resolve(e)
    const feeling = await read($, mood)

    return (
      <Box
        flexDirection="column"
        gap={1}
        backgroundColor={paper}
        borderStyle="round"
        borderColor={BAND_EDGE}
        paddingX={1}
      >
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
            {ROLE[words]} {ON_STAGE[words]}
          </Text>
          <Text color={BAND_INK_SOFT}>{release}</Text>
          <Button key="leave" label={LEAVE[words]} onPress={() => leaveByButton($)} />
        </Box>
        {engineBar}
        {turnBar}
      </Box>
    )
  })
}
