// The editing director's face: every word, picture and color the mod shows.
//
// One place to change the persona's name, interface words, picture and look.
// The voice itself (who the director is, how she speaks) is persona/contract.md; a unit test
// holds the two names here to the names written there.

import type { DirectorLang } from '../types'

/** The name on replies and on the band above the prompt. */
export const NAME: Record<DirectorLang, string> = {
  ru: 'Эльза',
  en: 'Elsa',
}

/** The role, dim after the name on the band. */
export const ROLE: Record<DirectorLang, string> = {
  ru: '(режиссёр монтажа)',
  en: '(editing director)',
}

/** The dim words after the name on the band. */
export const ON_STAGE: Record<DirectorLang, string> = {
  ru: 'на связи',
  en: 'on stage',
}

/** The label of the button that sends the director away. */
export const LEAVE: Record<DirectorLang, string> = {
  ru: 'Выйти',
  en: 'Leave',
}

/** The toast after the Leave button. */
export const LEFT_TOAST: Record<DirectorLang, string> = {
  ru: 'Эльза вышла — дальше отвечает обычный Claude.',
  en: 'Elsa has left — plain Claude from here.',
}

/** What /director-off prints when the director was on stage. */
export const LEFT_TEXT: Record<DirectorLang, string> = {
  ru: 'Эльза вышла: дальше отвечает обычный Claude, оформление вернулось к прежнему. Позвать её снова — /director или любая команда плагина.',
  en: 'Elsa has left: plain Claude answers from here and the usual look is back. To call her again: /director or any command of the plugin.',
}

/** What /director-off prints when there was nobody to send away. */
export const NOT_ON_STAGE_TEXT: Record<DirectorLang, string> = {
  ru: 'Эльзы в этом разговоре сейчас нет. Позвать — /director.',
  en: 'Elsa is not in this conversation now. To call her: /director.',
}

/** What /director prints. */
export const HERE_TEXT: Record<DirectorLang, string> = {
  ru: 'Эльза на связи. Отпустить её — кнопка «Выйти» над полем ввода или /director-off.',
  en: 'Elsa is on stage. To let her go: the Leave button above the prompt, or /director-off.',
}

/** What /director prints when the environment switch holds the persona off. */
export const SWITCHED_OFF_TEXT: Record<DirectorLang, string> = {
  ru: 'Эльза выключена переменной AI_MOVIE_COMPANY_DIRECTOR=off.',
  en: 'Elsa is switched off by AI_MOVIE_COMPANY_DIRECTOR=off.',
}

/**
 * The director's words while a turn runs. Generic on purpose: the indicator shows during any
 * step, and the director never claims to watch a picture she has not seen.
 */
export const SPINNER_WORDS: Record<DirectorLang, readonly string[]> = {
  ru: ['Работаю с записью', 'Думаю над монтажом', 'Сверяюсь с материалом'],
  en: ['Working on the recording', 'Thinking about the cut', 'Checking the material'],
}

/** The accent of the look, the band's edge and the progress fill. */
export const ACCENT = '#b4532a'

/**
 * The release on the band, after the name. A unit test holds both to
 * .claude-plugin/plugin.json and to the top heading of CHANGELOG.md.
 */
export const VERSION = '0.8.2'
export const RELEASED = '2026-11-19'

/** The release date as the person reads it. */
export function releasedText(words: DirectorLang): string {
  const [year, month, day] = RELEASED.split('-')

  return words === 'ru' ? `${day}.${month}.${year}` : RELEASED
}

/**
 * Elsa's face: a woman of about forty with an auburn bob, gold earrings and a warm smile. She
 * blinks, her head sways a little and the circle behind her slowly changes colour. SMIL only,
 * no script; where a surface draws it as a still image she simply holds still.
 */
