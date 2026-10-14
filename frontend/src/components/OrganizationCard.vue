<template>
  <!-- Organization card — org node at top, hub-and-spoke SVG, founder/member columns.
       3-zone flex column: header / body (org node + spokes + people row) / footer.
       People capped at MAX_PEOPLE (4). Fixed portrait size — no shrink-to-fit.
       Sequential stagger: header → org node → spokes draw one-by-one →
         person col 0 → person col 1 → … → footer. -->
  <div
    class="feed-card feed-card--org"
    :class="{ 'in-view': inView }"
    :aria-label="`${card.org.name} — ${card.headline}`"
  >
    <!-- ── Zone 1: Header ── -->
    <div class="card__header">
      <span class="eyebrow">{{ headlineLabel }}</span>
    </div>

    <!-- ── Zone 2: Body ── -->
    <div class="card__body">

      <!-- Org node: logo/monogram + name + meta -->
      <div class="org-node-area">
        <button
          class="portrait portrait--org-node"
          :aria-label="card.org.name"
          :style="`width:${orgNodeSize};height:${orgNodeSize}`"
          @click="$emit('selectOrg', card.org.qid, card.org.name)"
        >
          <img
            v-if="!orgImgError && card.org.has_logo"
            :src="`/api/images/${card.org.qid}.jpg`"
            :alt="card.org.name"
            @error="orgImgError = true"
          />
          <span v-else class="monogram monogram--org">{{ initials(card.org.name) }}</span>
        </button>
        <button class="org-name-btn" @click="$emit('selectOrg', card.org.qid, card.org.name)">
          {{ card.org.name }}
        </button>
        <!-- org meta: founded · hq · flags -->
        <div class="org-meta-row">
          <p v-if="orgMeta" class="org-meta">{{ orgMeta }}</p>
        </div>
      </div>

      <!-- Connector: a horizontal bus under the org node + a vertical drop
           centred on each person. Pure CSS / layout-driven, so it always lines
           up with the portraits regardless of count or adaptive sizing. The
           per-person drop is the .founder-col::before below. -->
      <div class="org-connector" aria-hidden="true">
        <span class="org-connector__trunk"></span>
        <span
          class="org-connector__bus"
          :class="`org-connector__bus--count-${visiblePeople.length}`"
        ></span>
      </div>

      <!-- People row: adaptive-size portraits + name/role/desc -->
      <div class="founders-row" :class="`founders-row--count-${visiblePeople.length}`">
        <div
          v-for="(person, i) in visiblePeople"
          :key="person.qid"
          class="founder-col"
          :style="`--founder-idx: ${i}`"
        >
          <button
            class="portrait portrait--founder"
            :aria-label="person.name"
            :style="`width:${founderPortraitSize};height:${founderPortraitSize}`"
            @click="$emit('selectPerson', person.qid, person.name)"
          >
            <img
              v-if="!peopleImgErrors[person.qid]"
              :src="`/api/images/${person.qid}.jpg`"
              :alt="person.name"
              @error="peopleImgErrors[person.qid] = true"
            />
            <span v-else class="monogram monogram--person">{{ initials(person.name) }}</span>
          </button>
          <div class="founder-info" :style="`--info-idx: ${i}; --name-size: ${founderNameSize}`">
            <button class="founder-name-btn" @click="$emit('selectPerson', person.qid, person.name)">
              {{ person.name }}
            </button>
            <span v-if="person.role && localizedRole(person.role)" class="founder-role">{{ localizedRole(person.role) }}</span>
            <!-- desc: 2 lines, clamp only on real overflow -->
            <span class="founder-desc">{{ person.desc }}</span>
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
import { ref, computed, reactive } from 'vue'
import { useLocale } from '../composables/useLocale.js'

const { t } = useLocale()

const props = defineProps({
  card:   { type: Object,  required: true },
  inView: { type: Boolean, default: false },
})

defineEmits(['selectPerson', 'selectOrg'])

const orgImgError = ref(false)
const peopleImgErrors = reactive({})

// Cap at 5 for uniform layout
const MAX_PEOPLE = 5
const visiblePeople = computed(() => (props.card.people || []).slice(0, MAX_PEOPLE))

