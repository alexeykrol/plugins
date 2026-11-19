// The state contract of the ai-movie-company mod: every value the hooks module keeps in `$.state`.

/** The language of the mod's own words in the interface (name, band, notices). */
export type DirectorLang = 'ru' | 'en'

/** The running step of the engine, as read from <VP_HOME>/progress-live.json. */
export type LiveProgress = {
  step: string
  percent: number
  etaS: number | null
}

/**
 * Elsa's mood, as her face on the band shows it: at rest (a turn ended well), working (a turn
 * runs), waiting (a question to the person is open) or in trouble (a tool failed).
 */
export type DirectorMood = 'idle' | 'working' | 'waiting' | 'trouble'

/** One entry of the task list the model keeps (TodoWrite, or TaskCreate and TaskUpdate). */
export type WorkTask = {
  id: string
  subject: string
  /** The present-tense form ("Rendering the reel"), when the model gave one. */
  activeForm: string | null
  status: 'pending' | 'in_progress' | 'completed'
}

/** What the model is doing in the conversation, beside the engine's own steps. */
export type TurnWork = {
  /** True from a main-loop turn's start to its end. */
  isRunning: boolean
  /** When the running turn started (ms, the mod's clock); null when none ran yet. */
  startedAt: number | null
  /** What runs now, as the last tool call says it; null before the first call. */
  label: string | null
  /** The model's task list, as it last wrote it. */
  tasks: WorkTask[]
}

declare module 'claude-code' {
  interface PluginState {
    'ai-movie-company': {
      /** True while the editing director is on stage in this session. */
      onStage: boolean
      /** The language the person writes in, as last seen. */
      lang: DirectorLang
      /** The band's hue in degrees; it drifts within the warm range while the director is on stage. */
      tint: number
      /** The engine's running step, or null when nothing runs. */
      progress: LiveProgress | null
      /** Elsa's mood on the band. */
      mood: DirectorMood
      /** The face's clock: one beat per frame swap while the director is on stage. */
      beat: number
      /** The model's own work in the conversation (turns, tool calls, task list). */
      work: TurnWork
    }
  }
}
