import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// ---------------------------------------------------------------------------
// Mock useApi so search() never makes real HTTP requests
// ---------------------------------------------------------------------------
const mockSearch = vi.fn()

vi.mock('../composables/useApi.js', () => ({
  useApi: () => ({
    search: mockSearch,
  }),
}))

import { useSearch } from '../composables/useSearch.js'

describe('useSearch', () => {
  beforeEach(() => {
    // Vitest 4 fakes more timer APIs by default than the composable uses, and
    // faking them breaks vitest's own afterEach hook timing (it reports a bogus
    // "Hook timed out"). Fake only what a debounce actually touches.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    mockSearch.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('initialises with empty state', () => {
    const { results, loading, query } = useSearch()
    expect(results.value).toEqual([])
    expect(loading.value).toBe(false)
    expect(query.value).toBe('')
  })

  it('clears results immediately for queries shorter than 2 chars', () => {
    const { results, loading, search } = useSearch()
    results.value = [{ id: '1', name: 'test' }]

    search('')
    expect(results.value).toEqual([])
    expect(loading.value).toBe(false)

    search('a')
    expect(results.value).toEqual([])
    expect(loading.value).toBe(false)
  })

  it('sets query.value immediately on every call', () => {
    const { query, search } = useSearch()
    search('ma')
    expect(query.value).toBe('ma')
    search('mar')
    expect(query.value).toBe('mar')
  })

  it('does not call the API before the 300 ms debounce elapses', () => {
    const { search } = useSearch()
    search('mario')
    expect(mockSearch).not.toHaveBeenCalled()
  })

  it('calls the API exactly once after 300 ms', async () => {
    mockSearch.mockResolvedValue({ items: [{ id: 'Q1', name: 'Mario Rossi' }] })

    const { search, results } = useSearch()
    search('mario')

    vi.advanceTimersByTime(300)
    // flush the async api call
    await Promise.resolve()
    await Promise.resolve()

    expect(mockSearch).toHaveBeenCalledOnce()
    expect(mockSearch).toHaveBeenCalledWith('mario', 10)
    expect(results.value).toEqual([{ id: 'Q1', name: 'Mario Rossi' }])
  })

  it('debounces: rapid successive calls only trigger one API request', async () => {
    mockSearch.mockResolvedValue({ items: [] })

    const { search } = useSearch()
    search('m')
    search('ma')
    search('mar')
    search('mari')
    search('mario')

    vi.advanceTimersByTime(300)
    await Promise.resolve()
    await Promise.resolve()

    // Only the last call should have gone through
    expect(mockSearch).toHaveBeenCalledOnce()
    expect(mockSearch).toHaveBeenCalledWith('mario', 10)
  })

  it('ignores stale responses when a newer query is in flight', async () => {
    // Simulate two in-flight calls; the second resolves first
    let resolveFirst
    const firstPromise = new Promise((res) => { resolveFirst = res })
    mockSearch
      .mockReturnValueOnce(firstPromise)
      .mockResolvedValueOnce({ items: [{ id: 'Q2', name: 'Marco' }] })

    const { search, results } = useSearch()

    // Fire first query
    search('mar')
    vi.advanceTimersByTime(300)
    await Promise.resolve()

    // Fire second query before first resolves
    search('marco')
    vi.advanceTimersByTime(300)
    await Promise.resolve()
    await Promise.resolve()

    // Resolve the stale first call — it should be discarded
    resolveFirst({ items: [{ id: 'Q99', name: 'Stale result' }] })
    await Promise.resolve()

    // Results should reflect the second (newer) query only
    expect(results.value).toEqual([{ id: 'Q2', name: 'Marco' }])
  })

  it('sets results to [] on API error', async () => {
    mockSearch.mockRejectedValue(new Error('network error'))

    const { search, results, loading } = useSearch()
    search('error')

    vi.advanceTimersByTime(300)
    await Promise.resolve()
    await Promise.resolve()

    expect(results.value).toEqual([])
    expect(loading.value).toBe(false)
  })
})
