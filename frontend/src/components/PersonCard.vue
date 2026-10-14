<template>
  <div class="person-card" v-if="person">
    <button class="close-btn" @click="$emit('close')" :aria-label="t.ui.close">&times;</button>
    <div class="card-header">
      <div
        class="avatar"
        :class="[avatarClass, { 'avatar-clickable': person.hasImage }]"
        :style="avatarStyle"
        :role="person.hasImage ? 'button' : undefined"
        :tabindex="person.hasImage ? '0' : undefined"
        :aria-label="person.hasImage ? `${prefName(person)}` : undefined"
        @click="person.hasImage && (showLightbox = true)"
        @keydown.enter="person.hasImage && (showLightbox = true)"
        @keydown.space.prevent="person.hasImage && (showLightbox = true)"
      >
        <span v-if="!person.hasImage">{{ initials }}</span>
      </div>
      <div class="header-info">
        <div class="name-row">
          <h2 :class="{ 'org-title': isOrg }">{{ prefName(person) }}</h2>
          <span v-if="isPerson && person.gender" class="gender-icon" :title="genderLabel">{{ genderIcon }}</span>
        </div>
        <p v-if="person.name_en && person.name_it && person.name_en !== person.name_it" class="name-en">{{ locale === 'en' ? person.name_it : person.name_en }}</p>
        <span v-if="!isPerson" class="entity-badge" :class="entityBadgeClass">{{ entityBadgeLabel }}</span>
        <p v-if="person.description_it || person.description_en || person.description" class="description">
          {{ prefDesc(person) }}
        </p>
      </div>
    </div>

    <p v-if="imageAttribution" class="image-credit">
      {{ t.ui.imageCredit }} <a :href="imageAttribution.sourceUrl" target="_blank" rel="noopener">{{ imageAttribution.creator }}</a>
      <template v-if="imageAttribution.licenseUrl"> · <a :href="imageAttribution.licenseUrl" target="_blank" rel="noopener">{{ imageAttribution.license }}</a></template>
      <span v-if="imageAttribution.modified"> · {{ t.ui.imageResized }}</span>
    </p>

    <!-- Person fields -->
    <div v-if="isPerson" class="details">
      <div v-if="person.birth_date || person.birth_place" class="detail-row">
        <span class="label">{{ t.person.born }}</span>
        <span class="detail-multiline">
          <span v-if="person.birth_date">{{ formatDate(person.birth_date) }}</span>
          <span v-if="person.birth_place" class="detail-place">{{ person.birth_place }}</span>
        </span>
      </div>
      <div v-if="person.death_date || person.death_place || person.cause_of_death" class="detail-row">
        <span class="label">{{ t.person.died }}</span>
        <span class="detail-multiline">
          <span v-if="person.death_date">{{ formatDate(person.death_date) }}</span>
          <span v-if="person.death_place" class="detail-place">{{ person.death_place }}</span>
          <span v-if="person.cause_of_death" class="detail-cause">{{ person.cause_of_death }}</span>
        </span>
      </div>
      <div v-if="person.occupations?.length" class="detail-row">
        <span class="label">{{ t.person.occupation }}</span>
        <span>{{ person.occupations.map(translateOccupation).join(', ') }}</span>
      </div>
    </div>

    <!-- Organization fields -->
    <div v-else-if="isOrg" class="details">
      <div v-if="person.type" class="detail-row">
        <span class="label">{{ t.organization.type }}</span>
        <span>{{ translateOrgType(person.type) }}</span>
      </div>
      <div v-if="person.founded" class="detail-row">
        <span class="label">{{ t.organization.founded }}</span>
        <span>{{ formatYear(person.founded) }}</span>
      </div>
      <div v-if="person.dissolved" class="detail-row">
        <span class="label">{{ t.organization.dissolved }}</span>
        <span>{{ formatYear(person.dissolved) }}</span>
      </div>
      <div v-if="person.hiddenMemberCount > 0 || memberLimit > 0" class="detail-row member-toggle-row">
        <span class="label">{{ t.ui.members }}</span>
        <span v-if="person.hiddenMemberCount > 0" class="hidden-count">{{ t.ui.othersNotShown.replace('{n}', person.hiddenMemberCount) }}</span>
        <div class="member-limit-btns">
          <button class="member-toggle-btn" :class="{ active: memberLimit === 0 }" @click="$emit('toggleOrgMembers', person.qid, 0)">20</button>
          <button class="member-toggle-btn" :class="{ active: memberLimit === 200 }" @click="$emit('toggleOrgMembers', person.qid, 200)">200</button>
          <button class="member-toggle-btn" :class="{ active: memberLimit === 1000 }" @click="$emit('toggleOrgMembers', person.qid, 1000)">1000</button>
        </div>
      </div>
    </div>

    <!-- Event fields -->
    <div v-else-if="isEvent" class="details">
      <div v-if="person.type" class="detail-row">
        <span class="label">{{ t.event.type }}</span>
        <span>{{ translateEventType(person.type) }}</span>
      </div>
      <div v-if="person.date" class="detail-row">
        <span class="label">{{ t.event.date }}</span>
        <span>{{ formatDate(person.date) }}</span>
      </div>
    </div>

    <div class="actions">
      <button v-if="layoutMode !== 'org-circle'" class="action-btn explore-btn" @click="$emit('enter', person.qid)">
        {{ t.graph.enter }}
      </button>
      <button
        class="action-btn investigate-btn"
        @click="$emit('investigate', person)"
        :aria-label="t.dossier.investigate"
        :title="t.dossier.investigate"
      >{{ t.dossier.investigate }}</button>
      <button
        class="action-btn save-btn"
        :class="{ saved: trivia.isBookmarked(person.qid) }"
        @click="toggleSave"
        :aria-label="trivia.isBookmarked(person.qid) ? t.saved.remove : t.saved.title"
      >
        {{ trivia.isBookmarked(person.qid) ? '★' : '☆' }}
        <span v-if="savedToast" class="save-toast">{{ t.saved.title }}</span>
      </button>
    </div>

    <Teleport to="body">
      <div
        v-if="showLightbox"
        class="lightbox-overlay"
        role="dialog"
        aria-modal="true"
        :aria-label="prefName(props.person)"
        @click.self="closeLightbox"
        @keydown.esc="closeLightbox"
        @keydown.tab.prevent="trapFocus"
      >
        <div class="lightbox-inner" ref="lightboxInnerEl">
          <button class="lightbox-close" ref="lightboxCloseEl" @click="closeLightbox" :aria-label="t.ui.close">&times;</button>
          <img
            :src="api.imageUrl(props.person.qid)"
            :alt="prefName(props.person)"
            class="lightbox-img"
          />
          <div class="lightbox-name">{{ prefName(props.person) }}</div>
          <p v-if="imageAttribution" class="lightbox-credit">
            {{ t.ui.imageCredit }}
            <a :href="imageAttribution.sourceUrl" target="_blank" rel="noopener">{{ imageAttribution.creator }}</a>
            <template v-if="imageAttribution.licenseUrl">
              · <a :href="imageAttribution.licenseUrl" target="_blank" rel="noopener">{{ imageAttribution.license }}</a>
            </template>
            <span v-if="imageAttribution.modified"> · {{ t.ui.imageResized }}</span>
          </p>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, ref, watch, nextTick } from 'vue'
