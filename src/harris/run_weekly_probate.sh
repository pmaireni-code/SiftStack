#!/usr/bin/env bash
# Weekly Harris PROBATE run: new probate cases -> the decedent's HCAD property -> DataSift "Harris - Probate".
#
#   bash src/harris/run_weekly_probate.sh [--dry-run]
#
# Stateless like run_weekly.sh (each scheduled run starts in a fresh container): reads the cases FILED in
# the last SINCE_DAYS (default 9, so consecutive Mondays overlap), refreshes the HCAD index, matches,
# scores the Lead Temperature against today's Market Finder buy box and uploads with read-back. A house
# already in DataSift keeps its owner and never gets the same case note twice, so the overlap is harmless.
# Separate from run_weekly.sh on purpose: a probate failure never touches the foreclosure run.
set -euo pipefail
cd "$(dirname "$0")/../.."
SINCE_DAYS="${SINCE_DAYS:-9}"
COMMIT="--commit"; [[ "${1:-}" == "--dry-run" ]] && COMMIT=""
W=work/probate_weekly_$(date +%Y%m%d)
mkdir -p "$W"
FROM=$(date -d "-$SINCE_DAYS days" +%m/%d/%Y)
TO=$(date +%m/%d/%Y)

echo "== probate cases filed $FROM - $TO"
node src/harris/probate_cases.js "$FROM" "$TO" "$W/cases.json" | grep -v "cases read$"
if [[ "$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$W/cases.json")" == "0" ]]; then
  echo "RESULT: no new probate cases"; exit 0
fi

echo "== HCAD index"
python3 src/harris/hcad_index.py work/hcad --download
echo "== match"
python3 src/harris/probate_match.py "$W/cases.json" work/hcad/idx.tsv "$W/match.csv"
echo "== Market Finder buy box + leads"
python3 src/harris/market_datasift.py "$W/market.json"
(cd src/harris && python3 probate_to_datasift.py "../../$W/match.csv" "../../$W/leads.csv" \
    --market "../../$W/market.json" --excluded "../../$W/excluded.csv") | tee "$W/leads.log"
echo "== upload ${COMMIT:-(dry run)}"
(cd src/harris && python3 -u probate_upload.py "../../$W/leads.csv" $COMMIT) > "$W/upload.log" 2>&1 || true
grep -vE "^  (OK|NEW|EXISTS)|^    |^      " "$W/upload.log" || true

python3 - "$W/leads.csv" "$W/upload.log" <<'EOF'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
log = open(sys.argv[2]).read()
fails, warns = log.count("\n  FAIL"), log.count("\n  WARN")
temps = {t: sum(r["Lead Temperature"] == t for r in rows) for t in ("Hot", "Warm", "Cold")}
print(f"RESULT: {len(rows)} probate leads ({', '.join(f'{k} {v}' for k, v in temps.items())}); {fails} failed, {warns} warnings")
for r in rows:
    if r["Lead Temperature"] == "Hot":
        print(f"  HOT {r['Property Street Address']}, {r['Property ZIP Code']} | executor {r['Executor']} "
              f"({r['Mailing City']} {r['Mailing State']}) | {r['Temperature Notes']}")
EOF
