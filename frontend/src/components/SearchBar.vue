<template>
  <div class="search-bar">
    <div class="search-input-wrapper">
      <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
      </svg>
      <input
        ref="inputEl"
        type="text"
        role="combobox"
        :aria-expanded="showDropdown && results.length > 0 ? 'true' : 'false'"
        aria-autocomplete="list"
        aria-haspopup="listbox"
        :aria-controls="listboxId"
        :aria-activedescendant="activeIndex >= 0 ? `${listboxId}-opt-${activeIndex}` : undefined"
        :placeholder="selectedName || props.placeholder || t.search.placeholder"
        :value="query"
        @input="onInput"
        @focus="showDropdown = true"
        @keydown="onKeydown"
      />
      <span v-if="loading" class="spinner" role="status" aria-label="Ricerca in corso"></span>
    </div>
    <ul
      v-if="showDropdown && results.length"
      :id="listboxId"
      role="listbox"
      class="search-results"
      :aria-label="t.search.placeholder"
    >
      <li
        v-for="(person, index) in results"
        :key="person.id || person._key"
        :id="`${listboxId}-opt-${index}`"
        role="option"
        :aria-selected="index === activeIndex"
        @mousedown.prevent
        @click="select(person)"
        @mouseover="activeIndex = index"
      >
        <strong>{{ prefName(person) }}</strong>
        <span v-if="person.name_en && person.name_it && person.name_en !== person.name_it" class="name-en">{{ person.name_it }}</span>
        <span v-if="person.description_en || person.description_it" class="desc">{{ prefName({ name_en: person.description_en, name_it: person.description_it }) }}</span>
      </li>
    </ul>
    <div v-if="showDropdown && query.length >= 2 && !loading && !results.length" class="no-results" role="status">
      {{ t.search.noResults }}
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { useSearch } from '../composables/useSearch.js'
import { useLocale } from '../composables/useLocale.js'

const { t, prefName } = useLocale()
const props = defineProps({
  placeholder: { type: String, default: null },
  selectedName: { type: String, default: null },
})
const emit = defineEmits(['select'])
const { results, loading, query, search } = useSearch()
const showDropdown = ref(false)
const inputEl = ref(null)
const activeIndex = ref(-1)

const uid = Math.random().toString(36).slice(2, 8)
const listboxId = `search-results-${uid}`

function onInput(e) {
  search(e.target.value)
  showDropdown.value = true
  activeIndex.value = -1
}

function onKeydown(e) {
  if (e.key === 'Escape') {
    showDropdown.value = false
    activeIndex.value = -1
    return
  }
  if (!showDropdown.value || !results.value.length) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    activeIndex.value = Math.min(activeIndex.value + 1, results.value.length - 1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    activeIndex.value = Math.max(activeIndex.value - 1, -1)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    if (activeIndex.value >= 0 && results.value[activeIndex.value]) {
      select(results.value[activeIndex.value])
    }
  }
}

function select(person) {
  showDropdown.value = false
  activeIndex.value = -1
  query.value = ''
  results.value = []
  emit('select', person)
}

watch(results, () => { activeIndex.value = -1 })

function onClickOutside(e) {
  if (inputEl.value && !inputEl.value.closest('.search-bar').contains(e.target)) {
    showDropdown.value = false
    activeIndex.value = -1
  }
}

onMounted(() => document.addEventListener('click', onClickOutside))
onUnmounted(() => document.removeEventListener('click', onClickOutside))
</script>

<style scoped>
.search-bar {
  position: relative;
  width: 100%;
  max-width: 320px;
  min-width: 200px;
}
.search-input-wrapper {
  display: flex;
  align-items: center;
  background: rgba(255, 255, 255, 0.7);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0 12px;
  transition: border-color 0.2s, background 0.2s;
}
.search-input-wrapper:focus-within {
  border-color: var(--accent);
  background: var(--bg);
}
.search-icon {
  width: 18px;
  height: 18px;
  color: rgba(28, 21, 16, 0.45);
  flex-shrink: 0;
}
input {
  flex: 1;
  border: none;
  background: none;
  color: var(--text);
  font-size: 14px;
  padding: 10px 8px;
  outline: none;
}
input::placeholder {
  color: rgba(28, 21, 16, 0.4);
}
.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(28, 21, 16, 0.2);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}
.search-results {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-top: 4px;
  max-height: 300px;
  overflow-y: auto;
  list-style: none;
  padding: 4px;
  z-index: 100;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.10);
}
.search-results li {
  padding: 10px 12px;
  cursor: pointer;
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.search-results li:hover,
.search-results li[aria-selected="true"] {
  background: rgba(0, 0, 0, 0.05);
}
.name-en {
  font-size: 12px;
  color: rgba(28, 21, 16, 0.55);
}
.desc {
  font-size: 11px;
  color: rgba(28, 21, 16, 0.5);
}
@media (max-width: 768px) {
  .search-bar {
    max-width: none;
  }
}
.no-results {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-top: 4px;
  padding: 12px;
  color: rgba(28, 21, 16, 0.5);
  font-size: 13px;
  z-index: 100;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
}
</style>
