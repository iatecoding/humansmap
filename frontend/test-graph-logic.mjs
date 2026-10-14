/**
 * Standalone tests for useGraph.js logic — edge filtering, BFS, root protection.
 * Run: node test-graph-logic.mjs
 */

// ── Helpers extracted from useGraph.js ──

const INVERSE_EDGE_MAP = { child: 'parent', employee: 'employer', participant_in: 'participant', victim: 'killed_by' }
const SWAP_DIRECTION_TYPES = new Set(['parent', 'father', 'mother', 'employer'])
const FAMILY_EDGE_TYPES = new Set(['parent', 'father', 'mother', 'child', 'sibling', 'spouse', 'relative'])
const SPECIFIC_FAMILY_TYPES = new Set(['parent', 'sibling', 'spouse'])

function _canonicalEdgeType(type) { return INVERSE_EDGE_MAP[type] ?? type }

function computeGenerations(rootQid, rawEdges) {
  const gen = { [rootQid]: 0 }
  const queue = [rootQid]
  while (queue.length > 0) {
    const current = queue.shift()
    for (const edge of rawEdges) {
      if (!FAMILY_EDGE_TYPES.has(edge.type)) continue
      const fromKey = edge._from?.split('/')[1]
      const toKey = edge._to?.split('/')[1]
      if (!fromKey || !toKey) continue
      const t = edge.type
      if (t === 'parent' || t === 'father' || t === 'mother') {
        if (fromKey === current && !(toKey in gen)) { gen[toKey] = gen[current] - 1; queue.push(toKey) }
        if (toKey === current && !(fromKey in gen)) { gen[fromKey] = gen[current] + 1; queue.push(fromKey) }
      } else if (t === 'child') {
        if (fromKey === current && !(toKey in gen)) { gen[toKey] = gen[current] + 1; queue.push(toKey) }
        if (toKey === current && !(fromKey in gen)) { gen[fromKey] = gen[current] - 1; queue.push(fromKey) }
      } else if (t === 'sibling' || t === 'spouse') {
        if (fromKey === current && !(toKey in gen)) { gen[toKey] = gen[current]; queue.push(toKey) }
        if (toKey === current && !(fromKey in gen)) { gen[fromKey] = gen[current]; queue.push(fromKey) }
      }
    }
  }
  return gen
}

// Simulate the edge element building pipeline (canonical dedup + relative suppression + merge)
function buildEdgeElements(rawEdges, focalQid) {
  const seenEdges = new Set()
  const edgeEls = rawEdges
    .flatMap((e) => {
      const fromKey = e._from?.split('/')[1]
      const toKey = e._to?.split('/')[1]
      if (!fromKey || !toKey) return []
      const canonicalType = _canonicalEdgeType(e.type)
      const canonical = [...[fromKey, toKey].sort(), canonicalType].join('|')
      if (seenEdges.has(canonical)) return []
      seenEdges.add(canonical)
      const swap = SWAP_DIRECTION_TYPES.has(e.type)
      return [{
        data: {
          id: canonical,
          source: swap ? toKey : fromKey,
          target: swap ? fromKey : toKey,
          label: canonicalType,
          type: canonicalType,
        },
      }]
    })

  // Suppress redundant "relative" edges
  const pairTypes = new Map()
  for (const el of edgeEls) {
    const pk = [el.data.source, el.data.target].sort().join('|')
    if (!pairTypes.has(pk)) pairTypes.set(pk, new Set())
    pairTypes.get(pk).add(el.data.type)
  }
  const filteredEdgeEls = edgeEls.filter((el) => {
    if (el.data.type !== 'relative') return true
    const pk = [el.data.source, el.data.target].sort().join('|')
    const types = pairTypes.get(pk)
    for (const t of types) {
      if (SPECIFIC_FAMILY_TYPES.has(t)) return false
    }
    return true
  })

  // Merge parallel edges
  const mergedEdgeMap = new Map()
  const mergedEdges = []
  for (const el of filteredEdgeEls) {
    const pairKey = `${el.data.source}|${el.data.target}`
    if (!mergedEdgeMap.has(pairKey)) {
      mergedEdgeMap.set(pairKey, el)
      mergedEdges.push(el)
    } else {
      const existing = mergedEdgeMap.get(pairKey)
      if (el.data.label && !existing.data.label.includes(el.data.label)) {
        existing.data.label = `${existing.data.label} · ${el.data.label}`
      }
    }
  }
  return mergedEdges
}

