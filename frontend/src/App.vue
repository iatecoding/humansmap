<template>
  <div class="app" :class="{ 'app--feed-full': mode === 'feed' }">
    <!-- Full-page feed: minimal site link top-left (fills the top space and is
         the way back to Explore, since the menu bar is hidden in feed mode). -->
    <button
      v-if="mode === 'feed' && !graph.selectedNode.value && !dossier.open.value"
      class="feed-home-link"
      @click="goHome()"
      :aria-label="t.app.title + ' — home'"
    >{{ t.app.title }}</button>

    <AppHeader
      :title="t.app.title"
      :subtitle="t.app.subtitle"
      :mode="mode"
      :selectedName="rootPersonName"
      :feedFull="mode === 'feed'"
      @home="goHome()"
      @mode="setMode"
      @search="onSearchSelect"
    />

    <main
      id="main-content"
      class="main-area"
      :class="{
        'main-area--detail-open': graph.selectedNode.value || dossier.open.value,
        'main-area--person-open': graph.selectedNode.value && !dossier.open.value,
        'main-area--dossier-open': dossier.open.value,
      }"
    >
      <!-- ── Scroll / Feed mode — full-area, replaces graph chrome ─────────── -->
      <FeedView
        v-if="mode === 'feed'"
        class="feed-view-slot"
        :initialCardId="feedInitialCardId"
        @selectPerson="onFeedSelectPerson"
        @selectOrg="onFeedSelectOrg"
      />

      <!-- Sidebar: stats/leaderboard — hidden in parliament view, grid view, or feed mode -->
      <div v-if="mode === 'explore' && !gridVisible && !showHemicycle" class="sidebar-left" :class="{ 'sidebar-left--loaded': graphLoaded }">
        <StatsOverlay :active="showLeaderboard" @leaderboard="showLeaderboard = !showLeaderboard" />
        <Leaderboard
          v-if="showLeaderboard"
          :contextNationality="rootContextNationality"
          :contextOccupation="rootContextOccupation"
          @close="showLeaderboard = false"
          @explore="onLeaderboardExplore"
          @explore-occupation="onExploreOccupation"
        />
      </div>

      <!-- Legend: hidden in parliament view or grid view -->
      <GraphLegend
        v-if="mode === 'explore' && !gridVisible && !showHemicycle"
        :class="{ 'legend-hidden-mobile': !graphLoaded }"
        :layoutMode="graph.layoutMode.value"
        :hasFamilyEdges="graph.hasFamilyEdges.value"
        :familyColors="graph.activeFamilyColors.value"
        :rolesPresent="graph.orgCircleRolesPresent?.value"
      />

      <div
        v-if="mode !== 'feed'"
        class="graph-wrapper"
        role="region"
        aria-labelledby="graph-region-title"
        aria-describedby="graph-region-summary"
      >
        <span id="graph-region-title" class="sr-only">{{ t.app.title }} — {{ t.app.subtitle }}</span>
        <span id="graph-region-summary" class="sr-only" aria-live="polite">{{ graphAriaLabel }}</span>
        <!-- Empty states — hidden when grid view is active -->
        <div v-if="!graphLoaded && !gridVisible" class="empty-state-host">
          <WelcomeOverlay
            v-if="mode === 'explore' && tour.isActive.value && tour.currentStepId.value === 'welcome'"
            :showcasePeople="tour.showcasePeople"
            @select="onShowcaseSelect"
            @skip="tour.skip()"
          />
          <ExploreEmptyState
            v-else-if="mode === 'explore'"
            :suggestions="SUGGESTIONS"
            :loading="suggestionsLoading"
            :loadingQid="suggestionLoading"
            @select="loadSuggestion"
          />
        </div>

        <!-- Grid view of leaderboard on canvas -->
        <LeaderboardGrid
          v-if="gridVisible"
          @select="onGridSelect"
          @explore="onGridExplore"
          @explore-occupation="onExploreOccupation"
        />

        <div v-if="graph.loading.value" class="loading-overlay">
          <div class="spinner-lg"></div>
          <span>{{ t.graph.loading }}</span>
        </div>

        <GraphCanvas v-show="mode !== 'game'" :graph="graph" />

        <!-- Parliament hemicycle overlay — decorative GOVERNMENT/OPPOSITION labels only -->
        <HemicycleView
          v-if="showHemicycle && graph.rootNodeId.value"
          :gov-label-x="hemicycleLabelX.gov"
          :opp-label-x="hemicycleLabelX.opp"
        />

        <!-- Edge detail card -->
        <EdgeCard
          :edge="graph.selectedEdge.value"
          :cy="graph.cy.value"
          @close="graph.selectedEdge.value = null"
        />

        <!-- Onboarding pulse ring on target node -->
        <PulseRing
          v-if="tour.isActive.value && tour.pulseNodeId.value"
          :cy="graph.cy.value"
          :nodeId="tour.pulseNodeId.value"
        />

        <!-- Layout mode switcher — visible when org is focal node (desktop only) -->
        <div v-if="graphLoaded && canToggleOrgLayout" class="layout-mode-switcher">
          <button
            class="layout-mode-btn"
            :class="{ active: graph.layoutMode.value === 'network' && !showHemicycle }"
            @click="setLayoutMode('network'); showHemicycle = false"
            :title="t.ui.networkView"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
              <circle cx="8" cy="4" r="2"/><circle cx="3" cy="12" r="2"/><circle cx="13" cy="12" r="2"/>
              <line x1="8" y1="6" x2="3" y2="10"/><line x1="8" y1="6" x2="13" y2="10"/>
            </svg>
          </button>
          <button
            class="layout-mode-btn"
            :class="{ active: graph.layoutMode.value === 'org-circle' && !showHemicycle }"
            @click="setLayoutMode('org-circle'); showHemicycle = false"
            :title="t.ui.orgCircleView"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
              <circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="2.5"/>
            </svg>
          </button>
          <!-- Parliament hemicycle button — only shown for parliament/legislative orgs -->
          <button
            v-if="isParliamentOrg"
            class="layout-mode-btn"
            :class="{ active: showHemicycle }"
            @click="toggleHemicycle()"
            title="Parliament hemicycle view"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M2 13 A6 6 0 0 1 14 13" stroke-linecap="round"/>
              <circle cx="5" cy="10" r="1" fill="currentColor" stroke="none"/>
              <circle cx="8" cy="8" r="1" fill="currentColor" stroke="none"/>
              <circle cx="11" cy="10" r="1" fill="currentColor" stroke="none"/>
              <circle cx="3.5" cy="12.5" r="1" fill="currentColor" stroke="none"/>
              <circle cx="12.5" cy="12.5" r="1" fill="currentColor" stroke="none"/>
            </svg>
          </button>
        </div>

        <!-- Controls: zoom buttons + settings/saved/help panels -->
        <ControlsPanel
          :graphLoaded="graphLoaded"
          :bookmarks="trivia.bookmarks.value"
          :vizSettings="vizSettings"
          :imageUrl="api.imageUrl"
          :showLeaderboard="showLeaderboard"
          :rootQid="graph.rootNodeId.value"
          :rootName="rootPersonName"
          @update:showLeaderboard="showLeaderboard = $event"
          @zoomIn="zoomIn"
          @zoomOut="zoomOut"
          @zoomFit="zoomFit"
          @showHelp="tour.startTour(graphLoaded ? 'help' : 'fresh')"
          @savedExplore="onSavedExplore"
          @removeBookmark="trivia.toggleBookmark"
          @screenshot="exportScreenshot"
        />
      </div>

      <!-- Dossier panel replaces compact card when open -->
      <DossierPanel
        v-if="dossier.open.value"
        class="entity-detail-panel"
        :node="dossier.nodeData.value"
        @close="onDossierClose"
        @enter="onEnterNode"
      />
      <PersonCard
        v-else-if="graph.selectedNode.value"
        class="entity-detail-panel"
        :person="graph.selectedNode.value"
        :memberLimit="graph.orgMemberLimit(graph.selectedNode.value?.qid)"
        :layoutMode="mode === 'feed' ? 'network' : graph.layoutMode.value"
        @close="graph.selectedNode.value = null"
        @enter="onPersonCardEnter"
        @toggleOrgMembers="onToggleOrgMembers"
        @investigate="onInvestigate"
      />

      <!-- Trivia mode overlay — position:absolute fills the main-area -->
      <TriviaPage
        v-if="mode === 'game'"
        @explore="onTriviaExplore"
      />

      <!-- Onboarding tour coach mark -->
      <CoachMark
        v-if="tour.isActive.value && tour.currentStepId.value !== 'welcome' && tour.coachMarkProps.value"
        v-bind="tour.coachMarkProps.value"
        @action="onCoachMarkAction"
      />
    </main>

    <!-- Wikidata/Commons attribution footer — shown in every mode, including
         the full-page feed (fills the bottom space below the centred card). -->
    <footer class="attribution">
      {{ t.footer.data }} <a href="https://www.wikidata.org" target="_blank" rel="noopener">Wikidata</a> (CC0) &middot;
      {{ t.footer.images }} <a href="https://commons.wikimedia.org" target="_blank" rel="noopener">Wikimedia Commons</a> (CC BY-SA / CC0) &middot;
      <a href="#" @click.prevent="showAbout = true">{{ t.footer.about }}</a> &middot;
      &copy; {{ new Date().getFullYear() }} Humans Map
    </footer>

    <AboutModal v-if="showAbout" @close="showAbout = false" />
    <OnboardingModal v-if="showOnboarding" @complete="completeOnboarding" />
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from './composables/useApi.js'
import { useGraph } from './composables/useGraph.js'
import { useLocale } from './composables/useLocale.js'
import { useSettings } from './composables/useSettings.js'
import { useTrivia } from './composables/useTrivia.js'
import { useGraphNavigation, COLLAPSED_EDGE_TYPES } from './composables/useGraphNavigation.js'
import { useDossier } from './composables/useDossier.js'
import { useDocumentHead } from './composables/useDocumentHead.js'
import { slugify } from './utils/slug.js'
import { useScreenshot } from './composables/useScreenshot.js'
import { setEdgeColors } from './utils/cytoscape-config.js'

