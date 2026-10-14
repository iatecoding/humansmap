# Architecture — HumansMap

Knowledge graph for exploring connections between public figures. Two modes: **Explore** (interactive graph) and **Play** (path-finding game + trivia).

---

## Stack at a Glance

| Layer | Technology | Role |
|---|---|---|
| Web server | Caddy 2 | Reverse proxy, HTTPS termination, static assets |
| Frontend | Vue 3 + Cytoscape.js | SPA — graph canvas, games, search |
| API | FastAPI + Uvicorn | REST, validation, rate limiting |
| Database | ArangoDB 3.11 | Graph storage, full-text search, BFS traversal |
| Cache | Redis 7 | Query result caching (optional, fallback-safe) |
| Images | Local filesystem | Optimized JPEG cache at `/app/images/{qid}.jpg` |
| Data source | Wikidata SPARQL / RDF dump | ETL pipeline — persons, orgs, events, edges |

---

## Repository Layout

```
/
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI app, middleware, lifespan
│   │   ├── config.py        # All tuneable thresholds (env-overridable)
│   │   ├── database.py      # ArangoDB client, collection + index setup
│   │   ├── rate_limit.py    # slowapi helpers, real-IP extraction
│   │   ├── validators.py    # QID format validation
│   │   ├── models/
│   │   │   └── schemas.py   # Pydantic data models (PersonData, EdgeData, …)
│   │   ├── routers/         # One file per API domain (see Endpoints below)
│   │   ├── services/
│   │   │   ├── cache.py     # Redis cache-aside (get/set/flush)
│   │   │   ├── image_cache.py  # Download, resize, store person images
│   │   │   ├── trending.py  # Wikipedia pageview → notable persons
│   │   │   └── etl.py       # Orchestrate provider → ArangoDB pipeline
│   │   └── providers/
│   │       ├── base.py      # Abstract DataProvider interface
│   │       └── wikidata_sparql.py  # Wikidata SPARQL implementation
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── App.vue          # Root SPA component (most logic lives here)
│   │   ├── router.js        # Single route `/` — not used for navigation
│   │   ├── components/      # UI components (GraphCanvas, Trivia games, modals…)
│   │   ├── composables/     # Reusable state (useGraph, useTrivia, useGameMode…)
│   │   ├── utils/
│   │   │   └── cytoscape-config.js  # Cytoscape styling + layout configuration
│   │   ├── styles/
│   │   │   └── main.css     # Design tokens (CSS variables), global styles
│   │   └── i18n/            # Translation strings (EN, IT, FR, DE, ES, PT, NL, PL, SV)
│   └── Dockerfile
├── arango-init/
│   └── init-user.js         # Creates app_user with restricted DB permissions
├── caddy/
│   └── Caddyfile            # Reverse proxy + static file routes
├── docker-compose.yml
└── ARCHITECTURE.md          # This file
```

---

## Database Schema

### Collections

**Document collections:**

| Collection | Contents | Key fields |
|---|---|---|
| `persons` | Public figures | `wikidata_id`, `name_en`, `name_it`, `occupations[]`, `nationalities[]`, `wikidata_statements_count`, `birth_date`, `death_date`, `image_cached_path` |
| `organizations` | Companies, parties, institutions | `wikidata_id`, `name_en`, `name_it`, `type` (organization/position), `founded`, `dissolved`, `sec_cik`, `sec_ticker` |
| `events` | Historical events, conferences | `wikidata_id`, `name_en`, `name_it`, `type`, `date` |
| `game_challenges` | Pre-generated path challenges | `start_qid`, `target_qid`, `shortest_path_length`, `nationality`, `active` |
| `sec_cik_map` | Wikidata QID ↔ SEC CIK mapping | `_key` (QID), `cik`, `ticker`, `name`, `org_qid` |
| `sec_person_candidates` | Scoped (org, person) pairs for SEC name matching | `org_qid`, `person_qid`, `label`, `source` |

**Edge collections:**

| Collection | Connects | Common `type` values |
|---|---|---|
| `relationships` | Person ↔ Person | `spouse`, `parent`, `child`, `sibling` |
| `affiliations` | Person → Organization | `employee`, `member_of`, `position_held`, `director_of`, `officer_of`, `insider_of`, `beneficial_owner_of` |
| `involvements` | Person → Event | `participant`, `significant_event` |
| `org_structure` | Org → Org | `owns`, `owned_by`, `parent_of`, `subsidiary_of`, `holds_shares_of` |

