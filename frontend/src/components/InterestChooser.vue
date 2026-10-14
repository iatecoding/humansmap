<template>
  <!--
    InterestChooser — snap-section-styled card that lets the user tailor the feed.
    Rendered as its own snap section in FeedView, injected after card 2.
    Emits:
      confirm({ categories: string[], nationality: string })
        — user picked interests and/or a nationality and confirmed
      skip()
        — user wants the unfiltered feed
    Keyboard-accessible: chips are <button> elements with aria-pressed.
    Reduced-motion safe: no entrance animation beyond what .in-view gives.
  -->
  <div
    class="feed-card chooser-card"
    :class="{ 'in-view': inView }"
    role="region"
    :aria-label="t.feed.interestTitle"
  >
    <!-- Header eyebrow -->
    <div class="card__header">
      <span class="eyebrow">{{ t.feed.didYouKnow }}</span>
    </div>

    <!-- Title + subtitle -->
    <div class="chooser-title-block">
      <h2 class="chooser-title">{{ t.feed.interestTitle }}</h2>
      <p class="chooser-subtitle">{{ t.feed.interestSubtitle }}</p>
    </div>

    <!-- Interest chips -->
    <div class="chips-grid" role="group" :aria-label="t.feed.interestTitle">
      <button
        v-for="chip in CHIPS"
        :key="chip.id"
        class="chip"
        :class="{ 'chip--selected': selected.has(chip.id) }"
        :aria-pressed="selected.has(chip.id)"
        @click="toggleChip(chip.id)"
      >
        <span class="chip__icon" aria-hidden="true">{{ chip.icon }}</span>
        <span class="chip__label">{{ t.feed[chip.labelKey] }}</span>
      </button>
    </div>

    <!-- Card-type selector — which archetypes to show -->
    <div class="types-section" role="group" :aria-label="t.feed.interestTypeTitle || 'Show me'">
      <span class="types-label">{{ t.feed.interestTypeTitle || 'Show me' }}</span>
      <div class="types-chips">
        <button
          v-for="tchip in TYPE_CHIPS"
          :key="tchip.id"
          class="chip type-chip"
          :class="{ 'chip--selected': selectedTypes.has(tchip.id) }"
          :aria-pressed="selectedTypes.has(tchip.id)"
          @click="toggleType(tchip.id)"
        >
          <span class="chip__icon" aria-hidden="true">{{ tchip.icon }}</span>
          <span class="chip__label">{{ typeLabel(tchip) }}</span>
        </button>
      </div>
    </div>

    <!-- Nationality selector -->
    <div class="nationality-section">
      <label class="nationality-label" :for="'nat-select-' + uid">
        {{ t.feed.interestNationality }}
      </label>
      <select
        :id="'nat-select-' + uid"
        class="nationality-select"
        v-model="chosenNationality"
        :aria-label="t.feed.interestNationality"
      >
        <option value="">{{ t.feed.nationalityWorldwide }}</option>
        <option
          v-for="item in sortedCountries"
          :key="item.code"
          :value="item.code"
        >{{ item.name }}</option>
      </select>
    </div>

    <!-- Action buttons -->
    <div class="chooser-actions">
      <button
        class="chooser-btn chooser-btn--primary"
        :disabled="selectedTypes.size === 0"
        @click="onConfirm"
      >
        {{ t.feed.interestConfirm }}
      </button>
      <button class="chooser-btn chooser-btn--secondary" @click="$emit('skip')">
        {{ t.feed.interestSkip }}
      </button>
    </div>

    <!-- Footer -->
    <div class="card__footer" aria-hidden="true">
      <span class="footer-brand">humansmap.com</span>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { useLocale } from '../composables/useLocale.js'

const { t, locale } = useLocale()

const props = defineProps({
  inView: { type: Boolean, default: false },
  // Pre-selected category ids (for re-open / edit flow)
  initialSelected: { type: Array, default: () => [] },
  // Current nationality (ISO-2 string or '' for worldwide)
  initialNationality: { type: String, default: '' },
  // Pre-selected card-type ids; empty array = all types (no filter)
  initialTypes: { type: Array, default: () => [] },
})

