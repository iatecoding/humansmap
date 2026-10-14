# Deployment Runbook — HumansMap

Target: **humansmap.com** on a small VPS (8 vCPU, 16 GB RAM, 160 GB disk)
Stack: Caddy + FastAPI + ArangoDB + Redis + Vue SPA, all in Docker Compose.

---

## 1. Prerequisites

### Server specs

| Resource | Requirement |
|---|---|
| CPU | 8 vCPU (minimum 4 for ArangoDB + backend workers) |
| RAM | 16 GB (ArangoDB cap: 4096 MB, Redis cache: 1 GB (1280 MB container cap), backend: 1024 MB) |
| Disk | 160 GB (Wikidata dump ~100 GB compressed + ArangoDB data + backups) |
| OS | Ubuntu 22.04 LTS |

### Required software

```bash
# Docker Engine (not Docker Desktop)
curl -fsSL https://get.docker.com | sh
usermod -aG docker deploy   # add deploy user to docker group; re-login

# docker compose v2 (ships with Docker Engine >= 20.10)
docker compose version      # must print v2.x

# lbzip2 — parallel bzip2 for dump decompression
apt-get install -y lbzip2

# wget — for dump downloads
apt-get install -y wget
```

### DNS / Cloudflare

- A record `humansmap.com` → server IP, **Proxied** (orange cloud).
- SSL/TLS mode: **Full** (Caddy uses a self-signed cert; Cloudflare terminates public TLS).
- No port 8529 or 6379 is ever exposed externally — only 80 and 443 via Caddy.

### Lock the origin to Cloudflare

The origin is proxied through Cloudflare, but its public IP still answers on
80/443, so anyone who learns the IP can bypass Cloudflare (WAF, bot management,
caching, DDoS protection) by connecting straight to it. Close that hole:

```bash
scripts/lock_origin_to_cloudflare.sh
```

The script:

