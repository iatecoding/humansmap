import { describe, expect, it } from 'vitest'
import { getCytoscapeStyle } from '../utils/cytoscape-config.js'

describe('portrait person nodes', () => {
  it('uses a vertical rounded rectangle by default without changing its approximate area', () => {
    const styles = getCytoscapeStyle(() => '')
    const portrait = styles.find((rule) => rule.selector === 'node[entityType="person"]')
    const node = { data: (key) => key === 'size' ? 80 : undefined }

    expect(portrait.style.shape).toBe('round-rectangle')
    expect(portrait.style['corner-radius']).toBe(6)
    expect(portrait.style.width(node)).toBeCloseTo(65.6)
    expect(portrait.style.height(node)).toBeCloseTo(97.6)
  })

  it('keeps organization nodes as rounded rectangles', () => {
    const styles = getCytoscapeStyle(() => '')
    const organization = styles.find((rule) => rule.selector === 'node[entityType="organization"]')

    expect(organization.style.shape).toBe('round-rectangle')
    expect(organization.style['corner-radius']).toBe(3)
  })
})