const emit = defineEmits(['confirm', 'skip'])

// Unique id suffix so multiple instances don't share label targets
const uid = Math.random().toString(36).slice(2, 7)

// Supported nationalities (all populated in backend feed:meta:v2.countries)
const SUPPORTED_CODES = [
  'AT','BE','BG','CA','CH','CZ','DE','DK','EE','ES','FI','FR',
  'GB','GR','HR','HU','IE','IS','IT','LT','LV','MX','NL','NO',
  'PL','PT','RO','RS','SE','SI','SK','UA','US',
]

// Build localised display names via Intl.DisplayNames; fall back to the code.
const sortedCountries = computed(() => {
  let displayNames
  try {
    displayNames = new Intl.DisplayNames([locale.value], { type: 'region' })
  } catch {
    displayNames = null
  }
  return SUPPORTED_CODES
    .map(code => ({
      code,
      name: displayNames ? (() => { try { return displayNames.of(code) || code } catch { return code } })() : code,
    }))
    .sort((a, b) => a.name.localeCompare(b.name, locale.value))
})

const CHIPS = [
  { id: 'politics',          icon: '🏛', labelKey: 'catPolitics' },
  { id: 'business',          icon: '💼', labelKey: 'catBusiness' },
  { id: 'sport',             icon: '⚽', labelKey: 'catSport' },
  { id: 'science_education', icon: '🔬', labelKey: 'catScienceEducation' },
  { id: 'arts',              icon: '🎨', labelKey: 'catArts' },
]

// Card archetypes. "People" = relationship ("Did you know") cards.
// labelKey falls back to `fallback` when a locale lacks the string.
const TYPE_CHIPS = [
  { id: 'relationship', icon: '❤️', labelKey: 'typePeople',        fallback: 'People' },
  { id: 'dynasty',      icon: '🌳', labelKey: 'typeDynasties',     fallback: 'Dynasties' },
  { id: 'organization', icon: '🏢', labelKey: 'typeOrganizations', fallback: 'Organizations' },
]
const ALL_TYPE_IDS = TYPE_CHIPS.map(c => c.id)

// Initialise from props so "Edit interests" re-opens with current selections
const selected = reactive(new Set(props.initialSelected))
const chosenNationality = ref(props.initialNationality)
// Empty initialTypes means "all types"; default to every archetype selected.
const selectedTypes = reactive(
  new Set(props.initialTypes.length ? props.initialTypes : ALL_TYPE_IDS),
)

// Keep the controls in sync if the parent's current filters change while the
// chooser exists, so reopening it to add one filter never silently drops the
// others (e.g. selecting types must not wipe a previously-chosen nationality).
watch(() => props.initialNationality, (v) => { chosenNationality.value = v ?? '' })
watch(() => props.initialSelected, (v) => {
  selected.clear(); (v || []).forEach((x) => selected.add(x))
})
watch(() => props.initialTypes, (v) => {
  selectedTypes.clear(); (v && v.length ? v : ALL_TYPE_IDS).forEach((x) => selectedTypes.add(x))
})

function typeLabel(chip) {
  return t.value.feed[chip.labelKey] || chip.fallback
}

function toggleChip(id) {
  if (selected.has(id)) {
    selected.delete(id)
  } else {
    selected.add(id)
  }
}

function toggleType(id) {
  if (selectedTypes.has(id)) {
    selectedTypes.delete(id)
  } else {
    selectedTypes.add(id)
  }
}

function onConfirm() {
  if (selectedTypes.size === 0) return
  // All types selected ⇒ no filter ⇒ emit empty array.
  const types = selectedTypes.size === ALL_TYPE_IDS.length ? [] : [...selectedTypes]
  emit('confirm', {
    categories: [...selected],
    nationality: chosenNationality.value,
    types,
  })
}
</script>