export const AVATAR =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">' +
  '<defs>' +
  '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">' +
  '<stop offset="0" stop-color="#ff8a5c"><animate attributeName="stop-color" ' +
  'values="#ff8a5c;#ffb347;#5fd3bc;#8f84ff;#ff8a5c" dur="30s" repeatCount="indefinite"/></stop>' +
  '<stop offset="1" stop-color="#c86dd7"><animate attributeName="stop-color" ' +
  'values="#c86dd7;#ff6f91;#ffb347;#4fc3e8;#c86dd7" dur="30s" repeatCount="indefinite"/></stop>' +
  '</linearGradient>' +
  '<clipPath id="disc"><circle cx="16" cy="16" r="16"/></clipPath>' +
  '</defs>' +
  '<circle cx="16" cy="16" r="16" fill="url(#bg)"/>' +
  '<g clip-path="url(#disc)">' +
  '<g><animateTransform attributeName="transform" type="rotate" values="-3 16 22;3 16 22;-3 16 22" ' +
  'dur="7s" repeatCount="indefinite"/>' +
  '<path d="M7 18C7 8 11.5 5 16 5S25 8 25 18L25 24.5C23 25.5 21 25.5 20 24L12 24C11 25.5 9 25.5 7 24.5Z" fill="#8a3b1f"/>' +
  '<rect x="13.6" y="21" width="4.8" height="5" fill="#eebc9c"/>' +
  '<path d="M5 33C6 27 10.5 25.5 16 25.5S26 27 27 33Z" fill="#2f3d63"/>' +
  '<path d="M13 25.6L16 28.5L19 25.6" fill="none" stroke="#f3d27a" stroke-width="0.9"/>' +
  '<ellipse cx="16" cy="16" rx="6" ry="7" fill="#f6d2b8"/>' +
  '<path d="M9.8 14C10.6 8.5 14.5 7.2 17.8 7.8C21 8.5 22.4 11 22.6 14.6C19.6 12.1 15 11.6 9.8 14Z" fill="#9b4423"/>' +
  '<circle cx="9.9" cy="19.3" r="0.8" fill="#f3d27a"/><circle cx="22.1" cy="19.3" r="0.8" fill="#f3d27a"/>' +
  '<path d="M12.4 14.3Q13.6 13.6 14.8 14.2M17.2 14.2Q18.4 13.6 19.6 14.3" stroke="#6b2f17" ' +
  'stroke-width="0.6" fill="none" stroke-linecap="round"/>' +
  '<ellipse cx="13.6" cy="16.1" rx="0.85" ry="1.1" fill="#2a1a12"><animate attributeName="ry" ' +
  'values="1.1;1.1;0.12;1.1;1.1" keyTimes="0;0.9;0.94;0.98;1" dur="4.6s" repeatCount="indefinite"/></ellipse>' +
  '<ellipse cx="18.4" cy="16.1" rx="0.85" ry="1.1" fill="#2a1a12"><animate attributeName="ry" ' +
  'values="1.1;1.1;0.12;1.1;1.1" keyTimes="0;0.9;0.94;0.98;1" dur="4.6s" repeatCount="indefinite"/></ellipse>' +
  '<circle cx="12.3" cy="18.6" r="1.1" fill="#f29a8e" opacity="0.45"/>' +
  '<circle cx="19.7" cy="18.6" r="1.1" fill="#f29a8e" opacity="0.45"/>' +
  '<path d="M13.9 19.8Q16 21.5 18.1 19.8" stroke="#b5503f" stroke-width="0.9" fill="none" stroke-linecap="round"/>' +
  '</g></g></svg>'

/** The picture's stand-in where a surface draws no SVG (the terminal). */
export const AVATAR_TEXT = '👩'

/**
 * The band's colour: a light pastel whose hue drifts slowly to and fro inside the warm range
 * (peach, apricot, honey: WARM_HUES), once there and back in BAND_CYCLE_S, so it matches the
 * session's warm look and the change is felt over minutes, never seen as a jump. Dark ink on it
 * in both themes.
 */
export const BAND_CYCLE_S = 180
export const WARM_HUES = { from: 12, to: 46 } as const
export const BAND_INK = '#2a1b13'
export const BAND_INK_SOFT = '#5a4337'
export const BAND_EDGE = '#e07a4f'

/** The band's hue (degrees) at a moment, in seconds: a triangle wave across WARM_HUES. */
export function warmHue(seconds: number): number {
  const phase = (((seconds / BAND_CYCLE_S) % 1) + 1) % 1
  const there = phase < 0.5 ? phase * 2 : 2 - phase * 2

  return Math.round(WARM_HUES.from + (WARM_HUES.to - WARM_HUES.from) * there)
}

