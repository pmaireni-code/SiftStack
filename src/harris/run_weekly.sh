#!/usr/bin/env bash
# Weekly Harris run: new trustee sale notices -> HCAD -> DataSift.
#
#   bash src/harris/run_weekly.sh [--dry-run]
#
# Stateless by design (each scheduled run starts in a fresh container): downloads only notices FILED
# in the last SINCE_DAYS for the next sale months, refreshes the HCAD index, matches, and uploads.
# Re-uploading an existing property attaches to its record and never duplicates its note, so the
# overlap between weeks is harmless. Needs DATASIFT_EMAIL / DATASIFT_PASSWORD in the environment.
# Production profile with step isolation (steps.sh): a failed or blocked step is logged, the steps
# after it still run (those whose input is missing say so), and the run log lines print at the end.
set -euo pipefail
cd "$(dirname "$0")/../.."
RUN_NAME=weekly; source src/harris/steps.sh
SINCE_DAYS="${SINCE_DAYS:-9}"
COMMIT="--commit"; [[ "${1:-}" == "--dry-run" ]] && COMMIT=""
W=work/weekly_$(date +%Y%m%d)
mkdir -p "$W"
run_banner "$COMMIT"

command -v tesseract >/dev/null || { apt-get install -y -q tesseract-ocr >/dev/null 2>&1 || (apt-get update -q >/dev/null && apt-get install -y -q tesseract-ocr >/dev/null); }
command -v pdftoppm >/dev/null || apt-get install -y -q poppler-utils >/dev/null

# Sale months still open to new notices: this month if its first Tuesday is ahead, plus the next two
read -r -a MONTHS <<<"$(python3 - <<'EOF'
from datetime import date, timedelta
t = date.today()
def first_tuesday(y, m):
    d = date(y, m, 1)
    return d + timedelta(days=(1 - d.weekday()) % 7)
out, y, m = [], t.year, t.month
for _ in range(3):
    if first_tuesday(y, m) >= t:
        out.append(f"{y}:{m}")
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
if len(out) < 3:
    out.append(f"{y}:{m}")
print(" ".join(out[:3]))
EOF
)"

notices() {
  for ym in "${MONTHS[@]}"; do
    node src/harris/harris_notices.js "${ym%%:*}" "${ym##*:}" "$W/notices" --since-days "$SINCE_DAYS" | { grep -v "downloaded$" || true; }
  done
}
hcad()     { python3 src/harris/hcad_index.py work/hcad --download; }
ocr()      { python3 src/harris/harris_ocr.py "$W/notices" "$W/ocr" >/dev/null; }
match()    { python3 src/harris/harris_match.py "$W/ocr" work/hcad/idx.tsv "$W/match.csv"
             python3 src/harris/harris_to_datasift.py "$W/match.csv" "$W/notices/meta.json" "$W/leads.csv"; }
bids()     { node src/harris/bid_sources.js "$W/bids.json"
             cd src/harris && python3 harris_bid_updater.py "../../$W/leads.csv" "../../$W/bids.json" "../../$W/leads.csv"; }
upload()   { python3 -u src/harris/harris_upload.py "$W/leads.csv" $COMMIT | tee "$W/upload.log" | { grep -vE "^  OK" || true; }
             test "${PIPESTATUS[0]}" -eq 0; }
result()   { echo "RESULT: $(grep -c . "$W/leads.csv" | awk '{print $1-1}') leads from $(ls "$W"/notices/*.pdf | wc -l) new notices; $(grep -cE '^  FAIL' "$W/upload.log" || true) failed"; }

echo "== notices filed in the last $SINCE_DAYS days for sales ${MONTHS[*]}"
step "notices" -- notices
if ! ls "$W"/notices/*.pdf >/dev/null 2>&1; then echo "RESULT: no new notices this week"; run_footer; exit $?; fi
step "hcad index" -- hcad
step "ocr"        -- ocr
step "match"      needs work/hcad/idx.tsv -- match
step "bids"       needs "$W/leads.csv" -- bids
step "upload"     needs "$W/leads.csv" -- upload
step "result"     needs "$W/leads.csv" -- result
run_footer