// Simulate org member capping to check root protection
function simulateOrgCap(vertices, edges, focalQid, orgMemberCap) {
  const removedNodeKeys = new Set()
  const orgConnections = new Map()
  for (const e of edges) {
    const fromKey = e._from?.split('/')[1]
    const toKey = e._to?.split('/')[1]
    if (!fromKey || !toKey) continue
    const fromIsOrg = vertices.find(v => v._key === fromKey)?.entityType === 'organization'
    const toIsOrg = vertices.find(v => v._key === toKey)?.entityType === 'organization'
    if (fromIsOrg) {
      if (!orgConnections.has(fromKey)) orgConnections.set(fromKey, new Set())
      orgConnections.get(fromKey).add(toKey)
    }
    if (toIsOrg) {
      if (!orgConnections.has(toKey)) orgConnections.set(toKey, new Set())
      orgConnections.get(toKey).add(fromKey)
    }
  }
  const statementsMap = new Map()
  for (const v of vertices) statementsMap.set(v._key, v.statementsCount || 0)

  for (const [orgKey, connectedKeys] of orgConnections) {
    if (connectedKeys.size <= orgMemberCap) continue
    const sorted = [...connectedKeys].sort((a, b) => (statementsMap.get(b) || 0) - (statementsMap.get(a) || 0))
    const excess = sorted.slice(orgMemberCap)
    for (const pKey of excess) {
      if (pKey === focalQid) continue  // THE FIX: never remove the focal node
      let canRemove
      if (focalQid === orgKey) {
        canRemove = true
      } else if (focalQid) {
        canRemove = !edges.some((e) => {
          const fk = e._from?.split('/')[1]
          const tk = e._to?.split('/')[1]
          return (fk === pKey && tk === focalQid) || (tk === pKey && fk === focalQid)
        })
      } else {
        canRemove = true
      }
      if (canRemove && !removedNodeKeys.has(pKey)) {
        removedNodeKeys.add(pKey)
      }
    }
  }
  return removedNodeKeys
}

// ── Test runner ──

let passed = 0
let failed = 0

function assert(condition, msg) {
  if (condition) { passed++; console.log(`  ✓ ${msg}`) }
  else { failed++; console.error(`  ✗ ${msg}`) }
}

// ── Test 1: Relative edge suppression ──
console.log('\n── Relative edge suppression ──')

{
  // Sibling + relative between same pair → relative should be suppressed
  const edges = [
    { _from: 'persons/A', _to: 'persons/B', type: 'sibling' },
    { _from: 'persons/A', _to: 'persons/B', type: 'relative' },
  ]
  const result = buildEdgeElements(edges, 'A')
  assert(result.length === 1, 'sibling + relative → 1 edge (relative suppressed)')
  assert(result[0].data.type === 'sibling', 'surviving edge is sibling')
  assert(!result[0].data.label.includes('relative'), 'label does not include relative')
}

{
  // Parent + relative between same pair → relative should be suppressed
  const edges = [
    { _from: 'persons/A', _to: 'persons/B', type: 'parent' },  // A's parent is B
    { _from: 'persons/A', _to: 'persons/B', type: 'relative' },
  ]
  const result = buildEdgeElements(edges, 'A')
  assert(result.length === 1, 'parent + relative → 1 edge (relative suppressed)')
  assert(result[0].data.type === 'parent', 'surviving edge is parent')
}

{
  // Child + relative between same pair → relative should be suppressed (child canonicalizes to parent)
  const edges = [
    { _from: 'persons/A', _to: 'persons/B', type: 'child' },
    { _from: 'persons/A', _to: 'persons/B', type: 'relative' },
  ]
  const result = buildEdgeElements(edges, 'A')
  // child canonicalizes to parent; both parent and relative exist for same pair
  assert(result.length === 1, 'child + relative → 1 edge (relative suppressed after child→parent canonical)')
  assert(result[0].data.type === 'parent', 'surviving edge is parent (canonical of child)')
}

{
  // Spouse + relative → relative should be suppressed
  const edges = [
    { _from: 'persons/A', _to: 'persons/B', type: 'spouse' },
    { _from: 'persons/A', _to: 'persons/B', type: 'relative' },
  ]
  const result = buildEdgeElements(edges, 'A')
  assert(result.length === 1, 'spouse + relative → 1 edge')
  assert(result[0].data.type === 'spouse', 'surviving edge is spouse')
}

{
  // Only relative (no other family) → relative should survive
  const edges = [
    { _from: 'persons/A', _to: 'persons/B', type: 'relative' },
  ]
  const result = buildEdgeElements(edges, 'A')
  assert(result.length === 1, 'sole relative → survives')
  assert(result[0].data.type === 'relative', 'type is relative')
}

{
  // Relative + employer (non-family) → both should survive (different pair types context)
  const edges = [
    { _from: 'persons/A', _to: 'organizations/O1', type: 'employer' },
    { _from: 'persons/A', _to: 'persons/B', type: 'relative' },
  ]
  const result = buildEdgeElements(edges, 'A')
  assert(result.length === 2, 'relative + employer (different pairs) → both survive')
}

// ── Test 2: BFS generation computation ──
console.log('\n── BFS generation computation ──')

