<template>
  <div class="graph-legend" @click.stop>
    <button
      class="legend-toggle"
      @click="toggle"
      :aria-expanded="open"
      :aria-label="t.legend.title"
    >
      <span class="legend-title-text">{{ t.legend.title }}</span>
      <svg
        class="legend-chevron"
        :class="{ rotated: !open }"
        width="10" height="6" viewBox="0 0 10 6" fill="none"
        aria-hidden="true"
      >
        <path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>

    <Transition name="legend-body">
      <div v-if="open" class="legend-body">

        <!-- Root node — always present -->
        <div class="legend-items">
          <div class="legend-row">
            <span class="node-swatch node-swatch--root"></span>
            <span class="legend-label">{{ t.legend.rootNode }}</span>
          </div>
        </div>

        <!-- Org-circle role borders — only in org-circle mode -->
        <template v-if="layoutMode === 'org-circle'">
          <div class="legend-sep"></div>
          <div class="legend-items">
            <div class="legend-row" v-for="role in roleItems" :key="role.key">
              <span
                class="node-swatch"
                :class="role.swatchClass"
                :style="role.style"
              ></span>
              <span class="legend-label">{{ role.label }}</span>
            </div>
          </div>
        </template>

        <!-- Family relationship borders -->
        <template v-if="layoutMode !== 'org-circle' && hasFamilyEdges">
          <div class="legend-sep"></div>
          <div class="legend-items">
            <div class="legend-row" v-for="item in familyItems" :key="item.key">
              <span
                class="node-swatch node-swatch--role"
                :style="item.style"
              ></span>
              <span class="legend-label">{{ item.label }}</span>
            </div>
          </div>
        </template>

      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useLocale } from '../composables/useLocale.js'
const { t } = useLocale()

const props = defineProps({
  layoutMode:     { type: String,  default: 'network' },
  hasFamilyEdges: { type: Boolean, default: false },
  familyColors:   { type: Object,  default: () => ({}) },
  rolesPresent:   { type: Object,  default: null },
})

// Keep the graph readable on first paint: the legend remains one tap away on
// narrow screens instead of covering a large part of the exploration canvas.
const open = ref(!window.matchMedia('(max-width: 768px)').matches)

function toggle() {
  open.value = !open.value
}

// Role border colors mirror ROLE_CIRCLE_COLORS in useGraph.js
const ALL_ROLE_ITEMS = computed(() => [
  {
    key: 'founders',
    swatchClass: 'node-swatch--role',
    style: { borderColor: '#c08020', background: 'rgba(192,128,32,0.12)' },
    label: t.value.ui?.ringFounders || 'Founders',
  },
  {
    key: 'directors',
    swatchClass: 'node-swatch--role',
    style: { borderColor: '#2e8a7a', background: 'rgba(46,138,122,0.12)' },
    label: t.value.ui?.ringDirectors || 'Directors',
  },
  {
    key: 'members',
    swatchClass: 'node-swatch--members',
    style: {},
    label: t.value.ui?.ringMembers || 'Members',
  },
])

const roleItems = computed(() => {
  if (!props.rolesPresent) return ALL_ROLE_ITEMS.value
  return ALL_ROLE_ITEMS.value.filter((item) => props.rolesPresent.has(item.key))
})

// Family relationship border colors — reads actual colors from the graph via familyColors prop
const familyItems = computed(() => {
  const leg = t.value.legend || {}
  const fc = props.familyColors
  // Only show relationships that are actually present in the current graph
  const items = []
  const spouseLabel = leg.familySpouse || 'Spouse / Partner'
  const childLabel = leg.familyChildren || 'Children'

  if (fc.parent) items.push({
    key: 'parent',
    style: { borderColor: fc.parent, background: `${fc.parent}20` },
    label: leg.familyParents || 'Parents',
  })
  // Spouse units: each spouse + their children share a color
  if (fc.spouseUnits && fc.spouseUnits.length > 0) {
    fc.spouseUnits.forEach((u, i) => {
      const unitLabel = u.name || `${spouseLabel} ${i + 1}`
      items.push({
        key: `spouse-${i}`,
        style: { borderColor: u.color, background: `${u.color}20` },
        label: `${unitLabel} + ${childLabel.toLowerCase()}`,
      })
    })
  }
  // Children whose other-parent is not in the graph — shown whenever any child
  // received the fallback "unknown spouse" color, regardless of how many known
  // spouse units exist. Without this entry those children had no legend row.
  if (fc.childUnknown) items.push({
    key: 'child-unknown',
    style: { borderColor: fc.childUnknown, background: `${fc.childUnknown}20` },
    label: childLabel,
  })
  if (fc.sibling) items.push({
    key: 'sibling',
    style: { borderColor: fc.sibling, background: `${fc.sibling}20` },
    label: leg.familySiblings || 'Siblings',
  })
  if (fc.relative) items.push({
    key: 'relative',
    style: { borderColor: fc.relative, background: `${fc.relative}20` },
    label: leg.familyRelatives || 'Relatives',
  })
  return items
})
</script>

<style scoped>
.graph-legend {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 7;
  background: var(--bg-panel-solid, rgba(249,248,244,0.97));
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  backdrop-filter: blur(4px);
  box-shadow: 0 1px 6px rgba(28,21,16,0.07);
  width: fit-content;
  min-width: 155px;
}

/* ── Toggle ── */
.legend-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 10px;
  background: none;
  border: none;
  cursor: pointer;
  width: 100%;
}
.legend-toggle:hover .legend-title-text {
  color: var(--text);
}
.legend-title-text {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: rgba(28,21,16,0.5);
  transition: color 0.15s;
}
.legend-chevron {
  color: rgba(28,21,16,0.35);
  transition: transform 0.2s ease;
  flex-shrink: 0;
}
.legend-chevron.rotated {
  transform: rotate(-90deg);
}

/* ── Body ── */
.legend-body {
  padding: 0 10px 9px;
}
.legend-items {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.legend-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.legend-label {
  font-size: 12px;
  color: rgba(28,21,16,0.72);
  white-space: nowrap;
  line-height: 1;
}
.legend-sep {
  height: 1px;
  background: var(--border-subtle);
  margin: 7px 0 6px;
}

/* ── Swatches — circles that show borders, not fills ── */
.node-swatch {
  width: 13px;
  height: 13px;
  border-radius: 50%;
  flex-shrink: 0;
  display: block;
  background: var(--surface, #ece8de);
}

/* Root node: neutral fill + amber ring */
.node-swatch--root {
  box-shadow: 0 0 0 2.5px #d4920a;
}

/* Org-circle founder/director + family relationships: colored border */
.node-swatch--role {
  border: 2.5px solid;
}

/* Members: neutral fill with visible dark border matching default node style */
.node-swatch--members {
  border: 2px solid rgba(28, 21, 16, 0.55);
}

/* ── Transition ── */
.legend-body-enter-active,
.legend-body-leave-active {
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.legend-body-enter-from,
.legend-body-leave-to {
  opacity: 0;
  transform: translateY(-3px);
}

/* ── Mobile ── */
@media (max-width: 768px) {
  .graph-legend {
    top: 8px;
    right: 8px;
  }
}
</style>
