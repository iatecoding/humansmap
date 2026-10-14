/**
 * Render lightweight WebP thumbnails for the marketing gallery.
 *
 * Usage (from scripts/marketing/):
 *   node render-gallery-thumbs.js [marketingDir]
 *
 * Defaults: ../docs/marketing
 *
 * For every PNG under the marketing tree it writes responsive WebP variants to
 * <marketingDir>/thumbs/<same path>.webp at the widths listed in WIDTHS. The
 * gallery page uses these for its grid, so visitors never download the full
 * 2–4 MB masters unless they open an image.
 */
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');
const os = require('os');

const CHROME =
  process.env.CHROME_PATH ||
  os.homedir() +
    '/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell';

const WIDTHS = [480, 960];
const QUALITY = 80;
const EXCLUDE = [/[/\\]thumbs[/\\]/, /insight-cards/i];

function walk(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(p));
    else if (/\.png$/i.test(entry.name) && !EXCLUDE.some((re) => re.test(p))) out.push(p);
  }
  return out;
}

(async () => {
  const root = path.resolve(process.argv[2] || '../../docs/marketing');
  const files = walk(root);
  const browser = await chromium.launch({
    executablePath: CHROME,
    args: ['--no-sandbox', '--allow-file-access-from-files'],
  });
  const page = await browser.newPage();

  for (const file of files) {
    const rel = path.relative(root, file).replace(/\.png$/i, '.webp');
    const dest = path.join(root, 'thumbs', rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });

    const dataUrl = 'data:image/png;base64,' + fs.readFileSync(file).toString('base64');
    const variants = await page.evaluate(
      async ({ dataUrl, widths, quality }) => {
        const img = new Image();
        img.src = dataUrl;
        await img.decode();
        const results = [];
        for (const w of widths) {
          if (w > img.naturalWidth) continue;
          const h = Math.round((img.naturalHeight / img.naturalWidth) * w);
          const canvas = document.createElement('canvas');
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, w, h);
          const blob = await new Promise((r) => canvas.toBlob(r, 'image/webp', quality));
          const buf = new Uint8Array(await blob.arrayBuffer());
          results.push({ w, bytes: Array.from(buf) });
        }
        return results;
      },
      { dataUrl, widths: WIDTHS, quality: QUALITY },
    );

    const made = [];
    for (const v of variants) {
      const variantPath = dest.replace(/\.webp$/, `-${v.w}.webp`);
      fs.writeFileSync(variantPath, Buffer.from(v.bytes));
      made.push(`${v.w}px`);
    }
    console.log(`${rel}: ${made.join(', ')}`);
  }

  await browser.close();
  console.log(`done — ${files.length} images -> ${path.join(root, 'thumbs')}`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
