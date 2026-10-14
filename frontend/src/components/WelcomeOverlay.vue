<template>
  <div class="welcome-overlay">
    <div class="welcome-content">

      <!-- Title -->
      <h1 class="welcome-title">{{ t.app.title }}</h1>
      <p class="welcome-tagline">{{ t.tour.welcome }}</p>

      <!-- Showcase cards -->
      <div class="showcase-cards" role="list">
        <button
          v-for="person in showcasePeople"
          :key="person.qid"
          class="showcase-card"
          role="listitem"
          @click="$emit('select', person)"
        >
          <div class="card-photo-wrap">
            <img
              v-if="!imgErrors[person.qid]"
              :src="`/api/images/${person.qid}.jpg`"
              :alt="displayName(person)"
              class="card-photo"
              loading="eager"
              @error="imgErrors[person.qid] = true"
            />
            <div v-else class="card-photo card-initials" aria-hidden="true">
              {{ displayName(person).charAt(0).toUpperCase() }}
            </div>
          </div>
          <div class="card-name">{{ displayName(person) }}</div>
          <div class="card-desc">{{ displayDesc(person) }}</div>
        </button>
      </div>

      <!-- Prompt -->
      <p class="welcome-prompt">{{ t.tour.pickPrompt }}</p>

      <!-- Language selector -->
      <select
        class="lang-select"
        :value="locale"
        @change="setLocale($event.target.value)"
        :aria-label="t.ui?.language || 'Language'"
      >
        <option v-for="l in SUPPORTED_LOCALES" :key="l.code" :value="l.code">{{ l.label }}</option>
      </select>

      <!-- Skip link -->
      <button class="skip-btn" @click="$emit('skip')">
        {{ t.tour.skip }} →
      </button>

    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useLocale } from '../composables/useLocale.js'

const props = defineProps({
  showcasePeople: { type: Array, default: () => [] },
})

defineEmits(['select', 'skip'])

const { locale, t, setLocale, SUPPORTED_LOCALES } = useLocale()

// Track per-QID image load errors for fallback to initials
const imgErrors = ref({})

function displayName(p) {
  if (locale.value === 'it') return p.name_it || p.name_en || ''
  return p.name_en || p.name_it || ''
}

function displayDesc(p) {
  if (locale.value === 'it') return p.description_it || p.description_en || ''
  return p.description_en || p.description_it || ''
}
</script>

<style scoped>
.welcome-overlay {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  /* pointer-events on parent may be none; children opt back in via pointer-events: auto */
  pointer-events: none;
}

.welcome-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  pointer-events: auto;
  padding: 2rem 1rem;
}

/* Title */
.welcome-title {
  font-family: 'Playfair Display', Georgia, serif;
  font-size: 32px;
  font-weight: 700;
  color: var(--text, #1c1510);
  margin: 0 0 8px;
  line-height: 1.2;
}

/* Tagline */
.welcome-tagline {
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 16px;
  color: rgba(28, 21, 16, 0.55);
  margin: 0 0 40px;
  line-height: 1.5;
}

/* Showcase cards row */
.showcase-cards {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  justify-content: center;
}

/* Individual card */
.showcase-card {
  width: 140px;
  cursor: pointer;
  text-align: center;
  background: none;
  border: none;
  padding: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  pointer-events: auto;
  transition: transform 0.15s ease;
  /* Ensure touch target ≥ 44px — the photo alone is 80px */
}

.showcase-card:hover {
  transform: scale(1.05);
}

.showcase-card:hover .card-photo {
  border-color: var(--accent, #1b4d8e);
}

.showcase-card:focus-visible {
  outline: 2px solid var(--accent, #1b4d8e);
  outline-offset: 4px;
  border-radius: 4px;
}

/* Photo / initials circle */
.card-photo-wrap {
  width: 80px;
  height: 80px;
  flex-shrink: 0;
}

.card-photo {
  width: 80px;
  height: 80px;
  border-radius: 50%;
  object-fit: cover;
  border: 3px solid var(--surface, #ece8de);
  display: block;
  transition: border-color 0.15s ease;
}

.card-initials {
  display: flex;
  align-items: center;
  justify-content: center;
  background: #bf6b28;
  color: #fff;
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 28px;
  font-weight: 700;
  /* border already set by .card-photo */
}

.card-name {
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 14px;
  font-weight: 600;
  color: var(--text, #1c1510);
  margin-top: 10px;
  line-height: 1.3;
}

.card-desc {
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 12px;
  color: rgba(28, 21, 16, 0.5);
  margin-top: 4px;
  line-height: 1.4;
}

/* Prompt */
.welcome-prompt {
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 13px;
  color: rgba(28, 21, 16, 0.35);
  margin: 28px 0 0;
}

/* Language selector */
.lang-select {
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 12px;
  padding: 4px 8px;
  border: 1px solid var(--border-subtle, rgba(0, 0, 0, 0.12));
  border-radius: 6px;
  background: var(--canvas-bg, #f2ede3);
  color: var(--accent, #1b4d8e);
  cursor: pointer;
  outline: none;
  margin-top: 20px;
  pointer-events: auto;
}

.lang-select:focus {
  border-color: var(--accent, #1b4d8e);
}

/* Skip link */
.skip-btn {
  font-family: 'DM Sans', system-ui, sans-serif;
  font-size: 12px;
  color: rgba(28, 21, 16, 0.35);
  margin-top: 12px;
  cursor: pointer;
  background: none;
  border: none;
  padding: 4px 8px;
  pointer-events: auto;
  transition: color 0.15s;
  min-height: 44px;
  display: flex;
  align-items: center;
}

.skip-btn:hover {
  color: rgba(28, 21, 16, 0.6);
}

/* Mobile */
@media (max-width: 768px) {
  .welcome-title {
    font-size: 24px;
  }

  .welcome-tagline {
    font-size: 14px;
  }

  .showcase-cards {
    gap: 16px;
  }

  .showcase-card {
    width: 110px;
  }

  .card-photo-wrap {
    width: 64px;
    height: 64px;
  }

  .card-photo {
    width: 64px;
    height: 64px;
  }

  .card-initials {
    font-size: 22px;
  }
}
</style>
