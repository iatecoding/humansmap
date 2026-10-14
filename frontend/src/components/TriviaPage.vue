<template>
  <div class="trivia-page">
    <!-- Loading -->
    <div v-if="trivia.loading.value" class="trivia-loading">
      <div class="spinner"></div>
      <p>{{ t.trivia.loading }}</p>
    </div>

    <!-- Error -->
    <div v-else-if="trivia.error.value" class="trivia-error">
      <p>{{ t.trivia.error }}</p>
      <button class="btn-retry" @click="trivia.reset()">↩ {{ t.trivia.reset }}</button>
    </div>

    <!-- Idle: filter bar + play button -->
    <div v-else-if="trivia.phase.value === 'idle'" class="trivia-idle">
      <div class="idle-header">
        <p class="idle-sub">{{ t.trivia.idleDesc }}</p>
      </div>

      <div class="filters">
        <!-- Difficulty -->
        <div class="filter-group" role="group" aria-labelledby="filter-label-difficulty">
          <span id="filter-label-difficulty" class="filter-label">{{ t.trivia.difficulty }}</span>
          <div class="filter-pills">
            <button
              class="filter-pill"
              :class="{ active: selectedDifficulty === 'easy' }"
              :aria-pressed="selectedDifficulty === 'easy'"
              @click="selectedDifficulty = 'easy'"
            >{{ t.trivia.diffEasy }}</button>
            <button
              class="filter-pill"
              :class="{ active: selectedDifficulty === '' }"
              :aria-pressed="selectedDifficulty === ''"
              @click="selectedDifficulty = ''"
            >{{ t.trivia.diffNormal }}</button>
            <button
              class="filter-pill"
              :class="{ active: selectedDifficulty === 'hard' }"
              :aria-pressed="selectedDifficulty === 'hard'"
              @click="selectedDifficulty = 'hard'"
            >{{ t.trivia.diffHard }}</button>
          </div>
        </div>

        <!-- Living / deceased -->
        <div class="filter-group" role="group" aria-labelledby="filter-label-living">
          <span id="filter-label-living" class="filter-label">{{ t.trivia.livingStatus }}</span>
          <div class="filter-pills">
            <button
              class="filter-pill"
              :class="{ active: selectedLiving === null }"
              :aria-pressed="selectedLiving === null"
              @click="selectedLiving = null"
            >{{ t.trivia.allPersons }}</button>
            <button
              class="filter-pill"
              :class="{ active: selectedLiving === true }"
              :aria-pressed="selectedLiving === true"
              @click="selectedLiving = true"
            >{{ t.trivia.livingOnly }}</button>
            <button
              class="filter-pill"
              :class="{ active: selectedLiving === false }"
              :aria-pressed="selectedLiving === false"
              @click="selectedLiving = false"
            >{{ t.trivia.deceasedOnly }}</button>
          </div>
        </div>

        <!-- Category -->
        <div class="filter-group" role="group" aria-labelledby="filter-label-category">
          <span id="filter-label-category" class="filter-label">{{ t.trivia.category }}</span>
          <div class="filter-pills">
            <button
              class="filter-pill"
              :class="{ active: selectedCategory === '' }"
              :aria-pressed="selectedCategory === ''"
              @click="selectedCategory = ''"
            >{{ t.trivia.allCategories }}</button>
            <button
              v-for="cat in categories"
              :key="cat"
              class="filter-pill"
              :class="{ active: selectedCategory === cat }"
              :aria-pressed="selectedCategory === cat"
              @click="selectedCategory = cat"
            >{{ categoryLabel(cat) }}</button>
          </div>
        </div>

        <!-- Nationality -->
        <div class="filter-group" role="group" aria-labelledby="filter-label-nationality">
          <span id="filter-label-nationality" class="filter-label">{{ t.trivia.nationality }}</span>
          <div class="filter-pills">
            <template v-if="loadingNationalities">
              <div class="nat-loading" role="status" :aria-label="t.trivia.loadingNationalities"></div>
            </template>
            <template v-else-if="availableNationalities.length > 1">
              <button
                class="filter-pill"
                :class="{ active: selectedNationality === '' }"
                :aria-pressed="selectedNationality === ''"
                @click="selectedNationality = ''"
              >{{ t.trivia.allNationalities }}</button>
              <button
                v-for="nat in availableNationalities"
                :key="nat"
                class="filter-pill"
                :class="{ active: selectedNationality === nat }"
                :aria-pressed="selectedNationality === nat"
                @click="selectedNationality = nat"
              >{{ t.nationalities[nat] || nat }}</button>
            </template>
          </div>
        </div>
      </div>

      <button class="btn-play" @click="startRound">
        {{ t.trivia.play }} →
      </button>
    </div>

    <!-- Playing / Reveal -->
    <div v-else-if="trivia.phase.value === 'playing' || trivia.phase.value === 'reveal'" class="trivia-game">
      <!-- Progress bar -->
      <div class="progress-bar" role="progressbar" :aria-valuenow="progressPct" aria-valuemin="0" aria-valuemax="100">
        <div class="progress-fill" :style="{ transform: `scaleX(${progressPct / 100})` }"></div>
      </div>

      <!-- Progress info -->
      <div class="progress-info">
        <span class="question-counter">
          {{
            t.trivia.questionOf
              .replace('{n}', trivia.currentIndex.value + 1)
              .replace('{total}', trivia.totalQuestions.value)
          }}
        </span>
        <span class="score-chip">
          {{ trivia.score.value }} / {{ trivia.maxScore.value }}
          {{ t.trivia.points.replace('{n}', '').trim() }}
        </span>
      </div>

      <!-- Current question -->
      <div class="question-container">
        <component
          :is="questionComponent"
          :key="trivia.currentIndex.value"
          :question="trivia.currentQuestion.value"
          @answer="onAnswer"
        />
      </div>

      <!-- Reveal overlay -->
      <TriviaReveal
        v-if="trivia.phase.value === 'reveal'"
        :correct="trivia.lastAnswer.value?.correct"
        :points="trivia.lastAnswer.value?.points ?? 0"
        :question="trivia.currentQuestion.value"
        :persons="revealPersons"
        :isBookmarked="trivia.isBookmarked"
        @continue="trivia.nextQuestion()"
        @bookmark="trivia.toggleBookmark"
      />
    </div>

    <!-- Summary -->
    <div v-else-if="trivia.phase.value === 'summary'" class="trivia-summary-wrap">
      <TriviaSummary
        :answers="trivia.answers.value"
        :score="trivia.score.value"
        :maxScore="trivia.maxScore.value"
        :correctCount="trivia.correctCount.value"
        :totalQuestions="trivia.totalQuestions.value"
        :bookmarks="trivia.bookmarks.value"
        :shareText="trivia.generateShareText()"
        @playAgain="trivia.reset()"
        @explore="onExplore"
      />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, shallowRef } from 'vue'
