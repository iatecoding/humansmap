#!/usr/bin/env python3
"""Bilingualism matrix — renders the language-bridge payload as a directed
language x language heatmap, over two different denominators.

The bridge payload carries two populations per language:

  * ``speakers``     — people whose record shows *two or more* languages.
  * ``speakers_all`` — everyone with the language recorded at all, including
    the large majority whose record lists exactly one language.

Both grids use the same numerator — the people recorded with both languages —
and differ only in the denominator, which is why they can be read side by side:

  * **Among recorded multilingual speakers** — of the people recorded speaking
    the row language with 2+ languages, how many also speak the column one.
    This is the only population where a *bilingual rate* can be measured.
  * **Among everyone with the language recorded** — of all people recorded with
    the row language, how many also have the column one recorded.

The second number is far smaller and is much closer to a measure of how sparse
``languages_spoken`` (P1412) is than to how many people actually speak two
languages: half of the best-documented people in Wikidata still have only one
language recorded. The card says so, and prints the coverage rate — the share of
a language's recorded speakers who have any second language recorded at all —
next to the grids.

Reads the cached payload only — no database access, no ArangoDB, no network:

    python3 backend/scripts/marketing_langs_matrix.py \\
        --from-json docs/marketing/langs/langs.json \\
        --out-dir docs/marketing/langmatrix

Then render the card to PNG (from scripts/marketing/):

    source env.sh
    node render-card.js ../../docs/marketing/langmatrix/langmatrix.html \\
        ../../docs/marketing/langmatrix/langmatrix.png

Flags:
    --from-json PATH   the language-bridge payload (required)
    --out-dir PATH     output directory (default: docs/marketing/langmatrix)
    --languages N      rows/columns kept, biggest speaker base first (default 24)
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

DEFAULT_N = 24
CELL = 20.0
LABEL_W = 116.0
LABEL_TOP = 108.0

# Sequential ramp: pale paper -> brand orange -> deep umber. One hue, so "more
# of the row's speakers" is the only thing the eye has to rank.
RAMP = [(0.00, (245, 241, 232)), (0.20, (233, 205, 170)),
        (0.45, (214, 150, 88)), (0.70, (191, 107, 40)), (1.00, (116, 52, 16))]


def _esc(v) -> str:
    return html.escape(str(v))


def _n(v) -> str:
    return f"{int(v):,}"


def _ramp(t: float) -> str:
    t = min(1.0, max(0.0, t))
    for i in range(len(RAMP) - 1):
        t0, c0 = RAMP[i]
        t1, c1 = RAMP[i + 1]
        if t <= t1:
            f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
            rgb = tuple(round(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
            return "#%02x%02x%02x" % rgb
    return "#%02x%02x%02x" % RAMP[-1][1]


# ---------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------


def derive(payload: dict, top_n: int) -> dict:
    nodes = {n["lang"]: n for n in payload["nodes"]}
    living = [n for n in payload["nodes"] if not n.get("historical")]
    living.sort(key=lambda n: -n["speakers"])
    picked = [n["lang"] for n in living[:top_n]]

    count: dict = {}
    for e in payload["edges"]:
        a, b = e["a"], e["b"]
        count[(a, b)] = e["count"]
        count[(b, a)] = e["count"]

    mult = {n["lang"]: n["speakers"] for n in payload["nodes"]}
    full = {n["lang"]: n.get("speakers_all", n["speakers"])
            for n in payload["nodes"]}

    def share(base: dict, a: str, b: str) -> float:
        denom = base.get(a, 0)
        return (count.get((a, b), 0) / denom) if denom else 0.0

    rows = []
    for a in picked:
        cells = {b: share(mult, a, b) for b in picked if b != a}
        cells_all = {b: share(full, a, b) for b in picked if b != a}
        rows.append({
            "lang": a,
            "speakers": mult[a],
            "speakers_all": full[a],
            "coverage": (mult[a] / full[a]) if full[a] else 0.0,
            "group": nodes[a]["cluster_label"],
            "branch": nodes[a].get("branch", ""),
            "cells": cells,
            "cells_all": cells_all,
            "english": cells.get("English") if a != "English" else None,
            "links": sum(count.get((a, b), 0) for b in picked if b != a),
        })

    ranked = sorted((r for r in rows if r["english"] is not None),
                    key=lambda r: -r["english"])
    return {"rows": rows, "english_ranked": ranked, "nodes": nodes,
            "top_n": top_n}


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _matrix_svg(slots: list, cols: list, rows_by_lang: dict, key: str,
                with_labels: bool, header: str) -> str:
    """One heatmap.

    ``slots`` fixes the row positions in a shared order so the two panels line
    up row for row.
    """
    n = len(cols)
    overhang = 84.0 if with_labels else 2.0
    w = (LABEL_W if with_labels else 0.0) + n * CELL + overhang
    h = LABEL_TOP + len(slots) * CELL + 2.0
    x0 = LABEL_W if with_labels else 1.0
    y0 = LABEL_TOP

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" '
             f'width="{w:.0f}" height="{h:.0f}" role="img" aria-label="{_esc(header)}">']

    if with_labels:
        for j, lang in enumerate(cols):
            cx = x0 + (j + 0.5) * CELL
            cy = y0 - 6.0
            parts.append(
                f'<text x="{cx:.1f}" y="{cy:.1f}" text-anchor="start" '
                f'transform="rotate(-52 {cx:.1f} {cy:.1f})" class="mlabel">{_esc(lang)}</text>')

    for i, slot in enumerate(slots):
        y = y0 + i * CELL
        row = rows_by_lang.get(slot)
        if with_labels:
            parts.append(
                f'<text x="{LABEL_W - 7.0:.1f}" y="{y + CELL * 0.7:.1f}" '
                f'text-anchor="end" class="rlabel">{_esc(slot)}</text>')
        if not row:
            continue
        for j, lang in enumerate(cols):
            x = x0 + j * CELL
            if lang == slot:
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{CELL - 1:.1f}" '
                             f'height="{CELL - 1:.1f}" fill="#e3ddd0"/>')
                continue
            cell = row.get(key, {}).get(lang)
            if cell is None:
                continue
            fill = _ramp(cell ** 0.7)
            tip = (f'{_esc(slot)} \u2192 {_esc(lang)}: '
                   f'{round(100 * cell)}% of recorded {_esc(slot)} speakers '
                   f'also speak {_esc(lang)}')
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{CELL - 1:.1f}" '
                f'height="{CELL - 1:.1f}" fill="{fill}"><title>{tip}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap — bilingualism matrix</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;
    --data:#bf6b28;
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
  h1 { font:800 34px/1.06 var(--display); margin:9px 0 0; letter-spacing:-.012em;
    max-width:60ch; }
  .deck { font:400 13.6px/1.42 var(--body); color:var(--structure); margin:7px 0 0;
    max-width:1512px; }
  .deck b { color:var(--text); font-weight:700; }
  .stage { margin-top:9px; display:flex; gap:28px; }
  .panels { flex:0 0 auto; }
  .ptitle { font:700 13.5px/1 var(--display); color:var(--text); margin:0 0 2px; }
  .psub { font:400 10.6px/1.3 var(--body); color:var(--structure); margin:0 0 4px;
    max-width:520px; }
  .panels .row { display:flex; gap:16px; align-items:flex-start; }
  .mlabel { font-family:var(--body); font-weight:500; font-size:10.4px; fill:var(--text); }
  .rlabel { font-family:var(--body); font-weight:500; font-size:11px; fill:var(--text); }
  .keys { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.16em; text-transform:uppercase;
    color:var(--structure); margin-bottom:9px; }
  .read { display:flex; flex-direction:column; gap:6px; font:400 12.2px/1.35 var(--body);
    color:var(--structure); }
  .read b { color:var(--text); }
  .ramp { display:flex; align-items:center; gap:8px; font:500 10.4px/1 var(--body);
    color:var(--structure); }
  .rampbar { width:150px; height:12px; border-radius:2px;
    background:linear-gradient(90deg,#f5f1e8,#e9cd9a,#d69658,#bf6b28,#743410); }
  .list { display:flex; flex-direction:column; gap:5px; margin-top:2px; }
  .lrow { display:flex; align-items:center; gap:8px; font:500 10.6px/1.2 var(--body); }
  .lrow .lname { flex:0 0 122px; }
  .lrow .lbar { flex:1 1 auto; height:9px; border-radius:2px; background:var(--surface);
    position:relative; overflow:hidden; }
  .lrow .lbar i { position:absolute; inset:0 auto 0 0; background:var(--data);
    border-radius:2px; }
  .lrow .lbar i.mut { background:#8a8278; }
  .lrow .lval { flex:0 0 34px; text-align:right; color:var(--data); font-weight:700; }
  .stat { display:flex; gap:18px; margin-top:auto; padding-top:10px;
    border-top:1px solid var(--line); }
  .stat div b { display:block; font:800 18px/1 var(--display); }
  .stat div span { display:block; font:500 9.5px/1.25 var(--body); color:var(--structure);
    text-transform:uppercase; letter-spacing:.06em; margin-top:3px; }
  .caveat { font:400 11px/1.4 var(--body); color:var(--structure); margin-top:11px;
    padding-top:9px; border-top:1px solid var(--line); }
  .caveat b { color:var(--text); }
  .card footer { margin-top:8px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 12.3px/1.4 var(--body); color:var(--structure); }
</style></head>
<body>"""


