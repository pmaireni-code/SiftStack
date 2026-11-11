# Shared by the production run_*.sh scripts (sourced, not run). Production profile + step isolation.
#
#   RUN_NAME=daily; source "$(dirname "$0")/steps.sh"   (from the repo root, after `cd`)
#   step "bid updater" needs "$W/leads.csv" -- python3 ...      # one step; a failure never stops the next
#   run_footer                                                    # the run's log lines; exit 1 if a step failed
#
# SIFT_PROFILE=scheduled: these scripts ARE production (a scheduled task, or a person running one by
# hand). Writes go live without the manual dry-run, over 500 leads they go in batches, over 5,000 a step
# stops without writing (crm/guard.py). --dry-run (no --commit) stays the way to simulate.
export SIFT_PROFILE=scheduled
export SIFT_RUN_ID="${SIFT_RUN_ID:-${RUN_NAME:-run}-$(date -u +%Y%m%dT%H%M%SZ)}"
FAILED_STEPS=()

run_banner() {  # $1 = "--commit" or ""
  if [[ -n "${1:-}" ]]; then
    echo "################################################################################"
    echo "# MODO REAL / LIVE: this run WRITES to DataSift (production profile, run ${SIFT_RUN_ID})."
    echo "# Each step prints '[guard] <step>: LIVE - plans to touch N leads' before its first write."
    echo "# To simulate instead: add --dry-run to the same command"
    echo "################################################################################"
  else
    echo "== DRY RUN (--dry-run): nothing is written to DataSift (run ${SIFT_RUN_ID})"
  fi
}

# step NAME [needs FILE...] -- COMMAND...   Runs COMMAND in a subshell with errexit; its exit code is
# recorded (crm/runlog.py --step-end) and the run moves on to the next step either way.
step() {
  local name="$1"; shift
  local need=()
  if [[ "${1:-}" == "needs" ]]; then
    shift
    while [[ "$1" != "--" ]]; do need+=("$1"); shift; done
  fi
  [[ "${1:-}" == "--" ]] && shift
  echo "== $name"
  local f rc=0
  for f in "${need[@]}"; do
    if [[ ! -s "$f" ]]; then
      echo "  SKIPPED: needs $f, which an earlier step did not produce"
      rc=4
    fi
  done
  if [[ $rc -eq 0 ]]; then
    set +e
    ( set -eo pipefail; export SIFT_STEP="$name"; "$@" )
    rc=$?
    set -e
  fi
  (cd src/harris && SIFT_STEP="$name" python3 -m crm.runlog --step-end "$name" "$rc") || true
  if [[ $rc -ne 0 ]]; then
    FAILED_STEPS+=("$name")
    echo "  STEP FAILED: $name (exit $rc). The remaining steps still run."
  fi
  return 0
}

# run_footer "$COMMIT": the run's own line, the log lines, and in a live run (a) the push of those lines to
# the run-logs branch (a failed push is reported, never fails the run) and (c) an ALERT when a step was
# blocked or failed, for the scheduled task to email right away. A --dry-run publishes and alerts nothing.
run_footer() {
  local live="${1:-}"
  (cd src/harris && python3 -m crm.runlog --run-end "${RUN_NAME:-run}" "${#FAILED_STEPS[@]}" "${FAILED_STEPS[*]:-}") || true
  echo "== run log ($SIFT_RUN_ID)"
  (cd src/harris && python3 - "$SIFT_RUN_ID" <<'PY'
import json, sys
from crm import runlog
rows = runlog.read(run_id=sys.argv[1])
rows.sort(key=lambda r: (r.get("status") not in ("blocked", "error"), r.get("ts_start") or ""))
for r in rows:
    flag = "BLOCKED" if r.get("status") == "blocked" else "ERROR" if r.get("status") == "error" else "ok"
    print(f"  {flag:<7} {r.get('step') or r.get('task')}: {r.get('task')} | {r.get('mode')} | written {r.get('leads_written')}"
          f" | batches {'yes' if r.get('batched') else 'no'}" + (f" | {r.get('error')}" if r.get("error") else "")
          + (f" | would be blocked: {r.get('would_block')}" if r.get("would_block") else ""))
print("RUN LOG LINES:")
for r in rows:
    print(json.dumps(r))
PY
  ) || true
  if [[ -n "$live" ]]; then
    (cd src/harris && python3 -m crm.runlog --publish "$SIFT_RUN_ID") || echo "WARN: run log push failed (see above); the business steps are not affected"
    (cd src/harris && python3 -m crm.runlog --alert "$SIFT_RUN_ID" "../../work/alert/$SIFT_RUN_ID") || true
  fi
  # Exit status of the whole run: 0 all steps ok; 3 one or more steps BLOCKED by the safety brakes on purpose
  # (nothing written by them) and none failed; 1 at least one step FAILED (an error, or skipped for lack of input)
  local verdict
  verdict=$(cd src/harris && python3 - "$SIFT_RUN_ID" <<'PY'
import sys
from crm import runlog
rows = [r for r in runlog.read(run_id=sys.argv[1]) if r.get("step") != "(whole run)"]
print("error" if any(r.get("status") == "error" for r in rows) else "blocked" if any(r.get("status") == "blocked" for r in rows) else "ok")
PY
  ) || verdict="error"
  if [[ ${#FAILED_STEPS[@]} -gt 0 && "$verdict" == "ok" ]]; then verdict="error"; fi
  case "$verdict" in
    ok)      echo "RUN STATUS: all steps ok"; return 0 ;;
    blocked) echo "RUN STATUS: BLOCKED (exit 3): ${FAILED_STEPS[*]} stopped by the safety brakes on purpose, nothing written by them; the other steps ran"; return 3 ;;
    *)       echo "RUN STATUS: FAILED (exit 1): ${#FAILED_STEPS[@]} step(s) failed or blocked: ${FAILED_STEPS[*]}"; return 1 ;;
  esac
}