import { useLocale } from '../composables/useLocale.js'
import { useTrivia } from '../composables/useTrivia.js'

import ChiE from './ChiE.vue'
import VeroFalso from './VeroFalso.vue'
import Intruso from './Intruso.vue'
import IndovinaChi from './IndovinaChi.vue'
import TriviaReveal from './TriviaReveal.vue'
import TriviaSummary from './TriviaSummary.vue'

const emit = defineEmits(['explore'])

const { t, locale } = useLocale()
const trivia = useTrivia()

const selectedCategory = ref('')
const selectedNationality = ref('')
const selectedLiving = ref(null)
const selectedDifficulty = ref('')
const availableNationalities = ref([])
const loadingNationalities = ref(true)

const categories = ['sport', 'politics', 'music', 'film_tv', 'arts_culture', 'science', 'business']

function categoryLabel(cat) {
  return t.value.trivia?.categories?.[cat] || cat
}

const QUESTION_COMPONENTS = {
  chi_e: ChiE,
  vero_falso: VeroFalso,
  intruso: Intruso,
  intruso_graph: Intruso,
  indovina_chi: IndovinaChi,
}

const questionComponent = computed(() => {
  const type = trivia.currentQuestion.value?.type
  return QUESTION_COMPONENTS[type] || null
})

const progressPct = computed(() => {
  const total = trivia.totalQuestions.value
  if (!total) return 0
  return ((trivia.currentIndex.value) / total) * 100
})

// Persons to show in the reveal overlay for bookmark
const revealPersons = computed(() => {
  const q = trivia.currentQuestion.value
  if (!q) return []
  if (q.type === 'chi_e') return q.person ? [q.person] : []
  if (q.type === 'vero_falso') {
    const persons = q.person ? [{ ...q.person }] : []
    if (q.related_person) {
      const label = locale.value === 'en' ? (q.rel_label_en || '') : (q.rel_label_it || '')
      persons.push({ ...q.related_person, _role: label })
    }
    return persons
  }
  if (q.type === 'indovina_chi') {
    // Show correct answer + all options that have names
    const correct = q.options?.find((o) => o.correct)
    return correct ? [correct] : []
  }
  if (q.type === 'intruso_graph') {
    const persons = []
    if (q.hub) persons.push({ ...q.hub, _role: t.value.trivia.roleHub })
    const impostor = q.nodes?.find((n) => n.is_impostor)
    if (impostor) persons.push({ ...impostor, _role: t.value.trivia.roleImpostor })
    return persons
  }
  if (q.type === 'intruso') {
    const intruso = q.persons?.find((p) => p.is_intruso)
    const groupMember = q.persons?.find((p) => !p.is_intruso)
    const persons = []
    if (intruso) persons.push({ ...intruso, _role: t.value.trivia.roleOddOneOut })
    if (groupMember) persons.push({ ...groupMember, _role: t.value.trivia.roleFromGroup })
    return persons
  }
  return []
})