Edge types are free strings — new types from Wikidata or SEC auto-propagate to the frontend without code changes.

**Named graph:** `human_graph` — registers all collections (including `org_structure`) for ArangoDB native traversal.

**Search view:** `search_view` (ArangoSearch)
- `text_en` analyzer on `name_en` (built-in)
- `text_it` analyzer on `name_it` (Italian stemming, accent stripping)
- BM25 ranking + BOOST(5×) for full-token matches

### Key Indexes

- Hash: `name_en`, `name_it` on persons; `name_en` on orgs/events
- Persistent: `wikidata_statements_count`, `birth_date`, `occupations[*]`, `nationalities[*]` on persons; `date` on events; `type` + `active` on challenges

---

## Backend

### Startup & Middleware (`main.py`)

- `RequestSizeLimitMiddleware` — rejects bodies > 1 MB
- `CDNCacheMiddleware` — sets `Cache-Control` per endpoint (guides Cloudflare CDN)
- `CORSMiddleware` — origins from `ALLOWED_ORIGINS` env var
- Rate limiter — slowapi, keyed on `X-Real-IP` (Caddy injects Cloudflare real IP)
- `UTF8JSONResponse` — custom encoder that preserves non-ASCII (no `\uXXXX` escapes)
- Lifespan hook — calls `ensure_*` on ArangoDB at startup (idempotent)

### Configuration (`config.py`)

All thresholds are `pydantic.BaseSettings` — override via environment variables.

Key values:

| Key | Default | Meaning |
|---|---|---|
| `default_traversal_depth` | 2 | Graph neighborhood depth |
| `max_traversal_depth` | 3 | Hard cap on depth |
| `neighborhood_node_limit` | 200 | Max nodes in a subgraph response |
| `notability_min_statements` | 1 | Min Wikidata statements to import |
| `image_min_statements` | 50 | Min statements to cache image |
| `game_challenge_min_statements` | 150 | Min statements for challenge persons |
| `game_challenge_min_degree` | 3 | Min connections for challenge persons |
| `game_challenge_deck_size` | 300 | Pre-generated challenges kept active |
| `sparql_delay_seconds` | 2.0 | Rate limit between SPARQL queries |

### API Endpoints

All endpoints return JSON. Rate limits are per-IP.

#### Persons
| Method | Path | Rate | Description |
|---|---|---|---|
| GET | `/api/persons` | 60/min | Paginated list; filter by `occupation`, sort by various fields |
| GET | `/api/persons/{qid}` | 120/min | Single person by QID |
| GET | `/api/persons/{qid}/connections` | 120/min | All edges for a person; optional `type` filter |
| GET | `/api/persons/{qid}/filings` | 120/min | SEC filing summary: insider transactions, ownership |

#### Organizations
| Method | Path | Rate | Description |
|---|---|---|---|
| GET | `/api/organizations/{qid}/filings` | 120/min | SEC filing summary: directors & officers, insider transactions, institutional holders |

#### Graph
| Method | Path | Rate | Description |
|---|---|---|---|
| GET | `/api/graph/neighborhood/{qid}` | 120/min | BFS subgraph; params: `depth`, `limit`, `edge_type`, `node_types`, `target_qid` |
| GET | `/api/graph/path/{qid1}/{qid2}` | 30/min | Shortest path; symmetric cache key |
| GET | `/api/graph/cluster/{edge_type}` | 60/min | All edges of a type + endpoint vertices (max 200) |
| GET | `/api/graph/occupation/{occupation}` | 30/min | Virtual neighborhood: synthetic occupation node + top persons by notability |

#### Search & Discovery
| Method | Path | Rate | Description |
|---|---|---|---|
| GET | `/api/search?q=` | 60/min | Full-text search via ArangoSearch (BM25 + prefix fallback) |
| GET | `/api/edge-types` | cacheable | All distinct edge type strings across all collections |
| GET | `/api/stats` | cacheable | Node/edge counts per collection |
| GET | `/api/featured?lang=` | 60/min | Trending persons (Wikimedia pageview API + fallback) |

#### Game
| Method | Path | Rate | Description |
|---|---|---|---|
| GET | `/api/game/challenge` | 60/min | Random pre-generated path challenge; optional `nationality` filter |
| GET | `/api/game/challenge/{id}/reveal` | 60/min | Full shortest path for a completed challenge |
| GET | `/api/game/nationalities` | 60/min | Nationalities with ≥3 challenges |

