<template>
  <div class="leaderboard-panel" @click.stop>
    <div class="lb-tabs" role="tablist">
      <button
        role="tab"
        class="lb-tab"
        :class="{ active: tab === 'persons' }"
        :aria-selected="tab === 'persons'"
        @click="switchTab('persons')"
      >{{ t.leaderboard.tabPersons }}</button>
      <button
        role="tab"
        class="lb-tab"
        :class="{ active: tab === 'orgs' }"
        :aria-selected="tab === 'orgs'"
        @click="switchTab('orgs')"
      >{{ t.leaderboard.tabOrgs }}</button>
    </div>

    <div v-if="tab === 'persons'" class="lb-filter lb-filter--row">
      <select class="lb-select" v-model="selectedStatus" @change="onFilterChange">
        <option value="living">{{ t.leaderboard.statusLiving }}</option>
        <option value="deceased">{{ t.leaderboard.statusDeceased }}</option>
        <option value="all">{{ t.leaderboard.statusAll }}</option>
      </select>
      <select class="lb-select" v-model="selectedNat" @change="onFilterChange">
        <option value="">{{ t.leaderboard.allNationalities }}</option>
        <option v-for="nat in translatedNationalities" :key="nat" :value="nat">{{ t.nationalities[nat] }}</option>
      </select>
      <select class="lb-select" v-model="selectedPersonCategory" @change="onFilterChange">
        <option value="">{{ t.leaderboard.allCategories }}</option>
        <option v-for="cat in personCategories" :key="cat" :value="cat">{{ (t.leaderboard.personCategories && t.leaderboard.personCategories[cat]) || cat }}</option>
        <option disabled>───</option>
        <option v-for="occ in personOccupations" :key="occ" :value="occ">{{ t.occupations[occ] || occ.replace(/_/g, ' ') }}</option>
      </select>
    </div>

    <div v-if="tab === 'orgs'" class="lb-filter">
      <select class="lb-select" v-model="selectedCategory" @change="onOrgCatChange">
        <option value="">{{ t.leaderboard.allOrgs }}</option>
        <option v-for="cat in orgCategories" :key="cat" :value="cat">{{ t.leaderboard.categories[cat] || cat }}</option>
      </select>
    </div>

    <div
      v-if="!(tab === 'orgs' && selectedCategory === 'occupations')"
      class="lb-filter lb-filter--sort"
    >
      <select class="lb-select lb-sort" v-model="selectedSort" :aria-label="t.leaderboard.sortBy" @change="onFilterChange">
        <option v-for="opt in sortOptions" :key="opt" :value="opt">{{ t.leaderboard[sortLabelKey(opt)] || opt }}</option>
      </select>
    </div>

    <div v-if="loading" class="lb-state">
      <span class="chip-spinner" aria-hidden="true"></span>
    </div>
    <p v-else-if="error" class="lb-state lb-error">{{ error }}</p>
    <p v-else-if="items.length === 0" class="lb-state lb-empty">{{ t.leaderboard.empty }}</p>

    <ol v-else class="lb-list">
      <li
        v-for="(item, i) in items"
        :key="item.qid"
        class="lb-item"
        role="button"
        tabindex="0"
        @click="onItemClick(item)"
        @keydown.enter="onItemClick(item)"
        @keydown.space.prevent="onItemClick(item)"
      >
        <span class="lb-rank" :class="rankClass(i)">{{ i + 1 }}</span>
        <div class="lb-avatar" :style="avatarBgStyle(item)">
          <img
            v-if="item.has_image && !imageError[item.qid]"
            :src="api.imageUrl(item.qid)"
            @error="imageError[item.qid] = true"
            class="lb-avatar-img"
            loading="lazy"
            alt=""
          />
          <span v-else>{{ initials(item) }}</span>
        </div>
        <div class="lb-info">
          <span class="lb-name">{{ itemName(item) }}</span>
          <span v-if="tab === 'persons' && firstOcc(item)" class="lb-sub">{{ firstOcc(item) }}</span>
          <span v-if="tab === 'orgs' && !isOccupationList && item.type" class="lb-sub">{{ translateOrgType(item.type) }}</span>
          <span v-if="isOccupationList && item.member_count" class="lb-sub">{{ item.member_count.toLocaleString() }}</span>
        </div>
        <span class="lb-metric" :title="metricTitle(item)">{{ metricDisplay(item) }}</span>
      </li>
    </ol>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, reactive, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../composables/useApi.js'
