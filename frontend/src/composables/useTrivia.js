import { ref, computed } from 'vue'
import { useApi } from './useApi.js'
import { useLocale } from './useLocale.js'

const NAT_CACHE_KEY = 'hm_nationalities'

function _loadNationalitiesFromStorage() {
  try {
    const raw = localStorage.getItem(NAT_CACHE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch { return null }
}

function _saveNationalitiesToStorage(list) {
  try { localStorage.setItem(NAT_CACHE_KEY, JSON.stringify(list)) } catch {}
}

// Module-level cache — seeded from localStorage so it's available immediately
let _nationalitiesCache = _loadNationalitiesFromStorage()
let _nationalitiesFetching = null

const BOOKMARK_KEY = 'hm_bookmarks'

function _loadBookmarks() {
  try {
    const raw = localStorage.getItem(BOOKMARK_KEY)
    return raw ? JSON.parse(raw) : []
  } catch { return [] }
}

function _saveBookmarks(list) {
  try { localStorage.setItem(BOOKMARK_KEY, JSON.stringify(list)) } catch {}
}

// Module-level bookmarks ref — shared across all useTrivia() calls
const _bookmarks = ref(_loadBookmarks())

export function useTrivia() {
  const api = useApi()
  const { locale } = useLocale()

  const round = ref(null)
  const currentIndex = ref(0)
  // answers: [{correct, points, type, questionIndex}]
  const answers = ref([])
  const bookmarks = _bookmarks // [{qid, name_it, name_en, image_cached_path}]
  const loading = ref(false)
  const error = ref(null)
  // phase: 'idle' | 'playing' | 'reveal' | 'summary'
  const phase = ref('idle')

  // The answer for the current question (used during reveal phase)
  const lastAnswer = ref(null) // {correct, points}

  const currentQuestion = computed(() =>
    round.value?.questions?.[currentIndex.value] ?? null
  )

  const totalQuestions = computed(() => round.value?.questions?.length ?? 0)

  const score = computed(() => answers.value.reduce((s, a) => s + (a.points || 0), 0))

  const correctCount = computed(() => answers.value.filter((a) => a.correct).length)

  const maxScore = computed(() => {
    if (!round.value) return 13
    return round.value.questions.reduce((s, q) => s + (q.type === 'chi_e' ? 4 : 1), 0)
  })

  async function startRound(category, nationality, living, difficulty) {
    loading.value = true
    error.value = null
    round.value = null
    currentIndex.value = 0
    answers.value = []
    lastAnswer.value = null
    phase.value = 'idle'
    try {
      round.value = await api.getTriviaRound(category || null, nationality || null, living != null ? living : null, difficulty || null)
      phase.value = 'playing'
    } catch (e) {
      error.value = e.message || 'error'
    } finally {
      loading.value = false
    }
  }

  function submitAnswer(correct, points) {
    lastAnswer.value = { correct, points }
    answers.value.push({
      correct,
      points,
      type: currentQuestion.value?.type,
      questionIndex: currentIndex.value,
    })
    phase.value = 'reveal'
  }

  function nextQuestion() {
    lastAnswer.value = null
    if (currentIndex.value < totalQuestions.value - 1) {
      currentIndex.value++
      phase.value = 'playing'
    } else {
      phase.value = 'summary'
    }
  }

  function toggleBookmark(person) {
    const idx = bookmarks.value.findIndex((b) => b.qid === person.qid)
    if (idx === -1) {
      bookmarks.value.push(person)
    } else {
      bookmarks.value.splice(idx, 1)
    }
    _saveBookmarks(bookmarks.value)
  }

  function isBookmarked(qid) {
    return bookmarks.value.some((b) => b.qid === qid)
  }

  function generateShareText() {
    if (!round.value) return ''
    const filters = round.value.filters || {}
    const params = new URLSearchParams()
    params.set('s', String(correctCount.value))
    params.set('t', String(totalQuestions.value))
    if (filters.nationality) params.set('nationality', filters.nationality)
    if (filters.category) params.set('category', filters.category)
    if (filters.difficulty) params.set('difficulty', filters.difficulty)
    params.set('lang', locale.value)
    const base = window.location.origin
    return `${base}/share?${params.toString()}`
  }

  async function fetchNationalities() {
    if (_nationalitiesCache) {
      // Refresh in background so cache stays fresh, but return immediately
      if (!_nationalitiesFetching) {
        _nationalitiesFetching = api.getTriviaNationalities()
          .then((data) => {
            _nationalitiesCache = data.nationalities || []
            _saveNationalitiesToStorage(_nationalitiesCache)
            _nationalitiesFetching = null
            return _nationalitiesCache
          })
          .catch(() => { _nationalitiesFetching = null })
      }
      return _nationalitiesCache
    }
    if (!_nationalitiesFetching) {
      _nationalitiesFetching = api.getTriviaNationalities()
        .then((data) => {
          _nationalitiesCache = data.nationalities || []
          _saveNationalitiesToStorage(_nationalitiesCache)
          _nationalitiesFetching = null
          return _nationalitiesCache
        })
        .catch(() => {
          _nationalitiesFetching = null
          return []
        })
    }
    return _nationalitiesFetching
  }

  function reset() {
    round.value = null
    currentIndex.value = 0
    answers.value = []
    lastAnswer.value = null
    error.value = null
    phase.value = 'idle'
  }

  return {
    // state
    round,
    currentIndex,
    answers,
    bookmarks,
    loading,
    error,
    phase,
    lastAnswer,
    // computed
    currentQuestion,
    totalQuestions,
    score,
    correctCount,
    maxScore,
    // actions
    startRound,
    submitAnswer,
    nextQuestion,
    toggleBookmark,
    isBookmarked,
    generateShareText,
    fetchNationalities,
    reset,
  }
}
