<template>
  <div class="summary">
    <div class="summary-header">
      <h2 class="summary-title">{{ t.trivia.results }}</h2>
      <div class="score-display">
        <span class="score-value">{{ score }}</span>
        <span class="score-sep">/</span>
        <span class="score-max">{{ maxScore }}</span>
        <span class="score-label">{{ t.trivia.points.replace('{n}', '') }}</span>
      </div>
      <div class="correct-display">
        {{ correctCount }}/{{ totalQuestions }} {{ t.trivia.correct.toLowerCase() }}
      </div>
    </div>

    <!-- Answer breakdown -->
    <div class="answer-breakdown">
      <div
        v-for="(answer, idx) in answers"
        :key="idx"
        class="answer-row"
        :class="answer.correct ? 'row-correct' : 'row-wrong'"
      >
        <span class="answer-icon">{{ answer.correct ? '✓' : '✗' }}</span>
        <span class="answer-type">{{ typeLabel(answer.type) }}</span>
        <span class="answer-pts">+{{ answer.points }}</span>
      </div>
    </div>

    <!-- Share + Play again -->
    <div class="action-row">
      <button class="btn-share" @click="share">
        {{ copied ? '✓ ' + t.trivia.copied : t.trivia.share }}
      </button>
      <button class="btn-play-again" @click="$emit('playAgain')">
        {{ t.trivia.playAgain }}
      </button>
    </div>

    <!-- Bookmarks -->
    <div v-if="bookmarks.length > 0" class="bookmarks-section">
      <h3 class="bookmarks-title">{{ t.trivia.bookmarked }}</h3>
      <div class="bookmarks-list">
        <div v-for="person in bookmarks" :key="person.qid" class="bookmark-item">
          <div class="bk-person">
            <img
              v-if="person.image_cached_path && !imgErrors[person.qid]"
              :src="person.image_cached_path"
              :alt="localizedName(person, locale)"
              loading="lazy"
              class="bk-avatar"
              @error="imgErrors[person.qid] = true"
            />
            <div v-else class="bk-avatar bk-avatar--placeholder">{{ localizedInitial(person, locale) }}</div>
            <span class="bk-name">{{ localizedName(person, locale) }}</span>
          </div>
          <button class="btn-explore" @click="$emit('explore', person)">
            {{ t.trivia.exploreInGraph }}
          </button>
        </div>
      </div>
    </div>

    <div v-else class="no-bookmarks">
      <p>{{ t.trivia.noBookmarks }}</p>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { useLocale } from '../composables/useLocale.js'
import { localizedInitial, localizedName } from '../utils/localized-content.js'

const props = defineProps({
  answers: { type: Array, required: true },
  score: { type: Number, required: true },
  maxScore: { type: Number, required: true },
  correctCount: { type: Number, required: true },
  totalQuestions: { type: Number, required: true },
  bookmarks: { type: Array, default: () => [] },
  shareText: { type: String, default: '' },
})

const emit = defineEmits(['playAgain', 'explore'])

const { t, locale } = useLocale()

const copied = ref(false)
const imgErrors = reactive({})

const TYPE_LABEL_KEYS = {
  chi_e:         'questionTypeChiE',
  vero_falso:    'questionTypeVeroFalso',
  intruso:       'questionTypeIntruso',
  intruso_graph: 'questionTypeIntrusoGraph',
  indovina_chi:  'questionTypeIndovinaChi',
}

function typeLabel(type) {
  const key = TYPE_LABEL_KEYS[type]
  if (!key) return type
  return t.value.trivia[key] || type
}

async function share() {
  if (!props.shareText) return
  try {
    await navigator.clipboard.writeText(props.shareText)
    copied.value = true
    setTimeout(() => (copied.value = false), 2000)
  } catch {
    // Clipboard not available
  }
}


</script>

<style scoped>
.summary {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 20px;
  padding: 28px 20px 40px;
  max-width: 480px;
  margin: 0 auto;
  width: 100%;
}

.summary-header {
  text-align: center;
}

.summary-title {
  font-family: var(--font-display);
  font-size: 22px;
  margin: 0 0 12px;
  color: var(--text);
}

.score-display {
  display: flex;
  align-items: baseline;
  justify-content: center;
  gap: 4px;
  margin-bottom: 6px;
}

.score-value {
  font-size: 48px;
  font-weight: 700;
  font-family: var(--font-display);
  color: var(--accent);
  line-height: 1;
}

.score-sep {
  font-size: 28px;
  color: rgba(28, 21, 16, 0.3);
  line-height: 1;
}

.score-max {
  font-size: 28px;
  color: rgba(28, 21, 16, 0.5);
  line-height: 1;
}

.score-label {
  font-size: 14px;
  color: rgba(28, 21, 16, 0.5);
  margin-left: 4px;
}

.correct-display {
  font-size: 14px;
  color: rgba(28, 21, 16, 0.55);
}

.answer-breakdown {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  max-width: 340px;
}

.answer-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  border-radius: 8px;
  background: var(--surface);
  border: 1px solid var(--border-subtle);
}

.row-correct {
  border-left: 3px solid var(--color-correct);
}

.row-wrong {
  border-left: 3px solid var(--color-wrong);
}

.answer-icon {
  font-size: 14px;
  font-weight: 700;
  width: 18px;
  flex-shrink: 0;
}

.row-correct .answer-icon { color: var(--color-correct); }
.row-wrong  .answer-icon { color: var(--color-wrong); }

.answer-type {
  flex: 1;
  font-size: 13px;
  color: var(--text);
  font-weight: 500;
}

.answer-pts {
  font-size: 13px;
  font-weight: 700;
  color: var(--accent);
}

.action-row {
  display: flex;
  gap: 10px;
  width: 100%;
  max-width: 400px;
}

.btn-share {
  padding: 12px 20px;
  background: var(--surface);
  border: 2px solid var(--border);
  border-radius: 10px;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: border-color 0.18s, color 0.18s;
  flex: 1;
}

.btn-share:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.bookmarks-section {
  width: 100%;
  max-width: 400px;
}

.bookmarks-title {
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: rgba(28, 21, 16, 0.45);
  font-weight: 600;
  margin: 0 0 10px;
}

.bookmarks-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.bookmark-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  background: var(--surface);
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
}

.bk-person {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.bk-avatar {
  width: 38px;
  height: 38px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
}

.bk-avatar--placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-person);
  color: #fff;
  font-weight: 700;
  font-size: 15px;
}

.bk-name {
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.btn-explore {
  padding: 7px 12px;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 7px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
  transition: background 0.18s;
}

.btn-explore:hover { background: var(--accent-hover); }

.no-bookmarks p {
  font-size: 13px;
  color: rgba(28, 21, 16, 0.4);
  text-align: center;
  margin: 0;
}

.btn-play-again {
  padding: 12px 20px;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 10px;
  font-size: 15px;
  font-weight: 700;
  cursor: pointer;
  transition: background 0.18s;
  flex: 1;
}

.btn-play-again:hover { background: var(--accent-hover); }
</style>
