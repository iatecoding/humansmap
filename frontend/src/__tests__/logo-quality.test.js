import { describe, it, expect, beforeEach } from 'vitest'
import {
  evaluateLogoMetrics,
  getLogoVerdict,
  setLogoVerdict,
  resetLogoVerdicts,
} from '../utils/logo-quality.js'

describe('evaluateLogoMetrics', () => {
  it('rejects fully transparent / blank images', () => {
    expect(evaluateLogoMetrics({ avgLuminance: 200, opaqueFraction: 0, aspect: 1 })).toBe(false)
    expect(evaluateLogoMetrics({ avgLuminance: 200, opaqueFraction: 0.02, aspect: 1 })).toBe(false)
  })

  it('rejects white wordmarks on a filled black background (the classic slab)', () => {
    expect(evaluateLogoMetrics({ avgLuminance: 25, opaqueFraction: 1, aspect: 1.6 })).toBe(false)
    expect(evaluateLogoMetrics({ avgLuminance: 60, opaqueFraction: 0.92, aspect: 2 })).toBe(false)
  })

  it('keeps normal logos on a light background', () => {
    expect(evaluateLogoMetrics({ avgLuminance: 235, opaqueFraction: 1, aspect: 1.5 })).toBe(true)
  })

  it('keeps dark glyphs on a transparent background (rendered over the tile)', () => {
    expect(evaluateLogoMetrics({ avgLuminance: 30, opaqueFraction: 0.3, aspect: 1.2 })).toBe(true)
  })

  it('rejects extreme banner crops', () => {
    expect(evaluateLogoMetrics({ avgLuminance: 220, opaqueFraction: 0.9, aspect: 8 })).toBe(false)
    expect(evaluateLogoMetrics({ avgLuminance: 220, opaqueFraction: 0.9, aspect: 0.1 })).toBe(false)
  })

  it('keeps borderline square-ish logos', () => {
    expect(evaluateLogoMetrics({ avgLuminance: 140, opaqueFraction: 0.7, aspect: 3.5 })).toBe(true)
  })
})

describe('logo verdict cache', () => {
  beforeEach(() => resetLogoVerdicts())

  it('starts empty and round-trips a verdict', () => {
    expect(getLogoVerdict('/api/images/Q1.jpg')).toBeUndefined()
    setLogoVerdict('/api/images/Q1.jpg', false)
    expect(getLogoVerdict('/api/images/Q1.jpg')).toBe(false)
    setLogoVerdict('/api/images/Q1.jpg', true)
    expect(getLogoVerdict('/api/images/Q1.jpg')).toBe(true)
  })

  it('ignores empty urls', () => {
    setLogoVerdict('', false)
    expect(getLogoVerdict('')).toBeUndefined()
  })
})