#### Trivia
| Method | Path | Rate | Description |
|---|---|---|---|
| GET | `/api/trivia/round` | 60/min | 10-question round; filter by `category`, `nationality`, `living`, `difficulty` |
| GET | `/api/trivia/nationalities` | cacheable | Nationalities available for filtering |

#### Leaderboard
| Method | Path | Rate | Description |
|---|---|---|---|
| GET | `/api/leaderboard/degree` | 30/min | Top persons by `wikidata_statements_count`; filter by `nationality`, `status`, `category` (grouped or individual occupation) |
| GET | `/api/leaderboard/orgs` | 30/min | Top orgs by affiliated person count; `category` filter incl. `occupazioni` for occupation ranking |
| GET | `/api/leaderboard/orgs/categories` | cacheable | Available org categories for filtering |
| GET | `/api/leaderboard/persons/categories` | cacheable | Available person categories + individual occupations |

#### SEO & Social
| Method | Path | Rate | Description |
|---|---|---|---|
| GET | `/share` | — | OG meta tag page for social share links (redirects to SPA) |
| GET | `/person/{qid_slug}` | — | Server-rendered person page with OG meta for social crawlers |
| GET | `/og/{qid}.png` | cacheable | Dynamic OG image generation (PNG card for social previews) |
| GET | `/sitemap.xml` | cacheable | Sitemap index |
| GET | `/sitemap-{page}.xml` | cacheable | Paginated sub-sitemaps |

#### Feed (Scroll mode)
| Method | Path | Rate | Description |
|---|---|---|---|
| GET | `/api/feed` | 120/min | Paginated connections feed; params: `cursor` (offset), `limit` (1–50), `lang`; 503 if feed not generated. Day-seeded shuffle within score bands. `Cache-Control: public, max-age=300`. |

#### Other
| Method | Path | Rate | Description |
|---|---|---|---|
| GET | `/api/images/{qid}.jpg` | cacheable | Serve optimized local JPEG |
| POST | `/api/report` | 5/hour | User issue report (JSONL logged to `/app/reports.log`) |
| POST | `/api/admin/flush-cache` | auth | Flush Redis (Bearer token) |
| GET | `/api/admin/reports` | auth | Read last N reports (Bearer token) |

### Services

**`services/cache.py`** — Redis cache-aside. Connection pool singleton (max 20, 2s timeout). All failures are silent — application never crashes on Redis outage. `flush_pattern("*")` does FLUSHDB; targeted patterns use SCAN+DELETE.

**`services/image_cache.py`** — Download person images from Wikimedia thumb API. Allowed licenses: `cc0`, `cc-by`, `cc-by-sa`, public domain. Converts to RGB JPEG, max 300px, 80% quality, stores at `/app/images/{qid}.jpg`.

**`services/trending.py`** — Fetches top Wikipedia article titles (Wikimedia pageview API), matches against DB persons, ranks by `wikidata_statements_count`. Falls back to statement-count ranking filtered by nationality if API fails. 24h Redis TTL.

**`services/etl.py`** — Orchestrates provider → DB ingestion: persons → orgs → events → edges → images. Generates dedup key for edges: `{from_id}_{to_id}_{type}_{wikidata_property}`. Skips edges where target node is missing.

### Data Provider (`providers/wikidata_sparql.py`)

Fetches from `https://query.wikidata.org/sparql` with 2s delay between queries.

**Occupation categories fetched:** politician, businessperson, judge, lawyer, journalist, military_person, criminal, actor, singer, composer.

**Wikidata properties → edge types:**

| Property | Edge collection | `type` value |
|---|---|---|
| P26 (spouse) | relationships | `spouse` |
| P22/P25 (parent) | relationships | `parent` |
| P40 (child) | relationships | `child` |
| P3373 (sibling) | relationships | `sibling` |
| P108 (employer) | affiliations | `employee` |
| P39 (position held) | affiliations | `position_held` |
| P102 (party) | affiliations | `member_of` |
| P463 (member of) | affiliations | `member_of` |
| P1344 (participant) | involvements | `participant` |
| P793 (significant event) | involvements | `significant_event` |

### Rate Limiting (`rate_limit.py`)

`get_real_ip()` checks `X-Real-IP` header first (Caddy injects Cloudflare-verified client IP), falls back to `request.client.host` for local dev.

---

## Frontend

### App Structure

`App.vue` is the single root component — it manages all primary state. Vue Router has only one route (`/`); navigation between modes is state-driven, not URL-driven.

### Composables