import AppHeader from './components/AppHeader.vue'
import GraphCanvas from './components/GraphCanvas.vue'
import PersonCard from './components/PersonCard.vue'
import DossierPanel from './components/DossierPanel.vue'
import TriviaPage from './components/TriviaPage.vue'
import StatsOverlay from './components/StatsOverlay.vue'
import GraphLegend from './components/GraphLegend.vue'
import AboutModal from './components/AboutModal.vue'
import OnboardingModal from './components/OnboardingModal.vue'
import CoachMark from './components/CoachMark.vue'
import WelcomeOverlay from './components/WelcomeOverlay.vue'
import PulseRing from './components/PulseRing.vue'
import { useOnboardingTour } from './composables/useOnboardingTour.js'
import Leaderboard from './components/Leaderboard.vue'
import ExploreEmptyState from './components/ExploreEmptyState.vue'
import EdgeCard from './components/EdgeCard.vue'
import ControlsPanel from './components/ControlsPanel.vue'
import LeaderboardGrid from './components/LeaderboardGrid.vue'
import HemicycleView from './components/HemicycleView.vue'
import FeedView from './components/FeedView.vue'

// ── Core state ────────────────────────────────────────────────────────────────
const route = useRoute()
const router = useRouter()
const api = useApi()
const graph = useGraph()
const { t, locale, prefName, prefDesc } = useLocale()
const trivia = useTrivia()
const vizSettings = useSettings()
const dossier = useDossier()
const documentHead = useDocumentHead()

