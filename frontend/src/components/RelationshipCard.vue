<template>
  <!-- Relationship card — two people connected by an animated line + relation pill.
       3-zone flex column: header (eyebrow) / body (couple + connector) / footer (brand).
       Body uses flex:1 + align-items:center to fill the uniform slide frame.
       Children capped at 4; extras shown as "+N more" chip.
       Sequential stagger: portrait A → connector → portrait B → info A → info B → children. -->
  <div
    class="feed-card feed-card--relationship"
    :class="{ 'in-view': inView }"
    :aria-label="`Connection between ${card.a.name} and ${card.b.name}`"
  >
    <!-- ── Zone 1: Header ── -->
    <div class="card__header">
      <span class="eyebrow">{{ t.feed.didYouKnow }}</span>
      <span v-if="domainTag" class="domain-tag">{{ domainTag }}</span>
    </div>

    <!-- ── Zone 2: Body — fills remaining height ── -->
    <div class="card__body">

      <!-- Portraits + connector -->
      <div class="connector-area">
        <!-- Person A -->
        <div class="person-col">
          <button
            class="portrait portrait--a"
            :aria-label="card.a.name"
            @click="$emit('selectPerson', card.a.qid, card.a.name)"
          >
            <img
              v-if="!imgErrorA"
              :src="`/api/images/${card.a.qid}.jpg`"
              :alt="card.a.name"
              @error="imgErrorA = true"
            />
            <span v-else class="monogram">{{ initials(card.a.name) }}</span>
          </button>
          <div class="person-info person-info--a">
            <button class="person-name-btn" @click="$emit('selectPerson', card.a.qid, card.a.name)">
              {{ card.a.name }}
            </button>
            <p class="person-desc">{{ card.a.desc }}</p>
            <p v-if="worksA" class="known-for">{{ t.feed.knownFor }} {{ worksA }}</p>
          </div>
        </div>

        <!-- Animated connecting line + relation pill -->
        <div class="connector" aria-hidden="true">
          <div class="connector__line">
            <div class="connector__fill"></div>
          </div>
          <div class="relation-pill">
            <span class="relation-label">{{ card.relation }}</span>
            <span v-if="card.dates" class="relation-dates">{{ card.dates }}</span>
          </div>
          <div class="connector__line connector__line--bottom">
            <div class="connector__fill connector__fill--bottom"></div>
          </div>
        </div>

        <!-- Person B -->
        <div class="person-col">
          <button
            class="portrait portrait--b"
            :aria-label="card.b.name"
            @click="$emit('selectPerson', card.b.qid, card.b.name)"
          >
            <img
              v-if="!imgErrorB"
              :src="`/api/images/${card.b.qid}.jpg`"
              :alt="card.b.name"
              @error="imgErrorB = true"
            />
            <span v-else class="monogram">{{ initials(card.b.name) }}</span>
          </button>
          <div class="person-info person-info--b">
            <button class="person-name-btn" @click="$emit('selectPerson', card.b.qid, card.b.name)">
              {{ card.b.name }}
            </button>
            <p class="person-desc">{{ card.b.desc }}</p>
            <p v-if="worksB" class="known-for">{{ t.feed.knownFor }} {{ worksB }}</p>
          </div>
        </div>
      </div>

      <!-- Children cluster — capped at MAX_CHILDREN (4) -->
      <div v-if="children.length" class="children-section">
        <div class="children-label" aria-label="Children">{{ t.feed.children || 'Children' }}</div>
        <div class="children-row" role="list">
          <div
            v-for="(child, i) in children"
            :key="child.qid || child.name || i"
            class="child-col"
            :style="`--child-idx: ${i}`"
            role="listitem"
          >
            <template v-if="!child.overflow">
              <button
                class="child-portrait"
                :aria-label="child.name"
                @click="child.qid && $emit('selectPerson', child.qid, child.name)"
                :disabled="!child.qid"
              >
                <img
                  v-if="child.qid && !childImgErrors[child.qid]"
                  :src="`/api/images/${child.qid}.jpg`"
                  :alt="child.name"
                  @error="childImgErrors[child.qid] = true"
                />
                <span v-else class="child-monogram">{{ initials(child.name) }}</span>
              </button>
              <span class="child-name">{{ firstName(child.name) }}</span>
              <span v-if="birthYear(child.life)" class="child-year">{{ birthYear(child.life) }}</span>
            </template>
            <!-- "+N more" overflow chip -->
            <template v-else>
              <div class="child-overflow-badge" aria-label="More children">{{ child.name }}</div>
            </template>
          </div>
        </div>
      </div>

      <!-- Other relationships — below the couple & children, vertical & date-ordered.
           Each row carries the other person's portrait, a bigger name and date. -->
      <div v-if="othersA.length || othersB.length" class="other-rels-section">
        <div class="other-rels-section__label">{{ t.feed.otherRelationships || 'Other relationships' }}</div>
        <div class="other-rels-groups">
          <div v-if="othersA.length" class="other-rels-group">
            <div class="other-rels-group__who">{{ firstName(card.a.name) }}</div>
            <ul class="other-rels">
              <li v-for="(rel, i) in othersA" :key="'a' + (rel.qid || rel.name || i)" class="other-rel" :style="`--rel-idx: ${i}`">
                <button class="other-rel__portrait" :aria-label="rel.name" @click="rel.qid && $emit('selectPerson', rel.qid, rel.name)" :disabled="!rel.qid">
                  <img v-if="rel.qid && !otherImgErrors[rel.qid]" :src="`/api/images/${rel.qid}.jpg`" :alt="rel.name" @error="otherImgErrors[rel.qid] = true" />
                  <span v-else class="other-rel__monogram">{{ initials(rel.name) }}</span>
                </button>
                <div class="other-rel__text">
                  <button class="other-rel__name" @click="rel.qid && $emit('selectPerson', rel.qid, rel.name)" :disabled="!rel.qid">{{ rel.name }}</button>
                  <span class="other-rel__meta">
                    <span v-if="rel.dates" class="other-rel__dates">{{ rel.dates }}</span>
                    <span class="other-rel__label">{{ rel.relation }}</span>
                  </span>
                </div>
              </li>
            </ul>
          </div>
          <div v-if="othersB.length" class="other-rels-group">
            <div class="other-rels-group__who">{{ firstName(card.b.name) }}</div>
            <ul class="other-rels">
              <li v-for="(rel, i) in othersB" :key="'b' + (rel.qid || rel.name || i)" class="other-rel" :style="`--rel-idx: ${i}`">
                <button class="other-rel__portrait" :aria-label="rel.name" @click="rel.qid && $emit('selectPerson', rel.qid, rel.name)" :disabled="!rel.qid">
                  <img v-if="rel.qid && !otherImgErrors[rel.qid]" :src="`/api/images/${rel.qid}.jpg`" :alt="rel.name" @error="otherImgErrors[rel.qid] = true" />
                  <span v-else class="other-rel__monogram">{{ initials(rel.name) }}</span>
                </button>
                <div class="other-rel__text">
                  <button class="other-rel__name" @click="rel.qid && $emit('selectPerson', rel.qid, rel.name)" :disabled="!rel.qid">{{ rel.name }}</button>
                  <span class="other-rel__meta">
                    <span v-if="rel.dates" class="other-rel__dates">{{ rel.dates }}</span>
                    <span class="other-rel__label">{{ rel.relation }}</span>
                  </span>
                </div>
              </li>
            </ul>
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