<style scoped>
/* ── Card shell — matches feed-card aesthetic ──────────────────────────────── */
.chooser-card {
  background: var(--bg);
  border: 1px solid var(--border-subtle);
  border-radius: 16px;
  width: 100%;
  max-width: 640px;
  padding: 32px 30px 24px;
  box-shadow:
    0 2px 8px rgba(28, 21, 16, 0.06),
    0 12px 36px rgba(28, 21, 16, 0.09);
  will-change: transform, opacity;
  transform: translateY(18px);
  opacity: 0;
  transition:
    transform 400ms cubic-bezier(.22,.61,.36,1),
    opacity   400ms cubic-bezier(.22,.61,.36,1);
}
.chooser-card.in-view {
  transform: translateY(0);
  opacity: 1;
  animation: idle-float 4s ease-in-out infinite;
  animation-delay: 1.2s;
}
@keyframes idle-float {
  0%,  100% { box-shadow: 0 2px 8px rgba(28,21,16,.06), 0 12px 36px rgba(28,21,16,.09); }
  50%        { box-shadow: 0 6px 18px rgba(28,21,16,.09), 0 20px 48px rgba(28,21,16,.13); }
}

/* ── Card header ────────────────────────────────────────────────────────────── */
.card__header {
  display: flex;
  align-items: center;
  margin-bottom: 20px;
  will-change: transform, opacity;
  transform: translateY(8px);
  opacity: 0;
  transition:
    transform 350ms cubic-bezier(.22,.61,.36,1) 80ms,
    opacity   350ms cubic-bezier(.22,.61,.36,1) 80ms;
}
.in-view .card__header { transform: translateY(0); opacity: 1; }

.eyebrow {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--accent);
}

/* ── Title block ────────────────────────────────────────────────────────────── */
.chooser-title-block {
  margin-bottom: 24px;
  text-align: center;
  will-change: transform, opacity;
  transform: translateY(8px);
  opacity: 0;
  transition:
    transform 380ms cubic-bezier(.22,.61,.36,1) 160ms,
    opacity   380ms cubic-bezier(.22,.61,.36,1) 160ms;
}
.in-view .chooser-title-block { transform: translateY(0); opacity: 1; }

.chooser-title {
  font-family: var(--font-display);
  font-size: clamp(18px, 5vw, 22px);
  font-weight: 700;
  color: var(--text);
  line-height: 1.2;
  margin: 0 0 8px;
}
.chooser-subtitle {
  font-size: clamp(13px, 3.5vw, 15px);
  color: var(--text-muted);
  line-height: 1.5;
  margin: 0;
}

/* ── Interest chips grid ────────────────────────────────────────────────────── */
.chips-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 10px;
  margin-bottom: 24px;
  will-change: transform, opacity;
  transform: translateY(8px);
  opacity: 0;
  transition:
    transform 380ms cubic-bezier(.22,.61,.36,1) 300ms,
    opacity   380ms cubic-bezier(.22,.61,.36,1) 300ms;
}
.in-view .chips-grid { transform: translateY(0); opacity: 1; }

.chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-radius: 10px;
  border: 2px solid var(--border-subtle);
  background: var(--surface);
  cursor: pointer;
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  text-align: left;
  transition:
    background 180ms ease,
    border-color 180ms ease,
    color 180ms ease;
  min-height: 48px; /* touch target */
}
.chip:hover {
  border-color: var(--accent);
  background: var(--card);
}
.chip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.chip--selected {
  border-color: var(--accent);
  background: var(--accent);
  color: #fff;
}
.chip--selected:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
.chip__icon {
  font-size: 18px;
  line-height: 1;
  flex-shrink: 0;
}
.chip__label {
  line-height: 1.25;
}

/* ── Card-type selector ─────────────────────────────────────────────────────── */
.types-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 20px;
  will-change: transform, opacity;
  transform: translateY(8px);
  opacity: 0;
  transition:
    transform 380ms cubic-bezier(.22,.61,.36,1) 345ms,
    opacity   380ms cubic-bezier(.22,.61,.36,1) 345ms;
}
.in-view .types-section { transform: translateY(0); opacity: 1; }