const edgeTypes = ref([])
const activeEdgeTypes = ref(new Set())
const activeNodeTypes = ref(new Set(['person', 'organization', 'event']))
const graphLoaded = ref(false)
const mode = ref('explore')
const rootPersonName = ref(null)
// Deep-link target: when arriving via /?feed&card=<id> (shared card link),
// this id is passed down to FeedView so it opens that specific card first.
const feedInitialCardId = ref(null)
const { exporting, exportScreenshot } = useScreenshot({ graph, rootPersonName })
const tour = useOnboardingTour({ graph, graphLoaded, rootPersonName })

// ── UI overlay state ──────────────────────────────────────────────────────────
// Closed by default so it does NOT auto-open over a person's graph when you
// land on it. It's purely a user-controlled switch (StatsOverlay @leaderboard /
// ControlsPanel toggle). The default landing experience (the LeaderboardGrid on
// the canvas) is driven by `showGrid`, not this flag, so the grid is unaffected.
const showLeaderboard = ref(false)
const showAbout = ref(false)
const showOnboarding = ref(false)
function graphThemeOptions() {
  return {
    // Cytoscape cannot resolve CSS custom properties; keep these in sync with main.css.
    textOutlineColor: '#f2ede3',
    labelColor: '#1c1510',
    edgeLabelColor: '#6b5e52',
  }
}

// Guided tour is always available; grid intro banner handles first-visit orientation

// Grid leaderboard is the default landing
const showGrid = ref(true)
const gridVisible = computed(() => showGrid.value && !graphLoaded.value && mode.value === 'explore' && !tour.isActive.value)

function completeOnboarding() {
  localStorage.setItem('hm_onboarded', '1')
  showOnboarding.value = false
}

// ── Explore suggestions ───────────────────────────────────────────────────────
const SUGGESTIONS = ref([])
const suggestionsLoading = ref(false)
const suggestionLoading = ref(null)

async function fetchSuggestions() {
  suggestionsLoading.value = true
  try {
    const data = await api.getFeatured(locale.value, 6)
    SUGGESTIONS.value = data.people || []
  } catch {
    SUGGESTIONS.value = []
  } finally {
    suggestionsLoading.value = false
  }
}

fetchSuggestions()
watch(locale, fetchSuggestions)

async function loadSuggestion(person) {
  suggestionLoading.value = person.qid
  try {
    await onPersonSelect({ id: person.qid, name_it: person.name_it, name_en: person.name_en })
  } catch {
    // silently ignore — user can search manually
  } finally {
    suggestionLoading.value = null
  }
}

// ── Graph navigation composable ───────────────────────────────────────────────
const {
  nodeTypesParam,
  syncNewEdgeTypes,
  onPersonSelect,
  onEnterNode,
  onToggleOrgMembers,
} = useGraphNavigation({
  graph, api, activeEdgeTypes, activeNodeTypes, edgeTypes, graphLoaded, vizSettings, rootPersonName, router,
})

// ── Computed ──────────────────────────────────────────────────────────────────
const graphAriaLabel = computed(() => {
  if (!graphLoaded.value) return t.value.app.subtitle
  const cy = graph.cy.value
  if (!cy) return t.value.app.subtitle
  return `${cy.nodes().length} ${t.value.stats.totalNodes}, ${cy.edges().length} ${t.value.stats.totalEdges}`
})

const canToggleOrgLayout = computed(() => {
  if (!graph.rootNodeId.value) return false
  const rootNode = graph.cy.value?.$id(graph.rootNodeId.value)
  return rootNode?.data('entityType') === 'organization'
})

// True when the current root org is a parliament/congress/senate/assembly.
// Checks both the cy node label (when canvas is populated) and the current
// layoutMode (in parliament mode the root node is never on the canvas).
const isParliamentOrg = computed(() => {
  if (!graph.rootNodeId.value) return false
  // layoutMode=parliament is the authoritative signal after the short-circuit fires
  if (graph.layoutMode.value === 'parliament') return true
  if (showHemicycle.value) return true
  const cy = graph.cy.value
  if (!cy) return false
  const node = cy.$id(graph.rootNodeId.value)
  const name = node?.data('name_en') || node?.data('fullLabel') || ''
  return /parliament|congress|senate|assembl/i.test(name)
})

