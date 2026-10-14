/**
 * Org logo quality filter.
 *
 * Wikidata/Commons "logos" are frequently white wordmarks on a black JPEG,
 * or extreme banner crops (e.g. 300×38). Rendered into a square node they
 * become black slabs that read as broken. This module samples a logo once,
 * caches the verdict, and lets the graph fall back to the warm-grey tile.
 *
 * All thresholds are deliberately conservative: when in doubt we keep the
 * image (fail open) and only hide logos that are provably unreadable.
 */

const STORAGE_KEY = 'hm_logo_verdicts_v1'

/** url -> boolean, populated lazily and mirrored to sessionStorage. */
let _mem = null

function _cache() {
  if (_mem) return _mem
  _mem = new Map()
  try {
    if (typeof window === 'undefined') return _mem
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (raw) {
      for (const [url, ok] of Object.entries(JSON.parse(raw))) _mem.set(url, ok)
    }
  } catch {
    // Private mode / quota / SSR — memory cache still works for this session.
  }
  return _mem
}

/** @returns {boolean|undefined} undefined when the logo has not been probed. */
export function getLogoVerdict(url) {
  if (!url) return undefined
  return _cache().get(url)
}

export function setLogoVerdict(url, ok) {
  if (!url) return
  _cache().set(url, ok)
  try {
    window.sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(Object.fromEntries(_cache()))
    )
  } catch {
    // Ignore persistence failures.
  }
}

/** Test helper — clears both the in-memory and persisted cache. */
export function resetLogoVerdicts() {
  _mem = new Map()
  try {
    window.sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Ignore.
  }
}

/**
 * Decide whether a logo survives being cropped into a square node.
 *
 * @param {{avgLuminance:number, opaqueFraction:number, aspect:number}} m
 *   avgLuminance: mean sRGB luminance (0-255) of opaque pixels.
 *   opaqueFraction: share of pixels with alpha >= 0.5 (1.0 for JPEGs).
 *   aspect: naturalWidth / naturalHeight.
 * @returns {boolean} true if the image is usable as a node background.
 */
export function evaluateLogoMetrics({ avgLuminance, opaqueFraction, aspect }) {
  // Effectively blank / fully transparent.
  if (!(opaqueFraction >= 0.06)) return false
  // White wordmark on a filled black rectangle: the classic "black slab".
  if (opaqueFraction >= 0.8 && avgLuminance < 75) return false
  // Extreme banner shapes crop into an unreadable fragment under `cover`.
  if (aspect > 4.5 || aspect < 0.22) return false
  return true
}

/**
 * Load an image and sample it on a small canvas. Never rejects: any failure
 * (missing canvas support, network, CORS) resolves to `true` so a good logo is
 * never hidden because of an error in the checker.
 *
 * @returns {Promise<boolean>}
 */
export async function probeLogo(url) {
  const cached = getLogoVerdict(url)
  if (cached !== undefined) return cached
  if (!url || typeof document === 'undefined' || typeof Image === 'undefined') {
    return true
  }

  try {
    const img = await new Promise((resolve, reject) => {
      const el = new Image()
      el.onload = () => resolve(el)
      el.onerror = () => reject(new Error('image load failed'))
      el.src = url
    })

    const width = 32
    const height = Math.max(
      4,
      Math.min(64, Math.round(width * (img.naturalHeight / (img.naturalWidth || 1))))
    )
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext('2d')
    if (!ctx) return true

    ctx.drawImage(img, 0, 0, width, height)
    const { data } = ctx.getImageData(0, 0, width, height)

    let opaque = 0
    let luminanceSum = 0
    const total = data.length / 4
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] / 255 < 0.5) continue
      opaque += 1
      luminanceSum +=
        0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2]
    }

    const ok = evaluateLogoMetrics({
      avgLuminance: opaque ? luminanceSum / opaque : 0,
      opaqueFraction: opaque / total,
      aspect: (img.naturalWidth || 1) / (img.naturalHeight || 1),
    })
    setLogoVerdict(url, ok)
    return ok
  } catch {
    return true
  }
}
