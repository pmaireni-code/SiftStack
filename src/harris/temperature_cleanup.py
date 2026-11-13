"""Put DataSift's own temperature back to 0 where the PIPELINE set it before anyone talked to the owner.

    python3 temperature_cleanup.py                     # read only: who qualifies and why the rest are kept
    python3 temperature_cleanup.py --commit            # through the safety brakes: a simulation (summary + 5
                                                       # examples with address), nothing written
    SIFT_MODE=live python3 temperature_cleanup.py --commit --allow-destructive
                                                       # real: ONE lead first, read back, then the rest

Until 2026-11-11 harris_grade (foreclosure) and the probate upload set DataSift's temperature (Hot 5, Warm 3)
before any contact. A lead goes back to 0 only when EVERY condition holds, re-checked on the live record
(operator, 2026-11-11):
  1. it is on "Harris - Foreclosure" or "Harris - Probate", and NOT also on "Harris - Senior Equity" (Senior
     is out of the cleanup until the operator says otherwise; read from the record's own lists, the Senior
     list itself is never read);
  2. its temperature is 3 or 5 AND equals what the code would have written from its score:
     foreclosure Deal Grade / probate Lead Temperature Hot -> 5, Warm -> 3. Anything else is left alone;
  3. its history shows at least one temperature change, every one of them made by the pipeline's DataSift
     user, and the LATEST one went from 0 to exactly the current value (no record of who set it -> left
     alone). DataSift's log endpoint does not always return the same events for a record (measured
     2026-11-11), so the history is read twice and the two reads are merged before judging;
  4. no contact at all: no property status, 0 call and SMS attempts, and no call, SMS, caller note
     (message board), phone disposition, task, status change or SiftLine card in its history.
The operator also sets temperatures by hand from the same user (condition 2 is what tells those apart),
so a lead that fails any check keeps its temperature. This is the ONLY code allowed to write the
temperature, and only the value 0 (crm/guard.py); that exception is removed once the cleanup is confirmed.
"""
import base64, collections, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402

LISTS = {"Harris - Foreclosure": "Deal Grade", "Harris - Probate": "Lead Temperature"}
EXCLUDED_LIST = "Harris - Senior Equity"
CODE_VALUE = {"Hot": 5, "Warm": 3}
CONTACT_PREFIXES = ("owner.call.", "owner.sms", "property.sms", "property.message.added", "property.status.",
                    "owner.phone.status.updated", "task.", "property.siftline", "siftline.", "card.")


def pipeline_user(api):
    """The e-mail of the DataSift user the pipeline writes as (from its session token)."""
    p = api.token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))).get("email") or ""


def author(e):
    a = e.get("author")
    return (a.get("email") or a.get("uuid")) if isinstance(a, dict) else a


def verdict(lead, events, me):
    """-> "" when the lead qualifies, else the reason it is kept."""
    h, score = lead["hotness"], lead["score"]
    if lead.get("senior"):
        return "also on Harris - Senior Equity (excluded)"
    if h not in (3, 5):
        return "temperature not 3 or 5"
    if CODE_VALUE.get(score) != h:
        return "temperature does not match its score (may be set by hand)"
    if lead["status"]:
        return "has a property status"
    if lead["calls"] or lead["sms"]:
        return "has call or SMS attempts"
    if any((e.get("event_type") or "").startswith(CONTACT_PREFIXES) for e in events):
        return "has contact in its history"
    hot = [e for e in events if "hotness" in (e.get("event_type") or "")]
    if not hot:
        return "no record of who set the temperature"
    if any(author(e) != me for e in hot):
        return "temperature changed by someone else"
    last = max(hot, key=lambda e: str(e.get("timestamp") or e.get("created") or ""))
    change = ((last.get("payload") or {}).get("property") or {}).get("hotness")
    if not (isinstance(change, list) and len(change) == 2 and int(change[0] or 0) == 0 and int(change[1] or 0) == h):
        return "last temperature change is not the pipeline's 0 -> current value"
    return ""


