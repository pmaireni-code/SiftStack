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
set -euo pipefail
cd "$(dirname "$0")/../.."
COMMIT="--commit"; [[ "${1:-}" == "--dry-run" ]] && COMMIT=""
W=work/daily_$(date +%Y%m%d)
mkdir -p "$W"

python3 src/harris/harris_from_crm.py "$W/leads.csv"
node src/harris/bid_sources.js "$W/bids.json"
cd src/harris
python3 harris_bid_updater.py "../../$W/leads.csv" "../../$W/bids.json" "../../$W/refreshed.csv" --crm $COMMIT | tee "../../$W/daily.log"

echo "== Market Finder refresh + Deal Grade"
python3 market_datasift.py "../../$W/market.json"
python3 harris_grade.py "../../$W/market.json" "../../$W/graded.csv" --crm $COMMIT | tee "../../$W/grade.log" | grep -vE "^  (OK|DRY)" || true

echo "== Probate: assign to Maireni Peguero 14 days after the death"
# Independent of the foreclosure steps above: a failure here is reported, never fatal
python3 probate_assign.py $COMMIT | tee "../../$W/probate_assign.log" || echo "WARN: probate assignment failed (see probate_assign.log)"

echo "== Phone priority on the call pipeline (Priority 1 / Blitz Active)"
python3 phone_priority.py $COMMIT | tee "../../$W/phone_priority.log" || echo "WARN: phone priority failed (see phone_priority.log)"

python3 - "../../$W/graded.csv" <<'EOF'
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
EOF
