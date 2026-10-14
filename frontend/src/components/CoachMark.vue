<template>
  <Teleport to="body">
    <!-- Spotlight overlay -->
    <div
      v-if="spotlight && targetEl"
      class="coach-spotlight"
      :style="spotlightStyle"
    />

    <!-- Tooltip -->
    <Transition name="coach-mark">
      <div
        v-if="show"
        :key="target"
        class="coach-mark"
        :class="`coach-mark--${resolvedPosition}`"
        :style="tooltipStyle"
        role="dialog"
        aria-live="polite"
      >
        <!-- Step counter -->
        <span v-if="stepLabel" class="coach-mark__step">{{ stepLabel }}</span>

        <p class="coach-mark__text" v-html="text" />

        <div v-if="actions.length" class="coach-mark__actions">
          <button
            v-for="action in sortedActions"
            :key="action.type"
            class="coach-mark__btn"
            :class="`coach-mark__btn--${action.type}`"
            @click="emit('action', action.type)"
          >{{ action.label }}</button>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'

const props = defineProps({
  target:    { type: String,  default: null },
  text:      { type: String,  default: '' },
  actions:   { type: Array,   default: () => [] },
  position:  { type: String,  default: 'auto' },
  spotlight: { type: Boolean, default: false },
  stepLabel: { type: String,  default: '' },
})

const emit = defineEmits(['action'])

const show             = ref(false)
const targetEl         = ref(null)
const tooltipStyle     = ref({})
const spotlightStyle   = ref({})
const resolvedPosition = ref('bottom')

const sortedActions = computed(() => {
  const order = { next: 0, done: 0, skip: 1 }
  return [...props.actions].sort((a, b) => (order[a.type] ?? 2) - (order[b.type] ?? 2))
})

// ─── Target resolution with retry ────────────────────────────────────────────
let retryTimer = null

function clearRetry () {
  if (retryTimer !== null) { clearInterval(retryTimer); retryTimer = null }
}

function resolveTarget (sel) {
  clearRetry()
  if (!sel) { targetEl.value = null; return }
  targetEl.value = document.querySelector(sel) || null
  if (!targetEl.value) {
    let attempts = 0
    retryTimer = setInterval(() => {
      attempts++
      const el = document.querySelector(sel)
      if (el || attempts >= 6) {
        clearRetry()
        if (el) {
          targetEl.value = el
          reposition()
          setupObserver()
        }
      }
    }, 250)
  }
}

// ─── Target z-index elevation (so children like dropdowns sit above spotlight)
let savedZIndex = null
let elevatedEl = null

function elevateTarget (el) {
  restoreTarget()
  if (!el) return
  savedZIndex = el.style.zIndex || ''
  el.style.position = el.style.position || 'relative'
  el.style.zIndex = '2001'
  elevatedEl = el
}

function restoreTarget () {
  if (elevatedEl) {
    elevatedEl.style.zIndex = savedZIndex ?? ''
    elevatedEl = null
    savedZIndex = null
  }
}

// ─── ResizeObserver ──────────────────────────────────────────────────────────
let resizeObs = null

function setupObserver () {
  resizeObs?.disconnect()
  if (!targetEl.value) return
  resizeObs = new ResizeObserver(() => reposition())
  resizeObs.observe(targetEl.value)
  window.addEventListener('resize', reposition, { passive: true })
  window.addEventListener('scroll', reposition, { passive: true })
}

function teardownObserver () {
  resizeObs?.disconnect()
  resizeObs = null
  window.removeEventListener('resize', reposition)
  window.removeEventListener('scroll', reposition)
}

// ─── Positioning ─────────────────────────────────────────────────────────────
const TOOLTIP_W  = 320
const TOOLTIP_H  = 140
const ARROW      = 8
const GAP        = 16
const MOBILE_BP  = 768

