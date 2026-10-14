<template>
  <div class="indovina-chi">
    <div class="question-type-label">{{ t.trivia.questionTypeIndovinaChi }}</div>

    <div class="clue-box">
      <span class="clue-icon">🔗</span>
      <p class="clue-text">{{ clueText }}</p>
    </div>

    <div class="options-grid">
      <button
        v-for="option in question.options"
        :key="option.qid"
        class="option-card"
        :class="answered ? optionClass(option) : ''"
        :disabled="answered"
        @click="pickOption(option)"
      >
        <div class="option-avatar-wrap">
          <img
            v-if="option.image_cached_path && !imgErrors[option.qid]"
            :src="option.image_cached_path"
            :alt="localizedName(option, locale)"
            loading="lazy"
            class="option-avatar"
            @error="imgErrors[option.qid] = true"
          />
          <div v-else class="option-avatar option-avatar--placeholder">
            {{ localizedInitial(option, locale) }}
          </div>
          <div v-if="answered" class="card-overlay" :class="overlayClass(option)">
            <span v-if="option.correct">✓</span>
            <span v-else>✗</span>
          </div>
        </div>
        <span class="option-name">{{ localizedName(option, locale) }}</span>
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, reactive } from 'vue'
import { useLocale } from '../composables/useLocale.js'
import { localizedInitial, localizedName } from '../utils/localized-content.js'

const props = defineProps({
  question: { type: Object, required: true },
})
const emit = defineEmits(['answer'])

const { t, locale } = useLocale()

const answered = ref(false)
const selected = ref(null)
const imgErrors = reactive({})

const clueText = computed(() => {
  const q = props.question
  if (locale.value === 'en') return q.clue_en || q.clue_it || ''
  return q.clue_it || q.clue_en || ''
})

function pickOption(option) {
  if (answered.value) return
  answered.value = true
  selected.value = option.qid
  const correct = option.correct === true
  emit('answer', { correct, points: correct ? 1 : 0 })
}

function optionClass(option) {
  return {
    'is-correct': option.correct,
    'is-wrong': !option.correct && selected.value === option.qid,
  }
}

function overlayClass(option) {
  if (option.correct) return 'overlay-correct'
  if (selected.value === option.qid) return 'overlay-wrong'
  return 'overlay-neutral'
}
</script>

<style scoped>
.indovina-chi {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 20px;
  padding: 24px 20px;
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

.clue-box {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  background: var(--surface);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  width: 100%;
  max-width: 400px;
}

.clue-icon {
  font-size: 20px;
  flex-shrink: 0;
}

.clue-text {
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
  margin: 0;
  line-height: 1.4;
  font-family: var(--font-display);
}

.options-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  width: 100%;
  max-width: 400px;
}

.option-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 14px 10px;
  background: var(--surface);
  border: 2px solid var(--border);
  border-radius: 14px;
  cursor: pointer;
  transition: border-color 0.18s, background-color 0.18s, transform 0.18s;
}

.option-card:hover:not(:disabled) {
  border-color: var(--border-visible);
  background: var(--card);
  transform: translateY(-2px);
}

.option-card:disabled {
  cursor: default;
}

.option-card.is-correct {
  border-color: var(--color-correct-border);
  background: var(--color-correct-bg);
}

.option-card.is-wrong {
  border-color: var(--color-wrong-border);
  background: var(--color-wrong-bg);
}

.option-avatar-wrap {
  position: relative;
  width: 70px;
  height: 70px;
}

.option-avatar {
  width: 70px;
  height: 70px;
  border-radius: 50%;
  object-fit: cover;
  border: 2px solid var(--border);
  display: block;
}

.option-avatar--placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-person);
  color: #fff;
  font-size: 24px;
  font-weight: 700;
}

.card-overlay {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  font-weight: 700;
  animation: pop-in 0.2s ease;
}

.overlay-correct {
  background: rgba(46, 160, 67, 0.68);
  color: #fff;
}

.overlay-wrong {
  background: rgba(176, 48, 48, 0.72);
  color: #fff;
}

.overlay-neutral {
  display: none;
}

@keyframes pop-in {
  from { opacity: 0; transform: scale(0.5); }
  to   { opacity: 1; transform: scale(1); }
}

.option-name {
  font-size: 13px;
  font-weight: 600;
  text-align: center;
  color: var(--text);
  line-height: 1.3;
}
</style>
