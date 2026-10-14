# Media

Screenshots and recorded flows of the running HumansMap service, captured with
Playwright (headless Chromium) against <https://humansmap.com>. Everything is
real product output — no mock data.

```
screens/   Still images (PNG). Stills are 1600 px wide; the mobile shot is 780 px.
videos/    Flow recordings (WebM, VP8) plus a PNG poster frame for each.
```

| File | What it shows |
|---|---|
| `screens/explore-graph.png` | Explore mode: a person's network on the graph canvas |
| `screens/person-card.png` | A selected node's detail card |
| `screens/home-leaderboard.png` | Explore home: the notability leaderboard grid |
| `screens/play-setup.png` | Play mode: challenge filters |
| `screens/play-round.png` | Play mode: a "Who is it?" round |
| `screens/feed.png` | Feed mode: a generated connection card |
| `screens/gallery.png` | The public data gallery |
| `screens/mobile.png` | Explore mode on a phone viewport |
| `videos/explore-flow.webm` | Opening a network and a person card |
| `videos/feed-flow.webm` | Scrolling the feed |
| `videos/game-flow.webm` | Starting and playing a trivia round |

People and organizations come from Wikidata (CC0); portraits come from Wikimedia
Commons (per-image licenses, attributed in the app). See
[`../../THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md).

## Regenerating

The capture script lives at `scripts/marketing/capture-flows.js` and uses the
same Playwright install as the marketing renderers:

```bash
cd scripts/marketing
npm install          # once — installs Playwright
source env.sh        # LD_LIBRARY_PATH + headless-shell path for this host
node capture-flows.js /tmp/humansmap-media
```

Screenshots are written at 2× and should be downscaled to ~1600 px before
committing (see the comments in the script).
