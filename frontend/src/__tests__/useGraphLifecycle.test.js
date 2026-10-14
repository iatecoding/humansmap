import { beforeEach, describe, expect, it, vi } from 'vitest'

const fakeCy = {
  on: vi.fn(),
  destroyed: vi.fn(() => false),
}
const cytoscape = vi.fn(() => fakeCy)
cytoscape.use = vi.fn()

vi.mock('cytoscape', () => ({ default: cytoscape }))
vi.mock('cytoscape-cose-bilkent', () => ({ default: {} }))
vi.mock('../composables/useApi.js', () => ({
  useApi: () => ({
    imageUrl: vi.fn(),
    getNeighborhood: vi.fn(),
  }),
}))
vi.mock('../composables/useLocale.js', () => ({
  useLocale: () => ({
    t: { value: { edgeTypes: {} } },
    prefName: vi.fn((entity) => entity?.name_en),
  }),
}))

import { useGraph } from '../composables/useGraph.js'

describe('useGraph initialization lifecycle', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    fakeCy.destroyed.mockReturnValue(false)
  })

  it('fails explicitly when navigation starts without a mounted canvas', async () => {
    const graph = useGraph()

    await expect(graph.whenReady()).rejects.toThrow('Graph canvas is not mounted')
    await expect(graph.loadNeighborhood('Q1')).rejects.toThrow('Graph canvas is not mounted')
  })

  it('shares the lazy initialization barrier with entity navigation', async () => {
    const graph = useGraph()
    const container = {
      addEventListener: vi.fn(),
      getBoundingClientRect: vi.fn(() => ({ left: 0, top: 0 })),
    }

    const initializing = graph.init(container)
    const ready = graph.whenReady()

    await expect(ready).resolves.toBe(fakeCy)
    await expect(initializing).resolves.toBe(fakeCy)
    expect(cytoscape).toHaveBeenCalledOnce()
  })

  it('rejects readiness after an in-flight mount is invalidated', async () => {
    const graph = useGraph()
    const container = {
      addEventListener: vi.fn(),
      getBoundingClientRect: vi.fn(() => ({ left: 0, top: 0 })),
    }

    graph.init(container)
    graph.destroy()

    await expect(graph.whenReady()).rejects.toThrow('Graph canvas is not mounted')
  })
})
