// Pure helpers for family-layout reasoning. Kept free of cytoscape so they can
// be unit-tested and reused. Every function consumes the raw ArangoDB edge
// shape (`{ _from, _to, type }`) and returns plain data.

export const FAMILY_EDGE_TYPES = new Set([
  'parent', 'father', 'mother', 'child', 'sibling', 'spouse', 'relative',
])

// Edges whose natural arrow direction is "subject's X is object" — these need
// swapping when rendered so the arrow points parent→child, employer→employee.
// owned_by A→B (B owns A) → swap so arrow shows B→A labeled "owns"
// parent_of A→B (A's parent org is B) → swap so arrow shows B→A labeled "parent_of"
// Both are only swapped when their edge survives seenEdges dedup before their inverse
// counterpart; the inverse (owns/subsidiary_of) has the correct raw direction already.
export const SWAP_DIRECTION_TYPES = new Set([
  'parent', 'father', 'mother', 'employer',
  'owned_by',
  'parent_of',
])

function endpoints(edge) {
  return [edge._from?.split('/')[1], edge._to?.split('/')[1]]
}

// BFS through raw family edges to assign a generation offset to each reachable
// node relative to rootQid. Root is 0, parents are -1, children are +1, etc.
export function computeGenerations(rootQid, rawEdges) {
  const gen = { [rootQid]: 0 }
  const queue = [rootQid]
  while (queue.length > 0) {
    const current = queue.shift()
    for (const edge of rawEdges) {
      if (!FAMILY_EDGE_TYPES.has(edge.type)) continue
      const [fromKey, toKey] = endpoints(edge)
      if (!fromKey || !toKey) continue
      const t = edge.type
      if (t === 'parent' || t === 'father' || t === 'mother') {
        if (fromKey === current && !(toKey in gen))   { gen[toKey]   = gen[current] - 1; queue.push(toKey) }
        if (toKey   === current && !(fromKey in gen)) { gen[fromKey] = gen[current] + 1; queue.push(fromKey) }
      } else if (t === 'child') {
        if (fromKey === current && !(toKey in gen))   { gen[toKey]   = gen[current] + 1; queue.push(toKey) }
        if (toKey   === current && !(fromKey in gen)) { gen[fromKey] = gen[current] - 1; queue.push(fromKey) }
      } else if (t === 'sibling' || t === 'spouse' || t === 'relative') {
        if (fromKey === current && !(toKey in gen))   { gen[toKey]   = gen[current]; queue.push(toKey) }
        if (toKey   === current && !(fromKey in gen)) { gen[fromKey] = gen[current]; queue.push(fromKey) }
      }
    }
  }
  return gen
}

// Raw-edge directional inspection between node and root. Returns the canonical
// relationship string ("parent", "child", "spouse", "sibling", "relative") or
// null when there is no direct edge between them.
// Scans all edges rather than returning on the first match so that specific
// relationships (spouse, sibling, parent/child) take precedence over the generic
// "relative" — a node can have both edges and should be styled by the specific one.
export function getRelationshipToRoot(nodeQid, rootQid, rawEdges) {
  let relativeFound = false
  for (const edge of rawEdges) {
    const [fk, tk] = endpoints(edge)
    if ((fk !== rootQid && tk !== rootQid) || (fk !== nodeQid && tk !== nodeQid)) continue
    const t = edge.type
    if (t === 'spouse')  return 'spouse'
    if (t === 'sibling') return 'sibling'
    if (t === 'parent' || t === 'father' || t === 'mother') {
      return fk === rootQid ? 'parent' : 'child'
    }
    if (t === 'child') {
      return fk === rootQid ? 'child' : 'parent'
    }
    if (t === 'relative') relativeFound = true
  }
  return relativeFound ? 'relative' : null
}

// Single-pass build of a `childKey → Set<parentKey>` lookup. Used everywhere
// we need to know "who are the parents of X" — avoids re-scanning the full
// edge list for each lookup.
export function buildParentsMap(rawEdges) {
  const byChild = new Map()
  for (const edge of rawEdges) {
    const [fk, tk] = endpoints(edge)
    if (!fk || !tk) continue
    const t = edge.type
    let childKey = null
    let parentKey = null
    if (t === 'parent' || t === 'father' || t === 'mother') {
      childKey = fk; parentKey = tk
    } else if (t === 'child') {
      childKey = tk; parentKey = fk
    } else continue
    if (!byChild.has(childKey)) byChild.set(childKey, new Set())
    byChild.get(childKey).add(parentKey)
  }
  return byChild
}

// Set of qids that are direct parents of root via a family edge.
export function getRootParents(rootQid, rawEdges, parentsMap = null) {
  const map = parentsMap || buildParentsMap(rawEdges)
  return new Set(map.get(rootQid) || [])
}