def history(crm, lead_id):
    """The record's events, read twice and merged: one read can miss events."""
    seen, out = set(), []
    for _ in range(2):
        for e in crm.lead_activity(lead_id)[1]:
            k = e.get("uuid") or json.dumps(e, sort_keys=True, default=str)
            if k not in seen:
                seen.add(k)
                out.append(e)
    return out


def refresh(crm, lead):
    """The lead as it stands NOW (temperature, status, attempts, lists); the score comes from the scan."""
    d = crm.api.call(f"/api/internal/property/{lead['id']}/")
    st = d.get("status")
    on = {(x.get("title") or x.get("name")) if isinstance(x, dict) else x for x in d.get("lists") or []}
    return dict(lead, hotness=int(d.get("hotness") or 0), status=(st.get("title") if isinstance(st, dict) else st) or "",
                calls=d.get("predictivecall_attempts") or 0, sms=d.get("sms_attempts") or 0,
                senior=EXCLUDED_LIST in on)


def read(crm):
    options = {u: label for f in crm.fields.values() for label, u in (f.get("options") or {}).items()}
    out = []
    for list_name, score_f in LISTS.items():
        for r in crm.list_assignments(list_name, (score_f,)):
            d = crm.api.call(f"/api/internal/property/{r['id']}/")
            st = d.get("status")
            on = {(x.get("title") or x.get("name")) if isinstance(x, dict) else x for x in d.get("lists") or []}
            out.append({"id": r["id"], "address": r["Property Street Address"], "list": list_name,
                        "score": options.get(r[score_f], r[score_f] or ""), "hotness": int(d.get("hotness") or 0),
                        "status": (st.get("title") if isinstance(st, dict) else st) or "",
                        "calls": d.get("predictivecall_attempts") or 0, "sms": d.get("sms_attempts") or 0,
                        "senior": EXCLUDED_LIST in on})
    return out


def main(*flags):
    commit = "--commit" in flags
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    crm = get_crm()
    me = pipeline_user(crm.api)
    leads = read(crm)
    keep, todo = collections.Counter(), []
    for l in leads:
        if not l["hotness"]:
            continue
        why = verdict(l, history(crm, l["id"]), me)
        if why:
            keep[(l["list"], why)] += 1
        else:
            todo.append(l)
    also = collections.Counter(l["list"] for l in leads if l.get("senior"))
    print(f"also on {EXCLUDED_LIST} (from each record's own lists): "
          + ", ".join(f"{k} {also.get(k, 0)}" for k in LISTS))
    print(f"{len(leads)} leads read; {sum(1 for l in leads if l['hotness'])} with a temperature; "
          f"{len(todo)} qualify for reset to 0")
    for (lst, why), n in sorted(keep.items()):
        print(f"  kept {n:>3} | {lst} | {why}")
    by = collections.Counter((l["list"], l["hotness"]) for l in todo)
    print("  qualify: " + ", ".join(f"{k[0]} temp {k[1]}: {v}" for k, v in sorted(by.items())))
    todo = todo[: limit or None]
    crm.plan([{"id": l["id"], "address": l["address"], "set": {"Property Temperature": ""},
               "before": {"Property Temperature": str(l["hotness"])}} for l in todo], commit)
    if not commit:
        print("Nothing written. --commit runs it through the safety brakes (a simulation unless SIFT_MODE=live).")
        return 0
    ok = failed = skipped = 0
    for i, l in enumerate(todo):
        now = refresh(crm, l)  # re-checked at the moment of writing: the scan above can be minutes old
        why = verdict(now, history(crm, l["id"]), me)
        if why:
            skipped += 1
            print(f"  SKIP {l['address']}: changed since the scan ({why}), left as is")
            continue
        if crm.reset_temperature(l["id"], now["hotness"], l["address"]):
            print(f"  reset {l['address']} | {l['list']} | {now['hotness']} -> 0 (read back)")
            ok += 1
        else:
            failed += 1
            print(f"  WARN {l['address']}: temperature did not read back as 0")
            if ok == 0:
                print("STOPPED after the first lead: it did not read back. Nothing else written.")
                return 1
    print(f"TEMPERATURE CLEANUP: {ok} reset to 0 and read back, {skipped} skipped (changed since the scan), "
          f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
