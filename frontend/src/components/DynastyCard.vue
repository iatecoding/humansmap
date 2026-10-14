<template>
  <!-- Dynasty card — vertical timeline spine with staggered member rows.
       3-zone flex column: header / body (spine + rows) / footer.
       Members capped at MAX_MEMBERS (5). Portraits are FIXED size — no depth scaling.
       Sequential stagger: header → title → spine grows → row 0 → row 1 → … → footer. -->
  <div
    class="feed-card feed-card--dynasty"
    :class="{ 'in-view': inView, 'dynasty--dense': isDense }"
    :aria-label="`${card.title} family dynasty`"
  >
    <!-- ── Zone 1: Header ── -->
    <div class="card__header">
      <span class="eyebrow">{{ t.feed.familyDynasty }}</span>
      <span v-if="card.field" class="dynasty-field-tag">{{ card.field }}</span>
    </div>

    <!-- ── Zone 2: Body ── -->
    <div class="card__body">

      <!-- Dynasty title + subtitle -->
      <div class="dynasty-title">
        <h2 class="dynasty-family-label">{{ card.title }}</h2>
        <p class="dynasty-subtitle">{{ card.subtitle }}</p>
      </div>

      <!-- Timeline spine + member rows -->
      <div class="dynasty-timeline">
        <!-- Vertical spine (decorative) -->
        <div class="dynasty-spine" aria-hidden="true">
          <div class="dynasty-spine__fill"></div>
        </div>

        <!-- Member rows -->
        <div class="dynasty-rows">
          <div
            v-for="(member, i) in visibleMembers"
            :key="member.qid"
            class="dynasty-row"
            :data-row-idx="i"
            :style="`--row-idx: ${i}`"
          >
            <!-- Adaptive-size portrait -->
            <button
              class="portrait"
              :aria-label="member.name"
              :style="`width:${memberPortraitSize};height:${memberPortraitSize}`"
              @click="$emit('selectPerson', member.qid, member.name)"
            >
              <img
                v-if="!imgErrors[member.qid]"
                :src="`/api/images/${member.qid}.jpg`"
                :alt="member.name"
                @error="imgErrors[member.qid] = true"
              />
              <span v-else class="monogram-dynasty">{{ initials(member.name) }}</span>
            </button>

            <!-- Member info -->
            <div class="dynasty-row__info">
              <button class="dynasty-name-btn" @click="$emit('selectPerson', member.qid, member.name)">
                {{ member.name }}
              </button>
              <!-- desc: allow up to 2 lines, clamp only on real overflow -->
              <span class="dynasty-label">{{ member.desc }}</span>
              <span v-if="member.life" class="dynasty-lifespan">{{ member.life }}</span>
            </div>
          </div>

          <!-- "+N more" chip -->
          <div v-if="overflowCount > 0" class="dynasty-overflow" aria-label="More members">
            +{{ overflowCount }} more
          </div>
        </div>
      </div>

    </div>

    <!-- ── Zone 3: Footer ── -->
    <div class="card__footer" aria-hidden="true">
      <span class="footer-brand">humansmap.com</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, reactive, onMounted, onBeforeUnmount } from 'vue'
import { useLocale } from '../composables/useLocale.js'

const { t } = useLocale()

const props = defineProps({
  card:   { type: Object,  required: true },
  inView: { type: Boolean, default: false },
})

defineEmits(['selectPerson'])

const imgErrors = reactive({})

// On narrow screens cap members so all rows fit the fixed card frame without
// being clipped by overflow:hidden. 4 members at ~44px each + gaps = ~212px
// which fits even on a 375×568 phone (timeline ≈ 214px available).
const MAX_MEMBERS        = 6
const MOBILE_MAX_MEMBERS = 4

