"""Assign probate leads to a user once two weeks have passed since the date of death. Dry run unless --commit.

    python3 probate_assign.py [--commit] [--days 14] [--user "Maireni Peguero"]
    python3 probate_assign.py --preset --commit      # the "Harris - Probate 2 Weeks" filter preset (once)

Reads every lead on "Harris - Probate" through the CRM connector. A lead is assigned when it has a Date
of Death, at least DAYS days have passed since it, and nobody is assigned yet: an existing assignment
(to anyone) is never changed, so a lead the operator reassigned stays where it is. When the clerk's case
carries no Date of Death, the count starts at the Probate Open Date instead (operator, 2026-11-09: there
is no case without a decedent, so the filing date stands in). Each assignment is read back.
Operator's rule, 2026-11-09: 14 days after the death, to Maireni Peguero.

Every lead past that mark also gets the tag "Probate 2 Weeks", assigned or not, and the filter preset
"Harris - Probate 2 Weeks" (folder Harris Probate, created with --preset) is the list AND that tag. A saved
preset cannot express "today minus 14 days" on a custom date field in a form we could verify, so the
daily run keeps the tag current instead.
"""
import os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402
from harris_status import parse_date  # noqa: E402

LIST = "Harris - Probate"
TAG = "Probate 2 Weeks"
FOLDER, PRESET = "Harris Probate", "Harris - Probate 2 Weeks"


def preset(crm, commit):
    """Folder Harris Probate, preset = list Harris - Probate AND tag Probate 2 Weeks; read back and counted."""
    api = crm.api
    L = lambda r: r if isinstance(r, list) else r.get("results") or []
    lst = next(x["uuid"] for x in L(api.call("/api/internal/list/?limit=999")) if x["title"] == LIST)
    tag = next((t for t in L(api.call("/api/internal/tag/?limit=9999")) if (t.get("name") or t.get("title")) == TAG), None)
    if not tag:
        raise SystemExit(f"tag {TAG!r} not in DataSift yet: run probate_assign.py --commit first")
    must = {"any_lists": [lst], "any_tags": [tag["uuid"]]}
    folder = next(f for f in L(api.call("/api/internal/filter-preset-folder/?limit=999")) if f.get("title") == FOLDER)
    have = {p["title"]: p for p in L(api.call(f"/api/internal/filter-preset-folder/{folder['uuid']}/filter-preset/?limit=999"))}
    if PRESET in have:
        uuid = have[PRESET]["uuid"]
    elif commit:
        uuid = api.call("/api/internal/filter-preset/", "POST", {"title": PRESET, "folder": folder["uuid"], "quick_filter": False,
                                                                 "type": "properties", "filters": {"must": must}})["uuid"]
    else:
        print(f"would create preset {PRESET!r}: {must}")
        return
    back = (api.call(f"/api/internal/filter-preset/{uuid}/").get("filters") or {}).get("must")
    n = 0
    for pt in ("clean", "incomplete"):
        r = api.call("/api/internal/property/", "POST", {"limit": 100, "offset": 0, "query": {"must": {**back, "property_type": pt}}},
                     headers={"x-http-method-override": "GET"})
        n += len(r.get("results") or r.get("data") or [])
    print(f"{PRESET}: saved as sent {back == must} | {n} records")


def main(*flags):
    commit = "--commit" in flags
    if "--preset" in flags:
        preset(get_crm(), commit)
        return 0
    days = int(flags[flags.index("--days") + 1]) if "--days" in flags else 14
    user = flags[flags.index("--user") + 1] if "--user" in flags else "Maireni Peguero"
    crm = get_crm()
    leads = crm.list_assignments(LIST, ("Date of Death", "Probate Open Date"))
    # The day the 14-day count starts: the death, or the case's opening when the clerk gave no death date
    start = lambda l: parse_date(l["Date of Death"]) or parse_date(l["Probate Open Date"])
    today = date.today()
    due, waiting, no_dod, no_date, taken = [], [], 0, 0, 0
    for l in leads:
        if l["Assigned To"]:
            taken += 1
            continue
        dod = start(l)
        no_dod += not parse_date(l["Date of Death"])
        if not dod:
            no_date += 1
        elif (today - dod).days >= days:
            due.append(l)
        else:
            waiting.append((l, days - (today - dod).days))
    # The tag marks every lead past the mark, whoever it is assigned to
    past = [l for l in leads if start(l) and (today - start(l)).days >= days]
    to_tag = [l for l in past if TAG not in [t.strip() for t in (l.get("Tags") or "").split(",")]]
    tagged = 0
    for l in to_tag:
        if not commit:
            continue
        try:
            problems = crm.update_lead(l["id"], {}, [TAG])
            tagged += not problems
            for p in problems:
                print(f"  WARN {l['Property Street Address']}: {p}")
        except Exception as e:
            print(f"  FAIL tag {l['Property Street Address']}: {str(e)[:160]}")
    ok = failed = 0
    for l in due:
        if not commit:
            continue
        try:
            if crm.assign_lead(l["id"], user):
                ok += 1
            else:
                failed += 1
                print(f"  WARN {l['Property Street Address']}: did not read back as {user}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {l['Property Street Address']}: {str(e)[:160]}")
    verb = "assigned" if commit else "would assign"
    print(f"PROBATE ASSIGN: {len(leads)} leads | {verb} {ok if commit else len(due)} to {user} ({days}+ days since death)"
          f" | {len(waiting)} not yet due | {taken} already assigned | {no_dod} counted from the case opening (no death date)"
          + (f" | {no_date} with no date at all" if no_date else "")
          + (f" | {failed} failed" if failed else "")
          + f" | tag {TAG!r}: {'added to ' + str(tagged) if commit else 'would add to ' + str(len(to_tag))} of {len(past)}")
    for l, left in sorted(waiting, key=lambda x: x[1]):
        print(f"  due in {left} day(s): {l['Property Street Address']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
