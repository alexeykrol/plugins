// The state contract of the ai-movie-company mod: every value the hooks module keeps in `$.state`.

/** The language of the mod's own words in the interface (name, band, notices). */
export type DirectorLang = 'ru' | 'en'

declare module 'claude-code' {
  interface PluginState {
    'ai-movie-company': {
      /** True while the editing director is on stage in this session. */
      onStage: boolean
      /** The language the person writes in, as last seen. */
      lang: DirectorLang
    }
  }
}