| File | Responsibility |
|---|---|
| `useApi.js` | Thin wrappers around every `/api/*` endpoint |
| `useGraph.js` | Graph state: nodes, edges, layout mode, selection |
| `useGraphNavigation.js` | Navigation history, back/forward, root-node tracking |
| `useSearch.js` | Debounced full-text search (300ms) |
| `useGameMode.js` | Challenge state: start/target, player path, win detection |
| `useTrivia.js` | Trivia round state, answer history, bookmarks, share link |
| `useLocale.js` | i18n: 9 languages, locale detection, `t()`, `prefName()`, `prefDesc()` |
| `useSettings.js` | Graph display settings (localStorage-backed) |
| `useDossier.js` | Dossier panel state: filings, SEC data for selected node |
| `useScreenshot.js` | Hi-res graph screenshot (PNG export + share) |

### Key Components

| Component | Role |
|---|---|
| `GraphCanvas.vue` | Cytoscape.js wrapper, interaction handlers (hover, click, zoom) |
| `DossierPanel.vue` | Entity detail panel: bio, filings, SEC directors, institutional holders |
| `Leaderboard.vue` | Always-visible sidebar: top persons/orgs by degree |
| `GameHUD.vue` | In-game overlay: start/target, player path breadcrumbs, timer |
| `GameEndModal.vue` | Result screen: score, share button, play again |
| `TriviaPage.vue` | Trivia UI shell + question routing |
| `ChiE.vue` | "Who is this?" question type |
| `VeroFalso.vue` | True/false question type |
| `Intruso.vue` | Spot-the-imposter question type (4 persons, 3 share a criterion) |
| `PersonCard.vue` | Detail card: name, occupations, birth/death, image |
| `SearchBar.vue` | Autocomplete search input |
| `FilterPanel.vue` | Graph traversal filters (depth, edge types, node types) |
| `GraphLegend.vue` | Graph legend (top-right overlay) |
| `OnboardingModal.vue` | First-run guide |
| `TriviaSummary.vue` | Round summary + score breakdown |
| `ExploreEmptyState.vue` | Landing empty state with trending suggestions |

### Graph Rendering (`utils/cytoscape-config.js`)