onMounted(async () => {
  // Pre-fill filters from URL params (shared challenge links)
  const urlParams = new URLSearchParams(window.location.search)
  const urlCategory = urlParams.get('category')
  const urlNationality = urlParams.get('nationality')
  if (urlCategory && categories.includes(urlCategory)) selectedCategory.value = urlCategory
  if (urlNationality) selectedNationality.value = urlNationality
  const urlLiving = urlParams.get('living')
  if (urlLiving === 'true') selectedLiving.value = true
  else if (urlLiving === 'false') selectedLiving.value = false
  const urlDifficulty = urlParams.get('difficulty')
  if (urlDifficulty === 'easy' || urlDifficulty === 'hard') selectedDifficulty.value = urlDifficulty

  // Cached — instant on repeat visits
  availableNationalities.value = await trivia.fetchNationalities()
  loadingNationalities.value = false

  // Default nationality based on UI language (unless URL param already set)
  if (!selectedNationality.value && locale.value === 'it') {
    if (availableNationalities.value.includes('IT')) {
      selectedNationality.value = 'IT'
    }
  }
})

async function startRound() {
  await trivia.startRound(
    selectedCategory.value || null,
    selectedNationality.value || null,
    selectedLiving.value,
    selectedDifficulty.value || null,
  )
}

function onAnswer({ correct, points }) {
  trivia.submitAnswer(correct, points)
}

function onExplore(person) {
  emit('explore', person)
}
</script>

<style scoped>
.trivia-page {
  position: absolute;
  inset: 0;
  z-index: 200;
  background: var(--canvas-bg);
  background-image: radial-gradient(circle, rgba(0, 0, 0, 0.065) 1px, transparent 1px);
  background-size: 32px 32px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

/* ── Loading / Error ─────────────────────────────────── */
.trivia-loading,
.trivia-error {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  color: rgba(28, 21, 16, 0.55);
}

.spinner {
  width: 36px;
  height: 36px;
  border: 3px solid rgba(0, 0, 0, 0.1);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

.btn-retry {
  padding: 10px 20px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
}

/* ── Idle: filter + play ─────────────────────────────── */
.trivia-idle {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 28px;
  padding: 32px 20px;
  max-width: 560px;
  margin: 0 auto;
  width: 100%;
}

.idle-header {
  text-align: center;
}

.idle-title {
  font-family: var(--font-display);
  font-size: 28px;
  margin: 0 0 8px;
  color: var(--text);
}

.idle-sub {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 700;
  color: var(--text-muted);
  margin: 0 0 12px;
  line-height: 1.35;
  letter-spacing: -0.2px;
  max-width: 480px;
}

.filters {
  display: flex;
  flex-direction: column;
  gap: 18px;
  width: 100%;
}

.filter-group {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.filter-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: rgba(28, 21, 16, 0.45);
}

.filter-pills {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 6px;
  min-height: 32px;
}

.nat-loading {
  width: 80px;
  height: 28px;
  border-radius: 20px;
  background: var(--border);
  opacity: 0.5;
  animation: pulse 1.2s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 0.6; }
}

.filter-pill {
  padding: 9px 16px;
  border: 1.5px solid var(--border);
  border-radius: 20px;
  background: var(--surface);
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s, background-color 0.15s, color 0.15s;
  color: rgba(28, 21, 16, 0.7);
  min-height: 44px;
  display: inline-flex;
  align-items: center;
}

.filter-pill:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.filter-pill.active {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}

.btn-play {
  padding: 16px 40px;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 12px;
  font-size: 18px;
  font-weight: 700;
  cursor: pointer;
  transition: background-color 0.18s, transform 0.15s;
  /* Ensure touch target */
  min-height: 56px;
}

.btn-play:hover {
  background: var(--accent-hover);
  transform: translateY(-1px);
}

/* ── Game ────────────────────────────────────────────── */
.trivia-game {
  flex: 1;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
}

.progress-bar {
  height: 4px;
  background: rgba(0, 0, 0, 0.07);
  flex-shrink: 0;
}

.progress-fill {
  height: 100%;
  width: 100%;
  background: var(--accent);
  transform-origin: left center;
  transition: transform 0.4s ease;
  will-change: transform;
}

.progress-info {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 20px 6px;
  flex-shrink: 0;
}

.question-counter {
  font-size: 13px;
  color: rgba(28, 21, 16, 0.5);
  font-weight: 500;
}

.score-chip {
  font-size: 13px;
  font-weight: 700;
  color: var(--accent);
  background: rgba(27, 77, 142, 0.08);
  padding: 3px 10px;
  border-radius: 20px;
}

.question-container {
  flex: 1;
  overflow-y: auto;
  padding-bottom: 20px;
}

/* ── Summary ─────────────────────────────────────────── */
.trivia-summary-wrap {
  flex: 1;
  overflow-y: auto;
}
</style>
