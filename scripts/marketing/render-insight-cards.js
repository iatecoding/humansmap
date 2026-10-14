/**
 * Render the HumansMap marketing insight card sheet to individual PNGs.
 *
 * Usage (from scripts/marketing/):
 *   node render-insight-cards.js [input.html] [outDir]
 *
 * Defaults: ../docs/marketing/insight-cards.html -> ../docs/marketing/cards/
 * Each `.card` element in the sheet is screenshotted at 2x for retina-crisp
 * images suitable for Reddit / X.
 */
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const CHROME =
  process.env.CHROME_PATH ||
  require('os').homedir() +
    '/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell';

(async () => {
  const htmlPath = path.resolve(process.argv[2] || '../../docs/marketing/insight-cards.html');
  const outDir = path.resolve(process.argv[3] || '../../docs/marketing/cards');
  if (!fs.existsSync(htmlPath)) {
    console.error(`Card sheet not found: ${htmlPath}`);
    process.exit(1);
  }
  fs.mkdirSync(outDir, { recursive: true });

  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage({
    viewport: { width: 1200, height: 1200 },
    deviceScaleFactor: 2,
  });
  await page.goto('file://' + htmlPath, { waitUntil: 'networkidle', timeout: 60000 });
  // Wait for webfonts so Playfair/DM Sans are actually applied.
  await page.evaluate(() => (document.fonts ? document.fonts.ready : Promise.resolve()));
  await page.waitForTimeout(1200);

  const cards = page.locator('.card');
  const n = await cards.count();
  for (let i = 0; i < n; i++) {
    const name = `card-${String(i + 1).padStart(2, '0')}.png`;
    await cards.nth(i).screenshot({ path: path.join(outDir, name) });
    console.log('wrote', path.join(outDir, name));
  }
  console.log(`rendered ${n} cards -> ${outDir}`);
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
