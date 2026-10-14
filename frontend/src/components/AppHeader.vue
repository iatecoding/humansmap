<template>
  <header class="top-bar" :class="{ 'top-bar--feed-full': feedFull }">
    <button
      class="brand"
      type="button"
      @click="$emit('home')"
      @keydown.space.prevent="$emit('home')"
      :aria-label="`${title} — home`"
    >
      <h1>{{ title }}</h1>
      <span class="subtitle">{{ subtitle }}</span>
    </button>

    <div class="top-center">
      <div
        class="mode-tabs"
        role="tablist"
        aria-label="View mode"
        @keydown="onTabKeydown"
      >
        <button
          v-for="(tab, index) in tabs"
          :key="tab.id"
          :ref="(element) => setTabRef(element, index)"
          type="button"
          role="tab"
          :id="`mode-tab-${tab.id}`"
          :aria-selected="mode === tab.id"
          aria-controls="main-content"
          :tabindex="mode === tab.id ? 0 : -1"
          :class="{ active: mode === tab.id }"
          @click="$emit('mode', tab.id)"
        >{{ tab.label }}</button>
      </div>
    </div>

    <div class="top-right">
      <a class="gallery-link" href="/gallery/">{{ t.ui.gallery }}</a>
      <SearchBar
        v-if="mode === 'explore'"
        :selectedName="selectedName"
        @select="$emit('search', $event)"
      />
    </div>
  </header>
</template>

<script setup>
import { ref } from 'vue'
import { useLocale } from '../composables/useLocale.js'
import SearchBar from './SearchBar.vue'

const { t } = useLocale()

defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, required: true },
  mode: { type: String, required: true },
  selectedName: { type: String, default: null },
  feedFull: { type: Boolean, default: false },
})

defineEmits(['home', 'mode', 'search'])

const tabs = [
  { id: 'explore', get label() { return t.value.ui.explore } },
  { id: 'game', get label() { return t.value.ui.play } },
  { id: 'feed', get label() { return t.value.ui.scroll } },
]
const tabRefs = ref([])

function setTabRef(element, index) {
  if (element) tabRefs.value[index] = element
}

function onTabKeydown(event) {
  const current = tabRefs.value.findIndex((element) => element === event.target)
  if (current < 0) return

  let next = current
  if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (current + 1) % tabs.length
  if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (current - 1 + tabs.length) % tabs.length
  if (event.key === 'Home') next = 0
  if (event.key === 'End') next = tabs.length - 1
  if (next === current) return

  event.preventDefault()
  tabRefs.value[next]?.focus()
}
</script>

<style scoped>
.top-bar {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  padding: 10px 20px;
  background: var(--surface);
  border-bottom: 2px solid var(--border);
  z-index: 10;
  gap: 12px;
}
.top-bar--feed-full { display: none; }
.brand {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-shrink: 0;
  min-height: var(--touch-target);
  border: 0;
  padding: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  user-select: none;
  text-align: left;
}
.brand:hover h1 { color: rgba(28, 21, 16, 0.7); }
.brand h1 {
  margin: 0;
  font-size: 23px;
  font-weight: 700;
  font-family: var(--font-display);
  letter-spacing: -0.5px;
  color: var(--accent);
}
.brand:focus-visible,
.mode-tabs button:focus-visible,
.gallery-link:focus-visible {
  outline: var(--focus-ring);
  outline-offset: 3px;
}
.gallery-link {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  min-height: var(--touch-target);
  padding: 0 10px;
  border-radius: var(--radius-control);
  color: var(--accent);
  font-size: 14px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
  transition: color 0.2s;
}
.gallery-link:hover {
  color: var(--accent-hover);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.subtitle {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--accent);
  font-weight: 500;
}
.top-center {
  display: flex;
  justify-content: center;
  align-items: center;
}
.top-right {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.mode-tabs {
  display: flex;
  background: rgba(0, 0, 0, 0.06);
  border-radius: 8px;
  padding: 2px;
  flex-shrink: 0;
}
.mode-tabs button {
  padding: 6px 16px;
  min-height: var(--touch-target);
  border: none;
  background: none;
  color: rgba(28, 21, 16, 0.5);
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  border-radius: 6px;
  transition: background-color 0.2s, color 0.2s;
  white-space: nowrap;
}
.mode-tabs button.active { background: var(--accent); color: #fff; }
.mode-tabs button:hover:not(.active) {
  color: rgba(28, 21, 16, 0.8);
  background: rgba(0, 0, 0, 0.05);
}

@media (min-width: 769px) {
  .top-bar--feed-full { display: grid; }
}
@media (max-width: 768px) {
  .top-bar {
    grid-template-columns: auto 1fr;
    grid-template-rows: auto auto;
    gap: 8px;
    padding: 8px 12px;
  }
  .brand { grid-column: 1; grid-row: 1; }
  .top-center { grid-column: 1 / -1; grid-row: 2; }
  .top-right { grid-column: 2; grid-row: 1; justify-content: flex-end; }
  .mode-tabs { width: 100%; }
  .mode-tabs button { flex: 1; text-align: center; }
}
@media (max-width: 480px) {
  .brand h1 { font-size: 17px; }
  .subtitle { display: none; }
  .mode-tabs button { font-size: 13px; padding: 6px 10px; }
  .gallery-link { font-size: 13px; padding: 0 8px; }
  .top-right :deep(.search-bar) {
    min-width: 0;
    max-width: 100%;
  }
}
</style>
