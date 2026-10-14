// Client-side document head manager.
//
// The server renders the correct <title>/meta for the first load (see
// backend/app/routers/person_page.py and organization_page.py), but a Vue
// Router SPA navigation never touches the document head. This small,
// dependency-free composable updates just the tags the shell already defines
// in frontend/index.html — no new tags, no JSON-LD rewriting.
//
// Callers pass already-loaded entity data (graph node / dossier) so no extra
// API request is needed.

const SITE_ORIGIN = 'https://humansmap.com'

// Mirrors the shell defaults in frontend/index.html. Kept as literals so a
// client-side navigation back home can restore them even when the document was
// server-rendered with entity-specific tags.
export const HOME_HEAD = {
  title: 'Humans Map | Explore connections between public figures',
  description: 'Explore how public figures are connected — shared companies, board seats, business ties, and relationships across Europe, the US, and Canada.',
  canonical: `${SITE_ORIGIN}/`,
  ogType: 'website',
  ogTitle: 'Humans Map',
  twitterTitle: 'Humans Map',
}

const PERSON_TITLE_SUFFIX = ' | humansmap.com'
const ORG_TITLE_SUFFIX = ': ownership, people and organization network | Humans Map'

function normalizeEntityType(entityType) {
  return entityType === 'organization' ? 'organization' : 'person'
}

function setMeta(attr, key, content) {
  if (content == null || content === '') return
  let el = document.head.querySelector(`meta[${attr}="${key}"]`)
  if (!el) {
    el = document.createElement('meta')
    el.setAttribute(attr, key)
    document.head.appendChild(el)
  }
  el.setAttribute('content', content)
}

function setCanonical(href) {
  if (!href) return
  let el = document.head.querySelector('link[rel="canonical"]')
  if (!el) {
    el = document.createElement('link')
    el.setAttribute('rel', 'canonical')
    document.head.appendChild(el)
  }
  el.setAttribute('href', href)
}

/**
 * Apply a fully-resolved head descriptor. Any field left undefined keeps the
 * existing tag untouched, so callers only need to pass what they know.
 */
export function applyHead({
  title,
  description,
  canonical,
  ogType,
  ogUrl,
  ogTitle,
  ogDescription,
  twitterTitle,
  twitterDescription,
} = {}) {
  if (title) document.title = title
  setMeta('name', 'description', description)
  setCanonical(canonical)
  setMeta('property', 'og:type', ogType)
  setMeta('property', 'og:url', ogUrl ?? canonical)
  setMeta('property', 'og:title', ogTitle)
  setMeta('property', 'og:description', ogDescription ?? description)
  setMeta('name', 'twitter:title', twitterTitle)
  setMeta('name', 'twitter:description', twitterDescription ?? description)
}

/** Restore the shell defaults (home route). */
export function applyHome() {
  applyHead(HOME_HEAD)
}

/**
 * Apply entity head data for /person/:slug or /organization/:slug.
 * `slug` is the full route slug (e.g. "Q303-goldie-hawn").
 */
export function applyEntity({ entityType = 'person', slug, name, description } = {}) {
  const type = normalizeEntityType(entityType)
  const isOrg = type === 'organization'
  const displayName = name || (isOrg ? 'Organization' : 'Profile')
  const desc = description || (isOrg
    ? `Explore ${displayName}'s people and organization network on Humans Map.`
    : `Explore ${displayName}'s connections on Humans Map.`)
  const path = `${type}/${slug || ''}`
  const canonical = `${SITE_ORIGIN}/${path}`

  applyHead({
    title: isOrg
      ? `${displayName}${ORG_TITLE_SUFFIX}`
      : `${displayName}${PERSON_TITLE_SUFFIX}`,
    description: desc,
    canonical,
    ogType: isOrg ? 'website' : 'profile',
    ogUrl: canonical,
    ogTitle: displayName,
    ogDescription: desc,
    twitterTitle: displayName,
    twitterDescription: desc,
  })
}

/**
 * Parse a best-effort display name out of a route slug, used as a fallback
 * before entity data has loaded. Handles "Q123-jane-doe" and "occ_actor".
 */
export function nameFromSlug(slug) {
  if (!slug) return null
  const raw = String(slug)
  if (raw.startsWith('occ_')) {
    return raw.slice(4).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
  }
  const match = raw.match(/^Q\d+-(.+)$/)
  if (match) {
    return match[1].replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
  }
  return null
}

export function useDocumentHead() {
  return { applyHead, applyHome, applyEntity, nameFromSlug, HOME_HEAD }
}
