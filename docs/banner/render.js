// usage: node render.js <page.html> <out.png> [width] [height] [scale]
const { chromium } = require('playwright');
(async () => {
  const [page = 'banner.html', out = 'social-preview.png', w = 1280, h = 640, sc = 1] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: +w, height: +h }, deviceScaleFactor: +sc });
  await p.goto('file://' + require('path').resolve(page));
  await p.evaluate(() => document.fonts.ready);
  await p.screenshot({ path: out });
  await b.close();
})();
