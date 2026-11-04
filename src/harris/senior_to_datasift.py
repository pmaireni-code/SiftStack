"""Senior / free-and-clear candidates (senior_sweep.py) -> the neutral leads CSV, list "Harris - Senior Equity".

    python3 senior_to_datasift.py <sweep.json> <hcad_idx.tsv> <senior_accts.json> <market.json> <out.csv>

A lead must pass all of:
  senior     HCAD over-65 homestead (hcad_senior.py: "Over-65" or "Over-65 or Disabled") AND SiftMap 75+
  equity     free and clear (equity 100%), or 70%+ when USPS vacant; Estimated Debt = value x (1 - equity)
  property   single family, built 1990 or earlier, inside DataSift's Market Finder buy box (sq ft, beds)
  value      middle 60% of Harris single-family values (p20-p80 of the HCAD roll), capped at $300,000
             for the free-and-clear leads (operator, 2026-11-01)
  Market Check, the operator's four ZIP criteria, every one against a county reference stored on the lead:
    1 ZIP median SALE price below the ZIP median home VALUE (sellers are not getting retail), and
      inside the middle 60% (closings in the investor band)
    2 ZIP median days on market at or under the county's
    3 lead value inside the middle 60% (above)
    4 ZIP investor purchases (6 months) "solid, not the highest": between the median ZIP and the
      90th-percentile ZIP. Top-decile ZIPs carry the heaviest competition
Senior Tier: Priority = vacant, or built 1975 or earlier with a single owner on the deed (no "&");
Standard = the rest. One list, two filter presets.

Lead Temperature (how likely the owner sells, all leads already pass the market):
  +2 USPS vacant   +1 owner 80+   +1 single owner   +1 built 1975 or earlier
  +1 HCAD says Over-65 (not "or Disabled")   +1 ZIP sells 20%+ faster than the county
Hot = 4+, Warm = 2-3, Cold = 0-1. Sets the CRM's own temperature on first upload (Hot 5, Warm 3).
"""
import collections, csv, json, re, statistics, sys
from datetime import date

from harris_to_datasift import split_owner, street, tc

LIST = "Harris - Senior Equity"
TEMPERATURE_CRM = {"Hot": 5, "Warm": 3}
SENIOR = ("Over-65", "Over-65 or Disabled")
IDX = ["acct", "mailto", "mail_addr_1", "mail_city", "mail_state", "mail_zip", "site_addr_1", "site_addr_2",
       "site_addr_3", "state_class", "tot_mkt_val", "bld_ar", "yr_impr"]
norm = lambda s: re.sub(r"[^A-Z0-9]", "", (s or "").upper())
money = lambda v: f"${v:,.0f}"


def market_refs(market):
    zips, county = market["zips"], market["county"]
    tx = sorted(m.get("num_investor_transactions") or 0 for m in zips.values())
    q = lambda p: tx[int(p * (len(tx) - 1))]
    return {"dom": county["median_days_on_market"], "value": county["median_property_value"],
            "sale": county["median_sale_price"], "tx": (q(0.5), q(0.9))}


def market_check(zm, value, band, ref):
    """-> (passes all four, one-line verdict, the ZIP numbers)."""
    s, v = zm.get("median_sale_price"), zm.get("median_property_value")
    d, t = zm.get("median_days_on_market"), zm.get("num_investor_transactions") or 0
    lo, hi = band
    checks = [
        ("sale<value", bool(s and v and s < v and lo <= s <= hi),
         f"ZIP sale {money(s or 0)} " + ("<" if s and v and s < v else ">=") + f" value {money(v or 0)}"
         + ("" if s and lo <= s <= hi else " (sale outside middle 60%)")),
        ("dom", bool(d and d <= ref["dom"]), f"DOM {d or 0:.0f} " + ("<=" if d and d <= ref["dom"] else ">") + f" county {ref['dom']:.0f}"),
        ("value", lo <= value <= hi, f"lead value {money(value)} " + ("in" if lo <= value <= hi else "outside") + " middle 60%"),
        ("investors", ref["tx"][0] <= t <= ref["tx"][1], f"investors {t:.0f} " + ("in" if ref["tx"][0] <= t <= ref["tx"][1] else "outside")
         + f" {ref['tx'][0]:.0f}-{ref['tx'][1]:.0f}")]
    n = sum(ok for _, ok, _ in checks)
    return n == 4, f"{n}/4: " + "; ".join(txt for _, _, txt in checks), (s, v, d, t)


