import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ replace: vi.fn() }),
}))

vi.mock('../composables/useApi.js', () => {
  const api = {
    getLeaderboard: vi.fn(async () => ({ items: [] })),
    getOrgLeaderboard: vi.fn(async () => ({ items: [] })),
    getGameNationalities: vi.fn(async () => ({ nationalities: [] })),
    getOrgCategories: vi.fn(async () => ({ categories: [] })),
    getPersonCategories: vi.fn(async () => ({ categories: [], occupations: [] })),
    imageUrl: vi.fn((qid) => `/img/${qid}`),
  }
  return { useApi: () => api }
})

vi.mock('../composables/useLocale.js', async () => {
  const { ref } = await import('vue')
  const labels = {
    leaderboard: {
      tabPersons: 'People',
      tabOrgs: 'Orgs',
      statusLiving: 'Living',
      statusDeceased: 'Deceased',
      statusAll: 'All',
      allNationalities: 'All nationalities',
      allCategories: 'All categories',
      allOrgs: 'All orgs',
      empty: 'No results',
      categories: {},
      personCategories: {},
      sortBy: 'Sort',
      sortDegree: 'Notability',
      sortBirth: 'Birth date',
      sortMembers: 'Members',
      sortFounded: 'Founded',
      connections: 'connections',
      members: 'members',
    },
    nationalities: {},
    occupations: {},
    orgTypes: {},
  }
  return { useLocale: () => ({ t: ref(labels), prefName: (e) => e?.name_en }) }
})

import Leaderboard from '../components/Leaderboard.vue'
import { useApi } from '../composables/useApi.js'
import {
  ORG_SORTS,
  PERSON_SORTS,
  isValidSort,
  sortLabelKey,
} from '../composables/leaderboardSorts.js'

const api = useApi()

describe('leaderboardSorts', () => {
  it('does not expose the slow nationality sort', () => {
    expect(PERSON_SORTS).not.toContain('nationality')
    expect(PERSON_SORTS).toEqual(['degree', 'birth_date'])
    expect(ORG_SORTS).toEqual(['members', 'founded'])
  })

  it('maps sort keys to i18n labels and validates per tab', () => {
    expect(sortLabelKey('birth_date')).toBe('sortBirth')
    expect(sortLabelKey('founded')).toBe('sortFounded')
    expect(isValidSort('persons', 'founded')).toBe(false)
    expect(isValidSort('orgs', 'founded')).toBe(true)
  })
})

describe('Leaderboard sort control', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('defaults to degree on load', async () => {
    const wrapper = mount(Leaderboard)
    await flushPromises()
    const args = api.getLeaderboard.mock.calls.at(-1)
    expect(args[3]).toBe('degree')
    wrapper.unmount()
  })

  it('reloads persons with the selected birth_date sort', async () => {
    const wrapper = mount(Leaderboard)
    await flushPromises()

    const options = wrapper.find('.lb-sort').findAll('option').map(o => o.element.value)
    expect(options).toEqual(PERSON_SORTS)

    await wrapper.find('.lb-sort').setValue('birth_date')
    await flushPromises()

    // Each reload must reach the API with the new sort (and a fresh signal).
    const args = api.getLeaderboard.mock.calls.at(-1)
    expect(args[3]).toBe('birth_date')
    wrapper.unmount()
  })

  it('mounts the org sort control and reloads with founded', async () => {
    const wrapper = mount(Leaderboard)
    await flushPromises()

    await wrapper.findAll('.lb-tab')[1].trigger('click')
    await flushPromises()

    const options = wrapper.find('.lb-sort').findAll('option').map(o => o.element.value)
    expect(options).toEqual(ORG_SORTS)

    await wrapper.find('.lb-sort').setValue('founded')
    await flushPromises()

    const args = api.getOrgLeaderboard.mock.calls.at(-1)
    expect(args[1]).toBe('founded')
    wrapper.unmount()
  })
})
