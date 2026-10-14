<template>
  <div
    class="dossier-panel"
    role="dialog"
    aria-modal="false"
    :aria-label="t.dossier.title"
    @keydown.esc="$emit('close')"
    tabindex="-1"
    ref="panelEl"
  >
    <!-- Header -->
    <div class="dossier-header">
      <div class="dossier-identity">
        <div
          class="dossier-avatar"
          :class="[avatarClass, { 'avatar-clickable': node?.hasImage }]"
          :style="avatarStyle"
          @click="node?.hasImage && (showLightbox = true)"
        >
          <span v-if="!node?.hasImage">{{ initials }}</span>
        </div>
        <div class="dossier-title-block">
          <h2 class="dossier-name">{{ prefName(node) }}</h2>
          <p v-if="node?.description_it || node?.description_en || node?.description" class="dossier-desc">
            {{ prefDesc(node) }}
          </p>
        </div>
      </div>
      <button class="dossier-close" @click="$emit('close')" :aria-label="t.ui.close">&times;</button>
    </div>

    <!-- Anchor strip (sticky, only when there's more than Overview) -->
    <nav
      v-if="visibleSections.length > 1"
      class="dossier-anchors"
      :aria-label="t.dossier.title"
    >
      <button
        v-for="sec in visibleSections"
        :key="sec.id"
        class="anchor-chip"
        :class="{ active: activeAnchor === sec.id }"
        @click="scrollToSection(sec.id)"
        @keydown.enter.prevent="scrollToSection(sec.id)"
        @keydown.space.prevent="scrollToSection(sec.id)"
      >{{ sec.label }}</button>
    </nav>

    <!-- Single scrollable body -->
    <div class="dossier-body" ref="bodyEl">

      <!-- ── OVERVIEW ─────────────────────────────────────────────── -->
      <section class="dossier-section" id="section-overview">
        <h3 v-if="visibleSections.length > 1" class="section-heading">{{ t.dossier.tabs.overview }}</h3>

        <!-- Person overview -->
        <template v-if="isPerson">
          <div class="field-group">
            <div v-if="node?.birth_date || node?.birth_place" class="field-row">
              <span class="field-label">{{ t.person.born }}</span>
              <span class="field-value">
                <span v-if="node?.birth_date">{{ formatDate(node.birth_date) }}</span>
                <span v-if="node?.birth_place" class="field-sub">{{ node.birth_place }}</span>
              </span>
            </div>
            <div v-if="node?.death_date || node?.death_place || node?.cause_of_death || detail?.cause_of_death" class="field-row">
              <span class="field-label">{{ t.person.died }}</span>
              <span class="field-value">
                <span v-if="node?.death_date">{{ formatDate(node.death_date) }}</span>
                <span v-if="node?.death_place" class="field-sub">{{ node.death_place }}</span>
                <span v-if="detail?.cause_of_death || node?.cause_of_death" class="field-cause">
                  {{ t.person.cause }} {{ detail?.cause_of_death || node?.cause_of_death }}
                </span>
              </span>
            </div>
            <div v-if="node?.occupations?.length" class="field-row">
              <span class="field-label">{{ t.person.occupation }}</span>
              <span class="field-value">{{ node.occupations.map(translateOccupation).join(', ') }}</span>
            </div>
          </div>

          <!-- Notable works -->
          <div v-if="detail?.notable_works?.length" class="dossier-subsection">
            <h3 class="section-heading">{{ t.person.notableWorks }}</h3>
            <ul class="works-list">
              <li v-for="work in detail.notable_works" :key="work" class="works-item">
                <span class="works-dot"></span>{{ work }}
              </li>
            </ul>
          </div>

          <div v-if="!detail && detailLoading" class="loading-inline">
            <span class="spinner-sm"></span>
          </div>
        </template>

        <!-- Org overview -->
        <template v-else-if="isOrg">
          <div class="field-group">
            <div v-if="node?.type" class="field-row">
              <span class="field-label">{{ t.organization.type }}</span>
              <span class="field-value">{{ translateOrgType(node.type) }}</span>
            </div>
            <div v-if="node?.founded" class="field-row">
              <span class="field-label">{{ t.organization.founded }}</span>
              <span class="field-value">{{ formatYear(node.founded) }}</span>
            </div>
            <div v-if="node?.dissolved" class="field-row">
              <span class="field-label">{{ t.organization.dissolved }}</span>
              <span class="field-value">{{ formatYear(node.dissolved) }}</span>
            </div>
            <div v-if="detail?.country" class="field-row">
              <span class="field-label">{{ t.organization.country }}</span>
              <span class="field-value">{{ detail.country }}</span>
            </div>
            <div v-if="detail?.headquarters" class="field-row">
              <span class="field-label">{{ t.organization.headquarters }}</span>
              <span class="field-value">{{ detail.headquarters }}</span>
            </div>
            <div v-if="detail?.employees_count" class="field-row">
              <span class="field-label">{{ t.organization.employees }}</span>
              <span class="field-value">{{ detail.employees_count.toLocaleString() }}</span>
            </div>
            <div v-if="detail?.website" class="field-row">
              <span class="field-label">{{ t.organization.website }}</span>
              <span class="field-value">
                <a :href="detail.website" target="_blank" rel="noopener" class="ext-link">{{ detail.website.replace(/^https?:\/\//, '') }}</a>
              </span>
            </div>
          </div>
        </template>

        <!-- Event overview -->
        <template v-else-if="isEvent">
          <div class="field-group">
            <div v-if="node?.type" class="field-row">
              <span class="field-label">{{ t.event.type }}</span>
              <span class="field-value">{{ translateEventType(node.type) }}</span>
            </div>
            <div v-if="node?.date" class="field-row">
              <span class="field-label">{{ t.event.date }}</span>
              <span class="field-value">{{ formatDate(node.date) }}</span>
            </div>
            <div v-if="detail?.location" class="field-row">
              <span class="field-label">{{ t.event.location }}</span>
              <span class="field-value">{{ detail.location }}</span>
            </div>
            <div v-if="detail?.country" class="field-row">
              <span class="field-label">{{ t.organization.country }}</span>
              <span class="field-value">{{ detail.country }}</span>
            </div>
            <div v-if="detail?.deaths != null" class="field-row">
              <span class="field-label">{{ t.event.deaths }}</span>
              <span class="field-value death-count">{{ detail.deaths.toLocaleString() }}</span>
            </div>
          </div>
        </template>
      </section>

      <!-- ── CAREER (person only) ────────────────────────────────── -->
      <section
        v-if="hasCareerData"
        class="dossier-section"
        id="section-career"
      >
        <h3 class="section-heading">{{ t.dossier.tabs.career }}</h3>
        <div v-if="careerLoading" class="loading-inline">
          <span class="spinner-sm"></span>
        </div>
        <div v-else class="career-timeline">
          <div
            v-for="(item, idx) in careerItems"
            :key="idx"
            class="career-entry"
            :class="{ 'career-entry--current': !item.end_date }"
          >
            <div class="career-line">
              <div class="career-dot"></div>
              <div v-if="idx < careerItems.length - 1" class="career-track"></div>
            </div>
            <div class="career-content">
              <div class="career-org">{{ item.org_name }}</div>
              <div class="career-role" v-if="item.role">{{ item.role }}</div>
              <div class="career-period">
                <span v-if="item.start_date">{{ formatYear(item.start_date) }}</span>
                <span v-if="item.start_date && item.end_date" class="period-sep">–</span>
                <span v-if="item.end_date">{{ formatYear(item.end_date) }}</span>
                <span v-if="item.start_date && !item.end_date" class="career-current-badge">{{ t.dossier.present }}</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- ── NETWORK (person only) ──────────────────────────────── -->
      <section
        v-if="hasNetworkData"
        class="dossier-section"
        id="section-network"
      >
        <h3 class="section-heading">{{ t.dossier.tabs.network }}</h3>
        <div v-if="networkLoading" class="loading-inline">
          <span class="spinner-sm"></span>
        </div>
        <div v-else class="network-summary">
          <div
            v-for="group in networkSummary"
            :key="group.type"
            class="network-group"
          >
            <div class="network-group-header">
              <span class="network-type">{{ translateEdgeType(group.type) }}</span>
              <span class="network-count">{{ group.count }}</span>
            </div>
            <ul v-if="group.top.length" class="network-names">
              <li v-for="name in group.top" :key="name">{{ name }}</li>
            </ul>
          </div>
          <button class="expand-network-btn" @click="$emit('enter', dossier.qid.value)">
            {{ t.graph.enter }} &#8594;
          </button>
        </div>
      </section>

      <!-- ── AWARDS (person only) ───────────────────────────────── -->
      <section
        v-if="hasAwardsData"
        class="dossier-section"
        id="section-awards"
      >
        <h3 class="section-heading">{{ t.dossier.tabs.awards }}</h3>
        <div v-if="detailLoading" class="loading-inline">
          <span class="spinner-sm"></span>
        </div>
        <div v-else class="awards-section">
          <div class="awards-chips">
            <span
              v-for="award in detail.awards_top5"
              :key="award"
              class="award-chip"
            >{{ award }}</span>
          </div>
        </div>
      </section>

      <!-- ── PEOPLE (org only) ──────────────────────────────────── -->
      <section
        v-if="hasOrgPeopleData"
        class="dossier-section"
        id="section-people"
      >
        <h3 class="section-heading">{{ t.dossier.tabs.people }}</h3>
        <div v-if="orgPeopleLoading" class="loading-inline">
          <span class="spinner-sm"></span>
        </div>
        <ul v-else class="people-list">
          <li v-for="p in orgPeople" :key="p._key" class="people-item">
            <span class="people-name">{{ p.name_en || p.name_it }}</span>
            <span v-if="p.role" class="people-role">{{ p.role }}</span>
          </li>
        </ul>
      </section>

      <!-- ── STRUCTURE (org only) ───────────────────────────────── -->
      <section
        v-if="hasStructureData"
        class="dossier-section"
        id="section-structure"
      >
        <h3 class="section-heading">{{ t.dossier.tabs.structure }}</h3>
        <div v-if="structureLoading" class="loading-inline">
          <span class="spinner-sm"></span>
        </div>
        <div v-else class="structure-tree">
          <!-- Parent chain -->
          <div v-if="structureTree.parents.length" class="tree-section">
            <div class="tree-section-label">&#8593; {{ t.dossier.parentChain }}</div>
            <div class="tree-chain">
              <div
                v-for="(parent, i) in structureTree.parents"
                :key="parent._key"
                class="tree-node tree-node--parent"
                :style="{ paddingLeft: (i * 16) + 'px' }"
              >
                <span class="tree-connector" v-if="i > 0">&#9492;</span>
                <span class="tree-name">{{ parent.name_en || parent.name_it }}</span>
              </div>
            </div>
          </div>

          <!-- Current node -->
          <div class="tree-current">
            <span class="tree-bullet">&#11044;</span>
            <span class="tree-name tree-name--current">{{ prefName(node) }}</span>
          </div>

          <!-- Subsidiaries -->
          <div v-if="structureTree.subsidiaries.length" class="tree-section">
            <div class="tree-section-label">&#8595; {{ t.dossier.subsidiaries }}</div>
            <div class="tree-chain">
              <div
                v-for="(sub, i) in structureTree.subsidiaries"
                :key="sub._key"
                class="tree-node tree-node--sub"
                :style="{ paddingLeft: ((i % 3 + 1) * 12) + 'px' }"
              >
                <span class="tree-connector">&#9492;</span>
                <span class="tree-name">{{ sub.name_en || sub.name_it }}</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- ── MEMBERS (org only) ─────────────────────────────────── -->
      <section
        v-if="hasOrgMembersData"
        class="dossier-section"
        id="section-members"
      >
        <h3 class="section-heading">{{ t.dossier.tabs.members }}</h3>
        <div v-if="orgPeopleLoading" class="loading-inline">
          <span class="spinner-sm"></span>
        </div>
        <ul v-else class="people-list">
          <li v-for="p in orgMembers" :key="p._key" class="people-item">
            <span class="people-name">{{ p.name_en || p.name_it }}</span>
          </li>
        </ul>
      </section>

      <!-- ── FILINGS (org + person) ────────────────────────────────── -->
      <section
        v-if="hasFilingsData"
        class="dossier-section"
        id="section-filings"
      >
        <h3 class="section-heading">{{ t.dossier.tabs.filings }}</h3>

        <!-- ORG variant -->
        <template v-if="isOrg">
          <div v-if="filingsLoading" class="loading-inline">
            <span class="spinner-sm"></span>
          </div>
          <div v-else-if="!filingsData" class="empty-tab">
            <p>{{ t.dossier.filings.emptyOrg }}</p>
          </div>
          <div v-else class="filings-pane">

            <!-- Metadata block -->
            <div v-if="Object.keys(filingsData.sec_metadata).length" class="filings-section">
              <div v-if="filingsData.sec_metadata.entity_type" class="filings-registered-as">
                {{ t.dossier.filings.registeredAs }}: {{ filingsData.sec_metadata.entity_type }}
              </div>
              <dl class="filings-meta-dl">
                <template v-if="filingsData.sec_metadata.cik">
                  <dt>{{ t.dossier.filings.cik }}</dt>
                  <dd>{{ filingsData.sec_metadata.cik }}</dd>
                </template>
                <template v-if="filingsData.sec_metadata.sic_description">
                  <dt>SIC</dt>
                  <dd>{{ filingsData.sec_metadata.sic_description }}</dd>
                </template>
                <template v-if="filingsData.sec_metadata.state_of_incorporation">
                  <dt>{{ t.dossier.filings.state }}</dt>
                  <dd>{{ filingsData.sec_metadata.state_of_incorporation }}</dd>
                </template>
                <template v-if="filingsData.sec_metadata.fiscal_year_end">
                  <dt>{{ t.dossier.filings.fiscalYear }}</dt>
                  <dd>{{ filingsData.sec_metadata.fiscal_year_end }}</dd>
                </template>
                <template v-if="filingsData.sec_metadata.exchanges?.length">
                  <dt>{{ t.dossier.filings.exchange }}</dt>
                  <dd>{{ filingsData.sec_metadata.exchanges.join(', ') }}</dd>
                </template>
                <template v-if="filingsData.sec_metadata.tickers?.length">
                  <dt>{{ t.dossier.filings.ticker }}</dt>
                  <dd>{{ filingsData.sec_metadata.tickers.join(', ') }}</dd>
                </template>
              </dl>
            </div>

            <!-- Insiders block -->
            <div class="filings-section">
              <div class="filings-section-header">
                <span class="filings-section-title">{{ t.dossier.filings.recentInsiderActivity }}</span>
                <span class="filings-chip">
                  {{ filingsData.insiders.totals.matched_persons }} {{ t.dossier.filings.matched }}
                  · {{ filingsData.insiders.totals.unmatched }} {{ t.dossier.filings.unmatched }}
                </span>
              </div>

              <!-- Matched persons -->
              <ul v-if="filingsData.insiders.matched.length" class="filings-insider-list">
                <li
                  v-for="person in filingsData.insiders.matched"
                  :key="person.person_qid"
                  class="filings-insider-row"
                >
                  <div class="filings-insider-header" @click="toggleInsider(person.person_qid)">
                    <div class="filings-insider-avatar" :style="person.has_image ? {backgroundImage: `url(${api.imageUrl(person.person_qid)})`} : {}">
                      <span v-if="!person.has_image" class="filings-avatar-initials">{{ (person.name || '?').split(' ').map(w => w[0]).join('').slice(0,2).toUpperCase() }}</span>
                    </div>
                    <div class="filings-insider-info">
                      <span class="filings-insider-name">{{ person.name }}</span>
                      <span v-if="person.roles.length" class="filings-insider-role">{{ person.roles[0].role || person.roles[0].type }}</span>
                    </div>
                    <div class="filings-insider-summary">
                      <span v-if="person.transaction_summary.count">
                        {{ person.transaction_summary.count }} {{ t.dossier.filings.txns }}
                        <template v-if="person.transaction_summary.last_date">
                          · {{ person.transaction_summary.last_date.slice(0, 10) }}
                        </template>
                      </span>
                    </div>
                    <span class="filings-expand-icon">{{ expandedInsiders.has(person.person_qid) ? '▲' : '▼' }}</span>
                  </div>

                  <!-- Expanded transactions -->
                  <div v-if="expandedInsiders.has(person.person_qid) && person.recent_transactions.length" class="filings-txn-list">
                    <div
                      v-for="(txn, i) in person.recent_transactions"
                      :key="i"
                      class="filings-txn-row"
                    >
                      <span class="filings-txn-date">{{ txn.transaction_date?.slice(0, 10) || '—' }}</span>
                      <span class="filings-txn-dir" :class="txn.direction === 'A' ? 'dir-buy' : 'dir-sell'">
                        {{ txn.direction === 'A' ? t.dossier.filings.directionBuy : t.dossier.filings.directionSell }}
                      </span>
                      <span class="filings-txn-shares">{{ txn.shares != null ? txn.shares.toLocaleString() : '—' }}</span>
                      <span class="filings-txn-price">{{ txn.price_usd != null ? '$' + txn.price_usd.toFixed(2) : '—' }}</span>
                      <span class="filings-txn-total" v-if="txn.shares != null && txn.price_usd != null">${{ Math.round(txn.shares * txn.price_usd).toLocaleString() }}</span>
                      <a v-if="txn.filing_url" :href="txn.filing_url" target="_blank" rel="noopener" class="filings-ext-link">↗</a>
                    </div>
                  </div>
                </li>
              </ul>
              <p v-else class="filings-empty-section">—</p>

              <!-- Unmatched reporters -->
              <details v-if="filingsData.insiders.unmatched.length" class="filings-unmatched">
                <summary class="filings-unmatched-summary">
                  {{ t.dossier.filings.unmatchedDirectors }} ({{ filingsData.insiders.unmatched.length }})
                </summary>
                <ul class="filings-unmatched-list">
                  <li v-for="u in filingsData.insiders.unmatched" :key="u.accession" class="filings-unmatched-item">
                    <span class="filings-unmatched-name">{{ u.reporter_name }}</span>
                    <span class="filings-unmatched-meta">{{ u.filing_date?.slice(0,10) }} · {{ u.form_type }}</span>
                    <a v-if="u.filing_url" :href="u.filing_url" target="_blank" rel="noopener" class="filings-ext-link">↗</a>
                  </li>
                </ul>
              </details>
            </div>

            <!-- Directors & Officers block (governance roles, transaction-independent) -->
            <div v-if="filingsData.directors && (filingsData.directors.matched.length || filingsData.directors.unmatched.length)" class="filings-section">
              <div class="filings-section-header">
                <span class="filings-section-title">{{ t.dossier.filings.directorsOfficers }}</span>
                <span class="filings-chip">
                  {{ filingsData.directors.totals.matched }} · {{ filingsData.directors.totals.unmatched }} {{ t.dossier.filings.unmatchedDirectors.toLowerCase() }}
                </span>
              </div>

              <ul v-if="filingsData.directors.matched.length" class="filings-insider-list">
                <li
                  v-for="person in filingsData.directors.matched"
                  :key="person.person_qid"
                  class="filings-insider-row"
                >
                  <div
                    class="filings-insider-header"
                    @click="dossier.openDossier({ qid: person.person_qid, entityType: 'person' })"
                    role="button"
                    tabindex="0"
                  >
                    <div class="filings-insider-avatar" :style="person.has_image ? {backgroundImage: `url(${api.imageUrl(person.person_qid)})`} : {}">
                      <span v-if="!person.has_image" class="filings-avatar-initials">{{ (person.name || '?').split(' ').map(w => w[0]).join('').slice(0,2).toUpperCase() }}</span>
                    </div>
                    <div class="filings-insider-info">
                      <span class="filings-insider-name">{{ person.name }}</span>
                      <div class="filings-director-roles">
                        <span
                          v-for="(r, i) in person.roles"
                          :key="i"
                          class="filings-director-role-chip"
                          :class="r.type === 'officer_of' ? 'role-officer' : 'role-director'"
                        >
                          <span class="filings-role-label">
                            {{ r.role || (r.type === 'officer_of' ? t.dossier.filings.roleOfficer : t.dossier.filings.roleDirector) }}
                          </span>
                          <span v-if="r.start_date" class="filings-role-dates">
                            {{ r.start_date.slice(0, 4) }}<template v-if="r.end_date">–{{ r.end_date.slice(0, 4) }}</template>
                          </span>
                          <span
                            v-if="r.source === 'sec_def14a'"
                            class="filings-source-badge badge-def14a"
                            :title="t.dossier.filings.sourceDef14a"
                          >DEF 14A</span>
                          <span
                            v-else
                            class="filings-source-badge badge-form3"
                            :title="t.dossier.filings.sourceForm3"
                          >Form 3</span>
                        </span>
                      </div>
                    </div>
                  </div>
                </li>
              </ul>

              <!-- Unmatched directors (plain name chips, no links) -->
              <details v-if="filingsData.directors.unmatched.length" class="filings-unmatched">
                <summary class="filings-unmatched-summary">
                  {{ t.dossier.filings.unmatchedDirectors }} ({{ filingsData.directors.unmatched.length }})
                </summary>
                <div class="filings-unmatched-chips">
                  <span
                    v-for="(name, i) in filingsData.directors.unmatched"
                    :key="i"
                    class="filings-unmatched-chip"
                  >{{ name }}</span>
                </div>
              </details>
            </div>

            <!-- Institutional Holders block (13F holds_shares_of) -->
            <div v-if="filingsData.institutional_holders?.length" class="filings-section">
              <div class="filings-section-header">
                <span class="filings-section-title">{{ t.dossier.filings.institutionalHolders }}</span>
                <span class="filings-chip">{{ filingsData.institutional_holders.length }}</span>
              </div>
              <ul class="filings-insider-list">
                <li
                  v-for="holder in filingsData.institutional_holders"
                  :key="holder.manager_qid"
                  class="filings-insider-row"
                >
                  <div
                    class="filings-insider-header filings-role-row"
                    @click="dossier.openDossier({ qid: holder.manager_qid, entityType: 'organization' })"
                    role="button"
                    tabindex="0"
                    style="cursor: pointer"
                  >
                    <div
                      class="filings-insider-avatar"
                      style="border-radius: 6px"
                      :style="holder.manager_has_image ? {backgroundImage: `url(${api.imageUrl(holder.manager_qid)})`} : {}"
                    >
                      <span v-if="!holder.manager_has_image" class="filings-avatar-initials">{{ (holder.manager_name || '?').slice(0,2).toUpperCase() }}</span>
                    </div>
                    <div class="filings-insider-info">
                      <span class="filings-insider-name filings-org-link">{{ holder.manager_name }}</span>
                      <span class="filings-insider-role">
                        <template v-if="holder.value_usd">${{ (holder.value_usd / 1_000_000).toFixed(1) }}M</template>
                        <template v-if="holder.shares"> · {{ holder.shares.toLocaleString() }} shares</template>
                      </span>
                    </div>
                    <div class="filings-insider-summary">
                      <span v-if="holder.period_of_report">{{ holder.period_of_report.slice(0,7) }}</span>
                    </div>
                  </div>
                </li>
              </ul>
            </div>

            <!-- Material Events block -->
            <div v-if="filingsData.material_events.page.total > 0" class="filings-section">
              <div class="filings-section-header">
                <span class="filings-section-title">{{ t.dossier.filings.materialEvents }}</span>
                <span class="filings-chip">{{ filingsData.material_events.page.total }}</span>
              </div>

              <!-- Subtype chips -->
              <div class="filings-subtype-chips">
                <span
                  v-for="(count, subtype) in filingsData.material_events.by_subtype"
                  :key="subtype"
                  class="filings-subtype-chip"
                >
                  {{ subtype.replace(/_/g, ' ') }} ({{ count }})
                </span>
              </div>

              <!-- Event items -->
              <ul class="filings-events-list">
                <li
                  v-for="ev in filingsData.material_events.items"
                  :key="ev.key"
                  class="filings-event-row"
                >
                  <span class="filings-event-date">{{ ev.filing_date?.slice(0,10) || '—' }}</span>
                  <span class="filings-event-subtype">{{ ev.subtype?.replace(/_/g, ' ') || '—' }}</span>
                  <span v-if="ev.items?.length" class="filings-event-items">{{ ev.items.join(' · ') }}</span>
                  <span v-if="ev.is_amendment" class="filings-event-amend">A</span>
                  <a v-if="ev.filing_url" :href="ev.filing_url" target="_blank" rel="noopener" class="filings-ext-link">↗</a>
                </li>
              </ul>
            </div>

            <!-- Former names -->
            <details v-if="filingsData.sec_metadata.former_names?.length" class="filings-former-names">
              <summary class="filings-unmatched-summary">{{ t.dossier.filings.formerNames }} ({{ filingsData.sec_metadata.former_names.length }})</summary>
              <ul class="filings-unmatched-list">
                <li v-for="fn in filingsData.sec_metadata.former_names" :key="fn.name || fn" class="filings-unmatched-item">
                  <span class="filings-unmatched-name">{{ fn.name || fn }}</span>
                  <span v-if="fn.date" class="filings-unmatched-meta">{{ t.dossier.filings.formerNameUntil }} {{ fn.date?.slice(0,10) }}</span>
                </li>
              </ul>
            </details>

            <p class="filings-disclaimer">{{ t.dossier.filings.disclaimer }}</p>
          </div>
        </template>

        <!-- PERSON variant -->
        <template v-else-if="isPerson">
          <div v-if="personFilingsLoading" class="loading-inline">
            <span class="spinner-sm"></span>
          </div>
          <div v-else-if="!personFilingsData" class="empty-tab">
            <p>{{ t.dossier.filings.emptyPerson }}</p>
          </div>
          <div v-else class="filings-pane">

            <!-- No SEC data signal -->
            <div
              v-if="!personFilingsData.sec_cik && personFilingsData.roles.length === 0 && personFilingsData.transactions.length === 0"
              class="empty-tab"
            >
              <p>{{ t.dossier.filings.emptyPerson }}</p>
            </div>

            <template v-else>
              <!-- CIK header line -->
              <div v-if="personFilingsData.sec_cik" class="filings-registered-as">
                SEC CIK: {{ personFilingsData.sec_cik }}
              </div>

              <!-- Roles block -->
              <div v-if="personFilingsData.roles.length" class="filings-section">
                <div class="filings-section-header">
                  <span class="filings-section-title">{{ t.dossier.filings.boardSeats }}</span>
                  <span class="filings-chip">{{ personFilingsData.roles.length }}</span>
                </div>
                <ul class="filings-insider-list">
                  <li
                    v-for="(role, i) in personFilingsData.roles"
                    :key="i"
                    class="filings-insider-row"
                  >
                    <div class="filings-insider-header filings-role-row">
                      <div
                        class="filings-insider-avatar"
                        :style="role.org_has_image ? {backgroundImage: `url(${api.imageUrl(role.org_qid)})`} : {}"
                        @click="dossier.openDossier({ qid: role.org_qid, entityType: 'organization' })"
                        style="cursor: pointer"
                      >
                        <span v-if="!role.org_has_image" class="filings-avatar-initials">{{ (role.org_name || '?').slice(0,2).toUpperCase() }}</span>
                      </div>
                      <div class="filings-insider-info">
                        <span
                          class="filings-insider-name filings-org-link"
                          @click="dossier.openDossier({ qid: role.org_qid, entityType: 'organization' })"
                        >{{ role.org_name }}</span>
                        <span class="filings-insider-role">
                          {{ role.role || role.type?.replace(/_/g, ' ') }}
                          <span class="filings-subtype-chip" style="margin-left: 4px">{{ role.type?.replace(/_of$/, '').replace(/_/g, ' ') }}</span>
                        </span>
                      </div>
                      <div class="filings-insider-summary">
                        <span v-if="role.start_date">{{ role.start_date?.slice(0,10) }}</span>
                        <template v-if="role.last_seen"> – {{ role.last_seen?.slice(0,10) }}</template>
                      </div>
                    </div>
                  </li>
                </ul>
              </div>

              <!-- Transaction summary block -->
              <div v-if="personFilingsData.transaction_summary.count > 0" class="filings-section">
                <div class="filings-section-header">
                  <span class="filings-section-title">{{ t.dossier.filings.insiderTransactions }}</span>
                  <span class="filings-chip">{{ personFilingsData.transaction_summary.count }}</span>
                </div>
                <!-- Direction split -->
                <div class="filings-subtype-chips">
                  <span
                    v-for="(n, dir) in personFilingsData.transaction_summary.directions"
                    :key="dir"
                    class="filings-subtype-chip"
                  >
                    {{ dir === 'A' ? t.dossier.filings.directionBuy : t.dossier.filings.directionSell }} · {{ n }}
                  </span>
                </div>
                <!-- By-org breakdown -->
                <div class="filings-subtype-chips" style="margin-top: 4px">
                  <span
                    v-for="org in personFilingsData.transaction_summary.by_org"
                    :key="org.org_qid"
                    class="filings-subtype-chip"
                  >
                    {{ org.org_name }} · {{ org.count }}
                    <template v-if="org.last_date"> · {{ org.last_date.slice(0,10) }}</template>
                  </span>
                </div>
              </div>

              <!-- Recent transactions list -->
              <div v-if="personFilingsData.transactions.length" class="filings-section">
                <div class="filings-section-header">
                  <span class="filings-section-title">{{ t.dossier.filings.recentTransactions }}</span>
                  <span class="filings-chip">
                    {{ personFilingsData.page.offset + 1 }}–{{ Math.min(personFilingsData.page.offset + personFilingsData.transactions.length, personFilingsData.page.total) }}
                    / {{ personFilingsData.page.total }}
                  </span>
                </div>
                <ul class="filings-events-list">
                  <li
                    v-for="(txn, i) in personFilingsData.transactions"
                    :key="i"
                    class="filings-txn-row filings-event-row"
                  >
                    <span class="filings-txn-date filings-event-date">{{ txn.transaction_date?.slice(0,10) || '—' }}</span>
                    <span
                      class="filings-insider-name"
                      style="cursor: pointer; font-size: 12px; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                      @click="dossier.openDossier({ qid: txn.org_qid, entityType: 'organization' })"
                    >{{ txn.org_name }}</span>
                    <span class="filings-txn-dir" :class="txn.direction === 'A' ? 'dir-buy' : 'dir-sell'">
                      {{ txn.direction === 'A' ? t.dossier.filings.directionBuy : t.dossier.filings.directionSell }}
                    </span>
                    <span class="filings-txn-shares">{{ txn.shares != null ? txn.shares.toLocaleString() : '—' }}</span>
                    <span class="filings-txn-price">{{ txn.price_usd != null ? '$' + txn.price_usd.toFixed(2) : '—' }}</span>
                    <span class="filings-txn-total" v-if="txn.shares != null && txn.price_usd != null">${{ Math.round(txn.shares * txn.price_usd).toLocaleString() }}</span>
                    <a v-if="txn.filing_url" :href="txn.filing_url" target="_blank" rel="noopener" class="filings-ext-link">↗</a>
                  </li>
                </ul>
                <!-- Pagination -->
                <div v-if="personFilingsData.page.total > personFilingsData.page.limit" class="filings-pagination">
                  <button
                    class="filings-page-btn"
                    :disabled="personFilingsData.page.offset === 0"
                    @click="loadPersonFilings(Math.max(0, personFilingsData.page.offset - personFilingsData.page.limit))"
                  >&#8592;</button>
                  <button
                    class="filings-page-btn"
                    :disabled="personFilingsData.page.offset + personFilingsData.page.limit >= personFilingsData.page.total"
                    @click="loadPersonFilings(personFilingsData.page.offset + personFilingsData.page.limit)"
                  >&#8594;</button>
                </div>
              </div>

              <p class="filings-disclaimer">{{ t.dossier.filings.disclaimer }}</p>
            </template>
          </div>
        </template>
      </section>

      <!-- ── PARTICIPANTS (event only) ──────────────────────────── -->
      <section
        v-if="hasParticipantsData"
        class="dossier-section"
        id="section-participants"
      >
        <h3 class="section-heading">{{ t.dossier.tabs.participants }}</h3>
        <div v-if="eventPeopleLoading" class="loading-inline">
          <span class="spinner-sm"></span>
        </div>
        <ul v-else class="people-list">
          <li v-for="p in eventParticipants" :key="p._key" class="people-item">
            <span class="people-name">{{ p.name_en || p.name_it }}</span>
          </li>
        </ul>
      </section>

      <!-- ── CONTEXT (event only) ───────────────────────────────── -->
      <section
        v-if="hasContextData"
        class="dossier-section"
        id="section-context"
      >
        <h3 class="section-heading">{{ t.dossier.tabs.context }}</h3>
        <div v-if="detailLoading" class="loading-inline">
          <span class="spinner-sm"></span>
        </div>
        <div v-else class="field-group">
          <div v-if="detail?.location" class="field-row">
            <span class="field-label">{{ t.event.location }}</span>
            <span class="field-value">{{ detail.location }}</span>
          </div>
          <div v-if="detail?.country" class="field-row">
            <span class="field-label">{{ t.organization.country }}</span>
            <span class="field-value">{{ detail.country }}</span>
          </div>
          <div v-if="detail?.deaths != null" class="field-row">
            <span class="field-label">{{ t.event.deaths }}</span>
            <span class="field-value death-count">{{ detail.deaths.toLocaleString() }}</span>
          </div>
        </div>
      </section>

      <!-- Bottom padding so last section doesn't hug the edge -->
      <div class="body-spacer"></div>

    </div><!-- /dossier-body -->

    <!-- Image lightbox -->
    <Teleport to="body">
      <div
        v-if="showLightbox"
        class="lightbox-overlay"
        role="dialog"
        aria-modal="true"
        @click.self="showLightbox = false"
        @keydown.esc="showLightbox = false"
      >
        <div class="lightbox-inner">
          <button class="lightbox-close" @click="showLightbox = false" :aria-label="t.ui.close">&times;</button>
          <img :src="api.imageUrl(dossier.qid.value)" :alt="prefName(node)" class="lightbox-img" />
          <div class="lightbox-name">{{ prefName(node) }}</div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onUnmounted } from 'vue'
import { useApi } from '../composables/useApi.js'
import { useLocale } from '../composables/useLocale.js'
import { useDossier } from '../composables/useDossier.js'

const props = defineProps({
  node: Object,   // Cytoscape node data snapshot
})
const emit = defineEmits(['close', 'enter'])

const api = useApi()
const dossier = useDossier()
const { t, locale, prefName, prefDesc, dateLocale } = useLocale()

// ── Entity type helpers ───────────────────────────────────────────────────────
const isPerson = computed(() => !props.node?.entityType || props.node?.entityType === 'person')
const isOrg    = computed(() => props.node?.entityType === 'organization')
const isEvent  = computed(() => props.node?.entityType === 'event')
// Synthetic nodes (e.g. occ_physicist) have no real DB documents — skip endpoints that require a Wikidata QID
const isSynthetic = computed(() => !!props.node?.qid && !/^Q\d+$/.test(props.node.qid))

// ── Scrollable body ref (for IntersectionObserver) ────────────────────────────
const bodyEl = ref(null)

// ── Active anchor for highlighting ───────────────────────────────────────────
const activeAnchor = ref('overview')

// ── IntersectionObserver setup ────────────────────────────────────────────────
let observer = null

function initObserver() {
  cleanupObserver()
  if (!bodyEl.value) return

  // rootMargin: top offset covers the sticky header (~90px) + anchor strip (~40px) = ~130px
  // We use a negative top margin so a section is "active" once it reaches the top area.
  // threshold 0 means fire as soon as any pixel enters/exits the root.
  observer = new IntersectionObserver(
    (entries) => {
      // Find the topmost intersecting section
      const intersecting = entries
        .filter(e => e.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
      if (intersecting.length > 0) {
        const id = intersecting[0].target.id?.replace('section-', '')
        if (id) activeAnchor.value = id
      }
    },
    {
      root: bodyEl.value,
      rootMargin: '-10% 0px -60% 0px',
      threshold: 0,
    }
  )

  // Observe all rendered section elements inside the body
  nextTick(() => {
    if (!bodyEl.value) return
    bodyEl.value.querySelectorAll('[id^="section-"]').forEach(el => observer?.observe(el))
  })
}

function cleanupObserver() {
  if (observer) {
    observer.disconnect()
    observer = null
  }
}

function scrollToSection(id) {
  const body = bodyEl.value
  if (!body) return
  const el = body.querySelector(`#section-${id}`)
  if (!el) return
  // Scroll only the panel body — never the document. scrollIntoView() also
  // scrolls every scrollable ancestor, which on mobile (where the panel is an
  // in-flow bottom sheet) scrolled the whole page and revealed the white,
  // text-only server-rendered #seo-content block behind the app.
  const top = body.scrollTop + el.getBoundingClientRect().top - body.getBoundingClientRect().top
  body.scrollTo({ top, behavior: 'smooth' })
}

// ── Detail refs — declared before watchers to avoid TDZ ──────────────────────
const detail = ref(null)
const detailLoading = ref(false)

// ── Named has-data computeds ─────────────────────────────────────────────────
const careerItems = ref([])
const careerLoading = ref(false)
const hasCareerData = computed(() => isPerson.value && (careerLoading.value || careerItems.value.length > 0))

const networkSummary = ref([])
const networkLoading = ref(false)
const hasNetworkData = computed(() => isPerson.value && (networkLoading.value || networkSummary.value.length > 0))

const hasAwardsData = computed(() => isPerson.value && (detailLoading.value || !!detail.value?.awards_top5?.length))

const orgPeople = ref([])
const orgMembers = ref([])
const orgPeopleLoading = ref(false)
const hasOrgPeopleData = computed(() => isOrg.value && (orgPeopleLoading.value || orgPeople.value.length > 0))
const hasOrgMembersData = computed(() => isOrg.value && (orgPeopleLoading.value || orgMembers.value.length > 0))

const structureTree = ref(null)
const structureLoading = ref(false)
const hasStructureData = computed(() => {
  if (!isOrg.value) return false
  if (structureLoading.value) return true
  if (!structureTree.value) return false
  return structureTree.value.parents.length > 0 || structureTree.value.subsidiaries.length > 0
})

const filingsData = ref(null)
const filingsLoading = ref(false)
const personFilingsData = ref(null)
const personFilingsLoading = ref(false)
const hasFilingsData = computed(() => {
  if (isOrg.value) return filingsLoading.value || !!filingsData.value
  if (isPerson.value) {
    if (personFilingsLoading.value) return true
    if (!personFilingsData.value) return false
    const d = personFilingsData.value
    return !!(d.sec_cik || d.roles.length > 0 || d.transactions.length > 0)
  }
  return false
})

const eventParticipants = ref([])
const eventPeopleLoading = ref(false)
const hasParticipantsData = computed(() => isEvent.value && (eventPeopleLoading.value || eventParticipants.value.length > 0))

const hasContextData = computed(() => {
  if (!isEvent.value) return false
  if (detailLoading.value) return true
  return !!(detail.value?.location || detail.value?.country || detail.value?.deaths != null)
})

// ── Anchor strip: ordered sections with visibility ────────────────────────────
const ALL_PERSON_SECTIONS  = ['overview', 'career', 'network', 'awards', 'filings']
const ALL_ORG_SECTIONS     = ['overview', 'people', 'structure', 'members', 'filings']
const ALL_EVENT_SECTIONS   = ['overview', 'participants', 'context']

const sectionVisible = computed(() => ({
  overview: true,
  career: hasCareerData.value,
  network: hasNetworkData.value,
  awards: hasAwardsData.value,
  filings: hasFilingsData.value,
  people: hasOrgPeopleData.value,
  structure: hasStructureData.value,
  members: hasOrgMembersData.value,
  participants: hasParticipantsData.value,
  context: hasContextData.value,
}))

const visibleSections = computed(() => {
  const order = isPerson.value ? ALL_PERSON_SECTIONS : isOrg.value ? ALL_ORG_SECTIONS : ALL_EVENT_SECTIONS
  return order
    .filter(id => sectionVisible.value[id])
    .map(id => ({ id, label: t.value.dossier.tabs[id] || id }))
})

// ── Reset + parallel load when entity changes ─────────────────────────────────
function resetState() {
  cleanupObserver()
  activeAnchor.value = 'overview'
  detail.value = null
  careerItems.value = []
  networkSummary.value = []
  orgPeople.value = []
  orgMembers.value = []
  eventParticipants.value = []
  structureTree.value = null
  filingsData.value = null
  personFilingsData.value = null
}

watch(() => props.node?.qid, (newQid) => {
  resetState()
  if (newQid) loadAll()
}, { immediate: true })

// Re-init observer after visibleSections settles (data arrives)
watch(visibleSections, () => {
  nextTick(initObserver)
}, { deep: true })

// Re-init observer when body mounts
watch(bodyEl, (el) => {
  if (el) nextTick(initObserver)
})

onUnmounted(cleanupObserver)

// ── Avatar helpers ────────────────────────────────────────────────────────────
const initials = computed(() => {
  const name = prefName(props.node) || '?'
  return name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase()
})

const avatarClass = computed(() => {
  if (isOrg.value) return 'avatar-org'
  return 'avatar-person'
})

const avatarStyle = computed(() => {
  if (props.node?.hasImage && props.node?.qid) {
    return {
      backgroundImage: `url(${api.imageUrl(props.node.qid)})`,
      backgroundSize: isPerson.value ? 'cover' : 'contain',
      backgroundPosition: isPerson.value ? 'center top' : 'center',
      backgroundRepeat: 'no-repeat',
    }
  }
  return { backgroundColor: props.node?.color || 'var(--color-person)' }
})

const showLightbox = ref(false)

// ── Data fetching ─────────────────────────────────────────────────────────────

// --- Parallel load all sections on entity open ---
async function loadAll() {
  if (!props.node?.qid) return
  const qid = props.node.qid
  const promises = []

  // Always load detail (overview enrichment)
  promises.push(loadDetail())

  // Load section data in parallel based on entity type
  if (isPerson.value) {
    promises.push(loadCareer())
    promises.push(loadNetwork())
    promises.push(loadPersonFilings())
  } else if (isOrg.value) {
    promises.push(loadOrgPeople())
    promises.push(loadStructure())
    promises.push(loadFilings())
  } else if (isEvent.value) {
    promises.push(loadEventParticipants())
  }

  // Fire all in parallel; individual loaders handle their own error states
  await Promise.allSettled(promises)
}

// --- Overview detail (enrichment fields) ---
async function loadDetail() {
  if (!props.node?.qid || isSynthetic.value) return
  detailLoading.value = true
  try {
    if (isPerson.value) {
      detail.value = await api.getPerson(props.node.qid)
    } else if (isOrg.value) {
      detail.value = await api.getOrganization(props.node.qid)
    } else if (isEvent.value) {
      detail.value = await api.getEvent(props.node.qid)
    }
  } catch {
    detail.value = null
  } finally {
    detailLoading.value = false
  }
}

// --- Career timeline (person) ---
async function loadCareer() {
  if (!props.node?.qid) return
  careerLoading.value = true
  try {
    const data = await api.getConnections(props.node.qid)
    const CAREER_TYPES = new Set(['position_held', 'employee', 'employer'])
    const edges = (data.connections || [])
      .filter(e => CAREER_TYPES.has(e.type))
      .map(e => {
        // The edge points from person → org. org QID is the "_to" field.
        const orgId = e._to?.split('/')?.pop() || e._to
        return {
          org_name: e.org_name || e.role_org || orgId,
          role: e.role || null,
          start_date: e.start_date || null,
          end_date: e.end_date || null,
          _sort: e.start_date || '0000',
        }
      })
    // Sort by start_date DESC (most recent first)
    edges.sort((a, b) => b._sort.localeCompare(a._sort))
    careerItems.value = edges
  } catch {
    careerItems.value = []
  } finally {
    careerLoading.value = false

    // If we got edges but org names are missing, enrich from neighborhood data
    if (careerItems.value.some(i => !i.org_name || i.org_name.startsWith('Q'))) {
      await enrichCareerNames()
    }
  }
}

async function enrichCareerNames() {
  // Fetch neighborhood to resolve org names for career items
  try {
    const nbData = await api.getNeighborhood(props.node.qid, { node_types: 'organization' })
    const orgMap = {}
    for (const v of (nbData.vertices || [])) {
      if (v?._key) orgMap[v._key] = v.name_en || v.name_it || v._key
    }
    careerItems.value = careerItems.value.map(item => {
      if (!item.org_name || item.org_name.startsWith('Q')) {
        // org_name was set to the raw QID — try to resolve
        return { ...item, org_name: orgMap[item.org_name] || item.org_name }
      }
      return item
    })
  } catch { /* silently */ }
}

// --- Network summary (person) ---

// Inverse pairs for directional relationships — when an edge points AT the
// focal person, the raw type describes the other side's role, so flip it to
// get the focal person's label. Symmetric types (sibling, spouse, …) map to
// themselves and don't need to appear here.
const INVERSE_TYPE = {
  parent: 'child', child: 'parent',
  employee: 'employer', employer: 'employee',
  participant: 'participant_in', participant_in: 'participant',
  killed_by: 'victim', victim: 'killed_by',
}

async function loadNetwork() {
  if (!props.node?.qid) return
  networkLoading.value = true
  try {
    const data = await api.getConnections(props.node.qid)
    const edges = data.connections || []
    const focalId = `persons/${props.node.qid}`
    // Group by semantic type (from focal person's perspective), deduped by
    // (type, other_qid) so symmetric edges stored in both directions and
    // inverse pairs (parent↔child, employer↔employee) don't double-count.
    const groups = {}
    const seen = {} // type -> Set of other qids
    for (const e of edges) {
      const rawType = e.type || 'unknown'
      const pointsAtUs = e._to === focalId
      const type = pointsAtUs ? (INVERSE_TYPE[rawType] ?? rawType) : rawType
      const otherId = pointsAtUs ? e._from?.split('/')?.pop() : e._to?.split('/')?.pop()
      if (!otherId) continue
      if (!seen[type]) seen[type] = new Set()
      if (seen[type].has(otherId)) continue
      seen[type].add(otherId)
      if (!groups[type]) groups[type] = { count: 0, names: [] }
      groups[type].count++
      groups[type].names.push(otherId)
    }
    // Fetch neighborhood once to resolve names
    const nbData = await api.getNeighborhood(props.node.qid, { node_types: 'person,organization,event' })
    const nameMap = {}
    for (const v of (nbData.vertices || [])) {
      if (v?._key) nameMap[v._key] = v.name_en || v.name_it || v._key
    }
    networkSummary.value = Object.entries(groups)
      .sort((a, b) => b[1].count - a[1].count)
      .slice(0, 8) // top 8 relationship types
      .map(([type, g]) => ({
        type,
        count: g.count,
        top: g.names.map(id => nameMap[id] || id).filter(Boolean),
      }))
  } catch {
    networkSummary.value = []
  } finally {
    networkLoading.value = false
  }
}

// --- Org people (org) ---
async function loadOrgPeople() {
  if (!props.node?.qid || isSynthetic.value) return
  orgPeopleLoading.value = true
  try {
    const data = await api.getOrgMembers(props.node.qid)
    orgPeople.value = (data.leaders || []).map(r => ({ ...r.person, role: r.edge?.role || r.edge?.type || null }))
    orgMembers.value = (data.members || []).map(r => ({ ...r.person, role: r.edge?.role || r.edge?.type || null }))
  } catch {
    orgPeople.value = []
    orgMembers.value = []
  } finally {
    orgPeopleLoading.value = false
  }
}

// --- Corporate structure (org) ---
async function loadStructure() {
  if (!props.node?.qid || isSynthetic.value) return
  structureLoading.value = true
  try {
    // Fetch neighborhood — org_structure edges included if registered in graph
    // We use the neighborhood endpoint to get org-to-org edges
    const nbData = await api.getNeighborhood(props.node.qid, {
      node_types: 'organization',
      depth: 3,
      limit: 50,
    })
    const vertices = nbData.vertices || []
    const edges = nbData.edges || []
    const orgQid = props.node.qid

    const PARENT_TYPES = new Set(['parent_of', 'owned_by'])
    const SUB_TYPES    = new Set(['subsidiary_of', 'owns'])

    const orgMap = {}
    for (const v of vertices) {
      if (v?._key) orgMap[v._key] = v
    }

    const parents = []
    const subsidiaries = []

    for (const e of edges) {
      const fromId = e._from?.split('/')?.pop()
      const toId   = e._to?.split('/')?.pop()
      if (PARENT_TYPES.has(e.type)) {
        // current org is child → parent is the other end
        const parentId = fromId === orgQid ? toId : fromId
        if (orgMap[parentId]) parents.push(orgMap[parentId])
      } else if (SUB_TYPES.has(e.type)) {
        const subId = fromId === orgQid ? toId : fromId
        if (orgMap[subId]) subsidiaries.push(orgMap[subId])
      }
    }

    structureTree.value = { parents, subsidiaries }
  } catch {
    structureTree.value = null
  } finally {
    structureLoading.value = false
  }
}

// --- SEC Filings (org) ---
const expandedInsiders = ref(new Set())

function toggleInsider(personQid) {
  const s = new Set(expandedInsiders.value)
  if (s.has(personQid)) s.delete(personQid)
  else s.add(personQid)
  expandedInsiders.value = s
}

async function loadFilings() {
  if (!props.node?.qid || isSynthetic.value) return
  filingsLoading.value = true
  try {
    filingsData.value = await api.getFilings(props.node.qid)
  } catch {
    filingsData.value = null
  } finally {
    filingsLoading.value = false
  }
}

// --- SEC Filings (person) ---
const personFilingsTxnOffset = ref(0)

async function loadPersonFilings(offset = 0) {
  if (!props.node?.qid) return
  personFilingsLoading.value = true
  try {
    personFilingsData.value = await api.getPersonFilings(props.node.qid, {
      transactions_offset: offset,
      transactions_limit: 50,
    })
    personFilingsTxnOffset.value = offset
  } catch {
    personFilingsData.value = null
  } finally {
    personFilingsLoading.value = false
  }
}

// --- Event participants ---
async function loadEventParticipants() {
  if (!props.node?.qid) return
  eventPeopleLoading.value = true
  try {
    const nbData = await api.getNeighborhood(props.node.qid, { node_types: 'person,event' })
    eventParticipants.value = (nbData.vertices || [])
      .filter(v => v?._key && v._key !== props.node.qid && (!v.type || v.type === 'person'))
      .slice(0, 30)
  } catch {
    eventParticipants.value = []
  } finally {
    eventPeopleLoading.value = false
  }
}

// ── Formatters ────────────────────────────────────────────────────────────────
function formatDate(dateStr) {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr
  return d.toLocaleDateString(dateLocale.value, { year: 'numeric', month: 'long', day: 'numeric' })
}

function formatYear(dateStr) {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr.slice(0, 4) || dateStr
  return d.getFullYear().toString()
}

function translateOccupation(occ) {
  return t.value.occupations?.[occ] || occ?.replace(/_/g, ' ') || occ
}

function translateOrgType(type) {
  return t.value.orgTypes?.[type] || type?.replace(/_/g, ' ') || type
}

function translateEventType(type) {
  return t.value.eventTypes?.[type] || type?.replace(/_/g, ' ') || type
}

function translateEdgeType(type) {
  return t.value.edgeTypes?.[type] || type?.replace(/_/g, ' ') || type
}

// Panel focus management
const panelEl = ref(null)
watch(() => dossier.open.value, async (isOpen) => {
  if (isOpen) {
    await nextTick()
    panelEl.value?.focus()
  }
})
</script>

<style scoped>
/* ── Panel shell ───────────────────────────────────────────────────────────── */
.dossier-panel {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 440px;
  max-height: calc(100% - 20px);
  background: var(--card);
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  box-shadow: 0 6px 32px rgba(0, 0, 0, 0.14);
  display: flex;
  flex-direction: column;
  z-index: 8;
  overflow: hidden;
  /* Entrance animation */
  animation: dossier-in 0.22s cubic-bezier(0.22, 1, 0.36, 1) both;
  outline: none; /* focus managed programmatically */
}

@keyframes dossier-in {
  from {
    opacity: 0;
    transform: translateX(18px) scale(0.97);
  }
  to {
    opacity: 1;
    transform: translateX(0) scale(1);
  }
}

/* ── Header ───────────────────────────────────────────────────────────────── */
.dossier-header {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 18px 18px 14px;
  border-bottom: 1px solid var(--border-faint);
  flex-shrink: 0;
  position: relative;
}

.dossier-identity {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  flex: 1;
  min-width: 0;
  padding-right: 36px; /* space for close btn */
}

.dossier-avatar {
  width: 48px;
  height: 64px;
  border-radius: 8px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 17px;
  color: #fff;
  background: var(--color-person);
}

.dossier-avatar.avatar-org {
  width: 56px;
  height: 56px;
  border-radius: 9px;
  background: var(--color-org);
}

.avatar-clickable {
  cursor: pointer;
  transition: opacity 0.15s;
}
.avatar-clickable:hover { opacity: 0.82; }

.dossier-title-block {
  flex: 1;
  min-width: 0;
}

.dossier-name {
  margin: 0 0 3px;
  font-family: var(--font-display);
  font-size: 19px;
  font-weight: 700;
  line-height: 1.25;
  color: var(--text);
  overflow-wrap: break-word;
  word-break: break-word;
}

.dossier-desc {
  margin: 0;
  font-size: 12px;
  line-height: 1.45;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.dossier-close {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 32px;
  height: 32px;
  border: none;
  background: none;
  color: var(--text-subtle);
  font-size: 22px;
  cursor: pointer;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s, color 0.15s;
  flex-shrink: 0;
  line-height: 1;
}
.dossier-close:hover {
  background: rgba(0, 0, 0, 0.08);
  color: var(--text);
}

/* ── Anchor chip strip ────────────────────────────────────────────────────── */
.dossier-anchors {
  display: flex;
  gap: 6px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border-faint);
  flex-shrink: 0;
  overflow-x: auto;
  scrollbar-width: none;
  background: var(--card);
  position: sticky;
  top: 0;
  z-index: 2;
}
.dossier-anchors::-webkit-scrollbar { display: none; }

.anchor-chip {
  display: inline-flex;
  align-items: center;
  padding: 4px 12px;
  border-radius: 20px;
  border: 1px solid var(--border-subtle);
  background: var(--surface);
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  white-space: nowrap;
  cursor: pointer;
  transition: background 0.14s, color 0.14s, border-color 0.14s;
  flex-shrink: 0;
}
.anchor-chip:hover {
  background: var(--card);
  color: var(--text);
  border-color: var(--border-visible);
}
.anchor-chip.active {
  background: rgba(27, 77, 142, 0.1);
  border-color: rgba(27, 77, 142, 0.3);
  color: var(--accent);
}

/* ── Body / Sections ──────────────────────────────────────────────────────── */
.dossier-body {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
}

/* Each named section — sticky header (header ~88px) + anchor strip (~58px) */
.dossier-section {
  padding: 18px 18px 6px;
  scroll-margin-top: 58px;
}

/* Subsection within overview (e.g. notable works) — no extra scroll target */
.dossier-subsection {
  margin-top: 18px;
}

.body-spacer {
  height: 48px;
}

/* ── Shared field layout ──────────────────────────────────────────────────── */
.field-group {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 20px;
}

.field-row {
  display: flex;
  gap: 10px;
  font-size: 13px;
  align-items: baseline;
}

.field-label {
  min-width: 100px;
  flex-shrink: 0;
  color: var(--text-subtle);
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.field-value {
  color: var(--text);
  line-height: 1.45;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  overflow-wrap: break-word;
  word-break: break-word;
}

.field-sub {
  font-size: 12px;
  color: var(--text-muted);
}

.field-cause {
  font-size: 12px;
  color: var(--text-subtle);
  font-style: italic;
}

.death-count {
  font-variant-numeric: tabular-nums;
  color: var(--color-wrong);
  font-weight: 600;
}

.ext-link {
  color: var(--accent);
  text-decoration: none;
  font-size: 12px;
  overflow-wrap: break-word;
  word-break: break-all;
}
.ext-link:hover { text-decoration: underline; }

/* ── Section headings ─────────────────────────────────────────────────────── */
.section-heading {
  margin: 0 0 10px;
  font-family: var(--font-display);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--text-muted);
  text-transform: uppercase;
}

/* ── Notable works ────────────────────────────────────────────────────────── */
.works-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.works-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  color: var(--text);
  line-height: 1.4;
}

.works-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--color-person);
  flex-shrink: 0;
  margin-top: 1px;
  align-self: center;
}

/* ── Career timeline ──────────────────────────────────────────────────────── */
.career-timeline {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.career-entry {
  display: flex;
  gap: 14px;
  min-height: 52px;
}

.career-line {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 16px;
  flex-shrink: 0;
  padding-top: 3px;
}

.career-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2.5px solid var(--accent);
  background: var(--card);
  flex-shrink: 0;
  transition: background 0.15s;
}

.career-entry--current .career-dot {
  background: var(--accent);
}

.career-track {
  flex: 1;
  width: 2px;
  background: var(--border-subtle);
  margin-top: 3px;
}

.career-content {
  flex: 1;
  padding-bottom: 18px;
  min-width: 0;
}

.career-org {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.3;
  overflow-wrap: break-word;
}

.career-role {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 2px;
  overflow-wrap: break-word;
}

.career-period {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--text-subtle);
  font-variant-numeric: tabular-nums;
}

