// Light/dark choice. "system" follows prefers-color-scheme; the resolved mode is written to
// <html data-mode>, which styles.css keys the dark tokens on. index.html repeats resolveMode
// in an inline script so the first paint is already in the right mode.
export type ThemePreference = "system" | "light" | "dark"
export type ThemeMode = "light" | "dark"

export const THEME_STORAGE_KEY = "apim-backend-pool:theme"
export const THEME_PREFERENCES: readonly ThemePreference[] = ["system", "light", "dark"]

export function parsePreference(stored: string | null): ThemePreference {
  return stored === "light" || stored === "dark" ? stored : "system"
}

export function resolveMode(preference: ThemePreference, systemDark: boolean): ThemeMode {
  return preference === "system" ? (systemDark ? "dark" : "light") : preference
}

/** The toggle cycles system → light → dark → system. */
export function nextPreference(preference: ThemePreference): ThemePreference {
  return THEME_PREFERENCES[(THEME_PREFERENCES.indexOf(preference) + 1) % THEME_PREFERENCES.length]
}