function reposition () {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const mobile = vw < MOBILE_BP

  if (!targetEl.value) {
    resolvedPosition.value = 'center'
    tooltipStyle.value = {
      position: 'fixed',
      top: '50%', left: '50%',
      transform: 'translate(-50%, -50%)',
      maxWidth: mobile ? `calc(100vw - ${GAP * 2}px)` : `${TOOLTIP_W}px`,
    }
    spotlightStyle.value = {}
    return
  }

  const rect = targetEl.value.getBoundingClientRect()
  const space = {
    top:    rect.top - GAP,
    bottom: vh - rect.bottom - GAP,
    left:   rect.left - GAP,
    right:  vw - rect.right - GAP,
  }

  let dir = props.position
  if (dir === 'auto') {
    if (mobile) {
      dir = space.bottom >= space.top ? 'bottom' : 'top'
    } else {
      dir = ['bottom', 'top', 'right', 'left']
        .reduce((best, d) => space[d] > space[best] ? d : best, 'bottom')
    }
  } else if (mobile && (dir === 'left' || dir === 'right')) {
    dir = space.bottom >= space.top ? 'bottom' : 'top'
  }
  resolvedPosition.value = dir

  const maxW = mobile ? vw - GAP * 2 : TOOLTIP_W
  let x, y

  if (dir === 'bottom')     { x = rect.left + rect.width / 2 - maxW / 2;  y = rect.bottom + ARROW + 4 }
  else if (dir === 'top')   { x = rect.left + rect.width / 2 - maxW / 2;  y = rect.top - TOOLTIP_H - ARROW - 4 }
  else if (dir === 'right') { x = rect.right + ARROW + 4;                  y = rect.top + rect.height / 2 - TOOLTIP_H / 2 }
  else                      { x = rect.left - maxW - ARROW - 4;            y = rect.top + rect.height / 2 - TOOLTIP_H / 2 }

  x = Math.max(GAP, Math.min(x, vw - maxW - GAP))
  y = Math.max(GAP, Math.min(y, vh - TOOLTIP_H - GAP))

  tooltipStyle.value = {
    position: 'fixed',
    left: `${x}px`, top: `${y}px`,
    maxWidth: `${maxW}px`,
    transform: 'none',
  }

  if (props.spotlight) {
    const pad = 6
    spotlightStyle.value = {
      position: 'fixed',
      left:   `${rect.left - pad}px`,
      top:    `${rect.top - pad}px`,
      width:  `${rect.width + pad * 2}px`,
      height: `${rect.height + pad * 2}px`,
      borderRadius: '8px',
      boxShadow: '0 0 0 9999px rgba(28,21,16,0.45)',
      pointerEvents: 'none',
      zIndex: 1999,
    }
    elevateTarget(targetEl.value)
  } else {
    spotlightStyle.value = {}
    restoreTarget()
  }
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────
watch(
  () => props.target,
  async (sel) => {
    restoreTarget()
    teardownObserver()
    resolveTarget(sel)
    await nextTick()
    reposition()
    setupObserver()
  },
  { immediate: false }
)

onMounted(async () => {
  resolveTarget(props.target)
  await nextTick()
  reposition()
  setupObserver()
  requestAnimationFrame(() => { show.value = true })
})

onUnmounted(() => {
  teardownObserver()
  clearRetry()
  restoreTarget()
})
</script>

<style scoped>
.coach-mark {
  position: fixed;
  z-index: 2000;
  max-width: 320px;
  width: max-content;
  background: var(--bg-panel-solid, rgba(249,248,244,0.97));
  border: 1px solid var(--border, rgba(0,0,0,0.15));
  border-radius: 12px;
  box-shadow: 0 4px 16px rgba(28,21,16,0.12);
  padding: 16px 20px;
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 14px;
  color: var(--text, #1c1510);
  line-height: 1.55;
  pointer-events: auto;
}

@media (max-width: 767px) {
  .coach-mark { max-width: calc(100vw - 32px); }
}

/* ── Step counter ─────────────────────────────────────────────────────────── */
.coach-mark__step {
  display: block;
  font-size: 11px;
  font-weight: 600;
  color: rgba(28,21,16,0.35);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin-bottom: 6px;
}


/* ── Text ─────────────────────────────────────────────────────────────────── */
.coach-mark__text { margin: 0 0 14px; }
.coach-mark__text:last-child { margin-bottom: 0; }

/* ── Actions ──────────────────────────────────────────────────────────────── */
.coach-mark__actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}

.coach-mark__btn {
  font-family: 'DM Sans', system-ui, sans-serif;
  cursor: pointer;
  line-height: 1;
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 600;
  padding: 8px 18px;
  transition: background 0.15s, border-color 0.15s;
}

.coach-mark__btn--next,
.coach-mark__btn--done {
  color: #fff;
  background: var(--accent, #1b4d8e);
  border: 1.5px solid var(--accent, #1b4d8e);
}
.coach-mark__btn--next:hover,
.coach-mark__btn--done:hover {
  background: var(--accent-hover, #153d73);
  border-color: var(--accent-hover, #153d73);
}

.coach-mark__btn--skip {
  color: rgba(28,21,16,0.5);
  background: transparent;
  border: 1.5px solid rgba(28,21,16,0.15);
  font-weight: 500;
}
.coach-mark__btn--skip:hover {
  background: rgba(28,21,16,0.05);
  border-color: rgba(28,21,16,0.25);
}

/* ── Transitions ──────────────────────────────────────────────────────────── */
.coach-mark-enter-active { transition: opacity 0.25s ease-out, transform 0.25s ease-out; }
.coach-mark-leave-active { transition: opacity 0.15s ease-in; }
.coach-mark-enter-from   { opacity: 0; transform: translateY(8px); }
.coach-mark-leave-to     { opacity: 0; }

@media (prefers-reduced-motion: reduce) {
  .coach-mark-enter-active,
  .coach-mark-leave-active { transition: opacity 0.1s ease; }
  .coach-mark-enter-from { transform: none; }
}
</style>
