// Harris County Clerk probate cases filed in a date range, with parties and events.
//
// Usage: node probate_cases.js <from MM/DD/YYYY> <to MM/DD/YYYY> <out.json> [limit]
//
// The clerk's probate search (CourtSearch.aspx?CaseType=Probate) needs no login and no CAPTCHA.
// Results come 200 to a page in an ASP.NET ListView. Each row has two lazy panels the page loads
// with GET CourtCaseDetail.aspx?ID=<token>: one with the events (the "Case Initiated" comment carries
// the date of death as "DOD: MM/DD/YYYY"), one with the parties (Applicant name + mailing address +
// attorney, and the Deceased). Only decedent estates are kept: guardianships, wills deposited for
// safekeeping, trusts and the like have no heir selling a house.
const { chromium } = require('playwright');
const fs = require('fs');

const BASE = 'https://www.cclerk.hctx.net/Applications/WebSearch/';
const SKIP_TYPES = /GUARDIANSHIP|SAFE KEEPING|WILL DEPOSIT|POWER OF ATTORNEY|TRUST|SAFE DEPOSIT|MISCELLANEOUS|INCAPACITATED/;
const [from, to, out, limitArg] = process.argv.slice(2);
const limit = Number(limitArg) || Infinity;

const text = h => h.replace(/<br\s*\/?>/gi, '\n').replace(/<\/(td|tr|div|p)>/gi, '\n').replace(/<[^>]+>/g, ' ')
  .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/[ \t]+/g, ' ');

function parseParties(html) {
  // One <tr> per party: case, role, party name + address lines, attorney + address lines
  const rows = [];
  for (const tr of html.split(/<tr[^>]*>/i).slice(1)) {
    const tds = tr.split(/<td[^>]*>/i).slice(1).map(td => text(td).split('\n').map(s => s.trim()).filter(Boolean));
    if (tds.length < 3 || !/^\d+$/.test((tds[0] || [])[0] || '')) continue;
    rows.push({ role: (tds[1] || []).join(' '), party: tds[2] || [], attorney: tds[3] || [] });
  }
  return rows;
}

function parseEvents(html) {
  const rows = [];
  for (const tr of html.split(/<tr[^>]*>/i).slice(1)) {
    const tds = tr.split(/<td[^>]*>/i).slice(1).map(td => text(td).replace(/\s+/g, ' ').trim());
    if (tds.length >= 3 && /^\d+$/.test(tds[0])) rows.push({ date: tds[1], desc: tds[2], comment: tds[3] || '' });
  }
  return rows;
}

async function readPage(page) {
  return page.$$eval('a[id$="_btnSelect"]', as => as.map(a => {
    const tr = a.closest('tr');
    const cell = suffix => (tr.querySelector(`td[id$="_${suffix}"]`) || {}).innerText?.trim() || '';
    const panels = [...tr.querySelectorAll('a[onclick*="expand"]')].map(x => x.getAttribute('onclick').match(/expand\('([^']+)'/)[1]);
    return { case: a.innerText.trim(), court: cell('Td10'), file_date: cell('Td9'), status: cell('Td17'),
             type: cell('Td8'), subtype: cell('Td7'), style: cell('TdStyle'), events_id: panels[0], parties_id: panels[1] };
  }));
}

(async () => {
  if (!from || !to || !out) { console.log('usage: node probate_cases.js <from> <to> <out.json> [limit]'); process.exit(2); }
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(BASE + 'CourtSearch.aspx?CaseType=Probate', { waitUntil: 'domcontentloaded', timeout: 90000 });
  await page.fill('#ctl00_ContentPlaceHolder1_txtFrom', from);
  await page.fill('#ctl00_ContentPlaceHolder1_txtTo', to);
  await Promise.all([page.waitForLoadState('domcontentloaded'), page.click('#ctl00_ContentPlaceHolder1_btnSearchCase')]);
  await page.waitForSelector('a[id$="_btnSelect"]', { timeout: 60000 }).catch(() => null);
  const total = Number(((await page.innerText('body')).match(/(\d+) Record\(s\) Found/) || [])[1] || 0);
  console.log(`filed ${from} - ${to}: ${total} probate cases listed`);

  // Every page of the ListView ("Next" postback swaps the rows in place)
  const all = [];
  for (let n = 1; ; n++) {
    const rows = await readPage(page);
    all.push(...rows);
    const next = page.locator('a[href*="DataPagerLisViewCases1"]', { hasText: /^Next$/ });
    if (all.length >= total || !(await next.count())) break;
    const first = rows[0] && rows[0].case;
    await next.first().click();
    await page.waitForFunction(f => { const a = document.querySelector('a[id$="_btnSelect"]'); return a && a.innerText.trim() !== f; }, first, { timeout: 60000 });
  }
  const estates = all.filter(r => !SKIP_TYPES.test(r.type) && /DECEASED/i.test(r.style));
  console.log(`${all.length} rows read${total && all.length < total ? ' (SHORT of the listed count)' : ''}; ${estates.length} decedent estates`);

  const keep = estates.slice(0, limit);
  for (const [i, r] of keep.entries()) {
    const get = id => page.evaluate(async id => (await fetch('CourtCaseDetail.aspx?ID=' + encodeURIComponent(id))).text(), id);
    try {
      r.events = parseEvents(await get(r.events_id));
      r.parties = parseParties(await get(r.parties_id));
    } catch (e) { r.error = e.message.slice(0, 120); }
    delete r.events_id; delete r.parties_id;
    if ((i + 1) % 25 === 0) console.log(`  ${i + 1} cases read`);
  }
  fs.writeFileSync(out, JSON.stringify(keep, null, 1));
  console.log(`done: ${keep.length} cases -> ${out}`);
  await browser.close();
})();
