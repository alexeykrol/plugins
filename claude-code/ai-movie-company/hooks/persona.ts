// The editing director's face: every word, picture and color the mod shows.
//
// One place to change the persona's name, interface words, picture and look.
// The voice itself (who the director is, how he speaks) is persona/contract.md; a unit test
// holds the two names here to the names written there.

import type { DirectorLang } from '../types'

/** The name on replies and on the band above the prompt. */
export const NAME: Record<DirectorLang, string> = {
  ru: 'Режиссёр монтажа',
  en: 'Editing director',
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
  ru: 'Режиссёр монтажа вышел — дальше отвечает обычный Claude.',
  en: 'The editing director has left — plain Claude from here.',
}

/** What /director-off prints when the director was on stage. */
export const LEFT_TEXT: Record<DirectorLang, string> = {
  ru: 'Режиссёр монтажа вышел: дальше отвечает обычный Claude, оформление вернулось к прежнему. Позвать снова — /director или любая команда плагина.',
  en: 'The editing director has left: plain Claude answers from here and the usual look is back. To call him again: /director or any command of the plugin.',
}

/** What /director-off prints when there was nobody to send away. */
export const NOT_ON_STAGE_TEXT: Record<DirectorLang, string> = {
  ru: 'Режиссёра монтажа в этом разговоре сейчас нет. Позвать — /director.',
  en: 'The editing director is not in this conversation now. To call him: /director.',
}

/** What /director prints. */
export const HERE_TEXT: Record<DirectorLang, string> = {
  ru: 'Режиссёр монтажа на связи. Выйти — кнопка «Выйти» над полем ввода или /director-off.',
  en: 'The editing director is on stage. To leave: the Leave button above the prompt, or /director-off.',
}

/** What /director prints when the environment switch holds the persona off. */
export const SWITCHED_OFF_TEXT: Record<DirectorLang, string> = {
  ru: 'Режиссёр монтажа выключен переменной AI_MOVIE_COMPANY_DIRECTOR=off.',
  en: 'The editing director is switched off by AI_MOVIE_COMPANY_DIRECTOR=off.',
}

/**
 * The director's words while a turn runs. Generic on purpose: the indicator shows during any
 * step, and the director never claims to watch a picture he has not seen.
 */
export const SPINNER_WORDS: Record<DirectorLang, readonly string[]> = {
  ru: ['Работаю с записью', 'Думаю над монтажом', 'Сверяюсь с материалом'],
  en: ['Working on the recording', 'Thinking about the cut', 'Checking the material'],
}

/** The accent of the look and of the picture. */
export const ACCENT = '#0f7f8c'

/** The picture beside the name: a clapperboard in a circle. */
export const AVATAR =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">' +
  `<circle cx="16" cy="16" r="15" fill="${ACCENT}"/>` +
  '<rect x="8" y="12" width="16" height="10" rx="2" fill="#ffffff"/>' +
  '<path d="M8 12l3-4h3l-3 4zM14 12l3-4h3l-3 4zM20 12l3-4h1v4z" fill="#ffffff"/>' +
  '</svg>'

/** The picture's stand-in where a surface draws no SVG (the terminal). */
export const AVATAR_TEXT = '🎬'

/**
 * The look of a session the director is on stage in ("Cutting room"): cool gray surfaces and a
 * teal accent, by day and by night. The input of make-colors.js, checked there against the
 * desktop app's own contrast bar.
 */
export const LOOK_INPUT = {
  label: 'Cutting room',
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
        'surface-0': '#edf1f5',
        'surface-1': '#f2f5f8',
        'surface-2': '#f8fafc',
        'text-primary': '#131c25',
        'text-secondary': '#384653',
        'text-muted': '#52606c',
      },
    },
  },
  dark: {
    gray: {
      anchors: {
        'surface-0': '#11161b',
        'surface-1': '#161b21',
        'surface-2': '#1a2026',
        'surface-3': '#1d242a',
        'text-primary': '#e8eef4',
        'text-secondary': '#c2ccd6',
        'text-muted': '#9eabb7',
      },
    },
  },
} as const

/**
 * The commands and skills of this plugin that are a conversation with the director. The
 * technical ones (doctor, learn, judge) are not: they neither call him nor send him away.
 */
export const STAGE_COMMANDS: readonly string[] = [
  'start',
  'run',
  'review',
  'approve',
  'status',
  'help',
  'pipeline',
]

/** The heading the contract page opens with: how a note names it to the model. */
export const CONTRACT_TITLE = 'Editing director — the contract'

/** Put before the contract where it is handed to the model inside a command's or skill's text. */
export const CONTRACT_LEAD =
  'The editing director is on stage in this conversation. The page below is the contract: who ' +
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