import { useApi } from '../composables/useApi.js'
import { useLocale } from '../composables/useLocale.js'
import { useTrivia } from '../composables/useTrivia.js'

const { t, locale, prefName, prefDesc, dateLocale } = useLocale()
const api = useApi()
const trivia = useTrivia()
const props = defineProps({ person: Object, memberLimit: { type: Number, default: 0 }, layoutMode: { type: String, default: 'network' } })
defineEmits(['close', 'enter', 'toggleOrgMembers', 'investigate'])

const savedToast = ref(false)

function toggleSave() {
  trivia.toggleBookmark(props.person)
  if (!trivia.isBookmarked(props.person.qid)) return
  savedToast.value = true
  setTimeout(() => { savedToast.value = false }, 1800)
}

const showLightbox = ref(false)
const lightboxInnerEl = ref(null)
const lightboxCloseEl = ref(null)
let lightboxOpenerEl = null

watch(() => props.person?.qid, () => { showLightbox.value = false })
watch(showLightbox, async (val) => {
  if (val) {
    lightboxOpenerEl = document.activeElement
    await nextTick()
    lightboxCloseEl.value?.focus()
  } else {
    lightboxOpenerEl?.focus()
    lightboxOpenerEl = null
  }
})

function closeLightbox() {
  showLightbox.value = false
}

