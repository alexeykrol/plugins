// The welcome splash drawn above Elsa's first reply: "3D from code".
//
// A surface draws an SVG in a sandbox with no script, so there is no WebGL and no Three.js.
// Instead the mod computes a small 3D scene here, once at load: a carousel of film frames turning
// round a vertical axis, seen from slightly above through a perspective camera. Each frame's
// projected corners at every keyframe are baked into SMIL `values` lists, so the picture turns
// on its own with no script. Frames on the far half are drawn behind the clapperboard in the
// middle and those on the near half in front of it (each frame has a copy in both layers and
// fades between them where it is edge-on). Sparkles twinkle, the clapper claps, the greeting
// fades in.

import type { DirectorLang } from '../types'

/** The splash's own size in CSS pixels (its viewBox is twice as fine). */
export const SPLASH_WIDTH = 420
export const SPLASH_HEIGHT = 224

const VIEW_W = 480
const VIEW_H = 256
/** One turn of the carousel, in seconds. */
const TURN_S = 9
/** Keyframes per turn (one every 12 degrees). */
const KEYS = 30
const FRAMES = 8

/** Camera: distance from the axis, tilt (radians) and pixels per world unit at the axis. */
const CAMERA_D = 3.4
const TILT = 0.22
const SCALE = 165
const CX = VIEW_W / 2
const CY = 100
/** A film frame's size in world units (the carousel's radius is 1). */
const FRAME_W = 0.44
const FRAME_H = 0.32

/** The pictures inside the frames: warm, like the band. */
const PICTURES = ['#ffb38a', '#ffd27a', '#ff8f70', '#f7c59f', '#ffa86b', '#ffcf9e', '#ff9e8a', '#ffe0a3']
const FILM = '#3a2620'

const GREETING: Record<DirectorLang, string> = {
  ru: 'Привет! Я Эльза',
  en: "Hi! I'm Elsa",
}

const SUBTITLE: Record<DirectorLang, string> = {
  ru: 'режиссёр монтажа · AI Movie Company',
  en: 'editing director · AI Movie Company',
}

type Point = { x: number; y: number }

function num(value: number): string {
  return String(Math.round(value * 10) / 10)
}

/** World (x, y, z) -> the picture: tilt round x, then perspective. */
function project(x: number, y: number, z: number): { at: Point; depth: number } {
  const ty = y * Math.cos(TILT) - z * Math.sin(TILT)
  const tz = y * Math.sin(TILT) + z * Math.cos(TILT)
  const s = (SCALE * CAMERA_D) / (CAMERA_D - tz)

  return { at: { x: CX + x * s, y: CY - ty * s }, depth: tz }
}

/** One frame's outline at the carousel angle `theta`, and how near it is (-1 far .. 1 near). */
function frameAt(theta: number): { d: string; nearness: number } {
  const cx = Math.sin(theta)
  const cz = Math.cos(theta)
  // The tangent of the ring: frames stand along it, like a carousel's cars.
  const tx = Math.cos(theta)
  const tz = -Math.sin(theta)
  const corners = [
    [-1, 1],
    [1, 1],
    [1, -1],
    [-1, -1],
  ].map(([u, v]) =>
    project(cx + (tx * (u ?? 0) * FRAME_W) / 2, ((v ?? 0) * FRAME_H) / 2, cz + (tz * (u ?? 0) * FRAME_W) / 2),
  )
  const d = corners.map((one, i) => `${i === 0 ? 'M' : 'L'}${num(one.at.x)} ${num(one.at.y)}`).join('') + 'Z'

  return { d, nearness: cz }
}

function carousel(): { back: string; front: string } {
  let back = ''
  let front = ''

  for (let i = 0; i < FRAMES; i++) {
    const shapes: string[] = []
    const farOpacity: string[] = []
    const nearOpacity: string[] = []

    for (let k = 0; k <= KEYS; k++) {
      const theta = (2 * Math.PI * i) / FRAMES + (2 * Math.PI * k) / KEYS
      const { d, nearness } = frameAt(theta)
      shapes.push(d)
      farOpacity.push(nearness <= 0 ? num(0.45 + 0.35 * (1 + nearness)) : '0')
      nearOpacity.push(nearness > 0 ? num(0.8 + 0.2 * nearness) : '0')
    }
    const d = shapes.join(';')
    const path = (opacity: string[]) =>
      `<path d="${shapes[0]}" fill="${PICTURES[i % PICTURES.length]}" stroke="${FILM}" stroke-width="5" ` +
      `stroke-linejoin="round" opacity="${opacity[0]}">` +
      `<animate attributeName="d" values="${d}" dur="${TURN_S}s" repeatCount="indefinite"/>` +
      `<animate attributeName="opacity" values="${opacity.join(';')}" dur="${TURN_S}s" repeatCount="indefinite"/>` +
      '</path>'
    back += path(farOpacity)
    front += path(nearOpacity)
  }

  return { back, front }
}