// Hemicycle overlay state
const showHemicycle = ref(false)
// Store previous layout mode so we can restore it when toggling off
let _preParliamentLayoutMode = 'org-circle'

// Screen-space x positions (px from canvas left) for GOVERNMENT / OPPOSITION labels,
// centered over their respective bench nodes. Updated whenever the bench centers change
// or the user pans/zooms.
const hemicycleLabelX = ref({ gov: null, opp: null })

function updateHemicycleLabelPositions() {
  const centers = graph.parliamentBenchCenters.value
  if (!centers || !graph.cy.value) return
  const govPos = graph.cy.value.modelToRenderedPosition({ x: centers.govX, y: centers.topY })
  const oppPos = graph.cy.value.modelToRenderedPosition({ x: centers.oppX, y: centers.topY })
  hemicycleLabelX.value = { gov: govPos.x, opp: oppPos.x }
}

watch(() => graph.parliamentBenchCenters.value, async () => {
  // nextTick ensures the Cytoscape fit:true pan/zoom is flushed before we convert coords
  await nextTick()
  updateHemicycleLabelPositions()
  graph.cy.value?.off('pan zoom', updateHemicycleLabelPositions)
  graph.cy.value?.on('pan zoom', updateHemicycleLabelPositions)
})

watch(showHemicycle, (active) => {
  if (!active) graph.cy.value?.off('pan zoom', updateHemicycleLabelPositions)
})

// Reset hemicycle state when the root node changes
watch(() => graph.rootNodeId.value, () => {
  showHemicycle.value = false
  _preParliamentLayoutMode = 'org-circle'
})

// Auto-activate parliament view when the short-circuit in loadNeighborhood
// detects a parliament org (sets layoutMode='parliament' + bumps graphVersion).
// The short-circuit clears the canvas and sets the flag; this watcher then
// calls loadParliamentLayout to populate the hemicycle seats.
watch(() => graph.graphVersion.value, async () => {
  // Only trigger when layoutMode was set to 'parliament' by the short-circuit
  // AND we haven't already activated the hemicycle view.
  if (showHemicycle.value) return
  if (graph.layoutMode.value !== 'parliament') return
  const qid = graph.rootNodeId.value
  if (!qid) return
  _preParliamentLayoutMode = 'org-circle'
  showHemicycle.value = true
  await graph.loadParliamentLayout(qid)
  graph.applyVisualSettings(vizSettings)
})

const rootContextNationality = computed(() => {
  if (!graph.rootNodeId.value || !graph.cy.value) return null
  const node = graph.cy.value.$id(graph.rootNodeId.value)
  const nats = node?.data('nationalities')
  return nats?.[0] || null
})

const rootContextOccupation = computed(() => {
  if (!graph.rootNodeId.value || !graph.cy.value) return null
  const node = graph.cy.value.$id(graph.rootNodeId.value)
  const occs = node?.data('occupations')
  return occs?.[0] || null
})

// ── Watchers ──────────────────────────────────────────────────────────────────
watch(locale, () => {
  graph.relabelNodes()
  graph.applyVisualSettings(vizSettings)
})

watch(() => [vizSettings.showEdges, vizSettings.showEdgeLabels, vizSettings.showNodeLabels, vizSettings.focusOnHover], () => {
  graph.applyVisualSettings(vizSettings)
})

// ── Initialisation ────────────────────────────────────────────────────────────
// Pre-warm nationality cache so Play tab renders instantly
trivia.fetchNationalities()

// ── Document head ─────────────────────────────────────────────────────────────
// The server renders correct <title>/meta on first load; client-side routing
// never touches them. Prefer entity data already in memory (dossier / selected
// graph node / root node) so navigation needs no extra API request, and fall
// back to a humanised slug until the graph finishes loading.
function _headCandidateNames() {
  const rootData = graph.rootNodeId.value
    ? graph.cy.value?.$id(graph.rootNodeId.value)?.data()
    : null
  return [dossier.nodeData.value, graph.selectedNode.value, rootData]
}

function _headName() {
  for (const candidate of _headCandidateNames()) {
    const name = prefName(candidate)
    if (name) return name
  }
  // Slug-derived name reflects the *current* route; rootPersonName can still
  // hold the previous entity during back/forward navigation.
  return documentHead.nameFromSlug(route.params.slug) || rootPersonName.value
}

function _headDescription() {
  for (const candidate of [dossier.nodeData.value, graph.selectedNode.value]) {
    const desc = prefDesc(candidate)
    if (desc) return desc
  }
  return ''
}

function updateDocumentHead() {
  if (route.name === 'home' || !route.params.slug) {
    documentHead.applyHome()
    return
  }
  documentHead.applyEntity({
    // The route decides the canonical type; entity data only supplies copy.
    entityType: route.name === 'organization' ? 'organization' : 'person',
    slug: route.params.slug,
    name: _headName(),
    description: _headDescription(),
  })
}