import { useLocale } from '../composables/useLocale.js'
import {
  ORG_SORT_DEFAULT,
  PERSON_SORT_DEFAULT,
  isValidSort,
  sortDefault,
  sortLabelKey,
  sortOptions as sortOptionsFor,
} from '../composables/leaderboardSorts.js'

const props = defineProps({
  contextNationality: { type: String, default: null },
  contextOccupation: { type: String, default: null },
})

const emit = defineEmits(['close', 'explore', 'explore-occupation'])

const api = useApi()
const { t, prefName } = useLocale()
const route = useRoute()
const router = useRouter()

const tab = ref('persons')
const selectedNat = ref('')
const selectedStatus = ref('living')
const selectedPersonCategory = ref('')
const selectedCategory = ref('')
const nationalities = ref([])
const SCOPE_NATIONALITIES = new Set([
  'AD','AL','AM','AT','AZ','BA','BE','BG','BY','CH','CY','CZ','DE','DK','EE',
  'ES','FI','FO','FR','GB','GE','GR','HR','HU','IE','IS','IT','LI','LT','LU',
  'LV','MC','MD','ME','MK','MT','NL','NO','PL','PT','RO','RS','RU','SE','SI',
  'SK','SM','TR','UA','VA','XK',
  'US','CA',
])
const translatedNationalities = computed(() => {
  const labels = t.value?.nationalities || {}
  return nationalities.value.filter(n => SCOPE_NATIONALITIES.has(n) && labels[n])
})
const orgCategories = ref([])
const personCategories = ref([])
const personOccupations = ref([])
const items = ref([])
const loading = ref(false)
const error = ref(null)
const imageError = reactive({})

const selectedPersonSort = ref(PERSON_SORT_DEFAULT)
const selectedOrgSort = ref(ORG_SORT_DEFAULT)

const sortOptions = computed(() => sortOptionsFor(tab.value))
const selectedSort = computed({
  get: () => (tab.value === 'orgs' ? selectedOrgSort.value : selectedPersonSort.value),
  set: (value) => {
    if (tab.value === 'orgs') selectedOrgSort.value = value
    else selectedPersonSort.value = value
  },
})

// Race-condition guard: stale responses are dropped; in-flight requests abort.
let requestId = 0
let abortController = null

// ── URL state sync ───────────────────────────────────────────────────────────

function readUrlState() {
  const q = route.query
  if (q.lb_tab === 'orgs' || q.lb_tab === 'persons') tab.value = q.lb_tab
  if (q.lb_status) selectedStatus.value = q.lb_status
  if (q.lb_nat) selectedNat.value = q.lb_nat
  if (q.lb_cat) {
    if (tab.value === 'persons') selectedPersonCategory.value = q.lb_cat
    else selectedCategory.value = q.lb_cat
  }
  if (q.lb_sort) {
    if (isValidSort('persons', q.lb_sort)) selectedPersonSort.value = q.lb_sort
    if (isValidSort('orgs', q.lb_sort)) selectedOrgSort.value = q.lb_sort
  }
  if (tab.value === 'orgs' && selectedCategory.value === 'occupations') {
    selectedOrgSort.value = ORG_SORT_DEFAULT
  }
}

function pushUrlState() {
  const params = {}
  params.lb_tab = tab.value
  if (selectedStatus.value && selectedStatus.value !== 'living') params.lb_status = selectedStatus.value
  if (selectedSort.value !== sortDefault(tab.value)) params.lb_sort = selectedSort.value
  if (tab.value === 'persons') {
    if (selectedNat.value) params.lb_nat = selectedNat.value
    if (selectedPersonCategory.value) params.lb_cat = selectedPersonCategory.value
  } else {
    if (selectedCategory.value) params.lb_cat = selectedCategory.value
  }
  const newQuery = { ...route.query }
  // Clear all lb_ params first
  for (const key of Object.keys(newQuery)) {
    if (key.startsWith('lb_')) delete newQuery[key]
  }
  Object.assign(newQuery, params)
  router.replace({ query: newQuery })
}

// ── Data loading ─────────────────────────────────────────────────────────────

