#!/usr/bin/env python3
"""Family-or-contact split — where a language's bilingual links actually go.

For every living language in the bridge payload, this adds up the people it
shares with every other language and splits that total two ways:

  * **inside the family** — the other language is in the same linguistic branch
  * **across families** — it is not

"Family" here is a *hand-set* field (``FAMILY_OF`` in ``marketing_langs.py``):
Germanic, Romance, Slavic, Celtic, Baltic, Hellenic, Armenian, Albanian,
Iranian, Indo-Aryan, Dravidian, Uralic, Turkic, Semitic, Berber, Chadic,
Niger-Congo, Creole, Sinitic, Japonic, Koreanic, Kra-Dai, Austroasiatic,
Austronesian, Sino-Tibetan, Kartvelian, Isolate, Constructed, Sign language.
Wikidata records *that* people speak languages; it records nothing about how
those languages are related, so the branch is curation, not data — and the card
says so. It is deliberately not the ring's colour groups, which are broader
reading frames (some regional).

Reads the cached payload only — no database access:

    python3 backend/scripts/marketing_langs_family.py \\
        --from-json docs/marketing/langs/langs.json \\
        --out-dir docs/marketing/langfamily

Then render the card to PNG (from scripts/marketing/):

    source env.sh
    node render-card.js ../../docs/marketing/langfamily/langfamily.html \\
        ../../docs/marketing/langfamily/langfamily.png

Flags:
    --from-json PATH   the language-bridge payload (required)
    --out-dir PATH     output directory (default: docs/marketing/langfamily)
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

ROW_H = 18.2
LABEL_W = 104.0
BAR_W = 292.0
VAL_W = 30.0
COLS = 2
COL_GAP = 44.0

INSIDE = "#bf6b28"
ACROSS = "#1f8f8a"


def _esc(v) -> str:
    return html.escape(str(v))


def _n(v) -> str:
    return f"{int(v):,}"


def derive(payload: dict, min_links: int) -> dict:
    nodes = {n["lang"]: n for n in payload["nodes"]}
    edge = {}
    for e in payload["edges"]:
        edge[(e["a"], e["b"])] = e
        edge[(e["b"], e["a"])] = e

    rows = []
    for lang, n in nodes.items():
        if n.get("historical"):
            continue
        total = 0
        inside = 0
        for (a, b), e in edge.items():
            if a != lang:
                continue
            total += e["count"]
            if nodes[b].get("branch") == n.get("branch"):
                inside += e["count"]
        if total <= 0:
            continue
        rows.append({
            "lang": lang, "branch": n.get("branch", ""),
            "group": n.get("cluster_label", ""),
            "speakers": n["speakers"], "links": total, "inside": inside,
            "outside": total - inside, "share": inside / total,
            "rankable": total >= min_links,
        })
    rows.sort(key=lambda r: (-r["share"], -r["links"]))

    seen: dict = {}
    for r in rows:
        seen[r["branch"]] = seen.get(r["branch"], 0) + 1
    for r in rows:
        # A language that is the only member of its family in this record has no
        # relative to link to, so its 0% is structural, not a finding.
        r["lone"] = seen.get(r["branch"], 0) == 1

    cross = [e for e in payload["edges"] if e.get("cross_branch") and not (
        nodes[e["a"]].get("historical") or nodes[e["b"]].get("historical"))]
    cross.sort(key=lambda e: -e["count"])

    overall = (sum(r["inside"] for r in rows) / sum(r["links"] for r in rows)
               if rows else 0.0)
    return {"rows": rows, "cross": cross[:6], "overall": overall}


HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap — is bilingualism family or contact?</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;
    --data:#bf6b28;
    --across:#1f8f8a;
    --structure:#8a8278;
    --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }
  .card { width:1600px; height:1000px; background:var(--bg); color:var(--text);
    padding:30px 44px 16px; display:flex; flex-direction:column; position:relative;
    overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase;
    color:var(--chrome); }
  .brand { font:700 27px/1 var(--display); color:var(--chrome); }
  h1 { font:800 35px/1.06 var(--display); margin:9px 0 0; letter-spacing:-.012em;
    max-width:62ch; }
  .deck { font:400 13.6px/1.42 var(--body); color:var(--structure); margin:7px 0 0;
    max-width:1512px; }
  .deck b { color:var(--text); font-weight:700; }
  .stage { margin-top:12px; display:flex; gap:30px; align-items:flex-start; }
  .bcols { flex:0 0 auto; display:flex; gap:44px; }
  .bcol { flex:0 0 auto; }
  .chead { display:flex; gap:8px; align-items:baseline; margin:0 0 6px;
    font:700 10px/1 var(--body); letter-spacing:.14em; text-transform:uppercase;
    color:var(--structure); }
  .brow { display:flex; align-items:center; gap:8px; height:18.2px; }
  .brow .lname { flex:0 0 104px; font:500 10.5px/1.15 var(--body); color:var(--text);
    overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
  .brow .bar { flex:0 0 292px; height:10px; border-radius:2px; background:var(--surface);
    display:flex; overflow:hidden; }
  .brow .bar i { display:block; height:100%; }
  .brow .val { flex:0 0 30px; text-align:right; font:700 10px/1 var(--body);
    color:var(--structure); }
  .brow .val.muted { font-weight:500; color:#b3aa9c; }
  .brow .lname.lone { color:var(--structure); font-style:italic; }
  .brow .bar.lone { background:repeating-linear-gradient(135deg,#e6e0d4,#e6e0d4 4px,
    #d9d2c3 4px,#d9d2c3 8px); }
  .hinge { display:flex; align-items:center; gap:8px; font:500 11px/1.2 var(--body);
    color:var(--text); }
  .hinge .hname { flex:1 1 auto; }
  .hinge .hnum { flex:0 0 auto; font:700 11px/1 var(--body); color:var(--across); }
  .keys { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.16em; text-transform:uppercase;
    color:var(--structure); margin-bottom:9px; }
  .read { display:flex; flex-direction:column; gap:8px; font:400 12.2px/1.38 var(--body);
    color:var(--structure); }
  .read b { color:var(--text); }
  .key { display:flex; align-items:center; gap:8px; }
  .key i { width:22px; height:11px; border-radius:2px; display:inline-block; }
  .readkey { display:flex; flex-direction:column; gap:6px; margin-bottom:4px; }
  .famlist { font:400 11.4px/1.5 var(--body); color:var(--structure); }
  .famlist b { color:var(--text); font-weight:600; }
  .hinges { display:flex; flex-direction:column; gap:7px; }
  .big { font:800 30px/1 var(--display); color:var(--data); }
  .bigwrap { display:flex; align-items:center; gap:12px; margin-top:2px; }
  .bigwrap span { font:400 11.4px/1.35 var(--body); color:var(--structure); }
  .caveat { font:400 11px/1.4 var(--body); color:var(--structure); margin-top:auto;
    padding-top:9px; border-top:1px solid var(--line); }
  .caveat b { color:var(--text); }
  .card footer { margin-top:8px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 12.3px/1.4 var(--body); color:var(--structure); }
</style></head>
<body>"""


