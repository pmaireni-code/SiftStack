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

# 5. Into DataSift: one record first, read back, then the rest (re-runs attach, never duplicate notes)
python3 src/harris/harris_upload.py work/leads.csv --limit 1 --commit
python3 src/harris/harris_upload.py work/leads.csv --commit

# 6. Every day until the sale: opening bids, postponements, status
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

## DataSift contract (verified live 2026-10-31)

- Custom field values read back from `GET /api/internal/property/{uuid}/custom-field/`, keyed by
  `custom_field.uuid`; dates are written as `YYYY-MM-DD`, selects as the option uuid.
- Native fields `estimate_value`, `year`, `sqft`, `apn` and `auction_date` take a `PATCH` on the record.
- `add-notes` APPENDS and the record's `notes` cannot be edited over the API, so the uploader skips a
  note whose notice doc id is already on the record.

## Switching CRM (src/harris/crm/)

Everything that talks to a CRM lives in `crm/`. The rest of the pipeline only calls three methods
(`crm/base.py`): `upsert_lead(row, commit)`, `read_leads(list_name)`, `update_lead(id, values, add_tags)`.
`HARRIS_CRM` picks the connector (default `datasift`). To move to another CRM (Odoo, a white label):
copy `crm/TEMPLATE.py`, implement the three methods, register it in `crm/__init__.py`, set
`HARRIS_CRM=<name>`, then dry-run `harris_upload.py` and `run_daily.sh --dry-run`. Notices, OCR,
HCAD matching, dates, status and bids do not change. The lead columns are documented in `crm/base.py`.

DataSift specifics learned live (`crm/datasift.py`): a record short of data (company owner, missing
mailing line) is filed as `incomplete`, not `clean`, so reading a list must query both types (27 of
335 Harris leads were `incomplete`); clearing a custom field takes `""`, while `null` is rejected and
the whole update is dropped with it.

## Deal Grade (harris_grade.py + market_datasift.py)

Run daily inside `run_daily.sh`, after the status refresh. Every run first re-reads DataSift's Market
Finder for Harris (`/market-finder/buy-box/recommendations/` = the app's "Recommended Filters", and
`/market-finder/paginated/?geoGroup=zip` = investor activity per ZIP), then re-grades every open lead
(all but Auction Passed) and writes only the leads whose grade changed.

| Deal Grade | Rule |
|---|---|
| Not Viable | equity under 25% |
| Needs Data | no equity estimate |
| Hot | equity 40%+, single family, inside the buy box, ZIP not in the bottom third for investor buys, no Verify Equity |
| Warm | the rest at 25%+; Deal Notes says what kept it from Hot |

Buy box = DataSift's recommendation with two operator-approved changes: no year-built cap (post-2010
is 21% of Harris single-family stock and 35% of these leads) and baths up to 4. Verify Equity: a loan
taken in the last 5 years for 85%+ of the value. Flood: FEMA NFHL zone at the record's coordinates in
Deal Notes, plus the `Flood Zone` tag in the 100-year floodplain; never a grade change. FEMA's point
query must send the geometry as JSON (`{"x":..,"y":..,"spatialReference":{"wkid":4326}}`); the
`x,y` shorthand times out with a 500. Network: `hazards.fema.gov`.

## DataSift filter preset

Folder **Harris Foreclosure**, preset **Harris - Hot (Active, Urgent, Postponed)**: list Harris -
Foreclosure, Deal Grade Hot, Foreclosure Status Active / Urgent / Postponed (95 leads on 2026-11-01).
Custom fields filter as `"custom_fields": {"<field uuid>": ["<option uuid>", ...]}` inside `must`:
options of one field are OR, fields are AND. Verified by count delta (308 -> 90 clean); a bare
`"<field uuid>": [...]` key or `customFields` is silently ignored, and a list instead of a dict 400s.
A preset carries no Clean/Incomplete split: the Records view applies that, so 5 of the 95 sit under
Incomplete.

## DataSift Property Temperature (the snowflake / flame)

DataSift's own temperature marker is MANUAL by design (help article "Property Temperature
Explained"); every record starts cold (`hotness` 0). The grader sets it ONCE, when a lead is graded
for the first time and its temperature is still 0: Hot -> 5, Warm -> 3. Not Viable and Needs Data stay
cold. After that it is never touched again, so the operator's own call wins (operator's choice,
2026-11-01). Contract: `PATCH /property/{uuid}/ {"hotness": n}` is accepted and silently ignored;
the app saves it with `POST /api/internal/property/{uuid}/hotness/ {"hotness": n}`. Filter in the UI:
Records -> Add new filter block -> Deal Temperature.

## Probate (stage 2, live 2026-11-01: 53 leads uploaded and read back)