const isMobile = ref(false)
let mobileMql = null
function onMobileChange(e) { isMobile.value = e.matches }
onMounted(() => {
  if (typeof window !== 'undefined' && window.matchMedia) {
    mobileMql = window.matchMedia('(max-width: 640px)')
    isMobile.value = mobileMql.matches
    mobileMql.addEventListener ? mobileMql.addEventListener('change', onMobileChange)
                               : mobileMql.addListener(onMobileChange)
  }
})
onBeforeUnmount(() => {
  if (!mobileMql) return
  mobileMql.removeEventListener ? mobileMql.removeEventListener('change', onMobileChange)
                                 : mobileMql.removeListener(onMobileChange)
})

const effectiveMax   = computed(() => isMobile.value ? MOBILE_MAX_MEMBERS : MAX_MEMBERS)
const visibleMembers = computed(() => (props.card.members || []).slice(0, effectiveMax.value))
const overflowCount  = computed(() => Math.max(0, (props.card.members || []).length - effectiveMax.value))
// Dense layout: ≥5 members on desktop, ≥4 on mobile (where max is already 4)
const isDense        = computed(() => visibleMembers.value.length >= (isMobile.value ? 4 : 5))

function initials(name) {
  if (!name) return '?'
  const parts = name.trim().split(/\s+/)
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
  return name.slice(0, 2).toUpperCase()
}

// Adaptive portrait size by member count — same scale as org card
// 1 → large, 2 → large, 3 → medium, 4 → compact, 5–6 → smallest (still legible)
const memberPortraitSize = computed(() => {
  const n = visibleMembers.value.length
  if (n <= 1) return 'clamp(104px, 28vw, 124px)'
  if (n === 2) return 'clamp(76px, 20vw, 92px)'
  if (n === 3) return 'clamp(60px, 16vw, 72px)'
  if (n === 4) return 'clamp(50px, 13vw, 60px)'
  return 'clamp(44px, 11vw, 54px)'
})

</script>

<style scoped>
/* ── Card shell — 3-zone flex column ──────────────────────────────────── */
.feed-card {
  background: var(--bg);
  border: 1px solid var(--border-subtle);
  border-radius: 16px;
  width: 100%;
  height: 100%;          /* fills .feed-slide exactly */
  max-width: 640px;
  padding: 0;
  box-shadow:
    0 2px 8px  rgba(28, 21, 16, 0.06),
    0 12px 36px rgba(28, 21, 16, 0.09);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  will-change: box-shadow;
}
.feed-card.in-view {
  animation: idle-float 4s ease-in-out infinite;
  animation-delay: 1.2s;
}
@keyframes idle-float {
  0%,  100% { box-shadow: 0 2px 8px  rgba(28,21,16,.06), 0 12px 36px rgba(28,21,16,.09); }
  50%        { box-shadow: 0 6px 18px rgba(28,21,16,.09), 0 20px 48px rgba(28,21,16,.13); }
}

/* ── Zone 1: Header ─────────────────────────────────────────────────────── */
.card__header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 20px 24px 0;
  /* Action buttons are now outside the card (stage-actions in FeedView) */
  will-change: opacity;
  opacity: 0;
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1) 80ms;
}
.in-view .card__header { opacity: 1; }

.eyebrow {
  font-size: 14px; font-weight: 800; letter-spacing: .1em;
  text-transform: uppercase; color: var(--accent);
}

/* Family field tag — e.g. Film / Politics / Music */
.dynasty-field-tag {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--text-muted);
  background: var(--surface);
  padding: 3px 9px;
  border-radius: 20px;
  white-space: nowrap;
}

/* ── Zone 2: Body ────────────────────────────────────────────────────────── */
.card__body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  /* bottom-right safe zone: stage-actions min-height:44px × 2 + 6px gap + 14px bottom = ~108px */
  padding: 16px 24px 112px;
  gap: 16px;
  overflow: hidden;
}

/* ── Dynasty title block ─────────────────────────────────────────────────── */
.dynasty-title {
  flex-shrink: 0;
  will-change: opacity;
  opacity: 0;
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1) 360ms;
}
.in-view .dynasty-title { opacity: 1; }