def _brow(r: dict) -> str:
    if r["lone"]:
        tip = (f'{_esc(r["lang"])} is the only {_esc(r["branch"])} language in this '
               f'record \u2014 no relative is drawn, so none of its '
               f'{_n(r["links"])} links can be inside the family')
        return (
            f'<div class="brow" title="{tip}">'
            f'<span class="lname lone">{_esc(r["lang"])}</span>'
            f'<span class="bar lone"></span>'
            f'<span class="val muted">\u2013</span></div>'
        )
    pct = round(100 * r["share"])
    tip = (f'{_esc(r["lang"])} ({_esc(r["branch"])}): {_n(r["links"])} recorded '
           f'co-speaker links \u00b7 {pct}% stay inside {_esc(r["branch"])}')
    val = f'<span class="val{" muted" if not r["rankable"] else ""}">{pct}%</span>'
    return (
        f'<div class="brow" title="{tip}">'
        f'<span class="lname">{_esc(r["lang"])}</span>'
        f'<span class="bar"><i style="width:{pct}%;background:{INSIDE}"></i>'
        f'<i style="width:{100 - pct}%;background:{ACROSS}"></i></span>'
        f'{val}</div>'
    )


def render(d: dict, payload: dict, min_links: int) -> str:
    rows = d["rows"]
    rankable = [r for r in rows if r["rankable"]]
    overall = round(100 * d["overall"])

    # split the ranked rows into two columns so every language still fits
    half = -(-len(rows) // COLS)
    columns = [rows[i:i + half] for i in range(0, len(rows), half)]

    heads = ["sorted: most inside the family first", "continues: most across families"]
    cols = ""
    for i, chunk in enumerate(columns):
        head = f'<div class="chead"><span>{heads[i] if i < len(heads) else ""}</span></div>'
        cols += f'<div class="bcol">{head}{"".join(_brow(r) for r in chunk)}</div>'

    hinges = "".join(
        f'<div class="hinge"><span class="hname">{_esc(e["a"])} &middot; {_esc(e["b"])}'
        f'<br><span style="color:var(--structure);font-size:10px">'
        f'{_esc(payload_branch(payload, e["a"]))} \u2194 '
        f'{_esc(payload_branch(payload, e["b"]))}</span></span>'
        f'<span class="hnum">{_n(e["count"])}</span></div>'
        for e in d["cross"])

    branches = sorted({r["branch"] for r in rows if r["branch"]})
    deck = (
        f"Every recorded co-speaker link of a language is either to <b>another "
        f"language in its own family</b> or to <b>one outside it</b>. Across all "
        f"{len(rows)} living languages drawn on the bridge card, only "
        f"<b>{overall}%</b> of those links stay inside the family; the rest cross "
        f"families. Family is the linguistic branch a language belongs to, set by "
        f"hand \u2014 Wikidata records that people speak languages, not how the "
        f"languages are related."
    )

    readkey = (
        f'<div class="key"><i style="background:{INSIDE}"></i>'
        f'<span>links to a language <b>in the same family</b></span></div>'
        f'<div class="key"><i style="background:{ACROSS}"></i>'
        f'<span>links to a language <b>in another family</b></span></div>'
        f'<div class="key"><i style="background:repeating-linear-gradient(135deg,'
        f'#e6e0d4,#e6e0d4 4px,#d9d2c3 4px,#d9d2c3 8px)"></i>'
        f'<span>no relative of that family is drawn, so 0% is structural</span></div>'
    )

    famlist = (
        f'<span class="famlist"><b>Families used here:</b> '
        f'{", ".join(_esc(b) for b in branches)}. Historical and classical '
        f'languages are left out.</span>'
    )

    caveat = (
        "<b>Read with care.</b> Only people whose languages are recorded \u2014 a "
        "coverage that skews to well-documented figures and to Europe, so this is "
        "partly a map of Wikipedia. A language with few recorded links swings to 0% "
        "or 100% on a handful of people."
        + (f" Languages with fewer than {min_links} recorded links are drawn but"
           f" not ranked." if any(not r["rankable"] for r in rows) else "")
    )

    stats = (
        f'<div class="bigwrap"><span class="big">{overall}%</span>'
        f'<span>of all recorded co-speaker links stay inside the language\'s own '
        f'family</span></div>'
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">Family or contact?</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>Most bilingual links cross language families.</h1>
  <p class="deck">{deck}</p>
  <div class="stage">
    <div class="bcols">{cols}</div>
    <aside class="keys">
      <div class="plabel">Across every language here</div>
      {stats}
      <div class="plabel" style="margin-top:16px">How to read it</div>
      <div class="readkey">{readkey}</div>
      <div class="read">{famlist}</div>
      <div class="plabel" style="margin-top:16px">The strongest hinges across families</div>
      <div class="hinges">{hinges}</div>
      <div class="caveat">{caveat}</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span>{len(rows)} living languages &middot; link weight = people who speak both</span>
  </footer>
</section>"""
    return HTML_HEAD + card + "\n</body></html>\n"


def payload_branch(payload: dict, lang: str) -> str:
    for n in payload["nodes"]:
        if n["lang"] == lang:
            return n.get("branch", "")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-json", required=True)
    ap.add_argument("--out-dir", default="docs/marketing/langfamily")
    ap.add_argument("--min-links", type=int, default=300)
    args = ap.parse_args()

    payload = json.loads(Path(args.from_json).read_text())
    d = derive(payload, args.min_links)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "langfamily.html").write_text(render(d, payload, args.min_links))
    (out / "langfamily.json").write_text(json.dumps({
        "overall_inside_share": round(d["overall"], 4),
        "languages": [{"lang": r["lang"], "branch": r["branch"],
                       "links": r["links"], "inside_share": round(r["share"], 3)}
                      for r in d["rows"]],
        "strongest_cross_family": [
            {"a": e["a"], "b": e["b"], "count": e["count"],
             "branch_a": e.get("branch_a"), "branch_b": e.get("branch_b")}
            for e in d["cross"]],
    }, indent=2) + "\n")
    print(f"wrote {out / 'langfamily.html'}")
    print(f"overall inside-family share: {100 * d['overall']:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
