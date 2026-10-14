#!/usr/bin/env python3
"""Marketing "degrees of separation" wall — read-only, host-side.

Fetches the shortest documented chain between pairs of surprising, famous
people and assembles a shareable card. It never touches ArangoDB directly: it
uses the public, cached, rate-limited endpoint

    GET https://humansmap.com/api/graph/path/{qid1}/{qid2}

which returns ``{vertices, edges, found, length}``. Names/QIDs are resolved
against the public search endpoint ``GET /api/search?q=<name>`` rather than
guessed. Every raw response is cached under ``docs/marketing/paths/raw/`` so a
re-render needs no network at all.

This script is deliberately outside the container pipeline: it runs on the
host, talks only to the public API, and writes JSON + HTML + cached portraits
under ``docs/marketing/paths/``.

Usage (from the repo root):

    python3 backend/scripts/marketing_paths.py             # fetch (cached) + build
    python3 backend/scripts/marketing_paths.py --refresh   # force new network calls
    python3 backend/scripts/marketing_paths.py --from-json # rebuild HTML from paths.json
    python3 backend/scripts/marketing_paths.py --no-images # skip portrait downloads

The path endpoint is limited to 30 requests/minute, so calls are spaced ~3s.
Twelve-to-sixteen pairs are chosen, verified with search, and any pair that
404s, times out or comes back ``found: false`` is skipped and recorded.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

API = "https://humansmap.com"
USER_AGENT = "humansmap-marketing/1.0 (+https://humansmap.com; marketing asset)"
PATH_SLEEP = 3.0      # seconds between graph/path calls (30 req/min limit)
SEARCH_SLEEP = 1.0    # seconds between search calls

try:  # piped over stdin (``python -``) __file__ is "<stdin>", which has no parents
    REPO_ROOT = Path(__file__).resolve().parents[2]
except (NameError, OSError, IndexError):  # pragma: no cover - stdin invocation
    REPO_ROOT = Path.cwd()
DEFAULT_OUT = REPO_ROOT / "docs" / "marketing" / "paths"

# ---------------------------------------------------------------------------
# The wall: 15 verified famous names across deliberately unrelated domains.
# ``qa``/``qb`` are the expected QIDs — the search endpoint is still used to
# confirm the name resolves to that exact person (the script never trusts a
# hard-coded QID without checking it).
# ---------------------------------------------------------------------------
PAIRS = [
    {"slug": "einstein-jordan", "a": "Albert Einstein", "b": "Michael Jordan",
     "qa": "Q937", "qb": "Q41421", "topic": "Physics \u00d7 Basketball"},
    {"slug": "kahlo-musk", "a": "Frida Kahlo", "b": "Elon Musk",
     "qa": "Q5588", "qb": "Q317521", "topic": "Painting \u00d7 Spaceflight"},
    {"slug": "plato-madonna", "a": "Plato", "b": "Madonna",
     "qa": "Q859", "qb": "Q1744", "topic": "Philosophy \u00d7 Pop"},
    {"slug": "curie-monroe", "a": "Marie Curie", "b": "Marilyn Monroe",
     "qa": "Q7186", "qb": "Q4616", "topic": "Chemistry \u00d7 Cinema"},
    {"slug": "marley-elizabeth", "a": "Bob Marley", "b": "Elizabeth II",
     "qa": "Q409", "qb": "Q9682", "topic": "Reggae \u00d7 Monarchy"},
    {"slug": "leebruce-churchill", "a": "Bruce Lee", "b": "Winston Churchill",
     "qa": "Q16397", "qb": "Q8016", "topic": "Martial arts \u00d7 Statesmanship"},
    {"slug": "vangogh-jobs", "a": "Vincent van Gogh", "b": "Steve Jobs",
     "qa": "Q5582", "qb": "Q19837", "topic": "Post-Impressionism \u00d7 Computing"},
    {"slug": "cleopatra-serena", "a": "Cleopatra", "b": "Serena Williams",
     "qa": "Q635", "qb": "Q11459", "topic": "Antiquity \u00d7 Tennis"},
    {"slug": "picasso-mjackson", "a": "Pablo Picasso", "b": "Michael Jackson",
     "qa": "Q5593", "qb": "Q2831", "topic": "Cubism \u00d7 Pop"},
    {"slug": "tesla-beyonce", "a": "Nikola Tesla", "b": "Beyonc\u00e9",
     "qa": "Q9036", "qb": "Q36153", "topic": "Invention \u00d7 Pop"},
    {"slug": "mozart-ali", "a": "Wolfgang Amadeus Mozart", "b": "Muhammad Ali",
     "qa": "Q254", "qb": "Q36107", "topic": "Classical \u00d7 Boxing"},
    {"slug": "suntzu-ronaldo", "a": "Sun Tzu", "b": "Cristiano Ronaldo",
     "qa": "Q37151", "qb": "Q11571", "topic": "Strategy \u00d7 Football"},
    {"slug": "genghis-gaga", "a": "Genghis Khan", "b": "Lady Gaga",
     "qa": "Q720", "qb": "Q19848", "topic": "Empire \u00d7 Pop"},
    {"slug": "lovelace-kanye", "a": "Ada Lovelace", "b": "Kanye West",
     "qa": "Q7259", "qb": "Q15935", "topic": "Computing \u00d7 Hip-hop"},
    {"slug": "beethoven-napoleon", "a": "Ludwig van Beethoven", "b": "Napoleon",
     "qa": "Q255", "qb": "Q517", "topic": "Music \u00d7 Empire"},
]

KIND_LABELS = {
    "persons": "person",
    "organizations": "organization",
    "places": "place",
    "events": "event",
}

# Wikidata models some generic roles/positions as organisations. Flag the ones
# that show up as intermediate nodes so the card can describe them honestly.
POSITION_QIDS = {"Q140686", "Q116", "Q181888", "Q1251441"}  # chairperson, monarch, khan, leader


# ---------------------------------------------------------------------------
# HTTP with on-disk caching + simple rate limiting
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


PATH_LIMITER = RateLimiter(PATH_SLEEP)
SEARCH_LIMITER = RateLimiter(SEARCH_SLEEP)


def _get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def cached_json(cache_file: Path, url: str, limiter: RateLimiter,
                refresh: bool, timeout: int = 30) -> dict:
    """Return JSON from cache when present, else fetch once and cache it."""
    if cache_file.exists() and not refresh:
        return json.loads(cache_file.read_text())
    limiter.wait()
    data = _get_json(url, timeout=timeout)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return data


# ---------------------------------------------------------------------------
# Resolve people and paths
# ---------------------------------------------------------------------------
def resolve_person(name: str, expected_qid: str, raw_dir: Path,
                   refresh: bool) -> tuple[dict | None, str]:
    """Search the name and confirm it maps to ``expected_qid``.

    Returns ``({qid, name, description}, reason)``; on failure the first item is
    ``None`` and ``reason`` says why (never guesses a QID blindly).
    """
    cache_file = raw_dir / "search" / f"{_slug(name)}.json"
    url = f"{API}/api/search?q={urllib.parse.quote(name)}"
    try:
        data = cached_json(cache_file, url, SEARCH_LIMITER, refresh)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"  ! search failed for {name}: {exc}", file=sys.stderr)
        return None, f"search failed ({type(exc).__name__})"

    items = data.get("items") or []
    hit = next((it for it in items if it.get("_key") == expected_qid), None)
    if hit is None:
        print(f"  ! search for {name!r} did not return {expected_qid}",
              file=sys.stderr)
        return None, "name not confirmed by search"
    return {
        "qid": hit["_key"],
        "name": hit.get("name_en") or name,
        "description": hit.get("description_en") or "",
    }, ""


def fetch_path(a_qid: str, b_qid: str, raw_dir: Path,
               refresh: bool) -> tuple[dict | None, str]:
    cache_file = raw_dir / "path" / f"{a_qid}_{b_qid}.json"
    url = f"{API}/api/graph/path/{a_qid}/{b_qid}"
    try:
        data = cached_json(cache_file, url, PATH_LIMITER, refresh, timeout=60)
    except urllib.error.HTTPError as exc:
        print(f"  ! path {a_qid}/{b_qid} -> HTTP {exc.code}", file=sys.stderr)
        return None, f"HTTP {exc.code}"
    except TimeoutError:
        print(f"  ! path {a_qid}/{b_qid} timed out", file=sys.stderr)
        return None, "timed out"
    except (urllib.error.URLError, ValueError) as exc:
        print(f"  ! path {a_qid}/{b_qid} failed: {exc}", file=sys.stderr)
        return None, f"request failed ({type(exc).__name__})"
    if not data.get("found"):
        print(f"  ! path {a_qid}/{b_qid} not found", file=sys.stderr)
        return None, "no path found"
    return data, ""


def pretty_edge(edge_type: str) -> str:
    if not edge_type:
        return "linked to"
    return edge_type.replace("_", " ")


def kind_of(vertex: dict) -> str:
    if vertex.get("_key") in POSITION_QIDS:
        return "position"
    prefix = (vertex.get("_id") or "").split("/", 1)[0]
    return KIND_LABELS.get(prefix, "node")


def _vertex_node(v: dict) -> dict:
    return {
        "qid": v.get("_key"),
        "name": v.get("name_en") or v.get("name") or v.get("_key"),
        "kind": kind_of(v),
        "image": bool(v.get("image_cached_path")),
    }


# ---------------------------------------------------------------------------
# Image download (cached on disk)
# ---------------------------------------------------------------------------
def download_image(qid: str, img_dir: Path, timeout: int = 20) -> bool:
    dest = img_dir / f"{qid}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return True
    url = f"{API}/api/images/{qid}.jpg"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = resp.read()
        if not blob:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        return True
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


# ---------------------------------------------------------------------------
# Assemble chains
# ---------------------------------------------------------------------------
def assemble(pair: dict, a: dict, b: dict, raw: dict) -> dict:
    vertices = raw.get("vertices") or []
    edges = raw.get("edges") or []

    def key(v: dict) -> str:
        return v.get("_key")

    chain = []
    for i, v in enumerate(vertices):
        node = _vertex_node(v)
        if i > 0:
            prev = vertices[i - 1]
            match = next(
                (e for e in edges
                 if {e.get("_from", "").rsplit("/", 1)[-1],
                     e.get("_to", "").rsplit("/", 1)[-1]}
                 == {key(prev), key(v)}),
                None,
            )
            node["via"] = pretty_edge(match.get("type") if match else "") \
                if match else "linked to"
        chain.append(node)

    return {
        "slug": pair["slug"],
        "topic": pair["topic"],
        "a": a,
        "b": b,
        "hops": len(edges),
        "chain": chain,
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
def _initial(name: str) -> str:
    for ch in name:
        if ch.isalnum():
            return ch.upper()
    return "?"


def _node_html(node: dict, endpoint: bool) -> str:
    qid = node["qid"]
    name = node["name"]
    cls = "person" if node["kind"] == "person" else "org"
    if endpoint:
        cls += " endpoint"
    img = (f'<img src="img/{qid}.jpg" alt="" loading="eager">'
           if node.get("image") else "")
    initial = f'<span class="initial">{_initial(name)}</span>' if not img else ""
    return (
        f'<div class="station">'
        f'<div class="dot {cls}">{img}{initial}</div>'
        f'<div class="nm{" strong" if endpoint else ""}" title="{name}">{name}</div>'
        f'</div>'
    )


def _conn_html(label: str, step: int) -> str:
    return (f'<div class="conn"><span class="step">{step}</span>'
            f'<span class="rel">{label}</span>'
            f'<span class="ln"></span></div>')


def render_html(payload: dict, out_path: Path) -> None:
    pairs = payload["pairs"]
    n = len(pairs)

    rows = []
    max_hops = max((p["hops"] for p in pairs), default=0)
    for p in pairs:
        cells = []
        for i, node in enumerate(p["chain"]):
            if i > 0:
                cells.append(_conn_html(node.get("via", "linked to"), i))
            cells.append(_node_html(node, endpoint=(i == 0 or i == len(p["chain"]) - 1)))
        rows.append(
            f'<div class="row">'
            f'<div class="idx">{p["topic"]}</div>'
            f'<div class="chain">{"".join(cells)}</div>'
            f'<div class="hopscell"><span class="hn">{p["hops"]}</span>'
            f'<span class="hk">hops</span></div>'
            f'</div>'
        )

    card_h = 320 + n * 96 + 168
    generated = payload["generated"]
    skipped = payload.get("skipped") or []

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap degrees of separation</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;          /* chrome only: eyebrow, wordmark, headline emphasis */
    --data:#bf6b28;            /* the only data colour: people + documented links */
    --structure:#8a8278;       /* structure: intermediary orgs, labels, rules */
    --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }}
  .card {{ width:1600px; height:{card_h}px; background:var(--bg); color:var(--text);
    padding:34px 44px 22px; display:flex; flex-direction:column; position:relative;
    overflow:hidden; }}
  .card header {{ display:flex; justify-content:space-between; align-items:baseline; }}
  .eyebrow {{ font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase;
    color:var(--chrome); }}
  .brand {{ font:700 27px/1 var(--display); color:var(--chrome); }}
  h1 {{ font:800 40px/1.06 var(--display); margin:14px 0 0; letter-spacing:-.012em;
    max-width:36ch; }}
  h1 em {{ font-style:normal; color:var(--chrome); }}
  .deck {{ font:400 15px/1.45 var(--body); color:var(--structure); margin:10px 0 0;
    max-width:1460px; }}
  .deck b {{ color:var(--text); font-weight:700; }}
  .wall {{ margin-top:20px; display:flex; flex-direction:column; }}
  .row {{ display:grid; grid-template-columns:236px 1fr 92px; align-items:start;
    gap:18px; padding:11px 0 12px; border-top:1px solid var(--line); }}
  .row:first-child {{ border-top:2px solid var(--text); }}
  .idx {{ font:700 12.5px/1.25 var(--body); letter-spacing:.05em; text-transform:uppercase;
    color:var(--structure); padding-top:15px; }}
  .chain {{ display:flex; align-items:flex-start; }}
  .station {{ flex:1 1 0; min-width:0; display:flex; flex-direction:column;
    align-items:center; }}
  .dot {{ width:50px; height:50px; border-radius:50%; overflow:hidden; position:relative;
    display:flex; align-items:center; justify-content:center; background:var(--surface);
    flex:0 0 auto; }}
  .dot img {{ width:100%; height:100%; object-fit:cover; display:block; }}
  .dot .initial {{ font:700 20px/1 var(--display); }}
  .dot.person {{ outline:2.6px solid var(--data); outline-offset:-1px; }}
  .dot.person .initial {{ color:var(--data); }}
  .dot.org {{ outline:2.2px solid var(--structure); outline-offset:-1px; }}
  .dot.org .initial {{ color:var(--structure); font-size:17px; }}
  .dot.endpoint {{ box-shadow:0 0 0 4px rgba(191,107,40,.20); }}
  .dot.org.endpoint {{ box-shadow:0 0 0 4px rgba(138,130,120,.18); }}
  .nm {{ font:500 10.6px/1.18 var(--body); text-align:center; margin-top:6px;
    color:var(--text); max-width:100%; overflow:hidden;
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }}
  .nm.strong {{ font-weight:700; }}
  .conn {{ flex:0 0 66px; display:flex; flex-direction:column; align-items:center;
    padding-top:0; }}
  .conn .step {{ width:15px; height:15px; border-radius:50%; background:var(--data);
    color:#fff; font:700 9.4px/15px var(--body); text-align:center; margin-bottom:3px; }}
  .conn .rel {{ font:600 9.4px/1.15 var(--body); color:var(--data); text-align:center;
    max-width:70px; }}
  .conn .ln {{ position:relative; width:100%; height:0; border-top:1.7px solid var(--data);
    margin-top:10px; }}
  .conn .ln::after {{ content:''; position:absolute; right:-1px; top:-4.2px;
    border-left:7px solid var(--data); border-top:4.2px solid transparent;
    border-bottom:4.2px solid transparent; }}
  .hopscell {{ display:flex; flex-direction:column; align-items:flex-end; padding-top:4px; }}
  .hn {{ font:800 27px/1 var(--display); color:var(--data); }}
  .hk {{ font:500 10px/1.2 var(--body); color:var(--structure);
    text-transform:uppercase; letter-spacing:.08em; margin-top:3px; }}
  .band {{ margin-top:auto; border-top:2px solid var(--text); padding-top:11px;
    display:flex; justify-content:space-between; align-items:flex-end; gap:28px; }}
  .legend {{ display:flex; gap:22px; align-items:center;
    font:400 12.4px/1.35 var(--body); color:var(--structure); flex-wrap:wrap; }}
  .legend .item {{ display:flex; align-items:center; gap:8px; }}
  .legend .swatch {{ width:16px; height:16px; border-radius:50%; flex:0 0 auto; }}
  .legend .swatch.person {{ background:var(--data); }}
  .legend .swatch.org {{ background:var(--structure); }}
  .legend .arr {{ width:30px; height:0; border-top:2px solid var(--data);
    position:relative; }}
  .legend .arr::after {{ content:''; position:absolute; right:-1px; top:-4px;
    border-left:7px solid var(--data); border-top:4px solid transparent;
    border-bottom:4px solid transparent; }}
  .legend .stepdot {{ width:15px; height:15px; border-radius:50%; background:var(--data);
    color:#fff; font:700 9.4px/15px var(--body); text-align:center; flex:0 0 auto; }}
  .legend b {{ color:var(--text); font-weight:700; }}
  .caveat {{ font:400 11.4px/1.4 var(--body); color:var(--structure);
    max-width:620px; text-align:right; }}
  .card footer {{ margin-top:12px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 12.5px/1.4 var(--body); color:var(--structure); }}
</style></head>
<body><section class="card">
  <header>
    <span class="eyebrow">Degrees of separation</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>How <em>{n} unlikely pairs</em> of famous people are connected, in a few steps each.</h1>
  <p class="deck">Two famous people, almost nothing obviously in common. Each row is the
  <b>shortest chain of people and organisations</b> this map can find between them, drawn from
  <b>nearly 8 million people</b> and their recorded links. Portraits and names are the
  graph&rsquo;s own; the label on each arrow is the recorded relationship. One arrow is one
  recorded link, or &ldquo;hop&rdquo;.</p>
  <div class="wall">
    {"".join(rows)}
  </div>
  <div class="band">
    <div class="legend">
      <span class="item"><span class="swatch person"></span><b>person</b> (data)</span>
      <span class="item"><span class="swatch org"></span><b>organisation, school, role or event</b>
        (connective step)</span>
      <span class="item"><span class="arr"></span><b>documented link</b>:
        the arrow label names it</span>
      <span class="item"><span class="stepdot">3</span><b>numbered badge</b> = the
        link&rsquo;s step along the chain</span>
      <span class="item"><b>number</b> = hops between the two</span>
    </div>
    <div class="caveat"><b>Read with care.</b> A shortest path is the shortest
      <em>documented</em> chain &mdash; one of possibly several equally short ones &mdash; and
      not proof the two people met or knew each other. Edges come
      from Wikidata; some people are hubs (memberships, schools) so many paths route through
      them, and a few steps are generic roles (e.g. &ldquo;chairperson&rdquo;). Longest chain
      shown: {max_hops} hops.</div>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span>{n} pairs &middot; shortest documented chain</span>
  </footer>
</section>
</body></html>
"""
    out_path.write_text(html)
    if skipped:
        print(f"note: {len(skipped)} pair(s) skipped: "
              + ", ".join(s["slug"] for s in skipped), file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build_payload(raw_dir: Path, refresh: bool) -> dict:
    out = {
        "generated": date.today().isoformat(),
        "source": "https://humansmap.com/api/graph/path",
        "endpoint_note": ("Public cached endpoint; shortest path is the shortest "
                          "documented chain, edges from Wikidata."),
        "pairs": [],
        "skipped": [],
    }
    for pair in PAIRS:
        print(f"· {pair['a']} ↔ {pair['b']}")
        a, why_a = resolve_person(pair["a"], pair["qa"], raw_dir, refresh)
        if a is None:
            out["skipped"].append({"slug": pair["slug"], "reason": why_a})
            continue
        b, why_b = resolve_person(pair["b"], pair["qb"], raw_dir, refresh)
        if b is None:
            out["skipped"].append({"slug": pair["slug"], "reason": why_b})
            continue
        raw, why = fetch_path(a["qid"], b["qid"], raw_dir, refresh)
        if raw is None:
            out["skipped"].append({"slug": pair["slug"], "reason": why})
            continue
        entry = assemble(pair, a, b, raw)
        print(f"  -> {entry['hops']} hops via "
              + " → ".join(node["name"] for node in entry["chain"]))
        out["pairs"].append(entry)
    out["pairs"].sort(key=lambda p: (p["hops"], p["slug"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--raw-dir", type=Path, default=None,
                    help="raw API cache (default: <out-dir>/raw)")
    ap.add_argument("--from-json", type=Path, default=None,
                    help="rebuild HTML from a cached paths.json (no network)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore raw cache and re-fetch")
    ap.add_argument("--no-images", action="store_true",
                    help="do not download portraits")
    args = ap.parse_args()

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = args.raw_dir or (out_dir / "raw")

    if args.from_json:
        payload = json.loads(args.from_json.read_text())
    else:
        payload = build_payload(raw_dir, args.refresh)
        (out_dir / "paths.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1))

    img_dir = out_dir / "img"
    qids = {node["qid"] for p in payload["pairs"] for node in p["chain"]}
    have_image: dict[str, bool] = {}
    for qid in qids:
        if args.no_images:
            have_image[qid] = (img_dir / f"{qid}.jpg").exists()
        else:
            have_image[qid] = download_image(qid, img_dir)
    for p in payload["pairs"]:
        for node in p["chain"]:
            node["image"] = have_image.get(node["qid"], False)
    (out_dir / "paths.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1))
    render_html(payload, out_dir / "paths.html")
    print(f"wrote {out_dir / 'paths.json'}")
    print(f"wrote {out_dir / 'paths.html'} "
          f"({len(payload['pairs'])} pairs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
