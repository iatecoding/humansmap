#!/usr/bin/env python3
"""Assemble the static GitHub Pages showcase into an output directory.

The showcase is a backend-free preview of HumansMap: search, person /
organization / event pages and a Cytoscape graph, all running in the browser
from a small JSON file (`backend/data/demo/demo_graph.json`).

This script collects the static assets so the result can be served by GitHub
Pages (which only serves static files):

    showcase/*               -> OUT/
    backend/data/demo/*      -> OUT/data/
    frontend node_modules    -> OUT/vendor/   (cytoscape + cose-bilkent)
    docs/media/screens/*.png -> OUT/media/    (two live-site screenshots)

Usage:
    python scripts/build_showcase.py [OUT]      # default: _site
"""

from __future__ import annotations

import pathlib
import shutil
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

# (source, destination relative to OUT)
STATIC = [
    ("showcase/index.html", "index.html"),
    ("showcase/showcase.css", "showcase.css"),
    ("showcase/showcase.js", "showcase.js"),
    ("showcase/favicon.svg", "favicon.svg"),
    ("backend/data/demo/demo_graph.json", "data/demo_graph.json"),
    # Vendored browser libraries, copied from the pinned frontend dependency
    # tree so the showcase does not rely on a CDN. Load order matters:
    # layout-base → cose-base → cytoscape-cose-bilkent.
    ("frontend/node_modules/cytoscape/dist/cytoscape.min.js", "vendor/cytoscape.min.js"),
    ("frontend/node_modules/layout-base/layout-base.js", "vendor/layout-base.js"),
    ("frontend/node_modules/cose-base/cose-base.js", "vendor/cose-base.js"),
    ("frontend/node_modules/cytoscape-cose-bilkent/cytoscape-cose-bilkent.js",
     "vendor/cytoscape-cose-bilkent.js"),
    # Screenshots of the live site, used to contrast it with this static demo.
    ("docs/media/screens/explore-graph.png", "media/explore-graph.png"),
    ("docs/media/screens/person-card.png", "media/person-card.png"),
]


def build(out: pathlib.Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    missing: list[str] = []
    for src_rel, dst_rel in STATIC:
        src = REPO / src_rel
        if not src.exists():
            missing.append(src_rel)
            continue
        dst = out / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    if missing:
        print("error: missing showcase inputs:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print("hint: run `npm ci` in frontend/ first (the vendor JS comes from "
              "node_modules)", file=sys.stderr)
        raise SystemExit(1)

    # GitHub Pages runs Jekyll by default, which ignores files/dirs starting
    # with an underscore. We have none today, but .nojekyll keeps the pipeline
    # predictable and serves the tree verbatim.
    (out / ".nojekyll").write_text("", encoding="utf-8")

    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"built {out}  ({total / 1024:.0f} KB, "
          f"{sum(1 for f in out.rglob('*') if f.is_file())} files)")


def main() -> None:
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("_site")
    build(out if out.is_absolute() else (REPO / out))


if __name__ == "__main__":
    main()
