<template>
  <!--
    FeedView — persistent-card feed. A single white card frame stays fixed in
    the viewport; scrolling/gestures advance the active card index and swap the
    card content with a cross-fade + slide-up transition.

    Index driver: wheel events (desktop), touch-swipe (mobile), keyboard
    (ArrowDown / ArrowUp / Space / PageDown / PageUp). One gesture = one step.
    A per-gesture cooldown (ADVANCE_COOLDOWN_MS) prevents runaway advances from
    trackpad momentum.

    Infinite loading: when activeIndex approaches the end of the loaded list
    (within PREFETCH_THRESHOLD cards) the next page is fetched automatically.

    InterestChooser: injected as a virtual "card" after the 2nd real card
    (index 2 in the virtual list) when no stored interests exist. The chooser
    appears inside the same card frame; confirming/skipping reloads the feed.

    Person/org tap: emits 'selectPerson' / 'selectOrg' up to App.vue.

    Error states:
      • 503 → warming-up empty state
      • network error → generic error state
      • end of feed → reshuffle end state
  -->
  <div
    class="feed-view"
    ref="containerEl"
    @wheel="onWheel"
    @touchstart.passive="onTouchStart"
    @touchend.passive="onTouchEnd"
    @keydown="onKeydown"
    tabindex="0"
    aria-label="Connections feed"
    role="region"
  >
    <!-- ── Canvas dot-grid background (always visible) ── -->

    <!-- ── 503 / warming-up empty state ── -->
    <div v-if="status === 'warming'" class="feed-empty">
      <div class="feed-empty__inner">
        <div class="feed-empty__icon" aria-hidden="true">⏳</div>
        <h2 class="feed-empty__title">{{ t.feed.warmingUp }}</h2>
        <p class="feed-empty__body">{{ t.feed.warmingUpSub }}</p>
      </div>
    </div>

    <!-- ── Generic error ── -->
    <div v-else-if="status === 'error'" class="feed-empty">
      <div class="feed-empty__inner">
        <div class="feed-empty__icon" aria-hidden="true">⚠</div>
        <h2 class="feed-empty__title">Something went wrong</h2>
        <p class="feed-empty__body">Could not load the feed. Please try again later.</p>
        <button class="feed-retry-btn" @click="loadInitial">Retry</button>
      </div>
    </div>

    <!-- ── Main card stage ── -->
    <div v-else class="card-stage">

      <!-- Fixed card frame — content transitions inside -->
      <div class="card-frame" :class="{ 'card-frame--chooser': isChooserActive }">

        <!-- Loading skeleton while first page arrives -->
        <div v-if="status === 'loading' && virtualList.length === 0" class="card-skeleton">
          <div class="skeleton-pulse"></div>
        </div>

        <!-- Animated card content swap -->
        <Transition
          :name="transitionName"
          mode="out-in"
          @after-enter="onAfterEnter"
        >
          <!-- InterestChooser slot -->
          <div
            v-if="isChooserActive"
            key="chooser"
            class="card-inner"
          >
            <InterestChooser
              :inView="true"
              :initialSelected="interests"
              :initialNationality="nationality"
              :initialTypes="types"
              @confirm="onInterestConfirm"
              @skip="onInterestSkip"
            />
          </div>

          <!-- End-of-feed slot -->
          <div
            v-else-if="isEndActive"
            key="end"
            class="card-inner card-inner--end"
          >
            <div class="feed-end__inner">
              <p class="feed-end__title">{{ t.feed.endOfFeed }}</p>
              <p class="feed-end__sub">{{ t.feed.endOfFeedSub }}</p>
              <button class="feed-restart-btn" @click="restartFeed">
                {{ t.ui.scroll }}
              </button>
            </div>
          </div>

          <!-- Normal card -->
          <div
            v-else-if="activeCard"
            :key="activeCard.id"
            class="card-inner"
          >
            <FeedCard
              :card="activeCard"
              :inView="cardInView"
              @selectPerson="onSelectPerson"
              @selectOrg="onSelectOrg"
              @editInterests="openChooser"
            />
          </div>
        </Transition>

        <!-- Loading spinner overlay — next page fetch in progress -->
        <div v-if="status === 'loading' && virtualList.length > 0" class="card-loading-overlay" aria-hidden="true">
          <div class="spinner"></div>
        </div>

        <!-- Scroll-direction hint — anchored to the card, fades after first advance -->
        <Transition name="hint-fade">
          <div v-if="showScrollHint" class="scroll-hint" aria-hidden="true">
            <span class="scroll-hint__label">{{ t.ui.scroll }}</span>
            <!-- Double chevron for stronger affordance -->
            <div class="scroll-hint__chevrons">
              <svg class="scroll-hint__chevron scroll-hint__chevron--top" width="34" height="20" viewBox="0 0 34 20" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M5 5l12 10 12-10" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              <svg class="scroll-hint__chevron scroll-hint__chevron--bottom" width="34" height="20" viewBox="0 0 34 20" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M5 5l12 10 12-10" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </div>
          </div>
        </Transition>

        <!-- Bottom-right action cluster (share + settings) — anchored to the card -->
        <div v-if="activeCard" class="stage-actions" role="group" aria-label="Card actions">
          <button
            class="stage-action-btn"
            @click.stop="shareCard"
            :aria-label="t.feed.shareFeed"
            :title="t.feed.shareFeed"
          >
            <!-- Share icon -->
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24"
                 fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <circle cx="18" cy="5" r="3"/>
              <circle cx="6" cy="12" r="3"/>
              <circle cx="18" cy="19" r="3"/>
              <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/>
              <line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>
            </svg>
          </button>
          <button
            class="stage-action-btn"
            @click.stop="openChooser"
            :aria-label="t.feed.editInterests"
            :title="t.feed.editInterests"
          >
            <!-- Sliders icon -->
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24"
                 fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <line x1="4" y1="6" x2="20" y2="6"/>
              <line x1="4" y1="12" x2="20" y2="12"/>
              <line x1="4" y1="18" x2="20" y2="18"/>
              <circle cx="9"  cy="6"  r="2.5" fill="var(--bg)"/>
              <circle cx="15" cy="12" r="2.5" fill="var(--bg)"/>
              <circle cx="9"  cy="18" r="2.5" fill="var(--bg)"/>
            </svg>
          </button>

          <!-- "Link copied" toast -->
          <Transition name="toast-fade">
            <div v-if="shareToast" class="stage-copied-toast" role="status" aria-live="polite">
              {{ t.feed.linkCopied }}
            </div>
          </Transition>
        </div>
      </div>

    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useApi } from '../composables/useApi.js'