.types-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .09em;
  text-transform: uppercase;
  color: var(--text-muted, rgba(28,21,16,.55));
}

.types-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.type-chip {
  flex: 1 1 auto;
  justify-content: center;
  min-height: 44px;
  padding: 10px 12px;
}

/* ── Nationality selector ───────────────────────────────────────────────────── */
.nationality-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 20px;
  will-change: transform, opacity;
  transform: translateY(8px);
  opacity: 0;
  transition:
    transform 380ms cubic-bezier(.22,.61,.36,1) 390ms,
    opacity   380ms cubic-bezier(.22,.61,.36,1) 390ms;
}
.in-view .nationality-section { transform: translateY(0); opacity: 1; }

.nationality-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .09em;
  text-transform: uppercase;
  color: var(--text-muted, rgba(28,21,16,.55));
}

.nationality-select {
  width: 100%;
  min-height: 44px; /* touch target */
  padding: 10px 14px;
  border-radius: 10px;
  border: 2px solid var(--border-subtle);
  background: var(--surface);
  color: var(--text);
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  appearance: auto; /* keep native arrow for accessibility */
  transition: border-color 180ms ease, background 180ms ease;
}
.nationality-select:hover {
  border-color: var(--accent);
  background: var(--card);
}
.nationality-select:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* ── Action buttons ─────────────────────────────────────────────────────────── */
.chooser-actions {
  display: flex;
  flex-direction: column;
  gap: 10px;
  will-change: transform, opacity;
  transform: translateY(8px);
  opacity: 0;
  transition:
    transform 360ms cubic-bezier(.22,.61,.36,1) 480ms,
    opacity   360ms cubic-bezier(.22,.61,.36,1) 480ms;
}
.in-view .chooser-actions { transform: translateY(0); opacity: 1; }

.chooser-btn {
  width: 100%;
  min-height: 48px;
  padding: 12px 20px;
  border-radius: 10px;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  border: none;
  transition: background 180ms ease, opacity 180ms ease;
}
.chooser-btn--primary {
  background: var(--accent);
  color: #fff;
}
.chooser-btn--primary:hover:not(:disabled) { background: var(--accent-hover); }
.chooser-btn--primary:disabled {
  opacity: 0.45;
  cursor: default;
}
.chooser-btn--primary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}
.chooser-btn--secondary {
  background: transparent;
  color: var(--text-muted);
  border: 1px solid var(--border-subtle);
}
.chooser-btn--secondary:hover {
  background: var(--surface);
  color: var(--text);
}
.chooser-btn--secondary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* ── Card footer ────────────────────────────────────────────────────────────── */
.card__footer {
  margin-top: 20px;
  padding-top: 14px;
  border-top: 1px solid var(--border-faint);
  display: flex;
  align-items: center;
  justify-content: center;
  will-change: transform, opacity;
  transform: translateY(6px);
  opacity: 0;
  transition:
    transform 300ms cubic-bezier(.22,.61,.36,1) 640ms,
    opacity   300ms cubic-bezier(.22,.61,.36,1) 640ms;
}
.in-view .card__footer { transform: translateY(0); opacity: 1; }

.footer-brand {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--accent);
  opacity: .6;
}

/* ── Mobile ──────────────────────────────────────────────────────────────────── */
@media (max-width: 480px) {
  .chooser-card { padding: 24px 20px 18px; }
  .chips-grid { grid-template-columns: 1fr 1fr; gap: 8px; }
  .chip { padding: 10px 12px; font-size: 13px; }
}

/* ── Reduced motion ──────────────────────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .chooser-card,
  .card__header,
  .card__footer,
  .chooser-title-block,
  .chips-grid,
  .types-section,
  .nationality-section,
  .chooser-actions {
    transition: none !important;
    animation: none !important;
    transform: none !important;
    opacity: 1 !important;
  }
}
</style>
