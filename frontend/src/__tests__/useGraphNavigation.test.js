import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref } from 'vue'

const mockLoadNeighborhood = vi.fn()
const mockApplyFilters = vi.fn()
const mockApplyVisualSettings = vi.fn()

vi.mock('../composables/useApi.js', () => ({
  useApi: () => ({}),
}))

vi.mock('../composables/useGraph.js', () => ({
  useGraph: () => ({
    cy: ref(null),
    selectedNode: ref(null),
    selectedEdge: ref(null),
    _layoutModeOverride: ref(null),
    rootNodeId: ref(null),
    loadNeighborhood: mockLoadNeighborhood,
    applyFilters: mockApplyFilters,
    applyVisualSettings: mockApplyVisualSettings,
  }),
}))

vi.mock('../utils/cytoscape-config.js', () => ({
  getEdgeColor: vi.fn(),
}))

import { useGraphNavigation } from '../composables/useGraphNavigation.js'

function createNav(routerOverrides = {}) {
  const graph = {
    cy: ref(null),
    selectedNode: ref(null),
    selectedEdge: ref(null),
    _layoutModeOverride: ref(null),
    rootNodeId: ref(null),
    loadNeighborhood: mockLoadNeighborhood,
    applyFilters: mockApplyFilters,
    applyVisualSettings: mockApplyVisualSettings,
  }

  const router = {
    push: vi.fn(),
    replace: vi.fn(),
    ...routerOverrides,
  }

  const nav = useGraphNavigation({
    graph,
    api: {},
    activeEdgeTypes: ref(new Set()),
    activeNodeTypes: ref(new Set(['person', 'organization'])),
    edgeTypes: ref([]),
    graphLoaded: ref(false),
    vizSettings: {},
    rootPersonName: ref(null),
    router,
  })

  return { nav, graph, router }
}

describe('useGraphNavigation – history management', () => {
  beforeEach(() => {
    mockLoadNeighborhood.mockResolvedValue()
    mockApplyFilters.mockReturnValue()
    mockApplyVisualSettings.mockReturnValue()
  })

  it('uses a bounded one-hop graph for interactive navigation', async () => {
    const { nav } = createNav()
    await nav.onEnterNode('Q22686')
    expect(mockLoadNeighborhood).toHaveBeenCalledWith('Q22686', {
      node_types: 'person,organization',
      depth: 1,
      limit: 250,
    })
  })

  describe('onPersonSelect', () => {
    it('calls router.push by default', async () => {
      const { nav, router } = createNav()
      await nav.onPersonSelect({ wikidata_id: 'Q111', name_en: 'Person A' })
      expect(router.push).toHaveBeenCalledWith('/person/Q111')
      expect(router.replace).not.toHaveBeenCalled()
    })

    it('skips URL update when push=false (deep-link / popstate)', async () => {
      const { nav, router } = createNav()
      await nav.onPersonSelect({ wikidata_id: 'Q111' }, { push: false })
      expect(router.push).not.toHaveBeenCalled()
      expect(router.replace).not.toHaveBeenCalled()
    })
  })

  describe('onEnterNode', () => {
    it('calls router.push by default', async () => {
      const { nav, router } = createNav()
      await nav.onEnterNode('Q222')
      expect(router.push).toHaveBeenCalledWith('/person/Q222')
      expect(router.replace).not.toHaveBeenCalled()
    })

    it('skips URL update when push=false', async () => {
      const { nav, router } = createNav()
      await nav.onEnterNode('Q222', { push: false })
      expect(router.push).not.toHaveBeenCalled()
      expect(router.replace).not.toHaveBeenCalled()
    })
  })

  describe('sequential navigation builds history', () => {
    it('push is called for each forward navigation', async () => {
      const { nav, router } = createNav()

      await nav.onPersonSelect({ wikidata_id: 'Q111', name_en: 'A' })
      expect(router.push).toHaveBeenCalledTimes(1)
      expect(router.push).toHaveBeenLastCalledWith('/person/Q111')

      await nav.onEnterNode('Q222')
      expect(router.push).toHaveBeenCalledTimes(2)
      expect(router.push).toHaveBeenLastCalledWith('/person/Q222')

      await nav.onEnterNode('Q333')
      expect(router.push).toHaveBeenCalledTimes(3)
      expect(router.push).toHaveBeenLastCalledWith('/person/Q333')
    })

    it('back navigation (push=false) does not call push or replace', async () => {
      const { nav, router } = createNav()

      await nav.onPersonSelect({ wikidata_id: 'Q111' })
      await nav.onEnterNode('Q222')
      router.push.mockClear()
      router.replace.mockClear()

      await nav.onEnterNode('Q111', { push: false })
      expect(router.push).not.toHaveBeenCalled()
      expect(router.replace).not.toHaveBeenCalled()
    })
  })
})