const imgErrorA = ref(false)
const imgErrorB = ref(false)
const childImgErrors = reactive({})
const otherImgErrors = reactive({})

function initials(name) {
  if (!name) return '?'
  const parts = name.trim().split(/\s+/)
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
  return name.slice(0, 2).toUpperCase()
}

function firstName(name) {
  if (!name) return ''
  return name.trim().split(/\s+/)[0]
}

function birthYear(life) {
  if (!life) return null
  const m = life.match(/\b(1[0-9]{3}|20[0-9]{2})\b/)
  return m ? m[1] : null
}

const domainTag = computed(() => {
  const { domains } = props.card
  if (!domains || domains.length < 2) return null
  if (domains[0] === domains[1]) return null
  return `${domains[0]} ↔ ${domains[1]}`
})

const worksA = computed(() => {
  const w = props.card.a?.works
  if (!w || !w.length) return null
  return w.slice(0, 2).join(', ')
})
const worksB = computed(() => {
  const w = props.card.b?.works
  if (!w || !w.length) return null
  return w.slice(0, 2).join(', ')
})

// ── Other-relationships timeline ───────────────────────────────────────────
// The desktop feed card (≥1024px) is wide/tall enough to show BOTH partners'
// other-relationship lists; on narrow screens vertical space is tight, so we
// show only ONE partner's list (the one with more entries; ties favour A) to
// avoid overflowing the fixed card frame / colliding with the action buttons.
const isDesktop = ref(false)
let mql = null
function updateDesktop(e) { isDesktop.value = e.matches }
onMounted(() => {
  if (typeof window === 'undefined' || !window.matchMedia) { isDesktop.value = true; return }
  mql = window.matchMedia('(min-width: 1024px)')
  isDesktop.value = mql.matches
  mql.addEventListener ? mql.addEventListener('change', updateDesktop) : mql.addListener(updateDesktop)
})
onBeforeUnmount(() => {
  if (!mql) return
  mql.removeEventListener ? mql.removeEventListener('change', updateDesktop) : mql.removeListener(updateDesktop)
})

