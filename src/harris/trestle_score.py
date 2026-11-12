"""Score the call pipeline's phones with Trestle (paid) and write the result to DataSift.

    python3 trestle_score.py                         # read only: how many numbers, what it would cost
    python3 trestle_score.py --commit                # through the safety brakes: a simulation over 10 leads
    SIFT_MODE=live python3 trestle_score.py --commit --limit 10      # 10 NUMBERS first, then read back
    SIFT_MODE=live python3 trestle_score.py --commit                 # the rest, up to TRESTLE_MAX_QUERIES

Which numbers: every phone on the leads tagged Priority 1 or Blitz Active (--tags to change), except numbers
a caller marked DNC / wrong / dead, numbers already scored less than 180 days ago, and numbers marked
correct that were scored before. Leads also on "Harris - Senior Equity" are skipped (Senior is out until the
operator says otherwise). Each query costs $0.020 (Phone Validation $0.015 + Litigator Check $0.005); a run
stops before querying when it would go over TRESTLE_MAX_QUERIES (default 400 = $8.00).

What it writes (rules in trestle.py): per number the tags "Trestle Scored" (+ "Litigator") and the dial tier
from the score; per lead the field "Trestle Phone Scores" (number=score@date) and the tags "Litigator - Do
Not Contact" / "Needs Re-Skip" when they apply. The paid queries happen only after the brakes have approved
the plan, so a simulation never spends. Every write is read back.
"""
import os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm, guard  # noqa: E402
import phone_priority as PP  # noqa: E402
import trestle as T  # noqa: E402

SENIOR = "Harris - Senior Equity"


def collect(crm, ids, today):
    """-> [lead dicts]: id, address, owner, phones, scores (parsed field), todo (phones to query)."""
    leads = []
    for lid in ids:
        d = crm.api.call(f"/api/internal/property/{lid}/")
        lists = [(x.get("title") if isinstance(x, dict) else x) for x in d.get("lists") or []]
        if SENIOR in lists:
            continue
        owner = d.get("owner") or {}
        phones = []
        for p in owner.get("phones") or []:
            p = dict(p)
            p["tags"] = [(t.get("name") or t.get("title")) if isinstance(t, dict) else t for t in p.get("tags") or []]
            phones.append(p)
        scores = T.parse_field(crm.lead_field(lid, T.FIELD))
        todo = []
        for p in phones:
            st = (p.get("status") or "UNKNOWN").upper()
            if st in PP.DEAD or "DNC" in st:
                continue
            if T.due(scores.get(T.digits(p["number"])), st.startswith("CORRECT"), today):
                todo.append(p)
        leads.append({"id": lid, "address": (d.get("address") or {}).get("street") or lid, "owner": owner.get("uuid"),
                      "phones": phones, "scores": scores, "tags": [(t.get("name") or t.get("title")) if isinstance(t, dict) else t
                                                                    for t in d.get("tags") or []], "todo": todo})
    return leads


def lead_outcome(lead):
    """After scoring: new phone tag sets, lead tags to add, field text."""
    sc = lead["scores"]
    tiers = PP.plan(lead["phones"], sc)
    phone_tags = {}
    for p in lead["phones"]:
        s = sc.get(T.digits(p["number"]))
        extra = ({T.SCORED_TAG} if s else set()) | ({T.LITIGATOR_PHONE_TAG} if s and s["litigator"] else set())
        new = sorted((set(p["tags"]) - PP.ALL_TIERS) | {tiers[p["number"]]} | extra)
        if sorted(p["tags"]) != new:
            phone_tags[p["number"]] = new
    add = []
    if any(s["litigator"] for s in sc.values()):
        add.append(T.LITIGATOR_LEAD_TAG)
    correct = any((p.get("status") or "").upper().startswith("CORRECT") for p in lead["phones"])
    good = any(s["score"] is not None and s["score"] >= 61 and not s["litigator"] for s in sc.values())
    live = [p for p in lead["phones"] if not ((p.get("status") or "").upper() in PP.DEAD or "DNC" in (p.get("status") or "").upper())]
    all_scored = all(T.digits(p["number"]) in sc for p in live)
    if live and all_scored and not correct and not good:  # judged only once every live number has a score
        add.append(T.RESKIP_TAG)
    return phone_tags, [t for t in add if t not in lead["tags"]], T.format_field(sc)


