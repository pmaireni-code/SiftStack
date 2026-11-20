"""Investor buyers with the most affinity for a property under contract: a report (English) for dispo. READ ONLY.

    python3 buyer_affinity.py "5003 Kilkenny Dr, Houston, TX 77048" [--radius 1.5] [--months 24] [--band 0.30]
                              [--top 20] [--out work/buyers/<name>.xlsx]

Operator's design (2026-11-20):
  1. Subject: the DataSift record (location) + SiftMap detail (beds, baths, sq ft, year, value, subdivision).
  2. Comparables: SiftMap single-family homes whose last sale in the last --months was an investor transaction
     (pending / wholesale / wholetail / flip / rental), inside a --radius mile box, kept when within the radius,
     estimated value within +-band, living area within +-band and beds +-1 of the subject.
  3. Investor buyers: every purchase in the window from each comparable's sale history whose buyer is an LLC,
     company or trust, or a person who bought 2+ of the comparables. Outcome per purchase: resold (days held) or
     still owns. Behavior: wholesaler (resells in <= 60 days), flipper (resells later), holder / landlord.
  4. Affinity score 0-100: purchases in the area 35, recency of the last one 25, similarity of what they bought 25,
     distance 15.
  5. Top --top buyers: people behind the entity (Texas Comptroller franchise-tax search, free) and a mailing
     address (SiftMap owner record when they still own a comparable, else the Comptroller's).
Texas does not disclose sale prices: values are estimated values, never sale prices. No skip trace, no paid
service, nothing written to DataSift (non-read calls are refused).
"""
import json, math, os, re, sys, time, urllib.error, urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from crm import get_crm  # noqa: E402
from siftmap_read import county_address  # noqa: E402
import entity_owner as EO  # noqa: E402
import trestle_export  # noqa: E402

MAP = "https://map.reisift.io"
INVESTOR_TYPES = ["pending", "wholesale", "wholetail", "flip", "rental"]
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
W_VOLUME, W_RECENCY, W_SIMILAR, W_DISTANCE = 35, 25, 25, 15


