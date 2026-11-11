"""Daily refresh of Harris foreclosure leads: Starting Bid, postponements, and Foreclosure Status.

    node bid_sources.js bids.json
    python3 harris_bid_updater.py <leads.csv> <bids.json> <out.csv> [--crm [--commit]]

Per lead:
  * Starting Bid from the Auction.com / Xome listing for the SAME sale date (bids appear a few days
    ahead and change from TBD to a number). A listing with no bid yet is recorded as "listed, bid TBD".
  * Postponed: the same property listed for a LATER sale date moves Auction Date and Contract
    Deadline forward and marks the lead Postponed.
  * Foreclosure Status / Contract Deadline recomputed against today (see harris_status.py).
--crm writes Starting Bid, Auction Date, Contract Deadline and Foreclosure Status onto the
lead's CRM record (HARRIS_CRM, see crm/) (dry run unless --commit).
"""
import csv, json, re, sys
from datetime import date

from harris_status import assess, parse_date

SUFFIX = {"DRIVE": "DR", "LANE": "LN", "STREET": "ST", "ROAD": "RD", "COURT": "CT", "CIRCLE": "CIR", "AVENUE": "AVE",
          "BOULEVARD": "BLVD", "TRAIL": "TRL", "PLACE": "PL", "PARKWAY": "PKWY", "TERRACE": "TER", "HOLLOW": "HOLW"}
SAME_SALE_DAYS = 3


def addr_key(street):
    """'123 Example Terrace Lane' -> ('123', 'EXAMPLE TER LN'), units dropped."""
    s = re.sub(r"[^A-Z0-9 ]", " ", (street or "").upper())
    s = re.sub(r"\b(UNIT|APT|STE|#)\b.*$", "", s)
    toks = [SUFFIX.get(t, t) for t in s.split()]
    return (toks[0], " ".join(toks[1:])) if toks and toks[0].isdigit() else None


def listings_for(lead, by_num):
    """Every auction listing for this property, whatever its sale date."""
    key = addr_key(lead["Property Street Address"])
    if not key:
        return []
    out = []
    for x in by_num.get(key[0], []):
        k = addr_key(x["street"])
        if not k or k[1].split()[:1] != key[1].split()[:1]:
            continue
        if lead["Property ZIP Code"] and x["zip"] and lead["Property ZIP Code"][:5] != x["zip"][:5]:
            continue
        out.append(x)
    return out


def refresh(lead, by_num, today):
    sale = parse_date(lead.get("Auction Date") or lead["Foreclosure Date"])
    found = listings_for(lead, by_num)
    # Listing dates drift a day from the notice (Xome stores the sale in UTC: 12/01 10 AM shows 12/02), and
    # a real postponement moves to a later first Tuesday, so anything within SAME_SALE_DAYS is this sale
    later = sorted(d for d in (parse_date(x["sale_date"]) for x in found) if d and sale and (d - sale).days > SAME_SALE_DAYS)
    st = assess(sale, today, postponed_to=later[0] if later else None)
    current = [x for x in found if parse_date(x["sale_date"]) and abs((parse_date(x["sale_date"]) - st["auction_date"]).days) <= SAME_SALE_DAYS]
    # A published bid beats a TBD listing; Auction.com's "Opening Bid" beats Xome's estimate
    current.sort(key=lambda x: (x["bid"] is None, x["source"] != "Auction.com"))
    fmt = lambda d: d.strftime("%m/%d/%Y") if d else ""
    lead["Auction Date"], lead["Contract Deadline"] = fmt(st["auction_date"]), fmt(st["contract_deadline"])
    lead["Foreclosure Status"], lead["Days To Deadline"] = st["status"], st["days_to_deadline"]
    tags = [t.strip() for t in lead["Tags"].split(",") if t.strip()]
    if st["status"] == "Postponed":
        lead["Starting Bid"] = ""  # last month's bid does not carry over to the new sale
        tags.append("Postponed")  # the status reads Active again tomorrow; the tag keeps the history
        tags = [t for t in tags if t != "Bid Posted"]
    if current:
        x = current[0]
        lead["Bid Source"] = f"{x['source']} ({x['bid_type']})" if x["bid"] else f"{x['source']} (listed, bid TBD)"
        lead["Bid Listing URL"] = x["url"]
        if x["bid"]:
            lead["Starting Bid"] = str(round(x["bid"]))
            tags.append("Bid Posted")
    lead["Tags"] = ", ".join(dict.fromkeys(tags))
    lead["Bid Checked"] = today.strftime("%m/%d/%Y")
    return st["status"]