New probate cases -> the decedent's Harris property -> DataSift list **Harris - Probate**, with the
APPLICANT (would-be executor / administrator) as the contact. All free sources.

```bash
node src/harris/probate_cases.js 10/26/2026 10/30/2026 work/probate/cases.json       # clerk, by file date
python3 src/harris/probate_match.py work/probate/cases.json work/hcad/idx.tsv work/probate/match.csv
python3 src/harris/market_datasift.py work/probate/market.json                        # buy box (read-only)
(cd src/harris && python3 probate_to_datasift.py ../../work/probate/match.csv ../../work/probate/leads.csv \
    --market ../../work/probate/market.json --excluded ../../work/probate/excluded.csv)
(cd src/harris && python3 probate_upload.py ../../work/probate/leads.csv)             # dry run: schema plan + every lead
(cd src/harris && python3 probate_upload.py ../../work/probate/leads.csv --limit 1 --commit)   # one first, read it back

# Every Monday, all of the above for cases filed in the last 9 days (scheduled task):
bash src/harris/run_weekly_probate.sh [--dry-run]
```

**The clerk** (`CourtSearch.aspx?CaseType=Probate`, no login, no CAPTCHA) lists 200 cases a page.
Each row lazy-loads two panels with `GET CourtCaseDetail.aspx?ID=<token>`: events (the "Case
Initiated" comment carries `DOD: MM/DD/YYYY`) and parties (Applicant with mailing address and
attorney + phone; Deceased, with an address on ~10% of cases). About 147 cases a week, 128 of them
decedent estates once guardianships, wills for safekeeping, trusts and the like are dropped.

**Matching** (measured on 128 cases filed 10/26-10/30/2026): 59 cases give a property (high or
medium), 17 are ambiguous, 52 have no Harris home in the decedent's name. A PERSON on the roll is a
name plus a mailing address, so name hits are grouped by mailing address and a group is chosen by
an anchor (the decedent's or applicant's address) or by being the only one. Rules each learned
from a wrong match:
- An address match needs the decedent's FIRST name too: a decedent's address was her son's house.
- A middle initial breaks a tie only on a rare name (`MAX_NAMESAKES = 5`): CHARLES M JOHNSON had
  21 namesakes and the one "M" with a house need not be him.
- Address keys carry the ZIP and a second street word: `2806 KINGS FOREST` vs `2806 KINGS RETREAT`.
- Keep `&` in owner names: `SCHROEDER ALBERT & MARYANN` read MARYANN as Albert's middle name.
- HCAD hides the owner on ~37,600 protected accounts (`CURRENT OWNER`); a decedent's home can be
  one, found through the rentals that mail their tax bills to it.
- Trusts are written FIRST LAST (`VITO J GAUDIANO JR TRUST`) and indexed separately.

**Left out:** low / no-confidence matches, and surviving-spouse homes (operator, 2026-11-01): the
HCAD owner line names a co-owner who is not a decedent in the batch and the home is owner-occupied.
A rental the couple owned stays a lead. 12 homes a week.

**Lead Temperature** (Hot / Warm / Cold, reasons in Temperature Notes): points for an out-of-state
heir (+2) or one outside the Houston area (+1); nobody the estate relies on living there (rental,
decedent living elsewhere, or sole owner gone and the heir elsewhere) (+1); 2+ Harris properties
(+1); no will (+1); death 12+ months before the filing (+1); built before 1980 (+1). Hot = 4+ and
inside the Market Finder buy box (value, sq ft); Warm = 2-3 or 4+ outside it; Cold = 0-1. Sets
DataSift's own temperature on first upload (Hot 5, Warm 3) only while the record is still cold.
First week: Hot 5, Warm 29, Cold 19.

**CRM:** probate columns map to custom fields Decedent Name, Executor, Executor Attorney, Probate
Open Date, Date of Death, Probate Case Number, Probate Type, Lead Temperature (select) and
Temperature Notes, in a "Probate" group that `probate_upload.py --schema --commit` creates. A house
already in DataSift keeps its owner (`Keep Owner On Existing`): only list, tags, fields and the
note are added, since `create_property` would otherwise PATCH the owner. The note is skipped when
its `Note Key` (`probate case <n>`) is already on the record. `find_lead` searches clean AND
incomplete records.

First live upload (2026-11-01, cases filed 10/26-10/30): schema created and read back, 1 lead read back
field by field, then the other 52. Verified by reading the whole list back: 53 records (52 clean, 1
incomplete), every probate field filled, Lead Temperature equal to the CSV, DataSift temperature Hot 5 /
Warm 3 / Cold 0, no duplicated note. The list search lags the writes by up to a minute: 48 of 53 showed
right after the upload, 53 a minute later. `POST /custom-fields/group/` takes `{"label", "entity_type"}`;
`{"title"}` 400s.