// Adaptive portrait size based on people count:
// 1 person → large (clamp 120px … 180px)
// 2 people → large (clamp 100px … 140px)
// 3 people → medium (clamp 72px … 96px)
// 4-5 people → compact but still large enough to recognize on mobile
const founderPortraitSize = computed(() => {
  const n = visiblePeople.value.length
  if (n <= 1) return 'clamp(120px, 28vw, 180px)'
  if (n === 2) return 'clamp(100px, 23vw, 140px)'
  if (n === 3) return 'clamp(72px, 17vw, 96px)'
  return 'clamp(56px, 14vw, 72px)'
})

// Adaptive org-node logo size: scale up when only 1–2 people (more visual room)
const orgNodeSize = computed(() => {
  const n = visiblePeople.value.length
  if (n <= 1) return '88px'
  if (n === 2) return '78px'
  return '68px'
})

// Adaptive font size for single-person names when portrait is large
const founderNameSize = computed(() => {
  const n = visiblePeople.value.length
  if (n <= 1) return '19px'
  if (n === 2) return '17px'
  if (n === 3) return '15px'
  return '14px'   // 4–5 people — was 12px (too small)
})


function initials(name) {
  if (!name) return '?'
  const parts = name.trim().split(/\s+/)
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
  return name.slice(0, 2).toUpperCase()
}

const orgMeta = computed(() => {
  const parts = []
  if (props.card.org.founded) parts.push(`${t.value.feed.founded} ${props.card.org.founded}`)
  if (props.card.org.hq)     parts.push(props.card.org.hq)
  else if (props.card.org.label) parts.push(props.card.org.label)
  return parts.join(' · ')
})

const HEADLINE_MAP = {
  'Founded by':      'foundedBy',
  'Led by':          'ledBy',
  'On the board':    'onTheBoard',
  'Notable players': 'notablePlayers',
  'Members':         'orgMembers',
  'Leadership':      'leadership',
  'Notable alumni':  'notableAlumni',
  'Organization':    'organizationCard',
}
const headlineLabel = computed(() => {
  const key = HEADLINE_MAP[props.card.headline]
  return key ? t.value.feed[key] : (props.card.headline || 'CONNECTED TO')
})

const ROLE_KEY_MAP = {
  'Founder':      'roleFounder',
  'Chair':        'roleChair',
  'CEO':          'roleCeo',
  'Former CEO':   'roleFormerCeo',
  'Director':     'roleDirector',
  'Board member': 'roleBoardMember',
  'Officer':      'roleOfficer',
  'Owner':        'roleOwner',
  'Shareholder':  'roleShareholder',
}
function localizedRole(roleEn) {
  const key = ROLE_KEY_MAP[roleEn]
  if (!key) return roleEn
  return (t.value.feed.roles && t.value.feed.roles[key]) || roleEn
}

// Spoke x-positions: evenly distributed across SVG width
// Spoke endpoint x = centre of person i's column. The people row lays out as
// n equal-width slices spanning the full width, each portrait centred in its
// slice, so the centre of slice i is at (i + 0.5)/n. This makes every spoke
// actually point at its portrait (1–5 people) instead of a fixed fan that
// missed the centred columns.
function spokeX(i, n) {
  if (!n) return '50'
  return (((i + 0.5) * 100) / n).toFixed(1)
}

// Sequential spoke stagger: 460ms base, 110ms between spokes
const SPOKE_BASE  = 460
const SPOKE_PITCH = 110
function spokeDelay(i) {
  return SPOKE_BASE + i * SPOKE_PITCH
}

// Person col appears after its spoke finishes (spoke_delay + 400ms draw + 60ms pad)
const SPOKE_DRAW = 400
const SPOKE_PAD  = 60
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

/* ── Zone 2: Body ────────────────────────────────────────────────────────── */
.card__body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: space-evenly;
  /* bottom-right safe zone: stage-actions are min-height:44px × 2 buttons + 6px gap
     anchored at bottom:14px → occupies ~108px from bottom. Use 112px padding. */
  padding: 12px 20px 112px;
  gap: 4px;
  overflow: hidden;
}

/* ── Org node area ───────────────────────────────────────────────────────── */
.org-node-area {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  text-align: center;
  will-change: opacity;
  opacity: 0;
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1) 200ms;
}
.in-view .org-node-area { opacity: 1; }

