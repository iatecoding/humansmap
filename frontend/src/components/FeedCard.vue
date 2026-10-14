<template>
  <!-- FeedCard — thin dispatcher to the three card archetypes.
       The action buttons (share/settings) have moved to FeedView's stage-actions
       so they are always positioned at bottom-right of the viewport, outside the
       card frame, never overlapping card content.
       The .feed-slide wrapper still provides the entrance animation and the fixed
       height frame that all three card types fill. -->
  <div class="feed-slide" :class="{ 'feed-slide--in-view': inView }">

    <!-- ── Dispatch to the correct card archetype ── -->
    <RelationshipCard
      v-if="card.type === 'relationship'"
      :card="card"
      :inView="inView"
      @selectPerson="(qid, name) => $emit('selectPerson', qid, name)"
    />
    <DynastyCard
      v-else-if="card.type === 'dynasty'"
      :card="card"
      :inView="inView"
      @selectPerson="(qid, name) => $emit('selectPerson', qid, name)"
    />
    <OrganizationCard
      v-else-if="card.type === 'organization'"
      :card="card"
      :inView="inView"
      @selectPerson="(qid, name) => $emit('selectPerson', qid, name)"
      @selectOrg="(qid, name) => $emit('selectOrg', qid, name)"
    />
    <!-- Graceful fallback for unknown card types -->
    <div v-else class="feed-card-unknown" aria-hidden="true">
      Unknown card type: {{ card.type }}
    </div>
  </div>
</template>

<script setup>
import RelationshipCard from './RelationshipCard.vue'
import DynastyCard from './DynastyCard.vue'
import OrganizationCard from './OrganizationCard.vue'

defineProps({
  card:   { type: Object,  required: true },
  inView: { type: Boolean, default: false },
})

defineEmits(['selectPerson', 'selectOrg', 'editInterests'])
</script>

<style scoped>
/*
  .feed-slide: fills the card frame exactly (width: 100%, height: 100%).
  Entrance animation: the content rises up slightly from below when inView becomes true.
  The slide frame no longer moves — it fills a static .card-inner inside .card-frame.
  The entrance happens on every card swap (FeedView resets inView → false before the
  transition, then triggers inView → true after the new content enters).
*/
.feed-slide {
  position: relative;
  width: 100%;
  height: 100%;
  will-change: transform, opacity;
  transform: translateY(8px);
  opacity: 0;
  transition:
    transform 380ms cubic-bezier(.22, .61, .36, 1),
    opacity   360ms cubic-bezier(.22, .61, .36, 1);
}

/* Card content visible — driven by FeedView's cardInView ref */
.feed-slide--in-view {
  transform: translateY(0);
  opacity: 1;
}

/* ── Fallback unknown card type ──────────────────────────────────────────────── */
.feed-card-unknown {
  background: var(--surface);
  border: 1px dashed var(--border);
  border-radius: 12px;
  padding: 20px;
  font-size: 12px;
  color: var(--text-muted);
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
}

/* ── Reduced motion ──────────────────────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .feed-slide {
    transition: none !important;
    transform: none !important;
    opacity: 1 !important;
  }
}
</style>
