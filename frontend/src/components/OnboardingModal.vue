<template>
  <Teleport to="body">
    <div
      class="onboarding-overlay"
      @click.self="handleSkip"
      role="dialog"
      aria-modal="true"
      :aria-label="t.onboarding.title"
    >
      <div class="onboarding-card" @touchstart.passive="onTouchStart" @touchend.passive="onTouchEnd">

        <!-- Skip -->
        <button class="onboarding-skip" @click="handleSkip" :aria-label="t.onboarding.skip">
          {{ t.onboarding.skip }}
        </button>

        <!-- Slide viewport -->
        <div class="slides-viewport">
          <Transition :name="slideDir" mode="out-in">
            <div :key="current" class="slide">

              <!-- Slide 0: Welcome -->
              <template v-if="current === 0">
                <div class="slide-icon">
                  <svg width="64" height="64" viewBox="0 0 64 64" fill="none" aria-hidden="true">
                    <circle cx="32" cy="32" r="30" stroke="var(--accent)" stroke-width="2" fill="var(--canvas-bg)"/>
                    <circle cx="32" cy="20" r="5" fill="var(--accent)" opacity="0.9"/>
                    <circle cx="16" cy="38" r="5" fill="var(--color-person)" opacity="0.9"/>
                    <circle cx="48" cy="38" r="5" fill="var(--color-org)" opacity="0.9"/>
                    <circle cx="32" cy="50" r="4" fill="var(--color-event)" opacity="0.9"/>
                    <line x1="32" y1="25" x2="19" y2="34" stroke="var(--accent)" stroke-width="1.5" stroke-opacity="0.5"/>
                    <line x1="32" y1="25" x2="45" y2="34" stroke="var(--accent)" stroke-width="1.5" stroke-opacity="0.5"/>
                    <line x1="19" y1="42" x2="30" y2="47" stroke="var(--color-person)" stroke-width="1.5" stroke-opacity="0.5"/>
                    <line x1="45" y1="42" x2="34" y2="47" stroke="var(--color-org)" stroke-width="1.5" stroke-opacity="0.5"/>
                  </svg>
                </div>
                <h2 class="slide-title">{{ t.onboarding.slide0.title }}</h2>
                <p class="slide-body">{{ t.onboarding.slide0.body }}</p>
              </template>

              <!-- Slide 1: Explore -->
              <template v-else-if="current === 1">
                <div class="slide-icon">
                  <svg width="64" height="64" viewBox="0 0 64 64" fill="none" aria-hidden="true">
                    <circle cx="32" cy="32" r="30" stroke="var(--accent)" stroke-width="2" fill="var(--canvas-bg)"/>
                    <circle cx="28" cy="27" r="10" stroke="var(--accent)" stroke-width="2.5" fill="none"/>
                    <line x1="35" y1="35" x2="46" y2="46" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round"/>
                    <circle cx="28" cy="27" r="3" fill="var(--color-person)" opacity="0.85"/>
                    <circle cx="22" cy="21" r="2.5" fill="var(--color-org)" opacity="0.7"/>
                    <circle cx="34" cy="22" r="2.5" fill="var(--color-event)" opacity="0.7"/>
                    <line x1="28" y1="27" x2="22" y2="21" stroke="var(--border-visible)" stroke-width="1"/>
                    <line x1="28" y1="27" x2="34" y2="22" stroke="var(--border-visible)" stroke-width="1"/>
                  </svg>
                </div>
                <h2 class="slide-title">{{ t.onboarding.slide1.title }}</h2>
                <p class="slide-body">{{ t.onboarding.slide1.body }}</p>
                <ul class="slide-tips">
                  <li v-for="tip in t.onboarding.slide1.tips" :key="tip" v-html="'<span class=\'tip-dot\'>·</span>' + tip" />
                </ul>
              </template>

              <!-- Slide 2: Trivia -->
              <template v-else-if="current === 2">
                <div class="slide-icon">
                  <svg width="64" height="64" viewBox="0 0 64 64" fill="none" aria-hidden="true">
                    <circle cx="32" cy="32" r="30" stroke="var(--accent)" stroke-width="2" fill="var(--canvas-bg)"/>
                    <rect x="14" y="17" width="36" height="24" rx="5" stroke="var(--accent)" stroke-width="2" fill="var(--surface)"/>
                    <rect x="19" y="22" width="16" height="3" rx="1.5" fill="var(--color-person)" opacity="0.85"/>
                    <rect x="19" y="28" width="24" height="2.5" rx="1.25" fill="var(--border-visible)"/>
                    <rect x="19" y="32.5" width="20" height="2.5" rx="1.25" fill="var(--border-visible)"/>
                    <circle cx="44" cy="48" r="7" fill="var(--accent)"/>
                    <text x="44" y="52" text-anchor="middle" font-size="10" font-weight="700" fill="white">?</text>
                  </svg>
                </div>
                <h2 class="slide-title">{{ t.onboarding.slide2.title }}</h2>
                <p class="slide-body">{{ t.onboarding.slide2.body }}</p>
                <ul class="slide-tips">
                  <li v-for="tip in t.onboarding.slide2.tips" :key="tip" v-html="'<span class=\'tip-dot\'>·</span>' + tip" />
                </ul>
              </template>

              <!-- Slide 3: Saved People -->
              <template v-else-if="current === 3">
                <div class="slide-icon">
                  <svg width="64" height="64" viewBox="0 0 64 64" fill="none" aria-hidden="true">
                    <circle cx="32" cy="32" r="30" stroke="var(--accent)" stroke-width="2" fill="var(--canvas-bg)"/>
                    <path d="M32 14 L36 24 L47 25 L39 33 L41 44 L32 39 L23 44 L25 33 L17 25 L28 24 Z"
                      stroke="var(--color-bookmark,#c88b00)" stroke-width="2" fill="rgba(200,139,0,0.15)"
                      stroke-linejoin="round"/>
                    <circle cx="32" cy="44" r="8" fill="var(--surface)" stroke="var(--border)" stroke-width="1.5"/>
                    <text x="32" y="48.5" text-anchor="middle" font-size="10" fill="var(--accent)">&#9873;</text>
                  </svg>
                </div>
                <h2 class="slide-title">{{ t.onboarding.slide3.title }}</h2>
                <p class="slide-body">{{ t.onboarding.slide3.body }}</p>
              </template>

            </div>
          </Transition>
        </div>

        <!-- Progress dots -->
        <div class="slide-dots" role="tablist" :aria-label="t.onboarding.slideNav">
          <button
            v-for="(_, i) in 4"
            :key="i"
            class="dot"
            :class="{ active: i === current }"
            @click="jumpTo(i)"
            :aria-label="`${t.onboarding.slide} ${i + 1}`"
            :aria-selected="i === current"
            role="tab"
          />
        </div>

        <!-- Footer nav -->
        <div class="onboarding-nav">
          <div class="onboarding-lang">
            <select
              class="lang-select"
              :value="locale"
              @change="setLocale($event.target.value)"
              :aria-label="t.ui.language"
            >
              <option v-for="l in SUPPORTED_LOCALES" :key="l.code" :value="l.code">{{ l.label }}</option>
            </select>
          </div>

          <button
            v-if="current > 0"
            class="nav-btn nav-prev"
            @click="prev"
          >
            ← {{ t.onboarding.prev }}
          </button>
          <div v-else class="nav-spacer" />

          <button
            v-if="current < 3"
            class="nav-btn nav-next"
            @click="next"
          >
            {{ t.onboarding.next }} →
          </button>
          <button
            v-else
            class="nav-btn nav-cta"
            @click="handleComplete"
          >
            {{ t.onboarding.getStarted }}
          </button>
        </div>

      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { ref } from 'vue'