/* Org logo — rounded square, adaptive size via inline style */
.portrait--org-node {
  /* width/height set by :style binding (orgNodeSize computed) */
  border-radius: 14px;
  overflow: hidden;
  border: 2.5px solid var(--color-org);
  background: var(--surface);
  flex-shrink: 0; padding: 0; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  will-change: transform, opacity;
  transform: scale(.88);
  opacity: 0;
  transition:
    transform 460ms cubic-bezier(.34,1.35,.64,1) 200ms,
    opacity   420ms cubic-bezier(.22,.61,.36,1)  200ms;
}
.in-view .portrait--org-node { transform: scale(1); opacity: 1; }
.portrait--org-node img { width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none; }
.portrait--org-node:hover { opacity: 0.88; }
.portrait--org-node:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }

.monogram--org {
  color: #fff; font-size: 22px; font-weight: 700; font-family: var(--font-display);
  background: var(--color-org);
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
}

.org-name-btn {
  background: none; border: none; padding: 0; cursor: pointer;
  font-family: var(--font-display); font-size: 20px; font-weight: 700;
  color: var(--text); line-height: 1.15; text-align: center;
  overflow-wrap: break-word; word-break: break-word;
}
.org-name-btn:hover { color: var(--accent); text-decoration: underline; }
.org-name-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }

/* Org meta row: meta text + flags inline */
.org-meta-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  flex-wrap: wrap;
}

/* Meta: 2 lines, only ellipsis on real overflow */
.org-meta {
  font-size: 14px; color: var(--text-muted); line-height: 1.45; margin: 0;
  max-height: calc(14px * 1.45 * 3);
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden; text-align: center;
}


/* ── SVG spoke container ─────────────────────────────────────────────────── */
/* ── Connector: trunk from the org node + a horizontal bus; per-person drops
   are .founder-col::before. Layout-driven so it always aligns. ───────────── */
.org-connector {
  position: relative;
  width: 100%;
  height: 20px;
  flex-shrink: 0;
}
/* short vertical trunk down from the org node to the bus */
.org-connector__trunk {
  position: absolute;
  top: 0;
  left: 50%;
  width: 2px;
  height: 10px;
  background: var(--accent);
  opacity: .55;
  transform: translateX(-50%) scaleY(0);
  transform-origin: top;
  transition: transform 320ms cubic-bezier(.22,.61,.36,1) 560ms;
}
.in-view .org-connector__trunk { transform: translateX(-50%) scaleY(1); }
/* horizontal bus at the trunk's bottom — width spans first→last column centre */
.org-connector__bus {
  position: absolute;
  top: 10px;
  left: 50%;
  height: 2px;
  background: var(--accent);
  opacity: .55;
  transform: translateX(-50%) scaleX(0);
  transition: transform 420ms cubic-bezier(.22,.61,.36,1) 820ms;
}
.in-view .org-connector__bus { transform: translateX(-50%) scaleX(1); }
/* width = (n-1)/n of the row (centre of first col → centre of last col) */
.org-connector__bus--count-1 { width: 0; }
.org-connector__bus--count-2 { width: 50%; }
.org-connector__bus--count-3 { width: 66.6%; }
.org-connector__bus--count-4 { width: 75%; }
.org-connector__bus--count-5 { width: 80%; }

/* ── Founders/members row ────────────────────────────────────────────────── */
.founders-row {
  display: flex;
  justify-content: center;
  gap: clamp(6px, 1.6vw, 14px);
  flex-wrap: nowrap; /* fixed count ≤5, never wraps */
  width: 100%;
}

.founder-col {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  text-align: center;
  /* Equal full-width slices so each portrait is centred in its 1/n slice and
     its connector drop (::before) lines up with it for any count. */
  flex: 1 1 0;
  min-width: 0;
  will-change: opacity;
  opacity: 0;
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1)
    calc(1200ms + var(--founder-idx, 0) * 280ms);
}
.in-view .founder-col { opacity: 1; }
/* vertical drop from the bus down to this person's portrait, centred on the
   column (= centred on the portrait). Sits in the .org-connector zone above. */
.founder-col::before {
  content: "";
  position: absolute;
  top: -16px;
  left: 50%;
  width: 2px;
  height: 18px;
  background: var(--accent);
  opacity: .55;
  transform: translateX(-50%) scaleY(0);
  transform-origin: top;
  transition: transform 320ms cubic-bezier(.22,.61,.36,1)
    calc(1100ms + var(--founder-idx, 0) * 280ms);
}
.in-view .founder-col::before { transform: translateX(-50%) scaleY(1); }

