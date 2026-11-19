// The editing director's presence in the interface.
//
// While the director is "on stage" in a session, the person can see it: replies open with his
// name and picture, a band above the prompt says he is here and holds a Leave button, the
// work indicator speaks his words, and the app takes the "Cutting room" look. The model gets
// the one-page contract (persona/contract.md) with the first command that calls him, and in
// the system prompt after a compaction.
//
// The words on the interface are Russian or English: the system language the first time the
// director is called, then the language of the conversation (see noteLang).
//
// The stage is per session. It is entered by the plugin's conversational commands and skills
// (persona.ts: STAGE_COMMANDS) or by /director, and left by the Leave button or /director-off.
// The technical commands (doctor, learn) neither enter nor leave it. Other sessions stay plain.
//
// The mod never touches the engine's rules: gates, refusals and the human-answer guard live in
// the skills, the commands and src/vp.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, ThemeSetArgs } from 'claude-code'

import type { DirectorLang } from '../types'

import { makeColors } from './make-colors.js'
import {
  AVATAR,
  AVATAR_TEXT,
  BACK_NOTE,
  CALLED_NOTE,
  CONTRACT_LEAD,
  HERE_TEXT,
  LEAVE,
  LEFT_NOTE,
  LEFT_TEXT,
  LEFT_TOAST,
  LOOK_INPUT,
  NAME,
  NOT_ON_STAGE_TEXT,
  ON_STAGE,
  SPINNER_WORDS,
  STAGE_COMMANDS,
  SWITCHED_OFF_NOTE,
  SWITCHED_OFF_TEXT,
} from './persona'

const onStage = atom({ plugin: 'ai-movie-company', key: 'onStage' } as const, false)
const lang = atom({ plugin: 'ai-movie-company', key: 'lang' } as const, 'en')

/** `$.store` keys: what outlives the session's process. */
const STORE_SESSIONS = 'on-stage-sessions'
const STORE_LANG = 'lang'
/** How many sessions' "on stage" marks are kept; the oldest go first. */
const MAX_SESSIONS = 50
/** How many replies' first sightings are kept in this process. */
const MAX_SEEN = 2000

const LOOK: ThemeSetArgs = { colors: makeColors(LOOK_INPUT).theme }

// This process's own notes; a hot reload starts them over, which costs at most one more
// copy of the contract in the conversation.
let contractText: string | undefined
let isContractSent = false
/** False until the words' language came from the store, the system or the conversation. */
let isLangKnown = false
/** Reply id -> whether the director was on stage when the reply was first drawn. */
const seenReplies = new Map<string, boolean>()

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
  await $.theme.set(LOOK)
  const surfaces = await $.session.surfaces()
  const hasBand = surfaces.some(one => one === 'terminal' || one === 'desktop' || one === 'mobile')

  if (surfaces.length > 0 && !hasBand) {
    $.ui.status(NAME[await read($, lang)])
  }
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

/** Puts the director on stage; false when he already was, or the switch holds him off. */
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

  return true
}

/** Sends the director away, on the person's ask alone; false when he was not on stage. */
async function leave($: EngineInterface): Promise<boolean> {
  if (!(await read($, onStage))) {
    return false
  }
  await update($, onStage, () => false)
  await remember($, false)
  // The session has one theme and this clears it whoever set it: only here, on the person's ask.
  await $.theme.clear()
  $.ui.status(undefined)

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
 * is in the person's language by his contract, settles it either way.
 */
async function noteLang($: EngineInterface, text: string, isAnswer: boolean): Promise<void> {
  const seen = langOf(text)

  if (seen === 'ru' || (seen === 'en' && isAnswer)) {
    await setLang($, seen)
  }
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
  // opens with his face.
  on('prompt.submit', async ($, e, next) => {
    if (e.isMeta !== true) {
      await noteLang($, e.command === undefined ? e.text : e.command.args, false)
    }

    if (e.command !== undefined && isStageName(ownShortName(e.command.name, $.plugin.name))) {
      await enter($)
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

  // The director answers in the person's language: his answer says which words the band and
  // the work indicator use from now on.
  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && e.answer !== '' && (await read($, onStage))) {
      await noteLang($, e.answer, true)
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

  // Name and picture where a reply opens. A reply keeps what it was first drawn with: one made
  // before the director came stays plain, one of his keeps his name after he left.
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
    }

    if (!isHis) {
      return next(e)
    }
    // The label speaks the language of the reply it opens.
    const name = NAME[langOf(e.props.text) ?? (await read($, lang))]
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

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={1} alignItems="center">
          <Svg source={AVATAR} alt={name} width={20} height={20} />
          <Text bold>{name}</Text>
        </Box>
        {row}
      </Box>
    )
  })

  // The director's own words while a turn runs. A step's description ("Creating notes.md")
  // says more than any phrase of his, so only the engine's one-word verbs are replaced.
  on('ui.render', { component: 'Spinner' }, async ($, e, next) => {
    const word = e.props.word.trim()

    if (word === '' || /\s/.test(word) || !(await read($, onStage))) {
      return next(e)
    }
    const words = SPINNER_WORDS[await read($, lang)]

    return next({ ...e, props: { ...e.props, word: words[hashOf(word) % words.length] ?? word } })
  })

  // The band above the prompt: who the person is talking to, and the way out.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || !(await read($, onStage))) {
      return next(e)
    }
    const words = await read($, lang)

    if (e.surface === 'terminal') {
      const { Box, Button, Text } = $.ui.resolve(e)

      return (
        <Box flexDirection="row" gap={1}>
          <Text bold>
            {AVATAR_TEXT} {NAME[words]}
          </Text>
          <Text dimColor>{ON_STAGE[words]}</Text>
          <Button key="leave" label={LEAVE[words]} onPress={() => leaveByButton($)} />
        </Box>
      )
    }
    const { Box, Button, Text, Svg } = $.ui.resolve(e)

    return (
      <Box flexDirection="row" gap={1} alignItems="center">
        <Svg source={AVATAR} alt={NAME[words]} width={18} height={18} />
        <Text bold>{NAME[words]}</Text>
        <Text dimColor>{ON_STAGE[words]}</Text>
        <Button key="leave" label={LEAVE[words]} onPress={() => leaveByButton($)} />
      </Box>
    )
  })
}