import { useLocale } from '../composables/useLocale.js'
import FeedCard from './FeedCard.vue'
import InterestChooser from './InterestChooser.vue'

const props = defineProps({
  // When set (from a shared deep link /?feed&card=<id>), this card is fetched
  // and shown FIRST, ahead of the normal paginated page.
  initialCardId: { type: String, default: null },
})

const emit = defineEmits(['selectPerson', 'selectOrg'])

const { t, locale } = useLocale()
const api = useApi()

// ── Constants ──────────────────────────────────────────────────────────────────

const LS_KEY = 'hm_feed_interests'
const LS_KEY_NATIONALITY = 'hm_feed_nationality'
const LS_KEY_TYPES = 'hm_feed_types'

// Default home nationality derived from UI locale.
// en is multi-country → Worldwide (''); all others map to a single ISO-2 code.
const LANG_TO_NATIONALITY = {
  it: 'IT',
  de: 'DE',
  es: 'ES',
  fr: 'FR',
  nl: 'NL',
  pl: 'PL',
  pt: 'PT',
  sv: 'SE',
  en: '',
}
// How many cards before the end to trigger a next-page fetch
const PREFETCH_THRESHOLD = 5
// Minimum ms between consecutive advances (prevents momentum scroll runaway)
const ADVANCE_COOLDOWN_MS = 380
// Minimum wheel delta to count as a scroll gesture
const WHEEL_THRESHOLD = 30
// Minimum touch swipe distance (px) to count as a swipe
const SWIPE_THRESHOLD = 45
// Reveal-then-remove delay for the inView animation reset
const IN_VIEW_RESET_DELAY = 80

// ── State ──────────────────────────────────────────────────────────────────────

const cards = ref([])
const total = ref(0)
const nextCursor = ref(null)
// status: 'idle' | 'loading' | 'warming' | 'error' | 'end'
const status = ref('idle')

// Active index into the virtualList
const activeIndex = ref(0)

// Whether the card content is "in view" (drives the sequential node reveal)
const cardInView = ref(false)

// Direction of advance: 'forward' | 'backward'
const advanceDir = ref('forward')

// Interest categories
const interests = ref([])
const interestsSet = ref(false)
const showChooser = ref(false)

// Nationality filter (ISO-2 string; '' = worldwide).
// Initialised on mount from localStorage; falls back to the language default.
const nationality = ref('')

// Card-type filter: array of archetype ids (relationship/dynasty/organization).
// Empty array (or all three) = no filter / show everything.
const types = ref([])

// When a nationality-filtered feed runs out, we transparently continue with the
// worldwide feed (nationality dropped, other filters kept) so it never dead-ends.
const worldwidePhase = ref(false)
// Card ids already shown — dedupes the worldwide tail against the national head.
const seenIds = new Set()

// Scroll hint (shown on entry, hidden after first advance)
const showScrollHint = ref(true)
// Whether the user has advanced at least once
const hasAdvanced = ref(false)

