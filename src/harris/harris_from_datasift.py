"""Rebuild the working leads CSV from DataSift itself, so a scheduled run needs no local state.

    python3 harris_from_datasift.py <out.csv> [--list "Harris - Foreclosure"]

Every scheduled run starts in a fresh, empty cloud container, so DataSift is the source of truth:
this reads every record on the Harris list with its address, tags and the Foreclosure custom fields
(Auction Date, Starting Bid, Foreclosure Status) and writes the columns harris_bid_updater.py reads,
plus the record uuid so the write-back needs no address lookup. Read-only.
"""
import csv, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harris_upload import PatientApi  # noqa: E402

COLS = ["uuid", "Property Street Address", "Property ZIP Code", "Foreclosure Date", "Auction Date", "Contract Deadline",
        "Foreclosure Status", "Starting Bid", "Tags"]


def iso_to_us(v):
    v = str(v or "")[:10]
    return f"{v[5:7]}/{v[8:10]}/{v[:4]}" if len(v) == 10 and v[4] == "-" else v


def main(out_path, *flags):
    title = flags[flags.index("--list") + 1] if "--list" in flags else "Harris - Foreclosure"
    api = PatientApi()
    lists = api.call("/api/internal/list/?limit=999")
    lists = lists if isinstance(lists, list) else lists.get("results") or []
    lst = next((x for x in lists if (x.get("title") or "").strip().lower() == title.lower()), None)
    if not lst:
        sys.exit(f"no list named {title!r} in the account")
    fields = api.call("/api/internal/custom-fields/?limit=999")
    options = {o.get("uuid"): o.get("label") for f in (fields.get("results") or []) for o in (f.get("options") or [])}

    rows, offset = [], 0
    while True:
        res = api.call("/api/internal/property/", "POST", {"limit": 100, "offset": offset, "query": {"must": {
            "property_type": "clean", "any_lists": [lst["uuid"]]}}}, headers={"x-http-method-override": "GET"})
        page = res.get("results") or res.get("data") or []
        for rec in page:
            uuid = rec["uuid"]
            cf = api.call(f"/api/internal/property/{uuid}/custom-field/")
            vals = {(r.get("custom_field") or {}).get("label"): r.get("value") for r in (cf.get("results") or [])}
            tags = [(t.get("name") or t.get("title")) if isinstance(t, dict) else t for t in rec.get("tags") or []]
            addr = rec.get("address") or {}
            status = vals.get("Foreclosure Status")
            rows.append({
                "uuid": uuid, "Property Street Address": addr.get("street") or "",
                "Property ZIP Code": str(addr.get("postal_code") or "")[:5],
                "Foreclosure Date": iso_to_us(rec.get("foreclosure_date") or rec.get("auction_date")),
                "Auction Date": iso_to_us(vals.get("Auction Date") or rec.get("auction_date")),
                "Contract Deadline": iso_to_us(vals.get("Contract Deadline")),
                "Foreclosure Status": options.get(status, status or ""),
                "Starting Bid": str(int(float(vals["Starting Bid"]))) if vals.get("Starting Bid") not in (None, "") else "",
                "Tags": ", ".join(t for t in tags if t)})
        offset += len(page)
        if len(page) < 100 or offset >= (res.get("count") or 0):
            break
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} leads read from DataSift list {title!r} -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:])