const rawOthersA = computed(() => props.card.a?.other_relations || [])
const rawOthersB = computed(() => props.card.b?.other_relations || [])

// Which single partner wins the lone slot on mobile: more entries, tie → A.
const mobilePrimary = computed(() =>
  rawOthersB.value.length > rawOthersA.value.length ? 'b' : 'a',
)

const othersA = computed(() => {
  if (isDesktop.value) return rawOthersA.value
  return mobilePrimary.value === 'a' ? rawOthersA.value : []
})
const othersB = computed(() => {
  if (isDesktop.value) return rawOthersB.value
  return mobilePrimary.value === 'b' ? rawOthersB.value : []
})

// Cap children at 4; backend may already include an overflow sentinel
const MAX_CHILDREN = 4
const children = computed(() => {
  const raw = props.card.children
  if (!raw || !raw.length) return []
  const real = raw.filter(c => !c.overflow)
  const overflow = raw.find(c => c.overflow)
  const capped = real.slice(0, MAX_CHILDREN)
  // Compute total extras: backend overflow count + anything we sliced off
  const slicedOff = real.length - capped.length
  const backendExtra = overflow
    ? (parseInt(String(overflow.name).replace(/\D/g, '')) || 0)
    : 0
  const totalExtra = slicedOff + backendExtra
  if (totalExtra > 0) {
    capped.push({ overflow: true, name: `+${totalExtra}`, qid: null, life: null })
  }
  return capped
})
</script>

<style scoped>
/* ── Card shell — 3-zone flex column filling the .feed-slide frame ─────── */
.feed-card {
  background: var(--bg);
  border: 1px solid var(--border-subtle);
  border-radius: 16px;
  width: 100%;
  height: 100%;           /* fills .feed-slide height exactly */
  max-width: 640px;
  padding: 0;             /* zones own their own padding */
  box-shadow:
    0 2px 8px  rgba(28, 21, 16, 0.06),
    0 12px 36px rgba(28, 21, 16, 0.09);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  /* Entrance: delegated to .feed-slide wrapper; card itself just fades shadow */
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

/* ── Zone 1: Header ──────────────────────────────────────────────────────── */
.card__header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px 0;
  /* Action buttons are now outside the card (stage-actions in FeedView) */
  will-change: opacity;
  opacity: 0;
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1) 80ms;
}
.in-view .card__header {
  opacity: 1;
}

.eyebrow {
  font-size: 14px;
  font-weight: 800;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--accent);
}

.domain-tag {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--text-muted);
  background: var(--surface);
  padding: 3px 8px;
  border-radius: 20px;
  white-space: nowrap;
}

/* ── Zone 2: Body — flex:1, centers its content ─────────────────────────── */
.card__body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 20px;
  /* bottom-right safe zone: stage-actions min-height:44px × 2 + 6px gap + 14px bottom = ~108px */
  padding: 16px 24px 112px;
  overflow: hidden;
}