- fetches Cloudflare's published [IPv4](https://www.cloudflare.com/ips-v4) /
  [IPv6](https://www.cloudflare.com/ips-v6) ranges,
- rebuilds the `DOCKER-USER` iptables chain so **inbound** traffic to 80/443 on
  the public interface is allowed only from those ranges, and dropped otherwise,
- leaves all other traffic (including **outbound** container calls to Wikidata /
  Wikipedia / SEC) untouched, and
- rolls itself back and exits non-zero if the site stops responding.

It uses the `DOCKER-USER` chain rather than `ufw` because Docker's own iptables
rules are evaluated before `ufw`'s, so `ufw` does not filter Docker-published
ports. It runs without root by using a short-lived privileged helper container
(set `RUN_AS_ROOT=1` if you have root).

iptables state does not survive a reboot and Cloudflare's ranges change, so
schedule it (see Section 5).

---

## 2. Fresh Deployment

### 2.1 Clone and configure

```bash
git clone <repo-url> /path/to/humansmap
cd /path/to/humansmap
cp .env.example .env
$EDITOR .env   # fill in all required variables — see Section 3
```

### 2.2 Prepare host directories

```bash
mkdir -p /data/wikidump /data/backups
```

The dump loader reads from `/data/wikidump/` (mounted read-only into the backend container).
Backups are written to `/data/backups/`.

### 2.3 Start all services

```bash
cd /path/to/humansmap
docker compose up -d --build
```

Build order enforced by `depends_on`:
1. ArangoDB and Redis start first.
2. Backend starts after ArangoDB is running; health check polls `/api/health` every 15 s.
3. Frontend build container runs once (copies `dist/` into the `frontend_dist` volume), then exits.
4. Caddy starts after backend is healthy and frontend build completes.

Check everything is up:

```bash
docker compose ps
curl -s http://localhost/api/health
```

Expected health response: `{"status":"ok"}` or `{"status":"degraded","detail":"..."}`.

### 2.4 Initialize the database user

On the first ArangoDB start (empty data volume), the entrypoint runs
`arango-init/init-user.js`, which:

1. creates the application database (`ARANGO_DB`, default `world_graph_all`) if
   it does not exist,
2. creates `app_user` with the configured password (or updates it), and
3. grants `app_user` read/write on that database only — never on `_system`.

On existing deployments it is a no-op: the entrypoint only processes init
scripts when the data directory is freshly created.

To verify on a fresh install:

```bash
docker compose logs arangodb | grep -iE "Created database|user|Permissions"
```

### 2.5 Load initial data

For a quick look at the product without the full dump, load the small real demo
graph — see [`DEMO.md`](DEMO.md). Otherwise, see Section 4 for the full dump
load procedure.

---

## 3. Environment Variables

Copy `.env.example` to `.env` and fill in all values. The file is read by Docker Compose on every `up`/`restart`.

| Variable | Required | Description |
|---|---|---|
| `ARANGO_PASSWORD` | Yes | ArangoDB `root` user password. Used by `arangodb` service and `backup_db.sh`. |
| `ARANGO_APP_USERNAME` | No | App DB user (default: `app_user`). Must match what `arango-init/init-user.js` creates. |
| `ARANGO_APP_PASSWORD` | Yes | Password for `app_user`. Read by the backend service. |
| `ARANGO_DB` | No | Database the backend uses (default: `world_graph_all`). Must match the name granted in `arango-init/init-user.js`. |
| `ADMIN_TOKEN` | Recommended | Bearer token for `/api/admin/flush-cache` and `/api/admin/reports`. If blank, admin endpoints are disabled. |
| `ALLOWED_ORIGINS` | No | Comma-separated CORS origins (default: `https://humansmap.com,http://localhost:8080`). |

Optional settings that can be added to `.env` to override `config.py` defaults:

| Variable | Default | Description |
|---|---|---|
| `NOTABILITY_MIN_STATEMENTS` | `1` | Min Wikidata statements to import a person |
| `IMAGE_MIN_STATEMENTS` | `50` | Min statements to cache a person's image |
| `GAME_CHALLENGE_MIN_STATEMENTS` | `150` | Min statements for a challenge candidate |
| `SPARQL_DELAY_SECONDS` | `2.0` | Delay between Wikidata SPARQL queries |
| `NEIGHBORHOOD_NODE_LIMIT` | `200` | Max nodes in a graph neighborhood response |
| `DEFAULT_TRAVERSAL_DEPTH` | `2` | Default BFS depth |
| `DUMP_BATCH_SIZE` | `500` | Records per ArangoDB batch insert during dump load |
| `REPORT_EMAIL_TO` | `""` | If set, user reports are emailed to this address |
| `REPORT_SMTP_HOST` | `""` | SMTP host for report emails |
| `SEC_USER_AGENT` | `HumansMap admin@humansmap.com` | Required UA for SEC EDGAR; bad/empty UA → 403 from sec.gov |
| `SEC_DATA_DIR` | `/data/sec` | Cache + retained TSV dir for the SEC pipeline (~1.5 GB after full load) |
| `SEC_REQUESTS_PER_SECOND` | `8.0` | Token-bucket rate for SEC endpoints (max allowed is 10; default leaves headroom) |
| `SEC_FORM4_START_YEAR` | `2010` | Earliest quarterly Form 3/4/5 TSV year to ingest |

---

## 4. Initial Data Load

The initial load uses the Wikidata RDF dump (faster than SPARQL for bulk import). It is a two-pass process.

### 4.1 Download the dump

The dump is ~100 GB compressed. Use wget's `-N` flag so re-runs only download if the remote file is newer:

```bash
mkdir -p /data/wikidump
wget -N -P /data/wikidump \
  https://dumps.wikimedia.org/wikidatawiki/entities/latest-truthy.nt.bz2
```

This can take several hours depending on bandwidth.

### 4.2 Run pass 1 — identification

Pass 1 scans the dump and identifies all public figures and their referenced QIDs. It writes candidate lists to `/data/wikidump/` on the host.

```bash
lbzip2 -dc /data/wikidump/latest-truthy.nt.bz2 | \
  docker compose exec -T -u appuser backend \
    python /app/scripts/load_from_dump.py --pass1
```

Pass 1 is CPU-bound and takes 1–3 hours.

### 4.3 Run pass 2 — extraction

Pass 2 re-scans the dump and loads identified entities into ArangoDB.

```bash
lbzip2 -dc /data/wikidump/latest-truthy.nt.bz2 | \
  docker compose exec -T -u appuser backend \
    python /app/scripts/load_from_dump.py --pass2
```

Pass 2 is I/O- and DB-bound and takes 2–6 hours.

### 4.4 Flush Redis and verify

```bash
docker compose exec redis redis-cli FLUSHDB
curl -s http://localhost/api/stats | python3 -m json.tool
```

Stats should show non-zero node and edge counts.

### 4.5 Pre-generate game challenges (optional but recommended)

```bash
docker compose exec -u appuser backend \
  python /app/scripts/generate_challenges.py
```

Requires persons with `wikidata_statements_count >= 150` and `degree >= 3`. Generates up to 300 challenges.

### 4.6 Cache images

```bash
docker compose exec -u appuser backend \
  python /app/scripts/fetch_images.py
```

Downloads and optimizes JPEG thumbnails for all qualifying persons. Stores at `/app/images/{qid}.jpg` (backed by the `image_cache` Docker volume).

---

## 5. Cron Jobs

Install these as root (or the `deploy` user with Docker access) via `crontab -e`:

```cron
# DB backup — Sunday 02:00 UTC (before sync)
0 2 * * 0 /path/to/humansmap/scripts/backup_db.sh

# Wikidata dump sync — Sunday 03:00 UTC
0 3 * * 0 /path/to/humansmap/scripts/sync_dump.sh

# Incremental update — daily 04:00 UTC
0 4 * * * docker compose -f /path/to/humansmap/docker-compose.yml exec -T backend python /app/scripts/incremental_update.py >> /var/log/incremental_update.log 2>&1

# SEC EDGAR monthly refresh — first Monday of the month, 03:00 UTC
0 3 1-7 * 1 /path/to/humansmap/scripts/sec_monthly_refresh.sh

# Origin firewall — re-apply after reboot and refresh weekly (iptables does not persist)
@reboot sleep 30 && /path/to/humansmap/scripts/lock_origin_to_cloudflare.sh >> /path/to/humansmap/backend/logs/lock-origin.log 2>&1
17 4 * * 1 /path/to/humansmap/scripts/lock_origin_to_cloudflare.sh >> /path/to/humansmap/backend/logs/lock-origin.log 2>&1
```

### What each job does

**`backup_db.sh`** — Runs `arangodump` inside the `arangodb` container for the `world_graph_all` database, copies the dump to `/data/backups/db_backup_YYYYMMDD/`, then prunes backups older than the last 3. Reads `ARANGO_PASSWORD` from the environment or from `.env`.

**`sync_dump.sh`** — Downloads the latest Wikidata dump with `wget -N` (skips if unchanged), runs both loader passes directly against the dump mounted in the backend container, then flushes Redis and rebuilds the connections feed. Skips pass 1+2 if the dump file hasn't changed since the last successful sync (tracked by `/data/wikidump/.last_sync_timestamp`).

**`incremental_update.py`** — Collects changed QIDs from Wikidata EventStreams for the past 24 h, fetches updated data via SPARQL, upserts into ArangoDB, and invalidates only the affected Redis cache keys. State persisted to `/data/wikidump/.last_incremental_timestamp`. Supports `--since`, `--window`, and `--dry-run` flags for manual backfills.

**`sec_monthly_refresh.sh`** — Runs the SEC EDGAR ingestion pipeline. Currently executes the Phase 10 core scripts: `build_cik_map.py` → `fetch_form4_bulk.py` → `fetch_submissions.py` → `fetch_material_events.py` → `fetch_ownership.py`. Phase 11 scripts (`build_person_candidates.py`, `fetch_def14a.py`, `fetch_13f.py`, `fetch_sc13_bulk.py`) are not yet wired into the monthly cron — they were validated manually and need integration when the full SEC pipeline is production-ready. Scoped to CIKs already mapped to a Wikidata entity in `sec_cik_map`, so volume is bounded regardless of SEC's total filing flow.

### Log files

| Job | Log |
|---|---|
| backup_db.sh | `/var/log/humansmap-backup.log` |
| sync_dump.sh | `/var/log/humansmap-sync.log` |
| incremental_update.py | `/var/log/incremental_update.log` |
| sec_monthly_refresh.sh | `/var/log/humansmap-sec-refresh.log` |

---

## 6. Secret Rotation

### 6.1 Rotate `ARANGO_PASSWORD` (root)

1. Update `/path/to/humansmap/.env` with the new password.
2. Change the password inside ArangoDB:

   ```bash
   docker compose exec arangodb arangosh \
     --server.username root \
     --server.password OLD_PASSWORD \
     --javascript.execute-string \
     "require('@arangodb/users').update('root', 'NEW_PASSWORD');"
   ```

3. Restart the backend (it reads the password at startup via the compose env):

   ```bash
   docker compose restart backend
   ```

4. Verify: `curl -s http://localhost/api/health`

### 6.2 Rotate `ARANGO_APP_PASSWORD` (app_user)

1. Update `ARANGO_APP_PASSWORD` in `.env`.
2. Change the password in ArangoDB using root credentials:

   ```bash
   docker compose exec arangodb arangosh \
     --server.username root \
     --server.password ARANGO_ROOT_PASSWORD \
     --javascript.execute-string \
     "require('@arangodb/users').update('app_user', 'NEW_APP_PASSWORD');"
   ```

3. Restart the backend:

   ```bash
   docker compose restart backend
   ```

### 6.3 Rotate `ADMIN_TOKEN`

1. Generate a new token: `openssl rand -hex 32`
2. Update `ADMIN_TOKEN` in `.env`.
3. Restart the backend: `docker compose restart backend`
4. Update any scripts or monitoring that use the old token.

---

## 7. DB Backup and Restore

### Manual backup

```bash
/path/to/humansmap/scripts/backup_db.sh
```

Output: `/data/backups/db_backup_YYYYMMDD/`

### Manual restore

To restore from a backup (e.g. after a failed dump load):

```bash
BACKUP_DIR=/data/backups/db_backup_20240114

# Copy the backup into the running arangodb container
CONTAINER_ID=$(docker compose -f /path/to/humansmap/docker-compose.yml ps -q arangodb)
docker cp "$BACKUP_DIR/." "${CONTAINER_ID}:/tmp/restore/"

# Run arangorestore inside the container
docker compose -f /path/to/humansmap/docker-compose.yml exec -T arangodb \
  arangorestore \
    --server.database world_graph_all \
    --server.username root \
    --server.password "$ARANGO_PASSWORD" \
    --input-directory /tmp/restore \
    --overwrite true

# Clean up temp files
docker compose -f /path/to/humansmap/docker-compose.yml exec -T arangodb \
  rm -rf /tmp/restore

# Flush Redis (stale after restore)
docker compose -f /path/to/humansmap/docker-compose.yml exec redis redis-cli FLUSHDB
```

Verify: `curl -s http://localhost/api/stats`

---

## 8. Updating the Application

### Backend or frontend code change

```bash
cd /path/to/humansmap
git pull
./scripts/deploy_release.sh
```

The release script stamps the same Git identity into the backend and frontend,
rebuilds both artifacts, reloads Caddy, and fails unless the public health and
`/release.json` endpoints match. It also checks that a substantive organization
page is not accidentally shipped with `noindex`.

The script refuses to recreate the backend while the full image-import tmux
session is active. Finish or deliberately stop that job first; use
`ALLOW_ACTIVE_LOADERS=1` only when interrupting it is intentional.

### Config-only change (`.env` variable)

```bash
docker compose restart backend
```

### Caddy config change (`caddy/Caddyfile`)

```bash
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
# or, if the above fails:
docker compose restart caddy
```

### Full stack restart

```bash
docker compose down && docker compose up -d
```

This keeps all named volumes (`arango_data`, `image_cache`, etc.) intact.

---

## 9. Monitoring

### Health endpoint

```
GET /api/health
```

| Response | Meaning |
|---|---|
| `{"status":"ok"}` | All systems nominal |
| `{"status":"degraded","detail":"..."}` | ArangoDB or Redis connectivity issue; the app may still serve cached responses |

The backend health check runs every 15 s inside Docker. Caddy will not start until the backend is healthy.

### Log locations

| What | Where |
|---|---|
| Caddy access + error logs | `docker compose logs caddy` (JSON format, stdout) |
| Backend application logs | `docker compose logs backend` |
| ArangoDB logs | `docker compose logs arangodb` |
| User reports | `docker compose exec backend cat /app/reports.log` |
| Cron: backup | `/var/log/humansmap-backup.log` |
| Cron: sync | `/var/log/humansmap-sync.log` |
| Cron: incremental | `/var/log/incremental_update.log` |

### Quick status check

```bash
# Service states
docker compose -f /path/to/humansmap/docker-compose.yml ps

# Resource usage (CPU, RAM)
docker stats --no-stream

# Disk usage
df -h /data
du -sh /data/wikidump /data/backups
```

---

## 10. Troubleshooting

### ArangoDB OOM (killed or container restarting)

**Symptom:** `docker compose ps` shows `arangodb` restarting; logs contain `Killed` or OOM.

**Cause:** The memory limit in `docker-compose.yml` is 3072 MB. Large queries or index builds can exceed this.

**Fix:**
1. Check: `docker compose logs arangodb | tail -50`
2. Reduce query load temporarily: `docker compose restart backend` (resets rate limiter state).
3. If persistent, raise the limit in `docker-compose.yml` under `arangodb.deploy.resources.limits.memory` and restart.
4. Long-term: add a swap file (`fallocate -l 4G /swapfile; mkswap /swapfile; swapon /swapfile`).

### Redis memory full (evictions causing incorrect responses)

**Symptom:** `/api/stats` shows stale counts; featured persons are wrong; slow search.

**Cause:** Redis is capped at 1 GB with `allkeys-lru` eviction. Under heavy load all keys may be evicted.

**Fix:**
1. Check memory: `docker compose exec redis redis-cli INFO memory | grep used_memory_human`
2. Flush and let it warm up: `docker compose exec redis redis-cli FLUSHDB`
3. If routinely full, raise `--maxmemory` in the `redis` service command in `docker-compose.yml`.

### SPARQL rate limits (incremental update fails or is slow)

**Symptom:** `/var/log/incremental_update.log` shows 429 errors or timeouts from `query.wikidata.org`.

**Fix:**
1. The script already uses a 2 s delay and exponential backoff. Wait and re-run.
2. For manual backfills, use a narrower window: `--window 2` (hours).
3. Do not run the incremental updater and a full dump load simultaneously.

### Disk space exhausted

**Symptom:** `df -h /data` shows 100%; Docker services may fail to write logs or data.

**Common culprits:**

| Path | What it holds |
|---|---|
| `/data/wikidump/` | Wikidata dump (~100 GB compressed) + pass1 intermediate files |
| `/data/backups/` | DB backups (3 retained by default) |
| Docker volumes | `arango_data`, `image_cache` |
| Docker logs | JSON log files (capped at 10 MB × 3 per service) |

**Fix:**
1. Prune old backups: `ls -lh /data/backups/` and `rm -rf /data/backups/db_backup_OLD`
2. Remove pass1 intermediate files if the load is complete: `rm -f /data/wikidump/*.json`
3. Prune unused Docker images: `docker image prune -f`
4. Check Docker volume sizes: `docker system df -v`

### Backend not starting (ArangoDB connection refused)

**Symptom:** `docker compose ps` shows backend restarting; logs show `ConnectionRefusedError` or `ArangoConnectionError`.

**Fix:** ArangoDB can take 30–60 s to initialize on first boot. The backend health check has a 20 s start period — wait and check again. If it persists:

```bash
docker compose logs arangodb | tail -30
docker compose restart arangodb
```

### Frontend shows blank page or 404 on refresh

**Symptom:** Direct URL access (e.g. `/share`) returns 404.

**Cause:** The Caddy `try_files {path} /index.html` rule handles SPA routing. If Caddy is misconfigured or the `frontend_dist` volume is empty (frontend build failed), this breaks.

**Fix:**
```bash
docker compose logs frontend   # check for build errors
docker compose up -d --build frontend  # rebuild
docker compose restart caddy
```

### Cloudflare returning 523 (origin unreachable)

**Symptom:** Public users see a Cloudflare error page; direct server access works.

**Cause:** Caddy is not listening on 443 or the server IP changed.

**Fix:**
1. `docker compose ps caddy` — should show ports `0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp`.
2. Verify DNS A record in Cloudflare dashboard points to the correct server IP.
3. `docker compose restart caddy`
