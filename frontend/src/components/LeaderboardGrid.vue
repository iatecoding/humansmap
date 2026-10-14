<template>
  <div class="lb-grid-host" @click.stop>
    <!-- Filter bar -->
    <div class="lb-grid-bar">
      <div class="lb-grid-tabs" role="tablist">
        <button
          role="tab"
          class="lb-grid-tab"
          :class="{ active: tab === 'persons' }"
          :aria-selected="tab === 'persons'"
          aria-controls="lb-panel-persons"
          @click="switchTab('persons')"
        >{{ t.leaderboard.tabPersons }}</button>
        <button
          role="tab"
          class="lb-grid-tab"
          :class="{ active: tab === 'orgs' }"
          :aria-selected="tab === 'orgs'"
          aria-controls="lb-panel-orgs"
          @click="switchTab('orgs')"
        >{{ t.leaderboard.tabOrgs }}</button>
      </div>
      <select
        v-if="!(tab === 'orgs' && selectedOrgCat === 'occupations')"
        class="lb-grid-select lb-grid-sort"
        v-model="selectedSort"
        :aria-label="t.leaderboard.sortBy"
        @change="reload"
      >
        <option v-for="opt in sortOptions" :key="opt" :value="opt">
          {{ t.leaderboard[sortLabelKey(opt)] || opt }}
        </option>
      </select>
      <div v-if="tab === 'persons'" id="lb-panel-persons" role="tabpanel" class="lb-grid-filters">
        <select class="lb-grid-select" v-model="selectedStatus" :aria-label="t.leaderboard.statusLiving" @change="reload">
          <option value="living">{{ t.leaderboard.statusLiving }}</option>
          <option value="deceased">{{ t.leaderboard.statusDeceased }}</option>
          <option value="all">{{ t.leaderboard.statusAll }}</option>
        </select>
        <select class="lb-grid-select" v-model="selectedNat" :aria-label="t.leaderboard.allNationalities" @change="reload">
          <option value="">{{ t.leaderboard.allNationalities }}</option>
          <option v-for="nat in translatedNationalities" :key="nat" :value="nat">{{ t.nationalities[nat] }}</option>
        </select>
        <select class="lb-grid-select" v-model="selectedCat" :aria-label="t.leaderboard.allCategories" @change="reload">
          <option value="">{{ t.leaderboard.allCategories }}</option>
          <option v-for="cat in personCategories" :key="cat" :value="cat">{{ (t.leaderboard.personCategories && t.leaderboard.personCategories[cat]) || cat }}</option>
          <option disabled>───</option>
          <option v-for="occ in personOccupations" :key="occ" :value="occ">{{ t.occupations[occ] || occ.replace(/_/g, ' ') }}</option>
        </select>
      </div>
      <div v-if="tab === 'orgs'" id="lb-panel-orgs" role="tabpanel" class="lb-grid-filters">
        <select class="lb-grid-select" v-model="selectedOrgCat" :aria-label="t.leaderboard.allOrgs" @change="onOrgCatChange">
          <option value="">{{ t.leaderboard.allOrgs }}</option>
          <option v-for="cat in orgCategories" :key="cat" :value="cat">{{ t.leaderboard.categories[cat] || cat }}</option>
        </select>
      </div>
    </div>

    <div v-if="loading && allFetched.length === 0" class="lb-grid-loading">
      <span class="chip-spinner"></span>
    </div>
    <p v-else-if="!loading && visibleItems.length === 0" class="lb-grid-empty">{{ t.leaderboard.empty }}</p>

    <div v-else class="lb-grid-scroll" ref="scrollEl">
      <div class="lb-grid" :style="gridStyle">
        <template v-for="(item, i) in visibleItems" :key="item.qid">
          <div
            class="lb-cell"
            :class="{ 'lb-cell--selected': selectedQid === item.qid }"
            role="button"
            tabindex="0"
            @click="onCellClick(item)"
            @dblclick="onCellDblClick(item)"
            @keydown.enter="onCellClick(item)"
            @keydown.space.prevent="onCellClick(item)"
          >
            <span class="lb-cell-rank" :class="rankClass(i)">{{ i + 1 }}</span>
            <div class="lb-cell-avatar" :style="avatarBg(item)">
              <img
                v-if="item.has_image && !imgErr[item.qid]"
                :src="api.imageUrl(item.qid)"
                @error="imgErr[item.qid] = true"
                loading="lazy"
                alt=""
              />
              <span v-else class="lb-cell-initials">{{ initials(item) }}</span>
            </div>
            <span class="lb-cell-name">{{ itemName(item) }}</span>
            <span class="lb-cell-occ">{{ subtitle(item) }}</span>
          </div>
          <div v-if="isRowEnd(i)" class="lb-row-sep" />
        </template>
      </div>

      <!-- Sentinel for IntersectionObserver — triggers in-buffer reveal -->
      <div ref="sentinelEl" class="lb-sentinel" aria-hidden="true"></div>

      <!-- "Load more" button — only shown when buffer is exhausted and server has more -->
      <div v-if="showLoadMore" class="lb-load-more-wrap">
        <button
          v-if="!loadMoreError"
          class="lb-load-more-btn"
          :disabled="loading"
          @click="loadMore"
        >
          <span v-if="loading" class="chip-spinner"></span>
          <span v-else>{{ t.leaderboard.loadMore || 'Load more' }}</span>
        </button>
        <button v-else class="lb-load-more-btn lb-load-more-btn--retry" @click="loadMore">
          {{ t.leaderboard.retry || 'Retry' }}
        </button>
      </div>
    </div>

  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
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