/* ── Connector area ──────────────────────────────────────────────────────── */
.connector-area {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  gap: 0;
  align-items: start;
  width: 100%;
}

/* ── Square portraits — relationship always has exactly 2 people, so generous ── */
/* Mobile-first: keep faces prominent without crowding the connector. */
.portrait {
  width:  clamp(96px, 24vw, 128px);
  height: clamp(96px, 24vw, 128px);
  border-radius: 8px;
  overflow: hidden;
  border: 2.5px solid var(--color-person);
  background: var(--surface);
  flex-shrink: 0;
  cursor: pointer;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  will-change: transform, opacity;
}
.portrait img {
  width: 100%; height: 100%;
  object-fit: cover;
  display: block;
  pointer-events: none;
}
.monogram {
  color: #fff;
  font-size: clamp(20px, 5vw, 26px);
  font-weight: 700;
  font-family: var(--font-display);
  line-height: 1;
  background: var(--color-person);
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
}
.portrait:hover  { opacity: 0.88; }
.portrait:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }

/* Sequential stagger: A at 300ms, B at 620ms (A lands → connector → B starts) */
.portrait--a {
  transform: scale(.88);
  opacity: 0;
  transition:
    transform 460ms cubic-bezier(.22,.61,.36,1) 300ms,
    opacity   460ms cubic-bezier(.22,.61,.36,1) 300ms;
}
.portrait--b {
  transform: scale(.88);
  opacity: 0;
  transition:
    transform 460ms cubic-bezier(.22,.61,.36,1) 620ms,
    opacity   460ms cubic-bezier(.22,.61,.36,1) 620ms;
}
.in-view .portrait--a,
.in-view .portrait--b {
  transform: scale(1);
  opacity: 1;
}

/* ── Person columns ──────────────────────────────────────────────────────── */
.person-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  text-align: center;
  min-width: 0;
}

.person-name-btn {
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  font-family: var(--font-display);
  font-size: clamp(15px, 3.8vw, 18px);
  font-weight: 700;
  color: var(--text);
  line-height: 1.2;
  margin-bottom: 2px;
  text-align: center;
  overflow-wrap: break-word;
  word-break: break-word;
}
.person-name-btn:hover  { color: var(--accent); text-decoration: underline; }
.person-name-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }

/* person-info: stagger after their portrait lands */
.person-info {
  will-change: opacity;
  opacity: 0;
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1) 900ms;
}
.person-info--b {
  transition-delay: 1200ms;
}
.in-view .person-info {
  opacity: 1;
}

