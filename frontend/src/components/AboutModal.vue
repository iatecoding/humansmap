<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="$emit('close')" role="dialog" aria-modal="true" :aria-label="t.about.title">
      <div class="modal-card">
        <button class="modal-close" @click="$emit('close')" :aria-label="t.about.close">&#10005;</button>

        <h2 class="modal-title">{{ t.about.title }}</h2>

        <p class="modal-description">{{ t.about.description }}</p>

        <div class="modal-sources">
          <div class="source-row">
            <span class="source-label">{{ t.footer.data }}</span>
            <a href="https://www.wikidata.org" target="_blank" rel="noopener">Wikidata</a>
            <span class="source-license">CC0</span>
          </div>
          <div class="source-row">
            <span class="source-label">{{ t.footer.images }}</span>
            <a href="https://commons.wikimedia.org" target="_blank" rel="noopener">Wikimedia Commons</a>
            <span class="source-license">CC BY-SA / CC0</span>
          </div>
        </div>

        <div class="modal-credits">
          <p class="credits-line">{{ t.about.madeBy }} <strong>Francesco Viola</strong></p>
          <p class="credits-line credits-line--sub">{{ t.about.withClaude }}</p>
        </div>

        <div class="modal-report">
          <h3 class="report-title">{{ t.about.reportTitle }}</h3>
          <p class="report-desc">{{ t.about.reportDesc }}</p>

          <div v-if="reportState === 'success'" class="report-success">
            {{ t.about.reportSuccess }}
          </div>
          <template v-else>
            <textarea
              class="report-textarea"
              v-model="reportMessage"
              :placeholder="t.about.reportPlaceholder"
              rows="3"
              maxlength="1000"
              :disabled="reportState === 'sending'"
            ></textarea>
            <div class="report-footer">
              <span v-if="reportState === 'error'" class="report-error">
                {{ t.about.reportError }}
              </span>
              <button
                class="report-btn"
                :disabled="!reportMessage.trim() || reportState === 'sending'"
                @click="sendReport"
              >
                {{ reportState === 'sending' ? t.about.reportSending : t.about.reportSend }}
              </button>
            </div>
          </template>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { ref } from 'vue'
import { useLocale } from '../composables/useLocale.js'

defineEmits(['close'])

const { t } = useLocale()

const reportMessage = ref('')
const reportState = ref('idle') // idle | sending | success | error

async function sendReport() {
  if (!reportMessage.value.trim()) return
  reportState.value = 'sending'
  try {
    const res = await fetch('/api/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: reportMessage.value.trim() }),
    })
    if (!res.ok) throw new Error('server error')
    reportState.value = 'success'
  } catch {
    reportState.value = 'error'
  }
}
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(28, 21, 16, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 1rem;
  backdrop-filter: blur(2px);
}

.modal-card {
  background: var(--bg-panel-solid);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 2rem;
  max-width: 480px;
  width: 100%;
  position: relative;
  box-shadow: 0 8px 32px rgba(28, 21, 16, 0.18), 0 2px 8px rgba(28, 21, 16, 0.08);
}

.modal-close {
  position: absolute;
  top: 1rem;
  right: 1rem;
  background: none;
  border: none;
  color: rgba(28, 21, 16, 0.45);
  font-size: 1rem;
  cursor: pointer;
  padding: 0.25rem 0.5rem;
  border-radius: 4px;
  line-height: 1;
  transition: color 0.15s;
}
.modal-close:hover { color: var(--text); }

.modal-title {
  font-family: var(--font-display);
  font-size: 1.4rem;
  font-weight: 700;
  margin: 0 0 0.875rem;
  color: var(--text);
}

.modal-description {
  font-size: 0.9rem;
  line-height: 1.65;
  color: rgba(28, 21, 16, 0.7);
  margin: 0 0 1.25rem;
}

/* Sources block */
.modal-sources {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  padding: 0.75rem 0;
  border-top: 1px solid var(--border-faint);
  border-bottom: 1px solid var(--border-faint);
  margin-bottom: 1.5rem;
  font-size: 0.85rem;
}

.source-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.source-label {
  color: rgba(28, 21, 16, 0.55);
}

.source-row a {
  color: var(--accent);
  text-decoration: none;
}
.source-row a:hover { text-decoration: underline; }

.source-license {
  color: rgba(28, 21, 16, 0.42);
  font-size: 0.78rem;
}

/* Credits */
.modal-credits {
  margin-bottom: 1.5rem;
}

.credits-line {
  margin: 0;
  font-size: 0.9rem;
  color: rgba(28, 21, 16, 0.55);
}

.credits-line strong {
  font-weight: 600;
  color: rgba(28, 21, 16, 0.75);
}

.credits-line--sub {
  font-size: 0.82rem;
  color: rgba(28, 21, 16, 0.38);
}

/* Report section */
.report-title {
  font-size: 0.8rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: rgba(28, 21, 16, 0.5);
  margin: 0 0 0.35rem;
}

.report-desc {
  font-size: 0.875rem;
  color: rgba(28, 21, 16, 0.7);
  margin: 0 0 0.75rem;
  line-height: 1.5;
}

.report-textarea {
  width: 100%;
  box-sizing: border-box;
  background: var(--canvas-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text);
  font-size: 0.875rem;
  font-family: var(--font-body);
  padding: 0.6rem 0.75rem;
  resize: vertical;
  outline: none;
  transition: border-color 0.15s;
  line-height: 1.5;
}
.report-textarea::placeholder { color: rgba(28, 21, 16, 0.35); }
.report-textarea:focus { border-color: rgba(27, 77, 142, 0.5); }
.report-textarea:disabled { opacity: 0.5; cursor: not-allowed; }

.report-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.75rem;
  margin-top: 0.6rem;
}

/* Solid accent button — same as .play-again-btn in GameEndModal */
.report-btn {
  padding: 0.5rem 1.1rem;
  background: var(--accent);
  border: none;
  border-radius: 8px;
  color: #fff;
  font-size: 0.875rem;
  font-weight: 600;
  font-family: var(--font-body);
  cursor: pointer;
  transition: background 0.2s;
  white-space: nowrap;
}
.report-btn:hover:not(:disabled) { background: var(--accent-hover); }
.report-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.report-error {
  font-size: 0.82rem;
  color: var(--color-wrong);
}

/* Success state — accent blue, no background */
.report-success {
  font-size: 0.875rem;
  color: var(--accent);
  padding: 0.25rem 0;
}
</style>