/* Adaptive founder portrait — width/height set by :style binding (founderPortraitSize) */
.portrait--founder {
  /* width/height injected via inline style — adaptive by count */
  border-radius: 8px;
  overflow: hidden;
  border: 2px solid var(--color-person);
  background: var(--surface);
  flex-shrink: 0; padding: 0; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
}
.portrait--founder img { width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none; }
.portrait--founder:hover { opacity: 0.88; }
.portrait--founder:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* The desktop feed frame has enough width to make every person identifiable.
   These override the adaptive inline size while preserving the 5-column row. */
@media (min-width: 1024px) {
  .founders-row--count-1 .portrait--founder { width: 190px !important; height: 190px !important; }
  .founders-row--count-2 .portrait--founder { width: 156px !important; height: 156px !important; }
  .founders-row--count-3 .portrait--founder { width: 116px !important; height: 116px !important; }
  .founders-row--count-4 .portrait--founder,
  .founders-row--count-5 .portrait--founder { width: 84px !important; height: 84px !important; }
}

.monogram--person {
  color: #fff; font-size: clamp(13px, 3.5vw, 18px); font-weight: 700;
  font-family: var(--font-display);
  background: var(--color-person);
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
}

/* Founder info: stagger ~220ms after its portrait/col lands */
.founder-info {
  display: flex; flex-direction: column; gap: 2px;
  will-change: opacity;
  opacity: 0;
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1)
    calc(1420ms + var(--info-idx, 0) * 280ms);
}
.in-view .founder-info { opacity: 1; }

.founder-name-btn {
  background: none; border: none; padding: 0; cursor: pointer;
  font-family: var(--font-display);
  font-size: var(--name-size, 12px); /* adaptive via CSS var set on .founder-info */
  font-weight: 700;
  color: var(--text); line-height: 1.2; text-align: center;
  /* 2 lines; only clamps on real overflow */
  max-height: calc(var(--name-size, 12px) * 1.2 * 2);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; overflow-wrap: break-word; word-break: break-word;
}
.founder-name-btn:hover { color: var(--accent); text-decoration: underline; }
.founder-name-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }

/* Role chip */
.founder-role {
  display: inline-block;
  font-size: 9px; font-weight: 700; letter-spacing: .10em;
  text-transform: uppercase; color: var(--accent);
  background: rgba(27, 77, 142, 0.10);
  border-radius: 4px; padding: 2px 5px;
  line-height: 1.4; margin-bottom: 1px; white-space: nowrap;
}

/* Founder desc: 2 lines, clamp only on real overflow */
.founder-desc {
  font-size: clamp(12px, 2.6vw, 14px); color: var(--text-muted); line-height: 1.45;
  /* Use available vertical space: up to 4 lines, only clips on real overflow */
  max-height: calc(14px * 1.45 * 4);
  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical;
  overflow: hidden; overflow-wrap: break-word; word-break: break-word;
}

/* ── Zone 3: Footer ──────────────────────────────────────────────────────── */
.card__footer {
  flex-shrink: 0;
  padding: 12px 24px 18px;
  border-top: 1px solid var(--border-faint);
  display: flex; align-items: center; justify-content: center;
  will-change: opacity;
  opacity: 0;
  /* after last person info (up to 5 people: 1420ms + 4*280ms = 2540ms) */
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1) 2650ms;
}
.in-view .card__footer { opacity: 1; }

.footer-brand {
  font-size: 10px; font-weight: 600; letter-spacing: .1em;
  text-transform: uppercase; color: var(--accent); opacity: .6;
}

/* ── Reduced motion ──────────────────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .feed-card,
  .card__header,
  .card__footer,
  .org-node-area,
  .portrait--org-node,
  .org-connector__trunk,
  .org-connector__bus,
  .founder-col,
  .founder-info {
    transition: none !important;
    animation: none !important;
    opacity: 1 !important;
  }
  /* keep connector lines fully drawn (override the scale transforms) */
  .org-connector__trunk { transform: translateX(-50%) scaleY(1) !important; }
  .org-connector__bus   { transform: translateX(-50%) scaleX(1) !important; }
  .founder-col::before  { transform: translateX(-50%) scaleY(1) !important; }
  .founder-col          { opacity: 1 !important; }
}
</style>