/* Description — allows up to 3 lines; only clamps when text genuinely overflows */
.person-desc {
  font-size: clamp(14px, 3.4vw, 16px);
  color: var(--text-muted);
  line-height: 1.5;
  margin: 0 0 5px;
  overflow-wrap: break-word;
  word-break: break-word;
  /* Up to 3 lines — leaves room for children + other-relationships below */
  max-height: calc(16px * 1.5 * 3);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.known-for {
  font-size: 12px;
  font-weight: 500;
  font-style: italic;
  color: var(--accent);
  line-height: 1.4;
  margin: 0;
  /* 2 lines max; only shows ellipsis if genuinely too long */
  max-height: calc(12px * 1.4 * 2);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ── Animated connector (line + pill) ────────────────────────────────────── */
.connector {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 0 8px;
  /* match updated portrait height for vertical rhythm */
  min-height: clamp(96px, 24vw, 128px);
  flex: 1;
}

.connector__line {
  width: 2px;
  flex: 1;
  background: var(--border-subtle);
  border-radius: 1px;
  position: relative;
  overflow: hidden;
  margin: 6px 0;
  min-height: 20px;
}

.connector__fill {
  position: absolute;
  top: 0; left: 0; right: 0;
  background: var(--accent);
  height: 0;
  border-radius: 1px;
  /* connector draws after portrait A lands (300ms + 460ms = 760ms) → starts at 600ms */
  transition: height 520ms cubic-bezier(.22,.61,.36,1) 600ms;
}
.in-view .connector__fill { height: 100%; }

/* Bottom segment of the line (between pill and person B portrait) */
.connector__fill--bottom {
  transition-delay: 600ms;
}

/* Relation pill — spring pop after connector finishes (~1150ms) */
.relation-pill {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  background: var(--accent);
  color: #fff;
  border-radius: 20px;
  padding: 6px 12px;
  white-space: nowrap;
  will-change: transform, opacity;
  transform: scale(.75);
  opacity: 0;
  transition:
    transform 440ms cubic-bezier(.34,1.35,.64,1) 1150ms,
    opacity   380ms cubic-bezier(.22,.61,.36,1)  1150ms;
}
.in-view .relation-pill { transform: scale(1); opacity: 1; }

.relation-label {
  font-size: 10px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
}
.relation-dates {
  font-size: 9px; font-weight: 500; opacity: .85;
}

/* ── Other-relationships section (full-width, below couple & children) ────── */
.other-rels-section {
  width: 100%;
  padding-top: 16px;
  border-top: 1px solid var(--border-faint);
  will-change: opacity;
  opacity: 0;
  /* Appears after the children section / person-info blocks land */
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1) 1700ms;
}
.in-view .other-rels-section { opacity: 1; }

.other-rels-section__label {
  font-size: 10px; font-weight: 700; letter-spacing: .10em;
  text-transform: uppercase; color: var(--text-muted);
  margin-bottom: 12px; text-align: center;
}

.other-rels-groups {
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px 24px;
}
/* Two columns when both partners' lists are shown (desktop) */
@media (min-width: 1024px) {
  .portrait {
    width: clamp(152px, 12vw, 176px);
    height: clamp(152px, 12vw, 176px);
  }
  .connector { min-height: clamp(152px, 12vw, 176px); }
  .other-rels-groups { grid-template-columns: 1fr 1fr; }
}

.other-rels-group { min-width: 0; }
.other-rels-group__who {
  font-size: 11px; font-weight: 700; letter-spacing: .04em;
  color: var(--accent); margin-bottom: 8px; text-align: left;
}

.other-rels {
  list-style: none; margin: 0; padding: 0;
  display: flex; flex-direction: column; gap: 10px;
}

.other-rel {
  display: flex; align-items: center; gap: 10px;
  text-align: left;
  /* per-row micro-stagger */
  will-change: opacity;
  opacity: 0;
  transition: opacity 380ms cubic-bezier(.22,.61,.36,1)
    calc(1760ms + var(--rel-idx, 0) * 200ms);
}
.in-view .other-rel { opacity: 1; }

/* Portrait of the other person — square, image with monogram fallback */
.other-rel__portrait {
  width: 42px; height: 42px; flex-shrink: 0;
  border-radius: 8px; overflow: hidden;
  border: 2px solid var(--color-person);
  background: var(--surface);
  cursor: pointer; padding: 0;
  display: flex; align-items: center; justify-content: center;
}
.other-rel__portrait[disabled] { cursor: default; border-color: var(--border-subtle); }
.other-rel__portrait img { width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none; }
.other-rel__portrait:not([disabled]):hover { opacity: .85; }
.other-rel__portrait:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.other-rel__monogram {
  color: #fff; font-size: 13px; font-weight: 700; font-family: var(--font-display);
  background: var(--color-person);
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
}

.other-rel__text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }

.other-rel__name {
  background: none; border: none; padding: 0; cursor: pointer;
  font-family: var(--font-display);
  font-size: 15px; font-weight: 700; color: var(--text);
  line-height: 1.2; text-align: left;
  overflow-wrap: break-word; word-break: break-word;
}
.other-rel__name:not([disabled]):hover { color: var(--accent); text-decoration: underline; }
.other-rel__name[disabled] { cursor: default; }
.other-rel__name:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }

.other-rel__meta { display: flex; align-items: baseline; gap: 7px; flex-wrap: wrap; }
.other-rel__dates {
  font-size: 12.5px; font-weight: 600; color: var(--accent); letter-spacing: .02em;
}
.other-rel__label {
  font-size: 10px; font-weight: 700; letter-spacing: .06em;
  text-transform: uppercase; color: var(--text-muted);
}