const emit = defineEmits(['select', 'explore', 'explore-occupation'])

const api = useApi()
const { t, prefName } = useLocale()
const route = useRoute()
const router = useRouter()

// ── Filter state ────────────────────────────────────────────────────────────

const tab = ref('persons')
const selectedStatus = ref('living')
const selectedNat = ref('')
const selectedCat = ref('')
const selectedOrgCat = ref('')
const selectedPersonSort = ref(PERSON_SORT_DEFAULT)
const selectedOrgSort = ref(ORG_SORT_DEFAULT)
const selectedQid = ref(null)
const imgErr = reactive({})

const sortOptions = computed(() => sortOptionsFor(tab.value))
const selectedSort = computed({
  get: () => (tab.value === 'orgs' ? selectedOrgSort.value : selectedPersonSort.value),
  set: (value) => {
    if (tab.value === 'orgs') selectedOrgSort.value = value
    else selectedPersonSort.value = value
  },
})

// ── Category lists ──────────────────────────────────────────────────────────

const nationalities = ref([])
const personCategories = ref([])
const personOccupations = ref([])
const orgCategories = ref([])

// ── Infinite-scroll state ───────────────────────────────────────────────────

// Full 500-item buffer from last network fetch
const allFetched = ref([])
// How many items from allFetched are currently rendered
const revealedCount = ref(0)
// Tracks the next offset for fetching beyond the 500-item buffer
const deepOffset = ref(0)
// Whether the server indicated more results exist beyond the current buffer
const hasMore = ref(false)
// True while a network request is in flight
const loading = ref(true)
// Set when "Load more" network request fails
const loadMoreError = ref(false)
// Race-condition guard: each reload() increments this; stale responses are dropped
let requestId = 0
// AbortController for the in-flight request
let abortController = null

// ── Locked column count ─────────────────────────────────────────────────────
// Computed once from viewport width on first fetch; never reflows on reveal.

const lockedCols = ref(0)

function computeCols() {
  const w = window.innerWidth
  if (w < 640) return 4         // mobile — matches the CSS override
  if (w < 900) return 6
  if (w < 1100) return 7
  if (w < 1300) return 8
  if (w < 1500) return 9
  return 10
}

// ── Derived state ───────────────────────────────────────────────────────────

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

const isOccupationList = computed(() => selectedOrgCat.value === 'occupations')

const visibleItems = computed(() => allFetched.value.slice(0, revealedCount.value))

const gridStyle = computed(() => ({
  gridTemplateColumns: `repeat(${lockedCols.value || computeCols()}, 1fr)`,
}))

// Show "Load more" only when the in-memory buffer is exhausted AND server has more
const showLoadMore = computed(() =>
  visibleItems.value.length > 0 &&
  revealedCount.value >= allFetched.value.length &&
  hasMore.value
)

// Reveal batch size: smaller on narrow screens to avoid jank
function revealBatch() {
  return window.innerWidth < 640 ? 30 : 60
}

// ── IntersectionObserver ────────────────────────────────────────────────────

const sentinelEl = ref(null)
const scrollEl = ref(null)
let observer = null