// Identify the spouses of root (preserving first-seen order) and, for each of
// root's children, which spouse that child is also attached to. A child whose
// other-parent is not present in the graph ends up with no mapping.
//
// Uses a single edge-scan to build a parents-map, then O(children × spouses)
// membership checks — previously this was children × spouses × edges.
export function computeFamilyUnits(rootQid, rawEdges, parentsMap = null) {
  const spouseOrder = []
  const rootChildren = new Set()
  for (const edge of rawEdges) {
    const [fk, tk] = endpoints(edge)
    if (!fk || !tk) continue
    if (edge.type === 'spouse') {
      const spouse = fk === rootQid ? tk : (tk === rootQid ? fk : null)
      if (spouse && !spouseOrder.includes(spouse)) spouseOrder.push(spouse)
    } else if (edge.type === 'child' && fk === rootQid) {
      rootChildren.add(tk)
    } else if ((edge.type === 'parent' || edge.type === 'father' || edge.type === 'mother') && tk === rootQid) {
      rootChildren.add(fk)
    }
  }

  const pmap = parentsMap || buildParentsMap(rawEdges)
  const childToSpouse = {}
  for (const childKey of rootChildren) {
    const parents = pmap.get(childKey)
    if (!parents) continue
    for (const spouseKey of spouseOrder) {
      if (parents.has(spouseKey)) { childToSpouse[childKey] = spouseKey; break }
    }
  }
  return { spouseOrder, childToSpouse, rootChildren }
}

// Pure color assignment — returns per-node border colors + the distinct set of
// legend entries that should be drawn. Accepts palette arguments so it can be
// tested without importing visual constants.
//
// The sibling fallback (same-generation, no direct edge → sibling) only fires
// when the node provably shares a parent with root. Previously any gen-0 node
// with no direct edge was painted sibling-blue, which in non-family layouts
// (where generations propagate through spouse/relative chains) could mislabel
// in-laws.
export function computeFamilyColors({
  rootQid,
  rawEdges,
  generations,
  familyUnits,
  unitColors,
  borderColors,
  childUnknownColor,
  parentsMap = null,
}) {
  const pmap = parentsMap || buildParentsMap(rawEdges)
  const rootParents = pmap.get(rootQid) || new Set()
  const spouseSet = new Set(familyUnits.spouseOrder)

  const spouseColors = {}
  familyUnits.spouseOrder.forEach((sQid, i) => {
    spouseColors[sQid] = unitColors[i % unitColors.length]
  })

  const nodeColors = {}
  const foundRels = new Set()
  let hasUnknownSpouseChild = false
  const unitsWithChildren = new Set()

  const sharesParentWithRoot = (key) => {
    const kp = pmap.get(key)
    if (!kp) return false
    for (const p of kp) if (rootParents.has(p)) return true
    return false
  }

  // Find which spouse is a parent of this node (step-child detection).
  // Returns the spouse qid or null.
  const spouseParentOf = (key) => {
    const parents = pmap.get(key)
    if (!parents) return null
    for (const p of parents) if (spouseSet.has(p)) return p
    return null
  }

  for (const key of Object.keys(generations)) {
    if (key === rootQid) continue
    let rel = getRelationshipToRoot(key, rootQid, rawEdges)
    if (!rel && generations[key] === 0 && sharesParentWithRoot(key)) rel = 'sibling'

    // Step-child: no direct relationship to root, but is a child of one of
    // root's spouses. Color them with that spouse's unit color.
    if (!rel) {
      const sp = spouseParentOf(key)
      if (sp) {
        nodeColors[key] = spouseColors[sp] || unitColors[0]
        unitsWithChildren.add(sp)
        foundRels.add('child')
        continue
      }
    }

    if (!rel) continue

    let color = null
    if (rel === 'parent')        color = borderColors.parent
    else if (rel === 'sibling')  color = borderColors.sibling
    else if (rel === 'relative') color = borderColors.relative
    else if (rel === 'spouse')   color = spouseColors[key] || unitColors[0]
    else if (rel === 'child') {
      const spouseQid = familyUnits.childToSpouse[key]
      if (spouseQid) {
        color = spouseColors[spouseQid] || unitColors[0]
        unitsWithChildren.add(spouseQid)
      } else {
        // Check if a spouse is a parent of this child (shared child where
        // the edge to root is missing but the edge to spouse exists)
        const sp = spouseParentOf(key)
        if (sp) {
          color = spouseColors[sp] || unitColors[0]
          unitsWithChildren.add(sp)
        } else {
          color = childUnknownColor
          hasUnknownSpouseChild = true
        }
      }
    }
    if (!color) continue
    nodeColors[key] = color
    foundRels.add(rel)
  }

  const legend = {}
  if (foundRels.has('parent'))   legend.parent   = borderColors.parent
  if (foundRels.has('sibling'))  legend.sibling  = borderColors.sibling
  if (foundRels.has('relative')) legend.relative = borderColors.relative
  if (foundRels.has('spouse') || unitsWithChildren.size > 0) {
    legend.spouseUnits = familyUnits.spouseOrder
      .filter((sQid) => foundRels.has('spouse') || unitsWithChildren.has(sQid))
      .map((sQid) => ({ qid: sQid, color: spouseColors[sQid] }))
  }
  if (hasUnknownSpouseChild) legend.childUnknown = childUnknownColor

  return { nodeColors, legend, spouseColors }
}
