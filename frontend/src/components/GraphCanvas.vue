<template>
  <div ref="containerEl" class="graph-canvas" aria-hidden="true">
    <Transition name="pinch-hint">
      <div v-if="showPinchHint" class="pinch-hint" aria-hidden="true">
        {{ t.graph.pinchHint }}
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { useLocale } from '../composables/useLocale.js'

const props = defineProps({
  graph: { type: Object, required: true },
})

const containerEl = ref(null)
const showPinchHint = ref(false)
const { t } = useLocale()

let pinchHintTimeout = null

function hidePinchHint() {
  showPinchHint.value = false
  localStorage.setItem('hm_pinch_hint', '1')
  if (pinchHintTimeout) {
    clearTimeout(pinchHintTimeout)
    pinchHintTimeout = null
  }
  containerEl.value?.removeEventListener('touchstart', hidePinchHint, { once: true })
}

function maybeShowPinchHint() {
  if (localStorage.getItem('hm_pinch_hint')) return
  if (!window.matchMedia('(max-width: 768px)').matches) return
  const cy = props.graph.cy?.value
  if (!cy || cy.nodes().length === 0) return

  showPinchHint.value = true
  containerEl.value?.addEventListener('touchstart', hidePinchHint, { once: true })
  pinchHintTimeout = setTimeout(hidePinchHint, 3000)
}

// Watch for graph loading completion to trigger hint
let unwatch = null
onMounted(() => {
  if (containerEl.value) {
    props.graph.init(containerEl.value).catch((error) => {
      // Keep lazy-load failures visible to diagnostics without leaving an
      // unhandled promise; useGraph resets initialization so remount can retry.
      console.error('Unable to initialize the entity graph', error)
    })
  }

  // Watch graphLoaded flag (exposed as graph.cy changing from null)
  unwatch = watch(
    () => props.graph.cy?.value?.nodes().length,
    (count) => {
      if (count > 0) {
        maybeShowPinchHint()
      }
    },
  )
})

onUnmounted(() => {
  props.graph.destroy()
  if (unwatch) unwatch()
  if (pinchHintTimeout) clearTimeout(pinchHintTimeout)
})
</script>

<style scoped>
.graph-canvas {
  flex: 1;
  width: 100%;
  height: 100%;
  position: relative;
  z-index: var(--z-canvas);
  isolation: isolate;
}

.pinch-hint {
  position: absolute;
  bottom: 1.25rem;
  left: 50%;
  transform: translateX(-50%);
  z-index: var(--z-floating-ui);
  background: var(--scrim-strong);
  color: var(--text-inverse);
  font-size: 0.8125rem;
  font-weight: 500;
  letter-spacing: 0.01em;
  padding: 0.45rem 1.1rem;
  border-radius: 999px;
  pointer-events: none;
  white-space: nowrap;
  backdrop-filter: blur(4px);
}

.pinch-hint-enter-active,
.pinch-hint-leave-active {
  transition: opacity 0.35s ease;
}

.pinch-hint-enter-from,
.pinch-hint-leave-to {
  opacity: 0;
}
</style>
