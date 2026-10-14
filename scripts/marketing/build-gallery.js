/**
 * Build the public marketing gallery page.
 *
 * Usage (from anywhere):
 *   node scripts/marketing/build-gallery.js
 *
 * Reads the asset list below, verifies every master PNG and its responsive
 * thumbnails exist, then writes docs/marketing/gallery.html. Edit this file,
 * not the generated HTML, when assets are added or renamed.
 *
 * Thumbnails are produced by scripts/marketing/render-gallery-thumbs.js.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const HERE = __dirname;
const DOCS = path.resolve(HERE, '../../docs/marketing');
const OUT = path.join(DOCS, 'gallery.html');
const SITE = 'https://humansmap.com';

// Inline the real site favicon so the gallery matches humansmap.com exactly
// (and still renders when the file is opened locally).
let FAVICON = '/favicon.svg';
try {
  const svg = fs.readFileSync(path.resolve(HERE, '../../frontend/public/favicon.svg'), 'utf8');
  FAVICON = 'data:image/svg+xml,' + encodeURIComponent(svg);
} catch (err) {
  console.warn('  ! could not read frontend/public/favicon.svg: falling back to /favicon.svg');
}

// --- asset catalog ---------------------------------------------------------
// src is relative to docs/marketing. Order here is the order on the page.
const SECTIONS = [
  {
    id: 'cards',
    title: 'Insight cards',
    note: 'Portrait cards, 1080×1620: a shareable series on records, dynasties and scale.',
    items: [
      { src: 'cards/card-01.png', title: 'Who is connected to whom', desc: 'the scale of the dataset: 8.0M people, 10.9M connections.' },
      { src: 'cards/card-02.png', title: 'Most decorated', desc: 'who collected the most honours.' },
      { src: 'cards/card-03.png', title: 'Most marriages', desc: 'who married the most people.' },
      { src: 'cards/card-04.png', title: 'Most offices held', desc: 'who held the most public offices.' },
      { src: 'cards/card-05.png', title: 'Academic genealogy', desc: 'who trained the most doctoral students.' },
      { src: 'cards/card-06.png', title: 'Family trades', desc: 'the fields notable families cluster in.' },
      { src: 'cards/card-07.png', title: 'Serial founders', desc: 'who started the most organizations.' },
      { src: 'cards/card-08.png', title: 'Well-travelled athletes', desc: 'players who turned out for the most clubs.' },
      { src: 'cards/card-09.png', title: 'Sporting pipelines', desc: 'clubs that fielded the most notable players.' },
      { src: 'cards/card-10.png', title: 'Cross-field marriages', desc: 'couples who married across two different fields.' },
      { src: 'cards/card-11.png', title: 'Documentation by country', desc: 'where the public record is richest.' },
      { src: 'cards/card-12.png', title: 'Most common occupations', desc: 'the occupations with the most notable people.' },
      { src: 'cards/card-13.png', title: 'Most hold one office', desc: 'how many offices people hold, and the small group who held dozens.' },
    ],
  },
  {
    id: 'geography',
    title: 'Geography',
    note: 'Geography ranks reflect where the public record is rich, not where people were.',
    items: [
      { src: 'map/world-choropleth.png', title: 'The geography of documentation', desc: 'Wikidata coverage by country: an editing artefact, not a ranking of importance.' },
    ],
  },
  {
    id: 'work',
    title: 'People & work',
    note: 'How occupations pass between generations and open up over time.',
    items: [
      { src: 'inheritance/inheritance.png', title: 'Job inheritance', desc: 'parent-to-child job inheritance across three family structures.' },
      { src: 'pivots/pivots.png', title: 'Does the trade run in families?', desc: 'job inheritance by child birth cohort, adjusted for chance.' },
      { src: 'career/career.png', title: 'Occupations that co-occur', desc: 'a network of jobs held by the same people.' },
      { src: 'longevity/longevity.png', title: 'Longevity by occupation', desc: 'age at death by profession, adjusted for era.' },
      { src: 'gender/gender.png', title: 'When did each field open up?', desc: "women's share of each field, by birth decade." },
      { src: 'film/film.png', title: 'Who makes the movies', desc: 'a century of film people, who is still living, and the behind-the-camera gender gap.' },
    ],
  },
  {
    id: 'power',
    title: 'Power & institutions',
    note: 'Offices, boards and the lifespans of organizations.',
    items: [
      { src: 'politics/politics.png', title: 'Most office-holders hold one office', desc: 'offices held per person, and the small group who held dozens.' },
      { src: 'politics/politics_women.png', title: 'From 1 in 200 to 1 in 3', desc: "women's share of political office, by birth cohort." },
      { src: 'politics/politics_careers.png', title: 'Most political careers are short', desc: 'how long careers run, from first office to last.' },
      { src: 'boards/boards.png', title: 'Interlocking directorates', desc: 'organizations linked by the directors they share.' },
      { src: 'orgs/orgs.png', title: 'The rise and fall of organizations', desc: 'institutional lifespans across a thousand years.' },
    ],
  },
  {
    id: 'networks',
    title: 'Networks',
    note: 'Countries, clubs, languages and the shortest paths between people.',
    items: [
      { src: 'dualnat/dualnat.png', title: 'Countries linked by dual citizens', desc: 'where holding two passports connects nations.' },
      { src: 'clubs/clubs.png', title: "Football's shared-player network", desc: 'clubs linked by the players who turned out for both.' },
      { src: 'langs/langs.png', title: 'The language bridge', desc: 'languages linked by multilingual people.' },
      { src: 'langmatrix/langmatrix.png', title: 'The bilingualism matrix', desc: "how much of each language's record speaks another — over two denominators, and how often a second language is recorded at all." },
      { src: 'langfamily/langfamily.png', title: 'Family or contact?', desc: "how much of each language's bilingualism stays inside its own language family." },
      { src: 'langnative/langnative.png', title: 'Native or learned?', desc: 'how each language is held: mother tongue vs a language learned later.' },
      { src: 'paths/paths.png', title: 'Degrees of separation', desc: 'the shortest chains between unlikely pairs of figures.' },
    ],
  },
  {
    id: 'families',
    title: 'Families & curiosities',
    note: 'Lineages, polymaths and notable couples.',
    items: [
      { src: 'dynasty/medici.png', title: 'Medici', desc: '86 people across seven generations.' },
      { src: 'dynasty/tudor.png', title: 'Tudor', desc: '493 dated relatives.' },
      { src: 'dynasty/bourbon.png', title: 'Bourbon', desc: '329 dated relatives.' },
      { src: 'dynasty/kennedy.png', title: 'Kennedy', desc: '155 dated relatives.' },
      { src: 'dynasty/rockefeller.png', title: 'Rockefeller', desc: '108 dated relatives.' },
      { src: 'curiosities/polymaths.png', title: 'The polymath index', desc: 'the people with the most different occupations.' },
      { src: 'curiosities/agegap.png', title: 'Age gaps in couples', desc: 'the widest age gaps among married couples.' },
    ],
  },
];

// --- helpers ---------------------------------------------------------------
function pngSize(file) {
  const buf = fs.readFileSync(file);
  if (buf.length < 24 || buf.readUInt32BE(0) !== 0x89504e47) return null;
  return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
}

const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
const esc = (s) =>
  String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

let problems = 0;
const missing = (msg) => {
  console.error('  ✗ ' + msg);
  problems++;
};

// --- build tiles -----------------------------------------------------------
const lightboxes = [];
const sectionsHtml = SECTIONS.map((section) => {
  const tiles = section.items.map((item) => {
    const master = path.join(DOCS, item.src);
    if (!fs.existsSync(master)) {
      missing(`missing master: ${item.src}`);
      return '';
    }
    const size = pngSize(master);
    if (!size) missing(`not a readable PNG: ${item.src}`);

    const base = item.src.replace(/\.png$/i, '');
    const w480 = `thumbs/${base}-480.webp`;
    const w960 = `thumbs/${base}-960.webp`;
    if (!fs.existsSync(path.join(DOCS, w480))) missing(`missing thumbnail: ${w480} (run render-gallery-thumbs.js)`);

    // Content-hash the asset URLs: the masters and thumbnails are served with a
    // 7-day cache under stable filenames, so without this a returning visitor
    // (or the CDN) keeps the old art after a re-render.
    const version = crypto.createHash('sha1').update(fs.readFileSync(master)).digest('hex').slice(0, 8);
    const v480 = `${w480}?v=${version}`;
    const v960 = `${w960}?v=${version}`;
    const vMaster = `${item.src}?v=${version}`;

    const id = slug(item.src.replace(/\.png$/i, '').replace(/\//g, '-'));
    const alt = `${item.title}: ${item.desc.replace(/\.$/, '')}`;
    lightboxes.push(`  <div class="lightbox" id="img-${id}" role="dialog" aria-modal="true" aria-label="${esc(item.title)}">
    <a class="lightbox-back" href="#${section.id}">&#8592; Back to gallery</a>
    <a class="lightbox-home" href="${SITE}">humansmap.com &#8599;</a>
    <img src="${vMaster}" alt="${esc(alt)}" loading="lazy" decoding="async">
  </div>`);
    return `      <figure class="tile">
        <a class="tile-media" href="#img-${id}" title="View full size" aria-label="View ${esc(item.title)} full size">
          <img src="${v480}"
               srcset="${v480} 480w, ${v960} 960w"
               sizes="(max-width: 720px) 92vw, (max-width: 1080px) 46vw, (max-width: 1500px) 31vw, 400px"
               width="${size ? size.w : 1000}" height="${size ? size.h : 1000}"
               loading="lazy" decoding="async" alt="${esc(alt)}">
          <span class="zoom" aria-hidden="true">View full size</span>
        </a>
        <figcaption>
          <a class="tile-title" href="#img-${id}">${esc(item.title)}</a>
          <span class="tile-desc">${esc(item.desc)}</span>
        </figcaption>
      </figure>`;
  });

  return `    <section id="${section.id}" aria-labelledby="${section.id}-h">
      <h2 id="${section.id}-h">${esc(section.title)}</h2>
      <p class="section-note">${esc(section.note)}</p>
      <div class="grid">
${tiles.filter(Boolean).join('\n')}
      </div>
    </section>`;
}).join('\n\n');

const lightboxesHtml = lightboxes.join('\n');

const navHtml = SECTIONS.map(
  (s) => `        <a href="#${s.id}">${esc(s.title)}</a>`,
).join('\n');

const total = SECTIONS.reduce((n, s) => n + s.items.length, 0);

const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HumansMap data gallery</title>
<meta name="description" content="Shareable data stories from HumansMap: ${total} charts and cards on people, work, power and connection, built from Wikidata.">
<meta name="theme-color" content="#f9f8f4">
<meta name="color-scheme" content="light">
<meta name="robots" content="index, follow">
<!-- Set the canonical URL once the deploy path is fixed, e.g.:
<link rel="canonical" href="${SITE}/gallery/"> -->
<meta property="og:type" content="website">
<meta property="og:site_name" content="HumansMap">
<meta property="og:title" content="HumansMap data gallery">
<meta property="og:description" content="${total} charts and cards on people, work, power and connection, built from Wikidata.">
<link rel="icon" href="${FAVICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=Playfair+Display:wght@700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #f9f8f4; --panel: #ffffff; --text: #1c1510;
    --accent: #1b4d8e; --accent-hover: #153d73; --person: #bf6b28; --muted: #8a8278;
    --surface: #ece8de; --border: rgba(0, 0, 0, .15);
    --rule: rgba(28, 21, 16, .12); --tile-rule: rgba(28, 21, 16, .22);
    --shadow: 0 1px 2px rgba(28, 21, 16, .06), 0 6px 20px rgba(28, 21, 16, .05);
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 16px/1.6 "DM Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  a { color: var(--accent); }
  .skip { position: absolute; left: -999px; }
  .skip:focus { left: 12px; top: 12px; background: var(--text); color: var(--bg);
                padding: 8px 14px; border-radius: 6px; z-index: 20; }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 0 24px; }

  .topbar { background: var(--surface); border-bottom: 2px solid var(--border); }
  .topbar-inner { display: flex; align-items: center; justify-content: space-between;
                  gap: 16px; flex-wrap: wrap; padding-top: 10px; padding-bottom: 10px; }
  .brand { display: inline-flex; align-items: baseline; gap: 12px; text-decoration: none; }
  .brand-name { font-family: "Playfair Display", Georgia, serif; font-size: 23px;
                font-weight: 700; letter-spacing: -.5px; color: var(--accent); }
  .brand-tag { font-size: 11px; font-weight: 500; text-transform: uppercase;
               letter-spacing: .08em; color: var(--accent); }
  .brand:hover .brand-name, .brand:focus-visible .brand-name { color: var(--accent-hover); }
  .back { text-decoration: none; color: var(--accent); font-size: 13.5px;
          font-weight: 500; white-space: nowrap; }
  .back:hover, .back:focus-visible { text-decoration: underline; }
  header.masthead { padding: 44px 0 8px; }
  h1 { font-family: "Playfair Display", Georgia, serif; font-weight: 700;
       font-size: clamp(34px, 5.5vw, 56px); line-height: 1.05; letter-spacing: -.015em;
       margin: 18px 0 14px; max-width: 18ch; text-wrap: balance; }
  .lede { max-width: 62ch; color: #3a322b; margin: 0 0 14px; font-size: 18px; }
  .meta { color: var(--muted); font-size: 14px; margin: 0; }

  nav.toc { position: sticky; top: 0; z-index: 10; margin-top: 30px;
            background: rgba(249, 248, 244, .92); backdrop-filter: blur(8px);
            border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule); }
  nav.toc .wrap { display: flex; gap: 26px; overflow-x: auto; padding: 2px 24px 0;
                  scrollbar-width: none; }
  nav.toc .wrap::-webkit-scrollbar { display: none; }
  nav.toc a { position: relative; padding: 15px 0 13px; color: var(--muted);
              text-decoration: none; font-size: 12.5px; font-weight: 600;
              letter-spacing: .12em; text-transform: uppercase; white-space: nowrap;
              transition: color .18s; }
  nav.toc a::after { content: ""; position: absolute; left: 0; right: 0; bottom: -1px;
                     height: 2px; background: transparent; transition: background-color .2s; }
  nav.toc a:hover, nav.toc a:focus-visible { color: var(--text); }
  nav.toc a.is-active { color: var(--accent); }
  nav.toc a.is-active::after { background: var(--accent); }

  main { padding-bottom: 40px; }
  section { padding-top: 56px; scroll-margin-top: 64px; }
  h2 { font-family: "Playfair Display", Georgia, serif; font-weight: 700;
       font-size: clamp(24px, 3vw, 32px); line-height: 1.1; margin: 0 0 6px;
       text-wrap: balance; }
  .section-note { color: var(--muted); font-size: 15px; margin: 0 0 24px; max-width: 70ch; }

  .grid { display: grid; gap: 26px;
          grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr)); }
  @media (min-width: 1080px) {
    .grid { grid-template-columns: repeat(3, 1fr); }
    .grid > :only-child { grid-column: 1 / -1; }
  }
  figure.tile { margin: 0; background: var(--panel); border: 1px solid var(--tile-rule);
                border-radius: 12px; overflow: hidden; display: flex; flex-direction: column;
                box-shadow: var(--shadow); transition: transform .18s ease, box-shadow .18s ease; }
  figure.tile:hover { transform: translateY(-2px);
                      box-shadow: 0 2px 4px rgba(28,21,16,.07), 0 14px 34px rgba(28,21,16,.09); }
  /* Mount each chart on a matte so it has room inside the tile, with a hairline
     frame so full-bleed off-white cards still read as separate objects. */
  .tile-media { position: relative; display: block; background: var(--surface); padding: 14px;
                border-radius: 11px 11px 0 0; }
  .tile-media img { width: 100%; height: auto; display: block; border: 1px solid var(--tile-rule);
                    border-radius: 5px; box-shadow: 0 1px 3px rgba(28, 21, 16, .07); }
  .zoom { position: absolute; right: 24px; bottom: 24px; padding: 4px 9px; border-radius: 999px;
          background: var(--bg); border: 1px solid var(--rule); color: var(--text);
          font-size: 11.5px; font-weight: 600; opacity: 0; transform: translateY(3px);
          transition: opacity .18s ease, transform .18s ease; pointer-events: none; }
  .tile-media:hover .zoom, .tile-media:focus-visible .zoom { opacity: 1; transform: none; }
  .tile-media:focus-visible { outline: 3px solid var(--accent); outline-offset: -3px; }
  figcaption { padding: 15px 18px 17px; margin-top: auto;
               display: flex; flex-direction: column; gap: 3px; }
  .tile-title { font-weight: 600; font-size: 15px; color: var(--text); text-decoration: none; }
  .tile-title:hover, .tile-title:focus-visible { text-decoration: underline; text-underline-offset: 2px; }
  .tile-desc { color: var(--muted); font-size: 13.5px; line-height: 1.45; }

  /* Click a tile to open it full size. The overlay carries its own Back control
     so returning never depends on the browser chrome. */
  .lightbox { display: none; }
  .lightbox:target { display: flex; position: fixed; inset: 0; z-index: 60;
                     background: rgba(28, 21, 16, .95); padding: 68px 24px 24px;
                     align-items: center; justify-content: center; }
  .lightbox img { max-width: 100%; max-height: calc(100vh - 116px); width: auto; height: auto;
                  background: var(--bg); border-radius: 6px;
                  box-shadow: 0 18px 60px rgba(28, 21, 16, .5); }
  .lightbox-back { position: fixed; top: 20px; left: 20px;
                   display: inline-flex; align-items: center; gap: 8px;
                   padding: 10px 18px; border-radius: 999px;
                   background: var(--bg); color: var(--text); text-decoration: none;
                   font-size: 14px; font-weight: 600; }
  .lightbox-back:hover, .lightbox-back:focus-visible { background: var(--panel); }
  .lightbox-home { position: fixed; top: 20px; right: 20px;
                   display: inline-flex; align-items: center; gap: 6px;
                   padding: 10px 18px; border-radius: 999px;
                   background: var(--bg); color: var(--text); text-decoration: none;
                   font-size: 14px; font-weight: 600; }
  .lightbox-home:hover, .lightbox-home:focus-visible { background: var(--panel); }

  footer { border-top: 1px solid var(--rule); margin-top: 72px; padding: 34px 0 60px;
           color: var(--muted); font-size: 13.5px; }
  footer p { max-width: 78ch; margin: 0 0 8px; }
  footer a { font-weight: 500; }

  @media (max-width: 560px) {
    .wrap { padding: 0 16px; }
    header.masthead { padding: 28px 0 8px; }
    .brand-name { font-size: 19px; }
    h1 { margin-top: 14px; }
    .grid { gap: 16px; }
    figure.tile { border-radius: 10px; }
    .tile-media { border-radius: 9px 9px 0 0; padding: 10px; }
    .zoom { display: none; }
  }

  @media (max-width: 480px) {
    .brand-tag { display: none; }
    .brand-name { font-size: 18px; }
  }

  @media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    figure.tile { transition: none; }
  }
