"""Daily refresh of Harris foreclosure leads: Starting Bid, postponements, and Foreclosure Status.

    node bid_sources.js bids.json
    python3 harris_bid_updater.py <leads.csv> <bids.json> <out.csv> [--datasift [--commit]]

Per lead:
  * Starting Bid from the Auction.com / Xome listing for the SAME sale date (bids appear a few days
    ahead and change from TBD to a number). A listing with no bid yet is recorded as "listed, bid TBD".
  * Postponed: the same property listed for a LATER sale date moves Auction Date and Contract
    Deadline forward and marks the lead Postponed.
  * Foreclosure Status / Contract Deadline recomputed against today (see harris_status.py).
--datasift writes Starting Bid, Auction Date, Contract Deadline and Foreclosure Status onto the
lead's DataSift record (dry run unless --commit).
"""
import csv, json, re, sys
from datetime import date

from harris_status import assess, parse_date

SUFFIX = {"DRIVE": "DR", "LANE": "LN", "STREET": "ST", "ROAD": "RD", "COURT": "CT", "CIRCLE": "CIR", "AVENUE": "AVE",
          "BOULEVARD": "BLVD", "TRAIL": "TRL", "PLACE": "PL", "PARKWAY": "PKWY", "TERRACE": "TER", "HOLLOW": "HOLW"}
DATASIFT_FIELDS = ("Starting Bid", "Auction Date", "Contract Deadline", "Foreclosure Status")


def addr_key(street):
    """'4422 Sandhill Terrace Lane' -> ('4422', 'SANDHILL TERRACE LN'), units dropped."""
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
    later = sorted(parse_date(x["sale_date"]) for x in found if parse_date(x["sale_date"]) and sale and parse_date(x["sale_date"]) > sale)
    st = assess(sale, today, postponed_to=later[0] if later else None)
    current = [x for x in found if parse_date(x["sale_date"]) == st["auction_date"]]
    # A published bid beats a TBD listing; Auction.com's "Opening Bid" beats Xome's estimate
    current.sort(key=lambda x: (x["bid"] is None, x["source"] != "Auction.com"))
    fmt = lambda d: d.strftime("%m/%d/%Y") if d else ""
    lead["Auction Date"], lead["Contract Deadline"] = fmt(st["auction_date"]), fmt(st["contract_deadline"])
    lead["Foreclosure Status"], lead["Days To Deadline"] = st["status"], st["days_to_deadline"]
    tags = [t.strip() for t in lead["Tags"].split(",") if t.strip()]
    if st["status"] == "Postponed":
        lead["Starting Bid"] = ""  # last month's bid does not carry over to the new sale
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


def push_to_datasift(rows, commit):
    from ds import DS, rows as rows_of
    d = DS()
    fields = {f["label"]: f for f in rows_of(d.call("/api/internal/custom-fields/?limit=999")[1])}
    missing = [f for f in DATASIFT_FIELDS if f not in fields]
    if missing:
        print("DataSift: missing custom fields", missing)
    for r in rows:
        st, res = d.call("/api/internal/property/", "POST", {"limit": 10, "offset": 0, "query": {"must": {
            "property_type": "clean", "search": "address_prefix:" + r["Property Street Address"]}}}, {"x-http-method-override": "GET"})
        hit = next((x for x in rows_of(res) if addr_key((x.get("address") or {}).get("street")) == addr_key(r["Property Street Address"])), None)
        if not hit:
            print(f"  not in DataSift yet: {r['Property Street Address']}")
            continue
        values = []
        for label in DATASIFT_FIELDS:
            f, v = fields.get(label), r.get(label)
            if not f or not v:
                continue
            if f.get("field_type") == "date":
                v = parse_date(v).isoformat()
            elif f.get("field_type") == "select":
                v = next((o.get("uuid") or o.get("id") for o in f.get("options") or [] if o.get("label") == v), None)
            if v:
                values.append({"field_uuid": f["uuid"], "value": v})
        if not commit:
            print(f"  DRY  {r['Property Street Address']}: {', '.join(f'{k}={r.get(k)}' for k in DATASIFT_FIELDS if r.get(k))}")
            continue
        st, res = d.call(f"/api/internal/property/{hit['uuid']}/custom-field/update-values/", "PATCH", values)
        print(f"  {'OK  ' if st < 300 else 'FAIL'} {r['Property Street Address']} ({st}){'' if st < 300 else ' ' + str(res)[:200]}")


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
    if "--datasift" in flags:
        push_to_datasift(rows, "--commit" in flags)


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    main(*sys.argv[1:])
