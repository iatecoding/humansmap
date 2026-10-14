<template>
  <!-- Backdrop: closes panels when clicking outside them -->
  <div v-if="showSettings || showSaved || showShareMenu" class="panel-backdrop" @click="closeAll" />

  <!-- Saved people panel -->
  <div v-if="showSaved" class="settings-panel saved-panel" @click.stop>
    <div class="settings-header">
      <h4>{{ t.saved.title }}</h4>
      <button class="settings-close" @click="showSaved = false" :aria-label="t.ui.close">&times;</button>
    </div>
    <p v-if="bookmarks.length === 0" class="saved-empty">{{ t.saved.empty }}</p>
    <div v-else class="saved-list">
      <div v-for="person in bookmarks" :key="person.qid" class="saved-item">
        <img
          class="saved-avatar"
          :src="imageUrl(person.qid)"
          :alt="person.name_it || person.name_en"
          @error="$event.target.style.display='none'"
        />
        <span class="saved-name">{{ locale === 'it' ? (person.name_it || person.name_en) : (person.name_en || person.name_it) }}</span>
        <div class="saved-actions">
          <button class="saved-btn" @click="$emit('savedExplore', person)">{{ t.saved.explore }}</button>
          <button class="saved-btn saved-btn-remove" @click="$emit('removeBookmark', person)">&times;</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Settings panel -->
  <div v-if="showSettings" class="settings-panel" @click.stop>
    <div class="settings-header">
      <h4>{{ t.ui.visualisation }}</h4>
      <button class="settings-close" @click="showSettings = false" :aria-label="t.ui.closeSettings">&times;</button>
    </div>
    <label class="setting-row">
      <input type="checkbox" v-model="vizSettings.showEdges" />
      <span>{{ t.ui.showEdges }}</span>
    </label>
    <label class="setting-row">
      <input type="checkbox" v-model="vizSettings.showEdgeLabels" />
      <span>{{ t.ui.edgeLabels }}</span>
    </label>
    <label class="setting-row">
      <input type="checkbox" v-model="vizSettings.showNodeLabels" />
      <span>{{ t.ui.nodeLabels }}</span>
    </label>
    <label class="setting-row">
      <input type="checkbox" v-model="vizSettings.focusOnHover" />
      <span>{{ t.ui.highlightHover }}</span>
    </label>
    <div class="setting-divider"></div>
    <div class="setting-row setting-row--lang">
      <span>{{ t.ui.language }}</span>
      <select class="lang-select" :value="locale" @change="setLocale($event.target.value)">
        <option v-for="l in SUPPORTED_LOCALES" :key="l.code" :value="l.code">{{ l.label }}</option>
      </select>
    </div>
  </div>

  <!-- Zoom controls + action buttons (bottom-right) -->
  <div class="zoom-controls">
    <button v-show="graphLoaded" class="zoom-btn" @click="$emit('zoomIn')" :aria-label="t.ui.zoomIn">+</button>
    <button v-show="graphLoaded" class="zoom-btn" @click="$emit('zoomOut')" :aria-label="t.ui.zoomOut">&minus;</button>
    <button v-show="graphLoaded" class="zoom-btn" @click="$emit('zoomFit')" :aria-label="t.ui.zoomFit">&#8596;</button>
    <button
      v-show="graphLoaded"
      class="share-btn"
      @click="toggleShareMenu"
      :aria-label="t.ui.share"
      :title="t.ui.share"
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>
        <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>
      </svg>
    </button>

    <!-- Share menu popup -->
    <Transition name="toast-fade">
      <div v-if="showShareMenu" class="share-menu" @click.stop>
        <button class="share-menu-item" @click="handleCopyLink">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
            <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
          </svg>
          <span>{{ t.ui.copyLink }}</span>
        </button>
        <button class="share-menu-item" @click="handleScreenshot">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/>
            <polyline points="21 15 16 10 5 21"/>
          </svg>
          <span>{{ t.ui.screenshot }}</span>
        </button>
      </div>
    </Transition>

    <!-- Link copied toast -->
    <Transition name="toast-fade">
      <div v-if="showCopiedToast" class="copied-toast">{{ t.ui.linkCopied }}</div>
    </Transition>
    <button
      @click="$emit('showHelp')"
      class="settings-btn help-btn"
      :aria-label="t.onboarding.title"
      title="?"
    >?</button>
    <button
      @click="toggleSaved"
      :aria-expanded="showSaved"
      class="settings-btn bookmark-btn"
      :class="{ active: showSaved }"
      :aria-label="t.saved.title"
    >
      ★
      <span v-if="bookmarks.length > 0" class="bookmark-badge">{{ bookmarks.length }}</span>
    </button>
    <button
      @click="toggleSettings"
      :aria-label="t.ui.visualisationSettings"
      :aria-expanded="showSettings"
      class="settings-btn"
    >&#9881;</button>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useLocale } from '../composables/useLocale.js'