/* ── Children cluster ────────────────────────────────────────────────────── */
.children-section {
  width: 100%;
  padding-top: 16px;
  border-top: 1px solid var(--border-faint);
  will-change: opacity;
  opacity: 0;
  /* Appears after both person-info blocks land */
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1) 1500ms;
}
.in-view .children-section { opacity: 1; }

.children-label {
  font-size: 10px; font-weight: 700; letter-spacing: .10em;
  text-transform: uppercase; color: var(--text-muted);
  margin-bottom: 12px; text-align: center;
}

.children-row {
  display: flex;
  justify-content: center;
  gap: clamp(10px, 2.5vw, 20px);
  flex-wrap: nowrap; /* fixed count, never wraps */
}

.child-col {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  min-width: 0;
  will-change: opacity;
  opacity: 0;
  /* stagger each child: 1560ms + index×120ms */
  transition: opacity 380ms cubic-bezier(.22,.61,.36,1)
    calc(1560ms + var(--child-idx, 0) * 120ms);
}
.in-view .child-col { opacity: 1; }

/* Fixed size — does NOT scale with count */
.child-portrait {
  width: 52px; height: 52px;
  border-radius: 8px;
  overflow: hidden;
  border: 2px solid var(--color-person);
  background: var(--surface);
  flex-shrink: 0;
  cursor: pointer; padding: 0;
  display: flex; align-items: center; justify-content: center;
}
.child-portrait[disabled] { cursor: default; border-color: var(--border-subtle); }
.child-portrait img { width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none; }
.child-portrait:not([disabled]):hover { opacity: 0.85; }
.child-portrait:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.child-monogram {
  color: #fff; font-size: 14px; font-weight: 700; font-family: var(--font-display);
  background: var(--color-person);
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
}

.child-name {
  font-size: 11px; font-weight: 600; color: var(--text); line-height: 1.2;
  text-align: center; max-width: 54px;
  display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;
  overflow: hidden; overflow-wrap: break-word;
}
.child-year {
  font-size: 10px; color: var(--text-muted); line-height: 1; letter-spacing: .02em;
}

/* "+N more" overflow chip — same size as portrait */
.child-overflow-badge {
  width: 52px; height: 52px;
  border-radius: 8px;
  background: var(--surface);
  border: 2px dashed var(--border-subtle);
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 600; color: var(--text-muted);
  text-align: center; line-height: 1.2; padding: 4px;
}

/* ── Zone 3: Footer ──────────────────────────────────────────────────────── */
.card__footer {
  flex-shrink: 0;
  padding: 12px 24px 18px;
  border-top: 1px solid var(--border-faint);
  display: flex; align-items: center; justify-content: center;
  will-change: opacity;
  opacity: 0;
  /* After last child / other-rels row */
  transition: opacity 420ms cubic-bezier(.22,.61,.36,1) 2300ms;
}
.in-view .card__footer { opacity: 1; }

.footer-brand {
  font-size: 10px; font-weight: 600; letter-spacing: .1em;
  text-transform: uppercase; color: var(--accent); opacity: .6;
}

/* ── Mobile (≤640px): switch to flex-start so overflow goes downward ────── */
/* With justify-content:center, overflow is split equally at top AND bottom,
   hiding the portraits at the top. flex-start ensures portraits are
   always visible; excess content (children, other-rels) clips at the bottom. */
@media (max-width: 640px) {
  .card__body {
    justify-content: flex-start;
    padding-top: clamp(16px, 5vh, 44px);
    padding-left: 16px;
    padding-right: 16px;
  }
}

@media (min-width: 360px) and (max-width: 640px) {
  .connector-area { grid-template-columns: minmax(96px, 1fr) auto minmax(96px, 1fr); }
  .connector { padding-inline: 4px; }
}

/* ── Reduced motion ──────────────────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .feed-card,
  .card__header,
  .card__footer,
  .portrait--a, .portrait--b,
  .person-info,
  .connector__fill,
  .connector__fill--bottom,
  .relation-pill,
  .other-rels-section,
  .other-rel,
  .children-section,
  .child-col {
    transition: none !important;
    animation: none !important;
    transform: none !important;
    opacity: 1 !important;
  }
  .connector__fill,
  .connector__fill--bottom { height: 100% !important; }
}
</style>
