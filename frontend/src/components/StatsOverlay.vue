<template>
  <div v-if="stats" class="db-stats">
    <div class="db-stats-row">
      <span>{{ stats.persons?.toLocaleString(dateLocale) }} {{ t.stats.persons.toLowerCase() }}</span>
      <span>{{ stats.organizations?.toLocaleString(dateLocale) }} {{ t.stats.organizations.toLowerCase() }}</span>
    </div>
    <button
      class="db-stats-lb"
      :class="{ active }"
      @click="$emit('leaderboard')"
    >{{ t.leaderboard.title }} <span class="db-stats-lb-arrow" aria-hidden="true">›</span></button>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useApi } from '../composables/useApi.js'
import { useLocale } from '../composables/useLocale.js'

defineProps({ active: { type: Boolean, default: false } })
defineEmits(['leaderboard'])

const api = useApi()
const { t, dateLocale } = useLocale()
const stats = ref(null)

onMounted(async () => {
  try {
    stats.value = await api.getStats()
  } catch {
    // ignore
  }
})
</script>

<style scoped>
.db-stats {
  display: flex;
  flex-direction: column;
  gap: 0;
  font-size: 11px;
  color: var(--text-muted);
  margin: 0;
  padding: 6px 10px 0;
  width: fit-content;
  min-width: 160px;
  background: var(--bg-panel-solid, rgba(249,248,244,0.92));
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  backdrop-filter: blur(4px);
  box-shadow: 0 1px 6px rgba(28,21,16,0.07);
}
.db-stats-row {
  display: flex;
  flex-direction: row;
  gap: 8px;
  padding-bottom: 6px;
}
.db-stats-row span + span::before {
  content: '·';
  margin-right: 8px;
  color: var(--border-visible);
}
.db-stats-lb {
  background: none;
  border: none;
  border-top: 1px solid var(--border-subtle);
  padding: 5px 0 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  cursor: pointer;
  text-align: left;
  letter-spacing: 0.01em;
  width: 100%;
  transition: color 0.15s;
}
.db-stats-lb:hover,
.db-stats-lb.active {
  color: var(--accent-hover);
}
.db-stats-lb-arrow {
  font-size: 13px;
  line-height: 1;
  transition: transform 0.15s;
  display: inline-block;
}
.db-stats-lb.active .db-stats-lb-arrow {
  transform: rotate(90deg);
}
</style>