watch(
  () => [
    route.name,
    route.params.slug,
    dossier.nodeData.value,
    graph.selectedNode.value,
    graph.rootNodeId.value,
    graph.graphVersion.value,
    rootPersonName.value,
  ],
  updateDocumentHead,
  { immediate: true },
)

// React to browser back/forward by watching Vue Router's route params
watch(() => route.params.slug, async (slug, oldSlug) => {
  if (slug === oldSlug) return
  if (!slug) {
    // Back/forward landed on "/". If the entry carries ?feed, restore the feed
    // (so going Back from a card's visualization returns to the feed, not home).
    if (route.query.feed !== undefined || route.query.scroll !== undefined) {
      graph.selectedNode.value = null
      dossier.closeDossier?.()
      if (graph.cy.value) graph.cy.value.elements().remove()
      graph.resetState?.()
      graphLoaded.value = false
      mode.value = 'feed'
      showGrid.value = false
      return
    }
    if (graphLoaded.value) goHome({ push: false })
    return
  }
  const occMatch = String(slug).match(/^occ_(.+)$/)
  const qidMatch = String(slug).match(/^(Q\d+)/)
  const targetQid = occMatch ? `occ_${occMatch[1]}` : qidMatch?.[1]
  if (targetQid && targetQid !== graph.rootNodeId.value) {
    if (occMatch) {
      await onExploreOccupation(occMatch[1], { push: false })
    } else {
      await onEnterNode(targetQid, { push: false })
    }
  }
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
})

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  const urlParams = new URLSearchParams(window.location.search)
  if (urlParams.has('play') || urlParams.has('category') || urlParams.has('nationality')) {
    setMode('game')
  } else if (urlParams.has('feed') || urlParams.has('scroll')) {
    // Deep-link straight into the Scroll feed (shareable link target).
    // A `card` param (from /share?card=<id> → /?feed&card=<id>) opens that
    // specific card first; FeedView prepends it ahead of the normal page.
    const cardParam = urlParams.get('card')
    if (cardParam && /^[a-f0-9]{6,16}$/.test(cardParam)) {
      feedInitialCardId.value = cardParam
    }
    setMode('feed')
    showGrid.value = false
  }

  graph.restyle(graphThemeOptions())
  try {
    const data = await api.getEdgeTypes()
    edgeTypes.value = data.edge_types.filter((et) => !COLLAPSED_EDGE_TYPES.has(et.type))
    const types = edgeTypes.value.map((et) => et.type)
    setEdgeColors(types)
    activeEdgeTypes.value = new Set(types)
    graph.applyFilters(activeEdgeTypes.value, activeNodeTypes.value)
  } catch {
    // edge types will be empty
  }

  // Deep-link via path: /person/Q303-some-slug → extract QID and load graph
  const routeSlug = route.params.slug
  const routeQid = routeSlug ? (routeSlug.match(/^(Q\d+)(?:-.*)?$/) || [])[1] : null
  const routeOcc = routeSlug ? (routeSlug.match(/^occ_(.+)$/) || [])[1] : null

  if (routeOcc || routeQid || urlParams.get('person')) {
    showGrid.value = false
  }

  if (routeOcc) {
    try {
      await onExploreOccupation(routeOcc, { push: false })
    } catch {
      // silently ignore
    }
  } else {
    // Deep-link via query: ?person=Q303 — rewrite to canonical path form
    const personQid = routeQid || urlParams.get('person')
    if (personQid && /^Q\d+$/.test(personQid)) {
      // If we arrived via ?person=QID (legacy), rewrite URL to path form without reload
      if (!routeQid && urlParams.has('person')) {
        router.replace(`/person/${personQid}`)
      }
      try {
        await onPersonSelect({ wikidata_id: personQid }, { push: false })
      } catch {
        // silently ignore — user can search manually
      }
    }
  }

  // Guided tour for first-time deep-link visitors — grid visitors get the intro banner instead
  if (!localStorage.getItem('hm_onboarded') && route.params.slug) {
    tour.startTour('deeplink')
  }
})

// ── Navigation helpers ────────────────────────────────────────────────────────
function goHome({ push = true } = {}) {
  if (tour.isActive.value) tour.skip()
  dossier.closeDossier()
  mode.value = 'explore'
  graphLoaded.value = false
  graph.selectedNode.value = null
  graph.selectedEdge.value = null
  rootPersonName.value = null
  showHemicycle.value = false
  graph.cy.value?.nodes().grabify()
  graph.resetState()
  if (graph.cy.value) graph.cy.value.elements().remove()
  showGrid.value = true
  if (push) router.push('/')
}

function setMode(m) {
  if (tour.isActive.value) tour.skip()
  mode.value = m
  // Returning from Feed without a loaded graph must show the current grid
  // homepage, not the retired empty-state/suggestions screen.
  if (m === 'explore' && !graphLoaded.value) showGrid.value = true

  // Feed query state is meaningful only while Feed is active. Leaving it in
  // the URL makes refresh/back navigation resurrect an older homepage state.
  if (m !== 'feed' && route.query.feed !== undefined) {
    const nextQuery = { ...route.query }
    delete nextQuery.feed
    delete nextQuery.scroll
    delete nextQuery.card
    router.replace({ path: route.path, query: nextQuery })
  }

  // Give the feed its own history entry (/?feed) so that opening a card's
  // visualization and pressing Back returns to the feed, not the home graph.
  if (m === 'feed' && route.query.feed === undefined) {
    router.push({ path: '/', query: { feed: '1' } })
  }
}

