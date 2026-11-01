"""DataSift Market Finder data for one county, refreshed on every run (never cached between runs).

    python3 market_datasift.py <out.json> [--fips 48201]

Reads two Market Finder routes the app itself calls (internal, undocumented, verified 2026-11-01):
- /market-finder/buy-box/recommendations/?fips[]=  the county's "Recommended Filters": the property
  profile that covers most past investor deals (sqft, year built, beds, baths, value ranges)
- /market-finder/paginated/?fips[]=&geoGroup=zip   ~35 metrics per ZIP; investor transactions over
  6 months is the one the grade uses
- the same route with geoGroup=county                the county's own metrics, the reference a ZIP is
  compared with in the senior list's Market Check (median days on market, value, sale price)
This is a data source, not the CRM: it stays DataSift even if the leads move to another CRM.
"""
import json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm.datasift import PatientApi  # noqa: E402

HARRIS_FIPS = "48201"


def fetch(fips=HARRIS_FIPS, api=None):
    api = api or PatientApi()
    rec = api.call(f"/api/internal/market-finder/buy-box/recommendations/?fips[]={fips}")
    box = (rec.get("data") or [None])[0]
    if not box:
        raise SystemExit(f"Market Finder returned no buy box for FIPS {fips}")
    zips, offset = {}, 0
    while True:
        page = api.call(f"/api/internal/market-finder/paginated/?fips[]={fips}&offset={offset}&limit=1000&geoGroup=zip")
        for row in page.get("data") or []:
            z = str((row.get("property_fields") or {}).get("zip") or "")
            if z:
                zips[z] = row.get("metrics") or {}
        offset += len(page.get("data") or [])
        if not page.get("data") or offset >= (page.get("total") or 0):
            break
    if not zips:
        raise SystemExit(f"Market Finder returned no ZIP metrics for FIPS {fips}")
    # The county as a whole: the reference a ZIP is compared with (days on market, median value / sale price)
    cty = api.call(f"/api/internal/market-finder/paginated/?fips[]={fips}&offset=0&limit=1&geoGroup=county")
    county = ((cty.get("data") or [{}])[0]).get("metrics") or {}
    return {"fetched_at": datetime.now().isoformat(timespec="seconds"), "fips": fips, "buy_box": box, "zips": zips,
            "county": county}


if __name__ == "__main__":
    args = sys.argv[1:]
    fips = args[args.index("--fips") + 1] if "--fips" in args else HARRIS_FIPS
    data = fetch(fips)
    json.dump(data, open(args[0], "w"))
    b = data["buy_box"]
    print(f"Market Finder {fips}: buy box sqft {b['living_area_sqft_low']}-{b['living_area_sqft_high']}, "
          f"year {b['year_built_low']}-{b['year_built_high']}, beds {b['bedrooms_count_low']}-{b['bedrooms_count_high']}, "
          f"baths {b['bath_count_low']}-{b['bath_count_high']}, value {b['property_value_low']}-{b['property_value_high']} | "
          f"{len(data['zips'])} ZIPs")