import { useLocale } from '../composables/useLocale.js'

const emit = defineEmits(['complete'])
const { t, locale, setLocale, SUPPORTED_LOCALES } = useLocale()

const current = ref(0)
const slideDir = ref('slide-next')

function prev() {
  slideDir.value = 'slide-prev'
  current.value--
}

function next() {
  slideDir.value = 'slide-next'
  current.value++
}

function jumpTo(i) {
  slideDir.value = i > current.value ? 'slide-next' : 'slide-prev'
  current.value = i
}

function handleComplete() {
  emit('complete')
}

function handleSkip() {
  emit('complete')
}

// Touch swipe support
const touchStartX = ref(0)

function onTouchStart(e) {
  touchStartX.value = e.touches[0].clientX
}

function onTouchEnd(e) {
  const delta = touchStartX.value - e.changedTouches[0].clientX
  if (Math.abs(delta) < 40) return
  if (delta > 0 && current.value < 3) next()
  else if (delta < 0 && current.value > 0) prev()
}
</script>

<style scoped>
.onboarding-overlay {
  position: fixed;
  inset: 0;
  background: rgba(28, 21, 16, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
  padding: 1rem;
  padding-top: max(1rem, env(safe-area-inset-top));
  padding-bottom: max(1rem, env(safe-area-inset-bottom));
  backdrop-filter: blur(2px);
  overflow-y: auto;
}

.onboarding-card {
  background: var(--bg-panel-solid, rgba(249,248,244,0.97));
  border: 1px solid var(--border, rgba(0,0,0,0.15));
  border-radius: 16px;
  padding: 2.25rem 2.5rem 1.75rem;
  max-width: 460px;
  width: 100%;
  position: relative;
  box-shadow: 0 8px 32px rgba(28, 21, 16, 0.18), 0 2px 8px rgba(28, 21, 16, 0.08);
  max-height: calc(100dvh - 2rem);
  overflow-y: auto;
  flex-shrink: 0;
}

@media (max-width: 520px) {
  .onboarding-card {
    padding: 1.5rem 1.25rem 1.25rem;
    border-radius: 12px;
    max-height: calc(100dvh - 2rem);
  }
}

/* Skip button */
.onboarding-skip {
  position: absolute;
  top: 1rem;
  right: 1rem;
  background: none;
  border: none;
  color: rgba(28, 21, 16, 0.45);
  font-family: 'DM Sans', sans-serif;
  font-size: 0.8rem;
  cursor: pointer;
  padding: 0.25rem 0.5rem;
  border-radius: 4px;
  transition: color 0.15s;
  line-height: 1;
}
.onboarding-skip:hover {
  color: rgba(28, 21, 16, 0.75);
}

/* Slides viewport */
.slides-viewport {
  overflow: hidden;
  min-height: 260px;
  display: flex;
  align-items: flex-start;
}

@media (max-width: 520px) {
  .slides-viewport {
    min-height: 200px;
  }
  .slide-icon {
    width: 60px;
    height: 60px;
    margin-bottom: 1rem;
  }
  .slide-title {
    font-size: 1.2rem;
  }
  .slide-body {
    font-size: 0.85rem;
  }
}

.slide {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding-bottom: 0.5rem;
}

/* Slide icon */
.slide-icon {
  width: 80px;
  height: 80px;
  background: var(--canvas-bg, #f2ede3);
  border: 1px solid var(--border-subtle, rgba(0,0,0,0.10));
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 1.5rem;
  flex-shrink: 0;
}

/* Slide text */
.slide-title {
  font-family: 'Playfair Display', Georgia, serif;
  font-size: 1.4rem;
  font-weight: 700;
  color: var(--text, #1c1510);
  margin: 0 0 0.75rem;
  line-height: 1.25;
}

.slide-body {
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 0.9rem;
  line-height: 1.65;
  color: rgba(28, 21, 16, 0.7);
  margin: 0 0 1rem;
  max-width: 340px;
}

.slide-tips {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  align-self: stretch;
  text-align: left;
  padding: 0.75rem 1rem;
  background: var(--canvas-bg, #f2ede3);
  border-radius: 8px;
  border: 1px solid var(--border-faint, rgba(0,0,0,0.06));
}

.slide-tips li {
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 0.82rem;
  color: rgba(28, 21, 16, 0.7);
  line-height: 1.5;
  display: flex;
  gap: 0.5rem;
}

.tip-dot {
  color: var(--accent, #1b4d8e);
  font-weight: 700;
  flex-shrink: 0;
}

/* Dots */
.slide-dots {
  display: flex;
  justify-content: center;
  gap: 0.5rem;
  margin: 1.25rem 0 1rem;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--border-visible, rgba(0,0,0,0.22));
  border: none;
  cursor: pointer;
  padding: 0;
  transition: background 0.2s, transform 0.2s;
  flex-shrink: 0;
}

.dot.active {
  background: var(--accent, #1b4d8e);
  transform: scale(1.35);
}

/* Nav row */
.onboarding-nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.onboarding-lang {
  flex-shrink: 0;
}

.lang-select {
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.3rem 0.5rem;
  border: 1px solid var(--border-subtle, rgba(0,0,0,0.12));
  border-radius: 6px;
  background: var(--canvas-bg, #f2ede3);
  color: var(--accent, #1b4d8e);
  cursor: pointer;
  outline: none;
}

.lang-select:focus {
  border-color: var(--accent, #1b4d8e);
}

.nav-spacer {
  flex: 1;
}

.nav-btn {
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 0.875rem;
  font-weight: 600;
  border-radius: 8px;
  padding: 0.5rem 1.1rem;
  cursor: pointer;
  transition: background 0.15s, color 0.15s, opacity 0.15s;
  line-height: 1;
  white-space: nowrap;
}

.nav-prev {
  background: none;
  border: 1px solid var(--border, rgba(0,0,0,0.15));
  color: rgba(28, 21, 16, 0.6);
}
.nav-prev:hover {
  background: var(--canvas-bg, #f2ede3);
  color: var(--text, #1c1510);
}

.nav-next {
  background: none;
  border: 1px solid var(--accent, #1b4d8e);
  color: var(--accent, #1b4d8e);
}
.nav-next:hover {
  background: rgba(27, 77, 142, 0.06);
}

.nav-cta {
  background: var(--accent, #1b4d8e);
  border: 1px solid var(--accent, #1b4d8e);
  color: #fff;
}
.nav-cta:hover {
  background: var(--accent-hover, #153d73);
  border-color: var(--accent-hover, #153d73);
}

/* Slide transitions */
.slide-next-enter-active,
.slide-next-leave-active,
.slide-prev-enter-active,
.slide-prev-leave-active {
  transition: opacity 0.22s ease, transform 0.22s ease;
}

.slide-next-enter-from {
  opacity: 0;
  transform: translateX(28px);
}
.slide-next-leave-to {
  opacity: 0;
  transform: translateX(-28px);
}

.slide-prev-enter-from {
  opacity: 0;
  transform: translateX(-28px);
}
.slide-prev-leave-to {
  opacity: 0;
  transform: translateX(28px);
}

@media (prefers-reduced-motion: reduce) {
  .slide-next-enter-active,
  .slide-next-leave-active,
  .slide-prev-enter-active,
  .slide-prev-leave-active {
    transition: opacity 0.15s ease;
  }
  .slide-next-enter-from,
  .slide-next-leave-to,
  .slide-prev-enter-from,
  .slide-prev-leave-to {
    transform: none;
  }
}

@media (max-width: 420px) {
  .onboarding-nav {
    flex-wrap: wrap;
    row-gap: 8px;
  }
  .nav-cta,
  .nav-prev {
    flex: 1 1 auto;
  }
  .nav-spacer {
    display: none;
  }
}
</style>
