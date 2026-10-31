# Harris County, TX foreclosure pipeline

Buy-before-auction leads from Harris County trustee sale notices, delivered to DataSift with the
dates that decide whether the deal is still workable. Everything here runs in the cloud with no
paid service: the County Clerk posts the notices free and without a CAPTCHA, HCAD publishes the
whole property roll, and Auction.com / Xome publish the opening bids.

```
notices (County Clerk)  ->  OCR  ->  match to HCAD  ->  DataSift CSV  ->  daily refresh
harris_notices.js          harris_ocr.py  harris_match.py  harris_to_datasift.py  bid_sources.js + harris_bid_updater.py
```

## Run

```bash
# 1. Notices for a sale month (first Tuesday). Re-runs only fetch new ones.
node src/harris/harris_notices.js 2026 12 work/notices

# 2. OCR the scanned PDFs (Tesseract, 4 workers, ~2 s a notice)
python3 src/harris/harris_ocr.py work/notices work/notices/ocr

# 3. HCAD index (download ~210 MB once a week, build ~20 s)
python3 src/harris/hcad_index.py work/hcad --download

# 4. Match notices to properties, then the DataSift upload CSV
python3 src/harris/harris_match.py work/notices/ocr work/hcad/idx.tsv work/match.csv
python3 src/harris/harris_to_datasift.py work/match.csv work/notices/meta.json work/leads.csv

# 5. Every day until the sale: opening bids, postponements, status
node src/harris/bid_sources.js work/bids.json
python3 src/harris/harris_bid_updater.py work/leads.csv work/bids.json work/leads.csv --datasift          # dry run
python3 src/harris/harris_bid_updater.py work/leads.csv work/bids.json work/leads.csv --datasift --commit
```

Needs: Node + Playwright (Chromium), Python 3.9+, `tesseract`, `pdftoppm` (poppler).
DataSift calls read `DATASIFT_EMAIL` / `DATASIFT_PASSWORD` from the environment and never print them.
Network: `www.cclerk.hctx.net`, `download.hcad.org`, `www.auction.com`, `graph.auction.com`, `www.xome.com`,
`apiv2.reisift.io`.

**Never commit `work/`.** Notices, HCAD rows and lead CSVs carry owners' names and addresses, and this repo is public.

## What each lead carries

| Field | Source |
|---|---|
| Property / owner / mailing address, value, sq ft, year built, Parcel ID | HCAD roll |
| Original Loan Amount, Loan Date, Lender, Trustee Firm, Auction Platform | the notice (OCR) |
| Auction Date | notice grid sale date; moves forward on a postponement |
| Contract Deadline | Auction Date minus `DEADLINE_DAYS` (7), back to a weekday |
| Foreclosure Status | Active / Urgent / Deadline Passed / Postponed / Auction Passed, recomputed daily |
| Starting Bid | Auction.com Opening Bid, else Xome Est. Opening Bid, for the same sale date |

Tags: `Harris`, `Foreclosure`, `Auction MM/DD/YYYY`, plus `Verify Owner` (HCAD owner not named in the
notice), `Estate` (HCAD owner is "ESTATE OF"), `Bid Posted`.

## Matching (harris_match.py)

Order: HCAD account number printed in the notice, then the street address when the notice gives one,
then lot / block / subdivision against HCAD's legal description, then the borrower's name (condos).
Measured on 20 December 2026 notices: 17 matched, 15 with the HCAD owner's surname found in the notice.
Rules each learned from a wrong match:

- HCAD writes owners LAST FIRST. Only the owner's **surname** agreeing with the borrower counts, and only
  as a tie-break: a shared first name put Balmoral Sec 8's borrower on a Balmoral Sec 6 house.
- A different **section number** is a different subdivision and is penalized, however similar the text.
- HCAD keeps partial lots as `LT 2 LESS N 1 FT BLK 7`, so legal descriptions are keyed on the lot and
  block numbers, not the string.
- OCR truncates subdivisions (`FAIRGR`); a 6-character prefix hit counts.

## Opening bids (bid_sources.js)

Texas notices never state the opening bid; the lender sets its credit bid a few days before the sale.
Measured 2026-10-31 on the 11/03 sale: Auction.com listed 145 Harris trustee sales, 85 with an opening
bid; Xome listed the 12/01 sale already, with the bid still TBD. Both are read through the same requests
their public search pages make; neither site's robots.txt disallows the listing pages used.
A listing for a LATER sale date is a postponement and never back-fills this month's bid.

## Contract Deadline

`DEADLINE_DAYS = 7` (set by the operator). Servicers have up to 7 business days to send a payoff
statement, so a contract signed near the deadline should already have the payoff requested.
