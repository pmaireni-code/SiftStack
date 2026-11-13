"""Fill Prospect Score / Prospect Reasons on leads already in DataSift, from the score the list already holds.

    python3 prospect_backfill.py                       # dry run: what would change, nothing written
    python3 prospect_backfill.py --commit              # through the safety brakes: over 10 leads it is a
                                                       # simulation unless SIFT_MODE=live
    SIFT_MODE=live python3 prospect_backfill.py --commit --limit 1     # one first, read back, then the rest

Probate: Prospect Score = Lead Temperature, Prospect Reasons = Temperature Notes (what probate_to_datasift
writes for new leads since 2026-11-11). Foreclosure needs no backfill: the daily harris_grade writes it.
Senior Equity is left out until the operator says otherwise (operator, 2026-11-11). Only values are added:
a lead whose Prospect fields already match is skipped, and an empty source never clears anything.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402

SOURCES = {"Harris - Probate": ("Lead Temperature", "Temperature Notes")}
TARGET = ("Prospect Score", "Prospect Reasons")


def read(crm, list_name):
    score_f, notes_f = SOURCES[list_name]
    options = {u: label for f in crm.fields.values() for label, u in (f.get("options") or {}).items()}
    label = lambda v: options.get(v, v or "")
    rows = crm.list_assignments(list_name, (score_f, notes_f) + TARGET)
    return [{"id": r["id"], "address": r["Property Street Address"], "score": label(r[score_f]), "reasons": r[notes_f] or "",
             "has_score": label(r[TARGET[0]]), "has_reasons": r[TARGET[1]] or ""} for r in rows]


def plan_changes(rows):
    """Leads whose Prospect fields differ from their source score. A lead with no source score is skipped."""
    return [r for r in rows if r["score"] and (r["score"], r["reasons"]) != (r["has_score"], r["has_reasons"])]


def main(*flags):
    commit = "--commit" in flags
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    list_name = flags[flags.index("--list") + 1] if "--list" in flags else "Harris - Probate"
    if list_name not in SOURCES:
        raise SystemExit(f"backfill covers only {', '.join(SOURCES)} (Senior Equity is out until further notice)")
    crm = get_crm()
    missing = [f for f in TARGET if f not in crm.fields]
    if missing:
        raise SystemExit(f"custom fields missing in DataSift: {missing} (run probate_upload.py <csv> --schema --commit)")
    rows = read(crm, list_name)
    todo = plan_changes(rows)[: limit or None]
    print(f"{list_name}: {len(rows)} leads, {len(todo)} need Prospect Score / Reasons")
    crm.plan([{"id": r["id"], "address": r["address"], "set": {TARGET[0]: r["score"], TARGET[1]: r["reasons"][:500]},
               "before": {TARGET[0]: r["has_score"], TARGET[1]: r["has_reasons"]}} for r in todo], commit)
    if not commit:
        for r in todo[:5]:
            print(f"  would set {r['address']}: {r['score']} | {r['reasons'][:80]}")
        print("Nothing written. Re-run with --commit (start with --limit 1).")
        return 0
    ok = failed = 0
    for r in todo:
        problems = crm.update_lead(r["id"], {TARGET[0]: r["score"], TARGET[1]: r["reasons"][:500]}, [])
        back = read_back(crm, r["id"])
        if problems or back != r["score"]:
            failed += 1
            print(f"  WARN {r['address']}: reads {back!r} " + "; ".join(problems))
        else:
            ok += 1
    print(f"PROSPECT BACKFILL: {ok} written and read back, {failed} failed")
    return 1 if failed else 0


def read_back(crm, lead_id):
    f = crm.fields[TARGET[0]]
    cf = crm.api.call(f"/api/internal/property/{lead_id}/custom-field/")
    got = {(x.get("custom_field") or {}).get("uuid"): x.get("value") for x in (cf.get("results") or [])}.get(f["uuid"])
    return {u: label for label, u in (f.get("options") or {}).items()}.get(got, got or "")


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
