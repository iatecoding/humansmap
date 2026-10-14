import { describe, it, expect } from 'vitest'
import {
  groupByParty,
  computeWestminster,
  partyColor,
  partyDisplayName,
  partyOrderVal,
  PARTY_COLORS,
  PARTY_DISPLAY_NAMES,
  PARTY_ORDER,
} from '../utils/hemicycle.js'

// ── partyColor ────────────────────────────────────────────────────────────────
describe('partyColor', () => {
  it('returns known color for Labour', () => {
    expect(partyColor('Q9630')).toBe('#E4003B')
  })
  it('returns known color for Conservative', () => {
    expect(partyColor('Q9626')).toBe('#0077CC')
  })
  it('assigns a consistent fallback color for unknown QID', () => {
    const c1 = partyColor('Q999999999')
    const c2 = partyColor('Q999999999')
    expect(c1).toBe(c2)
  })
})

// ── partyDisplayName ──────────────────────────────────────────────────────────
describe('partyDisplayName', () => {
  it('returns abbreviation for known parties', () => {
    expect(partyDisplayName('Q9630', 'Labour Party')).toBe('Labour')
    expect(partyDisplayName('Q9626', 'Conservative Party')).toBe('Cons')
    expect(partyDisplayName('Q9624', 'Liberal Democrats')).toBe('Lib Dems')
    expect(partyDisplayName('Q215519', 'Democratic Unionist Party')).toBe('DUP')
  })
  it('falls back to rawName for unknown QIDs', () => {
    expect(partyDisplayName('Q000001', 'Some Party')).toBe('Some Party')
  })
  it('falls back to Independent for null rawName', () => {
    expect(partyDisplayName('Q000001', null)).toBe('Independent')
  })
})

// ── partyOrderVal ─────────────────────────────────────────────────────────────
describe('partyOrderVal', () => {
  it('Labour is left of Conservative', () => {
    expect(partyOrderVal('Q9630')).toBeLessThan(partyOrderVal('Q9626'))
  })
  it('Scottish Labour is adjacent to Labour', () => {
    expect(partyOrderVal('Q3179541')).toBeGreaterThan(partyOrderVal('Q9630'))
    expect(partyOrderVal('Q3179541')).toBeLessThan(partyOrderVal('Q9626'))
  })
  it('unknown QID returns 50 (middle fallback)', () => {
    expect(partyOrderVal('Q000001')).toBe(50)
  })
})

// ── groupByParty ──────────────────────────────────────────────────────────────
describe('groupByParty', () => {
  const makeMembers = (partyQid, count) =>
    Array.from({ length: count }, (_, i) => ({
      qid: `Q${Math.random().toString().slice(2, 10)}`,
      name: `Person ${i}`,
      image: null,
      party_qid: partyQid,
      party_name: partyQid ? 'Some Party' : null,
    }))

  it('groups members by party', () => {
    const members = [
      ...makeMembers('Q9630', 5),  // Labour
      ...makeMembers('Q9626', 3),  // Conservative
    ]
    const buckets = groupByParty(members)
    expect(buckets.length).toBe(2)
    const labourBucket = buckets.find(b => b.qid === 'Q9630')
    expect(labourBucket.members.length).toBe(5)
  })

  it('assigns Independent bucket for members with no party', () => {
    const members = makeMembers(null, 4)
    const buckets = groupByParty(members)
    const indep = buckets.find(b => b.qid === '__independent__')
    expect(indep).toBeDefined()
    expect(indep.members.length).toBe(4)
    expect(indep.isSynthetic).toBe(true)
  })

  it('merges tiny parties into Other when threshold > their count', () => {
    const members = [
      ...makeMembers('Q9630', 10),  // Labour
      ...makeMembers('Q9626', 10),  // Conservative
      ...makeMembers('Q9669', 1),   // Green — only 1 member
    ]
    const buckets = groupByParty(members, 2)  // threshold=2: 1 < 2 → merge
    const green = buckets.find(b => b.qid === 'Q9669')
    expect(green).toBeUndefined()
    const other = buckets.find(b => b.qid === '__other__')
    expect(other).toBeDefined()
    expect(other.members.length).toBe(1)
    expect(other.isSynthetic).toBe(true)
  })

  it('keeps all parties when threshold=1 (default)', () => {
    const members = [
      ...makeMembers('Q9630', 5),
      ...makeMembers('Q9669', 1),  // only 1 member — kept at threshold=1
    ]
    const buckets = groupByParty(members, 1)
    expect(buckets.find(b => b.qid === 'Q9669')).toBeDefined()
    expect(buckets.find(b => b.qid === '__other__')).toBeUndefined()
  })

  it('sorts buckets by political spectrum order (left→right)', () => {
    const members = [
      ...makeMembers('Q9626', 3),  // Conservative (order=18)
      ...makeMembers('Q9630', 5),  // Labour (order=6)
      ...makeMembers('Q10658', 2), // SNP (order=9)
    ]
    const buckets = groupByParty(members)
    const orders = buckets.map(b => b.order)
    const sorted = [...orders].sort((a, b) => a - b)
    expect(orders).toEqual(sorted)
  })

  it('isSynthetic=false for real parties', () => {
    const members = makeMembers('Q9630', 3)
    const buckets = groupByParty(members)
    expect(buckets[0].isSynthetic).toBe(false)
  })
})