function setupObserver() {
  if (observer) {
    observer.disconnect()
    observer = null
  }
  if (!sentinelEl.value) return

  observer = new IntersectionObserver(
    (entries) => {
      if (!entries[0].isIntersecting) return
      // If we still have items in the buffer, reveal more (no network)
      if (revealedCount.value < allFetched.value.length) {
        revealedCount.value = Math.min(
          revealedCount.value + revealBatch(),
          allFetched.value.length
        )
      }
      // If buffer exhausted + hasMore, showLoadMore computed handles the button
    },
    { root: scrollEl.value, rootMargin: '200px', threshold: 0 }
  )
  observer.observe(sentinelEl.value)
}

// ── Data loading ────────────────────────────────────────────────────────────

function resetScrollState() {
  allFetched.value = []
  revealedCount.value = 0
  deepOffset.value = 0
  hasMore.value = false
  loadMoreError.value = false
  selectedQid.value = null
}

async function reload() {
  // Cancel any in-flight request
  if (abortController) abortController.abort()
  abortController = new AbortController()
  const myId = ++requestId

  loading.value = true
  loadMoreError.value = false
  resetScrollState()
  pushUrlState()

  try {
    let data
    if (tab.value === 'persons') {
      data = await api.getLeaderboard(
        selectedNat.value || null,
        selectedStatus.value,
        selectedCat.value || null,
        selectedSort.value,
        500,
        0,
        abortController.signal
      )
    } else {
      data = await api.getOrgLeaderboard(selectedOrgCat.value || null, selectedSort.value, 500, 0, abortController.signal)
    }

    // Drop stale responses
    if (myId !== requestId) return

    allFetched.value = data.items || []
    hasMore.value = data.has_more ?? false
    deepOffset.value = 0
    // Lock cols from viewport on first fetch
    lockedCols.value = computeCols()
    // Reveal initial batch
    revealedCount.value = Math.min(revealBatch(), allFetched.value.length)
  } catch (err) {
    if (err.name === 'AbortError') return
    if (myId !== requestId) return
    allFetched.value = []
    hasMore.value = false
  } finally {
    if (myId === requestId) loading.value = false
  }
}

async function loadMore() {
  if (loading.value) return
  loadMoreError.value = false
  loading.value = true

  const nextOffset = deepOffset.value + 500
  const myId = requestId // don't increment — loadMore is not a filter change

  try {
    let data
    if (tab.value === 'persons') {
      data = await api.getLeaderboard(
        selectedNat.value || null,
        selectedStatus.value,
        selectedCat.value || null,
        selectedSort.value,
        500,
        nextOffset,
        abortController?.signal
      )
    } else {
      data = await api.getOrgLeaderboard(selectedOrgCat.value || null, selectedSort.value, 500, nextOffset, abortController?.signal)
    }

    if (myId !== requestId) return

    const newItems = data.items || []
    allFetched.value = [...allFetched.value, ...newItems]
    hasMore.value = data.has_more ?? false
    deepOffset.value = nextOffset
    // Reveal the newly appended batch
    revealedCount.value = Math.min(
      revealedCount.value + Math.min(revealBatch(), newItems.length),
      allFetched.value.length
    )
  } catch {
    if (myId !== requestId) return
    loadMoreError.value = true
  } finally {
    if (myId === requestId) loading.value = false
  }
}

// ── URL state ───────────────────────────────────────────────────────────────

function pushUrlState() {
  const params = {}
  params.lb_tab = tab.value
  if (selectedStatus.value && selectedStatus.value !== 'living') params.lb_status = selectedStatus.value
  if (selectedSort.value !== sortDefault(tab.value)) params.lb_sort = selectedSort.value
  if (tab.value === 'persons') {
    if (selectedNat.value) params.lb_nat = selectedNat.value
    if (selectedCat.value) params.lb_cat = selectedCat.value
  } else {
    if (selectedOrgCat.value) params.lb_cat = selectedOrgCat.value
  }
  const newQuery = { ...route.query }
  for (const key of Object.keys(newQuery)) {
    if (key.startsWith('lb_')) delete newQuery[key]
  }
  Object.assign(newQuery, params)
  router.replace({ query: newQuery })
}

