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

## Prospect Score vs DataSift's temperature (2026-11-11)

DataSift's own temperature (snowflake / flame, `hotness` 0-5) and the property status mean real seller
interest AFTER a conversation: the caller sets them. The pipeline never writes either (operator,
2026-11-11), and the connector's safety brakes refuse any write to them (`crm/guard.py`, code
`native_temperature_or_status`), like skip trace. The score computed before any contact goes to two custom
fields shared by every list (group "Prospect"):

| List | Prospect Score | Prospect Reasons |
|---|---|---|
| Harris - Foreclosure | = Deal Grade (Hot, Warm, Not Viable, Needs Data) | = Deal Notes |
| Harris - Probate | = Lead Temperature (Hot, Warm, Cold) | = Temperature Notes |
| Harris - Senior Equity | = Lead Temperature (Hot, Warm, Cold) | = Temperature Notes |

Deal Grade and Lead Temperature are still written as before (the probate Hot / Warm presets filter on
Lead Temperature). Until 2026-11-11 the grader and the probate / senior uploads set DataSift's temperature
(Hot 5, Warm 3) before any contact; `temperature_cleanup.py` puts those back to 0, only where nobody has
talked to the owner yet (see its docstring). Contract, for the record: `PATCH /property/{uuid}/
{"hotness": n}` is accepted and ignored; the app writes `POST /property/{uuid}/hotness/ {"hotness": n}`.

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
- A middle initial breaks a tie only on a rare name (`MAX_NAMESAKES = 5`): a common first + last name
  had 21 namesakes and the one "M" with a house need not be him.
- Address keys carry the ZIP and a second street word: `100 OAK HILL` vs `100 OAK RIDGE`.
- Keep `&` in owner names: `SMITH JOHN & MARY` read MARY as John's middle name.
- HCAD hides the owner on ~37,600 protected accounts (`CURRENT OWNER`); a decedent's home can be
  one, found through the rentals that mail their tax bills to it.
- Trusts are written FIRST LAST (`JOHN A SMITH JR TRUST`) and indexed separately.

**Left out:** low / no-confidence matches, and surviving-spouse homes (operator, 2026-11-01): the
HCAD owner line names a co-owner who is not a decedent in the batch and the home is owner-occupied.
A rental the couple owned stays a lead. 12 homes a week.