// ── computeWestminster ────────────────────────────────────────────────────────
describe('computeWestminster', () => {
  const makeParty = (qid, order, count) => ({
    qid,
    name: `Party ${qid}`,
    color: '#FF0000',
    order,
    isSynthetic: false,
    members: Array.from({ length: count }, (_, i) => ({
      qid: `Q${i}_${qid}`,
      name: `Person ${i}`,
      image: null,
    })),
  })

  it('returns seats for government and opposition', () => {
    const parties = [
      makeParty('Q9630', 6, 10),   // Labour → gov
      makeParty('Q9626', 18, 8),   // Conservative → opp
    ]
    const result = computeWestminster(parties, { govOrderMax: 7 })
    expect(result.seats.length).toBe(18)
    const govSeats = result.seats.filter(s => s.side === 'gov')
    const oppSeats = result.seats.filter(s => s.side === 'opp')
    expect(govSeats.length).toBe(10)
    expect(oppSeats.length).toBe(8)
  })

  it('government seats have x < aisleX', () => {
    const parties = [
      makeParty('Q9630', 6, 5),
    ]
    const result = computeWestminster(parties, { aisleX: 450 })
    result.seats.forEach(s => {
      expect(s.x).toBeLessThan(450)
    })
  })

  it('opposition seats have x > aisleX', () => {
    const parties = [
      makeParty('Q9630', 6, 6),    // Labour → gov
      makeParty('Q9626', 18, 5),
    ]
    const result = computeWestminster(parties, { aisleX: 450 })
    result.seats.filter(s => s.side === 'opp').forEach(s => {
      expect(s.x).toBeGreaterThan(450)
    })
  })

  it('returns correct govCols and oppCols', () => {
    const rows = 5
    const parties = [
      makeParty('Q9630', 6, 12),  // gov: 12/5 = ceil → 3 cols
      makeParty('Q9626', 18, 7),  // opp: 7/5 = ceil → 2 cols
    ]
    const result = computeWestminster(parties, { rows, govOrderMax: 7 })
    expect(result.govCols).toBe(3)
    expect(result.oppCols).toBe(2)
  })

  it('each seat has a member reference', () => {
    const parties = [makeParty('Q9630', 6, 3)]
    const result = computeWestminster(parties)
    result.seats.forEach(s => {
      expect(s.member).toBeDefined()
      expect(s.party).toBeDefined()
    })
  })

  it('handles empty parties gracefully', () => {
    const result = computeWestminster([])
    expect(result.seats).toEqual([])
    expect(result.govCols).toBe(1)
    expect(result.oppCols).toBe(1)
  })
})