function trapFocus(e) {
  const el = lightboxInnerEl.value
  if (!el) return
  const focusable = [...el.querySelectorAll('button, [href], input, [tabindex]:not([tabindex="-1"])')].filter(n => !n.disabled)
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (e.shiftKey) {
    if (document.activeElement === first) { last.focus() }
  } else {
    if (document.activeElement === last) { first.focus() }
  }
}

const isPerson = computed(() => !props.person?.entityType || props.person.entityType === 'person')
const isOrg = computed(() => props.person?.entityType === 'organization')
const isEvent = computed(() => props.person?.entityType === 'event')

const imageDetails = ref(null)
watch(
  () => [props.person?.qid, props.person?.entityType, props.person?.hasImage],
  async ([qid, entityType, hasImage]) => {
    imageDetails.value = null
    if (!qid || !hasImage || entityType === 'event') return
    try {
      imageDetails.value = entityType === 'organization'
        ? await api.getOrganization(qid)
        : await api.getPerson(qid)
    } catch {
      imageDetails.value = null
    }
  },
  { immediate: true },
)

const imageAttribution = computed(() => {
  const image = imageDetails.value || props.person || {}
  if (!image.image_source_url && !image.image_license_url) return null
  return {
    creator: image.image_author || image.image_credit || t.value.ui.imageCreditFallback,
    sourceUrl: image.image_source_url || 'https://commons.wikimedia.org',
    license: image.image_license || t.value.ui.imageLicenseFallback,
    licenseUrl: image.image_license_url || image.image_source_url,
    modified: image.image_modified !== false,
  }
})

const entityBadgeLabel = computed(() => {
  if (isOrg.value) return t.value.entityBadge.organization
  if (isEvent.value) return t.value.entityBadge.event
  return ''
})
const entityBadgeClass = computed(() => {
  if (isOrg.value) return 'badge-org'
  if (isEvent.value) return 'badge-event'
  return ''
})

const initials = computed(() => {
  const name = prefName(props.person) || '?'
  return name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
})

const avatarClass = computed(() => {
  if (isOrg.value) return 'avatar-org'
  return 'avatar-person'
})

const avatarStyle = computed(() => {
  if (props.person?.hasImage && props.person?.qid) {
    return {
      backgroundImage: `url(${api.imageUrl(props.person.qid)})`,
      backgroundSize: isPerson.value ? 'cover' : 'contain',
      backgroundPosition: isPerson.value ? 'center top' : 'center',
      backgroundRepeat: 'no-repeat',
    }
  }
  return { backgroundColor: props.person?.color || 'var(--color-person)' }
})

const genderIcon = computed(() => {
  const g = props.person?.gender?.toLowerCase()
  if (g === 'male') return '\u2642\uFE0E'
  if (g === 'female') return '\u2640\uFE0E'
  return '\u26A5\uFE0E'
})

const genderLabel = computed(() => {
  const g = props.person?.gender?.toLowerCase()
  if (g === 'male') return t.value.gender.male
  if (g === 'female') return t.value.gender.female
  return t.value.gender.unknown
})

function formatDate(dateStr) {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr
  return d.toLocaleDateString(dateLocale.value, { year: 'numeric', month: 'long', day: 'numeric' })
}

function formatYear(dateStr) {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr
  return d.getFullYear().toString()
}

function translateOccupation(occ) {
  return t.value.occupations[occ] || occ?.replace(/_/g, ' ') || occ
}

function translateOrgType(type) {
  return t.value.orgTypes[type] || type?.replace(/_/g, ' ') || type
}