async function setLayoutMode(lm) {
  if (lm === graph.layoutMode.value) return
  const qid = graph.rootNodeId.value
  if (!qid) return
  graph._layoutModeOverride.value = lm
  await graph.loadNeighborhood(qid, nodeTypesParam())
  syncNewEdgeTypes()
  graph.applyFilters(activeEdgeTypes.value, activeNodeTypes.value)
  graph.applyVisualSettings(vizSettings)
}

async function toggleHemicycle() {
  const qid = graph.rootNodeId.value
  if (!qid) return
  if (showHemicycle.value) {
    // Switch back to org-circle (or whatever was showing before)
    showHemicycle.value = false
    graph.cy.value?.nodes().grabify()
    graph._layoutModeOverride.value = _preParliamentLayoutMode
    await graph.loadNeighborhood(qid, nodeTypesParam())
    syncNewEdgeTypes()
    graph.applyFilters(activeEdgeTypes.value, activeNodeTypes.value)
    graph.applyVisualSettings(vizSettings)
  } else {
    _preParliamentLayoutMode = graph.layoutMode.value
    showHemicycle.value = true
    await graph.loadParliamentLayout(qid)
    graph.applyVisualSettings(vizSettings)
  }
}

function onGridSelect(item) {
  graph.selectedNode.value = {
    qid: item.qid,
    name_en: item.name_en,
    name_it: item.name_it,
    entityType: item.type ? 'organization' : 'person',
    type: item.type || undefined,
    occupations: item.occupations || [],
    nationalities: item.nationalities || [],
    birth_date: item.birth_date,
    founded: item.founded || undefined,
    hasImage: !!item.has_image,
    degree: item.degree,
    member_count: item.member_count,
  }
}

async function onPersonCardEnter(qid) {
  showGrid.value = false
  graph.selectedNode.value = null
  // If coming from feed mode, switch to explore so the graph is visible
  if (mode.value === 'feed') {
    mode.value = 'explore'
    // GraphCanvas is conditionally mounted. Let it recreate Cytoscape before
    // starting the entity request, otherwise a fast response can beat mount.
    await nextTick()
    await graph.whenReady()
  }
  try {
    await onPersonSelect({ id: qid })
  } catch {
    if (!graphLoaded.value) showGrid.value = true
  }
}

async function onGridExplore(qid) {
  showGrid.value = false
  try {
    await onEnterNode(qid)
  } catch {
    if (!graphLoaded.value) showGrid.value = true
  }
}

async function onLeaderboardExplore(qid) {
  showLeaderboard.value = false
  await onEnterNode(qid)
}

async function onExploreOccupation(occupation, { push = true } = {}) {
  showLeaderboard.value = false
  mode.value = 'explore'
  graphLoaded.value = false
  if (graph.cy.value) graph.cy.value.elements().remove()
  const virtualQid = `occ_${occupation}`
  const data = await api.getOccupationGraph(occupation)
  await graph.loadNeighborhood(virtualQid, {}, false, data)
  rootPersonName.value = graph.cy.value?.$id(virtualQid)?.data('name_it')
    || graph.cy.value?.$id(virtualQid)?.data('name_en')
    || graph.cy.value?.$id(virtualQid)?.data('label')
    || occupation.replace(/_/g, ' ')
  syncNewEdgeTypes()
  graph.applyFilters(activeEdgeTypes.value, activeNodeTypes.value)
  graph.applyVisualSettings(vizSettings)
  graphLoaded.value = true
  if (push) router.push(`/person/${virtualQid}`)
}

async function onTriviaExplore(person) {
  mode.value = 'explore'
  graphLoaded.value = false
  if (graph.cy.value) graph.cy.value.elements().remove()
  await graph.loadNeighborhood(person.qid, nodeTypesParam())
  rootPersonName.value = graph.cy.value?.$id(person.qid)?.data('name_it')
    || graph.cy.value?.$id(person.qid)?.data('name_en')
    || null
  syncNewEdgeTypes()
  graph.applyFilters(activeEdgeTypes.value, activeNodeTypes.value)
  graph.applyVisualSettings(vizSettings)
  graphLoaded.value = true
  const slug = slugify(rootPersonName.value || '')
  router.push(slug ? `/person/${person.qid}-${slug}` : `/person/${person.qid}`)
}

async function onSavedExplore(person) {
  mode.value = 'explore'
  graphLoaded.value = false
  if (graph.cy.value) graph.cy.value.elements().remove()
  await graph.loadNeighborhood(person.qid, nodeTypesParam())
  rootPersonName.value = graph.cy.value?.$id(person.qid)?.data('name_it')
    || graph.cy.value?.$id(person.qid)?.data('name_en')
    || null
  syncNewEdgeTypes()
  graph.applyFilters(activeEdgeTypes.value, activeNodeTypes.value)
  graph.applyVisualSettings(vizSettings)
  graphLoaded.value = true
  const slug2 = slugify(rootPersonName.value || '')
  router.push(slug2 ? `/person/${person.qid}-${slug2}` : `/person/${person.qid}`)
}