/** A pastel at hue `hue` (degrees), as hex: lightness 90%, saturation 85%. */
export function bandColor(hue: number): string {
  const s = 0.85
  const l = 0.9
  const c = (1 - Math.abs(2 * l - 1)) * s
  const h = (((hue % 360) + 360) % 360) / 60
  const x = c * (1 - Math.abs((h % 2) - 1))
  const [r, g, b] =
    h < 1 ? [c, x, 0] : h < 2 ? [x, c, 0] : h < 3 ? [0, c, x] : h < 4 ? [0, x, c] : h < 5 ? [x, 0, c] : [c, 0, x]
  const m = l - c / 2
  const hex = (v: number) => Math.round((v + m) * 255).toString(16).padStart(2, '0')

  return `#${hex(r)}${hex(g)}${hex(b)}`
}

/** The steps of the engine as the person reads them on the progress bar. */
export const STEP_WORDS: Record<DirectorLang, Readonly<Record<string, string>>> = {
  ru: {
    S0: 'Принимаю запись',
    S1: 'Вычитываю субтитры',
    S2: 'Выравниваю субтитры по речи',
    S3a: 'Выбираю фрагменты',
    S3b: 'Готовлю фрагменты к просмотру',
    S4: 'Режу фрагменты',
    S5: 'Собираю ролики',
    S5v: 'Проверяю кадр',
    S6a: 'Пишу тексты на экран',
    S6b: 'Пишу названия и описания',
    S7: 'Делаю обложки',
    S8: 'Загружаю на YouTube',
    S10: 'Связываю ролики ссылками',
    S9: 'Собираю пакет',
    PUB: 'Публикую',
    S11: 'Пишу отчёт',
    preview: 'Готовлю превью',
  },
  en: {
    S0: 'Taking in the recording',
    S1: 'Proofreading the subtitles',
    S2: 'Timing the subtitles to the speech',
    S3a: 'Choosing fragments',
    S3b: 'Getting fragments ready for review',
    S4: 'Cutting fragments',
    S5: 'Rendering the videos',
    S5v: 'Checking the picture',
    S6a: 'Writing on-screen texts',
    S6b: 'Writing titles and descriptions',
    S7: 'Making thumbnails',
    S8: 'Uploading to YouTube',
    S10: 'Linking the videos',
    S9: 'Packing up',
    PUB: 'Publishing',
    S11: 'Writing the report',
    preview: 'Making a preview',
  },
}

/** "~2 мин" — what is left, as the person reads it; undefined while it is still unknown. */
export function etaText(words: DirectorLang, seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) {
    return words === 'ru' ? 'считаю время' : 'estimating'
  }

  if (seconds < 60) {
    const s = Math.max(Math.round(seconds), 1)

    return words === 'ru' ? `ещё ~${s} с` : `~${s} s left`
  }
  const minutes = Math.round(seconds / 60)

  return words === 'ru' ? `ещё ~${minutes} мин` : `~${minutes} min left`
}

/**
 * The look of a session the director is on stage in ("Elsa's room"): very light warm cream and
 * peach surfaces by day, muted warm browns by night, a terracotta accent. Quiet on purpose: the
 * person tells Elsa's window from the others by its warmth, and nothing else moves. The input of
 * make-colors.js, checked there against the desktop app's own contrast bar (no "won't paint" line).
 */
export const LOOK_INPUT = {
  label: "Elsa's room",
  roles: {
    accent: ACCENT,
    danger: '#b3392f',
    success: '#3a7f4b',
    warning: '#a86f14',
    pro: '#6d4f94',
  },
  light: {
    gray: {
      anchors: {
        'surface-0': '#fbefe3',
        'surface-1': '#fdf4ea',
        'surface-2': '#fff9f3',
        'text-primary': '#2a1b13',
        'text-secondary': '#4e3a2f',
        'text-muted': '#6a5244',
      },
    },
  },
  dark: {
    gray: {
      anchors: {
        'surface-0': '#1c1612',
        'surface-1': '#221b16',
        'surface-2': '#271f19',
        'surface-3': '#2b231c',
        'text-primary': '#f4e9df',
        'text-secondary': '#dac9bb',
        'text-muted': '#b9a596',
      },
    },
  },
} as const

/**
 * The commands and skills of this plugin that are a conversation with the director. The
 * technical ones (doctor, learn, judge) are not: they neither call her nor send her away.
 */
export const STAGE_COMMANDS: readonly string[] = [
  'start',
  'run',
  'review',
  'approve',
  'status',
  'help',
  'connect',
  'pipeline',
]

/** The heading the contract page opens with: how a note names it to the model. */
export const CONTRACT_TITLE = 'Editing director — the contract'

