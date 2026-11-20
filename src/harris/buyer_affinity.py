"""Investor buyers with the most affinity for a property under contract: a report (English) for dispo. READ ONLY.

    python3 buyer_affinity.py "5003 Kilkenny Dr, Houston, TX 77048" [--radius 1.5] [--months 24] [--band 0.30]
                              [--top 20] [--out work/buyers/<name>.xlsx] [--sheet]

Operator's design (2026-11-20):
  1. Subject: the DataSift record (location) + SiftMap detail (beds, baths, sq ft, year, value, subdivision).
  2. Comparables: SiftMap single-family homes whose last sale in the last --months was an investor transaction
     (pending / wholesale / wholetail / flip / rental), inside a --radius mile box, kept when within the radius,
     estimated value within +-band, living area within +-band and beds +-1 of the subject.
  3. Investor buyers: every purchase in the window from each comparable's sale history whose buyer is an LLC,
     company or trust, or a person who bought 2+ of the comparables. Outcome per purchase: resold (days held) or
     still owns. Behavior: wholesaler (resells in <= 60 days), flipper (resells later), holder / landlord.
  4. One buyer, several companies: companies that share a manager / member / officer in the Texas Comptroller (or
     a parent company) are ONE buyer, purchases added up and every company listed.
  5. Affinity score 0-100: purchases in the area 35, recency of the last one 25, similarity of what they bought 25,
     distance 15; a wholesaler's score x0.75 so end buyers (flip or hold) rank first. Wholesalers stay, marked.
  6. Contact, public sources only: HCAD mailing address (appraisal roll index work/hcad/idx.tsv), registered agent
     and its address, Comptroller mailing. Several entities with the same name, no people listed, not found, or a
     company as manager -> "Review by hand".
  7. Output: Excel in work/buyers/; with --sheet also the Google Sheet (tabs Compradores, Compras, Reportes; see
     buyers_sheet.py: no duplicates, rows never deleted, the operator's columns never written).
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


TYPE_WEIGHT = {"Wholesaler": 0.75}  # operator 2026-11-20: end buyers (flip or hold) weigh more than wholesalers
PERSON_SUFFIX = {"JR", "SR", "II", "III", "IV"}
CACHE_DAYS = 30


def behavior(buys):
    days = [b["resold_days"] for b in buys if b["resold_days"] is not None]
    if not days:
        return "Holder / landlord"
    if sum(1 for x in days if x <= 60) * 2 >= len(days):
        return "Wholesaler"
    return "Flipper" if len(days) * 2 >= len(buys) else "Flipper + holder"


def score(buys, subject, today, months, radius, beh):
    """0-100: purchases in the area 35, recency 25, similarity 25, distance 15; times 0.75 for a wholesaler."""
    n = len(buys)
    last = max(b["date"] for b in buys)
    age_m = (today - last).days / 30.4
    parts = {"volume": W_VOLUME * min(n, 5) / 5, "recency": W_RECENCY * max(0.0, 1 - age_m / months),
             "similarity": W_SIMILAR * sum(similarity(subject, b) for b in buys) / n,
             "distance": W_DISTANCE * max(0.0, 1 - (sum(b["distance"] for b in buys) / n) / radius)}
    parts["type_weight"] = TYPE_WEIGHT.get(beh, 1.0)
    return round(sum(v for k, v in parts.items() if k != "type_weight") * parts["type_weight"], 1), parts


def why(buys, beh, subject, linked_by=""):
    n, last = len(buys), max(b["date"] for b in buys)
    vals = sorted(b["value"] for b in buys if b.get("value"))
    rng = f"${vals[0]:,.0f}" + (f"-${vals[-1]:,.0f}" if len(vals) > 1 and vals[-1] != vals[0] else "") if vals else "n/a"
    near = min(b["distance"] for b in buys)
    held = [b["resold_days"] for b in buys if b["resold_days"] is not None]
    hold_txt = f"; resold after {min(held)}-{max(held)} days" if held else "; still owns them"
    cash = sum(b["cash"] for b in buys)
    out = (f"{n} purchase(s) within {max(b['distance'] for b in buys):.1f} mi (closest {near:.1f} mi), last on "
           f"{last:%b %d, %Y}; similar homes valued {rng} vs subject ${subject['value']:,.0f}; {beh.lower()}"
           f"{hold_txt}; {cash} of {n} cash.")
    if beh == "Wholesaler":
        out += " Wholesaler: score weighted x0.75 (end buyers first)."
    if linked_by:
        out += f" Companies grouped: same {linked_by}."
    return out


# ---- people behind a buyer, and grouping companies that share them

def person_sig(name):
    """'ESCOBAR GIOVANNI A' and 'Giovanni Escobar' -> the same token set (order and initials ignored)."""
    toks = [t for t in re.sub(r"[^A-Z ]", " ", (name or "").upper()).split() if len(t) > 1 and t not in PERSON_SUFFIX]
    return frozenset(toks) if len(toks) >= 2 else None


def sig_match(a, b):
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return len(small) >= 2 and small <= big


def group_nodes(nodes):
    """nodes: {key: {"people": {name}, "entities": {norm}}} -> [(set of keys, linked_by)]. Two buyers are one when
    they share a manager / member / officer (a person, or a parent company), or one IS the other's officer."""
    keys = list(nodes)
    parent = {k: k for k in keys}
    link = {}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    sigs = {k: [(person_sig(p), p) for p in nodes[k]["people"] if person_sig(p)] for k in keys}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            hit = next((pa.title() for sa, pa in sigs[a] for sb, _ in sigs[b] if sig_match(sa, sb)), None)
            if not hit:
                common = nodes[a]["entities"] & nodes[b]["entities"]
                hit = sorted(common)[0].title() if common else None
            if hit:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra
                link[frozenset((a, b))] = hit
    groups = {}
    for k in keys:
        groups.setdefault(find(k), set()).add(k)
    out = []
    for g in groups.values():
        names = sorted({v for p, v in link.items() if p <= g})
        out.append((g, names[0] if names else ""))
    return out


