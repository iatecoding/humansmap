import { describe, it, expect, beforeEach, vi } from 'vitest'

// ---------------------------------------------------------------------------
// Lightweight localStorage stub (jsdom provides one but let's be explicit)
// ---------------------------------------------------------------------------
const localStorageStore = {}
const localStorageMock = {
  getItem: (k) => localStorageStore[k] ?? null,
  setItem: (k, v) => { localStorageStore[k] = String(v) },
  removeItem: (k) => { delete localStorageStore[k] },
}

vi.stubGlobal('localStorage', localStorageMock)
vi.stubGlobal('navigator', { language: 'en-US' })

// Import AFTER stubs so detectLocale() runs with mocked globals
import { useLocale, SUPPORTED_LOCALES } from '../composables/useLocale.js'

describe('useLocale', () => {
  beforeEach(() => {
    // Clear persisted locale between tests
    delete localStorageStore['locale']
  })

  it('exports SUPPORTED_LOCALES with all expected codes', () => {
    const codes = SUPPORTED_LOCALES.map((l) => l.code)
    expect(codes).toContain('en')
    expect(codes).toContain('it')
    expect(codes).toContain('fr')
    expect(codes).toContain('de')
    expect(codes).toContain('es')
    expect(codes.length).toBeGreaterThanOrEqual(9)
  })

  it('returns a t() computed with translation keys for the current locale', () => {
    const { t, setLocale } = useLocale()
    setLocale('en')
    expect(t.value.search.placeholder).toBeTruthy()
    expect(typeof t.value.search.placeholder).toBe('string')
  })

  it('setLocale changes the active locale', () => {
    const { locale, setLocale } = useLocale()
    setLocale('it')
    expect(locale.value).toBe('it')
    setLocale('en')
    expect(locale.value).toBe('en')
  })

  it('setLocale ignores unsupported codes', () => {
    const { locale, setLocale } = useLocale()
    setLocale('en')
    setLocale('zz') // not a real locale
    expect(locale.value).toBe('en')
  })

  it('setLocale persists to localStorage', () => {
    const { setLocale } = useLocale()
    setLocale('fr')
    expect(localStorageStore['locale']).toBe('fr')
  })

  describe('prefName()', () => {
    it('returns empty string for null/undefined input', () => {
      const { prefName, setLocale } = useLocale()
      setLocale('en')
      expect(prefName(null)).toBe('')
      expect(prefName(undefined)).toBe('')
    })

    it('returns name_en when locale is not Italian', () => {
      const { prefName, setLocale } = useLocale()
      setLocale('en')
      const item = { name_en: 'Mario Rossi', name_it: 'Mario Rossi IT' }
      expect(prefName(item)).toBe('Mario Rossi')
    })

    it('returns name_it when locale is Italian', () => {
      const { prefName, setLocale } = useLocale()
      setLocale('it')
      const item = { name_en: 'Mario Rossi', name_it: 'Mario Rossi IT' }
      expect(prefName(item)).toBe('Mario Rossi IT')
    })

    it('falls back to name_en when name_it is missing and locale is it', () => {
      const { prefName, setLocale } = useLocale()
      setLocale('it')
      const item = { name_en: 'Mario Rossi' }
      expect(prefName(item)).toBe('Mario Rossi')
    })

    it('falls back to name_it when name_en is missing and locale is en', () => {
      const { prefName, setLocale } = useLocale()
      setLocale('en')
      const item = { name_it: 'Mario Rossi IT' }
      expect(prefName(item)).toBe('Mario Rossi IT')
    })
  })

  describe('dateLocale computed', () => {
    it('returns correct BCP-47 tag for each supported locale', () => {
      const { dateLocale, setLocale } = useLocale()
      setLocale('it')
      expect(dateLocale.value).toBe('it-IT')
      setLocale('de')
      expect(dateLocale.value).toBe('de-DE')
      setLocale('en')
      expect(dateLocale.value).toBe('en-GB')
    })
  })
})
