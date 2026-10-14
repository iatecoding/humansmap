import { useApi } from './useApi.js'
import { useGraph } from './useGraph.js'
import { getEdgeColor } from '../utils/cytoscape-config.js'
import { slugify } from '../utils/slug.js'

// COLLAPSED_EDGE_TYPES: inverse types merged into their canonical counterpart in the graph
const COLLAPSED_EDGE_TYPES = new Set(['child', 'employee', 'participant_in', 'victim'])

export { COLLAPSED_EDGE_TYPES }

/**
 * Encapsulates all graph-loading and navigation logic that was previously
 * inline in App.vue. Requires access to shared refs for activeEdgeTypes,
 * activeNodeTypes, edgeTypes, graphLoaded, vizSettings, rootPersonName.
 */
export function useGraphNavigation({
  graph,
  api,
  activeEdgeTypes,
  activeNodeTypes,
  edgeTypes,
  graphLoaded,
  vizSettings,
  rootPersonName,
  router,
}) {
  function nodeTypesParam() {
    // Initial exploration is intentionally one hop. Two-hop traversals through
    // hubs routinely hit 1,000 nodes and make otherwise valid entity links
    // appear frozen while Cytoscape lays them out.
    return {
      node_types: [...activeNodeTypes.value].join(','),
      depth: 1,
      limit: 250,
    }
  }

  function entityPath(qid) {
    const node = graph.cy.value?.$id(qid)
    const name = node?.data('name_en') || ''
    const slug = slugify(name)
    const entityType = node?.data("entityType")
    const prefix = entityType === "organization" ? "/organization" : "/person"
    return slug ? `${prefix}/${qid}-${slug}` : `${prefix}/${qid}`
  }

  // After loading a neighborhood, register any edge types that exist in the
  // graph but aren't yet in activeEdgeTypes (e.g. new types added after a
  // data reload).
  function syncNewEdgeTypes() {
    const cy = graph.cy.value
    if (!cy) return
    const newTypes = []
    const seenNew = new Set()
    cy.edges().forEach((e) => {
      const t = e.data('type')
      if (t && !activeEdgeTypes.value.has(t) && !COLLAPSED_EDGE_TYPES.has(t) && !seenNew.has(t)) {
        newTypes.push(t)
        seenNew.add(t)
      }
    })
    if (newTypes.length === 0) return
    newTypes.forEach((t) => getEdgeColor(t))
    const updated = new Set(activeEdgeTypes.value)
    newTypes.forEach((t) => updated.add(t))
    activeEdgeTypes.value = updated
    edgeTypes.value = [...edgeTypes.value, ...newTypes.map((t) => ({ type: t, count: 0 }))]
  }

  async function onPersonSelect(person, { push = true } = {}) {
    graph.selectedNode.value = null
    graph.selectedEdge.value = null
    graph._layoutModeOverride.value = null
    rootPersonName.value = person.name_it || person.name_en || null
    const qid = person.wikidata_id || person.id || person._key
    await graph.loadNeighborhood(qid, nodeTypesParam())
    if (!rootPersonName.value) {
      rootPersonName.value =
        graph.cy.value?.$id(qid)?.data('name_it') ||
        graph.cy.value?.$id(qid)?.data('name_en') ||
        null
    }
    syncNewEdgeTypes()
    graph.applyFilters(activeEdgeTypes.value, activeNodeTypes.value)
    graph.applyVisualSettings(vizSettings)
    graphLoaded.value = true
    if (qid && router && push) {
      router.push(entityPath(qid))
    }
  }

  async function onEnterNode(qid, { push = true } = {}) {
    graph.selectedNode.value = null
    graph._layoutModeOverride.value = null
    await graph.loadNeighborhood(qid, nodeTypesParam())
    rootPersonName.value =
      graph.cy.value?.$id(qid)?.data('name_it') ||
      graph.cy.value?.$id(qid)?.data('name_en') ||
      null
    syncNewEdgeTypes()
    graph.applyFilters(activeEdgeTypes.value, activeNodeTypes.value)
    graph.applyVisualSettings(vizSettings)
    graphLoaded.value = true
    if (qid && router && push) {
      router.push(entityPath(qid))
    }
  }

  async function onToggleOrgMembers(orgQid, cap) {
    graph.toggleOrgMembers(orgQid, cap)
    const isVirtualOcc = orgQid.startsWith('occ_')
    if (isVirtualOcc) {
      const occupation = orgQid.slice(4)
      const data = await api.getOccupationGraph(occupation, cap || undefined)
      await graph.loadNeighborhood(orgQid, {}, false, data)
    } else {
      const params = { ...nodeTypesParam() }
      if (cap) params.limit = cap
      await graph.loadNeighborhood(orgQid, params)
    }
    syncNewEdgeTypes()
    graph.applyFilters(activeEdgeTypes.value, activeNodeTypes.value)
    graph.applyVisualSettings(vizSettings)
    const orgNode = graph.cy.value?.$id(orgQid)
    if (orgNode) graph.selectedNode.value = orgNode.data()
  }

  function onFilterChange(newTypes) {
    activeEdgeTypes.value = newTypes
    graph.applyFilters(newTypes, activeNodeTypes.value)
  }

  async function onNodeTypeFilterChange(newNodeTypes) {
    activeNodeTypes.value = newNodeTypes
    if (graphLoaded.value && graph.rootNodeId.value) {
      await graph.loadNeighborhood(graph.rootNodeId.value, nodeTypesParam())
      graph.applyVisualSettings(vizSettings)
    }
    graph.applyFilters(activeEdgeTypes.value, newNodeTypes)
  }

  return {
    nodeTypesParam,
    syncNewEdgeTypes,
    onPersonSelect,
    onEnterNode,
    onToggleOrgMembers,
    onFilterChange,
    onNodeTypeFilterChange,
  }
}
