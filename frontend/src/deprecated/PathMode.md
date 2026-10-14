# Path Mode — Deprecated

Removed from App.vue and PersonCard.vue. The underlying graph.loadPath() API call is
still used by game mode (onGameReveal, onGameNodeClick end state), so the backend
/api/path endpoint and useGraph.loadPath() are intentionally kept.

---

## App.vue — mode-tabs button

```html
<button
  :class="{ active: mode === 'path' }"
  @click="setMode('path')"
>{{ t.ui.findPath }}</button>
```

---

## App.vue — top-right search area (path-inputs)

```html
<!-- Path mode: two search fields -->
<div v-if="mode === 'path'" class="path-inputs">
  <SearchBar
    :placeholder="t.ui.from"
    @select="onPathFromSelect"
    :selectedName="pathFrom?.name"
  />
  <span class="path-arrow">&#8594;</span>
  <SearchBar
    :placeholder="t.ui.to"
    @select="onPathToSelect"
    :selectedName="pathTo?.name"
  />
  <button
    class="path-go"
    :disabled="!pathFrom || !pathTo"
    @click="findPath"
  >{{ t.ui.search }}</button>
</div>
```

---

## App.vue — path result banner (below header)

```html
<!-- Path result banner -->
<div v-if="pathResult" class="path-banner">
  <template v-if="pathResult.found">
    <span class="path-info">
      {{ t.ui.pathFound }} <strong>{{ pathResult.length }}</strong>
      {{ pathResult.length === 1 ? t.ui.connection : t.ui.connections }}
      {{ t.ui.between }} <strong>{{ pathFrom?.name }}</strong> {{ t.ui.and }} <strong>{{ pathTo?.name }}</strong>
    </span>
  </template>
  <template v-else>
    <span class="path-info path-not-found">
      {{ t.ui.noPathFound }} <strong>{{ pathFrom?.name }}</strong> {{ t.ui.and }} <strong>{{ pathTo?.name }}</strong>
    </span>
  </template>
  <button class="path-close" @click="pathResult = null" aria-label="Chiudi">&times;</button>
</div>
```

---

## App.vue — empty-state guidance block

```html
<!-- Path mode guidance -->
<template v-else-if="mode === 'path'">
  <p class="empty-lead">{{ t.emptyState.pathLead }}</p>
  <div class="path-steps">
    <div class="path-step">
      <span class="step-num">1</span>
      <span class="step-text">{{ t.emptyState.pathStep1 }}</span>
    </div>
    <div class="step-divider" aria-hidden="true">&#8594;</div>
    <div class="path-step">
      <span class="step-num">2</span>
      <span class="step-text">{{ t.emptyState.pathStep2 }}</span>
    </div>
  </div>
  <p class="empty-sub">{{ t.emptyState.pathSub }}</p>
</template>
```

---

## App.vue — PersonCard binding

```html
@findPath="startPathFrom"
```

---

## App.vue — script refs

```js
const pathFrom = ref(null)
const pathTo = ref(null)
const pathResult = ref(null)
```

---

## App.vue — script functions

```js
function onPathFromSelect(person) {
  pathFrom.value = { qid: person.id || person._key, name: person.name_it || person.name_en }
}

function onPathToSelect(person) {
  pathTo.value = { qid: person.id || person._key, name: person.name_it || person.name_en }
}

function startPathFrom(person) {
  mode.value = 'path'
  pathFrom.value = { qid: person.qid, name: person.name_it || person.name_en }
  pathTo.value = null
  pathResult.value = null
}

async function findPath() {
  if (!pathFrom.value || !pathTo.value) return
  pathResult.value = null
  const result = await graph.loadPath(pathFrom.value.qid, pathTo.value.qid)
  pathResult.value = result
  graph.applyVisualSettings(vizSettings)
  graphLoaded.value = true
}
```

---

## App.vue — setMode path branch

```js
if (m === 'path') {
  pathFrom.value = null
  pathTo.value = null
}
```

---

## App.vue — goHome path cleanup

```js
pathFrom.value = null
pathTo.value = null
pathResult.value = null
```

---

## App.vue — CSS

```css
.path-inputs {
  display: flex;
  align-items: center;
  gap: 8px;
}
.path-inputs :deep(.search-bar) {
  min-width: 180px;
  max-width: 240px;
}
.path-inputs :deep(.search-bar input) {
  min-width: 0;
}
.path-arrow {
  color: rgba(28, 21, 16, 0.35);
  font-size: 18px;
  flex-shrink: 0;
}
.path-go {
  padding: 11px 18px;
  min-height: 44px;
  background: var(--accent);
  border: none;
  border-radius: 8px;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.2s;
  white-space: nowrap;
  flex-shrink: 0;
}
.path-go:hover:not(:disabled) {
  background: var(--accent-hover);
}
.path-go:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.path-banner {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 8px 20px;
  background: rgba(27, 77, 142, 0.08);
  border-bottom: 1px solid rgba(27, 77, 142, 0.2);
  font-size: 14px;
}
.path-info { color: rgba(28, 21, 16, 0.75); }
.path-info strong { color: var(--text); }
.path-not-found { color: rgba(176, 48, 48, 0.8); }
.path-close {
  background: none;
  border: none;
  color: rgba(28, 21, 16, 0.45);
  font-size: 18px;
  cursor: pointer;
  min-width: 44px;
  min-height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border-radius: 4px;
}
.path-steps {
  display: flex;
  align-items: center;
  gap: 16px;
  margin: 0 0 16px;
  flex-wrap: wrap;
  justify-content: center;
}
.path-step {
  display: flex;
  align-items: center;
  gap: 10px;
  background: rgba(255, 255, 255, 0.55);
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  padding: 12px 18px;
  max-width: 220px;
}
.step-num {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: rgba(27, 77, 142, 0.15);
  color: var(--accent);
  font-size: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.step-text {
  font-size: 13px;
  color: rgba(28, 21, 16, 0.5);
  line-height: 1.4;
  text-align: left;
}
.step-text em {
  font-style: normal;
  color: rgba(28, 21, 16, 0.75);
}
.step-divider {
  font-size: 20px;
  color: rgba(28, 21, 16, 0.2);
  flex-shrink: 0;
}
```

---

## PersonCard.vue — findPath action button

```html
<button class="action-btn path-btn" @click="$emit('findPath', person)">
  {{ t.graph.findPath }}
</button>
```

And from defineEmits:
```js
defineEmits(['close', 'expand', 'findPath', 'toggleOrgMembers'])
// becomes:
defineEmits(['close', 'expand', 'toggleOrgMembers'])
```

---

## i18n keys (can be cleaned up later)

- `t.ui.findPath`
- `t.ui.from`
- `t.ui.to`
- `t.ui.search`
- `t.ui.pathFound`
- `t.ui.connection`
- `t.ui.connections`
- `t.ui.between`
- `t.ui.and`
- `t.ui.noPathFound`
- `t.emptyState.pathLead`
- `t.emptyState.pathStep1`
- `t.emptyState.pathStep2`
- `t.emptyState.pathSub`