// ── Dossier handlers ──────────────────────────────────────────────────────────
function onInvestigate(nodeData) {
  dossier.openDossier(nodeData)
  graph.selectedNode.value = null
}

function onDossierClose() {
  dossier.closeDossier()
}

// ── Onboarding tour handlers ─────────────────────────────────────────────────
async function onShowcaseSelect(person) {
  tour.onShowcaseSelect(person)
  await onPersonSelect({ id: person.qid, name_en: person.name_en, name_it: person.name_it })
}

async function onSearchSelect(person) {
  if (tour.isActive.value) tour.advance()
  showGrid.value = false
  try {
    if (person._isOccupation) {
      await onExploreOccupation(person.occupation)
    } else {
      await onPersonSelect(person)
    }
  } catch {
    if (!graphLoaded.value) showGrid.value = true
  }
}

function onCoachMarkAction(actionType) {
  if (actionType === 'next' || actionType === 'done') {
    // Close PersonCard before advancing so spotlight targets aren't obscured
    graph.selectedNode.value = null
    tour.advance()
  } else if (actionType === 'skip') {
    tour.skip()
  }
}

// Close dossier when user taps another node on the canvas
// (the existing node-tap handler sets graph.selectedNode; we watch for that)
watch(() => graph.selectedNode.value, (node) => {
  if (node && dossier.open.value) {
    dossier.closeDossier()
    // selectedNode stays set → PersonCard shows for new node
  }
})

// Close dossier on ESC key at app level
function onKeydown(e) {
  if (e.key === 'Escape' && tour.isActive.value) {
    tour.skip()
    return
  }
  if (e.key === 'Escape' && dossier.open.value) {
    dossier.closeDossier()
  }
}

// ── Graph tap behaviour ───────────────────────────────────────────────────────
// Single-tap selection is handled inside useGraph; double-tap enters the node.
watch(() => graph.cy.value, (cyInstance) => {
  if (!cyInstance) return
  cyInstance.off('dbltap', 'node')
  cyInstance.on('dbltap', 'node', async (e) => {
    const qid = e.target.data('qid')
    if (!qid) return
    // Skip synthetic parliament squares (Independent, Other) — no org page exists
    if (e.target.data('_isSynthetic')) return
    // Skip non-Wikidata-QID nodes (aisle, ring-circles, etc.)
    if (!/^Q\d+$/.test(qid)) return
    if (tour.isActive.value && tour.currentStepId.value === 'explore') tour.advance()
    await onEnterNode(qid)
  })
})

// ── Feed (Scroll mode) person/org tap handlers ────────────────────────────────
// Fetch the full person/org doc so PersonCard shows Overview text, occupations, etc.
// Falls back to a minimal object if the API call fails.
async function onFeedSelectPerson(qid, name) {
  try {
    const doc = await api.getPerson(qid)
    // The API returns wikidata_id; map it to qid so PersonCard avatar / lightbox work.
    graph.selectedNode.value = {
      ...doc,
      qid: doc.wikidata_id || qid,
      entityType: 'person',
      hasImage: !!(doc.image_cached_path || doc.has_image),
    }
  } catch {
    // Graceful fallback — PersonCard will still open without overview text
    graph.selectedNode.value = {
      qid,
      name_en: name,
      name_it: name,
      entityType: 'person',
      hasImage: true,
    }
  }
}

async function onFeedSelectOrg(qid, name) {
  try {
    // GET /api/organizations/{qid} returns the raw org doc with name_en, type, founded, etc.
    const doc = await api.getOrganization(qid)
    graph.selectedNode.value = {
      ...doc,
      // The base org endpoint sets id = _key; normalise to qid
      qid: doc.id || doc._key || qid,
      name_en: doc.name_en || doc.label_en || name,
      name_it: doc.name_it || doc.label_it || name,
      entityType: 'organization',
      hasImage: !!(doc.has_logo || doc.has_image),
    }
  } catch {
    // 404 or network error — open a minimal card (no overview, but actions still work)
    graph.selectedNode.value = {
      qid,
      name_en: name,
      name_it: name,
      entityType: 'organization',
      hasImage: false,
    }
  }
}

// ── Zoom controls ─────────────────────────────────────────────────────────────
function _zoomAround(factor) {
  const cy = graph.cy.value
  if (!cy) return
  const level = cy.zoom() * factor
  const rootNode = graph.rootNodeId.value ? cy.$id(graph.rootNodeId.value) : null
  if (rootNode && rootNode.length) {
    cy.zoom({ level, renderedPosition: rootNode.renderedPosition() })
  } else {
    cy.zoom(level)
  }
}
function zoomIn() { _zoomAround(1.3) }
function zoomOut() { _zoomAround(1 / 1.3) }
function zoomFit() { if (graph.cy.value) graph.cy.value.fit(undefined, 30) }
</script>

