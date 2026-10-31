// Download Harris County Clerk foreclosure notices (trustee sale notices) for one sale month.
//
// Usage: node harris_notices.js <year> <month> <out_dir> [limit]
//
// The clerk's FRCL search needs no login and no CAPTCHA. Results come 38 to a page in an ASP.NET
// grid; each Doc ID link streams the scanned PDF as a download. Two passes, because downloading
// from the grid page breaks its pager postback: first walk every page collecting the links, then
// download them from a second tab in the same session. Already-downloaded notices are skipped, so
// a re-run only fetches what is new. Writes <out_dir>/meta.json (doc id + grid cells: sale date,
// file date, page count) next to the PDFs.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = 'https://www.cclerk.hctx.net/Applications/WebSearch/';
const [year, month, outDir, limitArg] = process.argv.slice(2);
const limit = Number(limitArg) || Infinity;

async function readGrid(page, lastFirst) {
  // The pager postback swaps the grid in asynchronously: poll until it shows a different page
  for (let attempt = 0; attempt < 15; attempt++) {
    let rows = [];
    try {
      rows = await page.$$eval('a[href*="ViewECdocs"]', as => as.map(a => ({
        doc: a.innerText.trim(), href: a.getAttribute('href'),
        cells: [...a.closest('tr').querySelectorAll('td')].map(t => t.innerText.trim()),
      })));
    } catch (e) { /* mid-navigation */ }
    if (rows.length && rows[0].doc !== lastFirst) return rows;
    await page.waitForTimeout(2000);
  }
  return [];
}

(async () => {
  if (!year || !month || !outDir) { console.log('usage: node harris_notices.js <year> <month> <out_dir> [limit]'); process.exit(2); }
  fs.mkdirSync(outDir, { recursive: true });
  const metaPath = path.join(outDir, 'meta.json');
  const meta = fs.existsSync(metaPath) ? JSON.parse(fs.readFileSync(metaPath, 'utf8')) : [];
  const known = new Set(meta.map(m => m.doc));

  const browser = await chromium.launch();
  const context = await browser.newContext({ acceptDownloads: true });
  const grid = await context.newPage();
  await grid.goto(BASE + 'FRCL_R.aspx', { waitUntil: 'domcontentloaded', timeout: 90000 });
  await grid.check('#ctl00_ContentPlaceHolder1_rbtlDate_0');  // search by Sale Date
  await grid.selectOption('#ctl00_ContentPlaceHolder1_ddlYear', year);
  await grid.waitForTimeout(2000);  // the year postback refills the month list
  await grid.selectOption('#ctl00_ContentPlaceHolder1_ddlMonth', month);
  await Promise.all([grid.waitForLoadState('domcontentloaded'), grid.click('#ctl00_ContentPlaceHolder1_btnSearch')]);
  await grid.waitForTimeout(5000);
  const total = Number(((await grid.innerText('body')).match(/(\d+) Row\(s\) Found/) || [])[1] || 0);
  console.log(`sale ${month}/${year}: ${total || '?'} notices listed`);

  // Pass 1: every row of every page
  const all = [];
  let pageNo = 1, lastFirst = null;
  while (all.length < limit) {
    const rows = await readGrid(grid, lastFirst);
    if (!rows.length) { console.log(`page ${pageNo}: grid did not load, stopping the walk`); break; }
    lastFirst = rows[0].doc;
    all.push(...rows);
    // Next page: its numbered link, or the "..." link that opens the next block of ten
    const next = grid.locator(`a[href*="'Page$${pageNo + 1}'"]`).first();
    if (!(await next.count())) break;
    await next.click();
    pageNo++;
  }
  const todo = all.slice(0, limit);
  console.log(`walked ${pageNo} page(s): ${all.length} rows${total && all.length < Math.min(total, limit) ? ' (SHORT of the listed count)' : ''}`);

  // Pass 2: download from a separate tab in the same session
  const dlPage = await context.newPage();
  let fetched = 0;
  for (const r of todo) {
    if (known.has(r.doc) && fs.existsSync(path.join(outDir, `${r.doc}.pdf`))) continue;
    try {
      const [dl] = await Promise.all([
        dlPage.waitForEvent('download', { timeout: 60000 }),
        dlPage.goto(BASE + r.href).catch(() => null),  // the response is a download, so goto "fails" by design
      ]);
      await dl.saveAs(path.join(outDir, `${r.doc}.pdf`));
      if (!known.has(r.doc)) { meta.push({ doc: r.doc, cells: r.cells }); known.add(r.doc); }
      if (++fetched % 25 === 0) { fs.writeFileSync(metaPath, JSON.stringify(meta, null, 1)); console.log(`  ${fetched} downloaded`); }
    } catch (e) { console.log(`fail ${r.doc}: ${e.message.slice(0, 80)}`); }
  }
  fs.writeFileSync(metaPath, JSON.stringify(meta, null, 1));
  console.log(`done: ${fetched} new notices, ${meta.length} in ${outDir}`);
  await browser.close();
})();
