const BASE = '/api'

async function request(path, params = {}, { signal } = {}) {
  const url = new URL(path, window.location.origin)
  for (const [k, v] of Object.entries(params)) {
    if (v != null) url.searchParams.set(k, v)
  }
  const res = await fetch(url, { signal })
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`)
  return res.json()
}

export function useApi() {
  return {
    getPersons: (params) => request(`${BASE}/persons`, params),
    getPerson: (qid) => request(`${BASE}/persons/${qid}`),
    getConnections: (qid, type) => request(`${BASE}/persons/${qid}/connections`, { type }),
    getNeighborhood: (qid, params) => request(`${BASE}/graph/neighborhood/${qid}`, params),
    getPath: (qid1, qid2) => request(`${BASE}/graph/path/${qid1}/${qid2}`),
    getCluster: (edgeType, params) => request(`${BASE}/graph/cluster/${edgeType}`, params),
    search: (q, limit) => request(`${BASE}/search`, { q, limit }),
    getEdgeTypes: () => request(`${BASE}/edge-types`),
    getStats: () => request(`${BASE}/stats`),
    imageUrl: (qid) => `${BASE}/images/${qid}.jpg`,
    getChallenge: (nationality) => request(`${BASE}/game/challenge`, nationality ? { nationality } : {}),
    revealChallenge: (id) => request(`${BASE}/game/challenge/${id}/reveal`),
    getGameNationalities: () => request(`${BASE}/game/nationalities`),
    getTriviaRound: (category, nationality, living, difficulty) => request(`${BASE}/trivia/round`, { category, nationality, living, difficulty }),
    getTriviaCategories: () => Promise.resolve({ categories: Object.keys({ sport: 1, politics: 1, music: 1, film_tv: 1, arts_culture: 1, science: 1, business: 1 }) }),
    getTriviaNationalities: () => request(`${BASE}/trivia/nationalities`),
    getFeatured: (lang, limit) => request(`${BASE}/featured`, { lang, limit }),
    getLeaderboard: (nationality, status, category, sort_by, limit, offset = 0, signal) => request(`${BASE}/leaderboard/degree`, { nationality, status, category, sort_by, limit, offset }, { signal }),
    getOrgLeaderboard: (category, sort_by, limit, offset = 0, signal) => request(`${BASE}/leaderboard/orgs`, { category, sort_by, limit, offset, image_revision: '20260913b' }, { signal }),
    getOrgCategories: () => request(`${BASE}/leaderboard/orgs/categories`),
    getOrgMembers: (qid, params) => request(`${BASE}/graph/org-members/${qid}`, params),
    getOccupationGraph: (occupation, limit) => request(`${BASE}/graph/occupation/${occupation}`, { limit }),
    getPersonCategories: () => request(`${BASE}/leaderboard/persons/categories`),
    getOrganization: (qid) => request(`${BASE}/organizations/${qid}`),
    getFilings: (qid, params) => request(`${BASE}/organizations/${qid}/filings`, params),
    getPersonFilings: (qid, params) => request(`${BASE}/persons/${qid}/filings`, params),
    getEvent: (qid) => request(`${BASE}/events/${qid}`),
    getParliamentHemicycle: (qid) => request(`${BASE}/parliament/hemicycle/${qid}`),
    // Feed — GET /api/feed?cursor=0&limit=20&lang=en[&categories=politics,business]
    // Returns { cards: [...], next_cursor: int|null, total: int }
    // 503 if the Redis pool hasn't been generated yet.
    // categories: string[] | string | null — comma-joined when non-empty array passed.
    getFeed: ({ cursor = 0, limit = 20, lang, categories, seed, nationality, types } = {}) => {
      const params = { cursor, limit }
      if (lang) params.lang = lang
      if (categories) {
        const cat = Array.isArray(categories) ? categories.join(',') : categories
        if (cat) params.categories = cat
      }
      if (seed != null) params.seed = seed
      if (nationality) params.nationality = nationality
      if (types) {
        const tp = Array.isArray(types) ? types.join(',') : types
        if (tp) params.types = tp
      }
      return request(`${BASE}/feed`, params)
    },
    // Single feed card by id — GET /api/feed/card/{id}
    // Returns one card object (same shape as an element of getFeed's `cards`).
    // 404 if the id is unknown, 503 if the feed pool isn't generated yet.
    getFeedCard: (id) => request(`${BASE}/feed/card/${id}`),
  }
}
