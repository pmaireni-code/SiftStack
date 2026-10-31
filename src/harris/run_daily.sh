#!/usr/bin/env bash
# Daily Harris run: Starting Bid, postponements and Foreclosure Status for every lead in DataSift.
#
#   bash src/harris/run_daily.sh [--dry-run]
#
# Reads the leads from the DataSift list itself (no local state), pulls today's Auction.com / Xome
# listings, recomputes dates and status, and writes back only the leads that changed.
set -euo pipefail
cd "$(dirname "$0")/../.."
COMMIT="--commit"; [[ "${1:-}" == "--dry-run" ]] && COMMIT=""
W=work/daily_$(date +%Y%m%d)
mkdir -p "$W"

python3 src/harris/harris_from_datasift.py "$W/leads.csv"
node src/harris/bid_sources.js "$W/bids.json"
cd src/harris
python3 harris_bid_updater.py "../../$W/leads.csv" "../../$W/bids.json" "../../$W/refreshed.csv" --datasift $COMMIT | tee "../../$W/daily.log"
python3 - "../../$W/refreshed.csv" <<'EOF'
import csv, sys
from collections import Counter
rows = list(csv.DictReader(open(sys.argv[1])))
st = Counter(r["Foreclosure Status"] for r in rows)
urgent = sorted((r for r in rows if r["Foreclosure Status"] == "Urgent"), key=lambda r: r["Contract Deadline"])
bids = [r for r in rows if r.get("Starting Bid")]
print("RESULT:", ", ".join(f"{k} {v}" for k, v in st.most_common()), f"| {len(bids)} with a Starting Bid")
for r in urgent[:15]:
    print(f"  URGENT {r['Property Street Address']}: deadline {r['Contract Deadline']}, auction {r['Auction Date']}, bid {r.get('Starting Bid') or 'TBD'}")
EOF
