import { describe, it, expect } from 'vitest'
import {
  computeGenerations,
  computeFamilyUnits,
  getRelationshipToRoot,
  getRootParents,
  computeFamilyColors,
  buildParentsMap,
  FAMILY_EDGE_TYPES,
} from '../utils/family-graph.js'

// Helpers ------------------------------------------------------------------

const v = (key) => `persons/${key}`
const edge = (from, to, type) => ({ _from: v(from), _to: v(to), type })

// Fixed palettes used across tests — small, obvious colors so assertions read
// well. The real app uses the palette from cytoscape-config.js; these stand-in
// values let us verify the mapping logic without coupling to visual choices.
const BORDER = { parent: '#P', sibling: '#S', relative: '#R' }
const UNITS = ['#U1', '#U2', '#U3', '#U4']
const CHILD_UNKNOWN = '#CU'

// --------------------------------------------------------------------------

describe('FAMILY_EDGE_TYPES', () => {
  it('covers the core family predicates used by the ETL', () => {
    for (const t of ['parent', 'father', 'mother', 'child', 'sibling', 'spouse', 'relative']) {
      expect(FAMILY_EDGE_TYPES.has(t)).toBe(true)
    }
  })
})

describe('computeGenerations', () => {
  it('places root at gen 0 with no edges', () => {
    const gen = computeGenerations('R', [])
    expect(gen).toEqual({ R: 0 })
  })

  it('walks parent edges upward', () => {
    // R says "my father is F" → raw edge R→F type=father
    const gen = computeGenerations('R', [edge('R', 'F', 'father')])
    expect(gen).toEqual({ R: 0, F: -1 })
  })

  it('walks child edges downward', () => {
    const gen = computeGenerations('R', [edge('R', 'C', 'child')])
    expect(gen).toEqual({ R: 0, C: 1 })
  })

  it('keeps spouses and siblings on the same row', () => {
    const gen = computeGenerations('R', [
      edge('R', 'Sp', 'spouse'),
      edge('R', 'Sb', 'sibling'),
    ])
    expect(gen.Sp).toBe(0)
    expect(gen.Sb).toBe(0)
  })

  it('propagates transitively across multiple hops', () => {
    const gen = computeGenerations('R', [
      edge('R', 'F', 'father'),     // F is root's father
      edge('F', 'GF', 'father'),    // GF is F's father
      edge('R', 'C', 'child'),      // C is root's child
      edge('C', 'GC', 'child'),     // GC is C's child
    ])
    expect(gen.F).toBe(-1)
    expect(gen.GF).toBe(-2)
    expect(gen.C).toBe(1)
    expect(gen.GC).toBe(2)
  })
})

describe('getRelationshipToRoot', () => {
  it('detects a parent via a father edge regardless of direction', () => {
    const edges = [edge('R', 'F', 'father')]
    expect(getRelationshipToRoot('F', 'R', edges)).toBe('parent')
  })

  it('returns "child" when root claims the node as its child', () => {
    const edges = [edge('R', 'C', 'child')]
    expect(getRelationshipToRoot('C', 'R', edges)).toBe('child')
  })

  it('returns "child" when the node claims root as its parent', () => {
    const edges = [edge('C', 'R', 'father')]
    expect(getRelationshipToRoot('C', 'R', edges)).toBe('child')
  })

  it('distinguishes spouse, sibling, relative', () => {
    const edges = [
      edge('R', 'Sp', 'spouse'),
      edge('R', 'Sb', 'sibling'),
      edge('R', 'X', 'relative'),
    ]
    expect(getRelationshipToRoot('Sp', 'R', edges)).toBe('spouse')
    expect(getRelationshipToRoot('Sb', 'R', edges)).toBe('sibling')
    expect(getRelationshipToRoot('X', 'R', edges)).toBe('relative')
  })

  it('returns null when there is no direct edge between root and the node', () => {
    const edges = [edge('A', 'B', 'sibling')]
    expect(getRelationshipToRoot('A', 'R', edges)).toBeNull()
  })
})