/** A tiny deterministic random, so the sparkles sit in the same places every load. */
function seeded(seed: number): () => number {
  let state = seed

  return () => {
    state = (state * 16807) % 2147483647

    return state / 2147483647
  }
}

function sparkles(): string {
  const random = seeded(20261119)
  let out = ''

  for (let i = 0; i < 12; i++) {
    const x = 24 + random() * (VIEW_W - 48)
    const y = 14 + random() * 150
    const r = 2.5 + random() * 3.5
    const delay = num(random() * 4)
    const star = `M${num(x)} ${num(y - r)}Q${num(x)} ${num(y)} ${num(x + r)} ${num(y)}Q${num(x)} ${num(y)} ${num(x)} ${num(
      y + r,
    )}Q${num(x)} ${num(y)} ${num(x - r)} ${num(y)}Q${num(x)} ${num(y)} ${num(x)} ${num(y - r)}Z`
    out +=
      `<path d="${star}" fill="#fff6d6" opacity="0">` +
      `<animate attributeName="opacity" values="0;1;0;0" keyTimes="0;0.15;0.35;1" dur="4s" begin="${delay}s" ` +
      'repeatCount="indefinite"/></path>'
  }

  return out
}

/** The clapperboard in the middle of the carousel; its stick claps every three seconds. */
function clapper(): string {
  const stripes = (top: number, height: number, fill: string) =>
    [0, 1, 2, 3, 4]
      .map(i => `<path d="M${208 + i * 14} ${top}h8l-7 ${height}h-8z" fill="${fill}"/>`)
      .join('')

  return (
    '<g>' +
    '<rect x="203" y="56" width="74" height="48" rx="5" fill="#2b1d18"/>' +
    '<rect x="203" y="56" width="74" height="10" fill="#fff4e6"/>' +
    stripes(56, 10, '#2b1d18') +
    '<text x="240" y="82" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" font-size="10" ' +
    'font-weight="bold" fill="#ffd9b0" letter-spacing="1">ELSA</text>' +
    '<text x="240" y="96" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" font-size="7" ' +
    'fill="#e8b48e" letter-spacing="0.6">TAKE 1 · SCENE 1</text>' +
    '<g>' +
    '<animateTransform attributeName="transform" type="rotate" values="0 203 54;-26 203 54;-26 203 54;0 203 54;0 203 54" ' +
    'keyTimes="0;0.1;0.55;0.62;1" dur="3s" repeatCount="indefinite"/>' +
    '<rect x="203" y="44" width="74" height="12" rx="2" fill="#2b1d18"/>' +
    stripes(44, 12, '#fff4e6') +
    '</g></g>'
  )
}

function splashSvg(words: DirectorLang): string {
  const { back, front } = carousel()

  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${VIEW_W} ${VIEW_H}" width="${VIEW_W}" height="${VIEW_H}">` +
    '<defs>' +
    '<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff4e8"/>' +
    '<stop offset="1" stop-color="#ffd8c0"/></linearGradient>' +
    '<radialGradient id="glow" cx="0.5" cy="0.42" r="0.5"><stop offset="0" stop-color="#ffe9b8" stop-opacity="0.95"/>' +
    '<stop offset="1" stop-color="#ffe9b8" stop-opacity="0"/></radialGradient>' +
    '</defs>' +
    `<rect width="${VIEW_W}" height="${VIEW_H}" rx="18" fill="url(#sky)"/>` +
    `<ellipse cx="${CX}" cy="${CY}" rx="210" ry="96" fill="url(#glow)">` +
    '<animate attributeName="opacity" values="0.7;1;0.7" dur="4.5s" repeatCount="indefinite"/></ellipse>' +
    `<ellipse cx="${CX}" cy="${CY + 62}" rx="170" ry="14" fill="#e9a07c" opacity="0.25"/>` +
    sparkles() +
    back +
    clapper() +
    front +
    `<text x="${CX}" y="222" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" font-size="26" ` +
    // The words fade in where SMIL runs and simply stand where the picture is drawn still.
    `font-weight="bold" fill="#6b2f1c">${GREETING[words]}` +
    '<animate attributeName="opacity" values="0;0;1" keyTimes="0;0.2;1" dur="1.5s" fill="freeze"/></text>' +
    `<text x="${CX}" y="244" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" font-size="12" ` +
    `fill="#8a4a32">${SUBTITLE[words]}` +
    '<animate attributeName="opacity" values="0;0;1" keyTimes="0;0.5;1" dur="2.2s" fill="freeze"/></text>' +
    '</svg>'
  )
}

/** The splash in each language, made once at load. */
export const SPLASH: Readonly<Record<DirectorLang, string>> = {
  ru: splashSvg('ru'),
  en: splashSvg('en'),
}

/** What the splash says, for a reader that cannot see it. */
export const SPLASH_ALT: Record<DirectorLang, string> = {
  ru: 'Заставка: кадры плёнки кружатся вокруг хлопушки. Привет! Я Эльза',
  en: "Splash: film frames turning round a clapperboard. Hi! I'm Elsa",
}
