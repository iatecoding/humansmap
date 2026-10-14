import { describe, it, expect, beforeEach } from 'vitest'
import { useDocumentHead, HOME_HEAD, nameFromSlug } from '../composables/useDocumentHead.js'

const SHELL_HEAD = `
  <title>Humans Map | Explore connections between public figures</title>
  <meta name="description" content="shell description">
  <link rel="canonical" href="https://humansmap.com/">
  <meta property="og:type" content="website">
  <meta property="og:url" content="https://humansmap.com/">
  <meta property="og:title" content="Humans Map">
  <meta property="og:description" content="shell description">
  <meta name="twitter:title" content="Humans Map">
  <meta name="twitter:description" content="shell description">
`

function meta(attr, key) {
  return document.head.querySelector(`meta[${attr}="${key}"]`)?.getAttribute('content')
}

function canonical() {
  return document.head.querySelector('link[rel="canonical"]')?.getAttribute('href')
}

describe('useDocumentHead', () => {
  beforeEach(() => {
    document.head.innerHTML = SHELL_HEAD
  })

  it('restores the shell defaults for the home route', () => {
    const { applyHome } = useDocumentHead()
    applyHome()

    expect(document.title).toBe(HOME_HEAD.title)
    expect(meta('name', 'description')).toBe(HOME_HEAD.description)
    expect(canonical()).toBe('https://humansmap.com/')
    expect(meta('property', 'og:url')).toBe('https://humansmap.com/')
    expect(meta('property', 'og:title')).toBe('Humans Map')
    expect(meta('property', 'og:description')).toBe(HOME_HEAD.description)
    expect(meta('name', 'twitter:title')).toBe('Humans Map')
    expect(meta('name', 'twitter:description')).toBe(HOME_HEAD.description)
  })

  it('applies person head data with the canonical entity URL', () => {
    const { applyEntity } = useDocumentHead()
    applyEntity({
      entityType: 'person',
      slug: 'Q303-goldie-hawn',
      name: 'Goldie Hawn',
      description: 'American actress',
    })

    expect(document.title).toBe('Goldie Hawn | humansmap.com')
    expect(meta('name', 'description')).toBe('American actress')
    expect(canonical()).toBe('https://humansmap.com/person/Q303-goldie-hawn')
    expect(meta('property', 'og:type')).toBe('profile')
    expect(meta('property', 'og:url')).toBe('https://humansmap.com/person/Q303-goldie-hawn')
    expect(meta('property', 'og:title')).toBe('Goldie Hawn')
    expect(meta('property', 'og:description')).toBe('American actress')
    expect(meta('name', 'twitter:title')).toBe('Goldie Hawn')
    expect(meta('name', 'twitter:description')).toBe('American actress')
  })

  it('applies organization head data with the org title format', () => {
    const { applyEntity } = useDocumentHead()
    applyEntity({ entityType: 'organization', slug: 'Q95-google', name: 'Google' })

    expect(document.title).toBe('Google: ownership, people and organization network | Humans Map')
    expect(canonical()).toBe('https://humansmap.com/organization/Q95-google')
    expect(meta('property', 'og:type')).toBe('website')
    expect(meta('property', 'og:title')).toBe('Google')
    // Falls back to a generated description when the entity has none.
    expect(meta('name', 'description')).toContain('Google')
  })

  it('uses a sensible fallback title before entity data arrives', () => {
    const { applyEntity } = useDocumentHead()
    applyEntity({ entityType: 'person', slug: 'Q303-goldie-hawn' })

    expect(document.title).toBe('Profile | humansmap.com')
    expect(canonical()).toBe('https://humansmap.com/person/Q303-goldie-hawn')
  })

  it('creates missing head tags instead of failing', () => {
    document.head.innerHTML = '<title>Humans Map</title>'
    const { applyEntity } = useDocumentHead()
    applyEntity({ entityType: 'person', slug: 'Q1-jane', name: 'Jane' })

    expect(meta('name', 'description')).toBeTruthy()
    expect(canonical()).toBe('https://humansmap.com/person/Q1-jane')
  })

  it('does not touch <html lang>', () => {
    document.documentElement.setAttribute('lang', 'en')
    const { applyEntity } = useDocumentHead()
    applyEntity({ entityType: 'person', slug: 'Q1-jane', name: 'Jane' })
    expect(document.documentElement.getAttribute('lang')).toBe('en')
  })
})

describe('nameFromSlug', () => {
  it('humanises a QID + slug', () => {
    expect(nameFromSlug('Q303-goldie-hawn')).toBe('Goldie Hawn')
  })

  it('handles occupation virtual nodes', () => {
    expect(nameFromSlug('occ_head_of_state')).toBe('Head Of State')
  })

  it('returns null when the slug has no readable name', () => {
    expect(nameFromSlug('Q303')).toBeNull()
    expect(nameFromSlug(null)).toBeNull()
  })
})