.period-sep {
  color: var(--border-visible);
}

.career-current-badge {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 10px;
  background: rgba(27, 77, 142, 0.09);
  color: var(--accent);
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.03em;
}

/* ── Network summary ──────────────────────────────────────────────────────── */
.network-summary {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.network-group {
  border-left: 2.5px solid var(--border-subtle);
  padding-left: 12px;
}

.network-group-header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 4px;
}

.network-type {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  text-transform: capitalize;
}

.network-count {
  font-size: 11.5px;
  color: var(--text-subtle);
  font-variant-numeric: tabular-nums;
  background: var(--surface);
  padding: 1px 6px;
  border-radius: 8px;
}

.network-names {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.network-names li {
  font-size: 12px;
  color: var(--text-muted);
}

.expand-network-btn {
  margin-top: 6px;
  padding: 8px 14px;
  border: 1px solid rgba(27, 77, 142, 0.3);
  border-radius: 6px;
  background: rgba(27, 77, 142, 0.06);
  color: var(--accent);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s;
  align-self: flex-start;
}
.expand-network-btn:hover {
  background: rgba(27, 77, 142, 0.11);
}

/* ── Awards ───────────────────────────────────────────────────────────────── */
.awards-section {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.awards-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.award-chip {
  display: inline-block;
  padding: 5px 12px;
  border-radius: 4px;
  background: var(--surface);
  border: 1px solid var(--border-subtle);
  font-size: 12.5px;
  color: var(--text);
  line-height: 1.4;
  font-weight: 500;
}

.awards-more {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
  font-style: italic;
}

/* ── People / members list ────────────────────────────────────────────────── */
.people-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.people-item {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  padding: 6px 0;
  border-bottom: 1px solid var(--border-faint);
  font-size: 13px;
}
.people-item:last-child { border-bottom: none; }

.people-name { color: var(--text); font-weight: 500; }
.people-role { color: var(--text-muted); font-size: 11.5px; flex-shrink: 0; }

/* ── Corporate structure tree ─────────────────────────────────────────────── */
.structure-tree {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.tree-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tree-section-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-subtle);
  margin-bottom: 4px;
}

.tree-chain {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tree-node {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 13px;
}

.tree-connector {
  color: var(--text-subtle);
  font-size: 11px;
}

.tree-name { color: var(--text); }

.tree-current {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  background: rgba(27, 77, 142, 0.07);
  border-radius: 6px;
  border-left: 3px solid var(--accent);
}

.tree-bullet {
  color: var(--accent);
  font-size: 10px;
}

.tree-name--current {
  font-weight: 700;
  font-size: 14px;
  font-family: var(--font-display);
}

.tree-node--parent .tree-name { color: var(--text-muted); }
.tree-node--sub .tree-name { color: var(--text); }

/* ── Loading / empty ──────────────────────────────────────────────────────── */
.loading-inline {
  display: flex;
  justify-content: center;
  padding: 32px 0;
}

.spinner-sm {
  display: inline-block;
  width: 20px;
  height: 20px;
  border: 2px solid var(--border-subtle);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.empty-tab {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 36px 0;
  gap: 8px;
  color: var(--text-subtle);
}

.empty-icon {
  font-size: 24px;
  opacity: 0.4;
}

.empty-tab p {
  margin: 0;
  font-size: 13px;
}

/* ── Filings pane ─────────────────────────────────────────────────────────── */
.filings-pane {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.filings-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.filings-registered-as {
  font-size: 11.5px;
  color: var(--text-muted);
  font-style: italic;
  margin-bottom: 4px;
}

.filings-meta-dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 3px 12px;
  margin: 0;
  font-size: 12.5px;
}

.filings-meta-dl dt {
  color: var(--text-muted);
  font-weight: 500;
  white-space: nowrap;
}

.filings-meta-dl dd {
  color: var(--text);
  margin: 0;
}

.filings-section-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.filings-section-title {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.filings-chip {
  font-size: 11px;
  color: var(--accent);
  background: rgba(27, 77, 142, 0.09);
  border-radius: 4px;
  padding: 1px 6px;
}

.filings-insider-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.filings-insider-row {
  border-bottom: 1px solid var(--border-faint);
}
.filings-insider-row:last-child { border-bottom: none; }

.filings-insider-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 0;
  cursor: pointer;
  user-select: none;
}

.filings-insider-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--surface);
  background-size: cover;
  background-position: center;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.filings-avatar-initials {
  font-size: 10px;
  font-weight: 700;
  color: var(--text-muted);
}

.filings-insider-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.filings-insider-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.filings-insider-role {
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.filings-insider-summary {
  font-size: 11.5px;
  color: var(--text-muted);
  flex-shrink: 0;
  text-align: right;
}

.filings-expand-icon {
  font-size: 9px;
  color: var(--text-subtle);
  flex-shrink: 0;
}

.filings-txn-list {
  padding: 4px 0 8px 36px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.filings-txn-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.filings-txn-date { color: var(--text-muted); flex-shrink: 0; min-width: 72px; }
.filings-txn-shares { color: var(--text-muted); flex-shrink: 0; }
.filings-txn-price { color: var(--text-muted); flex-shrink: 0; }
.filings-txn-total {
  color: var(--text);
  font-weight: 600;
  flex-shrink: 0;
  margin-left: 2px;
  padding-left: 6px;
  border-left: 1.5px solid var(--border-subtle);
}

.filings-txn-dir {
  font-size: 10.5px;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 3px;
  flex-shrink: 0;
}

.dir-buy { background: rgba(46, 160, 67, 0.12); color: #1a7a32; }
.dir-sell { background: rgba(176, 48, 48, 0.1); color: #8b2020; }

.filings-ext-link {
  color: var(--accent);
  font-size: 12px;
  text-decoration: none;
  flex-shrink: 0;
}
.filings-ext-link:hover { color: var(--accent-hover); }

.filings-empty-section {
  color: var(--text-subtle);
  font-size: 13px;
  margin: 0;
}

.filings-unmatched {
  margin-top: 6px;
}

.filings-unmatched-summary {
  font-size: 12px;
  color: var(--text-muted);
  cursor: pointer;
  user-select: none;
  padding: 4px 0;
}

.filings-unmatched-list {
  list-style: none;
  margin: 6px 0 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.filings-unmatched-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  padding: 3px 0;
  border-bottom: 1px solid var(--border-faint);
}
.filings-unmatched-item:last-child { border-bottom: none; }

.filings-unmatched-name { color: var(--text); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.filings-unmatched-meta { color: var(--text-muted); flex-shrink: 0; font-size: 11px; }

/* Unmatched directors: plain name chips, no rows */
.filings-unmatched-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.filings-unmatched-chip {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 4px;
  background: var(--surface);
  color: var(--text-muted);
  border: 1px solid var(--border-faint);
}

/* Director/officer role chips */
.filings-director-roles {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.filings-director-role-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--surface);
  border: 1px solid var(--border-faint);
  color: var(--text);
}
.filings-director-role-chip.role-officer { border-left: 2px solid var(--accent); }
.filings-director-role-chip.role-director { border-left: 2px solid var(--color-person, #bf6b28); }
.filings-role-label { font-weight: 500; }
.filings-role-dates { color: var(--text-muted); font-size: 11px; }
.filings-source-badge {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 600;
}
.filings-source-badge.badge-def14a { background: rgba(27, 77, 142, 0.1); color: var(--accent); }
.filings-source-badge.badge-form3 { background: rgba(120, 120, 120, 0.12); color: var(--text-muted); }

.filings-subtype-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 4px;
}

.filings-subtype-chip {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 4px;
  background: var(--surface);
  color: var(--text-muted);
  border: 1px solid var(--border-faint);
}

.filings-events-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.filings-event-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  padding: 5px 0;
  border-bottom: 1px solid var(--border-faint);
}
.filings-event-row:last-child { border-bottom: none; }

.filings-event-date {
  color: var(--accent);
  font-size: 11.5px;
  font-weight: 600;
  flex-shrink: 0;
  min-width: 82px;
}

.filings-event-subtype {
  color: var(--text);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.filings-event-items {
  font-size: 11px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.filings-event-amend {
  font-size: 10px;
  font-weight: 700;
  color: var(--text-muted);
  border: 1px solid var(--border-faint);
  border-radius: 3px;
  padding: 0 3px;
  flex-shrink: 0;
}

.filings-former-names {
  margin-top: 4px;
}

.filings-disclaimer {
  font-size: 11px;
  color: var(--text-subtle);
  margin: 0;
  padding-top: 8px;
  border-top: 1px solid var(--border-faint);
}

.filings-role-row {
  cursor: default;
}

.filings-org-link {
  cursor: pointer;
  color: var(--accent);
}
.filings-org-link:hover {
  color: var(--accent-hover);
  text-decoration: underline;
}

.filings-pagination {
  display: flex;
  gap: 6px;
  margin-top: 8px;
}

.filings-page-btn {
  padding: 3px 10px;
  font-size: 12px;
  border: 1px solid var(--border-faint);
  border-radius: 4px;
  background: var(--surface);
  color: var(--accent);
  cursor: pointer;
}
.filings-page-btn:disabled {
  opacity: 0.35;
  cursor: default;
}
.filings-page-btn:not(:disabled):hover {
  background: var(--card);
}

/* ── Desktop readability ──────────────────────────────────────────────────────
   The dossier sits as a side panel next to a large graph. Its default sizes
   were tuned for narrow mobile sheets, leaving desktop text uncomfortably
   small. Widen the panel slightly and bump the body / label / table sizes to
   a comfortable editorial scale. Mobile/tablet (< 1024px) stay unchanged. */
@media (min-width: 1024px) {
  .dossier-panel { width: 480px; }

  /* Header */
  .dossier-name { font-size: 22px; }
  .dossier-desc { font-size: 14px; line-height: 1.5; }

  /* Anchor chips */
  .anchor-chip { font-size: 12.5px; padding: 5px 13px; }

  /* Section headings + shared fields */
  .section-heading { font-size: 14px; }
  .field-row { font-size: 15px; }
  .field-label { font-size: 12.5px; min-width: 108px; }
  .field-sub,
  .field-cause { font-size: 13.5px; }
  .ext-link { font-size: 13.5px; }

  /* Notable works */
  .works-item { font-size: 14.5px; }

  /* Career timeline */
  .career-org { font-size: 15px; }
  .career-role { font-size: 13px; }
  .career-period { font-size: 12.5px; }

  /* Network summary */
  .network-type { font-size: 14px; }
  .network-count { font-size: 12.5px; }
  .network-names li { font-size: 13.5px; }
  .expand-network-btn { font-size: 14px; }

  /* Awards / people / tree */
  .award-chip { font-size: 13.5px; }
  .people-item { font-size: 14px; }
  .people-role { font-size: 12.5px; }
  .tree-node { font-size: 14px; }
  .tree-name--current { font-size: 15px; }

  /* Filings — SEC directors, holders, transactions tables */
  .filings-section-title { font-size: 12px; }
  .filings-chip { font-size: 12px; }
  .filings-meta-dl { font-size: 13.5px; }
  .filings-insider-name { font-size: 14px; }
  .filings-insider-role { font-size: 12.5px; }
  .filings-insider-summary { font-size: 12.5px; }
  .filings-avatar-initials { font-size: 11px; }
  .filings-txn-row { font-size: 13px; }
  .filings-txn-dir { font-size: 11.5px; }
  .filings-event-row { font-size: 13px; }
  .filings-event-date { font-size: 12.5px; }
  .filings-event-items { font-size: 12px; }
  .filings-subtype-chip { font-size: 12px; }
  .filings-director-role-chip { font-size: 12.5px; }
  .filings-role-dates { font-size: 12px; }
  .filings-unmatched-summary,
  .filings-unmatched-item,
  .filings-unmatched-chip { font-size: 13px; }
  .filings-registered-as { font-size: 12.5px; }
  .filings-disclaimer { font-size: 12px; }
  .filings-empty-section { font-size: 14px; }
  .empty-tab p { font-size: 14px; }
}

/* ── Responsive ───────────────────────────────────────────────────────────── */
@media (max-width: 768px) {
  .filings-txn-row {
    flex-wrap: wrap;
    row-gap: 2px;
  }
  .dossier-panel {
    position: relative;
    top: auto;
    right: auto;
    width: 100%;
    border: none;
    border-top: 1px solid var(--border-subtle);
    border-radius: 0;
    max-height: 45vh;
    box-shadow: none;
    animation: none;
  }
  .dossier-anchors {
    padding: 6px 10px;
    gap: 4px;
  }
  .anchor-chip {
    padding: 3px 10px;
    font-size: 11.5px;
  }
  .dossier-section {
    padding: 14px 14px 6px;
  }
}
</style>

<!-- Lightbox: global (not scoped) to escape stacking context -->
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
  border: 1px solid rgba(0,0,0,0.2);
  border-radius: 50%;
  color: var(--text);
  font-size: 20px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
}
.lightbox-close:hover { background: var(--bg); }
.lightbox-name {
  color: rgba(255,255,255,0.8);
  font-size: 14px;
}
</style>
