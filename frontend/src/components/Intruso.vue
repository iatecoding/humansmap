<template>
  <div class="intruso">
    <!-- Graph variant: show hub context -->
    <template v-if="isGraphVariant">
      <div class="question-type-label">{{ t.trivia.questionTypeIntrusoGraph }}</div>
      <div class="hub-context">
        <div class="hub-avatar-wrap">
          <img
            v-if="question.hub?.image_cached_path && !hubImgError"
            :src="question.hub.image_cached_path"
            :alt="hubName"
            loading="lazy"
            class="hub-avatar"
            @error="hubImgError = true"
          />
          <div v-else class="hub-avatar hub-avatar--placeholder">
            {{ hubInitials }}
          </div>
        </div>
        <div class="hub-info">
          <p class="hub-label">{{ t.trivia.hubLabel }}</p>
          <p class="hub-name">{{ hubName }}</p>
        </div>
      </div>
      <p class="prompt">
        {{ answered ? t.trivia.impostorRevealed : t.trivia.whoDoesntBelong }}
      </p>
    </template>

    <!-- Standard variant -->
    <template v-else>
      <div class="question-type-label">{{ t.trivia.questionTypeIntruso }}</div>
      <p class="prompt">
        {{ answered ? revealedHint : t.trivia.whoIsOddOut }}
      </p>
    </template>

    <div class="persons-grid">
      <button
        v-for="person in persons"
        :key="person.qid"
        class="person-card"
        :class="cardClass(person)"
        :disabled="answered"
        @click="pickPerson(person)"
      >
        <div class="card-avatar-wrap">
          <img
            v-if="person.image_cached_path && !imgErrors[person.qid]"
            :src="person.image_cached_path"
            :alt="localizedName(person, locale)"
            loading="lazy"
            class="card-avatar"
            @error="imgErrors[person.qid] = true"
          />
          <div v-else class="card-avatar card-avatar--placeholder">
            {{ localizedInitial(person, locale) }}
          </div>
          <div v-if="answered" class="card-overlay" :class="overlayClass(person)">
            <span>{{ cardOverlayIcon(person) }}</span>
          </div>
        </div>
        <span class="card-name">{{ localizedName(person, locale) }}</span>
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed } from 'vue'
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
const hubImgError = ref(false)

const isGraphVariant = computed(() => props.question.type === 'intruso_graph')

// Unified persons list: standard uses question.persons, graph uses question.nodes
const persons = computed(() =>
  isGraphVariant.value ? (props.question.nodes || []) : (props.question.persons || [])
)

// For graph variant the wrong-answer field is is_impostor; for standard it's is_intruso
function isWrong(person) {
  return isGraphVariant.value ? person.is_impostor : person.is_intruso
}

const hubName = computed(() => localizedName(props.question.hub, locale.value))
const hubInitials = computed(() => localizedInitial(props.question.hub, locale.value))

const revealedHint = computed(() => {
  const q = props.question
  if (locale.value === 'en') return q.hint_en || q.hint_it || ''
  return q.hint_it || q.hint_en || ''
})

function pickPerson(person) {
  if (answered.value) return
  answered.value = true
  selected.value = person.qid
  const correct = isWrong(person)
  emit('answer', { correct, points: correct ? 1 : 0 })
}

function cardClass(person) {
  if (!answered.value) return {}
  const wasSelected = selected.value === person.qid
  const wrong = isWrong(person)
  return {
    'is-correct-pick':  wasSelected && wrong,   // user found the intruso ✓
    'is-wrong-pick':    wasSelected && !wrong,  // user picked wrong ✗
    'is-revealed':      !wasSelected && wrong,  // answer revealed after wrong pick
    'is-bystander':     !wasSelected && !wrong, // uninvolved group member
  }
}

function overlayClass(person) {
  const wasSelected = selected.value === person.qid
  const wrong = isWrong(person)
  if (wasSelected && wrong)  return 'overlay-correct'     // found it
  if (wasSelected && !wrong) return 'overlay-wrong-pick'  // bad guess
  if (!wasSelected && wrong) return 'overlay-reveal'      // reveal answer
  return 'overlay-neutral'                                // dim bystander
}

function cardOverlayIcon(person) {
  const wasSelected = selected.value === person.qid
  const wrong = isWrong(person)
  if (wasSelected && wrong)  return '✓'
  if (wasSelected && !wrong) return '✗'
  if (!wasSelected && wrong) return '!'
  return ''
}
</script>

<style scoped>
.intruso {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
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

.hub-context {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 18px;
  background: var(--surface);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  width: 100%;
  max-width: 400px;
}

.hub-avatar-wrap {
  flex-shrink: 0;
  width: 52px;
  height: 52px;
}

.hub-avatar {
  width: 52px;
  height: 52px;
  border-radius: 50%;
  object-fit: cover;
  border: 2px solid var(--border);
  display: block;
}

.hub-avatar--placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-person);
  color: #fff;
  font-size: 18px;
  font-weight: 700;
}

.hub-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.hub-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: rgba(28, 21, 16, 0.4);
  margin: 0;
  font-weight: 600;
}

.hub-name {
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
  margin: 0;
  font-family: var(--font-display);
}

.prompt {
  font-size: 19px;
  font-weight: 700;
  text-align: center;
  margin: 0;
  color: var(--text);
  font-family: var(--font-display);
  min-height: 26px;
  transition: color 0.3s;
}

.persons-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  width: 100%;
  max-width: 400px;
}

.person-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 14px 10px;
  background: var(--surface);
  border: 2px solid var(--border);
  border-radius: 14px;
  cursor: pointer;
  transition: border-color 0.18s, background-color 0.18s, transform 0.18s, opacity 0.18s;
}

.person-card:hover:not(:disabled) {
  border-color: var(--border-visible);
  background: var(--card);
  transform: translateY(-2px);
}

.person-card:disabled {
  cursor: default;
}

.person-card.is-correct-pick {
  border-color: var(--color-correct-border);
  background: var(--color-correct-bg);
}

.person-card.is-wrong-pick {
  border-color: var(--color-wrong-border);
  background: var(--color-wrong-bg);
}

.person-card.is-revealed {
  border-color: var(--color-reveal-border);
  background: var(--color-reveal-bg);
}

.person-card.is-bystander {
  opacity: 0.45;
}

.card-avatar-wrap {
  position: relative;
  width: 70px;
  height: 70px;
}

.card-avatar {
  width: 70px;
  height: 70px;
  border-radius: 50%;
  object-fit: cover;
  border: 2px solid var(--border);
  display: block;
}

.card-avatar--placeholder {
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
  background: rgba(46, 160, 67, 0.72);
  color: #fff;
}

.overlay-wrong-pick {
  background: rgba(176, 48, 48, 0.75);
  color: #fff;
}

.overlay-reveal {
  background: rgba(191, 107, 40, 0.78);
  color: #fff;
  font-size: 20px;
}

.overlay-neutral {
  display: none;
}

@keyframes pop-in {
  from { opacity: 0; transform: scale(0.5); }
  to   { opacity: 1; transform: scale(1); }
}

.card-name {
  font-size: 13px;
  font-weight: 600;
  text-align: center;
  color: var(--text);
  line-height: 1.3;
}
</style>