// Per-visit shuffle seed
const feedSeed = ref(Math.floor(Math.random() * 1e9))

// DOM ref
const containerEl = ref(null)

// Cooldown gate
let lastAdvanceAt = 0

// One-shot guard for the deep-linked initial card (props.initialCardId):
// honoured only on the first loadInitial(), so later reloads show the normal feed.
let deepLinkConsumed = false

// Wheel accumulation (for trackpad where individual delta is small)
let wheelAccum = 0
let wheelAccumTimer = null

// Touch state
let touchStartY = 0

// Share toast
const shareToast = ref(false)
let shareToastTimer = null

// Reduced motion
const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches

// ── Virtual list ───────────────────────────────────────────────────────────────
//
// The virtualList weaves real cards with a chooser entry (when needed) and an
// end entry (when the feed is exhausted). Each item has a `type` field:
//   'card'    — a real feed card
//   'chooser' — the InterestChooser
//   'end'     — the end-of-feed state

const virtualList = computed(() => {
  const list = []
  for (let i = 0; i < cards.value.length; i++) {
    list.push({ type: 'card', card: cards.value[i] })
    // Inject chooser after real-card index 1 (i.e. the second card)
    if (i === 1 && showChooser.value) {
      list.push({ type: 'chooser' })
    }
  }
  if (status.value === 'end' && cards.value.length > 0) {
    list.push({ type: 'end' })
  }
  return list
})

const activeItem = computed(() => virtualList.value[activeIndex.value] || null)
const activeCard = computed(() => activeItem.value?.type === 'card' ? activeItem.value.card : null)
const isChooserActive = computed(() => activeItem.value?.type === 'chooser')
const isEndActive = computed(() => activeItem.value?.type === 'end')

// ── Transition name (direction-aware) ─────────────────────────────────────────

const transitionName = computed(() => {
  if (prefersReducedMotion) return 'instant'
  return advanceDir.value === 'forward' ? 'card-up' : 'card-down'
})

// ── Interest persistence ───────────────────────────────────────────────────────

function loadInterestsFromStorage() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (raw !== null) {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        interests.value = parsed
        interestsSet.value = true
        showChooser.value = false
        return true
      }
    }
  } catch { /* ignore */ }
  return false
}

function saveInterests(ids) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(ids)) } catch { /* ignore */ }
}

function loadNationalityFromStorage() {
  try {
    const raw = localStorage.getItem(LS_KEY_NATIONALITY)
    if (raw !== null) {
      nationality.value = raw  // stored as a plain string (e.g. 'IT' or '')
      return
    }
  } catch { /* ignore */ }
  // No stored value — default from UI language
  nationality.value = LANG_TO_NATIONALITY[locale.value] ?? ''
}

function saveNationality(code) {
  try { localStorage.setItem(LS_KEY_NATIONALITY, code) } catch { /* ignore */ }
}

function loadTypesFromStorage() {
  try {
    const raw = localStorage.getItem(LS_KEY_TYPES)
    if (raw !== null) {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) types.value = parsed
    }
  } catch { /* ignore */ }
}

function saveTypes(ids) {
  try { localStorage.setItem(LS_KEY_TYPES, JSON.stringify(ids)) } catch { /* ignore */ }
}

// Send the types param only when it's a real subset (1–2 of 3). Empty or all
// three means "everything" → omit so the backend skips the filter.
function typesParam() {
  const t = types.value
  if (!Array.isArray(t) || t.length === 0 || t.length >= 3) return undefined
  return t
}

function openChooser() {
  // If chooser is already in the virtual list, jump to it
  const idx = virtualList.value.findIndex(v => v.type === 'chooser')
  if (idx !== -1) {
    advanceTo(idx)
  } else {
    // Force chooser on by re-enabling it
    showChooser.value = true
    nextTick(() => {
      const newIdx = virtualList.value.findIndex(v => v.type === 'chooser')
      if (newIdx !== -1) advanceTo(newIdx)
    })
  }
}

function onInterestConfirm({ categories, nationality: chosenNat, types: chosenTypes }) {
  saveInterests(categories)
  interests.value = categories
  interestsSet.value = true
  // Persist the chosen nationality and update state
  nationality.value = chosenNat ?? ''
  saveNationality(nationality.value)
  // Persist the chosen card types ([] = all/no filter)
  types.value = Array.isArray(chosenTypes) ? chosenTypes : []
  saveTypes(types.value)
  showChooser.value = false
  loadInitial()
}

function onInterestSkip() {
  saveInterests([])
  interests.value = []
  interestsSet.value = true
  // Skip = unfiltered feed: clear the card-type filter too
  types.value = []
  saveTypes(types.value)
  showChooser.value = false
  // Nationality stays at its current value (language default or previously set)
}

