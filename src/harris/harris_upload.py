"""Upload Harris foreclosure leads (harris_to_datasift.py output) into the CRM.

    python3 harris_upload.py <leads.csv>                       # dry run, prints the first payload
    python3 harris_upload.py <leads.csv> --limit 1 --commit    # ALWAYS one first, then read it back
    python3 harris_upload.py <leads.csv> --commit [--start N]  # --start resumes a cut-off run

The CRM is picked by HARRIS_CRM (default datasift, see crm/). Every connector upserts by property
address and reads each record back, reporting anything that did not land.
"""
import csv, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402


def main(path, *flags):
    commit = "--commit" in flags
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    start = int(flags[flags.index("--start") + 1]) if "--start" in flags else 0
    rows = list(csv.DictReader(open(path, encoding="utf-8")))[start:][: limit or None]
    crm = get_crm()
    print(f"[{'COMMIT' if commit else 'DRY RUN'}] {len(rows)} leads -> {crm.name}\n")
    crm.plan_rows(rows, commit)  # safety brakes (crm/guard.py)
    ok = failed = 0
    for i, row in enumerate(rows, 1):
        try:
            res = crm.upsert_lead(row, commit)
        except Exception as e:
            failed += 1
            print(f"  FAIL {row['Property Street Address']}\n       - {str(e)[:200]}")
            continue
        if not commit:
            if i == 1:
                print(res.get("preview", ""))
            continue
        ok += 1
        flag = "created" if res["created"] else "existing record"
        print(f"  {'OK  ' if not res['problems'] else 'WARN'} {row['Property Street Address']} ({flag}, {res['id']})")
        for p in res["problems"]:
            print("       -", p)
        time.sleep(0.3)
    if commit:
        print(f"\nuploaded={ok} failed={failed}")
    else:
        print("\nNothing written. Re-run with --commit (start with --limit 1).")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