{
  const edges = [
    { _from: 'persons/root', _to: 'persons/parent1', type: 'parent' },
    { _from: 'persons/root', _to: 'persons/sib1', type: 'sibling' },
    { _from: 'persons/root', _to: 'persons/child1', type: 'child' },
    { _from: 'persons/root', _to: 'persons/rel1', type: 'relative' },
    { _from: 'persons/root', _to: 'persons/spouse1', type: 'spouse' },
  ]
  const gen = computeGenerations('root', edges)
  assert(gen['root'] === 0, 'root is gen 0')
  assert(gen['parent1'] === -1, 'parent is gen -1')
  assert(gen['sib1'] === 0, 'sibling is gen 0')
  assert(gen['child1'] === 1, 'child is gen 1')
  assert(!('rel1' in gen), 'relative-only node does NOT get a generation (ambiguous)')
  assert(gen['spouse1'] === 0, 'spouse is gen 0')
}

{
  // relative-only connection should NOT get a generation (generation is ambiguous)
  const edges = [
    { _from: 'persons/A', _to: 'persons/B', type: 'relative' },
  ]
  const gen = computeGenerations('A', edges)
  assert(!('B' in gen), 'relative-only node has no generation number')
}

// ── Test 3: Root node protection in org capping ──
console.log('\n── Root node protection in org capping ──')

{
  // Create a big org with many members; root person is a low-statementsCount member
  const vertices = [
    { _key: 'root', _id: 'persons/root', entityType: 'person', statementsCount: 5 },
    { _key: 'org1', _id: 'organizations/org1', entityType: 'organization' },
  ]
  const edges = [
    { _from: 'persons/root', _to: 'organizations/org1', type: 'employer' },
  ]
  // Add 10 more high-statementsCount members
  for (let i = 1; i <= 10; i++) {
    const key = `member${i}`
    vertices.push({ _key: key, _id: `persons/${key}`, entityType: 'person', statementsCount: 100 + i })
    edges.push({ _from: `persons/${key}`, _to: 'organizations/org1', type: 'employer' })
  }

  const removed = simulateOrgCap(vertices, edges, 'root', 3)
  assert(!removed.has('root'), 'focal/root node is never removed by org cap')
  assert(removed.size > 0, 'some members are removed (cap applied)')
  assert(removed.size <= 8, 'not all members removed')
}

{
  // Same test but with family mode cap (3)
  const vertices = [
    { _key: 'focal', _id: 'persons/focal', entityType: 'person', statementsCount: 1 },
    { _key: 'bigorg', _id: 'organizations/bigorg', entityType: 'organization' },
  ]
  const edges = [
    { _from: 'persons/focal', _to: 'organizations/bigorg', type: 'employer' },
  ]
  for (let i = 1; i <= 20; i++) {
    const key = `p${i}`
    vertices.push({ _key: key, _id: `persons/${key}`, entityType: 'person', statementsCount: 50 + i })
    edges.push({ _from: `persons/${key}`, _to: 'organizations/bigorg', type: 'employer' })
  }

  const removed = simulateOrgCap(vertices, edges, 'focal', 3)
  assert(!removed.has('focal'), 'focal node protected even with tight cap (ORG_MEMBER_CAP_FAMILY=3)')
}

// ── Test 4: Same-gen edge hiding in family mode ──
console.log('\n── Same-gen edge hiding in family mode ──')

{
  const edges = [
    { _from: 'persons/root', _to: 'persons/parent1', type: 'parent' },
    { _from: 'persons/root', _to: 'persons/sib1', type: 'sibling' },
    { _from: 'persons/root', _to: 'persons/spouse1', type: 'spouse' },
    { _from: 'persons/root', _to: 'persons/rel1', type: 'relative' },
  ]
  const gen = computeGenerations('root', edges)

  // Simulate edge hiding: same-gen edges should be hidden
  const edgeEls = buildEdgeElements(edges, 'root')
  const sameGenHidden = []
  const crossGenVisible = []
  for (const el of edgeEls) {
    const srcGen = gen[el.data.source]
    const tgtGen = gen[el.data.target]
    if (srcGen !== undefined && tgtGen !== undefined && srcGen === tgtGen) {
      sameGenHidden.push(el.data.type)
    } else {
      crossGenVisible.push(el.data.type)
    }
  }
  assert(crossGenVisible.includes('parent'), 'parent (cross-gen) stays visible')
  assert(sameGenHidden.includes('sibling'), 'sibling (same-gen) would be hidden')
  assert(sameGenHidden.includes('spouse'), 'spouse (same-gen) would be hidden')
  // relative-only nodes (rel1) don't get generation numbers — they go to the non-family row
  assert(!(gen['rel1']), 'relative-only node has no generation → placed in non-family row')
}

// ── Summary ──
console.log(`\n${'─'.repeat(40)}`)
console.log(`Results: ${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)