// ── Data fetching ──────────────────────────────────────────────────────────────

function categoriesParam() {
  return interests.value.length > 0 ? interests.value : undefined
}

// Nationality is dropped once we've fallen back to the worldwide tail.
function nationalityParam() {
  if (worldwidePhase.value) return undefined
  return nationality.value || undefined
}

// Append a page of cards, skipping any already shown (dedupe across the
// national → worldwide phase boundary). Returns how many were actually added.
function appendCards(newCards) {
  let added = 0
  for (const c of newCards) {
    if (c.id && seenIds.has(c.id)) continue
    if (c.id) seenIds.add(c.id)
    cards.value.push(c)
    added++
  }
  return added
}

async function loadInitial({ reshuffle = false } = {}) {
  status.value = 'loading'
  cards.value = []
  nextCursor.value = null
  showChooser.value = false
  activeIndex.value = 0
  worldwidePhase.value = false
  seenIds.clear()
  // Keep the shuffle seed STABLE across filter changes within a session so that
  // adding/removing a filter only changes what the filter dictates — it must
  // not randomly reorder the whole feed (which masked the nationality effect).
  // Only the explicit "restart" action requests a fresh shuffle.
  if (reshuffle) feedSeed.value = Math.floor(Math.random() * 1e9)
  // Reset card reveal
  cardInView.value = false

  // Deep-link target: fetch the specific shared card so it can be shown first.
  // Fetched in parallel with the normal page; failures fall back silently.
  // One-shot — only the first load (the actual deep-link arrival) honours it;
  // later reloads (interest change, restart) get the normal feed.
  const initialCardId = deepLinkConsumed ? null : props.initialCardId
  deepLinkConsumed = true
  const initialCardPromise = initialCardId
    ? api.getFeedCard(initialCardId).catch(() => null)
    : Promise.resolve(null)

  try {
    const data = await api.getFeed({
      cursor: 0,
      limit: 20,
      lang: locale.value,
      categories: categoriesParam(),
      seed: feedSeed.value,
      nationality: nationalityParam(),
      types: typesParam(),
    })
    let pageCards = data.cards

    // Prepend the deep-linked card (if any), de-duplicating by id so it isn't
    // shown twice when the normal page also happens to include it.
    const initialCard = await initialCardPromise
    if (initialCard && initialCard.id) {
      pageCards = [
        initialCard,
        ...pageCards.filter((c) => c.id !== initialCard.id),
      ]
    }

    cards.value = pageCards
    pageCards.forEach((c) => { if (c.id) seenIds.add(c.id) })
    total.value = data.total
    nextCursor.value = data.next_cursor
    status.value = nextCursor.value === null && cards.value.length === 0 ? 'end' : 'idle'
    // If the very first national page already exhausted the feed, fall straight
    // through to the worldwide tail instead of dead-ending.
    if (nextCursor.value === null && cards.value.length > 0) maybeContinueOrEnd()

    // Prepare chooser for new users after card index 1
    if (!interestsSet.value && cards.value.length >= 2) {
      showChooser.value = true
    }

    // Trigger card reveal for the first card
    await nextTick()
    triggerInView()
    // Warm the next cards' images immediately (activeIndex stays 0, so the
    // watcher won't fire — prefetch explicitly here).
    prefetchUpcoming()
  } catch (err) {
    if (err.message && err.message.includes('503')) {
      status.value = 'warming'
    } else {
      status.value = 'error'
    }
  }
}

async function loadNextPage() {
  if (status.value === 'loading' || nextCursor.value === null) return
  status.value = 'loading'
  try {
    const data = await api.getFeed({
      cursor: nextCursor.value,
      limit: 20,
      lang: locale.value,
      categories: categoriesParam(),
      seed: feedSeed.value,
      nationality: nationalityParam(),
      types: typesParam(),
    })
    appendCards(data.cards)
    total.value = data.total
    nextCursor.value = data.next_cursor
    status.value = 'idle'
    if (nextCursor.value === null) maybeContinueOrEnd()
  } catch {
    status.value = 'idle' // soft failure — user can keep advancing
  }
}

// Called when the current ordered list is exhausted. If a nationality filter is
// active and we haven't already, switch to the worldwide tail (drop nationality,
// keep the other filters) and keep going; otherwise it's the real end.
function maybeContinueOrEnd() {
  if (nationality.value && !worldwidePhase.value) {
    worldwidePhase.value = true
    nextCursor.value = 0          // restart the cursor for the worldwide list
    loadNextPage()               // fetch the first worldwide page now
  } else {
    status.value = 'end'
  }
}

function restartFeed() {
  loadInitial({ reshuffle: true })
}

// ── Card reveal (inView) ───────────────────────────────────────────────────────

