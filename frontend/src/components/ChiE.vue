<template>
  <div class="chi-e">
    <div class="question-type-label">{{ t.trivia.questionTypeChiE }}</div>

    <!-- Photo with blur -->
    <div class="photo-wrap">
      <img
        v-if="question.person.image_cached_path && !imgError"
        :src="question.person.image_cached_path"
        :alt="answered ? localizedName(question.person, locale) : '?'"
        class="person-photo"
        :style="{ filter: photoFilter }"
        @error="imgError = true"
      />
      <div v-else class="person-photo person-photo--placeholder">?</div>
    </div>

    <!-- Clues revealed so far (shown in reverse order: description → occupation) -->
    <div class="clues-list">
      <div
        v-for="(clue, idx) in visibleClues"
        :key="idx"
        class="clue-item"
        :class="`clue-type-${clue.type}`"
      >
        <span class="clue-icon">{{ clueIcon(clue.type) }}</span>
        <span class="clue-text">{{ clueText(clue) }}</span>
      </div>
    </div>

    <!-- "Next clue" button — only when more clues remain and not yet answered -->
    <div v-if="!answered && revealedCount < totalClues" class="clue-actions">
      <button class="btn-next-clue" @click="revealNextClue">
        {{ t.trivia.nextClue }}
        <span class="clue-count">{{ revealedCount }}/{{ totalClues }}</span>
        <span class="pts-hint">({{ cluePointsHint }})</span>
      </button>
    </div>

    <!-- Options grid — always visible -->
    <div class="options-grid" :class="{ 'options-answered': answered }">
      <button
        v-for="option in question.options"
        :key="option.qid"
        class="option-card"
        :class="answered ? (option.correct ? 'option-correct' : (pickedQid === option.qid ? 'option-wrong' : 'option-dimmed')) : ''"
        :disabled="answered"
        @click="pickOption(option)"
      >
        <span class="option-name">{{ localizedName(option, locale) }}</span>
      </button>
    </div>

  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useLocale } from '../composables/useLocale.js'
import { localizedName } from '../utils/localized-content.js'

const props = defineProps({
  question: { type: Object, required: true },
})
const emit = defineEmits(['answer'])

const { t, locale } = useLocale()

const imgError = ref(false)
const answered = ref(false)
const pickedQid = ref(null)
const revealedCount = ref(1)  // Start with first clue visible

// Clues in backend order: vaguest first (era → occupation → nationality → connection → description)
const allClues = computed(() => [...(props.question.clues || [])])
const totalClues = computed(() => allClues.value.length)
const visibleClues = computed(() => allClues.value.slice(0, revealedCount.value))

// Blur: 20px → 0px as clues revealed
const photoFilter = computed(() => {
  if (answered.value) return 'blur(0px)'
  const steps = totalClues.value || 4
  const revealed = revealedCount.value
  const blurPx = Math.max(0, Math.round(20 - (20 / steps) * revealed))
  return `blur(${blurPx}px)`
})

// Points decrease as more clues are revealed
const currentPoints = computed(() => Math.max(1, totalClues.value + 1 - revealedCount.value))

const cluePointsHint = computed(() => {
  const p = currentPoints.value
  if (locale.value === 'en') return `${p} pt${p !== 1 ? 's' : ''} if correct`
  return `${p} punt${p !== 1 ? 'i' : 'o'} se corretto`
})

function clueText(clue) {
  if (locale.value === 'en') return clue.text_en || clue.text_it || ''
  return clue.text_it || clue.text_en || ''
}

function clueIcon(type) {
  const icons = { occupation: '💼', era: '📅', nationality: '🌍', connection: '🔗', description: '📖' }
  return icons[type] || '•'
}

function revealNextClue() {
  if (revealedCount.value < totalClues.value) {
    revealedCount.value++
  }
}

function pickOption(option) {
  if (answered.value) return
  answered.value = true
  pickedQid.value = option.qid
  const correct = option.correct === true
  const points = correct ? currentPoints.value : 0
  emit('answer', { correct, points })
}
</script>

<style scoped>
.chi-e {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 20px 20px;
  max-width: 520px;
  margin: 0 auto;
  width: 100%;
}

.question-type-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: rgba(28, 21, 16, 0.4);
  font-weight: 600;
}

.photo-wrap {
  width: 120px;
  height: 120px;
  flex-shrink: 0;
}

.person-photo {
  width: 120px;
  height: 120px;
  border-radius: 50%;
  object-fit: cover;
  border: 3px solid var(--border);
  transition: filter 0.6s ease;
  display: block;
}

.person-photo--placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--card);
  color: rgba(28, 21, 16, 0.25);
  font-size: 40px;
  font-weight: 300;
}

.clues-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  max-width: 380px;
}

.clue-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  background: var(--surface);
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  animation: fade-in 0.3s ease;
}

@keyframes fade-in {
  from { opacity: 0; transform: translateX(-8px); }
  to   { opacity: 1; transform: translateX(0); }
}

.clue-icon { font-size: 15px; line-height: 1.4; flex-shrink: 0; }

.clue-text {
  font-size: 14px;
  color: var(--text);
  line-height: 1.45;
}

.clue-type-description .clue-text {
  font-style: italic;
  color: rgba(28, 21, 16, 0.7);
}

.clue-actions {
  display: flex;
  justify-content: center;
  width: 100%;
  max-width: 380px;
}

.btn-next-clue {
  background: none;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 9px 18px;
  font-size: 13px;
  cursor: pointer;
  color: rgba(28, 21, 16, 0.65);
  display: flex;
  align-items: center;
  gap: 8px;
  transition: border-color 0.18s, color 0.18s;
}

.btn-next-clue:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.clue-count {
  font-size: 11px;
  opacity: 0.65;
}

.pts-hint {
  font-size: 11px;
  opacity: 0.55;
}

/* Options grid (2×2) — always visible */
.options-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  width: 100%;
  max-width: 400px;
}

.option-card {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px 12px;
  background: var(--surface);
  border: 2px solid var(--border);
  border-radius: 12px;
  cursor: pointer;
  transition: border-color 0.18s, background-color 0.18s, transform 0.18s, opacity 0.18s;
  min-height: 64px;
}

.option-card:hover:not(:disabled) {
  border-color: var(--border-visible);
  transform: translateY(-2px);
  background: var(--card);
}

.option-card:disabled {
  cursor: default;
}

.option-card.option-correct {
  border-color: var(--color-correct-border);
  background: var(--color-correct-bg);
}

.option-card.option-wrong {
  border-color: var(--color-wrong-border);
  background: var(--color-wrong-bg);
}

.option-card.option-dimmed {
  opacity: 0.45;
}

.option-name {
  font-size: 13px;
  font-weight: 600;
  text-align: center;
  line-height: 1.35;
  color: var(--text);
}
</style>