function translateEventType(type) {
  return t.value.eventTypes[type] || type?.replace(/_/g, ' ') || type
}
</script>

<style scoped>
.person-card {
  background: var(--card);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  width: 340px;
  padding: 20px;
  overflow-y: auto;
  position: absolute;
  top: 10px;
  right: 10px;
  max-height: calc(100% - 20px);
  z-index: 7;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.12);
}
.close-btn {
  position: absolute;
  top: 8px;
  right: 8px;
  background: none;
  border: none;
  color: rgba(28, 21, 16, 0.4);
  font-size: 22px;
  cursor: pointer;
  padding: 0;
  border-radius: 4px;
  min-width: 44px;
  min-height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.close-btn:hover {
  color: var(--text);
  background: rgba(0, 0, 0, 0.07);
}
.card-header {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  margin-bottom: 14px;
  padding-right: 48px;
}
.avatar {
  width: 72px;
  height: 72px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 20px;
  color: #fff;
  flex-shrink: 0;
}
.avatar-person {
  width: 64px;
  height: 84px;
  border-radius: 9px;
}
.avatar-org {
  border-radius: 8px;
}
.name-row {
  display: flex;
  align-items: flex-start;
  gap: 6px;
}
.header-info {
  flex: 1;
  min-width: 0;
}
.header-info h2 {
  margin: 0;
  font-size: 18px;
  font-family: var(--font-display);
  font-weight: 700;
  line-height: 1.3;
  color: var(--text);
  flex: 1;
  min-width: 0;
  overflow-wrap: break-word;
  word-break: break-word;
}
.org-title {
  font-style: italic;
}
.gender-icon {
  font-size: 17px;
  line-height: 1.3;
  color: var(--text-muted);
  cursor: default;
  flex-shrink: 0;
  align-self: flex-start;
  margin-top: 1px;
  user-select: none;
}
.name-en {
  color: rgba(28, 21, 16, 0.55);
  font-size: 13px;
  margin: 2px 0 0;
}
.entity-badge {
  display: inline-block;
  margin-top: 5px;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.badge-org {
  background: rgba(61, 145, 135, 0.12);
  color: var(--color-org);
  border: 1px solid rgba(61, 145, 135, 0.3);
}
.badge-event {
  background: rgba(206, 145, 120, 0.12);
  color: var(--color-event-text);
  border: 1px solid rgba(206, 145, 120, 0.3);
}
.description {
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.45;
  margin: 6px 0 0;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.image-credit {
  margin: -4px 0 14px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
}
.image-credit a { color: var(--accent); }
.image-credit a:hover { text-decoration: underline; }
.details {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 20px;
}
.detail-row {
  display: flex;
  gap: 8px;
  font-size: 13px;
  align-items: baseline;
}
.detail-multiline {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.detail-place {
  font-size: 12px;
  color: rgba(28, 21, 16, 0.55);
  overflow-wrap: break-word;
  word-break: break-word;
}
.detail-cause {
  font-size: 12px;
  color: rgba(28, 21, 16, 0.45);
  font-style: italic;
  overflow-wrap: break-word;
  word-break: break-word;
}
.label {
  color: rgba(28, 21, 16, 0.5);
  min-width: 90px;
  flex-shrink: 0;
}
.actions {
  display: flex;
  gap: 8px;
}
.action-btn {
  flex: 1;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.04);
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  transition: background 0.2s, transform 0.1s;
}
.action-btn:hover {
  background: rgba(0, 0, 0, 0.08);
}
.action-btn:active {
  background: rgba(0, 0, 0, 0.12);
  transform: scale(0.98);
}
.path-btn {
  border-color: rgba(27, 77, 142, 0.35);
  color: var(--accent);
}
.path-btn:hover {
  background: rgba(27, 77, 142, 0.08);
}
.explore-btn {
  border-color: rgba(27, 77, 142, 0.35);
  color: var(--accent);
  font-weight: 600;
  letter-spacing: 0.02em;
}
.explore-btn:hover {
  background: rgba(27, 77, 142, 0.08);
}
.save-btn {
  position: relative;
  flex: 0 0 auto;
  min-width: 44px;
  font-size: 18px;
  line-height: 1;
}
.save-btn.saved {
  color: var(--color-bookmark, #c88b00);
  border-color: rgba(200, 139, 0, 0.4);
  background: rgba(200, 139, 0, 0.07);
}
.save-btn.saved:hover {
  background: rgba(200, 139, 0, 0.14);
}
.save-toast {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  background: var(--text);
  color: var(--bg);
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
  padding: 3px 8px;
  border-radius: 6px;
  pointer-events: none;
  animation: toast-in 0.15s ease;
}
@keyframes toast-in {
  from { opacity: 0; transform: translateX(-50%) translateY(4px); }
  to   { opacity: 1; transform: translateX(-50%) translateY(0); }
}
.avatar-clickable {
  cursor: pointer;
  transition: opacity 0.15s;
}
.avatar-clickable:hover {
  opacity: 0.82;
}
.hidden-count {
  color: var(--color-event-text);
  opacity: 0.75;
  font-style: italic;
}
.member-toggle-row {
  flex-wrap: wrap;
}
.member-limit-btns {
  display: flex;
  gap: 4px;
  width: 100%;
  margin-top: 4px;
}
.member-toggle-btn {
  flex: 1;
  padding: 4px 0;
  border: 1px solid rgba(61, 145, 135, 0.35);
  border-radius: 5px;
  background: rgba(61, 145, 135, 0.08);
  color: var(--color-org);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s, color 0.2s;
}
.member-toggle-btn:hover {
  background: rgba(61, 145, 135, 0.16);
}
.member-toggle-btn.active {
  background: var(--color-org);
  color: #fff;
  border-color: var(--color-org);
}

/* ── Desktop readability: the side panel sits beside a large graph, so bump
   the body/label text to comfortable editorial sizes. Mobile/tablet untouched. */
@media (min-width: 1024px) {
  .person-card {
    width: 380px;
    padding: 24px;
  }
  .header-info h2 { font-size: 21px; }
  .name-en { font-size: 14px; }
  .description { font-size: 14px; line-height: 1.5; }
  .detail-row { font-size: 15px; }
  .detail-place,
  .detail-cause { font-size: 13.5px; }
  .label { min-width: 96px; }
  .entity-badge { font-size: 12px; }
  .action-btn { font-size: 14px; padding: 10px; }
  .member-toggle-btn { font-size: 13px; }
}

@media (max-width: 768px) {
  .person-card {
    position: relative;
    top: auto;
    right: auto;
    width: 100%;
    border: none;
    border-top: 1px solid var(--border-subtle);
    border-radius: 0;
    max-height: 45vh;
    box-shadow: none;
    padding: 16px;
  }
  .card-header {
    margin-bottom: 12px;
    align-items: flex-start;
  }
  .description {
    -webkit-line-clamp: 2;
    font-size: 11.5px;
  }
  /* More breathing room on mobile for action buttons */
  .actions {
    gap: 12px;
  }
  .action-btn {
    padding: 12px 8px;
  }
}
</style>

<style>
.lightbox-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}
.lightbox-inner {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
.lightbox-img {
  max-width: 80vw;
  max-height: 80vh;
  object-fit: contain;
  border-radius: 8px;
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.5);
}
.lightbox-close {
  position: absolute;
  top: -22px;
  right: -22px;
  width: 44px;
  height: 44px;
  background: var(--bg-panel);
  border: 1px solid rgba(0, 0, 0, 0.2);
  border-radius: 50%;
  color: var(--text);
  font-size: 20px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
}
.lightbox-close:hover {
  background: var(--bg);
}
.lightbox-name {
  color: rgba(255, 255, 255, 0.8);
  font-size: 14px;
}
.lightbox-credit {
  max-width: min(80vw, 680px);
  margin: -4px 16px 0;
  color: rgba(255, 255, 255, 0.72);
  font-size: 12px;
  line-height: 1.5;
  text-align: center;
}
.lightbox-credit a {
  color: #b9d8ff;
  text-decoration: underline;
  text-underline-offset: 2px;
}
</style>
