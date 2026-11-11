"""Deal Grade for every open Harris foreclosure lead: is buying it before the auction a viable deal?

    python3 harris_grade.py <market.json> <out.csv> [--crm [--commit]]

Reads the leads (with property facts) through the CRM connector, grades the open ones (everything
but Auction Passed) against today's Market Finder data, and with --crm writes back only the leads
whose grade, notes or ZIP activity changed (dry run unless --commit).

Rules (agreed with the operator 2026-11-01):
  Not Viable  equity under EQUITY_MIN: the sale price cannot pay off the loan plus costs
  Needs Data  no equity figure to judge on
  Hot         equity >= EQUITY_HOT, single family, inside the investor buy box, ZIP not in the
              bottom third for investor activity, and no Verify Equity flag
  Warm        everything else at or above EQUITY_MIN; Deal Notes says what kept it from Hot
Buy box = DataSift's Recommended Filters for the county with two adjustments: no year-built cap
(post-2010 homes are 21% of Harris single-family stock and 35% of these leads, and the equity rule
already catches the high-LTV new loan) and baths up to 4 (a 3,302 sq ft two-story is usually 3.5).
Flood: FEMA NFHL zone at the record's coordinates goes into Deal Notes, and a 100-year floodplain
adds the "Flood Zone" tag. It never changes the grade (a ~4.6% price discount in Harris, not a
deal killer) but it moves the offer.
Equity is DataSift's estimate (value minus estimated loan balances); it excludes arrears and
foreclosure fees, so EQUITY_HOT carries that margin. Verify Equity: a loan taken in the last
VERIFY_YEARS for most of the value cannot carry this much equity yet.
"""
import csv, json, os, sys, time, urllib.parse, urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harris_status import parse_date  # noqa: E402

EQUITY_HOT = 40.0
EQUITY_MIN = 25.0
BATHS_MAX = 4
VERIFY_YEARS = 5
VERIFY_LTV = 0.85
GRADE_FIELDS = ("Deal Grade", "Deal Notes", "ZIP Investor Activity")
# DataSift's own temperature marker, set ONCE: when a lead is graded for the first time and nobody has
# touched its temperature yet (still 0, cold). After that it is the operator's to change (agreed 2026-11-01)
INITIAL_TEMPERATURE = {"Hot": 5, "Warm": 3}
FLOOD_TAG = "Flood Zone"  # in FEMA's 100-year floodplain; a note and a tag, never a grade change
NFHL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"


