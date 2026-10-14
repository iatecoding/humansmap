/**
 * Slug helper — converts a person's display name to a URL-safe ASCII slug.
 *
 * Contract (mirrors backend/app/utils/slug.py exactly):
 *   - Lowercase
 *   - Accent-fold via NFKD normalisation + strip combining marks (U+0300–U+036F)
 *   - Replace any run of non-[a-z0-9] chars with a single hyphen
 *   - Trim leading/trailing hyphens
 *   - Cap at 60 characters (trim at last hyphen boundary if possible)
 *   - Empty / all-non-ASCII input returns ""
 *
 * @example
 * slugify("Cañón")          // → "canon"
 * slugify("J.S. Bach")      // → "j-s-bach"
 * slugify("hello 😀 world") // → "hello-world"
 * slugify("")               // → ""
 * slugify("日本語")          // → ""
 */
export function slugify(name) {
  if (!name) return ''

  // 1. NFKD normalisation then drop combining (accent) marks
  const ascii = name.normalize('NFKD').replace(/[\u0300-\u036f]/g, '')

  // 2. Lowercase
  const lower = ascii.toLowerCase()

  // 3. Replace runs of non-alphanumeric chars with a single hyphen
  let slugged = lower.replace(/[^a-z0-9]+/g, '-')

  // 4. Trim leading/trailing hyphens
  slugged = slugged.replace(/^-+|-+$/g, '')

  // 5. Cap at 60 chars; trim at last hyphen boundary when possible
  if (slugged.length > 60) {
    let truncated = slugged.slice(0, 60)
    const lastHyphen = truncated.lastIndexOf('-')
    if (lastHyphen > 0) {
      truncated = truncated.slice(0, lastHyphen)
    }
    slugged = truncated.replace(/^-+|-+$/g, '')
  }

  return slugged
}
