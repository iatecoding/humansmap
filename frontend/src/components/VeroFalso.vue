<template>
  <div class="vero-falso">
    <div class="question-type-label">{{ t.trivia.questionTypeVeroFalso }}</div>

    <!-- Person image (optional) -->
    <div class="person-preview" v-if="question.person">
      <div class="person-avatar-wrap">
        <img
          v-if="question.person.image_cached_path && !imgError"
          :src="question.person.image_cached_path"
          :alt="localizedName(question.person, locale)"
          loading="lazy"
          class="person-avatar"
          @error="imgError = true"
        />
        <div v-else class="person-avatar person-avatar--placeholder">
          {{ localizedInitial(question.person, locale) }}
        </div>
      </div>
    </div>

    <!-- Statement -->
    <p class="statement">{{ statementText }}</p>

    <!-- Answer buttons -->
    <div v-if="!answered" class="answer-buttons">
      <button class="answer-btn vero" @click="answer(true)">
        ✓ {{ t.trivia.true }}
      </button>
      <button class="answer-btn falso" @click="answer(false)">
        ✗ {{ t.trivia.false }}
      </button>
    </div>

    <!-- Revealed state: show correct/wrong highlights -->
    <div v-else class="revealed-buttons">
      <button
        class="answer-btn vero"
        :class="{
          'is-correct': question.answer === true,
          'is-wrong': question.answer === false && userAnswer === true,
        }"
        disabled
        aria-disabled="true"
      >
        ✓ {{ t.trivia.true }}
      </button>
      <button
        class="answer-btn falso"
        :class="{
          'is-correct': question.answer === false,
          'is-wrong': question.answer === true && userAnswer === false,
        }"
        disabled
        aria-disabled="true"
      >
        ✗ {{ t.trivia.false }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useLocale } from '../composables/useLocale.js'
import { localizedInitial, localizedName } from '../utils/localized-content.js'

const props = defineProps({
  question: { type: Object, required: true },
})
const emit = defineEmits(['answer'])

const { t, locale } = useLocale()

const answered = ref(false)
const userAnswer = ref(null)
const imgError = ref(false)

const statementText = computed(() => {
  if (locale.value === 'en') return props.question.statement_en || props.question.statement_it
  return props.question.statement_it || props.question.statement_en
})

function answer(userSaidTrue) {
  if (answered.value) return
  answered.value = true
  userAnswer.value = userSaidTrue
  const correct = userSaidTrue === props.question.answer
  emit('answer', { correct, points: correct ? 1 : 0 })
}
</script>

<style scoped>
.vero-falso {
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

.person-preview {
  display: flex;
  justify-content: center;
}

.person-avatar-wrap {
  width: 80px;
  height: 80px;
}

.person-avatar {
  width: 80px;
  height: 80px;
  border-radius: 50%;
  object-fit: cover;
  border: 2px solid var(--border);
}

.person-avatar--placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-person);
  color: #fff;
  font-size: 28px;
  font-weight: 700;
}

.statement {
  font-size: 22px;
  font-family: var(--font-display);
  font-weight: 600;
  text-align: center;
  line-height: 1.35;
  margin: 0;
  color: var(--text);
}

.answer-buttons,
.revealed-buttons {
  display: flex;
  gap: 16px;
  width: 100%;
  max-width: 360px;
}

.answer-btn {
  flex: 1;
  padding: 16px;
  border: 2px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  font-size: 16px;
  font-weight: 700;
  cursor: pointer;
  transition: border-color 0.18s, background-color 0.18s, color 0.18s;
  letter-spacing: 0.03em;
  /* Ensure minimum touch target */
  min-height: 56px;
}

.answer-btn:disabled {
  cursor: default;
}

.answer-btn.vero {
  color: var(--color-correct);
  border-color: rgba(46, 160, 67, 0.35);
}

.answer-btn.falso {
  color: var(--color-wrong);
  border-color: rgba(176, 48, 48, 0.35);
}

.answer-btn.vero:hover:not(:disabled) {
  background: var(--color-correct-bg);
  border-color: var(--color-correct-border);
}

.answer-btn.falso:hover:not(:disabled) {
  background: var(--color-wrong-bg);
  border-color: var(--color-wrong-border);
}

.answer-btn.is-correct {
  background: var(--color-correct-bg);
  border-color: var(--color-correct);
  color: var(--color-correct);
}

.answer-btn.is-wrong {
  background: var(--color-wrong-bg);
  border-color: var(--color-wrong);
  opacity: 0.75;
}
</style>
