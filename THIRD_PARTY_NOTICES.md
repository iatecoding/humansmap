# Third-party notices

HumansMap's own source code is licensed under the MIT License — see
[`LICENSE`](LICENSE). It bundles or depends on the third-party assets below,
which remain under their own terms.

## Fonts

| Asset | Where | License |
|---|---|---|
| **Inter** | `backend/fonts/Inter-Regular.ttf`, `Inter-Bold.ttf` | SIL Open Font License 1.1 |
| **Playfair Display** | served via Google Fonts to the web UI | SIL Open Font License 1.1 |
| **DM Sans** | served via Google Fonts to the web UI | SIL Open Font License 1.1 |

Inter is bundled to render Open Graph preview images server-side. Playfair
Display and DM Sans are loaded from Google Fonts by the browser.

## Data sources

| Source | License / terms |
|---|---|
| **Wikidata** | structured data under CC0 1.0 |
| **Wikimedia Commons** images | per-image licenses — only CC0, public-domain, CC BY and CC BY-SA images are cached, with attribution stored and displayed per image |
| **SEC EDGAR** filings | public-domain U.S. government works, used within [SEC fair-access](https://www.sec.gov/os/webmaster-faq#developers) limits |

## Map geometry

The world map used by the marketing graphics (`docs/marketing/map/world-map.svg`)
is derived from "BlankMap-World.svg" (Canuckguy and others, Wikimedia Commons),
released into the public domain.

## Software dependencies

Runtime dependencies are listed in [`frontend/package.json`](frontend/package.json),
[`backend/requirements.txt`](backend/requirements.txt) and
[`scripts/marketing/package.json`](scripts/marketing/package.json). These are
permissively licensed (MIT, BSD, Apache-2.0, ISC). The project deliberately
avoids GPL/AGPL dependencies.