function triggerInView() {
  if (prefersReducedMotion) {
    cardInView.value = true
    return
  }
  // Brief delay lets the enter-transition finish before starting the reveal
  setTimeout(() => {
    cardInView.value = true
  }, IN_VIEW_RESET_DELAY)
}

// ── Navigation ─────────────────────────────────────────────────────────────────

function canAdvance(dir) {
  const now = Date.now()
  if (now - lastAdvanceAt < ADVANCE_COOLDOWN_MS) return false
  if (dir === 'forward' && activeIndex.value >= virtualList.value.length - 1) {
    // At the very end — only possible if status === 'end' (end card is in list)
    return false
  }
  if (dir === 'backward' && activeIndex.value <= 0) return false
  return true
}

function advanceTo(targetIdx) {
  if (targetIdx === activeIndex.value) return
  advanceDir.value = targetIdx > activeIndex.value ? 'forward' : 'backward'
  lastAdvanceAt = Date.now()

  // Reset inView so the card re-animates
  cardInView.value = false

  activeIndex.value = targetIdx

  // Hide scroll hint after first advance
  if (!hasAdvanced.value) {
    hasAdvanced.value = true
    showScrollHint.value = false
  }

  // Prefetch next page if approaching the end
  const cardCount = cards.value.length
  const distFromEnd = cardCount - (activeIndex.value + 1)
  if (distFromEnd <= PREFETCH_THRESHOLD && nextCursor.value !== null && status.value === 'idle') {
    loadNextPage()
  }
}

function advance(dir) {
  if (!canAdvance(dir)) return
  const target = activeIndex.value + (dir === 'forward' ? 1 : -1)
  advanceTo(target)
}

// ── Gesture handlers ───────────────────────────────────────────────────────────

function onWheel(e) {
  // While the interest chooser is active, let it scroll its own (taller) content
  // natively instead of advancing the feed — otherwise scrolling to reach the
  // options/buttons skips the chooser entirely.
  if (isChooserActive.value) return
  // Block native page scroll only when we're actually driving the feed.
  e.preventDefault()
  // Accumulate wheel delta for trackpad (many small events) vs mouse wheel (one big event)
  const delta = Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX

  // Mouse wheel: large delta → advance immediately
  if (Math.abs(delta) >= WHEEL_THRESHOLD) {
    advance(delta > 0 ? 'forward' : 'backward')
    wheelAccum = 0
    clearTimeout(wheelAccumTimer)
    return
  }

  // Trackpad: accumulate
  wheelAccum += delta
  clearTimeout(wheelAccumTimer)
  wheelAccumTimer = setTimeout(() => { wheelAccum = 0 }, 300)

  if (Math.abs(wheelAccum) >= WHEEL_THRESHOLD) {
    advance(wheelAccum > 0 ? 'forward' : 'backward')
    wheelAccum = 0
    clearTimeout(wheelAccumTimer)
  }
}

function onTouchStart(e) {
  touchStartY = e.touches[0].clientY
}

function onTouchEnd(e) {
  // Don't let a swipe inside the chooser skip past it — let it scroll natively.
  if (isChooserActive.value) return
  const dy = touchStartY - e.changedTouches[0].clientY
  if (Math.abs(dy) >= SWIPE_THRESHOLD) {
    advance(dy > 0 ? 'forward' : 'backward')
  }
}

function onKeydown(e) {
  // Let the chooser's form controls own the keyboard; advance via its buttons.
  if (isChooserActive.value) return
  const fwd = ['ArrowDown', 'ArrowRight', 'Space', 'PageDown']
  const bwd = ['ArrowUp', 'ArrowLeft', 'PageUp']
  if (fwd.includes(e.code) || fwd.includes(e.key)) {
    e.preventDefault()
    advance('forward')
  } else if (bwd.includes(e.code) || bwd.includes(e.key)) {
    e.preventDefault()
    advance('backward')
  }
}

// ── Transition callbacks ───────────────────────────────────────────────────────

function onAfterEnter() {
  triggerInView()
}

// ── Share ─────────────────────────────────────────────────────────────────────

async function shareCard() {
  const id = activeCard.value?.id
  const url = id
    ? `${window.location.origin}/share?card=${encodeURIComponent(id)}`
    : `${window.location.origin}/?feed`
  const shareData = {
    title: 'HumansMap',
    text: t.value.feed.shareText || 'Explore surprising connections between public figures',
    url,
  }
  try {
    if (navigator.share) { await navigator.share(shareData); return }
  } catch { /* fall through */ }
  try {
    await navigator.clipboard.writeText(url)
    shareToast.value = true
    clearTimeout(shareToastTimer)
    shareToastTimer = setTimeout(() => { shareToast.value = false }, 2200)
  } catch {
    window.prompt(t.value.feed.shareCopyPrompt || 'Copy this link:', url)
  }
}