describe('getRootParents', () => {
  it('collects direct parents from either edge direction', () => {
    const edges = [
      edge('R', 'Fa', 'father'),   // root says "my father is Fa"
      edge('Mo', 'R', 'child'),    // Mo says "my child is R"
      edge('R', 'Sb', 'sibling'),  // irrelevant
    ]
    const parents = getRootParents('R', edges)
    expect(parents.has('Fa')).toBe(true)
    expect(parents.has('Mo')).toBe(true)
    expect(parents.has('Sb')).toBe(false)
  })
})

describe('buildParentsMap', () => {
  it('captures child→parents from both raw edge directions', () => {
    const edges = [
      edge('A', 'P1', 'father'),   // A's father is P1
      edge('P2', 'A', 'child'),    // P2's child is A  (A's other parent)
      edge('B', 'P1', 'mother'),   // B shares parent P1 with A
      edge('A', 'B', 'sibling'),   // should not affect the map
    ]
    const map = buildParentsMap(edges)
    expect([...(map.get('A') || [])].sort()).toEqual(['P1', 'P2'])
    expect([...(map.get('B') || [])]).toEqual(['P1'])
    expect(map.has('P1')).toBe(false)  // P1 is never a child in this graph
  })
})

describe('computeFamilyUnits', () => {
  it('preserves first-seen spouse order', () => {
    const edges = [
      edge('R', 'S1', 'spouse'),
      edge('R', 'S2', 'spouse'),
      edge('S1', 'R', 'spouse'),  // duplicate reversed — should not re-add
    ]
    const { spouseOrder } = computeFamilyUnits('R', edges)
    expect(spouseOrder).toEqual(['S1', 'S2'])
  })

  it('links each child to the spouse who is also their parent', () => {
    const edges = [
      edge('R', 'S1', 'spouse'),
      edge('R', 'S2', 'spouse'),
      edge('R', 'C1', 'child'),
      edge('R', 'C2', 'child'),
      edge('R', 'C3', 'child'),
      // C1's other parent is S1
      edge('S1', 'C1', 'child'),
      // C2's other parent is S2
      edge('C2', 'S2', 'father'),
      // C3 has no other parent in the graph
    ]
    const { childToSpouse, rootChildren } = computeFamilyUnits('R', edges)
    expect(childToSpouse).toEqual({ C1: 'S1', C2: 'S2' })
    expect(rootChildren.has('C3')).toBe(true)
    expect(childToSpouse.C3).toBeUndefined()
  })
})

