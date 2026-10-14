import { ref, computed, watch, onUnmounted } from 'vue'
import { useLocale } from './useLocale.js'

export const SHOWCASE_PEOPLE = [
  { qid: 'Q615',    name_en: 'Lionel Messi',  name_it: 'Lionel Messi',  description_en: 'Football legend',    description_it: 'Leggenda del calcio' },
  { qid: 'Q26876',  name_en: 'Taylor Swift',  name_it: 'Taylor Swift',  description_en: 'Singer-songwriter',  description_it: 'Cantautrice' },
  { qid: 'Q317521', name_en: 'Elon Musk',     name_it: 'Elon Musk',     description_en: 'Entrepreneur & CEO', description_it: 'Imprenditore' },
]

// ---------------------------------------------------------------------------
// Step definitions — kept deliberately simple.
// No step depends on a DOM element that may or may not be rendered.
// Spotlight targets are structural elements that always exist when the graph
// is loaded (.graph-legend, .search-bar, .share-btn).
// ---------------------------------------------------------------------------
const STEP_DEFS = {
  fresh: [
    { id: 'welcome', target: null,            spotlight: false, advanceOn: 'showcaseSelect' },
    { id: 'network', target: '.graph-wrapper', spotlight: false, advanceOn: 'next' },
    { id: 'legend',  target: '.graph-legend',  spotlight: false, advanceOn: 'next' },
    { id: 'explore', target: null,            spotlight: false, advanceOn: 'next' },
    { id: 'search',  target: '.search-bar',    spotlight: true,  advanceOn: 'next', allowInteract: true },
    { id: 'share',   target: null,             spotlight: false, advanceOn: 'done' },
  ],
  deeplink: [
    { id: 'network', target: '.graph-wrapper', spotlight: false, advanceOn: 'next' },
    { id: 'legend',  target: '.graph-legend',  spotlight: false, advanceOn: 'next' },
    { id: 'explore', target: null,            spotlight: false, advanceOn: 'next' },
    { id: 'search',  target: '.search-bar',    spotlight: true,  advanceOn: 'next', allowInteract: true },
    { id: 'share',   target: null,             spotlight: false, advanceOn: 'done' },
  ],
}
STEP_DEFS.help = STEP_DEFS.deeplink

