/**
 * Capture screenshots and flow recordings of the running HumansMap site.
 *
 * Usage (from scripts/marketing/):
 *   source env.sh
 *   node capture-flows.js [outDir]        # default /tmp/humansmap-media
 *
 * Produces:
 *   <outDir>/screens/*.png          2x stills (downscale to ~1600px before commit)
 *   <outDir>/videos/*.webm          WebM flow recordings (Playwright recordVideo)
 *
 * Poster frames for the videos can be produced with the ffmpeg that ships with
 * Playwright, e.g.:
 *   $(ls -d ~/.cache/ms-playwright/ffmpeg-*/ffmpeg-linux | tail -1) \
 *     -ss 3 -i explore-flow.webm -frames:v 1 explore-flow-poster.png
 *
 * The site must be reachable (this defaults to production). Override with
 * BASE_URL. The onboarding tour is suppressed for clean frames.
 */
const { chromium } = require('playwright');
const os = require('os');
const fs = require('fs');
const path = require('path');

const BASE = process.env.BASE_URL || 'https://humansmap.com';
const OUT = process.argv[2] || '/tmp/humansmap-media';
const SHOTS = path.join(OUT, 'screens');
const VIDS = path.join(OUT, 'videos');
const VIEW = { width: 1440, height: 900 };
const CHROME = process.env.CHROME_PATH ||
  `${os.homedir()}/.cache/ms-playwright/chromium-1155/chrome-linux/chrome`;

fs.mkdirSync(SHOTS, { recursive: true });
fs.mkdirSync(VIDS, { recursive: true });

const wait = (page, ms) => page.waitForTimeout(ms);

async function newPage(browser, extra = {}) {
  const context = await browser.newContext({
    viewport: VIEW,
    deviceScaleFactor: 2,
    ...extra,
  });
  // Suppress the first-visit guided tour so screenshots are uncluttered.
  await context.addInitScript(() => {
    try { localStorage.setItem('hm_onboarded', '1'); } catch {}
  });
  return { context, page: await context.newPage() };
}

async function screenshot(browser, name, url, action, { settle = 5000, context: ctxOpts } = {}) {
  const { context, page } = await newPage(browser, ctxOpts);
  try {
    await page.goto(BASE + url, { waitUntil: 'networkidle', timeout: 60000 });
    await wait(page, settle);
    if (action) await action(page);
    await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
    console.log('shot', name);
  } catch (error) {
    console.error('shot failed:', name, error.message);
  } finally {
    await context.close();
  }
}

async function record(browser, name, url, action) {
  const { context, page } = await newPage(browser, {
    recordVideo: { dir: VIDS, size: VIEW },
  });
  const video = page.video();
  try {
    await page.goto(BASE + url, { waitUntil: 'networkidle', timeout: 60000 });
    await wait(page, 4000);
    if (action) await action(page);
    await wait(page, 700);
    console.log('video', name);
  } catch (error) {
    console.error('video failed:', name, error.message);
  } finally {
    await context.close();
  }
  const source = await video.path();
  const target = path.join(VIDS, `${name}.webm`);
  try { fs.renameSync(source, target); } catch {}
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });

  await screenshot(browser, 'home-leaderboard', '/', null, { settle: 6000 });
  await screenshot(browser, 'explore-graph', '/person/Q303', null, { settle: 7000 });
  await screenshot(browser, 'person-card', '/person/Q303', async (page) => {
    const box = await (await page.$('canvas'))?.boundingBox();
    if (box) {
      await page.mouse.click(box.x + box.width * 0.70, box.y + box.height * 0.30);
      await wait(page, 2500);
    }
  }, { settle: 7000 });
  await screenshot(browser, 'feed', '/?feed', null, { settle: 4000 });
  await screenshot(browser, 'play-setup', '/?play', null, { settle: 3000 });
  await screenshot(browser, 'play-round', '/?play', async (page) => {
    await page.getByRole('button', { name: /play/i }).last().click({ timeout: 5000 }).catch(() => {});
    await wait(page, 4000);
  }, { settle: 3000 });
  await screenshot(browser, 'gallery', '/gallery/', null, { settle: 4000 });
  await screenshot(browser, 'mobile', '/person/Q303', null, {
    settle: 7000,
    context: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
  });

  await record(browser, 'explore-flow', '/person/Q303', async (page) => {
    const box = await (await page.$('canvas')).boundingBox();
    await wait(page, 3000);
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5, { steps: 20 });
    await wait(page, 1000);
    await page.mouse.click(box.x + box.width * 0.70, box.y + box.height * 0.30);
    await wait(page, 3000);
    await page.mouse.move(box.x + box.width * 0.58, box.y + box.height * 0.62, { steps: 20 });
    await wait(page, 2000);
  });
  await record(browser, 'feed-flow', '/?feed', async (page) => {
    await page.mouse.move(720, 500);
    for (let i = 0; i < 9; i++) { await page.mouse.wheel(0, 480); await wait(page, 600); }
    await wait(page, 1500);
  });
  await record(browser, 'game-flow', '/?play', async (page) => {
    await page.getByRole('button', { name: /play/i }).last().click({ timeout: 5000 }).catch(() => {});
    await wait(page, 3500);
    await page.mouse.click(720, 600);
    await wait(page, 3000);
    await page.mouse.click(720, 640);
    await wait(page, 2500);
  });

  await browser.close();
  console.log('done ->', OUT);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