describe('computeFamilyColors', () => {
  const base = (rawEdges) => {
    const generations = computeGenerations('R', rawEdges)
    const familyUnits = computeFamilyUnits('R', rawEdges)
    return computeFamilyColors({
      rootQid: 'R',
      rawEdges,
      generations,
      familyUnits,
      unitColors: UNITS,
      borderColors: BORDER,
      childUnknownColor: CHILD_UNKNOWN,
    })
  }

  it('assigns fixed colors to parent/sibling/relative', () => {
    const { nodeColors, legend } = base([
      edge('R', 'F', 'father'),
      edge('R', 'Sb', 'sibling'),
      edge('R', 'Re', 'relative'),
    ])
    expect(nodeColors.F).toBe(BORDER.parent)
    expect(nodeColors.Sb).toBe(BORDER.sibling)
    expect(nodeColors.Re).toBe(BORDER.relative)
    expect(legend).toMatchObject({
      parent: BORDER.parent,
      sibling: BORDER.sibling,
      relative: BORDER.relative,
    })
  })

  it('colors each spouse-unit distinctly and shares the color with their children', () => {
    const edges = [
      edge('R', 'S1', 'spouse'),
      edge('R', 'S2', 'spouse'),
      edge('R', 'C1', 'child'),
      edge('S1', 'C1', 'child'),
      edge('R', 'C2', 'child'),
      edge('S2', 'C2', 'child'),
    ]
    const { nodeColors, legend } = base(edges)
    expect(nodeColors.S1).toBe(UNITS[0])
    expect(nodeColors.C1).toBe(UNITS[0])
    expect(nodeColors.S2).toBe(UNITS[1])
    expect(nodeColors.C2).toBe(UNITS[1])
    expect(legend.spouseUnits).toEqual([
      { qid: 'S1', color: UNITS[0] },
      { qid: 'S2', color: UNITS[1] },
    ])
    expect(legend.childUnknown).toBeUndefined()
  })

  it('falls back to the child-unknown color and emits a legend entry when spouse is missing', () => {
    const { nodeColors, legend } = base([
      edge('R', 'C', 'child'),  // child with no known other-parent
    ])
    expect(nodeColors.C).toBe(CHILD_UNKNOWN)
    expect(legend.childUnknown).toBe(CHILD_UNKNOWN)
    // Without any spouse in the graph, spouseUnits must not appear — otherwise
    // the legend rendered an empty row with no label.
    expect(legend.spouseUnits).toBeUndefined()
  })

  it('emits both spouse-unit and child-unknown entries when children are mixed', () => {
    const { nodeColors, legend } = base([
      edge('R', 'S1', 'spouse'),
      edge('R', 'C1', 'child'),
      edge('S1', 'C1', 'child'),
      edge('R', 'C2', 'child'),  // other-parent missing from graph
    ])
    expect(nodeColors.C1).toBe(UNITS[0])
    expect(nodeColors.C2).toBe(CHILD_UNKNOWN)
    expect(legend.childUnknown).toBe(CHILD_UNKNOWN)
    expect(legend.spouseUnits).toEqual([{ qid: 'S1', color: UNITS[0] }])
  })

  it('never assigns sibling color to a root child', () => {
    // Regression: the 2nd spouse-unit color used to collide with the sibling
    // color. This test guards against a child sharing the sibling color even
    // when the palette gets reshuffled.
    const edges = [
      edge('R', 'S1', 'spouse'),
      edge('R', 'S2', 'spouse'),
      edge('R', 'Sb', 'sibling'),
      edge('R', 'C2', 'child'),
      edge('S2', 'C2', 'child'),
    ]
    const { nodeColors } = base(edges)
    expect(nodeColors.Sb).toBe(BORDER.sibling)
    // C2 must be colored by its spouse unit, not the sibling color.
    expect(nodeColors.C2).not.toBe(BORDER.sibling)
    expect(nodeColors.C2).toBe(UNITS[1])
  })

  it('applies sibling color only when node shares a parent with root', () => {
    // Reachable at gen 0 via root's spouse's relative, but not an actual
    // sibling — no shared parent. Should NOT be painted sibling-blue.
    const edges = [
      edge('R', 'Sp', 'spouse'),
      edge('Sp', 'X', 'relative'),  // X is same generation but not a sibling of R
    ]
    const generations = computeGenerations('R', edges)
    const familyUnits = computeFamilyUnits('R', edges)
    const { nodeColors } = computeFamilyColors({
      rootQid: 'R',
      rawEdges: edges,
      generations,
      familyUnits,
      unitColors: UNITS,
      borderColors: BORDER,
      childUnknownColor: CHILD_UNKNOWN,
    })
    expect(nodeColors.X).toBeUndefined()
  })

  it('still applies the sibling color when a shared parent is present without a direct sibling edge', () => {
    // A and R are both children of P — share a parent, no direct sibling edge.
    // Should be treated as siblings thanks to the shared-parent check.
    const edges = [
      edge('R', 'P', 'parent'),
      edge('A', 'P', 'parent'),
    ]
    const generations = computeGenerations('R', edges)
    const familyUnits = computeFamilyUnits('R', edges)
    const { nodeColors } = computeFamilyColors({
      rootQid: 'R',
      rawEdges: edges,
      generations,
      familyUnits,
      unitColors: UNITS,
      borderColors: BORDER,
      childUnknownColor: CHILD_UNKNOWN,
    })
    expect(nodeColors.A).toBe(BORDER.sibling)
  })

  it('does not emit spouse-unit entries for spouses with no children when only children drove detection', () => {
    // Only spouse-2 has a child, spouse-1 doesn't. Legend should surface both
    // when any spouse edge exists (user asked about spouses too), but must not
    // emit a spouseUnit entry for a spouse that is not in the graph.
    const { legend } = base([
      edge('R', 'S1', 'spouse'),
      edge('R', 'C', 'child'),
      edge('S2', 'C', 'child'),   // S2 not listed as spouse of R — only parent
    ])
    // S2 is not a spouse here, so childToSpouse won't match it; C is unknown-parent.
    expect(legend.childUnknown).toBe(CHILD_UNKNOWN)
    // S1 is present as a spouse → legend shows the spouse unit
    expect(legend.spouseUnits).toEqual([{ qid: 'S1', color: UNITS[0] }])
  })
})