/** Put before the contract where it is handed to the model inside a command's or skill's text. */
export const CONTRACT_LEAD =
  'The editing director (Elsa) is on stage in this conversation. The page below is the contract: who ' +
  'you are while you talk with the person here. It is not a task and needs no acknowledgement; ' +
  'follow it together with the instructions above.'

/** Told to the model when the director comes back in a conversation that already has the contract. */
export const BACK_NOTE =
  `The editing director is back on stage: the page "${CONTRACT_TITLE}" given earlier in this ` +
  'conversation applies again.'

/** Told to the model when the person sends the director away. */
export const LEFT_NOTE =
  'The person asked the editing director to leave (the Leave button or /director-off). From now ' +
  `on answer as plain Claude: drop the director's voice and the page "${CONTRACT_TITLE}". The ` +
  "plugin's engine rules and gates are unchanged. The director returns when the person runs " +
  '/director or a command of the ai-movie-company plugin.'

/** Told to the model when the environment switch holds the persona off. */
export const SWITCHED_OFF_NOTE =
  'The editing director is switched off here (AI_MOVIE_COMPANY_DIRECTOR=off): do not read ' +
  'persona/contract.md and do not speak as the director; answer in a plain neutral voice.'

/** Told to the model when the person calls the director with /director, no job named yet. */
export const CALLED_NOTE =
  'The person called the editing director with /director and has not said what they want yet. ' +
  'On their next message, answer as the director.'

/** What /elsa prints while the guided start is queued. */
export const ELSA_TEXT: Record<DirectorLang, string> = {
  ru: 'Эльза выходит на сцену…',
  en: 'Elsa is coming on stage…',
}

/**
 * Told to the model beside a message that only calls Elsa by name ("Эльза", "Привет, Эльза"):
 * the person means the plugin's guided start.
 */
export const NAME_CALL_NOTE =
  'The person called the editing director by name: they want to start working with her. Begin the ' +
  'guided start right now: invoke the Skill tool with skill "ai-movie-company:start" (no arguments) ' +
  'and follow it. Greet the person inside that start; do not answer the greeting separately first.'

/** Sent as a prompt when /elsa cannot queue the start command itself. */
export const START_FALLBACK =
  'The person typed /elsa: start working with the editing director. Invoke the Skill tool with skill ' +
  '"ai-movie-company:start" (no arguments) and follow it.'

/** Greetings that may stand beside the name in a message that only calls her. */
const GREETINGS: readonly string[] = [
  'привет',
  'приветик',
  'здравствуй',
  'здравствуйте',
  'добрый день',
  'доброе утро',
  'добрый вечер',
  'салют',
  'хай',
  'эй',
  'hi',
  'hello',
  'hey',
  'good morning',
  'good afternoon',
  'good evening',
]

/** The spellings of the name a message may call her by. */
const NAMES: readonly string[] = ['эльза', 'эльзочка', 'elsa']

/**
 * Whether a message only calls Elsa: her name alone, or with a greeting before or after it, in
 * any case and with any punctuation ("Эльза", "Привет, Эльза!", "Elsa, hi").
 */
export function isNameCall(text: string): boolean {
  const words = text
    .toLowerCase()
    .replace(/[^\p{L}\s]+/gu, ' ')
    .trim()
    .replace(/\s+/g, ' ')

  if (words === '' || words.length > 40) {
    return false
  }

  return NAMES.some(
    name =>
      words === name ||
      GREETINGS.some(greeting => words === `${greeting} ${name}` || words === `${name} ${greeting}`),
  )
}

/** The bar's words for a turn that runs before its first tool call. */
export const THINKING: Record<DirectorLang, string> = {
  ru: 'Думаю',
  en: 'Thinking',
}

/** "Шаг 2 из 5: Рендерю ролик" — the model's task list on the bar. */
export function stepOfText(words: DirectorLang, step: number, total: number, task: string): string {
  return words === 'ru' ? `Шаг ${step} из ${total}: ${task}` : `Step ${step} of ${total}: ${task}`
}

/** "идёт 1:05" — how long the running turn has gone on, as the bar shows it. */
export function elapsedText(words: DirectorLang, ms: number): string {
  const total = Math.max(Math.floor(ms / 1000), 0)
  const minutes = Math.floor(total / 60)
  const seconds = String(total % 60).padStart(2, '0')

  return words === 'ru' ? `идёт ${minutes}:${seconds}` : `${minutes}:${seconds} so far`
}