const { t, locale, setLocale, SUPPORTED_LOCALES } = useLocale()

const props = defineProps({
  graphLoaded: { type: Boolean, default: false },
  bookmarks: { type: Array, default: () => [] },
  vizSettings: { type: Object, required: true },
  imageUrl: { type: Function, required: true },
  showLeaderboard: { type: Boolean, default: false },
  rootQid: { type: String, default: null },
  rootName: { type: String, default: null },
})

const emit = defineEmits([
  'zoomIn', 'zoomOut', 'zoomFit', 'showHelp',
  'savedExplore', 'removeBookmark',
  'update:showLeaderboard',
  'screenshot',
])

const showCopiedToast = ref(false)
const showShareMenu = ref(false)

function toggleShareMenu() {
  showShareMenu.value = !showShareMenu.value
  showSettings.value = false
  showSaved.value = false
}

async function handleCopyLink() {
  if (!props.rootQid) return
  const url = `https://humansmap.com${window.location.pathname}`
  try {
    await navigator.clipboard.writeText(url)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = url
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
  showShareMenu.value = false
  showCopiedToast.value = true
  setTimeout(() => { showCopiedToast.value = false }, 2200)
}

function handleScreenshot() {
  showShareMenu.value = false
  emit('screenshot')
}

const showSettings = ref(false)
const showSaved = ref(false)

function closeAll() {
  showSettings.value = false
  showSaved.value = false
  showShareMenu.value = false
  emit('update:showLeaderboard', false)
}

function toggleSaved() {
  showSaved.value = !showSaved.value
  showSettings.value = false
  showShareMenu.value = false
  emit('update:showLeaderboard', false)
}

function toggleSettings() {
  showSettings.value = !showSettings.value
  showSaved.value = false
  showShareMenu.value = false
  emit('update:showLeaderboard', false)
}
</script>

<style scoped>
.zoom-controls {
  position: absolute;
  bottom: 12px;
  bottom: max(12px, env(safe-area-inset-bottom, 0px));
  right: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  z-index: 6;
}
.zoom-controls > button {
  width: 44px;
  height: 44px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: rgba(28, 21, 16, 0.7);
  font-size: 18px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s, transform 0.1s;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}
.zoom-controls > button:hover {
  background: var(--bg);
  color: var(--text);
}
.zoom-controls > button:active {
  background: var(--surface);
  transform: scale(0.93);
}
.settings-btn {
  margin-top: 8px;
}
.share-btn svg {
  display: block;
  pointer-events: none;
}
.share-menu {
  position: absolute;
  bottom: 0;
  right: calc(100% + 10px);
  background: var(--bg-panel-solid, #fff);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 6px;
  min-width: 180px;
  z-index: 7;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.12);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.share-menu-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 10px 14px;
  border: none;
  background: none;
  border-radius: 7px;
  font-size: 13.5px;
  font-weight: 500;
  color: var(--text);
  cursor: pointer;
  transition: background 0.15s;
  white-space: nowrap;
}
.share-menu-item:hover {
  background: rgba(0, 0, 0, 0.06);
}
.share-menu-item svg {
  flex-shrink: 0;
  width: 15px;
  height: 15px;
  opacity: 0.65;
}
.copied-toast {
  position: absolute;
  bottom: 100%;
  right: 0;
  margin-bottom: 8px;
  background: rgba(28, 21, 16, 0.88);
  color: #f9f8f4;
  font-size: 12px;
  font-weight: 500;
  padding: 6px 12px;
  border-radius: 6px;
  white-space: nowrap;
  pointer-events: none;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18);
}
.toast-fade-enter-active,
.toast-fade-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.toast-fade-enter-from,
.toast-fade-leave-to {
  opacity: 0;
  transform: translateY(4px);
}
.bookmark-btn {
  position: relative;
}
.bookmark-btn.active {
  background: var(--accent);
  color: #fff;
}
.bookmark-badge {
  position: absolute;
  top: 4px;
  right: 4px;
  background: #e64553;
  color: #fff;
  font-size: 9px;
  font-weight: 700;
  border-radius: 8px;
  padding: 1px 4px;
  line-height: 1.3;
  min-width: 14px;
  text-align: center;
  pointer-events: none;
}
.panel-backdrop {
  position: absolute;
  inset: 0;
  z-index: 6;
}
.settings-panel {
  position: absolute;
  bottom: 12px;
  right: 64px;
  background: var(--bg-panel-solid);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px;
  min-width: 200px;
  z-index: 7;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.12);
}
.saved-panel {
  max-height: 360px;
  display: flex;
  flex-direction: column;
  min-width: 260px;
}
.settings-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.settings-header h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.settings-close {
  background: none;
  border: none;
  color: rgba(28, 21, 16, 0.4);
  font-size: 20px;
  cursor: pointer;
  padding: 0 2px;
  line-height: 1;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
}
.settings-close:hover {
  color: var(--text);
  background: rgba(0, 0, 0, 0.07);
}
.setting-divider {
  height: 1px;
  background: var(--border-faint);
  margin: 8px 0;
}
.setting-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  cursor: pointer;
  font-size: 13px;
  color: rgba(28, 21, 16, 0.75);
}
.setting-row input {
  accent-color: var(--accent);
}
.setting-row--lang {
  align-items: center;
}
.lang-select {
  margin-left: auto;
  padding: 4px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  outline: none;
}
.lang-select:focus {
  border-color: var(--accent);
}
.saved-empty {
  font-size: 13px;
  color: rgba(28, 21, 16, 0.45);
  margin: 0;
  padding: 8px 0;
  text-align: center;
}
.saved-list {
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-right: 2px;
}
.saved-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 4px;
  border-radius: 6px;
  transition: background 0.15s;
}
.saved-item:hover {
  background: rgba(0, 0, 0, 0.04);
}
.saved-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
  background: var(--surface);
  border: 1px solid var(--border-faint);
}
.saved-name {
  flex: 1;
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.saved-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}
.saved-btn {
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
}
.saved-btn:hover {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.saved-btn-remove {
  color: rgba(28, 21, 16, 0.45);
  font-size: 14px;
  padding: 2px 7px;
}
.saved-btn-remove:hover {
  background: #e64553;
  border-color: #e64553;
  color: #fff;
}
@media (max-width: 768px) {
  .zoom-controls {
    right: 8px;
    bottom: max(8px, env(safe-area-inset-bottom, 0px));
    flex-direction: row;
    align-items: center;
  }
  .zoom-btn {
    display: none !important;
  }
  .settings-btn {
    margin-top: 0;
  }
  .settings-panel {
    right: 8px;
    bottom: calc(60px + env(safe-area-inset-bottom, 0px));
    width: min(280px, calc(100% - 16px));
    min-width: 0;
  }
  .share-menu {
    right: 0;
    bottom: calc(100% + 8px);
    min-width: 160px;
  }
  .share-menu-item {
    padding: 9px 12px;
    font-size: 13px;
  }
}
</style>
