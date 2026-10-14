import { describe, expect, it } from 'vitest'
import {
  localizedField,
  localizedInitial,
  localizedName,
} from '../utils/localized-content.js'

describe('localizedField', () => {
  const person = {
    name_en: 'English Name',
    name_it: 'Nome Italiano',
    description_en: 'English description',
    description_it: 'Descrizione italiana',
  }

  it('prefers English for the English locale', () => {
    expect(localizedName(person, 'en')).toBe('English Name')
  })

  it('prefers Italian for other supported locales', () => {
    expect(localizedName(person, 'it')).toBe('Nome Italiano')
    expect(localizedName(person, 'fr')).toBe('Nome Italiano')
  })

  it('falls back to the available language', () => {
    expect(localizedName({ name_it: 'Solo italiano' }, 'en')).toBe('Solo italiano')
    expect(localizedName({ name_en: 'English only' }, 'de')).toBe('English only')
  })

  it('supports fields other than names and explicit fallback values', () => {
    expect(localizedField(person, 'description', 'en')).toBe('English description')
    expect(localizedField(null, 'description', 'en', 'Unavailable')).toBe('Unavailable')
    expect(localizedField({}, 'description', 'en', 'Unavailable')).toBe('Unavailable')
  })
})

describe('localizedInitial', () => {
  it('returns an uppercase initial from the localized name', () => {
    expect(localizedInitial({ name_en: 'ada' }, 'en')).toBe('A')
    expect(localizedInitial({ name_it: ' élodie' }, 'it')).toBe('É')
  })

  it('returns the fallback when a name is unavailable', () => {
    expect(localizedInitial(null, 'en')).toBe('?')
    expect(localizedInitial({ name_en: '' }, 'en', '—')).toBe('—')
  })
})
