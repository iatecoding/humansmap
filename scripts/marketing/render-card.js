/**
 * Render a single large marketing graphic to PNG.
 *
 * Usage (from scripts/marketing/):
 *   node render-card.js <input.html|url> <out.png> [selector] [scale]
 *
 * The card sheet in each marketing HTML is a `.card` element sized in CSS px
 * (e.g. 1600×1000); we screenshot it at `scale`× device pixel ratio so the PNG
 * is print/feed crisp (1600×1000 @2x -> 3200×2000). Defaults: selector ".card",
 * scale 2.
 */
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');
const os = require('os');

const CHROME =
  process.env.CHROME_PATH ||
  os.homedir() +
    '/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell';

(async () => {
  const [, , input, output, selector = '.card', scaleArg = '2'] = process.argv;
  if (!input || !output) {
    console.error('usage: node render-card.js <input.html|url> <out.png> [selector] [scale]');
    process.exit(1);
  }
  const scale = Number(scaleArg) || 2;
  const url = /^https?:|^file:/.test(input) ? input : 'file://' + path.resolve(input);

  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage({
    viewport: { width: 2000, height: 1400 },
    deviceScaleFactor: scale,
  });
  await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
  await page.evaluate(() => (document.fonts ? document.fonts.ready : Promise.resolve()));
  await page.waitForTimeout(800);

  const el = page.locator(selector).first();
  await el.waitFor({ state: 'visible', timeout: 20000 });
  fs.mkdirSync(path.dirname(path.resolve(output)), { recursive: true });
  await el.screenshot({ path: path.resolve(output) });
  console.log(`wrote ${output} (${selector} @${scale}x)`);
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