function readUrlState() {
  const q = route.query
  if (q.lb_tab === 'orgs' || q.lb_tab === 'persons') tab.value = q.lb_tab
  if (q.lb_status) selectedStatus.value = q.lb_status
  if (q.lb_nat) selectedNat.value = q.lb_nat
  if (q.lb_cat) {
    if (tab.value === 'persons') selectedCat.value = q.lb_cat
    else selectedOrgCat.value = q.lb_cat
  }
  if (q.lb_sort) {
    if (isValidSort('persons', q.lb_sort)) selectedPersonSort.value = q.lb_sort
    if (isValidSort('orgs', q.lb_sort)) selectedOrgSort.value = q.lb_sort
  }
  // Occupation lists are always ranked by member count.
  if (tab.value === 'orgs' && selectedOrgCat.value === 'occupations') {
    selectedOrgSort.value = ORG_SORT_DEFAULT
  }
}

// ── Tab switching ───────────────────────────────────────────────────────────

function switchTab(newTab) {
  if (tab.value === newTab) return
  tab.value = newTab
  selectedNat.value = ''
  selectedStatus.value = 'living'
  selectedCat.value = ''
  selectedOrgCat.value = ''
  selectedPersonSort.value = PERSON_SORT_DEFAULT
  selectedOrgSort.value = ORG_SORT_DEFAULT
  Object.keys(imgErr).forEach(k => delete imgErr[k])
  reload()
}

function onOrgCatChange() {
  if (selectedOrgCat.value === 'occupations') selectedOrgSort.value = ORG_SORT_DEFAULT
  reload()
}

// ── Cell interactions ───────────────────────────────────────────────────────

function onCellClick(item) {
  if (isOccupationList.value) {
    emit('explore-occupation', item.qid)
    return
  }
  selectedQid.value = item.qid
  emit('select', item)
}

function onCellDblClick(item) {
  if (isOccupationList.value) return
  emit('explore', item.qid)
}

// ── Display helpers ─────────────────────────────────────────────────────────

function isRowEnd(i) {
  const cols = lockedCols.value || computeCols()
  return (i + 1) % cols === 0 && i < visibleItems.value.length - 1
}

function rankClass(i) {
  if (i === 0) return 'rank-gold'
  if (i === 1) return 'rank-silver'
  if (i === 2) return 'rank-bronze'
  return ''
}

function itemName(item) {
  if (item.type === 'occupation') {
    return t.value.occupations[item.name_en] || item.name_en.replace(/_/g, ' ')
  }
  return prefName(item)
}

function subtitle(item) {
  if (tab.value === 'persons') {
    const occ = item.occupations?.[0]
    return occ ? (t.value.occupations?.[occ] || occ.replace(/_/g, ' ')) : ''
  }
  if (isOccupationList.value && item.member_count) {
    return item.member_count.toLocaleString()
  }
  if (item.type && item.type !== 'occupation') {
    return t.value.orgTypes?.[item.type] || item.type.replace(/_/g, ' ')
  }
  return ''
}

function initials(item) {
  const name = prefName(item) || itemName(item) || '?'
  return name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase()
}

function avatarBg(item) {
  if (item.has_image && !imgErr[item.qid]) return {}
  const color = tab.value === 'orgs' ? '#8a8278' : '#bf6b28'
  return { backgroundColor: color }
}

// ── Lifecycle ───────────────────────────────────────────────────────────────

// Re-setup observer after visibleItems grow (sentinel may move)
watch(visibleItems, () => {
  // Let Vue flush the DOM first, then reattach observer
  setTimeout(setupObserver, 0)
})

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

  const hasUrlState = Object.keys(route.query).some(k => k.startsWith('lb_'))
  if (hasUrlState) readUrlState()

  await reload()
})

onUnmounted(() => {
  if (observer) observer.disconnect()
  if (abortController) abortController.abort()
})
</script>

<style scoped>
.lb-grid-host {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  z-index: 4;
  background: var(--canvas-bg);
  background-image: radial-gradient(circle, rgba(0, 0, 0, 0.065) 1px, transparent 1px);
  background-size: 32px 32px;
}

/* ── Filter bar ──────────────────────────────────────────────────────────── */

.lb-grid-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 20px;
  flex-shrink: 0;
  border-bottom: 1px solid var(--border-faint);
}