.dynasty-family-label {
  font-family: var(--font-display);
  font-size: 24px; font-weight: 700; color: var(--text);
  line-height: 1.15; margin: 0 0 3px;
}
.dynasty-subtitle {
  font-size: 14px; font-weight: 500; color: var(--text-muted);
  margin: 0; line-height: 1.45;
}

/* ── Timeline container ──────────────────────────────────────────────────── */
.dynasty-timeline {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 20px 1fr;
  gap: 0 12px;
  position: relative;
  overflow: hidden;
}

/* ── Vertical spine ──────────────────────────────────────────────────────── */
.dynasty-spine {
  position: relative;
}
.dynasty-spine__fill {
  position: absolute;
  top: 0; left: 9px; width: 2px;
  background: var(--accent);
  height: 0; border-radius: 1px;
  /* Spine starts growing after title is visible */
  transition: height 620ms cubic-bezier(.22,.61,.36,1) 620ms;
}
.in-view .dynasty-spine__fill { height: 100%; }

/* ── Member rows ─────────────────────────────────────────────────────────── */
.dynasty-rows {
  display: flex;
  flex-direction: column;
  /* Fill the timeline height and spread members across it so the card adapts
     to its dimensions instead of clustering at the top with dead space below. */
  height: 100%;
  justify-content: space-between;
  gap: clamp(8px, 1.8vh, 14px);
  overflow: hidden;
}

/* Sequential stagger: row 0 at 900ms, +280ms per row */
.dynasty-row {
  display: flex;
  align-items: center;
  gap: 12px;
  position: relative;
  will-change: opacity;
  opacity: 0;
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1)
    calc(900ms + var(--row-idx, 0) * 280ms);
}

/* Spine dot */
.dynasty-row::before {
  content: '';
  position: absolute;
  left: -16px; top: 50%;
  transform: translateY(-50%);
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--bg);
  flex-shrink: 0;
}

.in-view .dynasty-row { opacity: 1; }

/* ── Portrait — adaptive size injected via :style binding ─────────────────── */
.portrait {
  /* width/height set by :style binding (memberPortraitSize computed) */
  border-radius: 8px;
  overflow: hidden;
  border: 2px solid var(--color-person);
  background: var(--surface);
  flex-shrink: 0;
  cursor: pointer; padding: 0;
  display: flex; align-items: center; justify-content: center;
}
.portrait img {
  width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none;
}
.portrait:hover  { opacity: 0.88; }
.portrait:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.monogram-dynasty {
  color: #fff; font-size: clamp(11px, 3vw, 15px); font-weight: 700;
  font-family: var(--font-display);
  background: var(--color-person);
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
}

/* ── Member text info ────────────────────────────────────────────────────── */
.dynasty-row__info {
  display: flex; flex-direction: column; gap: 2px; min-width: 0;
}

.dynasty-name-btn {
  background: none; border: none; padding: 0; cursor: pointer;
  font-family: var(--font-display); font-size: clamp(15px, 3.4vw, 17px); font-weight: 700;
  color: var(--text); line-height: 1.2; text-align: left;
  overflow-wrap: break-word; word-break: break-word;
}
.dynasty-name-btn:hover { color: var(--accent); text-decoration: underline; }
.dynasty-name-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }

/* Desc: 2 lines max; only ellipsis when content genuinely overflows */
.dynasty-label {
  font-size: clamp(12px, 2.6vw, 14px); color: var(--text-muted); line-height: 1.45;
  /* up to 3 lines, clip only on real overflow */
  max-height: calc(14px * 1.45 * 3);
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden; overflow-wrap: break-word; word-break: break-word;
}

.dynasty-lifespan {
  font-size: 12px; font-weight: 500; color: var(--accent); letter-spacing: .03em;
}

/* Dense mode (5–6 members): tighten descriptions so every row fits the frame
   instead of the last member being clipped. */
.dynasty--dense .dynasty-label {
  -webkit-line-clamp: 2;
  max-height: calc(14px * 1.45 * 2);
}

