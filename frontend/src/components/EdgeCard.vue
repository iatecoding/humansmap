<template>
  <Transition name="edge-overlay">
    <div v-if="edge" class="edge-card" aria-live="polite">
      <button class="close-btn" @click="$emit('close')" :aria-label="t.ui.close">&times;</button>
      <div class="edge-card-inner">
        <span class="edge-card-type">{{ translateEdgeTypeLocal(edge.type) }}</span>
        <div class="edge-endpoints">
          <span class="endpoint-name">{{ panelData.sourceName }}</span>
          <span class="endpoint-arrow">&#8594;</span>
          <span class="endpoint-name">{{ panelData.targetName }}</span>
        </div>
        <div v-if="panelData.role || panelData.start_date || panelData.end_date" class="edge-details">
          <div v-if="panelData.role" class="detail-row">
            <span class="label">{{ t.ui.role }}</span>
            <span>{{ panelData.role }}</span>
          </div>
          <div v-if="panelData.start_date || panelData.end_date" class="detail-row">
            <span class="label">{{ t.ui.period }}</span>
            <span>{{ formatDate(panelData.start_date) }}{{ panelData.start_date && panelData.end_date ? '\u2013' : '' }}{{ formatDate(panelData.end_date) }}</span>
          </div>
        </div>
      </div>
    </div>
  </Transition>
</template>

<script setup>
import { computed } from 'vue'
import { useLocale } from '../composables/useLocale.js'
import { translateEdgeType } from '../utils/cytoscape-config.js'

const { t } = useLocale()

const props = defineProps({
  edge: { type: Object, default: null },
  cy: { type: Object, default: null },
})

defineEmits(['close'])

const panelData = computed(() => {
  const edge = props.edge
  if (!edge) return {}
  const src = props.cy?.$id(edge.source)?.data()
  const tgt = props.cy?.$id(edge.target)?.data()
  return {
    ...edge,
    sourceName: src?.name_it || src?.name_en || edge.source,
    targetName: tgt?.name_it || tgt?.name_en || edge.target,
  }
})

function translateEdgeTypeLocal(type) {
  return translateEdgeType(type, t.value.edgeTypes) || ''
}

function formatDate(dateStr) {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr
  return d.getFullYear().toString()
}
</script>

<style scoped>
.edge-card {
  position: absolute;
  top: 14px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--bg-panel);
  border: 1.5px solid rgba(191, 107, 40, 0.35);
  border-radius: 14px;
  padding: 10px 44px 10px 16px;
  box-shadow: 0 4px 18px rgba(0, 0, 0, 0.12);
  z-index: 8;
  max-width: 480px;
  min-width: 180px;
  pointer-events: auto;
  white-space: nowrap;
}
.edge-card .close-btn {
  position: absolute;
  top: 50%;
  right: 6px;
  transform: translateY(-50%);
  background: none;
  border: none;
  color: rgba(28, 21, 16, 0.4);
  font-size: 20px;
  cursor: pointer;
  padding: 0;
  border-radius: 4px;
  min-width: 32px;
  min-height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.edge-card .close-btn:hover {
  color: var(--text);
  background: rgba(0, 0, 0, 0.07);
}
.edge-card-inner {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.edge-card-type {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.8px;
  color: rgba(191, 107, 40, 0.8);
}
.edge-endpoints {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.endpoint-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.endpoint-arrow {
  color: rgba(28, 21, 16, 0.35);
  font-size: 13px;
  flex-shrink: 0;
}
.edge-details {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
}
.detail-row {
  display: flex;
  gap: 6px;
  font-size: 12px;
}
.label {
  color: rgba(28, 21, 16, 0.5);
  flex-shrink: 0;
}
.edge-overlay-enter-active,
.edge-overlay-leave-active {
  transition: opacity 0.18s, transform 0.18s;
}
.edge-overlay-enter-from,
.edge-overlay-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(-6px);
}

/* ── Mobile: bottom sheet to avoid overlap with top-left legend ── */
@media (max-width: 768px) {
  .edge-card {
    top: auto;
    bottom: max(16px, env(safe-area-inset-bottom, 16px));
    left: 12px;
    right: 12px;
    transform: none;
    max-width: none;
    white-space: normal;
    border-radius: 16px;
    padding: 12px 48px 12px 16px;
  }
  .edge-card .close-btn {
    top: 12px;
    right: 10px;
    transform: none;
    min-width: 36px;
    min-height: 36px;
    font-size: 22px;
  }
  .edge-overlay-enter-from,
  .edge-overlay-leave-to {
    opacity: 0;
    transform: translateY(10px);
  }
}
</style>
