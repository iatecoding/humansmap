import { reactive, watch } from 'vue'

const STORAGE_KEY = 'ckc-settings'

const defaults = {
  showEdgeLabels: true,
  showNodeLabels: true,
  showEdges: true,
  focusOnHover: true,
}

function load() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    return saved ? { ...defaults, ...JSON.parse(saved) } : { ...defaults }
  } catch {
    return { ...defaults }
  }
}

const settings = reactive(load())

watch(settings, (val) => {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(val))
  } catch {
    // ignore
  }
}, { deep: true })

export function useSettings() {
  return settings
}
