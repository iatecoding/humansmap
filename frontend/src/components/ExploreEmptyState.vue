<template>
  <div class="empty-state">
    <p class="empty-lead" style="white-space: pre-line">{{ t.emptyState.exploreLead }}</p>
    <p class="empty-sub">{{ t.emptyState.exploreSub }}</p>
    <div class="suggestions">
      <span class="suggestions-label">{{ t.emptyState.suggestionsLabel }}</span>
      <div class="suggestions-chips">
        <span v-if="loading" class="chip-spinner" aria-hidden="true"></span>
        <button
          v-for="p in suggestions"
          :key="p.qid"
          class="suggestion-chip"
          :disabled="loadingQid === p.qid"
          @click="$emit('select', p)"
        >
          <span v-if="loadingQid === p.qid" class="chip-spinner" aria-hidden="true"></span>
          {{ locale === 'it' ? (p.name_it || p.name_en) : (p.name_en || p.name_it) }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { useLocale } from '../composables/useLocale.js'

const { t, locale } = useLocale()

defineProps({
  suggestions: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  loadingQid: { type: String, default: null },
})

defineEmits(['select'])
</script>

<style scoped>
.empty-state {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  z-index: 2;
  padding: 32px 24px;
  text-align: center;
  pointer-events: none;
}

.empty-state .suggestions {
  pointer-events: auto;
}
.empty-lead {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 700;
  color: var(--text-muted);
  margin: 0 0 12px;
  line-height: 1.35;
  letter-spacing: -0.2px;
  max-width: 480px;
}
.empty-sub {
  font-size: 14px;
  color: var(--text-muted);
  margin: 0 0 28px;
  max-width: 360px;
  line-height: 1.5;
}
.suggestions {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.suggestions-chips {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: center;
}
.suggestions-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: rgba(28, 21, 16, 0.3);
}
.suggestion-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  min-height: 44px;
  background: rgba(255, 255, 255, 0.6);
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: 20px;
  color: rgba(28, 21, 16, 0.65);
  font-size: 13px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s, transform 0.1s;
}
.suggestion-chip:hover:not(:disabled) {
  background: rgba(27, 77, 142, 0.08);
  border-color: rgba(27, 77, 142, 0.3);
  color: var(--accent);
}
.suggestion-chip:active:not(:disabled) {
  background: rgba(27, 77, 142, 0.14);
  transform: scale(0.97);
}
.suggestion-chip:disabled {
  opacity: 0.6;
  cursor: wait;
}
.chip-spinner {
  width: 11px;
  height: 11px;
  border: 1.5px solid rgba(28, 21, 16, 0.2);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
  flex-shrink: 0;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
