import { ref, computed } from 'vue'
import en from '../i18n/en.js'
import it from '../i18n/it.js'
import fr from '../i18n/fr.js'
import de from '../i18n/de.js'
import es from '../i18n/es.js'
import pt from '../i18n/pt.js'
import nl from '../i18n/nl.js'
import pl from '../i18n/pl.js'
import sv from '../i18n/sv.js'

const TRANSLATIONS = { en, it, fr, de, es, pt, nl, pl, sv }

export const SUPPORTED_LOCALES = [
  { code: 'en', label: 'English' },
  { code: 'it', label: 'Italiano' },
  { code: 'fr', label: 'Français' },
  { code: 'de', label: 'Deutsch' },
  { code: 'es', label: 'Español' },
  { code: 'pt', label: 'Português' },
  { code: 'nl', label: 'Nederlands' },
  { code: 'pl', label: 'Polski' },
  { code: 'sv', label: 'Svenska' },
]

const DATE_LOCALES = {
  en: 'en-GB',
  it: 'it-IT',
  fr: 'fr-FR',
  de: 'de-DE',
  es: 'es-ES',
  pt: 'pt-PT',
  nl: 'nl-NL',
  pl: 'pl-PL',
  sv: 'sv-SE',
}

function detectLocale() {
  try {
    const saved = localStorage.getItem('locale')
    if (saved && TRANSLATIONS[saved]) return saved
  } catch {
    // ignore when localStorage is unavailable
  }

  const browser = typeof navigator !== 'undefined' ? navigator.language?.slice(0, 2) : undefined
  return TRANSLATIONS[browser] ? browser : 'en'
}

function deepMerge(base, override) {
  const out = { ...base }
  for (const k of Object.keys(override)) {
    if (override[k] && typeof override[k] === 'object' && !Array.isArray(override[k])
        && base[k] && typeof base[k] === 'object' && !Array.isArray(base[k])) {
      out[k] = deepMerge(base[k], override[k])
    } else {
      out[k] = override[k]
    }
  }
  return out
}

// Module-level singleton — shared across all components
const locale = ref(detectLocale())

export function useLocale() {
  const t = computed(() => {
    const lang = TRANSLATIONS[locale.value]
    if (!lang || locale.value === 'en') return TRANSLATIONS.en
    return deepMerge(TRANSLATIONS.en, lang)
  })

  function setLocale(lang) {
    if (!TRANSLATIONS[lang]) return
    locale.value = lang
    try {
      localStorage.setItem('locale', lang)
    } catch {
      // ignore
    }
  }

  // Prefer the name in the current UI language, fall back en → it
  function prefName(item) {
    if (!item) return ''
    if (locale.value === 'it') return item.name_it || item.name_en || ''
    return item.name_en || item.name_it || ''
  }

  // Prefer the description in the current UI language, fall back en → it
  function prefDesc(item) {
    if (!item) return ''
    if (locale.value === 'it') return item.description_it || item.description_en || item.description || ''
    return item.description_en || item.description_it || item.description || ''
  }

  // BCP-47 tag for date formatting
  const dateLocale = computed(() => DATE_LOCALES[locale.value] || 'en-GB')

  return { locale, t, setLocale, prefName, prefDesc, dateLocale, SUPPORTED_LOCALES }
}
