"""Upload Harris foreclosure leads (harris_to_datasift.py output) into DataSift over the API.

    python3 harris_upload.py <leads.csv>                       # dry run, prints the first payload
    python3 harris_upload.py <leads.csv> --limit 1 --commit    # ALWAYS one first, then read it back
    python3 harris_upload.py <leads.csv> --commit

Reuses datasift_api_upload for auth (minted JWT from DATASIFT_EMAIL / DATASIFT_PASSWORD), record
creation (duplicate address -> attach to the existing record) and the payload contract (tags as an
array, entity owners as company, notes posted separately). Adds the Harris custom fields, written
by label from the "Foreclosure" group, and the native property facts HCAD gives us. Every record is
read back after the write and any field that did not land is reported.
"""
import csv, os, re, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datasift_api_upload import Api, ApiError, build_property, create_property, field_index  # noqa: E402

from harris_status import parse_date  # noqa: E402

# CSV column -> custom-field label (created in the account's "Foreclosure" group)
FIELDS = {"Starting Bid": "Starting Bid", "Original Loan Amount": "Original Loan Amount", "Loan Date": "Loan Date",
          "Lender": "Lender", "Trustee Firm": "Trustee Firm", "Auction Date": "Auction Date",
          "Contract Deadline": "Contract Deadline", "Foreclosure Status": "Foreclosure Status"}
# CSV column -> native property field (names as the record detail returns them)
NATIVE = {"Estimated Value": "estimate_value", "Year Built": "year", "Living SqFt": "sqft", "Parcel ID": "apn",
          "Auction Date": "auction_date"}  # DataSift's own auction_date, so native filters see the sale too


def custom_values(row, idx, problems):
    out = []
    for col, label in FIELDS.items():
        raw = (row.get(col) or "").strip()
        if not raw:
            continue
        f = idx.get(label)
        if not f:
            problems.append(f"no custom field {label!r}")
            continue
        val = raw
        if f["field_type"] == "date":
            d = parse_date(raw)
            if not d:
                problems.append(f"{label}: unreadable date {raw!r}")
                continue
            val = d.isoformat()
        elif f["field_type"] in ("select", "multiselect"):
            val = f["options"].get(raw)
            if not val:  # never guess an option
                problems.append(f"{label}: no option {raw!r}")
                continue
        out.append({"field_uuid": f["uuid"], "value": val, "_label": label, "_raw": raw})
    return out


def native_values(row):
    out = {}
    for col, key in NATIVE.items():
        raw = (row.get(col) or "").strip()
        if not raw or raw == "0":
            continue
        if key == "auction_date":
            d = parse_date(raw)
            if d:
                out[key] = d.isoformat()
        else:
            out[key] = raw if key == "apn" else int(float(raw))
    return out


def read_back(api, uuid, values, native, row):
    """Compare what DataSift holds with what was sent; return a list of mismatches."""
    rec = api.call(f"/api/internal/property/{uuid}/")
    bad = []
    street = ((rec.get("address") or {}).get("street") or "").upper()
    if street.split()[:1] != row["Property Street Address"].upper().split()[:1]:
        bad.append(f"address reads back as {street!r}")
    lists = {(x.get("title") or x.get("name")) if isinstance(x, dict) else x for x in rec.get("lists") or []}
    if row.get("Lists") and row["Lists"] not in lists:
        bad.append(f"list {row['Lists']!r} missing (has {sorted(map(str, lists))})")
    tags = {(x.get("name") or x.get("title")) if isinstance(x, dict) else x for x in rec.get("tags") or []}
    want_tags = {t.strip() for t in (row.get("Tags") or "").split(",") if t.strip()}
    if want_tags - tags:
        bad.append(f"tags missing {sorted(want_tags - tags)}")
    for key, val in native.items():
        got = rec.get(key)
        try:
            same = float(got) == float(val)
        except (TypeError, ValueError):
            same = str(got) == str(val)
        if not same:
            bad.append(f"{key}: sent {val!r}, reads {got!r}")
    cf = api.call(f"/api/internal/property/{uuid}/custom-field/") if values else []
    rows = cf if isinstance(cf, list) else (cf.get("results") or cf.get("data") or [])
    got = {}
    for r in rows:
        fid = (r.get("custom_field") or {}).get("uuid")
        got[fid] = r.get("value")
    for v in values:
        g = got.get(v["field_uuid"])
        if g in (None, "", []):
            bad.append(f"{v['_label']}: sent {v['_raw']!r}, reads empty")
    return bad, rec


class PatientApi(Api):
    """DataSift throttles /api/internal/ hard (~2 req/s). Wait out a 429 or a 5xx, honouring the
    server's "available in N seconds" hint, instead of counting a throttled call as a failed lead."""

    def call(self, path, method="GET", body=None, _retry=True, headers=None):
        for attempt in range(6):
            try:
                return super().call(path, method, body, _retry, headers)
            except ApiError as e:
                if e.code != 429 and e.code < 500 or attempt == 5:
                    raise
                hint = re.search(r"(\d+) second", e.body or "")
                time.sleep(int(hint.group(1)) + 1 if hint else 2 ** attempt)


def main(path, *flags):
    commit = "--commit" in flags
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    start = int(flags[flags.index("--start") + 1]) if "--start" in flags else 0
    rows = list(csv.DictReader(open(path, encoding="utf-8")))[start:][: limit or None]
    api = PatientApi()
    idx = field_index(api)
    print(f"[{'COMMIT' if commit else 'DRY RUN'}] {len(rows)} leads | {len(idx)} custom fields in the account\n")
    ok = failed = 0
    for i, row in enumerate(rows, 1):
        problems = []
        prop = build_property(row)
        prop["address"]["state"] = row.get("Property State") or "TX"
        values = custom_values(row, idx, problems)
        native = native_values(row)
        if not commit:
            if i == 1:
                print("payload:", prop, "\nnative:", native, "\ncustom:", [(v["_label"], v["value"]) for v in values])
            continue
        try:
            res = create_property(api, prop)
            uuid = res["uuid"]
            if native:
                try:
                    api.call(f"/api/internal/property/{uuid}/", "PATCH", native)
                except ApiError as e:
                    problems.append(f"native fields rejected: {str(e)[:160]}")
            if values:
                api.call(f"/api/internal/property/{uuid}/custom-field/update-values/", "PATCH",
                         [{"field_uuid": v["field_uuid"], "value": v["value"]} for v in values])
            if row.get("Notes"):
                # add-notes APPENDS, so a re-run would stack the same note again: skip when this
                # notice's doc id is already in the record's notes
                doc = (row["Notes"].split("foreclosure notice ", 1)[1].split()[0] if "foreclosure notice " in row["Notes"] else "")
                have = api.call(f"/api/internal/property/{uuid}/").get("notes") or "" if res.get("existing") else ""
                if not doc or doc not in have:
                    api.call(f"/api/internal/property/{uuid}/add-notes/", "POST", {"notes": row["Notes"][:2000]})
            bad, _ = read_back(api, uuid, values, native, row)
            problems += bad
            ok += 1
            flag = "existing record" if res.get("existing") else "created"
            print(f"  {'OK  ' if not problems else 'WARN'} {row['Property Street Address']} ({flag}, {uuid})")
        except Exception as e:
            failed += 1
            problems.append(str(e)[:200])
            print(f"  FAIL {row['Property Street Address']}")
        for p in problems:
            print("       -", p)
        time.sleep(0.3)
    if commit:
        print(f"\nuploaded={ok} failed={failed}")
    else:
        print("\nNothing written. Re-run with --commit (start with --limit 1).")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
