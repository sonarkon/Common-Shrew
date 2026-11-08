const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 640 }, deviceScaleFactor: 1 });
  await p.goto('file://' + process.cwd() + '/banner.html');
  await p.evaluate(() => document.fonts.ready);
  await p.screenshot({ path: process.argv[2] || 'social-preview.png' });
  await b.close();
})();