/* "+N more" chip */
.dynasty-overflow {
  font-size: 11px; color: var(--text-muted); font-style: italic; padding-left: 2px;
  will-change: opacity;
  opacity: 0;
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1)
    calc(900ms + 6 * 280ms); /* after last row (up to 6 members) */
}
.in-view .dynasty-overflow { opacity: 1; }

/* ── Zone 3: Footer ──────────────────────────────────────────────────────── */
.card__footer {
  flex-shrink: 0;
  padding: 12px 24px 18px;
  border-top: 1px solid var(--border-faint);
  display: flex; align-items: center; justify-content: center;
  will-change: opacity;
  opacity: 0;
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1) 2500ms;
}
.in-view .card__footer { opacity: 1; }

.footer-brand {
  font-size: 10px; font-weight: 600; letter-spacing: .1em;
  text-transform: uppercase; color: var(--accent); opacity: .6;
}

/* ── Desktop: the enlarged feed frame (FeedView desktop media query) gives the
   dynasty card much more vertical room. Use it — larger portraits, roomier
   rows and line-height — so 6 members fill the taller frame instead of
   clustering with dead space below. Portrait size is set inline via the
   memberPortraitSize binding, so override it with !important here. */
@media (min-width: 1024px) {
  .card__body {
    gap: 22px;
    padding: 20px 28px 116px;
  }
  .dynasty-family-label { font-size: 28px; }
  .dynasty-subtitle { font-size: 15px; }

  .dynasty-rows {
    gap: clamp(14px, 2.4vh, 26px);
  }

  /* Larger portraits to occupy the taller frame (overrides inline clamp) */
  .portrait {
    width: clamp(80px, 7vw, 104px) !important;
    height: clamp(80px, 7vw, 104px) !important;
  }

  .dynasty-name-btn { font-size: 19px; }
  .dynasty-label { font-size: 15px; line-height: 1.5; max-height: calc(15px * 1.5 * 3); }
  .dynasty-lifespan { font-size: 13px; }

  /* Dense desktop (5–6 members): the roomy sizes above don't fit 6 rows in the
     frame (the 6th was being clipped). Tighten portraits, gaps, name and trim
     the description to one line so every member is fully visible. */
  .dynasty--dense .card__body { padding-bottom: 100px; }
  .dynasty--dense .dynasty-rows { gap: clamp(8px, 1.4vh, 16px); }
  .dynasty--dense .portrait {
    width: clamp(60px, 5vw, 64px) !important;
    height: clamp(60px, 5vw, 64px) !important;
  }
  .dynasty--dense .dynasty-name-btn { font-size: 17px; }
  .dynasty--dense .dynasty-label {
    font-size: 14px; line-height: 1.45;
    -webkit-line-clamp: 1; max-height: calc(14px * 1.45 * 1);
  }
}

/* ── Mobile (≤640px): cap rows to 4 members — CSS side ensures text fits ──── */
/* Portrait sizes stay per the JS binding; we tighten the text so each row
   height matches the portrait (~44px) instead of the multi-line text block. */
@media (max-width: 640px) {
  /* 1-line description — keeps row height at portrait size, prevents clipping */
  .dynasty-label {
    -webkit-line-clamp: 1;
    max-height: calc(14px * 1.45 * 1);
  }
  /* Lifespan is secondary on narrow screens; omit to save vertical space */
  .dynasty-lifespan {
    display: none;
  }
  /* Dense on mobile is anything ≥4 (JS sets isDense at that threshold) */
  .dynasty--dense .dynasty-label {
    -webkit-line-clamp: 1;
    max-height: calc(14px * 1.45 * 1);
  }
}

/* ── Reduced motion ──────────────────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .feed-card,
  .card__header,
  .card__footer,
  .dynasty-title,
  .dynasty-spine__fill,
  .dynasty-row,
  .dynasty-overflow {
    transition: none !important;
    animation: none !important;
    opacity: 1 !important;
  }
  .dynasty-spine__fill { height: 100% !important; }
}
</style>