**Lead Temperature** (Hot / Warm / Cold, reasons in Temperature Notes): points for an out-of-state
heir (+2) or one outside the Houston area (+1); nobody the estate relies on living there (rental,
decedent living elsewhere, or sole owner gone and the heir elsewhere) (+1); 2+ Harris properties
(+1); no will (+1); death 12+ months before the filing (+1); built before 1980 (+1). Hot = 4+ and
inside the Market Finder buy box (value, sq ft); Warm = 2-3 or 4+ outside it; Cold = 0-1. Also written
to Prospect Score / Prospect Reasons (since 2026-11-11 DataSift's own temperature is never set).
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

**Filter presets** (folder **Harris Probate**, 2026-11-01): **Harris - Probate Hot** and **Harris - Probate
Warm** = list Harris - Probate AND Lead Temperature Hot / Warm, in the same `custom_fields` grammar as the
foreclosure preset. Verified by count: 5 Hot, 29 Warm (19 Cold) of the 53 leads, equal to the CSV. A count
taken right after an upload can read low (the earlier 26 Warm was 6 records not yet indexed).

## Senior equity (stage 3): owner 75+, free and clear, Market Check

Leads to wholesale to investors: an older owner of a paid-off house the local investors buy. List
**Harris - Senior Equity**, folder **Harris Senior Equity** with two presets: the whole list ("4 criterios")
and **Priority** (vacant, or built 1975 or earlier with one owner on the deed).

```bash
python3 src/harris/hcad_senior.py work/hcad --download              # over-65 homesteads from HCAD's exemption amounts
python3 src/harris/senior_sweep.py work/hcad/idx.tsv work/senior/sweep.json   # SiftMap search, read-only
python3 src/harris/market_datasift.py work/senior/market.json        # ZIP + county Market Finder metrics
(cd src/harris && python3 senior_to_datasift.py ../../work/senior/sweep.json ../../work/hcad/idx.tsv \
    ../../work/hcad/senior_accts.json ../../work/senior/market.json ../../work/senior/leads.csv)
(cd src/harris && python3 senior_upload.py ../../work/senior/leads.csv)            # dry run
(cd src/harris && python3 senior_upload.py ../../work/senior/leads.csv --presets --commit)
```

**Senior** = HCAD over-65 homestead AND SiftMap owner age 75+. HCAD blanks the age and disability codes
(OVR / DIS are on zero accounts) but not the amounts: an over-65 homestead carries the district's OVR
amount on top of the residence homestead, folded into the RES row (`hcad_senior.py`). Most districts
give over-65 and disability the same amount ("Over-65 or Disabled"); 42 do not, and there 95% are
over-65. 28 of 40 probate decedents' homes carry the flag.

**SiftMap search** (`map.reisift.io/properties/search/`, the user JWT) is free and adds nothing to the
CRM. Keys verified by count delta: `extra_equity_percent_min`, `extra_owner_age_min`,
`extra_year_built_max`, `beds_min/max`, `preset_owner_occupied`, `preset_vacant`. Ignored silently:
`equity_percent_min`, `year_built_max`, `sqft_*`, `baths_*`, `owner_age_min`, `preset_senior`.
`result_index` is the ROW OFFSET: paging it by 1 walked one row at a time (516 calls, 765 rows).

**Market Check** (the operator's four ZIP criteria, every reference stored on the lead):
1. ZIP median sale price below the ZIP median home value, and inside the middle 60%
2. ZIP days on market at or under the county's (Market Finder county metrics, 58 days)
3. lead value inside the middle 60% = Harris single-family values without the cheapest and dearest 20%
   ($201,883 - $432,887 on the 2026 roll); free-and-clear leads also capped at $300,000
4. ZIP investor purchases between the median ZIP (53) and the 90th percentile (162): solid, not the
   most contested

First build (2026-11-01): 23,575 SiftMap homes -> 2,918 leads in 17 ZIPs (693 Priority, 2,225 Standard;
Hot 258, Warm 1,833, Cold 827). Left out: market check 15,487, no HCAD over-65 homestead 4,735, not on
the HCAD roll 384, sq ft outside the buy box 51. Property address and Estimated Value are SiftMap's
(HCAD appends townhome building/unit codes like `100 MAIN ST 10/45`; DataSift stores it as
`# 10`). Temperature: +2 vacant, +1 each for 80+, single owner, built 1975 or earlier, HCAD
"Over-65" (not "or Disabled"), ZIP selling 20%+ faster than the county.

## Trestle phone scores (trestle.py, trestle_score.py; 2026-11-12)

Paid: Phone Validation $0.015 + Litigator Check $0.005 = $0.020 per number (Trestle self-service pricing,
checked 2026-11-12). The API key is the environment variable `TRESTLE_API_KEY` (cloud environment settings),
sent as `x-api-key` exactly as Trestle documents; it is never printed or written. The network-secret form only
offers "Bearer token" or username/password and Trestle refused both, while the same key worked with a plain
`x-api-key` header. A run without the variable stops before querying anything.

`trestle_score.py` scores the phones on the call pipeline (Priority 1 / Blitz Active), skipping numbers marked
DNC / wrong / dead, numbers scored less than 180 days ago, numbers marked correct that were already scored, and
leads also on Harris - Senior Equity. A simulation (and the cost) comes first; the paid queries run only after
the brakes approve the plan, under `TRESTLE_MAX_QUERIES` (default 400 per run). `--limit N` caps numbers.

| Result | Phone | Lead |
|---|---|---|
| score 81-100 / 61-80 / 41-60 / 40 or less | Dial First / Second / Third / Drop | |
| litigator | Drop + tag Litigator | tag "Litigator - Do Not Contact" |
| no number 61+ (once every live number is scored), none marked correct | | tag "Needs Re-Skip" |
| every scored number | tag "Trestle Scored" | field "Trestle Phone Scores": `number=score@date` (`L` = litigator, `?` = invalid) |

`phone_priority.py` (daily) reads that field: scored numbers take their tier from the score, a number marked
correct stays Dial First, unscored numbers keep the old rule.