class SiftMap:
    def __init__(self, api, pause=0.35):
        self.api, self.pause = api, pause

    def _req(self, path, body=None):
        for attempt in range(5):
            h = {"accept": "application/json", "origin": "https://beta.reisift.io", "referer": "https://beta.reisift.io/",
                 "Authorization": "Bearer " + self.api.token}
            data = None
            if body is not None:
                h["Content-Type"] = "application/json"
                data = json.dumps(body).encode()
            req = urllib.request.Request(MAP + path, data=data, method="POST" if body is not None else "GET", headers=h)
            try:
                time.sleep(self.pause)
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt < 4:
                    self.api._mint()
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(4 * (attempt + 1))
                    continue
                raise RuntimeError(f"SiftMap HTTP {e.code} on {path}: {e.read().decode()[:200]}")
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 4:
                    time.sleep(4 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("gave up on " + path)

    def autocomplete(self, address):
        r = self._req("/properties/search-autocomplete/", {"search": address, "limit": 3})
        return (r.get("data") if isinstance(r, dict) else r) or []

    def detail(self, pid):
        return self._req(f"/properties/detail/{pid}/")

    def search_box(self, lat, lon, radius_mi, filters, fips="48201"):
        """Every row in a box around (lat, lon) (corners clockwise from north-east, as SiftMap wants)."""
        dlat = radius_mi / 69.0
        dlon = radius_mi / (69.0 * math.cos(math.radians(lat)))
        box = [{"lon": lon + dlon, "lat": lat + dlat}, {"lon": lon + dlon, "lat": lat - dlat},
               {"lon": lon - dlon, "lat": lat - dlat}, {"lon": lon - dlon, "lat": lat + dlat}]
        rows, idx = [], 1
        while True:
            r = self._req("/properties/search/", {"result_index": idx, "with_boundaries": False, "filters": filters,
                                                  "addresses": [county_address(fips)], "polygon": box})
            page = r.get("data") or []
            rows += page
            idx += len(page)
            if not page or len(rows) >= (r.get("total_results") or 0):
                return rows


def miles(a_lat, a_lon, b_lat, b_lon):
    return math.hypot((a_lat - b_lat) * 69.0, (a_lon - b_lon) * 69.0 * math.cos(math.radians(a_lat)))


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def norm_buyer(name):
    """'BOWES HOUSE 73 LLC' / 'Bowes House 73, L.L.C.' -> one key; several owners joined by ',' keep the first."""
    first = (name or "").split(",")[0] if not re.search(r",\s*(LLC|L\.L\.C|INC|LP|LTD)\b", name or "", re.I) else name
    return EO.norm(first or name)


def is_entity_name(name):
    return bool(EO.Q.ENTITY_WORDS.search(name or ""))


def parse_day(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def similarity(subject, comp):
    """0..1: how alike two houses are (value, living area, beds, year built)."""
    parts = []
    for key, scale in (("value", 0.30), ("sqft", 0.30)):
        a, b = subject.get(key), comp.get(key)
        if a and b:
            parts.append(max(0.0, 1 - abs(a - b) / (a * scale) / 1.0))
    if subject.get("beds") and comp.get("beds"):
        parts.append({0: 1.0, 1: 0.5}.get(abs(subject["beds"] - comp["beds"]), 0.0))
    if subject.get("year") and comp.get("year"):
        parts.append(max(0.0, 1 - abs(subject["year"] - comp["year"]) / 30))
    return sum(parts) / len(parts) if parts else 0.5


def purchases(detail, since, comp):
    """Investor-candidate purchases in the window from one comparable's sale history (newest first in SiftMap)."""
    sales = sorted(detail.get("sale_history") or [], key=lambda s: str(s.get("sale_date") or ""))
    out = []
    for i, s in enumerate(sales):
        d = parse_day(s.get("sale_date"))
        buyer = (s.get("buyer_name") or "").strip()
        if not d or d < since or not buyer:
            continue
        resold = None
        for later in sales[i + 1:]:
            if norm_buyer(later.get("seller_name")) == norm_buyer(buyer) and parse_day(later.get("sale_date")):
                resold = (parse_day(later.get("sale_date")) - d).days
                break
        out.append({"buyer": buyer, "key": norm_buyer(buyer), "date": d, "cash": bool(s.get("is_cash_sale")),
                    "resold_days": resold, **comp})
    return out


def behavior(buys):
    days = [b["resold_days"] for b in buys if b["resold_days"] is not None]
    if not days:
        return "Holder / landlord"
    if sum(1 for x in days if x <= 60) * 2 >= len(days):
        return "Wholesaler"
    return "Flipper" if len(days) * 2 >= len(buys) else "Flipper + holder"


def score(buys, subject, today, months, radius):
    n = len(buys)
    last = max(b["date"] for b in buys)
    age_m = (today - last).days / 30.4
    vol = W_VOLUME * min(n, 5) / 5
    rec = W_RECENCY * max(0.0, 1 - age_m / months)
    sim = W_SIMILAR * sum(similarity(subject, b) for b in buys) / n
    dist = W_DISTANCE * max(0.0, 1 - (sum(b["distance"] for b in buys) / n) / radius)
    return round(vol + rec + sim + dist, 1), {"volume": vol, "recency": rec, "similarity": sim, "distance": dist}


def why(buys, beh, subject):
    n, last = len(buys), max(b["date"] for b in buys)
    vals = sorted(b["value"] for b in buys if b.get("value"))
    rng = f"${vals[0]:,.0f}" + (f"-${vals[-1]:,.0f}" if len(vals) > 1 and vals[-1] != vals[0] else "") if vals else "n/a"
    near = min(b["distance"] for b in buys)
    held = [b["resold_days"] for b in buys if b["resold_days"] is not None]
    hold_txt = f"; resold after {min(held)}-{max(held)} days" if held else "; still owns them"
    cash = sum(b["cash"] for b in buys)
    return (f"{n} purchase(s) within {max(b['distance'] for b in buys):.1f} mi (closest {near:.1f} mi), last on "
            f"{last:%b %d, %Y}; similar homes valued {rng} vs subject ${subject['value']:,.0f}; {beh.lower()}"
            f"{hold_txt}; {cash} of {n} cash.")


def subject_record(crm, address):
    from datasift_api_upload import find_property
    m = re.match(r"\s*(.+?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})", address)
    if not m:
        raise SystemExit("address must look like '5003 Kilkenny Dr, Houston, TX 77048'")
    street, city, st, z = m.groups()
    pid = find_property(crm.api, {"street": street, "city": city, "state": st, "postal_code": z})
    if not pid:
        raise SystemExit("the subject is not in DataSift: its location comes from the record (add it first)")
    d = crm.api.call(f"/api/internal/property/{pid}/")
    a = d.get("address") or {}
    return pid, fnum(a.get("latitude")), fnum(a.get("longitude"))


def run(address, radius=1.5, months=24, band=0.30, top=20, out=None, today=None):
    today = today or date.today()
    since = today - timedelta(days=round(months * 30.44))
    crm = get_crm()
    trestle_export.read_only(crm.api)
    sm = SiftMap(crm.api)
    pid, lat, lon = subject_record(crm, address)
    ac = sm.autocomplete(address)
    if not ac:
        raise SystemExit("SiftMap did not find the subject")
    sd = sm.detail(ac[0]["id"])
    subject = {"address": address, "beds": fnum(sd.get("bedrooms")), "baths": fnum(sd.get("bathrooms")),
               "sqft": fnum(sd.get("living_square_feet")), "year": fnum(sd.get("years_built")),
               "value": fnum(sd.get("total_market_value")) or fnum(crm.api.call(f"/api/internal/property/{pid}/").get("estimate_value")),
               "subdivision": sd.get("subdivision"), "lat": lat, "lon": lon, "siftmap_id": ac[0]["id"]}
    print(f"SUBJECT: {address} | {subject['beds']:.0f} bd / {subject['baths']:.0f} ba, {subject['sqft']:,.0f} sq ft, "
          f"{subject['year']:.0f}, value ${subject['value']:,.0f}, {subject['subdivision']}")
    filters = {"type_single_family": True, "extra_last_sale_date_min": since.isoformat(),
               "extra_last_sale_investor_transaction_type": INVESTOR_TYPES}
    rows = sm.search_box(lat, lon, radius, filters)
    comps = []
    for r in rows:
        dist = miles(lat, lon, r["latitude"], r["longitude"])
        c = {"id": r["id"], "address": r["address"], "distance": round(dist, 2), "value": fnum(r.get("estimatedValue")),
             "sqft": fnum(r.get("squareFeet")), "beds": fnum(r.get("bedrooms")), "baths": fnum(r.get("bathrooms"))}
        if str(r["id"]) == str(subject["siftmap_id"]) or dist > radius:
            continue
        if not c["value"] or abs(c["value"] - subject["value"]) > band * subject["value"]:
            continue
        if c["sqft"] and subject["sqft"] and abs(c["sqft"] - subject["sqft"]) > band * subject["sqft"]:
            continue
        if c["beds"] and subject["beds"] and abs(c["beds"] - subject["beds"]) > 1:
            continue
        comps.append(c)
    print(f"COMPARABLES: {len(rows)} investor sales in the box; {len(comps)} within {radius} mi and the size/value band")
    buys, owners = [], {}
    for i, c in enumerate(comps, 1):
        d = sm.detail(c["id"])
        c["year"] = fnum(d.get("years_built"))
        c["subdivision"] = d.get("subdivision")
        oi = d.get("owner_info") or {}
        owners[c["id"]] = (oi.get("owner_name") or "", oi.get("owner_mail_address") or "")
        buys += purchases(d, since, c)
        if i % 50 == 0:
            print(f"  read {i}/{len(comps)}")
    persons = {}
    for b in buys:
        if not is_entity_name(b["buyer"]):
            persons[b["key"]] = persons.get(b["key"], 0) + 1
    by = {}
    for b in buys:
        if is_entity_name(b["buyer"]) or persons.get(b["key"], 0) >= 2:
            by.setdefault(b["key"], []).append(b)
    ranked = []
    for k, bl in by.items():
        sc, parts = score(bl, subject, today, months, radius)
        beh = behavior(bl)
        ranked.append({"key": k, "buyer": max((b["buyer"] for b in bl), key=len), "buys": sorted(bl, key=lambda b: b["date"], reverse=True),
                       "score": sc, "parts": parts, "behavior": beh, "why": why(bl, beh, subject)})
    ranked.sort(key=lambda r: (-r["score"], -len(r["buys"])))
    ranked = ranked[:top]
    print(f"BUYERS: {len(by)} investor buyers; researching the top {len(ranked)}")
    for r in ranked:
        mail = next((owners[b["id"]][1] for b in r["buys"] if norm_buyer(owners[b["id"]][0]) == r["key"]), "")
        r.update({"entity": "", "people": "", "agent": "", "status": "", "mail": mail, "match": "person (no entity lookup)"})
        if is_entity_name(r["buyer"]):
            try:
                kind, rec, cands = EO.lookup(r["buyer"])
            except Exception as e:  # noqa: BLE001
                kind, rec, cands = "error", None, [str(e)[:60]]
            if kind == "one":
                r["entity"] = rec.get("name") or ""
                r["status"] = rec.get("rightToTransactTX") or ""
                r["agent"] = rec.get("registeredAgentName") or ""
                r["people"] = "; ".join(f"{(o.get('AGNT_NM') or '').title()} ({(o.get('AGNT_TITL_TX') or '').title()})"
                                        for o in rec.get("officerInfo") or [])
                r["mail"] = r["mail"] or ", ".join(x for x in (rec.get("mailingAddressStreet"), rec.get("mailingAddressCity"),
                                                              rec.get("mailingAddressState"), EO.zip5(rec.get("mailingAddressZip"))) if x)
                r["match"] = "Texas Comptroller: exact name"
            else:
                r["match"] = {"several": "Texas Comptroller: several entities share the name",
                              "none": "Texas Comptroller: not found (may be registered in another state)"}.get(kind, f"lookup failed: {cands}")
    out = out or os.path.join(ROOT, "work", "buyers", re.sub(r"[^A-Za-z0-9]+", "_", address.split(",")[0]).strip("_")
                              + f"_buyers_{today:%Y%m%d}.xlsx")
    write_report(out, subject, ranked, comps, rows, radius, months, band, today)
    print(f"REPORT: {len(ranked)} buyers -> {out}")
    return out, ranked


def write_report(path, subject, ranked, comps, rows, radius, months, band, today):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Buyers"
    head = ["Rank", "Score", "Buyer", "Type", "Purchases in area", "Last purchase", "Texas entity", "Status",
            "People behind it", "Registered agent", "Mailing address", "Why", "Source of people"]
    ws.append(head)
    for i, r in enumerate(ranked, 1):
        ws.append([i, r["score"], r["buyer"], r["behavior"], len(r["buys"]), r["buys"][0]["date"].isoformat(), r["entity"],
                   r["status"], r["people"], r["agent"], r["mail"], r["why"], r["match"]])
    ev = wb.create_sheet("Evidence")
    ev.append(["Buyer", "Property bought", "Purchase date", "Estimated value", "Beds", "Baths", "Sq ft", "Year built",
               "Subdivision", "Distance (mi)", "Cash", "Outcome"])
    for r in ranked:
        for b in r["buys"]:
            ev.append([r["buyer"], b["address"], b["date"].isoformat(), b.get("value"), b.get("beds"), b.get("baths"),
                       b.get("sqft"), b.get("year"), b.get("subdivision"), b["distance"], "Yes" if b["cash"] else "No",
                       f"Resold after {b['resold_days']} days" if b["resold_days"] is not None else "Still owns it"])
    me = wb.create_sheet("Subject and method")
    for k, v in [("Subject", subject["address"]), ("Beds / baths", f"{subject['beds']:.0f} / {subject['baths']:.0f}"),
                 ("Living area", f"{subject['sqft']:,.0f} sq ft"), ("Year built", f"{subject['year']:.0f}"),
                 ("Estimated value", f"${subject['value']:,.0f}"), ("Subdivision", subject["subdivision"]), ("", ""),
                 ("Report date", today.isoformat()),
                 ("Comparables", f"single-family homes whose last sale in the last {months} months was an investor "
                                 f"transaction, within {radius} miles, value and living area within +-{band:.0%}, beds +-1"),
                 ("Investor sales found in the area", len(rows)), ("Comparables kept", len(comps)),
                 ("Investor buyer", "an LLC, company or trust, or a person who bought 2+ of the comparables"),
                 ("Score (0-100)", f"purchases in the area {W_VOLUME}, recency {W_RECENCY}, similarity {W_SIMILAR}, distance {W_DISTANCE}"),
                 ("Type", "Wholesaler: resells within 60 days; Flipper: resells later; Holder / landlord: still owns"),
                 ("Values", "Texas does not disclose sale prices: all values are estimated values (SiftMap)."),
                 ("Sources", "SiftMap (DataSift), Texas Comptroller franchise-tax search (public). No skip trace.")]:
        me.append([k, v])
    bold, fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E78")
    for sheet, widths in ((ws, [6, 7, 34, 18, 10, 13, 34, 9, 46, 30, 44, 90, 34]),
                          (ev, [34, 40, 13, 14, 6, 6, 8, 10, 28, 12, 7, 24]), (me, [32, 110])):
        for c in sheet[1]:
            if sheet is not me:
                c.font, c.fill = bold, fill
        for i, w in enumerate(widths):
            sheet.column_dimensions[chr(65 + i)].width = w
        sheet.freeze_panes = "A2" if sheet is not me else None
    for row in ws.iter_rows(min_row=2):
        row[11].alignment = Alignment(wrap_text=True, vertical="top")
    for row in ev.iter_rows(min_row=2):
        row[3].number_format = "$#,##0"
    wb.save(path)


def main(*argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    opt = lambda k, d: type(d)(argv[argv.index(k) + 1]) if k in argv else d
    run(args[0], radius=opt("--radius", 1.5), months=opt("--months", 24), band=opt("--band", 0.30), top=opt("--top", 20),
        out=opt("--out", "") or None)


if __name__ == "__main__":
    main(*sys.argv[1:])