def render(d: dict, payload: dict) -> str:
    rows = d["rows"]
    scale = payload["scale"]

    langs = [r["lang"] for r in rows]
    by_lang = {r["lang"]: r for r in rows}
    en_out = round(100 * by_lang["English"]["cells"].get("German", 0))
    de_out = round(100 * by_lang["German"]["cells"].get("English", 0))
    de_out_all = round(100 * by_lang["German"]["cells_all"].get("English", 0))

    deck = (
        f"Each row is a language, each column another, and the same numerator — "
        f"<b>people recorded with both</b> — sits over two denominators. Left: of the "
        f"people recorded speaking the row language with <b>two or more languages</b>. "
        f"Right: of <b>everyone recorded with</b> the row language, most of whom have "
        f"exactly one. So <b>{de_out}%</b> of recorded multilingual German speakers "
        f"also speak English, but only <b>{de_out_all}%</b> of everyone with German "
        f"recorded also has English recorded."
    )

    left = _matrix_svg(langs, langs, by_lang, "cells", True,
                       "Of recorded multilingual speakers")
    right = _matrix_svg(langs, langs, by_lang, "cells_all", False,
                        "Of everyone with the language recorded")

    ramp = (
        '<div class="ramp"><span>0%</span><span class="rampbar"></span>'
        "<span>100% of the row's speakers</span></div>"
    )

    eranked = d["english_ranked"]
    picks = sorted(eranked, key=lambda r: -r["english"])[:6]

    def erow(r: dict) -> str:
        pct = round(100 * r["english"])
        return (f'<div class="lrow"><span class="lname">{_esc(r["lang"])}</span>'
                f'<span class="lbar"><i style="width:{pct}%"></i></span>'
                f'<span class="lval">{pct}%</span></div>')

    def pair_row(a: str, b: str, v: float) -> str:
        pct = round(100 * v)
        return (f'<div class="lrow"><span class="lname">{_esc(a)} &rarr; {_esc(b)}</span>'
                f'<span class="lbar"><i style="width:{pct}%"></i></span>'
                f'<span class="lval">{pct}%</span></div>')

    nonenglish = sorted(
        ({"a": r["lang"], "b": b, "v": v}
         for r in rows for b, v in r["cells"].items()
         if r["lang"] != "English" and b != "English" and r["lang"] != b),
        key=lambda x: -x["v"])[:5]

    # Coverage: how often a second language is recorded at all. This is the
    # honest version of "speaks one and not the other" — and the reason the
    # right-hand grid is so much paler than the left.
    cov_sorted = sorted((r for r in rows if r["lang"] != "English"),
                        key=lambda r: -r["coverage"])
    cov_high = cov_sorted[:3]
    cov_low = cov_sorted[-3:]
    median_cov = sorted(r["coverage"] for r in rows if r["lang"] != "English")
    median_cov = median_cov[len(median_cov) // 2]

    def cov_row(r: dict) -> str:
        pct = round(100 * r["coverage"])
        return (f'<div class="lrow"><span class="lname">{_esc(r["lang"])}</span>'
                f'<span class="lbar"><i class="mut" style="width:{pct}%"></i></span>'
                f'<span class="lval" style="color:var(--structure)">{pct}%</span></div>')

    stats = (
        f'<div class="stat">'
        f'<div><b>{_n(scale["people_with_language"])}</b>'
        f'<span>people with any language recorded</span></div>'
        f'<div><b>{round(100 * scale["multilingua_people"] / scale["people_with_language"])}%</b>'
        f'<span>of them have two or more</span></div>'
        f'</div>'
    )

    caveat = (
        "<b>Read with care.</b> The right-hand grid is far paler not because people "
        "speak fewer languages but because <b>languages_spoken is usually left almost "
        "empty</b>: even half of the best-documented people in Wikidata have only one "
        "language recorded. Treat the left grid as the bilingual rate and the right "
        "grid as a measure of how much of the record is missing. Coverage skews to "
        "well-documented figures and to Europe. Historical languages are left out."
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">The bilingualism matrix</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>How much of each language's record also speaks another.</h1>
  <p class="deck">{deck}</p>
  <div class="stage">
    <div class="panels">
      <div class="row">
        <div>
          <p class="ptitle">Of recorded multilingual speakers</p>
          <p class="psub">of the people recorded speaking the row language with two or more languages</p>
          {left}
        </div>
        <div>
          <p class="ptitle">Of everyone with the language recorded</p>
          <p class="psub">of all people recorded with the row language, most of whom have only one</p>
          {right}
        </div>
      </div>
    </div>
    <aside class="keys">
      <div class="plabel">How to read it</div>
      <div class="read">{ramp}</div>
      <div class="plabel" style="margin-top:14px">Who also speaks English</div>
      <div class="list">{''.join(erow(r) for r in picks)}</div>
      <div class="plabel" style="margin-top:14px">A second language is recorded at all</div>
      <div class="list">{''.join(cov_row(r) for r in cov_high)}
        <div class="lrow" style="color:var(--structure)"><span class="lname" style="font-style:italic">…</span>
        <span class="lbar" style="background:transparent"></span>
        <span class="lval" style="color:var(--structure)">{round(100 * median_cov)}%</span></div>
        {''.join(cov_row(r) for r in cov_low)}</div>
      <div class="plabel" style="margin-top:14px">Strongest pairs without English</div>
      <div class="list">{''.join(pair_row(p['a'], p['b'], p['v']) for p in nonenglish)}</div>
      {stats}
      <div class="caveat">{caveat}</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span>{len(rows)} living languages &middot; same numerator, two denominators</span>
  </footer>
</section>"""
    return HTML_HEAD + card + "\n</body></html>\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-json", required=True)
    ap.add_argument("--out-dir", default="docs/marketing/langmatrix")
    ap.add_argument("--languages", type=int, default=DEFAULT_N)
    args = ap.parse_args()

    payload = json.loads(Path(args.from_json).read_text())
    d = derive(payload, args.languages)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "langmatrix.html").write_text(render(d, payload))
    summary = {
        "languages": [r["lang"] for r in d["rows"]],
        "coverage_multilingual_share": [
            {"lang": r["lang"], "coverage": round(r["coverage"], 3),
             "speakers_multi": r["speakers"], "speakers_all": r["speakers_all"]}
            for r in sorted(d["rows"], key=lambda r: -r["coverage"])],
        "english_ranked_multi": [
            {"lang": r["lang"], "share": round(r["english"], 3)}
            for r in sorted(d["english_ranked"], key=lambda r: -r["english"])],
    }
    (out / "langmatrix.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {out / 'langmatrix.html'}")
    lo = min(r["coverage"] for r in d["rows"])
    hi = max(r["coverage"] for r in d["rows"])
    print(f"coverage of a second language recorded: {100 * lo:.0f}% .. {100 * hi:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