.lb-grid-tabs {
  display: flex;
  background: var(--border-faint);
  border-radius: 7px;
  padding: 2px;
  flex-shrink: 0;
}
.lb-grid-tab {
  padding: 8px 14px;
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  border-radius: 5px;
  transition: background 0.2s, color 0.2s;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.lb-grid-tab.active {
  background: var(--accent);
  color: #fff;
}
.lb-grid-tab:hover:not(.active) {
  color: var(--text);
}

.lb-grid-filters {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
  margin-left: auto;
}
.lb-grid-select {
  padding: 8px 30px 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 7px;
  background: var(--bg-panel);
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.01em;
  cursor: pointer;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%231c1510' stroke-opacity='.35' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 7px center;
  transition: border-color 0.15s, color 0.15s;
  line-height: 1.4;
}
.lb-grid-select:hover {
  border-color: var(--border-visible);
  color: var(--text);
}
.lb-grid-select:focus {
  border-color: var(--accent);
  outline: none;
}
.lb-grid-sort {
  flex-shrink: 0;
}

.lb-grid-loading {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.lb-grid-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  font-size: 14px;
}

.lb-grid-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 0 68px 20px 20px;
  scrollbar-width: thin;
  scrollbar-color: rgba(28, 21, 16, 0.15) transparent;
}

.lb-grid {
  display: grid;
  gap: 0;
  max-width: 1200px;
  margin: 0 auto;
}

.lb-row-sep {
  grid-column: 1 / -1;
  height: 1px;
  background: var(--border-faint);
  margin: 2px 0;
}

.lb-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 10px 4px;
  cursor: pointer;
  border-radius: 8px;
  transition: background 0.15s;
  position: relative;
}
.lb-cell:hover {
  background: rgba(28, 21, 16, 0.05);
}
.lb-cell--selected {
  background: rgba(27, 77, 142, 0.08);
}
.lb-cell--selected:hover {
  background: rgba(27, 77, 142, 0.12);
}

.lb-cell-rank {
  font-size: 10px;
  font-weight: 700;
  color: rgba(28, 21, 16, 0.4);
  font-variant-numeric: tabular-nums;
  position: absolute;
  top: 4px;
  left: 6px;
}
.lb-cell-rank.rank-gold   { color: #c09000; }
.lb-cell-rank.rank-silver { color: #8a8a8a; }
.lb-cell-rank.rank-bronze { color: #a05030; }

.lb-cell-avatar {
  width: 72px;
  height: 72px;
  border-radius: 6px;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  background: var(--surface);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.1);
}
.lb-cell-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center top;
}
.lb-cell-initials {
  font-size: 19px;
  font-weight: 700;
  color: #fff;
}

.lb-cell-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  text-align: center;
  line-height: 1.25;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.lb-cell-occ {
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
  line-height: 1.2;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lb-cell--selected .lb-cell-avatar {
  box-shadow: 0 0 0 2px var(--accent), 0 2px 6px rgba(0, 0, 0, 0.12);
}

/* ── Sentinel (zero-height intersection target) ─────────────────────────── */
.lb-sentinel {
  height: 1px;
  width: 100%;
  pointer-events: none;
}

/* ── Load more ──────────────────────────────────────────────────────────── */
.lb-load-more-wrap {
  display: flex;
  justify-content: center;
  padding: 20px 0 12px;
}

.lb-load-more-btn {
  padding: 10px 28px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--bg-panel);
  color: var(--text-muted);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 120px;
  justify-content: center;
}
.lb-load-more-btn:hover:not(:disabled) {
  background: var(--surface);
  border-color: var(--border-visible);
  color: var(--text);
}
.lb-load-more-btn:disabled {
  opacity: 0.6;
  cursor: default;
}
.lb-load-more-btn--retry {
  border-color: #b03030;
  color: #b03030;
}
.lb-load-more-btn--retry:hover {
  background: rgba(176, 48, 48, 0.06);
}

@media (max-width: 768px) {
  .lb-grid-bar {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 6px 8px;
    align-items: center;
    padding: 10px 12px;
  }
  .lb-grid-filters {
    display: contents;
  }
  .lb-grid-select {
    width: 100%;
    min-width: 0;
  }
  .lb-grid-filters > .lb-grid-select:nth-child(1) { order: 3; }
  .lb-grid-filters > .lb-grid-select:nth-child(2) { order: 2; }
  .lb-grid-filters > .lb-grid-select:nth-child(3) { order: 1; }
  .lb-grid-scroll {
    padding: 0 68px 12px 12px;
  }
  .lb-grid {
    grid-template-columns: repeat(4, 1fr) !important;
  }
  .lb-cell {
    padding: 10px 2px;
  }
  .lb-cell-avatar {
    width: 58px;
    height: 58px;
  }
  .lb-cell-initials {
    font-size: 17px;
  }
  .lb-cell-name {
    font-size: 12px;
  }
  .lb-cell-occ {
    font-size: 11px;
  }
}
</style>
