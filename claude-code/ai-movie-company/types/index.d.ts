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

/**
 * One piece of work Elsa left running after her turn: a background subagent, a background shell
 * command, a monitor, a workflow. Kept until its end is heard, and dropped once it is older than
 * any sane run (a reload can miss the end).
 */
export type BackgroundTask = {
  /** The engine's id: the subagent's agentId, the shell's or the monitor's taskId. */
  id: string
  kind: 'agent' | 'shell' | 'monitor' | 'workflow' | 'other'
  /** What it does, as the call described it; '' when nothing was said. */
  label: string
  /** When it was first seen (ms, the mod's clock). */
  startedAt: number
}

/**
 * The document the pane beside the chat shows for the person to read and answer (a brief, a
 * screenplay, a plan), with the buttons whose label goes to the chat as the person's message.
 */
export type DocPane = {
  /** The file shown, as the model named it (absolute). */
  path: string
  /** The pane's title, short, in the person's language. */
  title: string
  /** The document's text as read when it was shown. */
  text: string
  /** The language of the pane's own words. */
  lang: DirectorLang
  /** The button labels, the approving one first. */
  buttons: string[]
  /** The label the person pressed, or null while the pane waits for an answer. */
  sent: string | null
}

/**
 * A notice above the prompt, shown only while the engine is idle: a message from the plugin's
 * authors (`vp messages poll`), a newer version downloaded by an update (restart Claude), or the
 * reminder to turn auto-update on.
 */
export type ChannelNotice = {
  /** `message` (from the feed, acknowledged by id), `restart`, or `reminder`. */
  source: 'message' | 'restart' | 'reminder'
  /** The feed's id for a message; 0 otherwise. */
  id: number
  /** `update` or `info` for a message; the source otherwise. */
  kind: string
  /** The text, in the person's language. */
  text: string
  /** The language of the notice's own words (buttons). */
  lang: DirectorLang
}

/**
 * A hand step the person is asked to do outside the chat, framed on its own card (the band above
 * the prompt, or a pane where no band is drawn) and followed by the check «did you understand».
 */
export type ActionCard = {
  /** The one action, e.g. «Закройте программу Claude». */
  title: string
  /** How to do it, pointing at something visible, e.g. «Нажмите кнопку «Copy» справа от ключа». */
  how: string
  /** The look-alike not to touch, or null. */
  notThis: string | null
  /** What the person will see or do afterwards, or null. */
  after: string | null
  /** A restart is remembered across it: the next session start greets the person. */
  kind: 'restart' | 'step'
  /** The language of the card's own words. */
  lang: DirectorLang
  /** The button labels, the understood one first. */
  buttons: string[]
  /** The label the person pressed (or typed), or null while the card waits. */
  sent: string | null
}

/** The welcome-back card after a restart the director asked for. */
export type WelcomeCard = {
  /** The step the person was on when Claude closed. */
  title: string
  /** How that step was to be done (told to the model, not drawn). */
  how: string
  /** What the person was to do after it, or null. */
  after: string | null
  lang: DirectorLang
}

/** A restart card as kept in `$.store` across the restart. */
export type PendingRestart = WelcomeCard & {
  /** When the card was shown (ms, the mod's clock), or null on a host that keeps no clock. */
  at: number | null
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
      /** The work still running after her turn ended (background agents, shells, monitors). */
      background: BackgroundTask[]
      /** The document in the pane beside the chat, or null before the first one was shown. */
      doc: DocPane | null
      /** The notice above the prompt, or null when there is none. */
      notice: ChannelNotice | null
      /** The hand step on the action card, or null when no card is up. */
      action: ActionCard | null
      /** The welcome-back card after a restart, or null. */
      welcome: WelcomeCard | null
    }
  }
}
