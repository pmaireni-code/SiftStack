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
# Production profile with step isolation (steps.sh), like run_weekly.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
RUN_NAME=weekly_probate; source src/harris/steps.sh
SINCE_DAYS="${SINCE_DAYS:-9}"
COMMIT="--commit"; [[ "${1:-}" == "--dry-run" ]] && COMMIT=""
W=work/probate_weekly_$(date +%Y%m%d)
mkdir -p "$W"
FROM=$(date -d "-$SINCE_DAYS days" +%m/%d/%Y)
TO=$(date +%m/%d/%Y)
run_banner "$COMMIT"

cases()  { node src/harris/probate_cases.js "$FROM" "$TO" "$W/cases.json" | { grep -v "cases read$" || true; }; }
hcad()   { python3 src/harris/hcad_index.py work/hcad --download; }
match()  { python3 src/harris/probate_match.py "$W/cases.json" work/hcad/idx.tsv "$W/match.csv"; }
market() { python3 src/harris/market_datasift.py "$W/market.json"; }
leads()  { cd src/harris && python3 probate_to_datasift.py "../../$W/match.csv" "../../$W/leads.csv" \
             --market "../../$W/market.json" --excluded "../../$W/excluded.csv" | tee "../../$W/leads.log"; }
upload() { local rc=0
           (cd src/harris && python3 -u probate_upload.py "../../$W/leads.csv" $COMMIT) > "$W/upload.log" 2>&1 || rc=$?
           grep -vE "^  (OK|NEW|EXISTS)|^    |^      " "$W/upload.log" || true
           return $rc; }
result() {
python3 - "$W/leads.csv" "$W/upload.log" <<'PY'
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
PY
}

echo "== probate cases filed $FROM - $TO"
step "probate cases" -- cases
if [[ ! -s "$W/cases.json" ]] || [[ "$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$W/cases.json")" == "0" ]]; then
  echo "RESULT: no new probate cases"; run_footer "$COMMIT"; exit $?
fi
step "hcad index"    -- hcad
step "match"         needs work/hcad/idx.tsv -- match
step "market finder" -- market
step "leads"         needs "$W/match.csv" "$W/market.json" -- leads
step "upload"        needs "$W/leads.csv" -- upload
step "result"        needs "$W/leads.csv" -- result
run_footer "$COMMIT"
