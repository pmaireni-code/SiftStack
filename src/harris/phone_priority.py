"""Tag every phone on the pipeline leads with its dialing order. Dry run unless --commit.

    python3 phone_priority.py [--commit] [--tags "Priority 1,Blitz Active"] [--limit N]

DataSift's skip trace returns phones with a type (MOBILE / LANDLINE), a connection flag and a status that
stays UNKNOWN until a caller marks the outcome, and no quality score. Without a scoring service (Trestle)
the order is a rule (operator, 2026-11-10):
  1. a number already marked CORRECT
  2. connected mobiles, in the order the skip trace returned them
  3. connected landlines, in the same order
  4. numbers the skip trace flags as not connected (mobiles, then landlines): last, never dropped, since
     on 2026-11-10 they were 117 of 291 numbers and the ONLY numbers on 11 of 96 leads
  -> Dial First, Dial Second, Dial Third, then Dial Fourth for the rest
  numbers a caller marked DNC / wrong / dead -> Drop (never dial)
With Trestle scores (trestle_score.py, operator 2026-11-12) a scored number takes its tier from the score
instead: 81-100 Dial First, 61-80 Dial Second, 41-60 Dial Third, 40 or less Drop; a litigator number and an
invalid one (no score) -> Drop; a number marked CORRECT stays Dial First. The scores are read from the lead's
"Trestle Phone Scores" field. Numbers not scored yet keep the rule above.
Re-run daily: new skip-trace numbers and outcomes from smrtPhone's call status reorder the lead. Only phones
whose tier changes are written; any other tier tag on the phone is removed first so a number never carries
two tiers. Every write is read back.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402
import trestle  # noqa: E402

TIERS = ["Dial First", "Dial Second", "Dial Third", "Dial Fourth"]
DROP = "Drop"
ALL_TIERS = set(TIERS) | {DROP}
DEAD = {"DNC", "CORRECT_DNC", "WRONG_DNC", "WRONG", "DEAD", "WRONG_NUMBER", "DEAD_NUMBER", "DNC_WRONG", "DNC_CORRECT"}


def plan(phones, scores=None):
    """number -> tier for one lead. scores: trestle.parse_field() of the lead (number -> score record)."""
    scores = scores or {}
    out, live = {}, []
    for i, p in enumerate(phones):
        st = (p.get("status") or "UNKNOWN").upper()
        sc = scores.get(trestle.digits(p["number"]))
        if st in DEAD or "DNC" in st:
            out[p["number"]] = DROP
        elif sc and sc["litigator"]:
            out[p["number"]] = DROP
        elif sc and not st.startswith("CORRECT"):
            out[p["number"]] = trestle.tier_for(sc["score"]) or DROP  # scored but invalid: no score
        else:  # a number marked CORRECT ranks first (Dial First); unscored numbers follow the rule
            mobile = (p.get("type") or "").upper() == "MOBILE"
            connected = p.get("is_connected", True)
            rank = 0 if st.startswith("CORRECT") else (1 if mobile else 2) if connected else (3 if mobile else 4)
            # Within a category the tier a number already holds wins over list position: DataSift REORDERS a
            # lead's phones when one is written (measured 2026-11-10), so position alone flips First and Second
            # on every run. The skip trace's order only decides numbers that have no tier yet.
            held = next((TIERS.index(t) for t in p.get("tags") or [] if t in TIERS), len(TIERS))
            live.append((rank, held, i, p["number"]))
    for n, (_, _, _, num) in enumerate(sorted(live)):
        out[num] = TIERS[min(n, len(TIERS) - 1)]
    return out


def pipeline_ids(crm, tags=("Priority 1", "Blitz Active"), limit=0):
    """Ids of the leads with any of these tags and at least one phone."""
    api = crm.api
    L = lambda r: r if isinstance(r, list) else r.get("results") or []
    have = {(t.get("name") or t.get("title")): t["uuid"] for t in L(api.call("/api/internal/tag/?limit=9999"))}
    ids = []
    for pt in ("clean", "incomplete"):
        o = 0
        while True:
            page = api.call("/api/internal/property/", "POST", {"limit": 100, "offset": o, "query": {"must": {
                "any_tags": [have[t.strip()] for t in tags if t.strip() in have], "phone_count": [1, None],
                "property_type": pt}}}, headers={"x-http-method-override": "GET"}).get("results") or []
            ids += [x["uuid"] for x in page]
            o += len(page)
            if len(page) < 100:
                break
    return ids[: limit or None]


def main(*flags):
    commit = "--commit" in flags
    tags = (flags[flags.index("--tags") + 1] if "--tags" in flags else "Priority 1,Blitz Active").split(",")
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    crm = get_crm()
    ids = pipeline_ids(crm, tags, limit)
    leads = phones_n = changed = written = failed = 0
    counts = {t: 0 for t in TIERS + [DROP]}
    todo = []  # every phone to rewrite, collected first so the safety brakes see the whole step
    for lid in ids:
        owner, phones = crm.read_phones(lid)
        if not owner or not phones:
            continue
        leads += 1
        tiers = plan(phones, trestle.parse_field(crm.lead_field(lid, trestle.FIELD)))
        for p in phones:
            phones_n += 1
            want = tiers[p["number"]]
            counts[want] += 1
            new = sorted((set(p["tags"]) - ALL_TIERS) | {want})
            if sorted(p["tags"]) == new:
                continue
            changed += 1
            todo.append((lid, owner, p, new))
    crm.plan([{"id": lid, "phone_tags_before": list(p["tags"]), "phone_tags_after": new} for lid, _, p, new in todo], commit)
    if commit:
        for lid, owner, p, new in todo:
            if crm.set_phone_tags(lid, owner, p, new):
                written += 1
            else:
                failed += 1
                print(f"  WARN phone ...{p['number'][-4:]} on {lid}: did not read back")
    print(f"PHONE PRIORITY: {leads} leads, {phones_n} phones | " + ", ".join(f"{k} {v}" for k, v in counts.items())
          + f" | {'written ' + str(written) if commit else 'would write ' + str(changed)} changed"
          + (f" | {failed} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