</style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>

<header class="topbar">
  <div class="wrap topbar-inner">
    <a class="brand" href="${SITE}">
      <span class="brand-name">Humans Map</span>
      <span class="brand-tag">Web of influence</span>
    </a>
    <a class="back" href="${SITE}">← Explore the map</a>
  </div>
</header>

<header class="masthead wrap">
  <h1>Data stories about people and how they connect.</h1>
  <p class="lede">A gallery of ${total} charts and cards built from the HumansMap dataset:
    records, dynasties, occupations, power and the networks that link them.
    Click any image to open it full size.</p>
  <p class="meta">Data: Wikidata (CC BY-SA) · <a href="${SITE}">humansmap.com</a></p>
</header>

<nav class="toc" aria-label="Gallery sections">
  <div class="wrap">
${navHtml}
  </div>
</nav>

<main id="main">
${sectionsHtml}
</main>

${lightboxesHtml}

<footer class="wrap">
  <p><strong>About the data.</strong> Everything here is computed from Wikidata
    (CC BY-SA) and published read-only. Geography and occupation rankings reflect
    where the public record is richest. They are an editing pattern, not a
    measure of importance. Samples are annotated on each chart.</p>
  <p>Explore the live graph at <a href="${SITE}">humansmap.com</a>.</p>
</footer>

<script src="gallery.js" defer></script>
</body>
</html>
`;

if (!fs.existsSync(path.join(DOCS, 'gallery.js'))) {
  missing('missing gallery.js (section nav + overlay close)');
}
if (problems) {
  console.error(`\n${problems} problem(s) found: gallery.html NOT written.`);
  process.exit(1);
}
fs.writeFileSync(OUT, html);
console.log(`wrote ${path.relative(process.cwd(), OUT)}: ${total} assets in ${SECTIONS.length} sections`);
