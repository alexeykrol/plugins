// Elsa's moving face on the band: one SVG per mood and frame, built once at load.
//
// Two kinds of motion, because a surface may draw an SVG as a still image: SMIL inside each
// picture (a slow sway, a blink, a loader dot), which runs where the picture is drawn
// interactive, and a swap between two or three frames of the current mood that the mod drives
// by its own clock (FRAME_MS), which runs wherever the band is drawn at all.

import type { DirectorMood } from '../types'

/** How often the mod moves the face on by one frame while the director is on stage. */
export const FRAME_MS = 700

type Eyes = 'open' | 'closed' | 'left' | 'right' | 'down' | 'wide'
type Brows = 'calm' | 'raised' | 'focused' | 'worried'
type Mouth = 'smile' | 'beam' | 'flat' | 'oh' | 'wavy'

type Pose = {
  eyes: Eyes
  brows: Brows
  mouth: Mouth
  /** Head tilt in degrees, around the neck. */
  tilt?: number
}

type MoodLook = {
  /** The disc behind her: two stops of a diagonal gradient. */
  disc: readonly [string, string]
  /** The frames of this mood. */
  poses: readonly Pose[]
  /** Which frame each beat shows: the face moves on by one entry every FRAME_MS. */
  beats: readonly number[]
  /** Something beside the face that tells the mood at a glance (SMIL inside). */
  extra: string
  /** Whether the head sways slowly (SMIL). */
  isSwaying: boolean
}

const EYE_Y = 16.1

function eyesOf(eyes: Eyes): string {
  const dx = eyes === 'left' ? -0.6 : eyes === 'right' ? 0.6 : 0
  const dy = eyes === 'down' ? 0.6 : 0
  const ry = eyes === 'closed' ? 0.12 : eyes === 'wide' ? 1.35 : 1.1
  // A blink of her own runs inside every open-eyed picture, so she blinks even between swaps.
  const blink =
    eyes === 'closed'
      ? ''
      : `<animate attributeName="ry" values="${ry};${ry};0.12;${ry};${ry}" keyTimes="0;0.9;0.94;0.98;1" ` +
        'dur="4.6s" repeatCount="indefinite"/>'
  const one = (cx: number) =>
    `<ellipse cx="${cx + dx}" cy="${EYE_Y + dy}" rx="0.85" ry="${ry}" fill="#2a1a12">${blink}</ellipse>`

  return one(13.6) + one(18.4)
}

function browsOf(brows: Brows): string {
  const paths: Record<Brows, string> = {
    calm: 'M12.4 14.3Q13.6 13.6 14.8 14.2M17.2 14.2Q18.4 13.6 19.6 14.3',
    raised: 'M12.4 13.3Q13.6 12.4 14.8 13.1M17.2 13.1Q18.4 12.4 19.6 13.3',
    focused: 'M12.4 14.0Q13.6 14.1 14.8 14.5M17.2 14.5Q18.4 14.1 19.6 14.0',
    worried: 'M12.4 14.6Q13.6 14.2 14.8 13.4M17.2 13.4Q18.4 14.2 19.6 14.6',
  }

  return (
    `<path d="${paths[brows]}" stroke="#6b2f17" stroke-width="0.6" fill="none" stroke-linecap="round"/>`
  )
}

function mouthOf(mouth: Mouth): string {
  switch (mouth) {
    case 'smile':
      return '<path d="M13.9 19.8Q16 21.5 18.1 19.8" stroke="#b5503f" stroke-width="0.9" fill="none" stroke-linecap="round"/>'
    case 'beam':
      return '<path d="M13.6 19.5Q16 22.6 18.4 19.5Z" fill="#9c3d2f"/><path d="M14.3 19.8Q16 20.4 17.7 19.8" stroke="#fff" stroke-width="0.5" fill="none"/>'
    case 'flat':
      return '<path d="M14.4 20.2Q16 20.7 17.6 20.2" stroke="#b5503f" stroke-width="0.8" fill="none" stroke-linecap="round"/>'
    case 'oh':
      return '<ellipse cx="16" cy="20.3" rx="0.9" ry="0.75" fill="#9c3d2f"/>'
    case 'wavy':
      return '<path d="M13.8 20.6Q14.8 19.8 15.9 20.6T18.2 20.4" stroke="#b5503f" stroke-width="0.8" fill="none" stroke-linecap="round"/>'
  }
}

/** A dot that runs round the disc's edge: she is busy. */
const LOADER =
  '<circle cx="16" cy="2.2" r="1.5" fill="#fff8ec" opacity="0.95">' +
  '<animateTransform attributeName="transform" type="rotate" values="0 16 16;360 16 16" dur="1.6s" ' +
  'repeatCount="indefinite"/></circle>'

/** A small question bubble at her shoulder: she waits for the person. */
const QUESTION =
  '<g><animateTransform attributeName="transform" type="translate" values="0 0;0 -0.8;0 0" dur="1.8s" ' +
  'repeatCount="indefinite"/><circle cx="26.2" cy="6.6" r="3.6" fill="#fff8ec"/>' +
  '<text x="26.2" y="8.4" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" font-size="5.4" ' +
  'font-weight="bold" fill="#b4532a">?</text></g>'

/** A drop at her temple: something went wrong. */
const DROP =
  '<path d="M23.2 10.4Q24.6 12.6 24.6 13.4A1.4 1.4 0 0 1 21.8 13.4Q21.8 12.6 23.2 10.4Z" fill="#8fd3ff">' +
  '<animate attributeName="opacity" values="1;0.35;1" dur="1.4s" repeatCount="indefinite"/></path>'