def main(*flags):
    commit = "--commit" in flags
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    tags = (flags[flags.index("--tags") + 1] if "--tags" in flags else "Priority 1,Blitz Active").split(",")
    cap = int(os.environ.get("TRESTLE_MAX_QUERIES") or 400)
    today = date.today()
    crm = get_crm()
    if T.FIELD not in crm.fields:
        raise SystemExit(f"custom field {T.FIELD!r} missing in DataSift (create it first: trestle_score.py --schema --commit)")
    leads = collect(crm, PP.pipeline_ids(crm, tags), today)
    numbers = []
    for l in leads:
        numbers += [(l, p) for p in l["todo"]]
    if limit:
        numbers = numbers[:limit]
    cost = len(numbers) * (T.COST_PHONE + T.COST_LITIGATOR)
    print(f"{len(leads)} pipeline leads, {sum(len(l['phones']) for l in leads)} phones; {len(numbers)} numbers to query "
          f"(${cost:.2f} at ${T.COST_PHONE + T.COST_LITIGATOR:.3f}); cap {cap} per run")
    if len(numbers) > cap:
        raise SystemExit(f"STOPPED: {len(numbers)} numbers is over the cap of {cap} (TRESTLE_MAX_QUERIES). Nothing queried.")
    touched = {id(l): l for l, _ in numbers}
    crm.plan([{"id": l["id"], "address": l["address"], "set": {T.FIELD: "(Trestle scores)"},
               "phone_tags_before": [], "phone_tags_after": ["Dial First"]} for l in touched.values()], commit)
    if not commit:
        print("Nothing queried, nothing written. --commit runs it through the safety brakes.")
        return 0
    run = guard.current()
    run.trestle_queries = 0
    errors = 0
    for l, p in numbers:
        r = T.query(p["number"])
        run.trestle_queries += 1
        if r.get("error") and r["score"] is None and not r["litigator"]:
            errors += 1
            print(f"  ERROR ...{T.digits(p['number'])[-4:]} on {l['address']}: {r['error']}")
            continue
        l["scores"][T.digits(p["number"])] = {"score": r["score"], "litigator": r["litigator"], "date": today}
    counts, lit, reskip, bad = {}, 0, 0, 0
    for l in touched.values():
        phone_tags, add, text = lead_outcome(l)
        problems = crm.update_lead(l["id"], {T.FIELD: text}, add)
        for p in l["phones"]:
            if p["number"] in phone_tags and not crm.set_phone_tags(l["id"], l["owner"], p, phone_tags[p["number"]]):
                bad += 1
                print(f"  WARN phone ...{T.digits(p['number'])[-4:]} on {l['address']}: tags did not read back")
        back = T.parse_field(crm.lead_field(l["id"], T.FIELD))
        if back != l["scores"] or problems:
            bad += 1
            print(f"  WARN {l['address']}: scores did not read back " + "; ".join(problems))
        lit += T.LITIGATOR_LEAD_TAG in add
        reskip += T.RESKIP_TAG in add
        for p in l["phones"]:
            s = l["scores"].get(T.digits(p["number"]))
            if s and s["date"] == today:
                t = "Litigator" if s["litigator"] else (T.tier_for(s["score"]) or "Invalid")
                counts[t] = counts.get(t, 0) + 1
    print(f"TRESTLE: {run.trestle_queries} queries (${run.trestle_queries * (T.COST_PHONE + T.COST_LITIGATOR):.2f}), "
          f"{errors} errors | scored: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
          + f" | leads: {lit} litigator, {reskip} Needs Re-Skip | {bad} read-back problems")
    return 1 if bad or errors else 0


def schema(commit):
    import probate_upload as P
    crm = get_crm()
    todo = P.ensure_schema(crm, commit, P.LIST, {"Prospect": [(T.FIELD, "text")]}, {})
    print("schema:", todo or "nothing to create")


if __name__ == "__main__":
    if "--schema" in sys.argv:
        schema("--commit" in sys.argv)
        sys.exit(0)
    sys.exit(main(*sys.argv[1:]))
