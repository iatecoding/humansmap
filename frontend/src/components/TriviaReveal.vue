<template>
  <div class="reveal-overlay" role="button" tabindex="0" @click="handleTap" @keydown.enter.prevent="handleTap" @keydown.space.prevent="handleTap">
    <div class="reveal-card" :class="correct ? 'correct' : 'wrong'">
      <div class="result-badge">
        <span class="result-icon">{{ correct ? '✓' : '✗' }}</span>
        <span class="result-label">{{ correct ? t.trivia.correct : t.trivia.wrong }}</span>
        <span v-if="points > 0" class="points-badge">+{{ points }}</span>
      </div>

      <!-- Fact -->
      <p v-if="factText" class="fact-text">{{ factText }}</p>

      <!-- Correction (for false Vero o Falso) -->
      <p v-if="correctionText" class="correction-text">{{ correctionText }}</p>

      <!-- Explanation (for L'Intruso — why the others belong together) -->
      <p v-if="explanationText" class="explanation-text">{{ explanationText }}</p>

      <!-- Persons: single card or two-card pair with connector -->
      <template v-for="(person, idx) in persons" :key="person.qid">
        <!-- Role label shown above each card when present -->
        <div v-if="person._role" class="person-role-label">
          {{ person._role }}
        </div>
        <div class="bookmark-row">
          <div class="bookmark-person">
            <img
              v-if="person.image_cached_path && !imgErrors[person.qid]"
              :src="person.image_cached_path"
              :alt="localizedName(person, locale)"
              loading="lazy"
              class="bookmark-avatar"
              @error="imgErrors[person.qid] = true"
            />
            <div v-else class="bookmark-avatar placeholder-avatar">{{ localizedInitial(person, locale) }}</div>
            <div class="bookmark-info">
              <span class="bookmark-name">{{ localizedName(person, locale) }}</span>
              <span v-if="personMeta(person)" class="bookmark-meta">{{ personMeta(person) }}</span>
            </div>
          </div>
          <button
            class="bookmark-btn"
            :class="{ bookmarked: isBookmarked(person.qid) }"
            @click.stop="$emit('bookmark', person)"
            :aria-label="t.trivia.bookmark"
          >
            {{ isBookmarked(person.qid) ? '★' : '☆' }}
          </button>
        </div>
      </template>

      <p class="tap-hint">{{ t.trivia.tapToContinue }}</p>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive } from 'vue'
import { useLocale } from '../composables/useLocale.js'
import { localizedInitial, localizedName } from '../utils/localized-content.js'

const props = defineProps({
  correct: { type: Boolean, required: true },
  points: { type: Number, default: 0 },
  question: { type: Object, required: true },
  persons: { type: Array, default: () => [] },
  isBookmarked: { type: Function, required: true },
})

const imgErrors = reactive({})

const emit = defineEmits(['continue', 'bookmark'])

const { t, locale } = useLocale()

const factText = computed(() => {
  const q = props.question
  if (!q) return null
  if (locale.value === 'en') return q.fact_en || q.fact_it || null
  return q.fact_it || q.fact_en || null
})

const correctionText = computed(() => {
  const q = props.question
  if (!q || q.type !== 'vero_falso' || props.correct) return null
  if (locale.value === 'en') return q.correction_en || null
  return q.correction_it || null
})

const explanationText = computed(() => {
  const q = props.question
  if (!q || (q.type !== 'intruso' && q.type !== 'intruso_graph')) return null
  if (locale.value === 'en') return q.hint_en || q.hint_it || null
  return q.hint_it || q.hint_en || null
})

function personMeta(p) {
  if (!p) return ''
  const parts = []
  const birth = p.birth_date ? String(p.birth_date).slice(0, 4) : null
  const death = p.death_date ? String(p.death_date).slice(0, 4) : null
  if (birth) parts.push(death ? `${birth}–${death}` : `${birth}`)
  return parts.join(' · ')
}

function handleTap() {
  emit('continue')
}
</script>

<style scoped>
.reveal-overlay {
  position: absolute;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-overlay);
  backdrop-filter: blur(4px);
  cursor: pointer;
}

.reveal-card {
  background: var(--surface);
  border: 2px solid var(--border);
  border-radius: 16px;
  padding: 28px 32px;
  max-width: 440px;
  width: calc(100% - 40px);
  text-align: center;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12);
  animation: slide-up 0.25s ease;
}

.reveal-card.correct {
  border-color: var(--color-correct-border);
  background: var(--surface);
}

.reveal-card.wrong {
  border-color: var(--color-wrong-border);
  background: var(--surface);
}

@keyframes slide-up {
  from { transform: translateY(20px); opacity: 0; }
  to   { transform: translateY(0);    opacity: 1; }
}

.result-badge {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  margin-bottom: 16px;
}

.result-icon {
  font-size: 24px;
  font-weight: 700;
}

.correct .result-icon { color: var(--color-correct); }
.wrong   .result-icon { color: var(--color-wrong); }

.result-label {
  font-size: 20px;
  font-weight: 700;
  font-family: var(--font-display);
}

.correct .result-label { color: var(--color-correct); }
.wrong   .result-label { color: var(--color-wrong); }

.points-badge {
  background: var(--accent);
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  padding: 3px 10px;
  border-radius: 20px;
}

.fact-text {
  font-size: 14px;
  color: rgba(28, 21, 16, 0.7);
  line-height: 1.5;
  margin: 0 0 12px;
}

.correction-text {
  font-size: 13px;
  color: rgba(28, 21, 16, 0.6);
  font-style: italic;
  margin: 0 0 16px;
  padding: 10px;
  background: rgba(0, 0, 0, 0.04);
  border-radius: 8px;
}

.explanation-text {
  font-size: 13px;
  color: rgba(28, 21, 16, 0.6);
  margin: 0 0 16px;
  padding: 10px;
  background: rgba(0, 0, 0, 0.04);
  border-radius: 8px;
}

.person-role-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: rgba(28, 21, 16, 0.4);
  text-align: left;
  padding: 0 4px;
  margin-top: 6px;
}

.bookmark-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  background: rgba(0, 0, 0, 0.03);
  border-radius: 10px;
  margin-bottom: 14px;
}

.bookmark-person {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.bookmark-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
}

.placeholder-avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-person);
  color: #fff;
  font-weight: 600;
  font-size: 15px;
}

.bookmark-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.bookmark-name {
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bookmark-meta {
  font-size: 11px;
  color: rgba(28, 21, 16, 0.45);
  font-weight: 400;
}

.bookmark-btn {
  font-size: 22px;
  background: none;
  border: none;
  cursor: pointer;
  padding: 4px;
  color: rgba(28, 21, 16, 0.35);
  transition: color 0.2s, transform 0.15s;
  flex-shrink: 0;
}

.bookmark-btn:hover {
  color: var(--accent);
}

.bookmark-btn.bookmarked {
  color: var(--color-bookmark);
}

.tap-hint {
  font-size: 11px;
  color: rgba(28, 21, 16, 0.3);
  margin: 0;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
</style>