def push_to_crm(rows, commit, before):
    """Write the day's changes back through the CRM connector (HARRIS_CRM, see crm/). Only leads whose
    Starting Bid, Auction Date, Contract Deadline or Foreclosure Status moved get a call: a quiet day
    costs a handful of writes, not one per lead."""
    from crm import DAILY_FIELDS, DAILY_TAGS, get_crm
    changed = [(r, b) for r, b in zip(rows, before) if any((r.get(k) or "") != (b.get(k) or "") for k in DAILY_FIELDS)]
    print(f"CRM: {len(changed)} of {len(rows)} leads changed today")
    if not changed:
        return
    crm = get_crm()
    # safety brakes: the whole day's write-back is checked before the first write (crm/guard.py); without
    # --commit this only previews what the brakes would decide
    crm.plan([{"id": r["id"], "address": r["Property Street Address"],
               "set": {k: r.get(k) or "" for k in DAILY_FIELDS}, "before": {k: b.get(k) or "" for k in DAILY_FIELDS},
               "add_tags": [t for t in DAILY_TAGS if t in r.get("Tags", "") and t not in b.get("Tags", "")]}
              for r, b in changed if r.get("id")], commit)
    for r, b in changed:
        what = ", ".join(f"{k}={r.get(k) or '-'}" for k in DAILY_FIELDS)
        if not r.get("id"):
            print(f"  SKIP {r['Property Street Address']}: no CRM id (read the leads with harris_from_crm.py)")
            continue
        if not commit:
            print(f"  DRY  {r['Property Street Address']}: {what}")
            continue
        add = [t for t in DAILY_TAGS if t in r.get("Tags", "") and t not in b.get("Tags", "")]
        try:
            problems = crm.update_lead(r["id"], {k: r.get(k) or "" for k in DAILY_FIELDS}, add)
            print(f"  {'OK  ' if not problems else 'WARN'} {r['Property Street Address']}: {what}")
            for p in problems:
                print("       -", p)
        except Exception as e:
            print(f"  FAIL {r['Property Street Address']}: {str(e)[:200]}")


def main(leads_path, bids_path, out_path, *flags):
    snap = json.load(open(bids_path))
    by_num = {}
    for x in snap["listings"]:
        k = addr_key(x["street"])
        if k:
            by_num.setdefault(k[0], []).append(x)
    rows = list(csv.DictReader(open(leads_path)))
    extra = ("Auction Date", "Contract Deadline", "Foreclosure Status", "Days To Deadline", "Bid Source", "Bid Listing URL", "Bid Checked")
    fields = list(rows[0].keys()) + [c for c in extra if c not in rows[0]]
    today = date.today()
    counts = {}
    before = [dict(r) for r in rows]
    for r in rows:
        s = refresh(r, by_num, today)
        counts[s] = counts.get(s, 0) + 1
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    listed = sum(bool(r.get("Bid Source")) for r in rows)
    bids = sum(bool(r.get("Starting Bid")) for r in rows)
    print(f"{len(rows)} leads | status {counts} | {listed} listed on an auction site | {bids} with a published bid")
    if "--crm" in flags or "--datasift" in flags:  # --datasift: the flag's name before connectors existed
        push_to_crm(rows, "--commit" in flags, before)


if __name__ == "__main__":
    here = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
    sys.path[:0] = [here, here + "/.."]
    main(*sys.argv[1:])