def main(sweep_path, idx_path, senior_path, market_path, out_path):
    sw = json.load(open(sweep_path))
    band, cap, older = tuple(sw["band"]), sw["max_value"], set(sw["older"])
    senior = json.load(open(senior_path))
    market = json.load(open(market_path))
    ref, box = market_refs(market), market["buy_box"]
    hcad = {}
    with open(idx_path, encoding="latin-1") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) >= len(IDX):
                r = dict(zip(IDX, c))
                hcad[(norm(r["site_addr_1"]), r["site_addr_3"][:5])] = r
    out, why = [], collections.Counter()
    for x in sw["rows"].values():
        z = re.search(r"(\d{5})$", x["address"])
        h = hcad.get((norm(x["address"].split(",")[0]), z.group(1) if z else ""))
        if not h:
            why["not found on the HCAD roll"] += 1; continue
        sen = senior.get(h["acct"])
        if sen not in SENIOR:
            why["no HCAD over-65 homestead"] += 1; continue
        sq = x.get("squareFeet") or 0
        if sq and not box["living_area_sqft_low"] <= sq <= box["living_area_sqft_high"]:
            why["sq ft outside the buy box"] += 1; continue
        value, eq = float(x.get("estimatedValue") or 0), float(x.get("equityPercent") or 0)
        vacant = bool((x.get("presets") or {}).get("vacant"))
        if not vacant and (eq < 100 or value > cap):
            why["not free and clear / over the cap"] += 1; continue
        zm = market["zips"].get(z.group(1), {})
        ok, verdict, (s, v, d, t) = market_check(zm, value, band, ref)
        if not ok:
            why["fails the market check"] += 1; continue
        yr = int(h["yr_impr"]) if h["yr_impr"].isdigit() and int(h["yr_impr"]) > 1800 else 0
        single = "&" not in h["mailto"]
        age = "80+" if x["id"] in older else "75+"
        tier = "Priority" if vacant or (0 < yr <= 1975 and single) else "Standard"
        pts, notes = 0, []
        for cond, p, txt in ((vacant, 2, "USPS vacant"), (age == "80+", 1, "owner 80+"), (single, 1, "single owner on the deed"),
                             (0 < yr <= 1975, 1, f"built {yr}"), (sen == "Over-65", 1, "HCAD over-65 confirmed"),
                             (bool(d and d <= 0.8 * ref["dom"]), 1, f"ZIP sells in {d or 0:.0f} days")):
            if cond:
                pts += p; notes.append(f"{txt} (+{p})")
        temp = "Hot" if pts >= 4 else "Warm" if pts >= 2 else "Cold"
        debt = 0 if eq >= 100 else round(value * (1 - eq / 100))
        first, last, _ = split_owner(h["mailto"])
        out.append({
            # SiftMap's address is DataSift's own postal form; HCAD appends building/unit codes ("100 MAIN
            # FOREST DR 10/45") that are no part of the street address
            "Property Street Address": x["address"].split(",")[0].strip(),
            "Property City": (x["address"].split(",")[1].strip() if x["address"].count(",") >= 2 else tc(h["site_addr_2"])),
            "Property State": "TX", "Property ZIP Code": h["site_addr_3"][:5], "Owner First Name": first, "Owner Last Name": last,
            "Mailing Street Address": street(h["mail_addr_1"]), "Mailing City": tc(h["mail_city"]),
            "Mailing State": h["mail_state"], "Mailing ZIP Code": h["mail_zip"][:5],
            "Lists": LIST, "Tags": ", ".join(["Harris", "Senior Equity"] + (["Vacant"] if vacant else []) + (["Free and Clear"] if eq >= 100 else [])),
            "Notes": (f"Harris senior / high-equity lead ({date.today():%m/%d/%Y}). Owner of record (HCAD): {h['mailto']}, "
                      f"account {h['acct']}; HCAD homestead {sen}; SiftMap owner age {age}. Equity {eq:.0f}% (SiftMap), "
                      f"estimated debt {money(debt)} on {money(value)} (SiftMap value; HCAD market value {money(float(h['tot_mkt_val'] or 0))}). "
                      f"Market Check {verdict}."),
            "Note Key": f"senior equity {h['acct']}", "Keep Owner On Existing": "Yes",
            # Estimated Value is SiftMap's: the figure every filter and the Market Check used
            "Estimated Value": f"{value:.0f}", "Parcel ID": h["acct"], "Year Built": str(yr or ""), "Living SqFt": h["bld_ar"],
            "County": "Harris", "Date Added": date.today().strftime("%m/%d/%Y"),
            "Senior Tier": tier, "Owner Age": age, "Senior Exemption": sen, "Equity Percent": f"{eq:.0f}",
            "Estimated Debt": str(debt), "Market Check": verdict,
            "ZIP Median Sale Price": f"{s:.0f}", "ZIP Median Home Value": f"{v:.0f}", "ZIP Days on Market": f"{d:.0f}",
            "ZIP Investor Transactions": f"{t:.0f}",
            "County Median Home Value": f"{ref['value']:.0f}", "County Days on Market": f"{ref['dom']:.0f}",
            "Price Band 60": f"{money(band[0])} - {money(band[1])}", "Investor Volume Band": f"{ref['tx'][0]:.0f} - {ref['tx'][1]:.0f} (6 months)",
            "Lead Temperature": temp, "Temperature Notes": (f"{temp} ({pts} pts): " + "; ".join(notes or ["no extra signal"]))[:500],
            "Property Temperature": TEMPERATURE_CRM.get(temp, "")})
    cols = list(dict.fromkeys(k for o in out for k in o))
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out)
    tiers = collections.Counter(o["Senior Tier"] for o in out)
    temps = collections.Counter(o["Lead Temperature"] for o in out)
    print(f"{len(out):,} leads -> {out_path} | tier {dict(tiers)} | {dict(temps)} | ZIPs {len({o['Property ZIP Code'] for o in out})} "
          f"| median value {money(statistics.median(float(o['Estimated Value'] or 0) for o in out)) if out else '-'}")
    print("left out: " + ", ".join(f"{k} {v:,}" for k, v in why.most_common()))


if __name__ == "__main__":
    main(*sys.argv[1:])
