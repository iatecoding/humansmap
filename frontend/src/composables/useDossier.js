import { ref } from 'vue'

// Module-level singleton — one dossier open at a time across the whole app
const open = ref(false)
const qid = ref(null)
const entityType = ref('person') // 'person' | 'organization' | 'event'
const nodeData = ref(null) // the compact Cytoscape node data snapshot

export function useDossier() {
  function openDossier(nodeObj) {
    // nodeObj = the Cytoscape node data object (same shape as graph.selectedNode.value)
    qid.value = nodeObj?.qid || nodeObj?._key || null
    entityType.value = nodeObj?.entityType || 'person'
    nodeData.value = nodeObj
    open.value = true
  }

  function closeDossier() {
    open.value = false
    qid.value = null
    entityType.value = 'person'
    nodeData.value = null
  }

  return { open, qid, entityType, nodeData, openDossier, closeDossier }
}
