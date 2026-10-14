#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

if [[ "${ALLOW_DIRTY_RELEASE:-0}" != "1" ]]; then
    dirty="$(git status --porcelain --untracked-files=normal)"
    if [[ -n "$dirty" ]]; then
        echo "Refusing to deploy a worktree that differs from HEAD:" >&2
        echo "$dirty" >&2
        echo "Commit the release first, or explicitly set ALLOW_DIRTY_RELEASE=1." >&2
        exit 2
    fi
fi

if tmux has-session -t humansmap-images-full 2>/dev/null && [[ "${ALLOW_ACTIVE_LOADERS:-0}" != "1" ]]; then
    echo "Refusing to recreate backend while humansmap-images-full is active." >&2
    echo "Stop it deliberately, or rerun with ALLOW_ACTIVE_LOADERS=1 after accepting interruption." >&2
    exit 3
fi

APP_BUILD_SHA="${APP_BUILD_SHA:-$(git rev-parse --short=12 HEAD)}"
export APP_BUILD_SHA

echo "Deploying HumansMap release $APP_BUILD_SHA"
docker compose build backend frontend
docker compose up -d --no-deps backend

backend_id="$(docker compose ps -q backend)"
for attempt in $(seq 1 60); do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$backend_id")"
    if [ "$health" = "healthy" ]; then
        break
    fi
    if [ "$attempt" -eq 60 ]; then
        echo "Backend did not become healthy after 120 seconds (status: $health)" >&2
        exit 1
    fi
    sleep 2
done

docker compose run --rm --no-deps frontend
# Recreate: bind-mounted files may have a new inode after git checkout/patch.
docker compose up -d --no-deps --force-recreate caddy
python3 scripts/verify_release.py

org_headers="$(mktemp)"
org_body="$(mktemp)"
sitemap_headers="$(mktemp)"
sitemap_body="$(mktemp)"
trap 'rm -f "$org_headers" "$org_body" "$sitemap_headers" "$sitemap_body"' EXIT
curl -fsSL -D "$org_headers" -o "$org_body" \
    -H 'Cache-Control: no-cache' https://humansmap.com/organization/Q95
if grep -qi '^x-robots-tag: *noindex' "$org_headers" || \
   grep -qi '<meta[^>]*name="robots"[^>]*noindex' "$org_body"; then
    echo "SEO canary failed: substantive organization Q95 is still noindex" >&2
    exit 1
fi

curl -fsSL -D "$sitemap_headers" -o "$sitemap_body" \
    -H 'Cache-Control: no-cache' https://humansmap.com/sitemap.xml
if ! grep -qi '^content-type: *application/xml' "$sitemap_headers" || \
   [[ "$(head -c 5 "$sitemap_body")" != "<?xml" ]]; then
    echo "SEO canary failed: /sitemap.xml is not XML" >&2
    exit 1
fi

trailing_status="$(curl -sS -o /dev/null -w '%{http_code}' \
    https://humansmap.com/sitemap.xml/)"
if [[ "$trailing_status" != "308" ]]; then
    echo "SEO canary failed: trailing-slash sitemap returned $trailing_status, expected 308" >&2
    exit 1
fi

echo "Release and organization-indexability checks passed."
