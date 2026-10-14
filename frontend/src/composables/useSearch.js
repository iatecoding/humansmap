import { ref } from 'vue'
import { useApi } from './useApi.js'

// Occupation categories that have virtual graph nodes (occ_*).
// Keeps in sync with full_load.py OCCUPATION_CATEGORIES + trivia_common.py OCCUPATION_LABELS.
const OCCUPATION_CATEGORIES = [
  { id: 'politician',          en: 'politician',          it: 'politici' },
  { id: 'judge',               en: 'judge',               it: 'giudici' },
  { id: 'lawyer',              en: 'lawyer',              it: 'avvocati' },
  { id: 'businessperson',      en: 'businessperson',      it: 'imprenditori' },
  { id: 'journalist',          en: 'journalist',          it: 'giornalisti' },
  { id: 'actor',               en: 'actor',               it: 'attori' },
  { id: 'film_director',       en: 'film director',       it: 'registi' },
  { id: 'singer',              en: 'singer',              it: 'cantanti' },
  { id: 'composer',            en: 'composer',            it: 'compositori' },
  { id: 'painter',             en: 'painter',             it: 'pittori' },
  { id: 'sculptor',            en: 'sculptor',            it: 'scultori' },
  { id: 'writer',              en: 'writer',              it: 'scrittori' },
  { id: 'photographer',        en: 'photographer',        it: 'fotografi' },
  { id: 'military_person',     en: 'military person',     it: 'militari' },
  { id: 'criminal',            en: 'criminal',            it: 'criminali' },
  { id: 'football_player',     en: 'football player',     it: 'calciatori' },
  { id: 'cyclist',             en: 'cyclist',             it: 'ciclisti' },
  { id: 'tennis_player',       en: 'tennis player',       it: 'tennisti' },
  { id: 'racing_driver',       en: 'racing driver',       it: 'piloti automobilistici' },
  { id: 'basketball_player',   en: 'basketball player',   it: 'cestisti' },
  { id: 'alpine_skier',        en: 'alpine skier',        it: 'sciatori alpini' },
  { id: 'boxer',               en: 'boxer',               it: 'pugili' },
  { id: 'sprinter',            en: 'sprinter',            it: 'velocisti' },
  { id: 'football_manager',    en: 'football manager',    it: 'allenatori di calcio' },
  { id: 'water_polo_player',   en: 'water polo player',   it: 'pallanuotisti' },
  { id: 'swimmer',             en: 'swimmer',             it: 'nuotatori' },
]

function matchOccupations(q) {
  const norm = q.toLowerCase().trim()
  if (norm.length < 2) return []
  return OCCUPATION_CATEGORIES
    .filter((occ) => {
      const label = occ.en.toLowerCase()
      const idLabel = occ.id.replace(/_/g, ' ')
      return label.includes(norm) || norm.includes(label) || idLabel.includes(norm) || norm.includes(idLabel)
    })
    .slice(0, 3)
    .map((occ) => ({
      id: `occ_${occ.id}`,
      name_en: occ.en.charAt(0).toUpperCase() + occ.en.slice(1) + 's',
      name_it: occ.it.charAt(0).toUpperCase() + occ.it.slice(1),
      description_en: 'Category',
      description_it: 'Categoria',
      _isOccupation: true,
      occupation: occ.id,
    }))
}

export function useSearch() {
  const api = useApi()
  const results = ref([])
  const loading = ref(false)
  const query = ref('')
  let debounceTimer = null

  function search(q) {
    query.value = q
    clearTimeout(debounceTimer)
    if (!q || q.length < 2) {
      results.value = []
      return
    }
    loading.value = true
    debounceTimer = setTimeout(async () => {
      try {
        const occResults = matchOccupations(q)
        const data = await api.search(q, 10)
        if (q !== query.value) return
        results.value = [...occResults, ...data.items]
      } catch {
        results.value = []
      } finally {
        loading.value = false
      }
    }, 300)
  }

  return { results, loading, query, search }
}