# ---- public sources: Texas Comptroller (cached) and the HCAD roll

def _cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def comptroller(names, cache_path, today):
    """{name: (kind, rec, cands)} for entity names; answers kept CACHE_DAYS days in work/buyers."""
    cache, out = _cache(cache_path), {}
    for i, name in enumerate(names, 1):
        k = EO.norm(name)
        hit = cache.get(k)
        if hit and (today - date.fromisoformat(hit["t"])).days <= CACHE_DAYS:
            out[name] = (hit["kind"], hit["rec"], hit["cands"])
            continue
        try:
            kind, rec, cands = EO.lookup(name)
        except Exception as e:  # noqa: BLE001
            out[name] = ("error", None, [str(e)[:60]])
            continue
        cache[k] = {"t": today.isoformat(), "kind": kind, "rec": rec, "cands": cands}
        out[name] = (kind, rec, cands)
        if i % 20 == 0:
            print(f"  Comptroller {i}/{len(names)}")
            _save(cache_path, cache)
    _save(cache_path, cache)
    return out


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f)
    os.replace(path + ".tmp", path)


def hcad_mailing(keys, idx_path):
    """{norm owner name: {"addr": most used mailing address, "n": Harris accounts}} from the HCAD roll index
    (acct, mailto, mail_addr_1, mail_city, mail_state, mail_zip, ...)."""
    want = set(keys)
    firsts = {k.split()[0] for k in want if k}
    seen = {}
    if not os.path.exists(idx_path):
        print(f"  WARN no HCAD index at {idx_path}: mailing addresses left blank")
        return {}
    with open(idx_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.split("\t", 7)
            if len(p) < 7 or not p[1] or p[1].split()[0] not in firsts:
                continue
            k = EO.norm(p[1])
            if k not in want:
                continue
            addr = f"{' '.join(p[2].split())}, {p[3].strip()}, {p[4].strip()} {EO.zip5(p[5])}".strip(", ")
            seen.setdefault(k, {}).setdefault(addr, 0)
            seen[k][addr] += 1
    return {k: {"addr": max(v, key=v.get), "n": sum(v.values())} for k, v in seen.items()}


def fmt_addr(*parts):
    return ", ".join(" ".join(str(x).split()) for x in parts if x and str(x).strip())


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
    return pid, fnum(a.get("latitude")), fnum(a.get("longitude")), d


def cached_detail(sm, pid, cache_dir, today):
    path = os.path.join(cache_dir, f"{pid}.json")
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < 7 * 86400:
        with open(path) as f:
            return json.load(f)
    d = sm.detail(pid)
    _save(path, d)
    return d


def build_buyers(buys, subject, today, months, radius, lookups, hcad):
    """Group, score and describe every investor buyer. -> list of buyer dicts, best first."""
    by_key = {}
    for b in buys:
        by_key.setdefault(b["key"], []).append(b)
    names = {k: max((b["buyer"] for b in bl), key=len) for k, bl in by_key.items()}
    nodes = {}
    for k in by_key:
        people, ents = set(), ({k} if is_entity_name(names[k]) else set())
        if not is_entity_name(names[k]):
            people.add(names[k])
        kind, rec, _ = lookups.get(names[k], ("person", None, []))
        for o in (rec or {}).get("officerInfo") or []:
            n = (o.get("AGNT_NM") or "").strip()
            (ents.add(EO.norm(n)) if is_entity_name(n) else people.add(n)) if n else None
        nodes[k] = {"people": people, "entities": ents}
    out = []
    for g, linked in group_nodes(nodes):
        bl = [b for k in g for b in by_key[k]]
        has_entity = any(is_entity_name(names[k]) for k in g)
        if not has_entity and len(bl) < 2:  # a person with one purchase is not an investor buyer
            continue
        keys = sorted(g, key=lambda k: (-len(by_key[k]), k))
        beh = behavior(bl)
        sc, parts = score(bl, subject, today, months, radius, beh)
        companies, people, agents, cmail, hmail, review, src, n_harris = [], {}, [], [], [], [], [], 0
        for k in keys:
            nm = names[k]
            kind, rec, cands = lookups.get(nm, ("person", None, []))
            h = hcad.get(k)
            if h:
                hmail.append((f"{nm}: " if len(keys) > 1 else "") + h["addr"])
                n_harris += h["n"]
            if not is_entity_name(nm):
                src.append(f"{nm}: person (no entity lookup)")
                continue
            companies.append(nm)
            if kind == "one":
                rec = rec or {}
                offs = rec.get("officerInfo") or []
                for o in offs:
                    n = (o.get("AGNT_NM") or "").title().strip()
                    if n:
                        people.setdefault(n, set()).add((o.get("AGNT_TITL_TX") or "").title().strip())
                        if is_entity_name(n):
                            review.append(f"{nm}: its manager is a company ({n}); look that one up")
                agents.append(fmt_addr(rec.get("registeredAgentName"), rec.get("registeredOfficeAddressStreet"),
                                       rec.get("registeredOfficeAddressCity"),
                                       f"{rec.get('registeredOfficeAddressState') or ''} {EO.zip5(rec.get('registeredOfficeAddressZip'))}"))
                cmail.append(fmt_addr(rec.get("mailingAddressStreet"), rec.get("mailingAddressCity"),
                                      f"{rec.get('mailingAddressState') or ''} {EO.zip5(rec.get('mailingAddressZip'))}"))
                src.append(f"{nm}: Texas Comptroller, exact name ({rec.get('rightToTransactTX') or 'status unknown'})")
                if not offs:
                    review.append(f"{nm}: the Comptroller lists no people")
            elif kind == "several":
                review.append(f"{nm}: several Texas entities share the name ({'; '.join(cands[:4])})")
                src.append(f"{nm}: Texas Comptroller, several entities share the name")
            elif kind == "none":
                review.append(f"{nm}: not found in the Texas Comptroller (may be registered in another state)")
                src.append(f"{nm}: Texas Comptroller, not found")
            else:
                review.append(f"{nm}: Comptroller lookup failed, run again")
                src.append(f"{nm}: lookup failed")
        display = names[keys[0]] if len(keys) == 1 else f"{linked} ({len(keys)} companies)"
        out.append({"id": "B:" + sorted(g)[0], "keys": sorted(g), "buyer": display, "companies": companies or [names[k] for k in keys],
                    "people": "; ".join(f"{n} ({', '.join(sorted(t for t in ts if t))})" if any(ts) else n
                                        for n, ts in people.items()),
                    "behavior": beh, "wholesaler": beh == "Wholesaler", "score": sc, "parts": parts,
                    "buys": sorted(bl, key=lambda b: b["date"], reverse=True), "why": why(bl, beh, subject, linked if len(keys) > 1 else ""),
                    "hcad_mail": "; ".join(dict.fromkeys(hmail)), "harris_n": n_harris,
                    "agent": "; ".join(dict.fromkeys(a for a in agents if a)), "comp_mail": "; ".join(dict.fromkeys(c for c in cmail if c)),
                    "review": "; ".join(dict.fromkeys(review)), "source": "; ".join(src)})
    out.sort(key=lambda r: (-r["score"], -len(r["buys"])))
    return out


def run(address, radius=1.5, months=24, band=0.30, top=20, out=None, today=None, sheet=False):
    today = today or date.today()
    since = today - timedelta(days=round(months * 30.44))
    cache_dir = os.path.join(ROOT, "work", "buyers", "cache")
    if sheet:  # fail before the slow part when the sheet is not reachable
        import buyers_sheet as BS
        sid, key = os.environ.get("BUYERS_SHEET_ID", "").strip(), BS.load_key()
        if not (sid and key):
            raise SystemExit("--sheet needs BUYERS_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_KEY in the environment")
        client = BS.SheetsClient(sid, key)
        client.tabs()
    crm = get_crm()
    trestle_export.read_only(crm.api)
    sm = SiftMap(crm.api)
    pid, lat, lon, rec = subject_record(crm, address)
    ac = sm.autocomplete(address)
    if not ac:
        raise SystemExit("SiftMap did not find the subject")
    sd = sm.detail(ac[0]["id"])
    subject = {"address": address, "beds": fnum(sd.get("bedrooms")), "baths": fnum(sd.get("bathrooms")),
               "sqft": fnum(sd.get("living_square_feet")), "year": fnum(sd.get("years_built")),
               "value": fnum(sd.get("total_market_value")) or fnum(rec.get("estimate_value")),
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
    buys = []
    for i, c in enumerate(comps, 1):
        d = cached_detail(sm, c["id"], os.path.join(cache_dir, "siftmap"), today)
        c["year"] = fnum(d.get("years_built"))
        c["subdivision"] = d.get("subdivision")
        buys += purchases(d, since, c)
        if i % 50 == 0:
            print(f"  read {i}/{len(comps)}")
    keys = {b["key"] for b in buys}
    names = {}
    for b in buys:
        names[b["key"]] = max(names.get(b["key"], ""), b["buyer"], key=len)
    hcad = hcad_mailing(keys, os.path.join(ROOT, "work", "hcad", "idx.tsv"))
    ents = sorted(n for n in names.values() if is_entity_name(n))
    print(f"PUBLIC SOURCES: HCAD mailing for {len(hcad)} of {len(keys)} buyers; Texas Comptroller for {len(ents)} companies")
    lookups = comptroller(ents, os.path.join(cache_dir, "comptroller.json"), today)
    buyers = build_buyers(buys, subject, today, months, radius, lookups, hcad)
    ranked = buyers[:top]
    grouped = sum(1 for b in buyers if len(b["keys"]) > 1)
    print(f"BUYERS: {len(buyers)} investor buyers ({grouped} groups of companies with the same people); top {len(ranked)}")
    out = out or os.path.join(ROOT, "work", "buyers", re.sub(r"[^A-Za-z0-9]+", "_", address.split(",")[0]).strip("_")
                              + f"_buyers_{today:%Y%m%d}.xlsx")
    write_report(out, subject, ranked, comps, rows, radius, months, band, today)
    print(f"REPORT: {len(ranked)} buyers -> {out}")
    if sheet:
        res = write_sheet(client, subject, ranked, today)
        for tab, r in res.items():
            print(f"SHEET {tab}: {r['added']} added, {r['updated']} updated, {r['unchanged']} unchanged; "
                  f"{r['user_cells']} of your cells read back unchanged")
            for d, hits in r["conflicts"]:
                print(f"  NOTE rows {hits} in {tab} are now one buyer ({d.get('Buyer')}): merge them by hand if you want")
    return out, ranked


# ---- Google Sheets: Compradores / Compras / Reportes

BUYER_COLS = ["Buyer ID", "Company keys", "Buyer", "Companies", "People behind it", "Type", "Wholesaler", "Review by hand",
              "HCAD mailing address", "Harris properties (HCAD)", "Registered agent and address", "Comptroller mailing",
              "Last purchase seen", "Last report", "Updated"]
PURCHASE_COLS = ["Purchase ID", "Buyer ID", "Buyer", "Bought as", "Property", "Purchase date", "Estimated value", "Beds",
                 "Baths", "Sq ft", "Year built", "Subdivision", "Cash", "Outcome", "Updated"]
REPORT_COLS = ["Report row ID", "Report date", "Subject", "Rank", "Score", "Buyer ID", "Buyer", "Type", "Wholesaler",
               "Purchases in area", "Last purchase", "Why", "Review by hand"]


def sheet_rows(subject, ranked, today):
    buyers, purchases_, reports = [], [], []
    for i, r in enumerate(ranked, 1):
        buyers.append({"Buyer ID": r["id"], "Company keys": " | ".join(r["keys"]), "Buyer": r["buyer"],
                       "Companies": "; ".join(r["companies"]), "People behind it": r["people"], "Type": r["behavior"],
                       "Wholesaler": "Yes" if r["wholesaler"] else "No", "Review by hand": r["review"],
                       "HCAD mailing address": r["hcad_mail"], "Harris properties (HCAD)": r["harris_n"] or "",
                       "Registered agent and address": r["agent"], "Comptroller mailing": r["comp_mail"],
                       "Last purchase seen": r["buys"][0]["date"].isoformat(),
                       "Last report": f"{subject['address']} ({today.isoformat()}): rank {i}, score {r['score']}",
                       "Updated": today.isoformat()})
        reports.append({"Report row ID": f"{subject['address']}|{today.isoformat()}|{r['id']}", "Report date": today.isoformat(),
                        "Subject": subject["address"], "Rank": i, "Score": r["score"], "Buyer ID": r["id"], "Buyer": r["buyer"],
                        "Type": r["behavior"], "Wholesaler": "Yes" if r["wholesaler"] else "No", "Purchases in area": len(r["buys"]),
                        "Last purchase": r["buys"][0]["date"].isoformat(), "Why": r["why"], "Review by hand": r["review"]})
        for b in r["buys"]:
            purchases_.append({"Purchase ID": f"{b['key']}|{b['address']}|{b['date'].isoformat()}", "Buyer ID": r["id"],
                               "Buyer": r["buyer"], "Bought as": b["buyer"], "Property": b["address"],
                               "Purchase date": b["date"].isoformat(), "Estimated value": b.get("value"), "Beds": b.get("beds"),
                               "Baths": b.get("baths"), "Sq ft": b.get("sqft"), "Year built": b.get("year"),
                               "Subdivision": b.get("subdivision") or "", "Cash": "Yes" if b["cash"] else "No",
                               "Outcome": f"Resold after {b['resold_days']} days" if b["resold_days"] is not None else "Still owns it",
                               "Updated": today.isoformat()})
    return buyers, purchases_, reports


def buyer_keys(d):
    ks = {"id:" + str(d.get("Buyer ID") or "").strip()} - {"id:"}
    return ks | {"k:" + k.strip() for k in str(d.get("Company keys") or "").split("|") if k.strip()}


def write_sheet(client, subject, ranked, today):
    import buyers_sheet as BS
    buyers, purchases_, reports = sheet_rows(subject, ranked, today)
    one = lambda col: (lambda d: {str(d.get(col) or "").strip()} - {""})
    return {"Compradores": BS.sync(client, "Compradores", BUYER_COLS, buyers, buyer_keys, BS.USER_COLUMNS),
            "Compras": BS.sync(client, "Compras", PURCHASE_COLS, purchases_, one("Purchase ID")),
            "Reportes": BS.sync(client, "Reportes", REPORT_COLS, reports, one("Report row ID"))}


def write_report(path, subject, ranked, comps, rows, radius, months, band, today):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Buyers"
    head = ["Rank", "Score", "Buyer", "Companies", "Type", "Wholesaler", "Purchases in area", "Last purchase",
            "People behind it", "HCAD mailing address", "Harris properties (HCAD)", "Registered agent and address",
            "Comptroller mailing", "Review by hand", "Why", "Sources"]
    ws.append(head)
    for i, r in enumerate(ranked, 1):
        ws.append([i, r["score"], r["buyer"], "; ".join(r["companies"]), r["behavior"], "Yes" if r["wholesaler"] else "No",
                   len(r["buys"]), r["buys"][0]["date"].isoformat(), r["people"], r["hcad_mail"], r["harris_n"] or "",
                   r["agent"], r["comp_mail"], r["review"], r["why"], r["source"]])
    ev = wb.create_sheet("Evidence")
    ev.append(["Buyer", "Bought as", "Property bought", "Purchase date", "Estimated value", "Beds", "Baths", "Sq ft",
               "Year built", "Subdivision", "Distance (mi)", "Cash", "Outcome"])
    for r in ranked:
        for b in r["buys"]:
            ev.append([r["buyer"], b["buyer"], b["address"], b["date"].isoformat(), b.get("value"), b.get("beds"), b.get("baths"),
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
                 ("One buyer, several companies", "companies that share a manager, member or officer (Texas Comptroller), "
                                                  "or a parent company, count as ONE buyer: purchases added up, every company listed"),
                 ("Score (0-100)", f"purchases in the area {W_VOLUME}, recency {W_RECENCY}, similarity {W_SIMILAR}, "
                                   f"distance {W_DISTANCE}; a wholesaler's score is multiplied by {TYPE_WEIGHT['Wholesaler']} "
                                   "so end buyers (flip or hold) rank first. Wholesalers stay on the list, marked."),
                 ("Type", "Wholesaler: resells within 60 days; Flipper: resells later; Holder / landlord: still owns"),
                 ("Review by hand", "several Texas entities share the name, the Comptroller lists no people, the name is "
                                    "not found, or the manager is itself a company"),
                 ("Contact", "HCAD mailing address (Harris County appraisal roll, the address the tax bills go to), the "
                             "registered agent and its address and the mailing address on file with the Texas Comptroller. "
                             "The registered agent is often a lawyer or an agent service, not the buyer."),
                 ("Values", "Texas does not disclose sale prices: all values are estimated values (SiftMap)."),
                 ("Sources", "SiftMap (DataSift), Texas Comptroller franchise-tax search, HCAD roll (public). No skip trace.")]:
        me.append([k, v])
    bold, fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E78")
    for sheet, widths in ((ws, [6, 7, 34, 40, 18, 11, 10, 13, 46, 44, 11, 50, 44, 50, 90, 50]),
                          (ev, [34, 34, 40, 13, 14, 6, 6, 8, 10, 28, 12, 7, 24]), (me, [32, 110])):
        for c in sheet[1]:
            if sheet is not me:
                c.font, c.fill = bold, fill
        for i, w in enumerate(widths):
            sheet.column_dimensions[chr(65 + i)].width = w
        sheet.freeze_panes = "A2" if sheet is not me else None
    for row in ws.iter_rows(min_row=2):
        for c in row[13:16]:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    for row in ev.iter_rows(min_row=2):
        row[4].number_format = "$#,##0"
    wb.save(path)


def main(*argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    opt = lambda k, d: type(d)(argv[argv.index(k) + 1]) if k in argv else d
    run(args[0], radius=opt("--radius", 1.5), months=opt("--months", 24), band=opt("--band", 0.30), top=opt("--top", 20),
        out=opt("--out", "") or None, sheet="--sheet" in argv)


if __name__ == "__main__":
    main(*sys.argv[1:])