// ---------------------------------------------------------------------------
// Composable
// ---------------------------------------------------------------------------
export function useOnboardingTour ({ graph, graphLoaded, rootPersonName }) {
  const { t } = useLocale()

  const active    = ref(false)
  const mode      = ref(null)
  const stepIndex = ref(0)

  let autoTimer = null

  const steps          = computed(() => (mode.value ? STEP_DEFS[mode.value] : []))
  const currentStepDef = computed(() => steps.value[stepIndex.value] ?? null)

  // -- Pulse node resolution ------------------------------------------------
  function resolvePulseNodeId (pulseType) {
    const cy = graph?.cy?.value
    if (!cy || !pulseType) return null
    const rootId = graph?.rootNodeId?.value
    if (pulseType === 'person') {
      const candidates = cy.nodes().filter(n =>
        n.data('entityType') === 'person' && n.data('qid') !== rootId
      )
      const withImage = candidates.filter(n => !!n.data('image_cached_path'))
      const chosen = withImage.length ? withImage[0] : (candidates.length ? candidates[0] : null)
      return chosen ? chosen.data('qid') : null
    }
    if (pulseType === 'organization') {
      const candidates = cy.nodes().filter(n =>
        n.data('entityType') === 'organization' && n.data('qid') !== rootId
      )
      return candidates.length ? candidates[0].data('qid') : null
    }
    return null
  }

  const pulseNodeId = computed(() => {
    if (!active.value || !currentStepDef.value) return null
    return resolvePulseNodeId(currentStepDef.value.pulseType ?? null)
  })

  // -- Timers ---------------------------------------------------------------
  function clearAutoTimer () {
    if (autoTimer !== null) { clearTimeout(autoTimer); autoTimer = null }
  }

  let targetPollTimer = null
  function clearTargetPoll () {
    if (targetPollTimer !== null) {
      clearTimeout(targetPollTimer)
      clearInterval(targetPollTimer)
      targetPollTimer = null
    }
  }

  // -- Step navigation ------------------------------------------------------
  function goToStep (index) {
    clearAutoTimer()
    clearTargetPoll()
    const list = steps.value
    if (index >= list.length) { completeTour(); return }

    const stepDef = list[index]

    // Poll for DOM target (structural elements like .search-bar / .share-btn
    // might lag one frame behind graphLoaded).
    if (stepDef.target && !document.querySelector(stepDef.target)) {
      let attempts = 0
      targetPollTimer = setInterval(() => {
        attempts++
        if (!active.value) { clearTargetPoll(); return }
        if (document.querySelector(stepDef.target) || attempts >= 10) {
          clearTargetPoll()
          stepIndex.value = index
        }
      }, 300)
      return
    }

    stepIndex.value = index
  }

  // -- Lifecycle ------------------------------------------------------------
  function completeTour () {
    clearAutoTimer()
    clearTargetPoll()
    if (typeof window !== 'undefined') localStorage.setItem('hm_onboarded', '1')
    active.value    = false
    mode.value      = null
    stepIndex.value = 0
  }

  function startTour (tourMode) {
    if (!STEP_DEFS[tourMode]) return
    clearAutoTimer()
    mode.value      = tourMode
    stepIndex.value = 0
    active.value    = true

    if (tourMode === 'fresh') {
      // Welcome overlay — no canvas needed
    } else if (graphLoaded.value) {
      goToStep(0)
    }
  }

  watch(graphLoaded, (loaded) => {
    if (loaded && active.value && mode.value !== 'fresh') {
      goToStep(stepIndex.value)
    }
  })

  // -- Public controls ------------------------------------------------------
  function advance () {
    if (!active.value) return
    goToStep(stepIndex.value + 1)
  }

  function skip () { completeTour() }

  function onShowcaseSelect () {
    if (!active.value) return
    if (currentStepDef.value?.advanceOn === 'showcaseSelect') {
      goToStep(stepIndex.value + 1)
    }
  }

  // -- Coach mark props (drives the UI) ------------------------------------
  const coachMarkProps = computed(() => {
    if (!active.value || !currentStepDef.value) return null
    const step = currentStepDef.value
    if (step.id === 'welcome') return null

    const tr   = t.value?.tour ?? {}
    const name = rootPersonName?.value ?? ''

    const isOrgCircle = graph?.layoutMode?.value === 'org-circle'
    const textMap = {
      network: (tr.network ?? '').replace('{name}', name).replace('{Name}', name),
      legend:  isOrgCircle ? (tr.legendOrg ?? tr.legend ?? '') : (tr.legend ?? ''),
      explore: tr.explore ?? tr.tapPerson ?? '',
      search:  tr.search  ?? '',
      share:   tr.share   ?? '',
    }

    const next = { label: tr.next  ?? 'Next',   type: 'next' }
    const skipBtn = { label: tr.skip  ?? 'Skip',   type: 'skip' }
    const done = { label: tr.gotIt ?? 'Got it', type: 'done' }

    const actionsMap = {
      network: [next, skipBtn],
      legend:  [next, skipBtn],
      explore: [next, skipBtn],
      search:  [next, skipBtn],
      share:   [done],
    }

    // Step counter: "1 / 5" — excludes the welcome step
    const offset = mode.value === 'fresh' ? 1 : 0
    const coachNum   = stepIndex.value - offset + 1
    const coachTotal = steps.value.length - offset

    return {
      target:    step.target,
      text:      textMap[step.id] ?? '',
      actions:   actionsMap[step.id] ?? [],
      position:  step.position ?? 'auto',
      spotlight: step.spotlight ?? false,
      stepLabel: `${coachNum} / ${coachTotal}`,
    }
  })

  // -- Cleanup --------------------------------------------------------------
  onUnmounted(() => { clearAutoTimer(); clearTargetPoll() })

  return {
    isActive:       computed(() => active.value),
    currentStep:    computed(() => stepIndex.value),
    currentStepId:  computed(() => currentStepDef.value?.id ?? null),
    coachMarkProps,
    pulseNodeId,
    showcasePeople: SHOWCASE_PEOPLE,
    startTour,
    advance,
    skip,
    onShowcaseSelect,
  }
}
