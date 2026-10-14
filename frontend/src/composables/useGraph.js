import { ref, shallowRef, computed } from 'vue'
import { useApi } from './useApi.js'
import { useLocale } from './useLocale.js'
import {
  getCytoscapeStyle,
  getLayoutOptions,
  getEdgeColor,
  getNodeColor,
  translateEdgeType,
  FAMILY_BORDER_COLORS,
  FAMILY_UNIT_COLORS,
  FAMILY_CHILD_UNKNOWN_COLOR,
} from '../utils/cytoscape-config.js'
import {
  FAMILY_EDGE_TYPES,
  SWAP_DIRECTION_TYPES,
  computeGenerations,
  computeFamilyUnits,
  getRootParents,
  computeFamilyColors,
} from '../utils/family-graph.js'
import { groupByParty, computeWestminster, NON_SITTING_PARTIES } from '../utils/hemicycle.js'
import { getLogoVerdict, probeLogo } from '../utils/logo-quality.js'

const REL_BORDER_WIDTH = 2.8  // colored relationship border thickness (family layout)

export function useGraph() {
  const api = useApi()
  const { t, prefName } = useLocale()
  const cy = shallowRef(null)
  const loading = ref(false)
  const selectedNode = ref(null)
  const selectedEdge = ref(null)
  let _selNodeEl = null
  const rootNodeId = ref(null)
  const focusOnHover = ref(true)
  // Increments after each graph mutation so dependents can react to cy contents changing
  const graphVersion = ref(0)
  // Store current filters so they can be reapplied after each load
  const _activeEdgeTypes = ref(null)
  const _activeNodeTypes = ref(null)
  // Raw edges from last API response, used for family layout generation BFS
  const _rawEdges = ref([])
  // True when the current graph has at least one family-type edge
  const hasFamilyEdges = computed(() => _rawEdges.value.some((e) => FAMILY_EDGE_TYPES.has(e.type)))
  // Actual relationship→color map from the last applyRelationshipBorders call (legend reads this)
  const activeFamilyColors = ref({})
  // Which role groups are actually present in the current org-circle layout
  const orgCircleRolesPresent = ref(new Set())
  // Cached generation map from last family layout — used by applyFilters to re-hide same-gen edges
  let _familyGenerations = null
  // Parliament party buckets — set by loadParliamentLayout, read by App.vue for dblclick
  const parliamentParties = ref([])
  // Graph-space x midpoints of gov/opp benches — used by App.vue to position GOVERNMENT/OPPOSITION labels
  const parliamentBenchCenters = ref(null)

  let initPromise = null
  let _cachedThemeOpts = {}
  // Invalidates an in-flight dynamic import when GraphCanvas is unmounted.
  // Without this, a late init could attach a Cytoscape instance to a container
  // that Vue has already removed.
  let initGeneration = 0

  function init(container) {
    if (cy.value && !cy.value.destroyed()) return Promise.resolve(cy.value)
    if (initPromise) return initPromise

    const generation = initGeneration
    initPromise = (async () => {
      const [{ default: cytoscape }, { default: coseBilkent }] = await Promise.all([
        import('cytoscape'),
        import('cytoscape-cose-bilkent'),
      ])
      if (generation !== initGeneration) return null
      cytoscape.use(coseBilkent)

      cy.value = cytoscape({
      container,
      style: getCytoscapeStyle(api.imageUrl, _cachedThemeOpts),
      minZoom: 0.2,
      maxZoom: 5,
      wheelSensitivity: 0.3,
      })

    // Mouse wheels emit large discrete deltas (deltaMode=1 for lines, or |deltaY| >= 50 in pixel mode).
    // Trackpads emit many small pixel deltas. Boost zoom step for mouse wheels so desktop doesn't feel sluggish.
    container.addEventListener('wheel', (e) => {
      const isMouseWheel = e.deltaMode !== 0 || Math.abs(e.deltaY) >= 50
      if (!isMouseWheel) return
      e.preventDefault()
      e.stopPropagation()
      const c = cy.value
      if (!c) return
      const rect = container.getBoundingClientRect()
      const factor = Math.exp(-e.deltaY * 0.003)
      const newZoom = Math.min(5, Math.max(0.2, c.zoom() * factor))
      c.zoom({
        level: newZoom,
        renderedPosition: { x: e.clientX - rect.left, y: e.clientY - rect.top },
      })
    }, { passive: false, capture: true })

    const _restoreRelBorder = (nodeEl) => {
      if (!nodeEl) return
      const saved = nodeEl.scratch('_relBorder') || nodeEl.scratch('_roleBorder') || nodeEl.scratch('_partyBorder')
      if (saved) {
        nodeEl.style({ 'border-width': saved.width, 'border-color': saved.color })
      } else {
        nodeEl.removeStyle('border-width border-color')
      }
    }

    cy.value.on('tap', 'node', (e) => {
      selectedEdge.value = null
      selectedNode.value = e.target.data()
      // In parliament mode: skip hover-dimming (too many nodes — looks bad)
      if (focusOnHover.value && layoutMode.value !== 'parliament') {
        const hood = e.target.closedNeighborhood()
        cy.value.batch(() => {
          cy.value.elements().not(hood).addClass('dimmed').removeClass('highlighted')
          hood.addClass('highlighted').removeClass('dimmed')
        })
      }
      // Parliament person nodes carry sparse data — background-fetch full record
      if (layoutMode.value === 'parliament' && e.target.data('entityType') === 'person') {
        const qid = e.target.data('qid')
        api.getPerson(qid).then((doc) => {
          if (!doc || selectedNode.value?.qid !== qid) return
          selectedNode.value = {
            ...selectedNode.value,
            birth_date:     doc.birth_date,
            birth_place:    doc.birth_place,
            death_date:     doc.death_date,
            death_place:    doc.death_place,
            cause_of_death: doc.cause_of_death,
            description_en: doc.description_en,
            description_it: doc.description_it,
            occupations:    doc.occupations    || [],
            nationalities:  doc.nationalities  || [],
            gender:         doc.gender,
          }
        }).catch(() => {})
      }
      // Restore previous node's relationship border
      if (_selNodeEl && _selNodeEl !== e.target) _restoreRelBorder(_selNodeEl)
      _selNodeEl = e.target
      // Apply selection border (overrides any inline relationship border)
      e.target.style({ 'border-width': 5, 'border-color': '#b03030' })
      // Selection emphasis — skip for reduced motion and parliament org square
      // nodes (they have inline dimensions that must not be animated).
      const node = e.target
      const isParliamentOrg = layoutMode.value === 'parliament' && node.data('entityType') === 'organization'
      const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
      if (!isParliamentOrg && !reduceMotion) {
        // Preserve the rendered aspect ratio, including portrait person nodes.
        const width = parseFloat(node.style('width')) || node.data('size')
        const height = parseFloat(node.style('height')) || node.data('size')
        node.stop(true)
        node.animate({ style: { width: width * 1.24, height: height * 1.24 }, duration: 170, easing: 'ease-out' })
            .animate({ style: { width, height }, duration: 180, easing: 'ease-in' })
      }
    })
    cy.value.on('tap', 'edge', (e) => {
      selectedNode.value = null
      selectedEdge.value = e.target.data()
    })
    cy.value.on('tap', (e) => {
      if (e.target === cy.value) {
        selectedNode.value = null
        selectedEdge.value = null
        cy.value.elements().removeClass('dimmed').removeClass('highlighted')
        _restoreRelBorder(_selNodeEl)
        _selNodeEl = null
      }
    })
    let _focusResetTimer = null

    cy.value.on('mouseover', 'node', (e) => {
      clearTimeout(_focusResetTimer)
      if (!focusOnHover.value || layoutMode.value === 'parliament') return
      const hood = e.target.closedNeighborhood()
      cy.value.batch(() => {
        cy.value.elements().not(hood).addClass('dimmed').removeClass('highlighted')
        hood.addClass('highlighted').removeClass('dimmed')
      })
    })
      cy.value.on('mouseout', 'node', () => {
      if (layoutMode.value === 'parliament') return
      _focusResetTimer = setTimeout(() => {
        cy.value.batch(() => {
          cy.value.elements().removeClass('dimmed').removeClass('highlighted')
        })
      }, 50)
      })
      return cy.value
    })().catch((error) => {
      // A failed lazy import must be retryable on the next mount.
      if (generation === initGeneration) initPromise = null
      throw error
    })

    return initPromise
  }

  async function whenReady() {
    const current = cy.value
    if (current && !current.destroyed()) return current

    const pending = initPromise
    if (!pending) {
      throw new Error("Graph canvas is not mounted")
    }

    const instance = await pending
    if (!instance || instance.destroyed()) {
      throw new Error("Graph canvas initialization was cancelled")
    }
    return instance
  }

  function _entityType(v) {
    const id = v._id || ''
    if (id.startsWith('organizations/')) return 'organization'
    if (id.startsWith('events/')) return 'event'
    return 'person'
  }

  function _entityColor(v) {
    const et = _entityType(v)
    if (et === 'organization') return '#8a8278'
    if (et === 'event') return '#ce9178'
    return getNodeColor(v.occupations)
  }

  // Map edge types to their canonical form — inverses collapse into one edge
  const INVERSE_EDGE_MAP = {
    child: 'parent',
    employee: 'employer',
    participant_in: 'participant',
    victim: 'killed_by',
    owned_by: 'owns',           // owned_by A→B (B owns A) → canonical "owns"
    subsidiary_of: 'parent_of', // subsidiary_of A→B (B is parent) → canonical "parent_of"
    doctoral_student: 'doctoral_advisor', // same relationship, opposite direction
  }

  // Max members to show per org node before hiding excess (default)
  const ORG_MEMBER_CAP = 20
  // Tighter cap used in family layout — orgs are secondary context there
  const ORG_MEMBER_CAP_FAMILY = 3
  // Map of org keys → chosen member cap (200 or 1000); absent = default cap
  const _expandedOrgs = ref(new Map())
  // Track whether current layout is org-circle mode
  const layoutMode = ref('network') // 'network' | 'family' | 'org-circle'
  // When user explicitly sets a mode, don't auto-detect on next load
  const _layoutModeOverride = ref(null)

  function _canonicalEdgeType(type) {
    return INVERSE_EDGE_MAP[type] ?? type
  }

  function _truncateLabel(text, maxWords = 3) {
    if (!text) return text
    const words = text.trim().split(/\s+/)
    if (words.length <= maxWords) return text
    return words.slice(0, maxWords).join(' ') + '\u2026'
  }

  // After layout settles, set per-node text-max-width and word count based on
  // horizontal distance to nearest neighbor — prevents label overlap in tight
  // layouts, shows more text where space allows. Batched for performance.
  function _adjustLabels() {
    if (!cy.value) return
    const visible = cy.value.nodes().not('.ring-circle').not('.layout-hidden').filter((n) => n.visible())
    if (visible.length === 0) return

    const items = new Array(visible.length)
    let idx = 0
    visible.forEach((n) => {
      const pos = n.position()
      items[idx++] = { node: n, x: pos.x, y: pos.y, valign: n.style('text-valign') || 'bottom' }
    })

    const VERT_THRESHOLD = 90
    const len = items.length

    cy.value.batch(() => {
      for (let i = 0; i < len; i++) {
        const item = items[i]
        let minDx = Infinity
        for (let j = 0; j < len; j++) {
          if (j === i) continue
          const other = items[j]
          if (Math.abs(other.y - item.y) > VERT_THRESHOLD) continue
          if (item.valign !== other.valign) continue
          const dx = Math.abs(other.x - item.x)
          if (dx > 0 && dx < minDx) minDx = dx
        }

        const textMaxWidth = minDx === Infinity
          ? 180
          : Math.max(55, Math.min(180, minDx - 10))

        const fullLabel = item.node.data('fullLabel')
        if (fullLabel) {
          const words = fullLabel.trim().split(/\s+/)
          const maxWords = textMaxWidth >= 150 ? 7
            : textMaxWidth >= 110 ? 5
            : textMaxWidth >= 80 ? 4
            : 3
          item.node.data('label',
            words.length <= maxWords
              ? fullLabel
              : words.slice(0, maxWords).join(' ') + '\u2026')
        }
        item.node.style('text-max-width', `${textMaxWidth}px`)
      }
    })
  }

  function buildElements(vertices, edges, activeEdgeTypes = null, focalQid = null, orgMemberCap = ORG_MEMBER_CAP) {
    const nodes = vertices.map((v) => {
      const key = v._key || v._id?.split('/')[1]
      const degree = edges.filter(
        (e) => e._from?.endsWith(`/${key}`) || e._to?.endsWith(`/${key}`)
      ).length
      const entityType = _entityType(v)
      const rawHasImage = !!v.image_cached_path
      const rawImageUrl = rawHasImage ? api.imageUrl(key) : undefined
      // Org logos that are dark slabs or extreme banners are hidden up front
      // when we already know their verdict, so they render as clean tiles.
      const logoRejected =
        entityType === 'organization' && rawImageUrl
          ? getLogoVerdict(rawImageUrl) === false
          : false
      const hasImage = rawHasImage && !logoRejected
      return {
        group: 'nodes',
        data: {
          id: key,
          qid: key,
          entityType,
          label: _truncateLabel(prefName(v) || key),
          fullLabel: prefName(v) || key,
          name_en: v.name_en,
          name_it: v.name_it,
          description_it: v.description_it,
          description_en: v.description_en,
          occupations: v.occupations,
          nationalities: v.nationalities,
          birth_date: v.birth_date,
          death_date: v.death_date,
          birth_place: v.birth_place,
          death_place: v.death_place,
          cause_of_death: v.cause_of_death,
          gender: v.gender,
          // org fields
          type: v.type,
          founded: v.founded,
          dissolved: v.dissolved,
          // event fields
          date: v.date,
          statementsCount: v.wikidata_statements_count || 0,
          // Generous nodes keep faces and entity types legible after fit(),
          // particularly on narrow touch screens.
          size: Math.max(38, Math.min(84, 38 + degree * 5.5)),
          color: _entityColor(v),
          hasImage: hasImage || undefined,
          imageUrl: hasImage ? rawImageUrl : undefined,
          // Deferred probe target — evaluated after the nodes are on the canvas.
          logoProbeUrl: entityType === 'organization' && rawHasImage ? rawImageUrl : undefined,
        },
      }
    })

    // Org member cap: build a map of org → connected node keys
    const removedNodeKeys = new Set()
    const orgConnections = new Map() // orgKey → Set of connected keys
    for (const e of edges) {
      const fromKey = e._from?.split('/')[1]
      const toKey = e._to?.split('/')[1]
      if (!fromKey || !toKey) continue
      const fromIsOrg = nodes.find((n) => n.data.id === fromKey)?.data.entityType === 'organization'
      const toIsOrg = nodes.find((n) => n.data.id === toKey)?.data.entityType === 'organization'
      if (fromIsOrg) {
        if (!orgConnections.has(fromKey)) orgConnections.set(fromKey, new Set())
        orgConnections.get(fromKey).add(toKey)
      }
      if (toIsOrg) {
        if (!orgConnections.has(toKey)) orgConnections.set(toKey, new Set())
        orgConnections.get(toKey).add(fromKey)
      }
    }
    // Build a quick lookup for statementsCount
    const statementsMap = new Map()
    for (const n of nodes) statementsMap.set(n.data.id, n.data.statementsCount || 0)

    for (const [orgKey, connectedKeys] of orgConnections) {
      const expandedCap = _expandedOrgs.value.get(orgKey)
      const effectiveCap = expandedCap || orgMemberCap
      if (connectedKeys.size <= effectiveCap) continue
      // Sort by notability descending so the most notable members stay visible
      const sorted = [...connectedKeys].sort((a, b) => (statementsMap.get(b) || 0) - (statementsMap.get(a) || 0))
      const excess = sorted.slice(effectiveCap)
      const focalIsThisOrg = focalQid === orgKey
      let actuallyRemoved = 0
      for (const pKey of excess) {
        if (pKey === focalQid) continue  // never remove the root/focal node
        let canRemove
        if (focalIsThisOrg) {
          // The org itself is focal — cap its members directly
          canRemove = true
        } else if (focalQid) {
          // Remove if not directly connected to the focal person
          canRemove = !edges.some((e) => {
            const fk = e._from?.split('/')[1]
            const tk = e._to?.split('/')[1]
            return (fk === pKey && tk === focalQid) || (tk === pKey && fk === focalQid)
          })
        } else {
          // Without focal context: conservative — only remove sole-org-connected nodes
          const nodeEdges = edges.filter((e) => {
            const fk = e._from?.split('/')[1]
            const tk = e._to?.split('/')[1]
            return fk === pKey || tk === pKey
          })
          canRemove = nodeEdges.every((e) => {
            const fk = e._from?.split('/')[1]
            const tk = e._to?.split('/')[1]
            return (fk === pKey ? tk : fk) === orgKey
          })
        }
        if (canRemove && !removedNodeKeys.has(pKey)) {
          removedNodeKeys.add(pKey)
          actuallyRemoved++
        }
      }
      const orgNode = nodes.find((n) => n.data.id === orgKey)
      if (orgNode && actuallyRemoved > 0)
        orgNode.data.hiddenMemberCount = (orgNode.data.hiddenMemberCount || 0) + actuallyRemoved
    }

    // Secondary org suppression: when the focal person has >= orgMemberCap direct org
    // connections, hide org nodes that are not directly connected to the focal node —
    // they belong to neighbors and clutter the view.
    if (focalQid) {
      const focalDirectOrgs = new Set()
      for (const e of edges) {
        const fk = e._from?.split('/')[1]
        const tk = e._to?.split('/')[1]
        const neighborKey = fk === focalQid ? tk : tk === focalQid ? fk : null
        if (!neighborKey) continue
        const neighbor = nodes.find((n) => n.data.id === neighborKey)
        if (neighbor?.data.entityType === 'organization') focalDirectOrgs.add(neighborKey)
      }
      if (focalDirectOrgs.size >= orgMemberCap) {
        for (const node of nodes) {
          if (node.data.entityType !== 'organization') continue
          if (node.data.id === focalQid) continue  // never suppress the focal org itself
          if (focalDirectOrgs.has(node.data.id)) continue
          if (!removedNodeKeys.has(node.data.id)) removedNodeKeys.add(node.data.id)
        }
      }

      // Focal-neighbor cap: when the focal node has too many direct neighbors of any
      // type, keep only the most notable ones. Without this, star topologies with 100+
      // leaves overwhelm any layout algorithm and produce overlapping clusters.
      const FOCAL_ORG_CAP = 12
      const FOCAL_PERSON_CAP = 40
      const focalNeighborsByType = { person: [], other: [] }  // other = org + event
      for (const e of edges) {
        const fk = e._from?.split('/')[1]
        const tk = e._to?.split('/')[1]
        const neighborKey = fk === focalQid ? tk : tk === focalQid ? fk : null
        if (!neighborKey || removedNodeKeys.has(neighborKey)) continue
        const neighbor = nodes.find((n) => n.data.id === neighborKey)
        if (!neighbor) continue
        const bucket = neighbor.data.entityType === 'person' ? 'person' : 'other'
        focalNeighborsByType[bucket].push({ key: neighborKey, sc: statementsMap.get(neighborKey) || 0 })
      }
      // Cap org/event neighbors
      if (focalNeighborsByType.other.length > FOCAL_ORG_CAP) {
        const seen = new Set()
        const unique = focalNeighborsByType.other.filter(({ key }) => seen.has(key) ? false : (seen.add(key), true))
        unique.sort((a, b) => b.sc - a.sc)
        for (const { key } of unique.slice(FOCAL_ORG_CAP)) {
          if (!removedNodeKeys.has(key)) removedNodeKeys.add(key)
        }
      }
      // Cap person neighbors — keeps the most notable, hides the rest.
      // Skip when the focal node is an org: org-circle layout handles its own member display,
      // and the "show all" feature needs every member present in the graph.
      const focalIsOrg = nodes.find((n) => n.data.id === focalQid)?.data.entityType === 'organization'
      if (!focalIsOrg && focalNeighborsByType.person.length > FOCAL_PERSON_CAP) {
        const seen = new Set()
        const unique = focalNeighborsByType.person.filter(({ key }) => seen.has(key) ? false : (seen.add(key), true))
        unique.sort((a, b) => b.sc - a.sc)
        for (const { key } of unique.slice(FOCAL_PERSON_CAP)) {
          if (!removedNodeKeys.has(key)) removedNodeKeys.add(key)
        }
      }
    }

    const filteredNodes = removedNodeKeys.size > 0
      ? nodes.filter((n) => !removedNodeKeys.has(n.data.id))
      : nodes

    // Pre-compute founder pairs to suppress redundant employer edges on the same pair
    const founderPairs = new Set()
    for (const e of edges) {
      if (_canonicalEdgeType(e.type) === 'founder') {
        const fromKey = e._from?.split('/')[1]
        const toKey = e._to?.split('/')[1]
        if (fromKey && toKey) founderPairs.add([fromKey, toKey].sort().join('|'))
      }
    }

    const seenEdges = new Set()
    const edgeEls = edges
      .filter((e) => {
        const fromKey = e._from?.split('/')[1]
        const toKey = e._to?.split('/')[1]
        if (removedNodeKeys.has(fromKey) || removedNodeKeys.has(toKey)) return false
        // Suppress employer when a founder relationship already exists for the same pair
        if (_canonicalEdgeType(e.type) === 'employer' && founderPairs.has([fromKey, toKey].sort().join('|'))) return false
        if (!activeEdgeTypes) return true
        const ct = _canonicalEdgeType(e.type)
        return activeEdgeTypes.has(e.type) || activeEdgeTypes.has(ct)
      })
      .flatMap((e) => {
        const fromKey = e._from?.split('/')[1]
        const toKey = e._to?.split('/')[1]
        if (!fromKey || !toKey) return []
        const canonicalType = _canonicalEdgeType(e.type)
        // Canonical key: sort endpoints + use canonical type so A→B child and B→A parent collapse
        const canonical = [...[fromKey, toKey].sort(), canonicalType].join('|')
        if (seenEdges.has(canonical)) return []
        seenEdges.add(canonical)
        // Swap direction for types where raw semantics are reversed
        // e.g. raw "parent" A→B means "A's parent is B", so arrow should go B→A
        // Only check raw e.type — canonicalType must NOT be checked, otherwise
        // "child" edges (A's child is B, arrow A→B correct) would wrongly swap
        // because they canonicalize to "parent" which is in SWAP_DIRECTION_TYPES
        const swap = SWAP_DIRECTION_TYPES.has(e.type)
        return [{
          group: 'edges',
          data: {
            id: canonical,
            source: swap ? toKey : fromKey,
            target: swap ? fromKey : toKey,
            label: translateEdgeType(canonicalType, t.value.edgeTypes),
            type: canonicalType,
            color: getEdgeColor(canonicalType),
            role: e.role,
            start_date: e.start_date,
            end_date: e.end_date,
            edgeCollection: e.edge_collection,
          },
        }]
      })

    // Suppress redundant "relative" edges: if a node pair already has a more specific
    // family edge (parent, sibling, spouse), the generic "relative" edge adds no information.
    const SPECIFIC_FAMILY_TYPES = new Set(['parent', 'sibling', 'spouse'])
    const pairTypes = new Map()  // sorted "A|B" → Set of canonical types
    for (const el of edgeEls) {
      const pk = [el.data.source, el.data.target].sort().join('|')
      if (!pairTypes.has(pk)) pairTypes.set(pk, new Set())
      pairTypes.get(pk).add(el.data.type)
    }
    const filteredEdgeEls = edgeEls.filter((el) => {
      if (el.data.type !== 'relative') return true
      const pk = [el.data.source, el.data.target].sort().join('|')
      const types = pairTypes.get(pk)
      // Keep relative only when it's the sole family edge for this pair
      for (const t of types) {
        if (SPECIFIC_FAMILY_TYPES.has(t)) return false
      }
      return true
    })

    // Merge parallel edges with the same directed (source → target) pair.
    // After canonical dedup above, remaining duplicates have different types (e.g. employer + founder).
    // Stacking them produces overlapping straight lines — merge into one edge, concatenate labels.
    // When a specific family type (spouse/sibling/parent/child) and a generic "relative" share
    // the same directed pair, the specific type wins so the edge is styled/colored correctly.
    const SPECIFIC_FAMILY_PRIORITY = new Set(['spouse', 'sibling', 'parent', 'child'])
    const mergedEdgeMap = new Map()
    const mergedEdges = []
    for (const el of filteredEdgeEls) {
      const pairKey = `${el.data.source}|${el.data.target}`
      if (!mergedEdgeMap.has(pairKey)) {
        mergedEdgeMap.set(pairKey, el)
        mergedEdges.push(el)
      } else {
        const existing = mergedEdgeMap.get(pairKey)
        const incomingIsSpecific = SPECIFIC_FAMILY_PRIORITY.has(el.data.type)
        const existingIsGeneric = existing.data.type === 'relative'
        if (incomingIsSpecific && existingIsGeneric) {
          // Promote the more-specific incoming edge; keep existing label appended
          if (existing.data.label && !el.data.label.includes(existing.data.label)) {
            el.data.label = `${el.data.label} · ${existing.data.label}`
          }
          mergedEdgeMap.set(pairKey, el)
          const idx = mergedEdges.indexOf(existing)
          if (idx >= 0) mergedEdges.splice(idx, 1, el)
        } else {
          if (el.data.label && !existing.data.label.includes(el.data.label)) {
            existing.data.label = `${existing.data.label} · ${el.data.label}`
          }
        }
      }
    }

    return [...filteredNodes, ...mergedEdges]
  }

  function _shouldUseFamilyLayout(rootQid, vertices, rawEdges) {
    const rootVertex = vertices.find((v) => (v._key || v._id?.split('/')[1]) === rootQid)
    if (!rootVertex || _entityType(rootVertex) !== 'person') return false
    return rawEdges.some((e) => FAMILY_EDGE_TYPES.has(e.type))
  }

  function applyRelationshipBorders(rootQid, rawEdges, familyUnits, generations) {
    const { nodeColors, legend } = computeFamilyColors({
      rootQid,
      rawEdges,
      generations,
      familyUnits,
      unitColors: FAMILY_UNIT_COLORS,
      borderColors: FAMILY_BORDER_COLORS,
      childUnknownColor: FAMILY_CHILD_UNKNOWN_COLOR,
    })

    cy.value.nodes().forEach((node) => {
      const key = node.data('qid')
      const color = nodeColors[key]
      if (!color) return
      node.scratch('_relBorder', { width: REL_BORDER_WIDTH, color })
      node.style({ 'border-width': REL_BORDER_WIDTH, 'border-color': color })
    })

    // Decorate spouse-unit legend entries with the spouse's display name (cy-only)
    if (legend.spouseUnits) {
      legend.spouseUnits = legend.spouseUnits.map((u) => ({
        ...u,
        name: cy.value.$id(u.qid)?.data('label') || '',
      }))
    }
    activeFamilyColors.value = legend
  }

  function applyFamilyLayout(rootQid, rawEdges) {
    const container = cy.value.container()
    const W = container.clientWidth
    const H = container.clientHeight
    const MARGIN = 0.08
    const MIN_NODE_GAP = 90   // minimum px between node centers (node max=70px + some clearance)
    const ROW_HEIGHT = 130    // px between generation rows

    const generations = computeGenerations(rootQid, rawEdges)
    _familyGenerations = generations  // cache for applyFilters to re-hide same-gen edges
    const genValues = Object.values(generations)
    const minGen = Math.min(...genValues)
    const maxGen = Math.max(...genValues)

    // If everyone is at the same generation level, no hierarchy — fall back
    if (minGen === maxGen) {
      const opts = getLayoutOptions(cy.value.nodes().not('.ring-circle').length)
      opts.stop = _adjustLabels
      cy.value.layout(opts).run()
      return
    }

    const genRange = maxGen - minGen
    const familyKeys = new Set(Object.keys(generations))
    const familyUnits = computeFamilyUnits(rootQid, rawEdges)
    const rootParentsSet = getRootParents(rootQid, rawEdges)

    // Group family nodes by generation, capping oversized rows.
    // For star topologies with 100+ relatives at the same generation, keep only the
    // most notable nodes per row. Root is always kept.
    const FAMILY_ROW_CAP = 16
    const byGen = {}
    for (const [key, g] of Object.entries(generations)) {
      if (!byGen[g]) byGen[g] = []
      byGen[g].push(key)
    }
    for (const [g, keys] of Object.entries(byGen)) {
      if (keys.length <= FAMILY_ROW_CAP) continue
      // Sort by notability (statementsCount from cy node data), keeping root always
      const withRoot = keys.filter((k) => k === rootQid)
      const rest = keys.filter((k) => k !== rootQid)
        .sort((a, b) => (cy.value.$id(b)?.data('statementsCount') || 0) - (cy.value.$id(a)?.data('statementsCount') || 0))
      const kept = new Set([...withRoot, ...rest.slice(0, FAMILY_ROW_CAP - withRoot.length)])
      // Hide excess nodes — use layout-hidden class so applyFilters won't un-hide them
      for (const k of keys) {
        if (!kept.has(k)) {
          cy.value.$id(k)?.addClass('layout-hidden').hide()
          familyKeys.delete(k)
        }
      }
      byGen[g] = keys.filter((k) => kept.has(k))
    }

    // Center the family tree vertically using a fixed row height
    const totalFamilyH = genRange * ROW_HEIGHT
    const familyStartY = Math.max(H * MARGIN, (H - totalFamilyH) / 2)

    // Compute positions for family nodes
    const positions = {}
    for (const [g, keys] of Object.entries(byGen)) {
      const gNum = Number(g)
      const y = familyStartY + (gNum - minGen) * ROW_HEIGHT

      // Order nodes within each generation row so that, visually, each person
      // is placed near the node that anchors their family role:
      //
      // Gen 0 (root's row): root at centre, spouses immediately adjacent, other
      //   in-generation nodes (siblings / same-gen relatives) fan outward.
      //
      // Gen -1 (root's parents row): direct parents of root straddle centre;
      //   any non-parent nodes on this row (uncles, aunts, grandparents-in-law)
      //   are pushed outward so a root with many uncles doesn't end up with its
      //   actual parents stranded at the edge.
      //
      // Gen > 0 (children row): known-spouse children are placed on the same
      //   side root's matching spouse sits at gen 0 — first half of the spouse
      //   order on the left, second half on the right. Unknown-spouse children
      //   sit in the middle directly under root, so they never drift beneath
      //   an uncle on the row above.
      //
      // All other generations keep BFS order.
      let orderedKeys = keys

      const splitAround = (arr, center) => {
        const left = arr.slice(0, Math.ceil(arr.length / 2))
        const right = arr.slice(Math.ceil(arr.length / 2))
        return [...left, ...(center ? [center] : []), ...right]
      }

      if (gNum === 0 && keys.includes(rootQid) && keys.length > 1) {
        const others = keys.filter((k) => k !== rootQid)
        const spouses = others.filter((k) => familyUnits.spouseOrder.includes(k))
        const nonSpouses = others.filter((k) => !familyUnits.spouseOrder.includes(k))
        const nsLeft = nonSpouses.slice(0, Math.ceil(nonSpouses.length / 2))
        const nsRight = nonSpouses.slice(Math.ceil(nonSpouses.length / 2))
        const spLeft = spouses.slice(0, Math.ceil(spouses.length / 2))
        const spRight = spouses.slice(Math.ceil(spouses.length / 2))
        orderedKeys = [...nsLeft, ...spLeft, rootQid, ...spRight, ...nsRight]
      } else if (gNum === -1 && keys.length > 1 && rootParentsSet.size > 0) {
        const parentsArr = keys.filter((k) => rootParentsSet.has(k))
        const others = keys.filter((k) => !rootParentsSet.has(k))
        const oLeft = others.slice(0, Math.ceil(others.length / 2))
        const oRight = others.slice(Math.ceil(others.length / 2))
        orderedKeys = [...oLeft, ...parentsArr, ...oRight]
      } else if (gNum > 0 && familyUnits.spouseOrder.length > 0) {
        // Partition children by their other-parent (spouse of root). Group
        // children whose other-parent is also in the graph on the matching
        // side; place unknown-spouse children in the centre directly under
        // root so they are never stranded far away.
        const byUnit = {}
        const unknown = []
        for (const k of keys) {
          const unit = familyUnits.childToSpouse[k]
          if (unit) {
            if (!byUnit[unit]) byUnit[unit] = []
            byUnit[unit].push(k)
          } else {
            unknown.push(k)
          }
        }
        const half = Math.ceil(familyUnits.spouseOrder.length / 2)
        const leftSpouses = familyUnits.spouseOrder.slice(0, half)
        const rightSpouses = familyUnits.spouseOrder.slice(half)
        orderedKeys = []
        for (const sQid of leftSpouses) if (byUnit[sQid]) orderedKeys.push(...byUnit[sQid])
        orderedKeys.push(...unknown)
        for (const sQid of rightSpouses) if (byUnit[sQid]) orderedKeys.push(...byUnit[sQid])
      } else if (gNum > 0) {
        // No known spouses at all — every child has unknown other-parent.
        // Centre them under root by splitting evenly to both sides.
        orderedKeys = splitAround(keys, null)
      }

      // Calculate spacing: use minimum gap or distribute evenly, whichever is larger
      const unitCount = orderedKeys.length
      const evenGap = (W * (1 - 2 * MARGIN)) / (unitCount + 1)
      const step = Math.max(evenGap, MIN_NODE_GAP)
      const totalWidth = step * (unitCount - 1)
      const startX = W / 2 - totalWidth / 2

      const unitKeyFor = (k) => {
        if (familyUnits.childToSpouse[k]) return familyUnits.childToSpouse[k]
        if (familyUnits.rootChildren?.has?.(k)) return '__unknownChildren__'
        return null
      }

      let accumulatedGap = 0
      orderedKeys.forEach((key, i) => {
        if (gNum > 0 && familyUnits.spouseOrder.length > 0 && i > 0) {
          const prevUnit = unitKeyFor(orderedKeys[i - 1])
          const curUnit = unitKeyFor(key)
          if (prevUnit && curUnit && prevUnit !== curUnit) accumulatedGap += step
        }
        positions[key] = { x: startX + i * step + accumulatedGap, y }
      })
    }

    // Place non-family nodes (orgs/events) in a horizontal row below the family tree,
    // ordered by the x position of their closest connected family node.
    const maxFamilyY = Math.max(...Object.values(positions).map((p) => p.y))
    const orgRowY = maxFamilyY + ROW_HEIGHT

    const nonFamilyItems = []
    cy.value.nodes().forEach((node) => {
      const key = node.data('qid')
      if (familyKeys.has(key)) return
      let refX = W / 2
      node.connectedEdges().forEach((edge) => {
        const other = edge.source().data('qid') === key
          ? edge.target().data('qid')
          : edge.source().data('qid')
        if (positions[other]) refX = positions[other].x
      })
      nonFamilyItems.push({ key, refX, entityType: node.data('entityType') })
    })
    // Sort: organizations first, then other entity types; within each group sort by refX
    nonFamilyItems.sort((a, b) => {
      const aIsOrg = a.entityType === 'organization'
      const bIsOrg = b.entityType === 'organization'
      if (aIsOrg !== bIsOrg) return aIsOrg ? -1 : 1
      return a.refX - b.refX
    })
    // Split non-family items into persons (likely relatives) and orgs for separate rows
    const nonFamilyPersons = nonFamilyItems.filter((item) => item.entityType === 'person')
    const nonFamilyOrgs = nonFamilyItems.filter((item) => item.entityType !== 'person')

    const NON_FAMILY_PERSON_CAP = 8
    const NON_FAMILY_ORG_CAP = 8
    const NON_FAMILY_GAP = MIN_NODE_GAP  // must clear node diameter (max 70px) + label width

    // Place relative persons in a row below family tree
    const visiblePersons = nonFamilyPersons.slice(0, NON_FAMILY_PERSON_CAP)
    nonFamilyPersons.slice(NON_FAMILY_PERSON_CAP).forEach(({ key }) => {
      cy.value.$id(key)?.addClass('layout-hidden').hide()
    })
    if (visiblePersons.length > 0) {
      const pStep = Math.max((W * (1 - 2 * MARGIN)) / (visiblePersons.length + 1), NON_FAMILY_GAP)
      const pTotalW = pStep * (visiblePersons.length - 1)
      const pStartX = W / 2 - pTotalW / 2
      visiblePersons.forEach(({ key }, i) => {
        positions[key] = { x: pStartX + i * pStep, y: orgRowY }
      })
    }

    // Place orgs in a row below relative persons
    const orgRowY2 = visiblePersons.length > 0 ? orgRowY + ROW_HEIGHT : orgRowY
    const visibleOrgs = nonFamilyOrgs.slice(0, NON_FAMILY_ORG_CAP)
    nonFamilyOrgs.slice(NON_FAMILY_ORG_CAP).forEach(({ key }) => {
      cy.value.$id(key)?.addClass('layout-hidden').hide()
    })
    if (visibleOrgs.length > 0) {
      // A single eight-item row dominates fit() on phones and makes the family
      // portraits unreadably small. Wrap secondary entities while preserving
      // the same items and their sorted order.
      const columns = W <= 768 ? Math.min(4, visibleOrgs.length) : visibleOrgs.length
      visibleOrgs.forEach(({ key }, i) => {
        const row = Math.floor(i / columns)
        const rowStart = row * columns
        const itemsInRow = Math.min(columns, visibleOrgs.length - rowStart)
        const col = i - rowStart
        const rowWidth = NON_FAMILY_GAP * (itemsInRow - 1)
        positions[key] = {
          x: W / 2 - rowWidth / 2 + col * NON_FAMILY_GAP,
          y: orgRowY2 + row * ROW_HEIGHT,
        }
      })
    }

    // Resolve overlapping positions — row-aware sweep that nudges until no pair overlaps.
    // Group nodes by row, sort each row by x, then push apart any pair closer than MIN_NODE_GAP.
    // Repeat until stable or max passes reached.
    const rowGroups = {}  // rounded y → [{ key, pos }]
    for (const [key, pos] of Object.entries(positions)) {
      const rowKey = Math.round(pos.y)
      if (!rowGroups[rowKey]) rowGroups[rowKey] = []
      rowGroups[rowKey].push({ key, pos })
    }
    const sweepOverlap = () => {
      for (let pass = 0; pass < 10; pass++) {
        let anyNudged = false
        for (const row of Object.values(rowGroups)) {
          if (row.length < 2) continue
          row.sort((a, b) => a.pos.x - b.pos.x)
          for (let i = 0; i < row.length - 1; i++) {
            const gap = row[i + 1].pos.x - row[i].pos.x
            if (gap < MIN_NODE_GAP) {
              const nudge = (MIN_NODE_GAP - gap) / 2 + 1
              row[i].pos.x -= nudge
              row[i + 1].pos.x += nudge
              anyNudged = true
            }
          }
        }
        if (!anyNudged) break
      }
    }
    sweepOverlap()

    // Re-center each row around W/2 after nudging shifted positions off-center.
    // Skipped for children rows (gen > 0) when spouses exist — those rows are
    // about to be re-anchored under each spouse's x below, and re-centering
    // would flatten that alignment back into an even distribution.
    const childRowYs = new Set()
    if (familyUnits.spouseOrder.length > 0) {
      for (const [g, keys] of Object.entries(byGen)) {
        if (Number(g) > 0 && keys.length > 0) {
          childRowYs.add(Math.round(positions[keys[0]]?.y))
        }
      }
    }
    for (const [rowKey, row] of Object.entries(rowGroups)) {
      if (row.length === 0) continue
      if (childRowYs.has(Number(rowKey))) continue
      const minX = Math.min(...row.map((r) => r.pos.x))
      const maxX = Math.max(...row.map((r) => r.pos.x))
      const rowCenter = (minX + maxX) / 2
      const shift = W / 2 - rowCenter
      for (const r of row) r.pos.x += shift
    }

    // Anchor each child-unit group under the x of the spouse that anchors it
    // (or root's x for unknown-spouse children). Units are placed sequentially
    // by anchor order with a MIN_NODE_GAP between them — centering each on its
    // spouse when possible, but pushing outward when spouses sit too close for
    // both unit spans to fit under their anchors. Within-unit spacing from the
    // initial placement is preserved, so kids of the same spouse stay together.
    if (familyUnits.spouseOrder.length > 0) {
      const rootX = positions[rootQid]?.x ?? W / 2
      for (const [g, keys] of Object.entries(byGen)) {
        if (Number(g) <= 0) continue
        const byUnit = new Map()
        for (const k of keys) {
          const unit = familyUnits.childToSpouse[k] || '__unknown__'
          if (!byUnit.has(unit)) byUnit.set(unit, [])
          byUnit.get(unit).push(k)
        }
        const units = []
        for (const [unit, groupKeys] of byUnit) {
          if (groupKeys.length === 0) continue
          groupKeys.sort((a, b) => positions[a].x - positions[b].x)
          const xs = groupKeys.map((k) => positions[k].x)
          const minX = Math.min(...xs)
          const maxX = Math.max(...xs)
          const anchor = unit === '__unknown__' ? rootX : (positions[unit]?.x ?? rootX)
          units.push({ unit, groupKeys, minX, maxX, width: maxX - minX, anchor })
        }
        units.sort((a, b) => a.anchor - b.anchor)
        // Left-to-right pass — each unit starts at max(ideal, prevEnd + gap),
        // so units never overlap and each sits as close to its anchor as the
        // previous unit allows.
        let prevEnd = -Infinity
        for (const u of units) {
          const ideal = u.anchor - u.width / 2
          u.placedMin = Math.max(ideal, prevEnd + MIN_NODE_GAP)
          prevEnd = u.placedMin + u.width
        }
        for (const u of units) {
          const shift = u.placedMin - u.minX
          if (Math.abs(shift) < 0.5) continue
          for (const k of u.groupKeys) positions[k].x += shift
        }
      }
      sweepOverlap()
    }

    // Hide any node that has no assigned position so it never lands at the center fallback.
    // This catches edge cases where a node slipped through all placement logic.
    cy.value.nodes().forEach((node) => {
      if (node.hasClass('ring-circle')) return
      if (!positions[node.data('qid')]) node.addClass('layout-hidden').hide()
    })

    cy.value.layout({
      name: 'preset',
      positions: (node) => positions[node.data('qid')] || { x: W / 2, y: H / 2 },
      animate: true,
      animationDuration: 600,
      fit: true,
      padding: W <= 768 ? 16 : 40,
      stop: () => {
        _adjustLabels()
        if (W <= 768) {
          // Run after Cytoscape commits the layout's final fit; applying zoom
          // synchronously in `stop` can be overwritten by that last fit frame.
          window.requestAnimationFrame(() => {
            const root = cy.value?.$id(rootQid)
            const renderedWidth = root?.renderedWidth?.() || 0
            if (renderedWidth > 0 && renderedWidth < 44) {
              // Start at a legible root size; users can still pan to secondary
              // organizations and pinch back out to the complete network.
              const boost = Math.min(2.2, 44 / renderedWidth)
              cy.value.center(root)
              cy.value.zoom({
                level: Math.min(5, cy.value.zoom() * boost),
                renderedPosition: { x: W / 2, y: H / 2 },
              })
            }
          })
        }
      },
    }).run()

    _hideSameGenFamilyEdges()
    applyRelationshipBorders(rootQid, rawEdges, familyUnits, generations)
  }

  // Hide same-generation edges (spouse, sibling) in family mode — they're
  // redundant with the colored borders and would only add visual noise.
  // Cross-generation edges (parent↔child) are kept so lineage remains
  // traceable. No-op outside family mode.
  //
  // Consolidated here because applyFilters and applyVisualSettings both call
  // cy.edges().show() at various points and need to re-hide after, and the
  // original duplicates had already drifted (one skipped already-hidden edges
  // as an optimization, the other didn't).
  function _hideSameGenFamilyEdges() {
    if (!cy.value) return
    if (layoutMode.value !== 'family' || !_familyGenerations) return
    const gens = _familyGenerations
    cy.value.edges().forEach((edge) => {
      if (edge.hidden()) return
      // Spouse edges are shown explicitly even in family mode — the colored
      // ring border already encodes the family unit, but an explicit edge is
      // still useful to see who is connected to whom.
      if (edge.data('type') === 'spouse') return
      const srcKey = edge.data('source')
      const tgtKey = edge.data('target')
      if (!(srcKey in gens) || !(tgtKey in gens)) return
      if (gens[srcKey] === gens[tgtKey]) edge.hide()
    })
  }

  function _shouldUseOrgCircleLayout(rootQid, vertices) {
    const rootVertex = vertices.find((v) => (v._key || v._id?.split('/')[1]) === rootQid)
    return rootVertex && _entityType(rootVertex) === 'organization'
  }

  // Maps edge types to human-readable role label keys for ring annotations
  const ROLE_LABEL_MAP = {
    founder: 'founders',
    owner: 'founders',
    chief_executive: 'directors',
    chairperson: 'directors',
    board_member: 'directors',
    // everything else falls to 'members'
  }

  // Role accent colors for org-circle mode — fixed, load-bearing visual encoding
  const ROLE_CIRCLE_COLORS = {
    founders:  { border: '#c08020', bg: '#a06818' },  // amber
    directors: { border: '#2e8a7a', bg: '#256b5e' },  // teal
  }

  function _removeRingNodes() {
    if (!cy.value) return
    cy.value.nodes('.ring-circle').remove()
    cy.value.elements().removeClass('layout-hidden').show()
    orgCircleRolesPresent.value = new Set()
    // Reset per-node inline styles applied by applyOrgCircleLayout
    cy.value.nodes().forEach((node) => {
      const origSize = node.data('_origSize')
      if (origSize !== undefined) {
        node.data('size', origSize)
        node.removeData('_origSize')
        node.style({ width: origSize, height: origSize })
      }
      node.removeStyle('border-color border-width background-color text-valign text-halign text-margin-y text-margin-x text-max-width font-size color text-outline-width text-outline-color text-rotation text-wrap text-opacity shape label width height')
      node.scratch('_roleBorder', null)
    })
  }

  // Concentric ring layout: org at center, members in rings by statementsCount,
  // with ring labels annotated by the dominant role types found in each ring.
  function applyOrgCircleLayout(rootQid) {
    _removeRingNodes()

    const container = cy.value.container()
    const W = container.clientWidth
    const H = container.clientHeight
    const cx = W / 2
    const cy_ = H / 2

    // Build member→edge info: count of connections and all edge types to this org
    const memberEdges = new Map() // qid → { count, types: [] }
    cy.value.edges().forEach((edge) => {
      const src = edge.data('source')
      const tgt = edge.data('target')
      const memberQid = src === rootQid ? tgt : (tgt === rootQid ? src : null)
      if (!memberQid) return
      if (!memberEdges.has(memberQid)) memberEdges.set(memberQid, { count: 0, types: [] })
      const info = memberEdges.get(memberQid)
      info.count++
      info.types.push(edge.data('type'))
    })

    // Collect members sorted by: 1) edge count to org (desc), 2) statementsCount (desc)
    // Only include persons DIRECTLY connected to the org — multi-hop neighbors are not members.
    const members = []
    cy.value.nodes().forEach((node) => {
      const qid = node.data('qid')
      if (qid === rootQid) return
      if (node.data('entityType') !== 'person') return
      const edgeInfo = memberEdges.get(qid)
      if (!edgeInfo) return  // not directly connected to this org — skip
      members.push({
        qid,
        edgeCount: edgeInfo.count,
        statementsCount: node.data('statementsCount') || 0,
        edgeTypes: edgeInfo.types,
      })
    })
    members.sort((a, b) => b.edgeCount - a.edgeCount || b.statementsCount - a.statementsCount)

    // Compute dominant role group per member — founders beats directors beats plain members
    const ROLE_PRIORITY = { founders: 0, directors: 1, members: 2 }
    const memberRoles = new Map()
    members.forEach((m) => {
      let best = null
      for (const et of m.edgeTypes) {
        const role = ROLE_LABEL_MAP[et] || 'members'
        if (best === null || ROLE_PRIORITY[role] < ROLE_PRIORITY[best]) best = role
      }
      if (best && best !== 'members') memberRoles.set(m.qid, best)
    })

    // Track which role groups are actually present for the legend
    const presentRoles = new Set(['members'])
    for (const role of memberRoles.values()) presentRoles.add(role)
    orgCircleRolesPresent.value = presentRoles

    // Hide non-person nodes and persons not directly connected to the org.
    // Multi-hop persons land at center with no edges — they'd just pile up invisibly.
    cy.value.nodes().forEach((node) => {
      if (node.data('qid') === rootQid) return
      if (node.data('entityType') !== 'person') { node.addClass('layout-hidden').hide(); return }
      if (!memberEdges.has(node.data('qid'))) node.addClass('layout-hidden').hide()
    })

    // Split members into capacity-limited rings
    function getRingCapacities(total) {
      const MAX_PER_RING = 50
      const caps = [5, 8, 10]
      let sum = 23
      let last = 10
      while (sum < total) {
        last = Math.min(Math.round(last * 1.35), MAX_PER_RING)
        caps.push(last)
        sum += last
      }
      return caps
    }

    const ringCaps = getRingCapacities(members.length)
    const rings = []
    let idx = 0
    for (const cap of ringCaps) {
      if (idx >= members.length) break
      rings.push(members.slice(idx, idx + cap))
      idx += cap
    }

    // Derive ring label from dominant role types in each ring
    const ringLabelTranslations = {
      founders: t.value.ui.ringFounders || 'Founders',
      directors: t.value.ui.ringDirectors || 'Directors',
      members: t.value.ui.ringMembers || 'Members',
    }
    function deriveRingLabel(ring) {
      const roleCounts = {}
      for (const m of ring) {
        for (const et of m.edgeTypes) {
          const labelKey = ROLE_LABEL_MAP[et] || 'members'
          roleCounts[labelKey] = (roleCounts[labelKey] || 0) + 1
        }
      }
      // Pick the most common role in this ring
      let best = 'members'
      let bestCount = 0
      for (const [key, count] of Object.entries(roleCounts)) {
        if (count > bestCount) { best = key; bestCount = count }
      }
      return ringLabelTranslations[best] || best
    }

    const RING_GAP = Math.min(150, Math.max(92, Math.min(W, H) / 7))
    const RING_PADDING = 20
    const totalRings = rings.length

    // Pre-compute scaled node sizes so ring radii can be sized to clear them.
    // Previously radii were a fixed multiple of RING_GAP, which let first-ring
    // nodes (scaled up 1.2×) overlap the larger center org node.
    const memberSize = new Map()
    rings.forEach((ring, ringIdx) => {
      const scale = totalRings <= 1 ? 1.15 : 1.32 - (ringIdx / (totalRings - 1)) * 0.25
      for (const m of ring) {
        const node = cy.value.$id(m.qid)
        const baseSize = node.data('_origSize') ?? node.data('size') ?? 40
        memberSize.set(m.qid, Math.max(44, Math.round(baseSize * scale * 1.2)))
      }
    })

    const rootOrigSize = cy.value.$id(rootQid).data('size') || 60
    const rootSize = Math.max(rootOrigSize, 96)
    const rootRadius = rootSize / 2

    // Compute per-ring radius that clears (a) the previous ring's outer edge
    // and (b) the chord clearance needed for the members on this ring. Without
    // the chord check, a densely populated inner ring squeezes its nodes into
    // each other; without the outer-edge check they overlap the center.
    const ringRadii = []
    let prevOuter = rootRadius
    rings.forEach((ring, ringIdx) => {
      const maxSize = Math.max(...ring.map((m) => memberSize.get(m.qid)))
      const nodeRadius = maxSize / 2
      const minClearCenter = prevOuter + nodeRadius + RING_PADDING
      const angleStep = (2 * Math.PI) / Math.max(ring.length, 2)
      const chordMin = ring.length > 1
        ? (maxSize + RING_PADDING) / (2 * Math.sin(angleStep / 2))
        : 0
      const radius = Math.max(minClearCenter, chordMin, RING_GAP * (ringIdx + 1))
      ringRadii.push(radius)
      prevOuter = radius + nodeRadius
    })

    // Build position map
    const positions = {}
    const nodeRingIndex = new Map()

    positions[rootQid] = { x: cx, y: cy_ }

    rings.forEach((ring, ringIdx) => {
      const radius = ringRadii[ringIdx]
      const angleStep = (2 * Math.PI) / ring.length
      const startAngle = -Math.PI / 2
      ring.forEach((member, i) => {
        const angle = startAngle + i * angleStep
        positions[member.qid] = {
          x: cx + radius * Math.cos(angle),
          y: cy_ + radius * Math.sin(angle),
        }
        nodeRingIndex.set(member.qid, ringIdx)
      })
    })

    // Adjust node sizes, label directions, and role colors per ring member
    cy.value.nodes().forEach((node) => {
      const qid = node.data('qid')
      if (qid === rootQid) return
      const ringIdx = nodeRingIndex.get(qid)
      if (ringIdx === undefined) return

      // Save original size once so _removeRingNodes can restore it
      if (node.data('_origSize') === undefined) node.data('_origSize', node.data('size'))
      const newSize = memberSize.get(qid)
      node.data('size', newSize)

      // Radiate labels outward: top half → label below, bottom half → label above
      // This prevents member labels from overlapping the center org node name
      const pos = positions[qid]
      const dy = pos ? pos.y - cy_ : 0
      const textValign = dy < -5 ? 'top' : 'bottom'
      const textMarginY = textValign === 'top' ? -8 : 8

      const styleUpdate = { width: newSize, height: newSize, 'text-valign': textValign, 'text-margin-y': textMarginY }

      // Role-based accent colors: border for all nodes, bg only when no photo
      const roleGroup = memberRoles.get(qid)
      const roleColors = roleGroup ? ROLE_CIRCLE_COLORS[roleGroup] : null
      if (roleColors) {
        styleUpdate['border-color'] = roleColors.border
        styleUpdate['border-width'] = 3.5
        if (!node.data('hasImage')) styleUpdate['background-color'] = roleColors.bg
        node.scratch('_roleBorder', { width: 3.5, color: roleColors.border })
      }
      node.style(styleUpdate)
    })

    // Style root org node: label below to avoid overlap with ring members
    const rootNode = cy.value.$id(rootQid)
    if (rootNode.length) {
      const origSize = rootNode.data('size')
      const rootSize = Math.max(origSize, 96)
      rootNode.data('size', rootSize)
      rootNode.style({
        width: rootSize,
        height: rootSize,
        'text-valign': 'bottom',
        'text-halign': 'center',
        'text-margin-y': 8,
        'text-max-width': `${rootSize + 20}px`,
        'font-size': '12px',
        color: '#1c1510',
        'text-outline-width': 3,
      })
    }

    // Run layout
    cy.value.layout({
      name: 'preset',
      positions: (node) => positions[node.data('qid')] || { x: cx, y: cy_ },
      animate: true,
      animationDuration: 600,
      fit: true,
      // Use more of the board on phones while retaining breathing room around
      // the outer ring and its labels on larger screens.
      padding: Math.max(14, Math.min(28, Math.round(Math.min(W, H) * 0.035))),
      stop: _adjustLabels,
    }).run()

    // Add ring circle visual nodes — only for rings that actually have members
    const ringElements = []
    let lastRingLabel = null
    for (let i = 0; i < rings.length; i++) {
      if (rings[i].length === 0) continue
      // Verify at least one member in this ring has a position (is actually placed)
      const hasPlacedMember = rings[i].some((m) => positions[m.qid])
      if (!hasPlacedMember) continue
      const label = deriveRingLabel(rings[i])
      // Suppress repeated labels on adjacent rings — only show label on first ring of each type
      const displayLabel = label !== lastRingLabel ? label : ''
      lastRingLabel = label
      ringElements.push({
        group: 'nodes',
        data: {
          id: `__ring_${i}`,
          ringSize: ringRadii[i] * 2,
          ringLabel: displayLabel,
          color: 'transparent',
        },
        position: { x: cx, y: cy_ },
        classes: 'ring-circle',
      })
    }
    if (ringElements.length > 0) cy.value.add(ringElements)

    // In circle mode: hide edges and their labels — ring position encodes the relationship
    cy.value.edges().hide()
    cy.value.edges().forEach((e) => e.style('label', ''))
    // Lock positions — ring layout is deterministic; dragging breaks the visual encoding
    cy.value.nodes().ungrabify()
  }

  // Detect whether a root vertex is a parliament org by name.
  // Used by loadNeighborhood short-circuit and by App.vue computed.
  function _isParliamentName(name) {
    return /parliament|congress|senate|assembl/i.test(name || '')
  }

  /**
   * Load and render a parliament hemicycle layout.
   *
   * Fetches /api/parliament/hemicycle/{orgQid}, computes the Westminster bench
   * positions, and populates the Cytoscape canvas with:
   *   - person dot nodes (13 graph-unit circles, party-colored border)
   *   - party org square nodes (14×14, grey fill, party-colored border, label above)
   *   - a decorative aisle rectangle in graph space
   *
   * The canvas must be cleared before calling this function (the short-circuit
   * in loadNeighborhood handles that). After this function returns,
   * layoutMode === 'parliament' and graphVersion has been bumped.
   *
   * Node-drop defence: batches cy.add() in chunks of 100 to avoid Cytoscape's
   * silent drop bug that was observed in the previous implementation with very
   * large add() calls. A console.assert verifies all person nodes are present.
   */
  async function loadParliamentLayout(orgQid) {
    // Every caller gets the same initialization barrier. In particular, Feed
    // unmounts GraphCanvas; entering an entity immediately after switching back
    // must wait for Cytoscape lazy chunk and the new canvas instance.
    await whenReady()
    loading.value = true
    parliamentParties.value = []
    try {
      const data = await api.getParliamentHemicycle(orgQid)
      const rawMembers = data.members ?? []
      if (rawMembers.length === 0) return

      // Speaker sits at the center chair — exclude from bench seat grid.
      const SPEAKER_QID = 'Q689660'
      const speakerMember = rawMembers.find(m => m.qid === SPEAKER_QID)
      const seatingMembers = rawMembers.filter(m => m.qid !== SPEAKER_QID)

      // Keep threshold=1 so every real party is its own bucket (no merging into Other).
      // Only true independents end up in the synthetic __independent__ bucket.
      const orderedParties = groupByParty(seatingMembers, 1)

      const HEMI_OPTS = { aisleX: 490, aisleHalf: 28, topY: 60, rowH: 16, seatW: 15, rows: 14, dotR: 7 }
      const result = computeWestminster(orderedParties, HEMI_OPTS)

      // Bench midpoints in graph space — converted to screen coords after layout runs
      const _benchModelX = {
        govX: result.govAisleX - (result.govCols * HEMI_OPTS.seatW) / 2,
        oppX: result.oppAisleX + (result.oppCols * HEMI_OPTS.seatW) / 2,
        topY: HEMI_OPTS.topY,
      }

      const elements = []
      const positions = {}
      const seenQids = new Set()

      // Decorative aisle divider — lives in graph space so it tracks zoom/pan
      const aisleTop    = result.seats.length > 0 ? (Math.min(...result.seats.map(s => s.y)) - 8) : 52
      const aisleBottom = result.height - 8
      const aisleCenterY = (aisleTop + aisleBottom) / 2
      const aisleNodeId = '__parliament_aisle__'
      elements.push({
        group: 'nodes',
        data: { id: aisleNodeId, qid: aisleNodeId, aisleHeight: aisleBottom - aisleTop },
        position: { x: 490, y: aisleCenterY },
        classes: 'parliament-aisle',
      })
      positions[aisleNodeId] = { x: 490, y: aisleCenterY }

      // Speaker node — placed at center between party squares and first bench row
      if (speakerMember) {
        elements.push({
          group: 'nodes',
          data: {
            id: SPEAKER_QID,
            qid: SPEAKER_QID,
            entityType: 'person',
            label: 'Speaker',
            fullLabel: speakerMember.name || SPEAKER_QID,
            name_en: speakerMember.name,
            size: 13,
            color: '#bf6b28',
            hasImage: speakerMember.image ? true : undefined,
            imageUrl: speakerMember.image ? api.imageUrl(SPEAKER_QID) : undefined,
            statementsCount: 0,
            occupations: [],
            _isSpeaker: true,
          },
        })
        positions[SPEAKER_QID] = { x: 490, y: 42 }
      }

      // Accumulate per-party X centroids for placing the party square markers
      const partyXSums   = {}
      const partyXCounts = {}

      for (const seat of result.seats) {
        const qid = seat.member.qid
        if (seenQids.has(qid)) continue
        seenQids.add(qid)

        const pqid = seat.party.qid
        partyXSums[pqid]   = (partyXSums[pqid]   || 0) + seat.x
        partyXCounts[pqid] = (partyXCounts[pqid]  || 0) + 1

        const hasImage = !!seat.member.image
        const isNonSitting = seat.party.nonSitting === true
        elements.push({
          group: 'nodes',
          data: {
            id: qid,
            qid,
            entityType: 'person',
            // Short name for tooltip only — labels hidden in parliament mode
            label: '',
            fullLabel: seat.member.name || qid,
            name_en: seat.member.name,
            size: 13,
            color: '#bf6b28',  // person node colour from design tokens
            hasImage: hasImage || undefined,
            imageUrl: hasImage ? api.imageUrl(qid) : undefined,
            statementsCount: 0,
            occupations: [],
            _partyQid:    seat.party.qid,
            _partyColor:  seat.party.dotColor,
            _nonSitting:  isNonSitting || undefined,
          },
        })
        positions[qid] = { x: seat.x, y: seat.y }
      }

      // --- Party square markers ---
      // Squares are ~14×14 graph units, grey background, party-colored border.
      // Placed above the seat area (y ≈ 22) at the horizontal centroid of each
      // party's seats. De-overlapped with a two-pass L→R push / R→L clamp sweep.

      const AISLE_X    = 490
      const SQ_HALF    = 7     // half-width of each square (12/2 = 6, use 7 for gap)
      const MIN_GAP    = 4     // minimum gap between squares
      const MIN_SPACING = 2 * SQ_HALF + MIN_GAP
      const MARGIN     = 10   // inset from canvas edge and from aisle

      const partyItems = orderedParties
        .filter(p => partyXCounts[p.qid])
        .map(p => ({ party: p, origX: partyXSums[p.qid] / partyXCounts[p.qid] }))

      const govItems = partyItems.filter(p => p.origX < AISLE_X).sort((a, b) => a.origX - b.origX)
      const oppItems = partyItems.filter(p => p.origX >= AISLE_X).sort((a, b) => a.origX - b.origX)

      function resolvePositions(items, xMin, xMax) {
        const safeMin = xMin + SQ_HALF
        const safeMax = xMax - SQ_HALF

        items.forEach(item => {
          item.finalX = Math.max(safeMin, Math.min(safeMax, item.origX))
        })
        // L→R push
        for (let k = 1; k < items.length; k++) {
          if (items[k].finalX - items[k - 1].finalX < MIN_SPACING) {
            items[k].finalX = items[k - 1].finalX + MIN_SPACING
          }
        }
        // R→L clamp
        for (let k = items.length - 1; k >= 0; k--) {
          if (items[k].finalX > safeMax) items[k].finalX = safeMax
          if (k > 0 && items[k].finalX - items[k - 1].finalX < MIN_SPACING) {
            items[k - 1].finalX = items[k].finalX - MIN_SPACING
          }
        }
        items.forEach(item => {
          if (item.finalX < safeMin) item.finalX = safeMin
        })
      }

      // Government bench: xMax = aisleX - MARGIN - aisle half-width
      const govXMax = AISLE_X - MARGIN - 28  // 28 = aisleHalf from computeWestminster
      resolvePositions(govItems, MARGIN, govXMax)

      // Opposition bench: xMin = aisleX + MARGIN + aisle half-width
      const oppXMin = AISLE_X + MARGIN + 28
      resolvePositions(oppItems, oppXMin, 980 - MARGIN)

      const SQUARE_Y = 22  // well above topY=60, plenty of clearance

      for (const item of [...govItems, ...oppItems]) {
        // Only add real Wikidata org nodes — skip synthetic buckets
        if (!item.party.qid.startsWith('Q')) continue
        elements.push({
          group: 'nodes',
          data: {
            id:          item.party.qid,
            qid:         item.party.qid,
            entityType:  'organization',
            label:       item.party.name,
            fullLabel:   item.party.name,
            name_en:     item.party.name,
            size:        14,
            color:       '#8a8278',  // org node colour from design tokens
            statementsCount: 1,
            occupations: [],
            _partyColor: item.party.color,
            _isSynthetic: false,
          },
        })
        positions[item.party.qid] = { x: item.finalX ?? AISLE_X, y: SQUARE_Y }
        // Ghost name node — rotated label above the square, no margin math needed
        const nameId = `${item.party.qid}__name`
        elements.push({
          group: 'nodes',
          data: { id: nameId, qid: nameId, entityType: '_pname', label: item.party.name, size: 1 },
        })
        positions[nameId] = { x: item.finalX ?? AISLE_X, y: SQUARE_Y - 28 }
      }

      // --- Add nodes in batches of 100 to avoid the Cytoscape silent-drop bug ---
      // The previous implementation added all ~670 nodes in one cy.add() call,
      // which silently dropped the last 20 nodes of large parties.
      // Batching in chunks of 100 prevents this.
      const BATCH_SIZE = 100
      cy.value.elements().remove()
      _rawEdges.value = []
      for (let i = 0; i < elements.length; i += BATCH_SIZE) {
        cy.value.add(elements.slice(i, i + BATCH_SIZE))
      }

      // Verify all expected person nodes are present
      const expectedPersonCount = seenQids.size
      const actualPersonCount = cy.value.nodes('[entityType="person"]').length
      console.assert(
        actualPersonCount === expectedPersonCount,
        `Parliament node drop detected! Expected ${expectedPersonCount} person nodes, got ${actualPersonCount}`
      )
      if (actualPersonCount !== expectedPersonCount) {
        console.warn(`[parliament] Missing ${expectedPersonCount - actualPersonCount} person nodes — retrying with single add()`)
        // Retry: remove and re-add all in one call (may work if batching caused the issue)
        cy.value.elements().remove()
        cy.value.add(elements)
      }

      // Now layoutMode is set — after add(elements) so applyVisualSettings
      // (called from onEnterNode before the graphVersion watcher fires) still
      // runs its full loop with the correct mode.
      layoutMode.value = 'parliament'
      _familyGenerations = null
      // rootNodeId is already set by loadNeighborhood short-circuit (or by the
      // caller in toggleHemicycle context). Do NOT set it here — it would
      // re-trigger the rootNodeId watcher in App.vue and reset showHemicycle.

      // Apply inline styles BEFORE running the preset layout so that fit:true
      // uses the actual square dimensions rather than the default data(size).
      // This prevents the leftmost square from being clipped outside the fitted viewport.
      cy.value.nodes().ungrabify()
      cy.value.nodes().forEach((node) => {
        const partyColor = node.data('_partyColor')
        if (node.data('entityType') === 'organization') {
          // Square party marker: grey fill, party-colored border, label above
          node.style({
            width: 12,
            height: 12,
            shape: 'rectangle',
            'background-color': '#8a8278',
            'text-opacity': 0,  // ghost _pname node carries the visible label
          })
          if (partyColor) {
            node.style({ 'border-color': partyColor, 'border-width': 2 })
            node.scratch('_partyBorder', { color: partyColor, width: 2 })
          }
        } else if (node.data('entityType') === '_pname') {
          // Ghost party-name label — rotated -90° (reads bottom-to-top), centered on node position.
          // Positioned 28 units above the square so no margin math interacts with rotation.
          node.style({
            'background-opacity': 0,
            'border-width': 0,
            width: 1,
            height: 1,
            'text-valign': 'center',
            'text-halign': 'center',
            'text-rotation': -1.5708,
            'font-size': '6.5px',
            'font-weight': 700,
            color: 'rgba(28,21,16,0.82)',
            'text-outline-width': 2,
            'text-outline-color': '#f2ede3',
          })
        } else if (node.data('_isSpeaker')) {
          // Speaker — same size as other person dots, neutral grey, "Speaker" label below
          node.style({
            'background-color': '#8a8278',
            'background-opacity': 0.55,
            'border-width': 0,
            'text-opacity': 1,
            'text-valign': 'bottom',
            'text-margin-y': 4,
            'text-halign': 'center',
            'font-size': '7px',
            'font-weight': 700,
            color: 'rgba(28,21,16,0.5)',
            'text-outline-width': 2,
            'text-outline-color': '#f2ede3',
          })
        } else if (node.hasClass('parliament-aisle')) {
          // Aisle: already styled via Cytoscape stylesheet
        } else {
          // Person dot: hide label, apply party-colored border
          node.style('text-opacity', 0)
          if (node.data('_nonSitting')) {
            // Non-sitting MPs (Sinn Féin): hollow dot, muted opacity
            node.style({
              'background-opacity': 0.15,
              'border-width': 1,
              'border-opacity': 0.45,
            })
            if (partyColor) node.style({ 'border-color': partyColor })
          } else if (partyColor) {
            node.style({ 'border-color': partyColor, 'border-width': 1.5 })
            node.scratch('_partyBorder', { color: partyColor, width: 1.5 })
          }
        }
      })

      cy.value.layout({
        name: 'preset',
        positions: (node) => positions[node.data('qid')] || { x: AISLE_X, y: 240 },
        animate: false,
        fit: true,
        padding: 52,
      }).run()

      // Convert bench midpoints to screen pixels NOW — fit:true has been applied,
      // so modelToRenderedPosition gives the correct coords for the overlay labels.
      parliamentBenchCenters.value = _benchModelX

      parliamentParties.value = orderedParties
      graphVersion.value++
    } finally {
      loading.value = false
    }
  }

  async function loadNeighborhood(qid, params = {}, mergeWithExisting = false, preloadedData = null) {
    // Await the current canvas lifecycle before graph mutation.
    await whenReady()
    loading.value = true
    if (!mergeWithExisting) rootNodeId.value = qid
    try {
      const data = preloadedData || await api.getNeighborhood(qid, params)
      // Detect family layout BEFORE filtering nulls — orphaned family edges shouldn't
      // prevent the family-tree layout from triggering for valid family connections.
      const hasFamilyEdgesRaw = (data.edges || []).some((e) => e && FAMILY_EDGE_TYPES.has(e.type))

      // Parliament short-circuit: if this is a parliament/congress/senate/assembly org,
      // clear the canvas and bump graphVersion so the App.vue watcher fires
      // loadParliamentLayout — this avoids any flash of the org-circle layout.
      // Only trigger on a fresh non-merge load with no user override.
      // Short-circuit only when in auto-detect mode (no user override).
      // When the user clicks "network" or "org-circle" button on a parliament org,
      // _layoutModeOverride is set and we skip the parliament path.
      if (!mergeWithExisting && !preloadedData && _layoutModeOverride.value === null) {
        const _preRootVertex = (data.vertices || []).find((v) => (v._key || v._id?.split('/')[1]) === qid)
        const _preRootName = _preRootVertex?.name_en || ''
        const _preIsOrg = _preRootVertex && _entityType(_preRootVertex) === 'organization'
        if (_preIsOrg && _isParliamentName(_preRootName)) {
          // Clear canvas so org-circle never flashes
          if (cy.value) {
            cy.value.elements().remove()
            _rawEdges.value = []
          }
          layoutMode.value = 'parliament'
          graphVersion.value++
          return
        }
      }

      // Filter out null entries — ArangoDB returns null vertex when edge target was deleted
      data.vertices = (data.vertices || []).filter(Boolean)
      const validIds = new Set(data.vertices.map((v) => v._id))
      data.edges = (data.edges || []).filter((e) => e && validIds.has(e._from) && validIds.has(e._to))
      if (!data.vertices.length) return

      const rootVertex = data.vertices.find((v) => (v._key || v._id?.split('/')[1]) === qid)
      const rootIsPersonAndHasFamily = rootVertex && _entityType(rootVertex) === 'person' && hasFamilyEdgesRaw
      const isOrgRoot = _shouldUseOrgCircleLayout(qid, data.vertices)
      // Respect user override; otherwise auto-detect
      const willUseOrgCircle = !mergeWithExisting && isOrgRoot &&
        (_layoutModeOverride.value === 'org-circle' || (_layoutModeOverride.value === null))

      // In family layout mode, exclude events — they're not relevant to family trees
      const willUseFamilyLayout = !willUseOrgCircle && (mergeWithExisting
        ? _rawEdges.value.some((e) => FAMILY_EDGE_TYPES.has(e.type))
        : rootIsPersonAndHasFamily)

      let vertices = data.vertices
      let edges = data.edges
      if (willUseFamilyLayout) {
        vertices = data.vertices.filter((v) => !v._id?.startsWith('events/'))
        edges = data.edges.filter((e) => !e._from?.startsWith('events/') && !e._to?.startsWith('events/'))
      } else if (willUseOrgCircle) {
        // In org-circle mode, suppress family edges and inter-member non-org edges.
        // Keep only edges that connect to the org node (spokes).
        const orgId = `organizations/${qid}`
        edges = data.edges.filter((e) => {
          return e._from === orgId || e._to === orgId
        })
      }

      const elements = buildElements(vertices, edges, null, qid, willUseFamilyLayout ? ORG_MEMBER_CAP_FAMILY : ORG_MEMBER_CAP)

      let addedNodeIds = null
      if (mergeWithExisting && cy.value.elements().length > 0) {
        // Additive expand: only add new nodes/edges
        const existingIds = new Set()
        cy.value.elements().forEach((el) => existingIds.add(el.id()))
        const newEls = elements.filter((el) => !existingIds.has(el.data.id))
        if (newEls.length > 0) cy.value.add(newEls)
        probeOrgLogos()
        addedNodeIds = new Set(
          newEls.filter((el) => el.group !== 'edges' && !el.data.source).map((el) => el.data.id)
        )
        // Merge raw edges (deduplicate by key)
        const existingEdgeKeys = new Set(
          _rawEdges.value.map((e) => e._key || `${e._from}|${e._to}|${e.type}`)
        )
        data.edges.forEach((e) => {
          const k = e._key || `${e._from}|${e._to}|${e.type}`
          if (!existingEdgeKeys.has(k)) _rawEdges.value.push(e)
        })
      } else {
        cy.value.elements().remove()
        _rawEdges.value = data.edges
        cy.value.add(elements)
        probeOrgLogos()
      }

      if (willUseOrgCircle) {
        layoutMode.value = 'org-circle'
        _familyGenerations = null
        applyOrgCircleLayout(rootNodeId.value)
      } else {
        _removeRingNodes()
        if (willUseFamilyLayout && mergeWithExisting) {
          // Expand on a family-root view: preserve existing family positions.
          // Place only the newly added nodes in a small circle around the
          // expanded node. Never re-run applyFamilyLayout here — it would
          // apply its 8-org cap and hide most new neighbors.
          if (addedNodeIds && addedNodeIds.size > 0) {
            const anchor = cy.value.$id(qid)
            const anchorPos = anchor && anchor.length ? anchor.position() : { x: 0, y: 0 }
            const n = addedNodeIds.size
            const radius = Math.max(140, 18 * n)
            let i = 0
            addedNodeIds.forEach((id) => {
              const angle = (i / n) * 2 * Math.PI
              const node = cy.value.$id(id)
              if (node && node.length) {
                node.position({
                  x: anchorPos.x + radius * Math.cos(angle),
                  y: anchorPos.y + radius * Math.sin(angle),
                })
              }
              i++
            })
            _adjustLabels()
          }
        } else if (willUseFamilyLayout) {
          cy.value.nodes().grabify()
          layoutMode.value = 'family'
          applyFamilyLayout(rootNodeId.value, _rawEdges.value)
        } else {
          cy.value.nodes().grabify()
          layoutMode.value = 'network'
          _familyGenerations = null
          const netOpts = getLayoutOptions(cy.value.nodes().not('.ring-circle').length)
          netOpts.stop = _adjustLabels
          cy.value.layout(netOpts).run()
          // Apply family relationship borders even in network mode when family edges exist
          if (_rawEdges.value.some((e) => FAMILY_EDGE_TYPES.has(e.type))) {
            const familyUnits = computeFamilyUnits(rootNodeId.value, _rawEdges.value)
            const generations = computeGenerations(rootNodeId.value, _rawEdges.value)
            applyRelationshipBorders(rootNodeId.value, _rawEdges.value, familyUnits, generations)
          }
        }
      }
      // Mark the root node with a visible ring so it stands out on the canvas
      cy.value.elements().removeClass('root-node')
      if (rootNodeId.value) cy.value.$id(rootNodeId.value).addClass('root-node')

      if (_activeEdgeTypes.value || _activeNodeTypes.value) {
        applyFilters(_activeEdgeTypes.value, _activeNodeTypes.value)
      }
      graphVersion.value++
    } finally {
      loading.value = false
    }
  }

  async function loadPath(qid1, qid2) {
    // Await the current canvas lifecycle before graph mutation.
    await whenReady()
    loading.value = true
    try {
      const data = await api.getPath(qid1, qid2)
      if (!data.found) return data
      data.vertices = (data.vertices || []).filter(Boolean)
      const validPathIds = new Set(data.vertices.map((v) => v._id))
      data.edges = (data.edges || []).filter((e) => e && validPathIds.has(e._from) && validPathIds.has(e._to))
      const elements = buildElements(data.vertices, data.edges)
      cy.value.elements().remove()
      cy.value.add(elements)
      probeOrgLogos()
      const layout = cy.value.layout({
        name: 'breadthfirst',
        directed: false,
        spacingFactor: 1.5,
        animate: true,
        stop: _adjustLabels,
      })
      layout.run()
      if (_activeEdgeTypes.value || _activeNodeTypes.value) {
        applyFilters(_activeEdgeTypes.value, _activeNodeTypes.value)
      }
      graphVersion.value++
      return data
    } finally {
      loading.value = false
    }
  }

  async function loadCluster(edgeType, limit) {
    // Await the current canvas lifecycle before graph mutation.
    await whenReady()
    loading.value = true
    try {
      const data = await api.getCluster(edgeType, { limit })
      if (!data.vertices?.length) return
      data.vertices = data.vertices.filter(Boolean)
      const validClusterIds = new Set(data.vertices.map((v) => v._id))
      data.edges = (data.edges || []).filter((e) => e && validClusterIds.has(e._from) && validClusterIds.has(e._to))
      const elements = buildElements(data.vertices, data.edges)
      cy.value.elements().remove()
      cy.value.add(elements)
      probeOrgLogos()
      const nodeCount = elements.filter((el) => el.group === 'nodes').length
      const clusterOpts = getLayoutOptions(nodeCount)
      clusterOpts.stop = _adjustLabels
      cy.value.layout(clusterOpts).run()
    } finally {
      loading.value = false
    }
  }

  function applyFilters(activeEdgeTypes, activeNodeTypes) {
    _activeEdgeTypes.value = activeEdgeTypes
    _activeNodeTypes.value = activeNodeTypes
    if (!cy.value) return
    // Parliament mode has no edges and controls visibility directly — skip filter logic.
    if (layoutMode.value === 'parliament') return
    // Step 1: show everything EXCEPT layout-hidden nodes (those hidden by
    // applyFamilyLayout / applyOrgCircleLayout for overflow control).
    cy.value.elements().removeClass('dimmed').removeClass('highlighted')
    cy.value.elements().not('.layout-hidden').show()
    cy.value.elements('.layout-hidden').hide()
    // Step 2: hide nodes not in activeNodeTypes (skip ring decoration nodes)
    if (activeNodeTypes) {
      cy.value.nodes().forEach((node) => {
        if (node.hasClass('ring-circle')) return
        const et = node.data('entityType')
        if (et && !activeNodeTypes.has(et)) node.hide()
      })
    }
    // Step 3: hide edges not in activeEdgeTypes or touching a hidden node
    cy.value.edges().forEach((edge) => {
      if (!activeEdgeTypes.has(edge.data('type'))) {
        edge.hide()
        return
      }
      if (edge.source().hidden() || edge.target().hidden()) edge.hide()
    })
    // Step 3b: Step 1's show() undid family-mode edge hiding — reapply.
    _hideSameGenFamilyEdges()
    // Step 4: hide orphaned nodes (no visible edges), keep root visible
    cy.value.nodes().forEach((node) => {
      if (node.hidden()) return
      if (node.hasClass('ring-circle')) return
      if (node.data('qid') === rootNodeId.value) return
      const hasVisibleEdge = node.connectedEdges().some((e) => !e.hidden())
      if (!hasVisibleEdge) node.hide()
    })
    _adjustLabels()
  }

  function applyEdgeFilter(activeTypes) {
    applyFilters(activeTypes, null)
  }

  let _cachedSettings = null

  function applyVisualSettings(settings) {
    _cachedSettings = settings
    if (!cy.value) return
    // Parliament mode manages its own visibility — skip generic visual settings.
    if (layoutMode.value === 'parliament') return
    const inCircleMode = layoutMode.value === 'org-circle'
    cy.value.edges().forEach((e) => {
      e.style('label', (!inCircleMode && settings.showEdgeLabels) ? e.data('label') : '')
    })
    cy.value.nodes().forEach((n) => {
      n.style('text-opacity', settings.showNodeLabels ? 1 : 0)
    })
    if (settings.showEdges === false || inCircleMode) {
      cy.value.edges().hide()
    } else {
      cy.value.edges().show()
      // show() undid family-mode edge hiding — reapply.
      _hideSameGenFamilyEdges()
      // Re-apply edge type filter if active (show() also undid type-based hiding)
      if (_activeEdgeTypes.value) {
        cy.value.edges().forEach((edge) => {
          if (edge.hidden()) return
          if (!_activeEdgeTypes.value.has(edge.data('type'))) edge.hide()
        })
      }
    }
    focusOnHover.value = settings.focusOnHover !== false
  }

  function toggleOrgMembers(orgQid, cap) {
    if (cap) {
      _expandedOrgs.value.set(orgQid, cap)
    } else {
      _expandedOrgs.value.delete(orgQid)
    }
  }

  function orgMemberLimit(orgQid) {
    return _expandedOrgs.value.get(orgQid) || 0
  }

  function destroy() {
    initGeneration++
    initPromise = null
    const instance = cy.value
    cy.value = null
    if (instance && !instance.destroyed()) instance.destroy()
  }

  // Org logo quality: probe each visible org logo once (cached per session) and
  // drop the ones that would render as black slabs or unreadable banner crops.
  const _logoProbed = new Set()
  async function probeOrgLogos() {
    if (!cy.value) return
    const nodes = cy.value.nodes('node[entityType="organization"]')
    for (const node of nodes) {
      const url = node.data('logoProbeUrl')
      if (!url || _logoProbed.has(url)) continue
      _logoProbed.add(url)
      const ok = await probeLogo(url)
      if (!ok && node.inside()) {
        node.data({ hasImage: null, imageUrl: null })
      }
    }
  }

  function restyle(themeOpts) {
    _cachedThemeOpts = themeOpts || {}
    if (!cy.value) return
    cy.value.style(getCytoscapeStyle(api.imageUrl, _cachedThemeOpts))
    if (layoutMode.value !== 'parliament' && layoutMode.value !== 'org-circle') {
      cy.value.nodes().not('.ring-circle').removeStyle('width height shape')
    }
    _adjustLabels()
  }

  // Re-apply node labels and edge labels after a locale change
  function relabelNodes() {
    if (!cy.value) return
    cy.value.nodes().forEach((node) => {
      const nameIt = node.data('name_it')
      const nameEn = node.data('name_en')
      const key = node.data('qid')
      const name = prefName({ name_it: nameIt, name_en: nameEn }) || key
      node.data('fullLabel', name)
      node.data('label', _truncateLabel(name))
    })
    cy.value.edges().forEach((edge) => {
      edge.data('label', translateEdgeType(edge.data('type'), t.value.edgeTypes))
    })
    _adjustLabels()
  }

  function resetState() {
    _rawEdges.value = []
    activeFamilyColors.value = {}
    layoutMode.value = 'network'
    _layoutModeOverride.value = null
    rootNodeId.value = null
    parliamentParties.value = []
  }

  return {
    cy,
    loading,
    selectedNode,
    selectedEdge,
    rootNodeId,
    graphVersion,
    focusOnHover,
    layoutMode,
    _layoutModeOverride,
    init,
    whenReady,
    loadNeighborhood,
    loadPath,
    loadCluster,
    loadParliamentLayout,
    applyFilters,
    applyEdgeFilter,
    applyVisualSettings,
    destroy,
    buildElements,
    toggleOrgMembers,
    orgMemberLimit,
    restyle,
    relabelNodes,
    resetState,
    hasFamilyEdges,
    activeFamilyColors,
    orgCircleRolesPresent,
    parliamentParties,
    parliamentBenchCenters,
  }
}