// ── Event handlers ─────────────────────────────────────────────────────────────

function onSelectPerson(qid, name) { emit('selectPerson', qid, name) }
function onSelectOrg(qid, name) { emit('selectOrg', qid, name) }

// ── Image prefetch ───────────────────────────────────────────────────────────
// Warm the browser cache with the NEXT cards' portraits so they appear
// instantly on advance instead of popping in after the card mounts (the main
// cause of "slow to render sometimes" — a cold image cache on advance).
const prefetchedImgs = new Set()

function cardQids(card) {
  if (!card) return []
  const out = []
  const push = (q) => { if (q) out.push(q) }
  if (card.type === 'relationship') {
    push(card.a?.qid); push(card.b?.qid)
    ;(card.children || []).forEach((c) => push(c.qid))
    ;(card.a?.other_relations || []).forEach((r) => push(r.qid))
    ;(card.b?.other_relations || []).forEach((r) => push(r.qid))
  } else if (card.type === 'dynasty') {
    ;(card.members || []).forEach((m) => push(m.qid))
  } else if (card.type === 'organization') {
    if (card.org?.has_logo) push(card.org.qid)
    ;(card.people || []).forEach((p) => push(p.qid))
  }
  return out.slice(0, 10) // cap — only the portraits a card actually shows
}

function prefetchCard(card) {
  for (const qid of cardQids(card)) {
    if (prefetchedImgs.has(qid)) continue
    prefetchedImgs.add(qid)
    const img = new Image()
    img.decoding = 'async'
    img.src = `/api/images/${qid}.jpg`
  }
}

// Prefetch the next two real cards ahead of the active one.
function prefetchUpcoming() {
  let found = 0
  for (let i = activeIndex.value + 1; i < virtualList.value.length && found < 2; i++) {
    const item = virtualList.value[i]
    if (item?.type === 'card') { prefetchCard(item.card); found++ }
  }
}

// Re-prefetch whenever the active card changes (covers every advance).
watch(activeIndex, prefetchUpcoming)

// ── Lifecycle ──────────────────────────────────────────────────────────────────

onMounted(async () => {
  loadInterestsFromStorage()
  loadNationalityFromStorage()
  loadTypesFromStorage()
  await loadInitial()
  // Do not steal focus on entry. Keyboard users reach this labelled region in
  // the normal tab order and receive the visible card-frame focus treatment.
})

onUnmounted(() => {
  clearTimeout(wheelAccumTimer)
  clearTimeout(shareToastTimer)
})

// Clamp activeIndex when virtualList shrinks (edge case on feed reload)
watch(virtualList, (list) => {
  if (activeIndex.value >= list.length && list.length > 0) {
    activeIndex.value = list.length - 1
  }
})

// Re-trigger inView when locale changes (card content refreshes)
watch(locale, () => {
  cardInView.value = false
  nextTick(() => triggerInView())
})
</script>

<style scoped>
/* ── Feed container ──────────────────────────────────────────────────────────── */
.feed-view {
  flex: 1;
  min-height: 0;
  width: 100%;
  height: 100%;
  position: relative;
  overflow: hidden;
  background: var(--canvas-bg);
  background-image: radial-gradient(circle, rgba(0, 0, 0, 0.065) 1px, transparent 1px);
  background-size: 32px 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  outline: none;
}
.feed-view:focus-visible {
  outline: none;
}
.feed-view:focus-visible .card-frame {
  outline: var(--focus-ring);
  outline-offset: 3px;
}

/* ── Card stage — centered column ────────────────────────────────────────────── */
.card-stage {
  width: 100%;
  max-width: 640px;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 56px clamp(12px, 4vw, 32px) 0;
  box-sizing: border-box;
  position: relative;
  gap: 12px;
}

/* On desktop the top bar is visible and the floating wordmark is hidden,
   so no padding-top reserve is needed. */
@media (min-width: 769px) {
  .card-stage { padding-top: 0; }
}

/* ── Fixed card frame — NEVER translates ─────────────────────────────────────── */
.card-frame {
  position: relative;
  width: 100%;
  /* Same height spec as before */
  height: clamp(460px, 82dvh, 680px);
  min-height: 0;
  max-height: 100%;
  /* Clip transitions within the frame */
  overflow: hidden;
  border-radius: 16px;
  background: var(--bg);
  box-shadow:
    0 2px 8px  rgba(28, 21, 16, 0.06),
    0 12px 36px rgba(28, 21, 16, 0.09);
  /* Idle float animation */
  animation: idle-float 4s ease-in-out infinite;
  animation-delay: 1.4s;
}

.card-frame--chooser {
  /* Slightly different shadow for the chooser */
  box-shadow:
    0 4px 16px rgba(28, 21, 16, 0.08),
    0 16px 48px rgba(28, 21, 16, 0.11);
}

