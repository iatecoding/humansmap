# Contributing to HumansMap

Thanks for helping improve HumansMap. The project combines a Vue frontend, a
FastAPI API, ArangoDB, Redis, and data pipelines for Wikidata and SEC EDGAR.

## Before you start

- Search existing issues before opening a new one.
- For substantial features, schema changes, or new data sources, open an issue
  first so the approach and data/licensing implications can be discussed.
- Keep pull requests focused. Separate cleanup from behavior changes when
  practical.
- Never commit credentials, local `.env` files, database dumps, cached images,
  filing caches, or other generated datasets.

## Local development

Copy the example configuration and start the development stack:

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.override.yml.dev up
```

The full graph requires a populated ArangoDB database. To see the product
working without the ~100 GB Wikidata dump, load the small real demo graph
instead — see [`docs/DEMO.md`](docs/DEMO.md):

```bash
docker compose exec backend python scripts/seed_demo_data.py
docker compose exec redis redis-cli FLUSHDB
```

Otherwise, see [`README.md`](README.md) and
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the full data and deployment
requirements.

## Tests

Backend tests require the development dependencies plus test ArangoDB and Redis
instances. Follow [`.github/workflows/ci.yml`](.github/workflows/ci.yml) for
the canonical service and environment configuration. With those available:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
PYTHONPATH=backend pytest backend/tests
```

Run frontend tests and a production build locally:

```bash
cd frontend
npm ci
npm test
npm run build
```

Add or update tests for behavior changes. If a check cannot be run locally,
state that clearly in the pull request.

## Data contributions

HumansMap is an exploration tool built from public sources; it is not an
authoritative identity or due-diligence database. Data fixes should include a
link to the relevant Wikidata entity, SEC filing, or another primary source.
Do not submit private personal data or unsupported allegations.

Respect source terms and rate limits. In particular, SEC ingestion code must
retain an identifying user agent and stay within the SEC fair-access limits.

## Pull requests

By submitting a contribution, you agree that it may be distributed under the
project's [MIT License](LICENSE). In the pull request, describe:

- what changed and why;
- how it was tested;
- any schema, migration, environment, or operational impact;
- the provenance and license of new data or media assets.