<style scoped>
.app {
  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100svh;
  background: var(--bg);
  color: var(--text);
}
.main-area {
  flex: 1;
  display: flex;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  position: relative;
  isolation: isolate;
  --detail-panel-offset: 0px;
}
.main-area--person-open {
  --detail-panel-offset: 364px;
}
.main-area--dossier-open {
  --detail-panel-offset: 464px;
}
.sidebar-left {
  position: absolute;
  top: 12px;
  left: 12px;
  z-index: var(--z-floating-ui);
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 300px;
}
.graph-wrapper {
  flex: 1;
  min-width: 0;
  min-height: 0;
  position: relative;
  z-index: var(--z-canvas);
  isolation: isolate;
  overflow: hidden;
  background: var(--canvas-bg);
  background-image: radial-gradient(circle, rgba(0, 0, 0, 0.065) 1px, transparent 1px);
  background-size: 32px 32px;
}
.empty-state-host {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: var(--z-canvas-overlay);
}
.loading-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: var(--canvas-bg-overlay);
  z-index: var(--z-loading);
  gap: 12px;
  color: rgba(28, 21, 16, 0.55);
  font-size: 14px;
}
.spinner-lg {
  width: 32px;
  height: 32px;
  border: 3px solid rgba(28, 21, 16, 0.12);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
.layout-mode-switcher {
  position: absolute;
  bottom: 12px;
  bottom: max(12px, env(safe-area-inset-bottom, 0px));
  left: 12px;
  display: flex;
  gap: 2px;
  z-index: var(--z-floating-ui);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 3px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}
.layout-mode-btn {
  width: var(--touch-target);
  height: var(--touch-target);
  background: none;
  border: none;
  border-radius: 7px;
  color: rgba(28, 21, 16, 0.45);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s, color 0.2s;
}
.layout-mode-btn:hover {
  background: rgba(0, 0, 0, 0.06);
  color: rgba(28, 21, 16, 0.7);
}
.layout-mode-btn.active {
  background: rgba(27, 77, 142, 0.1);
  color: var(--accent);
}
/* Feed view: fills the .main-area flex slot exactly like .graph-wrapper.
   NOTE: this class merges onto FeedView's root element (which IS the scroll
   container), so we must NOT set overflow:hidden here — it would override the
   scroller's overflow-y:scroll and break scrolling (only the first card shows). */
.feed-view-slot {
  flex: 1;
  min-width: 0;
  min-height: 0;
  position: relative;
  z-index: var(--z-canvas);
  isolation: isolate;
}

.entity-detail-panel {
  z-index: var(--z-detail-panel);
}

.attribution {
  padding: 6px max(20px, env(safe-area-inset-right, 0px)) calc(6px + env(safe-area-inset-bottom, 0px)) max(20px, env(safe-area-inset-left, 0px));
  background: var(--surface);
  border-top: 1px solid var(--border-faint);
  font-size: 12px;
  line-height: 1.4;
  color: rgba(28, 21, 16, 0.45);
  text-align: center;
  flex-shrink: 0;
}
.attribution a {
  color: var(--accent);
  text-decoration: none;
}
.attribution a:hover {
  text-decoration: underline;
}

/* Full-page feed: minimal site link, top-left, floating over the feed.
   Fills the empty top space and is the way back to Explore. */
.feed-home-link {
  position: fixed;
  top: clamp(10px, 2.4vh, 20px);
  left: clamp(12px, 3vw, 24px);
  z-index: 30;
  background: none;
  border: none;
  padding: 4px 2px;
  /* Match the site brand wordmark (.brand h1) — same font/size/weight, blue */
  font-family: var(--font-display);
  font-size: 23px;
  font-weight: 700;
  letter-spacing: -0.5px;
  color: var(--accent);
  cursor: pointer;
  user-select: none;
  line-height: 1;
}
.feed-home-link:hover { color: var(--accent-hover); text-decoration: underline; }
.feed-home-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 4px; }

/* On desktop the top bar already shows the wordmark + tabs, so the floating
   feed wordmark is redundant. */
@media (min-width: 769px) {
  .feed-home-link { display: none; }

  .main-area--detail-open :deep(.graph-legend) {
    right: calc(12px + var(--detail-panel-offset));
  }
  .main-area :deep(.graph-legend) {
    transition: right 180ms cubic-bezier(0.22, 1, 0.36, 1), opacity 180ms ease;
  }
}

@media (min-width: 1024px) {
  .main-area--person-open {
    --detail-panel-offset: 404px;
  }
}

/* On compact tablets, removing secondary chrome while a detail panel is open
   preserves a useful graph viewport instead of squeezing three overlays into
   the same horizontal strip. */
@media (min-width: 769px) and (max-width: 1023px) {
  .main-area--detail-open .sidebar-left,
  .main-area--detail-open :deep(.graph-legend) {
    opacity: 0;
    pointer-events: none;
  }
}

/* Tablet responsive */
@media (max-width: 1024px) and (min-width: 769px) {
  .person-card {
    width: 280px;
  }
}

/* Mobile responsive */
@media (max-width: 768px) {
  .main-area {
    flex-direction: column;
  }
  .graph-wrapper {
    flex: 1;
    min-height: 0;
  }
  .layout-mode-switcher {
    display: none;
  }
  .sidebar-left {
    top: 8px;
    left: 8px;
    right: 8px;
    max-width: calc(100vw - 16px);
  }
  .sidebar-left--loaded {
    display: none;
  }
  :deep(.legend-hidden-mobile) {
    display: none;
  }
}
</style>