- **Layout**: `cose-bilkent` for <100 nodes (quality), `cose` for 100+ (performance)
- **Node shapes**: persons → circle, orgs → rounded rectangle, events → hexagon
- **Node colors by occupation**: politician (#2e5fa3), businessperson (#2e7a3e), journalist (#c07820), lawyer (#5a3278), criminal (#b82020)
- **Edge colors**: 16-color palette, pre-seeded for common roles (`founder`, `chief_executive`, etc.), remaining types assigned in order
- **Interaction**: Hover highlights ego subgraph (dims unrelated nodes/edges), click selects

---

## Caching Strategy

| TTL | What |
|---|---|
| 7 days | Person images (immutable once written) |
| Persistent | Feed card pool (`feed:cardmap:v2` + `feed:meta:v2`) — explicitly rebuilt by `scripts/feed/generate_feed.py` after graph refreshes; self-heals after Redis loss |
| 7 days | Leaderboards (persons, orgs, categories), occupation graphs |
| 48h | Shortest paths (symmetric cache key: sorted QID pair) |
| 24h | Stats, edge-types, person detail, featured, game nationalities |
| 1h | Graph neighborhood (unfiltered), search results |
| Private / no-store | Trivia rounds, game challenges, admin endpoints |

Redis is optional — all cache misses fall through to ArangoDB. Cache keys are invalidated by `/api/admin/flush-cache`.

---

## Data Flow: Typical Request

**Example: user loads graph neighborhood for a person**

1. User clicks a node → `useGraph.loadNeighborhood(qid, depth)`
2. `useApi.getNeighborhood()` → `GET /api/graph/neighborhood/{qid}?depth=2`
3. `CDNCacheMiddleware` sets `Cache-Control: public, max-age=3600`
4. Rate limiter checks IP (120/min)
5. Router checks Redis (`neighborhood:{qid}:d2`) — hit: return immediately
6. On miss: ArangoDB BFS traversal, sorted by `wikidata_statements_count` DESC
7. Result written to Redis (1h TTL), returned as UTF8-safe JSON
8. Frontend updates Cytoscape canvas

---

## ETL Pipeline

### Data sync strategy

The graph is kept fresh through three layered jobs, so any single failure is recoverable:

1. **Weekly full rebuild from the Wikidata dump** (`scripts/sync_dump.sh`, Sunday 03:00 UTC) — authoritative refresh of the whole graph from the latest Wikidata N-Triples dump. Runs `load_from_dump.py` inside the backend container.
2. **Daily incremental update** (`backend/scripts/incremental_update.py`, 04:00 UTC) — reads the Wikidata EventStreams feed for the last 24h, resolves changed QIDs via targeted SPARQL, and patches only what moved. Keeps the DB current between weekly dumps.
3. **Weekly ArangoDB backup** (`scripts/backup_db.sh`, Sunday 02:00 UTC, before the dump sync) — `arangodump` of `world_graph_all`, keeps the last 3 snapshots on the host. If a dump sync corrupts the graph, restore from the previous snapshot.
4. **Monthly SEC EDGAR refresh** (`scripts/sec_monthly_refresh.sh`, first Monday 03:00 UTC) — rebuilds `sec_cik_map` from Wikidata P5531, then runs Phase 10 core scripts (Form 4, submissions, 8-K, SC 13D/G). Phase 11 scripts (DEF 14A, 13F, person candidates) are not yet wired in. Rate-limited to 8 rps, polite UA. Scoped to mapped CIKs only.

Cron is installed on the host for the `deploy` user. All three jobs write logs to `backend/logs/` (directory must be deploy-owned for the redirects to work).

### Scripts in `backend/scripts/`

| Script | Purpose | Scheduling |
|---|---|---|
| `load_from_dump.py` | Bulk import from Wikidata RDF dump (bz2) — faster than SPARQL, the canonical full-load path | Weekly via `sync_dump.sh` |
| `incremental_update.py` | EventStreams + targeted SPARQL for the last N hours; exponential backoff on HTTP 429/500/502/503/504 | Daily cron |
| `full_load.py` | Legacy paginated SPARQL full load — superseded by `load_from_dump.py` | Manual / historical |
| `fetch_images.py` | Download/cache images for existing persons, ordered by notability | Manual after bulk loads |
| `retry_images.py` | Re-attempt downloads for entities with `image_url` but no `image_cached_path`; env-configurable (`COLLECTION`, `DELAY`, `LIMIT`) | Manual; idempotent |
| `fetch_all_images.sh` | Bash wrapper chaining fetch + retry passes | Manual |
| `generate_challenges.py` | Pre-generate game challenges (BFS between qualifying persons) | Manual / periodic |
| `fix_occupations.py` | Data cleanup: normalize occupation strings | Manual one-off |
| `fix_unicode_escapes.py` | Data cleanup: unescape stored `\uXXXX` sequences | Manual one-off |
| `warm_leaderboard_cache.py` | Pre-warm Redis caches for all leaderboard + occupation graph queries; covers every exposed filter × sort combination (persons: `degree`, `birth_date`; orgs: `members`, `founded`); skips already-cached keys | Manual after deploy / cache flush, and after any `LEADERBOARD_CACHE_VERSION` bump |

Challenge generation requires persons with `wikidata_statements_count >= 150` and `degree >= 3`, with path length 2–6 hops.

### Host-side scripts in `/scripts/`

| Script | Purpose |
|---|---|
| `sync_dump.sh` | Orchestrates the weekly dump refresh: download latest Wikidata dump → run `load_from_dump.py` inside the backend container |
| `backup_db.sh` | Weekly `arangodump` of `world_graph_all` → host filesystem, retaining last 3 backups |
| `sec_monthly_refresh.sh` | Orchestrates the monthly SEC EDGAR refresh: currently runs Phase 10 core scripts (Phase 11 scripts not yet wired in), logs to `/var/log/humansmap-sec-refresh.log` |
| `lock_origin_to_cloudflare.sh` | Locks inbound 80/443 to Cloudflare's published ranges via the `DOCKER-USER` chain (bypass-proof), so the origin cannot be reached directly |

### SEC EDGAR pipeline (`backend/scripts/sec/`)

Second data provider (US public-company filings) joined to the graph via Wikidata P5531 (CIK). All scripts additive — no schema migrations, no Wikidata-only data touched. See `the project design notes` (Phase 10) and `the project design notes` (Phase 11) for the full design.

#### Phase 10 — core SEC ingestion

| Script | Purpose | Scheduling |
|---|---|---|
| `build_cik_map.py` | SPARQL against Wikidata for P5531 → populates `sec_cik_map` collection, backfills `sec_cik`/`sec_ticker` on orgs and persons | Monthly via `sec_monthly_refresh.sh` |
| `fetch_form4_bulk.py` | Quarterly Form 3/4/5 TSV ingestion → `insider_of` / `officer_of` / `director_of` edges | Monthly (latest quarter only) |
| `fetch_submissions.py` | Per-CIK `submissions.json` → officer/director rosters, historical affiliation edges | Monthly (diffs against `last_synced`) |
| `fetch_material_events.py` | 8-K subset (departures, acquisitions, bankruptcies) → `events` + `involvements` | Monthly |
| `fetch_ownership.py` | SC 13D/G cover pages → `beneficial_owner_of` edges; primary docs only, no exhibits | Monthly |

#### Phase 11 — rosters & institutional holdings

| Script | Purpose | Scheduling |
|---|---|---|
| `build_person_candidates.py` | SPARQL-built (org, person) match pool for SEC name resolution; includes P3320 all-ranks reverse | Monthly, before DEF 14A |
| `widen_cik_map.py` | Extend CIK map from SEC form-index for 13F filers not in Wikidata P5531 | Monthly, after `build_cik_map` |
| `fetch_def14a.py` | DEF 14A proxy statement ingestion → `director_of` / `officer_of` edges via layout-classified HTML parsing | Monthly |
| `def14a_layouts.py` | Layout classifier for DEF 14A HTML (table / mini / prose / span / unknown) | Library, used by `fetch_def14a.py` |
| `def14a_extractors.py` | Per-layout name+role extractors for DEF 14A filings | Library, used by `fetch_def14a.py` |
| `_match.py` | Shared person-name matching (tier-1 exact + global fallback) | Library, used by `fetch_def14a.py` and `fetch_form4_bulk.py` |
| `fetch_13f.py` | 13F-HR institutional holdings bulk loader → `holds_shares_of` Org→Org edges in `org_structure` | Monthly |
| `parse_13f.py` | XML parser for 13F `informationtable.xml` | Library, used by `fetch_13f.py` |
| `resolve_cusip.py` | CUSIP→Org resolver via local name matching; persistent cache at `/data/sec/cusip_to_qid.json` | Library, used by `fetch_13f.py` |
| `write_13f_edges.py` | Edge writer for 13F holdings (quarterly snapshots, idempotent upsert) | Library, used by `fetch_13f.py` |
| `fetch_sc13_bulk.py` | SC 13D/G index-only bulk loader | Monthly |
| `backfill_material_event_labels.py` | One-off: label existing `material_event` rows in affiliations | Manual |

All scripts go through `backend/app/providers/sec_edgar.py` which enforces an 8 rps token bucket, on-disk response cache at `/data/sec/cache/`, and exponential backoff on 429/403.

**Storage:** SEC data lives at `/data/sec/` inside the backend container (configured via `sec_data_dir`), backed by the `sec_data` named volume for persistence across rebuilds.

---

## Security

- **Rate limiting**: per-IP via `X-Real-IP`, all routes
- **Admin auth**: Bearer token, HMAC-SHA256 constant-time comparison
- **QID validation**: regex `Q[0-9]+` on all path parameters (HTTPException 400 otherwise). Synthetic node keys (e.g. `occ_physicist`) are only valid for the `/api/graph/occupation/{occupation}` endpoint; the frontend `DossierPanel` guards against calling real-QID endpoints for synthetic nodes via an `isSynthetic` computed (`!/^Q\d+$/.test(qid)`).
- **Report sanitization**: control chars stripped, message capped at 1000 chars
- **Request size**: 1 MB hard limit (middleware)
- **CORS**: explicit origin allowlist via env var

---

## Infrastructure

Defined in `docker-compose.yml`. All internal ports (8000, 6379, 8529) are not exposed externally — only Caddy ports 80/443 are published.

- **ArangoDB**: 3072 MB memory limit; data on `arango_data` volume
- **Redis**: 512 MB, `allkeys-lru` eviction
- **Frontend**: one-shot build container writing to `frontend_dist` volume; no restart
- **Backend**: 4096 MB memory limit; health check on `/api/health`
- **Caddy**: serves `frontend_dist` (SPA), `image_cache` (images), and `og_cache` (OG cards); proxies `/api/*` + `/share` + `/person/*` + `/og/*` + sitemaps to backend

**Volumes:** `arango_data`, `arango_apps`, `image_cache`, `og_cache`, `sec_data`, `caddy_data`, `caddy_config`, `redis_data`, `reports_data`, `frontend_dist`.

`arango-init/init-user.js` creates `app_user` with read/write on `world_graph_all` only — no `_system` access.
