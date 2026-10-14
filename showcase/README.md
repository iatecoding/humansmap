# Static showcase (GitHub Pages)

This directory is a **reduced, backend-free preview** of HumansMap. It exists so
the project can be shown on GitHub Pages, which serves static files only.

It is **not the real application**, and that distinction is stated prominently
in the UI. The live site is a full-stack app (FastAPI + ArangoDB + Redis + a Vue
single-page app) covering millions of entities, with live SEC EDGAR filings,
full-depth graph traversal, leaderboards, a path-finding game, a trivia game, a
scrollable connection feed and nine languages.

The showcase re-implements only the core browsing experience — search, person /
organization / event pages, and a Cytoscape graph — in plain JavaScript, from a
small JSON file: [`../backend/data/demo/demo_graph.json`](../backend/data/demo/demo_graph.json)
(a real Wikidata slice, CC0).

## What it can and cannot do

| | Showcase (this) | Full app |
|---|---|---|
| Entities | 54 people, 28 orgs, 1 event | ~8 million nodes |
| Search | client-side name match | ArangoSearch, BM25 + language analyzers |
| Graph | whole demo dataset | bounded BFS neighborhoods, depth 1–3 |
| Person / org pages | yes | yes, plus SEC filing dossiers |
| Games | – | path-finding + trivia |
| Feed | – | generated connection cards |
| Languages | – | 9 |
| Backend | none (static JSON) | FastAPI + ArangoDB + Redis |

## Files

- `index.html`, `showcase.css`, `showcase.js` — the app (no build step needed).
- Vendor JS (Cytoscape, cose-bilkent) is **not committed**; it is copied from the
  pinned `frontend/node_modules` at build time.

## Build and preview locally

```bash
cd frontend && npm ci      # provides cytoscape + cose-bilkent
cd ..
python scripts/build_showcase.py _site
python -m http.server -d _site 8080
# open http://localhost:8080
```

`scripts/build_showcase.py` copies the showcase, the demo JSON, the vendored
libraries and two live-site screenshots into `_site/` (about 1.2 MB) and adds
`.nojekyll`.

## Deploy

[`.github/workflows/pages.yml`](../.github/workflows/pages.yml) builds `_site/`
and deploys it to GitHub Pages on every push to `main` (or `master`), and via
manual dispatch.

One-time setup: in the repository, **Settings → Pages → Build and deployment →
Source: "GitHub Actions"**.

The published size (~1.2 MB) and repository size stay far below GitHub Pages
limits (1 GB site, 100 GB/month soft bandwidth).
