import { useEffect, useState } from "react"
import { Monitor, Moon, Sun } from "lucide-react"

import { fill, useI18n } from "../i18n"
import { nextPreference, parsePreference, resolveMode, THEME_STORAGE_KEY, type ThemePreference } from "../theme"

const SYSTEM_DARK = "(prefers-color-scheme: dark)"

/** Top-bar button that cycles system / light / dark and keeps <html data-mode> in step. */
export function ThemeToggle() {
  const { t } = useI18n()
  const [preference, setPreference] = useState<ThemePreference>(() => parsePreference(localStorage.getItem(THEME_STORAGE_KEY)))

  useEffect(() => {
    const query = window.matchMedia(SYSTEM_DARK)
    const apply = () => { document.documentElement.dataset.mode = resolveMode(preference, query.matches) }
    apply()
    if (preference !== "system") return
    query.addEventListener("change", apply)
    return () => query.removeEventListener("change", apply)
  }, [preference])

  const choose = (next: ThemePreference) => {
    if (next === "system") localStorage.removeItem(THEME_STORAGE_KEY)
    else localStorage.setItem(THEME_STORAGE_KEY, next)
    setPreference(next)
  }

  const labels = { system: t.themeSystem, light: t.themeLight, dark: t.themeDark }
  const Icon = preference === "light" ? Sun : preference === "dark" ? Moon : Monitor
  const title = fill(t.themeHint, { current: labels[preference], next: labels[nextPreference(preference)] })
  return <button type="button" className="icon-button" aria-label={title} title={title} onClick={() => choose(nextPreference(preference))}>
    <Icon size={15} />
  </button>
}
