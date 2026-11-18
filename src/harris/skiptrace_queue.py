"""Put the foreclosure leads that qualify for calling, but have no phone yet, in the skip-trace queue.
Dry run unless --commit.

    python3 skiptrace_queue.py [--commit] [--limit N]

Operator's rule (2026-11-17): every lead on "Harris - Foreclosure" that is
  - Single Family Residential,
  - estimated value $400,000 or less,
  - high equity: Equity Percent at or above harris_grade.EQUITY_HOT (40%),
  - with NO phone, and never skip traced (no "REISift Skipped" / "DataSift Skipped", no "Skip Trace No Result"),
gets the tag "Skip Trace Batch 1", the tag the operator runs DataSift's skip trace on. Leads also on
"Harris - Senior Equity", leads with a dead / closed / not-interested status, and leads that already carry
the tag are left alone. It only ADDS that tag (nothing is removed or overwritten); every write is read back.

Runs in the daily (run_daily.sh) and the weekly upload (run_weekly.sh), so new leads join the queue the day
they arrive; DataSift's skip trace itself is never run over the API (forbidden by the brakes).
"""
import os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402
from harris_grade import EQUITY_HOT  # noqa: E402

LIST, SENIOR = "Harris - Foreclosure", "Harris - Senior Equity"
TAG = "Skip Trace Batch 1"
# DataSift's skip trace tags the record "REISift Skipped" (+ month), renamed "DataSift Skipped" (+ month)
# by 2026-11-17; "Skip Trace No Result" is the operator's tag for a trace that found no phone.
TRACED_PREFIXES = ("REISift Skipped", "DataSift Skipped")
TRACED = {"Skip Trace No Result"}
STRUCTURE, MAX_VALUE = "Single Family Residential", 400000
DEAD = {"dead lead", "dead", "not_interested", "not interested", "closed", "sold", "dnc", "lost_deal"}
N = lambda t: (t.get("name") or t.get("title")) if isinstance(t, dict) else t


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


ENTITY_WORDS = re.compile(r"\b(LLC|L\.?L\.?C|INC|CORP|CORPORATION|COMPANY|CO|LTD|LP|L\.?P|LLP|PLLC|TRUST|TRUSTEE|ESTATE OF|"
                          r"HOLDINGS?|GROUP|PROPERTIES|INVESTMENTS?|PARTNERS|VENTURES|ENTERPRISES?|BANK|NATIONAL ASSOCIATION|"
                          r"MORTGAGE|FUNDING|CAPITAL|REALTY|HOMES|ASSOCIATION|CHURCH|MINISTRIES|FOUNDATION)\b", re.I)


def owner_name(d):
    o = d.get("owner") or {}
    return (o.get("company") or " ".join(x for x in (o.get("first_name"), o.get("last_name")) if x) or "").strip()


def is_entity(d):
    """An LLC, company, trust or bank owns it: DataSift's skip trace (people only) never processes it."""
    o = d.get("owner") or {}
    if o.get("company") and not (o.get("first_name") or o.get("last_name")):
        return True
    return bool(ENTITY_WORDS.search(owner_name(d)))


DEAD_FORECLOSURE = {"Deadline Passed", "Auction Passed"}  # harris_status: the lead is dead for this sale


def foreclosure_status(crm, row):
    """The lead's Foreclosure Status label from a list_assignments row (the field holds an option uuid)."""
    v = row.get("Foreclosure Status") or ""
    return {u: label for label, u in ((crm.fields.get("Foreclosure Status") or {}).get("options") or {}).items()}.get(v, v)


def why_not(d, entities_too=False, fstatus=""):
    """"" when the lead joins the queue, else the reason it does not. A business-owned lead goes to
    entity_owner.py instead (operator, 2026-11-18); entities_too=True judges it on the other rules only."""
    tags = {N(t) for t in d.get("tags") or []}
    lists = {N(x) for x in d.get("lists") or []}
    st = d.get("status")
    st = ((N(st) if isinstance(st, dict) else st) or "").strip().lower()
    value, eq = num(d.get("estimate_value")), num(d.get("equity_percent"))
    if SENIOR in lists:
        return "on Senior Equity"
    if fstatus in DEAD_FORECLOSURE:
        return f"foreclosure {fstatus.lower()} (dead for this sale)"
    if st in DEAD:
        return f"status {st}"
    if (d.get("owner") or {}).get("phones"):
        return "has phones"
    if tags & TRACED or any(str(t).startswith(TRACED_PREFIXES) for t in tags):
        return "already skip traced"
    if TAG in tags:
        return "already queued"
    if not entities_too and is_entity(d):
        return "owned by a company / trust (entity_owner.py)"
    if d.get("structure_type") != STRUCTURE:
        return "not single family"
    if value is None or value > MAX_VALUE:
        return "value over $400k or unknown"
    if eq is None or eq < EQUITY_HOT:
        return f"equity under {EQUITY_HOT:.0f}% or unknown"
    return ""


def main(*flags):
    commit = "--commit" in flags
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    crm = get_crm()
    todo, why = [], {}
    for r in crm.list_assignments(LIST, ("Foreclosure Status",)):
        d = crm.api.call(f"/api/internal/property/{r['id']}/")
        w = why_not(d, fstatus=foreclosure_status(crm, r))
        why[w or "QUEUE"] = why.get(w or "QUEUE", 0) + 1
        if not w:
            todo.append((r["id"], (d.get("address") or {}).get("street") or r["id"], [N(t) for t in d.get("tags") or []]))
    print(f"SKIPTRACE QUEUE: {sum(why.values())} leads on {LIST}; {len(todo)} to tag '{TAG}' | "
          + ", ".join(f"{k} {v}" for k, v in sorted(why.items()) if k != "QUEUE"))
    todo = todo[: limit or None]
    crm.plan([{"id": i, "address": a, "add_tags": [TAG]} for i, a, _ in todo], commit)
    if not commit:
        for _, a, _ in todo[:10]:
            print(f"  would tag {a}")
        return 0
    ok = bad = 0
    for i, a, before in todo:
        crm.update_lead(i, {}, [TAG])
        back = [N(t) for t in crm.api.call(f"/api/internal/property/{i}/").get("tags") or []]
        if TAG in back and all(t in back for t in before):
            ok += 1
            print(f"  queued {a} (read back)")
        else:
            bad += 1
            print(f"  WARN {a}: tags read {back}")
            if ok == 0:
                print("STOPPED after the first lead: it did not read back.")
                return 1
    print(f"SKIPTRACE QUEUE: {ok} tagged and read back, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