@keyframes idle-float {
  0%,  100% { box-shadow: 0 2px 8px  rgba(28,21,16,.06), 0 12px 36px rgba(28,21,16,.09); }
  50%        { box-shadow: 0 6px 18px rgba(28,21,16,.09), 0 20px 48px rgba(28,21,16,.13); }
}

/* ── Desktop: enlarge the stage + frame so the feed reads as a confident
   centerpiece rather than a small phone card stranded in white space.
   Mobile/tablet (< 1024px) stay exactly as before. ──────────────────────────── */
@media (min-width: 1024px) {
  .card-stage {
    max-width: 820px;
    gap: 16px;
  }
  .card-frame {
    height: clamp(560px, 84dvh, 760px);
  }
  /* The card archetypes (Relationship / Organization / Dynasty) self-cap at
     max-width:640px. Lift that cap so they fill the wider desktop frame
     instead of leaving side gutters. */
  :deep(.feed-card) {
    max-width: 100%;
  }
}

/* ── Card inner — fills the frame ────────────────────────────────────────────── */
.card-inner {
  width: 100%;
  height: 100%;
  position: absolute;
  inset: 0;
}

/* Full-page feed test: the per-card "humansmap.com" footer is replaced by a
   single page-level brand footer at the bottom of the app. */
:deep(.card__footer) { display: none; }

/* ── End-of-feed inner content ───────────────────────────────────────────────── */
.card-inner--end {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg);
}
.feed-end__inner {
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 24px;
}
.feed-end__title {
  font-family: var(--font-display);
  font-size: 20px; font-weight: 700; color: var(--text); margin: 0;
}
.feed-end__sub {
  font-size: 13px; color: var(--text-muted); margin: 0;
}
.feed-restart-btn {
  margin-top: 8px;
  padding: 10px 24px;
  background: var(--accent); color: var(--text-inverse);
  border: none; border-radius: 8px;
  font-size: 14px; font-weight: 600;
  cursor: pointer; min-height: 44px;
}
.feed-restart-btn:hover { background: var(--accent-hover); }

/* ── Loading skeleton ────────────────────────────────────────────────────────── */
.card-skeleton {
  width: 100%; height: 100%;
  position: absolute; inset: 0;
  background: var(--bg);
  display: flex; align-items: center; justify-content: center;
}
.skeleton-pulse {
  width: 48px; height: 48px;
  border-radius: 50%;
  background: var(--surface);
  animation: pulse 1.4s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 0.5; transform: scale(0.92); }
  50%       { opacity: 1;   transform: scale(1); }
}

/* ── Loading overlay (next-page fetch while cards exist) ─────────────────────── */
.card-loading-overlay {
  position: absolute;
  bottom: 16px; right: 16px;
  z-index: 20;
  pointer-events: none;
}
.spinner {
  width: 22px; height: 22px;
  border: 2.5px solid rgba(28, 21, 16, 0.12);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Scroll-direction hint — anchored to the card bottom ─────────────────────── */
.scroll-hint {
  position: absolute;
  bottom: 18px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  pointer-events: none;
  z-index: 25;
  /* pill backdrop so hint reads over card content */
  background: rgba(249, 248, 244, 0.88);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  padding: 10px 24px 12px;
  border-radius: 28px;
  border: 1px solid rgba(27, 77, 142, 0.20);
  box-shadow: 0 4px 16px rgba(28, 21, 16, 0.14);
}
.scroll-hint__label {
  font-size: 13px;
  font-weight: 800;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--accent);
  white-space: nowrap;
}
/* Double-chevron cluster — larger + accent-coloured so it clearly reads
   as "scroll down" on the first card. */
.scroll-hint__chevrons {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0;
  animation: bounce-down 1.4s cubic-bezier(.37,0,.63,1) infinite;
}
.scroll-hint__chevron--top {
  color: rgba(27, 77, 142, 0.45);
  margin-bottom: -7px;
}
.scroll-hint__chevron--bottom {
  color: var(--accent);
}
@keyframes bounce-down {
  0%, 100% { transform: translateY(0); }
  50%       { transform: translateY(7px); }
}

.hint-fade-enter-active {
  transition: opacity 0.3s ease;
}
.hint-fade-leave-active {
  transition: opacity 0.5s ease;
}
.hint-fade-enter-from,
.hint-fade-leave-to {
  opacity: 0;
}

/* ── Bottom-right action cluster — anchored to the card corner ───────────────── */
/* A radial scrim behind the buttons ensures they read even if a card didn't
   fully reserve the safe zone (e.g. chooser or end-card). */