def flood_zone(lat, lon):
    """FEMA flood zone at a point -> (label, in_100_year). The point must go as a JSON geometry:
    the "x,y" shorthand makes NFHL time out with a 500. Retries, since NFHL is slow and flaky."""
    if lat is None or lon is None:
        return "flood zone unknown (no coordinates)", False
    q = urllib.parse.urlencode({"geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
                                "geometryType": "esriGeometryPoint", "spatialRel": "esriSpatialRelIntersects",
                                "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF", "returnGeometry": "false", "f": "json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"{NFHL}?{q}", timeout=60) as r:
                feats = json.load(r).get("features")
            if feats is None:
                raise ValueError("no features key")
            break
        except Exception:
            time.sleep(3 * (attempt + 1))
    else:
        return "flood zone unknown (FEMA did not answer)", False
    if not feats:
        return "flood zone not mapped", False
    a = feats[0]["attributes"]
    zone, sub = a.get("FLD_ZONE") or "?", (a.get("ZONE_SUBTY") or "").upper()
    if a.get("SFHA_TF") == "T":
        return f"FLOOD ZONE {zone} (100-year floodplain)", True
    if "0.2" in sub:
        return f"flood zone {zone} (500-year)", False
    return f"flood zone {zone} (outside floodplain)", False


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def zip_tiers(zips):
    """ZIP -> (High/Medium/Low, investor transactions over 6 months), by thirds within the county."""
    tx = {z: m.get("num_investor_transactions") or 0 for z, m in zips.items()}
    ranked = sorted(tx.values())
    lo, hi = ranked[len(ranked) // 3], ranked[2 * len(ranked) // 3]
    return {z: ("High" if v >= hi else "Medium" if v >= lo else "Low", int(v)) for z, v in tx.items()}


def grade(lead, box, tiers, today):
    notes = []
    eq, value = num(lead.get("Equity Percent")), num(lead.get("Estimated Value"))
    tier, tx = tiers.get((lead.get("Property ZIP Code") or "")[:5], ("", None))
    if tier:
        notes.append(f"ZIP {lead['Property ZIP Code'][:5]}: {tx} investor buys in 6 mo ({tier})")
    if eq is None:
        return "Needs Data", ["no equity estimate in DataSift"] + notes, tier
    notes.insert(0, f"equity {eq:.0f}%")
    if eq < EQUITY_MIN:
        return "Not Viable", notes, tier
    why = []
    if "single family" not in (lead.get("Structure Type") or "").lower():
        why.append(f"not single family ({lead.get('Structure Type') or 'unknown type'})")
    checks = [("Living SqFt", box["living_area_sqft_low"], box["living_area_sqft_high"], "sq ft"),
              ("Bedrooms", box["bedrooms_count_low"], box["bedrooms_count_high"], "beds"),
              ("Bathrooms", box["bath_count_low"], BATHS_MAX, "baths"),
              ("Estimated Value", box["property_value_low"], box["property_value_high"], "value")]
    for col, low, high, label in checks:
        v = num(lead.get(col))
        if v is not None and not (low <= v <= high):
            why.append(f"{label} {v:,.0f} outside {low:,.0f}-{high:,.0f}")
    if tier == "Low":
        why.append("low investor activity in the ZIP")
    loan, loan_d = num(lead.get("Original Loan Amount")), parse_date(lead.get("Loan Date"))
    if loan and value and loan_d and (today - loan_d).days < VERIFY_YEARS * 365 and loan >= VERIFY_LTV * value:
        why.append(f"VERIFY EQUITY: ${loan:,.0f} loan from {loan_d:%m/%Y} vs ${value:,.0f} value")
    if eq < EQUITY_HOT:
        why.insert(0, f"equity under {EQUITY_HOT:.0f}%")
    return ("Hot" if not why else "Warm"), notes + why, tier


def main(market_path, out_path, *flags):
    from crm import get_crm
    market = json.load(open(market_path))
    tiers = zip_tiers(market["zips"])
    crm = get_crm()
    leads = crm.read_leads("Harris - Foreclosure", with_facts=True)
    today = date.today()
    before = [dict(l) for l in leads]
    counts = {}
    for l in leads:
        if l.get("Foreclosure Status") == "Auction Passed":
            continue  # closed: keep its last grade
        g, notes, tier = grade(l, market["buy_box"], tiers, today)
        flood, sfha = flood_zone(num(l.get("Latitude")), num(l.get("Longitude")))
        l["Deal Grade"], l["Deal Notes"], l["ZIP Investor Activity"] = g, "; ".join(notes + [flood])[:500], tier
        l["_flood_tag"] = sfha
        counts[g] = counts.get(g, 0) + 1
    cols = list(dict.fromkeys(k for l in leads for k in l))
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(leads)
    print(f"graded {sum(counts.values())} open leads: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
          + f" | {sum(bool(l.get('_flood_tag')) for l in leads)} in a 100-year floodplain"
          + f" | {sum('unknown' in (l.get('Deal Notes') or '') for l in leads)} flood unknown")
    if "--crm" not in flags:
        return
    commit = "--commit" in flags
    for l, b in zip(leads, before):
        first_grade = not (b.get("Deal Grade") or "") and l.get("Deal Grade")
        untouched = num(l.get("Property Temperature")) in (None, 0.0)
        if first_grade and untouched and l["Deal Grade"] in INITIAL_TEMPERATURE:
            l["_temperature"] = INITIAL_TEMPERATURE[l["Deal Grade"]]
    changed = [l for l, b in zip(leads, before) if any((l.get(k) or "") != (b.get(k) or "") for k in GRADE_FIELDS)]
    print(f"CRM: {len(changed)} leads with a new grade")
    if commit:  # safety brakes (crm/guard.py)
        crm.plan([{"id": l["id"], "address": l["Property Street Address"],
                   "set": {**{k: l.get(k) or "" for k in GRADE_FIELDS},
                           **({"Property Temperature": l["_temperature"]} if l.get("_temperature") else {})},
                   "before": {k: b.get(k) or "" for k in GRADE_FIELDS},
                   "add_tags": [FLOOD_TAG] if l.get("_flood_tag") and FLOOD_TAG not in (l.get("Tags") or "") else []}
                  for l, b in zip(leads, before) if l in changed])
    for l in changed:
        what = f"{l['Deal Grade']} | {l['Deal Notes'][:90]}"
        if not commit:
            print(f"  DRY  {l['Property Street Address']}: {what}")
            continue
        try:
            add = [FLOOD_TAG] if l.get("_flood_tag") and FLOOD_TAG not in (l.get("Tags") or "") else []
            vals = {k: l.get(k) or "" for k in GRADE_FIELDS}
            if l.get("_temperature"):
                vals["Property Temperature"] = l["_temperature"]
            problems = crm.update_lead(l["id"], vals, add)
            print(f"  {'OK  ' if not problems else 'WARN'} {l['Property Street Address']}: {l['Deal Grade']}")
            for p in problems:
                print("       -", p)
        except Exception as e:
            print(f"  FAIL {l['Property Street Address']}: {str(e)[:200]}")


if __name__ == "__main__":
    main(*sys.argv[1:])
