// Pull every Harris County auction listing (with its opening bid when published) from
// Auction.com and Xome, normalized to one shape. Read-only; uses the same requests the
// public search pages make.
//
// Usage: node bid_sources.js <out.json>
const { chromium } = require('playwright');
const fs = require('fs');

const get = (o, ...ks) => ks.reduce((a, k) => (a && typeof a === 'object' ? a[k] : undefined), o);
const money = s => (s == null || s === '' ? null : Number(String(s).replace(/[^0-9.]/g, '')) || null);

async function auctionCom(browser) {
  const p = await browser.newPage();
  let payload = null;
  p.on('response', async r => {
    if (!r.url().includes('graph.auction.com/graphql')) return;
    try {
      const j = await r.json();
      const d = get(j, 'data', 'seek_listings_from_filters');
      if (d && d.content && (!payload || d.content.length > payload.content.length)) payload = d;
    } catch {}
  });
  await p.goto('https://www.auction.com/residential/TX/harris-county/', { waitUntil: 'domcontentloaded', timeout: 90000 });
  for (let i = 0; i < 30 && !payload; i++) await p.waitForTimeout(1000);
  await p.waitForTimeout(3000);
  await p.close();
  if (!payload) throw new Error('auction.com: listing payload never arrived');
  return payload.content
    .filter(x => /harris/i.test((x.formatted_address || [])[1] || ''))
    .map(x => {
      const shown = get(x, 'listing_summary', 'show_opening_bid');
      const bid = get(x, 'auction', 'starting_bid');
      const [city, rest] = String((x.formatted_address || [])[1] || '').split(',').map(s => s.trim());
      return {
        source: 'Auction.com', listing_id: String(x.listing_id), product: get(x, 'listing_configuration', 'product_type'),
        street: (x.formatted_address || [])[0] || '', city, zip: ((rest || '').match(/\d{5}/) || [''])[0],
        sale_date: (get(x, 'auction', 'start_date') || '').slice(0, 10),
        bid: shown && bid != null ? Number(bid) : null, bid_type: 'Opening Bid',
        url: `https://www.auction.com/details/${x.listing_id}`,
      };
    });
}

async function xome(browser) {
  const p = await browser.newPage();
  let body = null;
  p.on('request', r => { if (/\/v1\/listing\/search$/.test(r.url()) && !body) body = JSON.parse(r.postData() || '{}'); });
  await p.goto('https://www.xome.com/auctions/tx/harris-county', { waitUntil: 'domcontentloaded', timeout: 90000 });
  for (let i = 0; i < 30 && !body; i++) await p.waitForTimeout(1000);
  if (!body) throw new Error('xome: search request never fired');
  const out = [];
  for (let offset = 0; offset < 2000; offset += 48) {
    const res = await p.evaluate(async b => {
      const r = await fetch('/api/listing-proxy/v1/listing/search', { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify(b) });
      return r.json();
    }, { ...body, offset, limit: 48 });
    const find = o => Array.isArray(o) && o.length && typeof o[0] === 'object' ? o
      : (o && typeof o === 'object' ? Object.values(o).map(find).find(Boolean) : null);
    const rows = find(res) || [];
    for (const x of rows) {
      const ai = x.auctionInfo || {};
      out.push({
        source: 'Xome', listing_id: String(x.listingID), product: x.auctionSaleTransactionTypeDisplayString,
        street: x.streetAddress || '', city: x.city || '', zip: String(x.zipCode || x.postalCode || x.zip || ''),
        sale_date: (ai.auctionStartDate || '').slice(0, 10),
        bid: ai.showBidAmount ? money(ai.bidAmount || ai.startingBid) : null, bid_type: (ai.bidType || '').trim() || 'Opening Bid',
        url: `https://www.xome.com/auctions/${x.listingNumber || x.listingID}`,
      });
    }
    if (rows.length < 48) break;
    await p.waitForTimeout(1500);
  }
  await p.close();
  return out;
}

(async () => {
  const browser = await chromium.launch();
  const all = [];
  for (const [name, fn] of [['Auction.com', auctionCom], ['Xome', xome]]) {
    try { const rows = await fn(browser); console.log(`${name}: ${rows.length} Harris listings, ${rows.filter(r => r.bid).length} with a bid`); all.push(...rows); }
    catch (e) { console.log(`${name}: FAILED ${e.message}`); }
  }
  await browser.close();
  fs.writeFileSync(process.argv[2] || 'bids.json', JSON.stringify({ fetched_at: new Date().toISOString(), listings: all }, null, 1));
})();