.stage-actions::before {
  content: '';
  position: absolute;
  inset: -20px -12px -12px -24px;
  background: radial-gradient(ellipse at bottom right, rgba(249,248,244,0.72) 40%, transparent 80%);
  border-radius: 16px;
  pointer-events: none;
  z-index: -1;
}
.stage-actions {
  position: absolute;
  bottom: 14px;
  right: 14px;
  z-index: 25;
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-end;
}

.stage-action-btn {
  width: 40px;
  height: 40px;
  min-height: 44px; /* touch target via auto-height */
  padding: 0;
  background: var(--bg-panel, var(--bg));
  border: 1px solid var(--border, rgba(28,21,16,.14));
  border-radius: 10px;
  color: rgba(28, 21, 16, 0.6);
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.10);
  transition: background 0.15s, color 0.15s, transform 0.1s;
}
.stage-action-btn svg { display: block; pointer-events: none; flex-shrink: 0; }
.stage-action-btn:hover { background: var(--bg); color: var(--text); }
.stage-action-btn:active { transform: scale(0.92); background: var(--surface); }
.stage-action-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.stage-copied-toast {
  position: absolute;
  bottom: calc(100% + 6px);
  right: 0;
  background: rgba(28, 21, 16, 0.88);
  color: var(--text-inverse);
  font-size: 12px; font-weight: 500;
  padding: 6px 12px;
  border-radius: 6px;
  white-space: nowrap;
  pointer-events: none;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18);
}

.toast-fade-enter-active,
.toast-fade-leave-active { transition: opacity 0.2s ease, transform 0.2s ease; }
.toast-fade-enter-from,
.toast-fade-leave-to { opacity: 0; transform: translateY(4px); }

/* ── Empty / warming states ──────────────────────────────────────────────────── */
.feed-empty {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  padding: 40px 24px;
}
.feed-empty__inner {
  max-width: 380px; text-align: center;
  display: flex; flex-direction: column; align-items: center; gap: 12px;
}
.feed-empty__icon  { font-size: 40px; line-height: 1; }
.feed-empty__title {
  font-family: var(--font-display); font-size: 20px; font-weight: 700;
  color: var(--text); margin: 0; line-height: 1.25;
}
.feed-empty__body  { font-size: 14px; color: var(--text-muted); line-height: 1.55; margin: 0; }
.feed-retry-btn {
  margin-top: 8px; padding: 10px 20px;
  background: var(--accent); color: var(--text-inverse);
  border: none; border-radius: 8px;
  font-size: 14px; font-weight: 600; cursor: pointer; min-height: 44px;
}
.feed-retry-btn:hover { background: var(--accent-hover); }

/* ── Card transition: forward (content slides up out, new slides up in) ────────*/
.card-up-enter-active {
  transition: transform 360ms cubic-bezier(.22,.61,.36,1), opacity 280ms cubic-bezier(.22,.61,.36,1);
}
.card-up-leave-active {
  transition: transform 280ms cubic-bezier(.55,.06,.68,.19), opacity 220ms ease;
}
.card-up-enter-from {
  transform: translateY(24px);
  opacity: 0;
}
.card-up-leave-to {
  transform: translateY(-20px);
  opacity: 0;
}

/* ── Card transition: backward (reverse) ────────────────────────────────────── */
.card-down-enter-active {
  transition: transform 360ms cubic-bezier(.22,.61,.36,1), opacity 280ms cubic-bezier(.22,.61,.36,1);
}
.card-down-leave-active {
  transition: transform 280ms cubic-bezier(.55,.06,.68,.19), opacity 220ms ease;
}
.card-down-enter-from {
  transform: translateY(-24px);
  opacity: 0;
}
.card-down-leave-to {
  transform: translateY(20px);
  opacity: 0;
}

/* ── Instant (reduced-motion) ────────────────────────────────────────────────── */
.instant-enter-active,
.instant-leave-active { transition: none !important; }
.instant-enter-from,
.instant-leave-to { opacity: 1 !important; transform: none !important; }

/* ── InterestChooser: flatten nested card when rendered inside .card-frame ─── */
/* The chooser-card has its own border/shadow/radius which creates a card-within-
   a-card effect. We neutralize those so it looks flush inside the frame. */
:deep(.chooser-card) {
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
  border-radius: 0 !important;
  /* Fill the card-frame height */
  width: 100% !important;
  max-width: 100% !important;
  height: 100% !important;
  overflow-y: auto;
  box-sizing: border-box;
  /* Suppress the chooser's own entrance animation — FeedView transition handles it */
  transform: none !important;
  opacity: 1 !important;
  transition: none !important;
  animation: none !important;
}

/* ── Reduced motion overrides ────────────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .card-frame {
    animation: none !important;
  }
  .scroll-hint__chevrons {
    animation: none !important;
  }
  .skeleton-pulse {
    animation: none !important;
    opacity: 0.6;
  }
  .spinner {
    animation: spin 1.2s linear infinite !important;
  }
}
</style>
