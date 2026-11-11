#!/usr/bin/env bash
# Daily Harris run: Starting Bid, postponements, Foreclosure Status and Deal Grade for every lead,
# then probate leads assigned to their user 14 days after the death (probate_assign.py), and the
# dialing order of every phone on the call pipeline (phone_priority.py).
#
#   bash src/harris/run_daily.sh [--dry-run]
#
# Reads the leads from the CRM list itself (no local state), pulls today's Auction.com / Xome
# listings, recomputes dates and status, then refreshes DataSift's Market Finder data (buy box +
# investor activity per ZIP) BEFORE re-grading every open lead. Writes back only what changed.
# Production profile with step isolation (steps.sh): a failed or blocked step is logged and the next
# step still runs; the run log lines print at the end and the exit is 1 if any step failed.
set -euo pipefail
cd "$(dirname "$0")/../.."
RUN_NAME=daily; source src/harris/steps.sh
COMMIT="--commit"; [[ "${1:-}" == "--dry-run" ]] && COMMIT=""
W=work/daily_$(date +%Y%m%d)
mkdir -p "$W"
run_banner "$COMMIT"

read_leads()   { python3 src/harris/harris_from_crm.py "$W/leads.csv"; }
bid_listings() { node src/harris/bid_sources.js "$W/bids.json"; }
bid_updater()  { cd src/harris && python3 harris_bid_updater.py "../../$W/leads.csv" "../../$W/bids.json" "../../$W/refreshed.csv" --crm $COMMIT | tee "../../$W/daily.log"; }
market()       { cd src/harris && python3 market_datasift.py "../../$W/market.json"; }
grade()        { cd src/harris && python3 harris_grade.py "../../$W/market.json" "../../$W/graded.csv" --crm $COMMIT | tee "../../$W/grade.log" | { grep -vE "^  (OK|DRY)" || true; }; }
probate()      { cd src/harris && python3 probate_assign.py $COMMIT | tee "../../$W/probate_assign.log"; }
phones()       { cd src/harris && python3 phone_priority.py $COMMIT | tee "../../$W/phone_priority.log"; }
summary() {
python3 - "$W/graded.csv" <<'PY'
import csv, sys
from collections import Counter
rows = list(csv.DictReader(open(sys.argv[1])))
st = Counter(r["Foreclosure Status"] for r in rows)
grades = Counter(r["Deal Grade"] for r in rows if r["Foreclosure Status"] != "Auction Passed")
bids = [r for r in rows if r.get("Starting Bid")]
print("RESULT:", ", ".join(f"{k} {v}" for k, v in st.most_common()), f"| {len(bids)} with a Starting Bid")
print("GRADES:", ", ".join(f"{k} {v}" for k, v in grades.most_common()))
urgent = sorted((r for r in rows if r["Foreclosure Status"] == "Urgent"), key=lambda r: (r["Deal Grade"] != "Hot", r["Contract Deadline"]))
for r in urgent[:20]:
    print(f"  URGENT [{r['Deal Grade'] or '-'}] {r['Property Street Address']}: deadline {r['Contract Deadline']}, "
          f"auction {r['Auction Date']}, bid {r.get('Starting Bid') or 'TBD'}")
PY
}

step "read leads"      -- read_leads
step "bid listings"    -- bid_listings
step "bid updater"     needs "$W/leads.csv" "$W/bids.json" -- bid_updater
step "market finder"   -- market
step "deal grade"      needs "$W/market.json" -- grade
step "probate assign"  -- probate        # Maireni Peguero, 14 days after the death
step "phone priority"  -- phones         # Priority 1 / Blitz Active
step "summary"         needs "$W/graded.csv" -- summary
run_footer
