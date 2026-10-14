/**
 * Read a localized field from an API record.
 *
 * Backend records currently expose English and Italian variants. English UI
 * prefers the English value; every other locale prefers Italian, matching the
 * existing trivia behavior. Missing preferred values fall back to the other
 * available language.
 */
export function localizedField(record, field, locale = 'it', fallback = '') {
  if (!record || typeof record !== 'object') return fallback

  const preferredLanguage = locale === 'en' ? 'en' : 'it'
  const alternateLanguage = preferredLanguage === 'en' ? 'it' : 'en'

  return (
    record[`${field}_${preferredLanguage}`] ||
    record[`${field}_${alternateLanguage}`] ||
    fallback
  )
}

export function localizedName(person, locale = 'it') {
  return localizedField(person, 'name', locale)
}

export function localizedInitial(person, locale = 'it', fallback = '?') {
  const name = localizedName(person, locale).trim()
  const [initial] = Array.from(name)
  return initial ? initial.toUpperCase() : fallback
}