const LOOKS: Record<DirectorMood, MoodLook> = {
  idle: {
    disc: ['#ff9b6a', '#ffc46b'],
    poses: [
      { eyes: 'open', brows: 'calm', mouth: 'smile' },
      { eyes: 'closed', brows: 'calm', mouth: 'smile' },
      { eyes: 'open', brows: 'raised', mouth: 'beam', tilt: 3 },
    ],
    beats: [0, 0, 0, 0, 1, 0, 0, 2, 2, 0, 0, 1],
    extra: '',
    isSwaying: true,
  },
  working: {
    disc: ['#ffb347', '#ff8a5c'],
    poses: [
      { eyes: 'left', brows: 'focused', mouth: 'flat' },
      { eyes: 'right', brows: 'focused', mouth: 'flat' },
      { eyes: 'down', brows: 'focused', mouth: 'flat', tilt: -2 },
    ],
    beats: [0, 0, 1, 1, 0, 0, 1, 1, 2, 2, 2, 0],
    extra: LOADER,
    isSwaying: false,
  },
  waiting: {
    disc: ['#ffd27a', '#ff9f80'],
    poses: [
      { eyes: 'wide', brows: 'raised', mouth: 'oh' },
      { eyes: 'open', brows: 'raised', mouth: 'smile', tilt: 6 },
      { eyes: 'closed', brows: 'raised', mouth: 'smile', tilt: 6 },
    ],
    beats: [0, 0, 0, 1, 1, 1, 2, 1, 1, 0, 0, 0],
    extra: QUESTION,
    isSwaying: false,
  },
  trouble: {
    disc: ['#ff7a6b', '#d65a4a'],
    poses: [
      { eyes: 'open', brows: 'worried', mouth: 'wavy' },
      { eyes: 'down', brows: 'worried', mouth: 'wavy', tilt: -4 },
    ],
    beats: [0, 0, 0, 1, 1, 1],
    extra: DROP,
    isSwaying: false,
  },
}

function faceSvg(mood: DirectorMood, pose: Pose): string {
  const look = LOOKS[mood]
  const sway = look.isSwaying
    ? '<animateTransform attributeName="transform" type="rotate" values="-3 16 22;3 16 22;-3 16 22" ' +
      'dur="7s" repeatCount="indefinite"/>'
    : ''
  const tilt = pose.tilt === undefined ? '' : ` transform="rotate(${pose.tilt} 16 22)"`

  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">' +
    '<defs>' +
    `<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${look.disc[0]}"/>` +
    `<stop offset="1" stop-color="${look.disc[1]}"/></linearGradient>` +
    '<clipPath id="disc"><circle cx="16" cy="16" r="16"/></clipPath>' +
    '</defs>' +
    '<circle cx="16" cy="16" r="16" fill="url(#bg)"/>' +
    `<g clip-path="url(#disc)"><g${tilt}><g>${sway}` +
    '<path d="M7 18C7 8 11.5 5 16 5S25 8 25 18L25 24.5C23 25.5 21 25.5 20 24L12 24C11 25.5 9 25.5 7 24.5Z" fill="#8a3b1f"/>' +
    '<rect x="13.6" y="21" width="4.8" height="5" fill="#eebc9c"/>' +
    '<path d="M5 33C6 27 10.5 25.5 16 25.5S26 27 27 33Z" fill="#2f3d63"/>' +
    '<path d="M13 25.6L16 28.5L19 25.6" fill="none" stroke="#f3d27a" stroke-width="0.9"/>' +
    '<ellipse cx="16" cy="16" rx="6" ry="7" fill="#f6d2b8"/>' +
    '<path d="M9.8 14C10.6 8.5 14.5 7.2 17.8 7.8C21 8.5 22.4 11 22.6 14.6C19.6 12.1 15 11.6 9.8 14Z" fill="#9b4423"/>' +
    '<circle cx="9.9" cy="19.3" r="0.8" fill="#f3d27a"/><circle cx="22.1" cy="19.3" r="0.8" fill="#f3d27a"/>' +
    browsOf(pose.brows) +
    eyesOf(pose.eyes) +
    '<circle cx="12.3" cy="18.6" r="1.1" fill="#f29a8e" opacity="0.45"/>' +
    '<circle cx="19.7" cy="18.6" r="1.1" fill="#f29a8e" opacity="0.45"/>' +
    mouthOf(pose.mouth) +
    '</g></g></g>' +
    look.extra +
    '</svg>'
  )
}

/** Every face, by mood and frame, made once. */
export const FACES: Readonly<Record<DirectorMood, readonly string[]>> = {
  idle: LOOKS.idle.poses.map(pose => faceSvg('idle', pose)),
  working: LOOKS.working.poses.map(pose => faceSvg('working', pose)),
  waiting: LOOKS.waiting.poses.map(pose => faceSvg('waiting', pose)),
  trouble: LOOKS.trouble.poses.map(pose => faceSvg('trouble', pose)),
}

/** The frame a mood shows at a beat of the mod's clock. */
export function frameAt(mood: DirectorMood, beat: number): number {
  const beats = LOOKS[mood].beats

  return beats[((beat % beats.length) + beats.length) % beats.length] ?? 0
}

/** The face to draw for a mood at a beat of the mod's clock. */
export function faceAt(mood: DirectorMood, beat: number): string {
  return FACES[mood][frameAt(mood, beat)] ?? FACES[mood][0] ?? ''
}

/** The mood as a person reads it, for the picture's alt text. */
export const MOOD_WORDS: Record<'ru' | 'en', Record<DirectorMood, string>> = {
  ru: { idle: 'довольна', working: 'работает', waiting: 'ждёт ответа', trouble: 'что-то пошло не так' },
  en: { idle: 'pleased', working: 'working', waiting: 'waiting for you', trouble: 'something went wrong' },
}