async function loadPersons() {
  if (abortController) abortController.abort()
  abortController = new AbortController()
  const myId = ++requestId

  loading.value = true
  error.value = null
  try {
    const result = await api.getLeaderboard(
      selectedNat.value || null,
      selectedStatus.value,
      selectedPersonCategory.value || null,
      selectedPersonSort.value,
      100,
      0,
      abortController.signal
    )
    if (myId !== requestId) return
    items.value = result.items
  } catch (err) {
    if (err.name === 'AbortError') return
    if (myId !== requestId) return
    error.value = t.value.leaderboard.empty
  } finally {
    if (myId === requestId) loading.value = false
  }
}

async function loadOrgs() {
  if (abortController) abortController.abort()
  abortController = new AbortController()
  const myId = ++requestId

  loading.value = true
  error.value = null
  try {
    const result = await api.getOrgLeaderboard(
      selectedCategory.value || null,
      selectedOrgSort.value,
      100,
      0,
      abortController.signal
    )
    if (myId !== requestId) return
    items.value = result.items
  } catch (err) {
    if (err.name === 'AbortError') return
    if (myId !== requestId) return
    error.value = t.value.leaderboard.empty
  } finally {
    if (myId === requestId) loading.value = false
  }
}

function onFilterChange() {
  pushUrlState()
  tab.value === 'persons' ? loadPersons() : loadOrgs()
}

function onOrgCatChange() {
  if (selectedCategory.value === 'occupations') selectedOrgSort.value = ORG_SORT_DEFAULT
  onFilterChange()
}

const isOccupationList = computed(() => selectedCategory.value === 'occupations')

function onItemClick(item) {
  if (isOccupationList.value) {
    emit('explore-occupation', item.qid)
  } else {
    emit('explore', item.qid)
  }
}

function itemName(item) {
  if (item.type === 'occupation') {
    return t.value.occupations[item.name_en] || item.name_en.replace(/_/g, ' ')
  }
  return prefName(item)
}

function switchTab(newTab) {
  if (tab.value === newTab) return
  tab.value = newTab
  items.value = []
  selectedNat.value = ''
  selectedStatus.value = 'living'
  selectedPersonCategory.value = ''
  selectedCategory.value = ''
  selectedPersonSort.value = PERSON_SORT_DEFAULT
  selectedOrgSort.value = ORG_SORT_DEFAULT
  Object.keys(imageError).forEach(k => delete imageError[k])
  pushUrlState()
  newTab === 'persons' ? loadPersons() : loadOrgs()
}

// ── Metric display ───────────────────────────────────────────────────────────

