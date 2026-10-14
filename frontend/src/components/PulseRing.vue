<template>
  <div
    v-if="visible"
    class="pulse-ring"
    :style="ringStyle"
  />
</template>

<script setup>
import { ref, computed, watch, onUnmounted } from 'vue'

const props = defineProps({
  cy: { type: Object, default: null },
  nodeId: { type: String, default: null },
})

const pos = ref({ x: 0, y: 0 })
const size = ref(40)

const visible = computed(() => {
  if (!props.cy || !props.nodeId) return false
  const node = props.cy.getElementById(props.nodeId)
  return node && node.length > 0
})

const ringStyle = computed(() => ({
  left: `${pos.value.x}px`,
  top: `${pos.value.y}px`,
  width: `${size.value}px`,
  height: `${size.value}px`,
}))

function updatePosition() {
  if (!props.cy || !props.nodeId) return
  const node = props.cy.getElementById(props.nodeId)
  if (!node || node.length === 0) return
  const rp = node.renderedPosition()
  pos.value = { x: rp.x, y: rp.y }
  size.value = node.renderedWidth() * 1.5
}

let cleanupListeners = null

function attachListeners(cy) {
  if (!cy) return
  cy.on('viewport', updatePosition)
  cy.on('position', updatePosition)
  cleanupListeners = () => {
    cy.off('viewport', updatePosition)
    cy.off('position', updatePosition)
  }
}

watch(
  () => [props.cy, props.nodeId],
  ([cy, nodeId]) => {
    if (cleanupListeners) {
      cleanupListeners()
      cleanupListeners = null
    }
    if (cy && nodeId) {
      updatePosition()
      attachListeners(cy)
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  if (cleanupListeners) cleanupListeners()
})
</script>

<style scoped>
.pulse-ring {
  position: absolute;
  border-radius: 50%;
  border: 3px solid var(--accent, #1b4d8e);
  pointer-events: none;
  z-index: 5;
  transform: translate(-50%, -50%);
  animation: pulse-ring 1.5s ease-out infinite;
}

@media (prefers-reduced-motion: reduce) {
  .pulse-ring {
    display: none;
  }
}
</style>
