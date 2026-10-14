<template>
  <div class="hemicycle-chrome">
    <div class="chamber-label" :style="govStyle">GOVERNMENT</div>
    <div class="chamber-label" :style="oppStyle">OPPOSITION</div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

// Decorative overlay for parliament hemicycle view.
// govLabelX / oppLabelX are screen-space pixel offsets from the left edge of the canvas,
// computed in App.vue from cy.modelToRenderedPosition and updated on pan/zoom.
const props = defineProps({
  govLabelX: { default: null },
  oppLabelX: { default: null },
})

const govStyle = computed(() =>
  props.govLabelX != null
    ? { left: `${props.govLabelX}px`, transform: 'translateX(-50%)' }
    : { left: '16px' }
)
const oppStyle = computed(() =>
  props.oppLabelX != null
    ? { left: `${props.oppLabelX}px`, transform: 'translateX(-50%)' }
    : { right: '16px' }
)
</script>

<style scoped>
.hemicycle-chrome {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 3;
}

.chamber-label {
  position: absolute;
  top: 10px;
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.07em;
  color: rgba(28, 21, 16, 0.35);
  pointer-events: none;
  font-family: Inter, DM Sans, sans-serif;
  white-space: nowrap;
}
</style>
