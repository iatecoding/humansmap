# Running the demo dataset

HumansMap is normally built from a ~100 GB Wikidata dump plus the SEC EDGAR
pipeline. That is the right way to run it for real, and it is not practical if
you just want to see the product working.

To make the repository self-contained we ship a **small real slice of the
graph**: `backend/data/demo/demo_graph.json`. It contains ~50 real public
figures (Ada Lovelace, Albert Einstein, Beyoncé, Steve Jobs, Cleopatra, …),
their organizations and events, and the real relations between them — all
sourced from Wikidata (CC0). It is ~56 KB and gives you:

- a working **Explore** graph with several connected clusters,
- **Leaderboards** and **Search** with real names and occupations,
- a **Play** deck of nine pre-computed path challenges,
- **Trivia** rounds (the dataset has 24 US figures, enough to exercise the
  nationality filter).

It is a demo, not the product: a few dozen nodes instead of ~8 million.

## Quick start

```bash
cp .env.example .env          # set ARANGO_PASSWORD and ARANGO_APP_PASSWORD
docker compose up -d --build  # build and start the stack (frontend build runs once)
```

On first boot ArangoDB creates the application database
(`world_graph_all` by default) and the restricted `app_user` automatically via
`arango-init/init-user.js`. Then load the demo graph and clear the cache:

```bash
docker compose exec backend python scripts/seed_demo_data.py
docker compose exec redis redis-cli FLUSHDB
```

Open <http://localhost> (or the host you mapped Caddy to) and you should see a
populated graph, leaderboards and a playable game.

> The loader uses the backend image, so rebuild the backend after changing the
> script or the JSON: `docker compose up -d --build backend`.

## What gets loaded

| Collection | Count | Examples |
|---|---|---|
| `persons` | 54 | Ada Lovelace, Albert Einstein, Beyoncé, Elon Musk, Frida Kahlo |
| `organizations` | 28 | Democratic Party, Republican Party, University of Cambridge, UNICEF |
| `events` | 1 | French Revolution |
| `relationships` | 12 | parent, child, sibling, spouse, … |
| `affiliations` | 57 | member_of, educated_at, position_held, employee, … |
| `involvements` | 2 | participant |
| `org_structure` | 2 | owns, holds_shares_of |
| `game_challenges` | 9 | shortest paths computed at load time |

## Options

```bash
# Point at a different graph file
docker compose exec backend python scripts/seed_demo_data.py --file /app/data/demo/demo_graph.json

# Skip challenge generation
docker compose exec backend python scripts/seed_demo_data.py --skip-challenges
```

The loader is idempotent: it overwrites the demo documents on each run. It never
touches data outside the demo QIDs it inserts, so it is safe to run against a
database that already has other content.

## Provenance and licence

The demo graph is a curated slice of the live graph, which is itself built from
Wikidata. Wikidata structured data is released under **CC0 1.0**, so the demo
data may be reused freely. The QIDs in the file are the real Wikidata
identifiers, so any node can be checked against
`https://www.wikidata.org/wiki/<QID>`.

## Going further: the real dataset

When you are ready to run the full product, see the "Running with full data"
section of the [README](../README.md) and [`DEPLOYMENT.md`](DEPLOYMENT.md).