function formatNumber(n) {
  if (n == null) return ''
  if (n >= 10000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k'
  return n.toLocaleString()
}

function metricDisplay(item) {
  if (tab.value === 'persons') return formatNumber(item.degree)
  if (isOccupationList.value) return ''
  return formatNumber(item.member_count)
}

function metricTitle(item) {
  if (tab.value === 'persons') {
    return `${item.degree || 0} ${t.value.leaderboard.connections || 'connections'}`
  }
  if (item.member_count != null) {
    return `${item.member_count.toLocaleString()} ${t.value.leaderboard.members || 'members'}`
  }
  return ''
}

// ── Context pre-filter ───────────────────────────────────��───────────────────

function applyContext() {
  if (props.contextNationality && !selectedNat.value) {
    if (SCOPE_NATIONALITIES.has(props.contextNationality)) {
      selectedNat.value = props.contextNationality
    }
  }
  if (props.contextOccupation && !selectedPersonCategory.value) {
    selectedPersonCategory.value = props.contextOccupation
  }
}

// ── Lifecycle ────────────────────────────────────────────────────────────────

onMounted(async () => {
  try {
    const [natData, orgCatData, personCatData] = await Promise.allSettled([
      api.getGameNationalities(),
      api.getOrgCategories(),
      api.getPersonCategories(),
    ])
    if (natData.status === 'fulfilled') nationalities.value = natData.value.nationalities || []
    if (orgCatData.status === 'fulfilled') orgCategories.value = orgCatData.value.categories || []
    if (personCatData.status === 'fulfilled') {
      personCategories.value = personCatData.value.categories || []
      personOccupations.value = personCatData.value.occupations || []
    }
  } catch { /* non-fatal */ }

  // URL params take priority over context props
  const hasUrlState = Object.keys(route.query).some(k => k.startsWith('lb_'))
  if (hasUrlState) {
    readUrlState()
  } else {
    applyContext()
  }

  tab.value === 'persons' ? await loadPersons() : await loadOrgs()
})

// React to context prop changes (e.g., user navigates to different person)
watch(() => [props.contextNationality, props.contextOccupation], () => {
  const hasUrlState = Object.keys(route.query).some(k => k.startsWith('lb_'))
  if (hasUrlState) return
  applyContext()
  if (tab.value === 'persons') loadPersons()
})

onUnmounted(() => {
  if (abortController) abortController.abort()
})

// ── Helpers ──────────────────────────────────────────────────────────────────

function initials(item) {
  const name = prefName(item) || '?'
  return name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase()
}

function avatarBgStyle(item) {
  if (item.has_image && !imageError[item.qid]) return {}
  return { backgroundColor: tab.value === 'orgs' ? 'var(--color-org)' : 'var(--color-person)' }
}

function firstOcc(item) {
  const occ = item.occupations?.[0]
  if (!occ) return ''
  return t.value.occupations[occ] || occ.replace(/_/g, ' ')
}

function translateOrgType(type) {
  return t.value.orgTypes[type] || type?.replace(/_/g, ' ') || ''
}

function rankClass(i) {
  if (i === 0) return 'rank-gold'
  if (i === 1) return 'rank-silver'
  if (i === 2) return 'rank-bronze'
  return ''
}
</script>

<style scoped>
.leaderboard-panel {
  background: var(--card);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  width: 300px;
  max-height: calc(100svh - 180px);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.12);
}
.lb-tabs {
  display: flex;
  gap: 0;
  padding: 6px 16px 0;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  margin-bottom: 0;
}
.lb-tab {
  flex: 1;
  padding: 7px 0;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  color: rgba(28, 21, 16, 0.45);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
  margin-bottom: -1px;
}
.lb-tab:hover {
  color: var(--text);
}
.lb-tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.lb-filter {
  padding: 10px 16px 6px;
  flex-shrink: 0;
}
.lb-filter--row {
  display: flex;
  gap: 6px;
}
.lb-filter--row .lb-select {
  flex: 1;
  min-width: 0;
}
.lb-filter--sort {
  padding-top: 0;
}
.lb-select {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%231c1510' stroke-opacity='.4' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 10px center;
  padding-right: 28px;
}
.lb-select:focus {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.lb-state {
  padding: 24px 16px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
  flex-shrink: 0;
}
.lb-error { color: #b03030; }
.lb-list {
  list-style: none;
  margin: 0;
  padding: 6px 0 8px;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
  scrollbar-width: thin;
  scrollbar-color: rgba(28, 21, 16, 0.18) transparent;
}
.lb-list::-webkit-scrollbar {
  width: 4px;
}
.lb-list::-webkit-scrollbar-track {
  background: transparent;
}
.lb-list::-webkit-scrollbar-thumb {
  background: rgba(28, 21, 16, 0.18);
  border-radius: 4px;
}
.lb-list::-webkit-scrollbar-thumb:hover {
  background: rgba(28, 21, 16, 0.32);
}
.lb-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 16px;
  cursor: pointer;
  transition: background 0.15s;
}
.lb-item:hover { background: rgba(0, 0, 0, 0.04); }
.lb-item:active { background: rgba(0, 0, 0, 0.08); }
.lb-rank {
  font-size: 12px;
  font-weight: 600;
  color: rgba(28, 21, 16, 0.35);
  min-width: 20px;
  text-align: right;
  flex-shrink: 0;
}
.lb-rank.rank-gold   { color: #c09000; }
.lb-rank.rank-silver { color: #8a8a8a; }
.lb-rank.rank-bronze { color: #a05030; }
.lb-avatar {
  width: 32px;
  height: 32px;
  border-radius: 6px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  color: #fff;
  overflow: hidden;
}
.lb-avatar-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center top;
  display: block;
}
.lb-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.lb-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.lb-sub {
  font-size: 12px;
  color: rgba(28, 21, 16, 0.5);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.lb-metric {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 600;
  color: rgba(28, 21, 16, 0.38);
  font-variant-numeric: tabular-nums;
  text-align: right;
  min-width: 32px;
}

@media (max-width: 768px) {
  .leaderboard-panel {
    width: 100%;
    border-radius: 0 0 8px 8px;
    border-top: none;
    max-height: 50vh;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.10);
  }
}
</style>
