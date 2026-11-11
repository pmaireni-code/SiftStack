"""Harris senior / free-and-clear candidates from SiftMap (DataSift's property search). READ-ONLY.

    python3 senior_sweep.py <hcad_idx.tsv> <out.json>

SiftMap search costs nothing and adds nothing to the CRM (add-properties-by-query would spend the
record allowance; it is never called). Two searches, both Harris single family, owner occupied,
not listed, built 1990 or earlier, 2-5 bedrooms, owner 75+ (SiftMap's estimated age):
  main     free and clear (equity 100%), value from the middle-60% floor up to MAX_VALUE
  vacant   equity 70%+, value anywhere in the middle 60%, USPS vacant
plus the ids of the same searches at 80+, for the temperature.

Middle 60% = Harris single-family market values (HCAD class A1) with the cheapest 20% and the dearest
20% cut off (operator's definition, 2026-11-01): below it deferred maintenance, above it institutional
buyers. Measured on the 2026 roll: $201,883 - $432,887.

Contract, verified live 2026-11-01 by count delta (an unknown key is SILENTLY ignored):
  extra_equity_percent_min, extra_owner_age_min, extra_year_built_max, beds_min/beds_max,
  preset_owner_occupied, preset_vacant work. equity_percent_min, year_built_max, sqft_*, baths_*,
  owner_age_min, preset_senior are ignored. result_index is the 1-based ROW OFFSET, not a page
  number: paging by 1 walks the same rows one at a time (516 calls, 765 distinct rows).
"""
import json, os, sys

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")]
from datasift_api_upload import Api  # noqa: E402
import siftmap_read as sp  # noqa: E402  (read-only SiftMap search; siftmap_pull is archived)

MAX_VALUE = 300000   # operator's cap, 2026-11-01
MIN_AGE, OLDER = 75, 80
PAGE = 250


def middle_band(idx_path):
    """(p20, p80) of Harris single-family market values on the HCAD roll."""
    v = []
    with open(idx_path, encoding="latin-1") as f:
        for line in f:
            c = line.split("\t")
            if len(c) > 10 and c[9] == "A1":
                try:
                    x = float(c[10])
                except ValueError:
                    continue
                if x > 1000:
                    v.append(x)
    v.sort()
    return round(v[int(0.2 * (len(v) - 1))]), round(v[int(0.8 * (len(v) - 1))])


def sweep(m, filters, ids_only=False):
    out, i = {}, 1
    while True:
        r = m._post("/properties/search/", {"result_index": i, "with_boundaries": False, "filters": filters,
                                             "addresses": [sp.county_address("48201", rich=False)], "polygon": sp.WORLD})
        rows = r.get("data") or []
        for x in rows:
            x.pop("parcel_boundary", None)
            out[x["id"]] = x["id"] if ids_only else x
        total = r.get("total_results") or 0
        if not rows or i + PAGE > total:
            if len(out) < total:
                print(f"  WARN: {len(out)} of {total} rows read", flush=True)
            return out
        i += PAGE


def main(idx_path, out_path):
    lo, hi = middle_band(idx_path)
    print(f"middle 60% of Harris single-family values: ${lo:,} - ${hi:,}")
    m = sp.Map(Api())
    common = {"type_single_family": True, "preset_owner_occupied": True, "extra_mls_status": ["not_listed"],
              "extra_year_built_max": 1990, "beds_min": 2, "beds_max": 5}
    main_f = dict(common, value_min=lo, value_max=MAX_VALUE, extra_equity_percent_min=100)
    vac_f = dict(common, value_min=lo, value_max=hi, extra_equity_percent_min=70, preset_vacant=True)
    rows = {}
    for name, f in (("main", main_f), ("vacant", vac_f)):
        got = sweep(m, dict(f, extra_owner_age_min=MIN_AGE))
        for k, x in got.items():
            x["_sweep"] = name if k not in rows else rows[k]["_sweep"]
            rows[k] = x
        print(f"{name}: {len(got):,} homes, owner {MIN_AGE}+", flush=True)
    older = set()
    for f in (main_f, vac_f):
        older |= set(sweep(m, dict(f, extra_owner_age_min=OLDER), ids_only=True))
    print(f"{OLDER}+: {len(older):,}")
    json.dump({"band": [lo, hi], "max_value": MAX_VALUE, "rows": rows, "older": sorted(older)}, open(out_path, "w"))
    print(f"{len(rows):,} distinct homes -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:])
