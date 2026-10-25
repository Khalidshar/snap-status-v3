// Renders the approved "روّق مع خالد" personal frame to a transparent 1080x1920 PNG.
// Run only when the design changes, then update assets/brand/rawwag/manifest.json:
//   node scripts/render_brand_frame.js
// Chromium shapes the Arabic text; the pinned FFmpeg drawtext mis-shapes Aref Ruqaa,
// so the bot composes this pinned image and never draws brand text itself.
const path = require("path");
const { chromium } = require("playwright");

(async () => {
  const dir = path.resolve(__dirname, "../assets/brand/rawwag");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 } });
  await page.goto("file://" + path.join(dir, "frame_personal.html"));
  await page.evaluate(() => document.fonts.ready);
  const loaded = await page.evaluate(() =>
    ["100px ruqaa", "600 42px cairo"].every((font) => document.fonts.check(font))
  );
  if (!loaded) throw new Error("Brand fonts did not load");
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(dir, "frame_personal.png"), omitBackground: true });
  await browser.close();
})();
