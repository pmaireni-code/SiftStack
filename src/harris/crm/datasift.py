"""DataSift (REISift) connector. Verified live 2026-10-31 on 350 Harris leads.

Auth: JWT minted from DATASIFT_EMAIL / DATASIFT_PASSWORD (datasift_api_upload.Api). Contract notes:
- create_property attaches to the existing record on a duplicate address, so upserts never duplicate
- custom field values read back from GET /property/{uuid}/custom-field/, keyed by custom_field.uuid;
  dates are written YYYY-MM-DD, selects as the option uuid
- estimate_value / year / sqft / apn / auction_date are native fields, written with a PATCH
- add-notes APPENDS and notes cannot be edited over the API: a note whose notice doc id is already on
  the record is skipped
- /api/internal/ throttles at ~2 req/s: PatientApi waits out 429s and 5xx
"""
import os, re, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from datasift_api_upload import Api, ApiError, attach_to_record, build_property, create_property, field_index  # noqa: E402

from harris_status import parse_date  # noqa: E402

from .base import CRM  # noqa: E402

# Lead column -> DataSift custom-field label (the account's "Foreclosure" group)
FIELDS = {"Starting Bid": "Starting Bid", "Original Loan Amount": "Original Loan Amount", "Loan Date": "Loan Date",
          "Lender": "Lender", "Trustee Firm": "Trustee Firm", "Auction Date": "Auction Date",
          "Contract Deadline": "Contract Deadline", "Foreclosure Status": "Foreclosure Status",
          "Deal Grade": "Deal Grade", "Deal Notes": "Deal Notes", "ZIP Investor Activity": "ZIP Investor Activity",
          # Probate (probate_to_datasift.py); a foreclosure row carries none of these columns, so nothing changes for it
          "Decedent Name": "Decedent Name", "Executor": "Executor", "Executor Attorney": "Executor Attorney",
          "Probate Open Date": "Probate Open Date", "Date of Death": "Date of Death",
          "Probate Case Number": "Probate Case Number", "Probate Type": "Probate Type",
          "Lead Temperature": "Lead Temperature", "Temperature Notes": "Temperature Notes",
          # Senior equity list (senior_to_datasift.py) and its Market Check
          **{k: k for k in ("Senior Tier", "Owner Age", "Senior Exemption", "Equity Percent", "Estimated Debt",
                            "Market Check", "ZIP Median Sale Price", "ZIP Median Home Value", "ZIP Days on Market",
                            "ZIP Investor Transactions", "County Median Home Value", "County Days on Market",
                            "Price Band 60", "Investor Volume Band")}}
# Lead column -> native property field (names as the record detail returns them)
NATIVE = {"Estimated Value": "estimate_value", "Year Built": "year", "Living SqFt": "sqft", "Parcel ID": "apn",
          "Auction Date": "auction_date"}  # DataSift's own auction_date, so native filters see the sale too


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


def _iso_to_us(v):
    v = str(v or "")[:10]
    return f"{v[5:7]}/{v[8:10]}/{v[:4]}" if len(v) == 10 and v[4] == "-" else v


class DataSiftCRM(CRM):
    name = "datasift"

    def __init__(self):
        self.api = PatientApi()
        self.fields = field_index(self.api)

    # ---- writing
    def _custom_values(self, row, problems, only=None):
        out = []
        for col, label in FIELDS.items():
            if only and col not in only:
                continue
            raw = (row.get(col) or "").strip()
            f = self.fields.get(label)
            if not f:
                if raw:
                    problems.append(f"no custom field {label!r}")
                continue
            if not raw:
                if only:  # an explicit update with an empty value clears the field: "" clears, None 400s
                    out.append({"field_uuid": f["uuid"], "value": "", "_label": label, "_raw": ""})
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

    @staticmethod
    def _native_values(row):
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

    def _read_back(self, uuid, values, native, row):
        """Compare what DataSift holds with what was sent; return a list of mismatches."""
        rec = self.api.call(f"/api/internal/property/{uuid}/")
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
            if key == "apn":  # DataSift stores the APN with dashes ("130-618-001-0009")
                same = "".join(c for c in str(got) if c.isdigit()) == str(val)
            if key == "estimate_value" and not same and got not in (None, ""):
                continue  # DataSift's own enrichment can replace HCAD's value; HCAD's stays in the notes
            if not same:
                bad.append(f"{key}: sent {val!r}, reads {got!r}")
        cf = self.api.call(f"/api/internal/property/{uuid}/custom-field/") if values else {}
        got = {(r.get("custom_field") or {}).get("uuid"): r.get("value") for r in (cf.get("results") or [])}
        for v in values:
            if got.get(v["field_uuid"]) in (None, "", []):
                bad.append(f"{v['_label']}: sent {v['_raw']!r}, reads empty")
        return bad

    def find_lead(self, row):
        """uuid of the record already at this property address, or "". READ-ONLY. Searches clean AND
        incomplete records (datasift_api_upload.find_property only sees clean ones)."""
        street = (row.get("Property Street Address") or "").strip()
        norm = lambda v: re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
        for ptype in ("clean", "incomplete"):
            res = self.api.call("/api/internal/property/", "POST", {"limit": 10, "offset": 0, "query": {"must": {
                "property_type": ptype, "search": "address_prefix:" + street}}}, headers={"x-http-method-override": "GET"})
            for rec in res.get("results") or res.get("data") or []:
                a = rec.get("address") or {}
                z = str(a.get("zip5") or a.get("postal_code") or "")[:5]
                if norm(a.get("street")) == norm(street) and (not z or z == (row.get("Property ZIP Code") or "")[:5]):
                    return rec["uuid"]
        return ""

    def upsert_lead(self, row, commit):
        problems = []
        prop = build_property(row)
        prop["address"]["state"] = row.get("Property State") or "TX"
        values = self._custom_values(row, problems)
        native = self._native_values(row)
        if not commit:
            return {"id": "", "created": False, "problems": problems,
                    "preview": f"payload: {prop}\nnative: {native}\ncustom: {[(v['_label'], v['value']) for v in values]}"}
        # "Keep Owner On Existing": a record that already exists (a foreclosure lead on the same house) keeps
        # its owner; only lists, tags, fields and the note are added. create_property would PATCH the owner
        existing = self.find_lead(row) if row.get("Keep Owner On Existing") == "Yes" else ""
        if existing:
            attach_to_record(self.api, existing, {k: v for k, v in prop.items() if k != "owner"})
            res = {"uuid": existing, "existing": True}
        else:
            res = create_property(self.api, prop)
        uuid = res["uuid"]
        if native:
            try:
                self.api.call(f"/api/internal/property/{uuid}/", "PATCH", native)
            except ApiError as e:
                problems.append(f"native fields rejected: {str(e)[:160]}")
        if values:
            self.api.call(f"/api/internal/property/{uuid}/custom-field/update-values/", "PATCH",
                          [{"field_uuid": v["field_uuid"], "value": v["value"]} for v in values])
        if row.get("Notes"):
            # add-notes APPENDS, so a re-run would stack the same note again: skip when this
            # notice's doc id is already in the record's notes
            doc = row.get("Note Key") or (row["Notes"].split("foreclosure notice ", 1)[1].split()[0] if "foreclosure notice " in row["Notes"] else "")
            have = (self.api.call(f"/api/internal/property/{uuid}/").get("notes") or "") if res.get("existing") else ""
            if not doc or doc not in have:
                self.api.call(f"/api/internal/property/{uuid}/add-notes/", "POST", {"notes": row["Notes"][:2000]})
        # DataSift's own temperature marker (snowflake / flame): set from the lead only while the record is
        # still cold (0), so an operator's call on an existing record is never overwritten
        temp = row.get("Property Temperature")
        if temp not in (None, ""):
            rec = self.api.call(f"/api/internal/property/{uuid}/")
            if not rec.get("hotness"):
                self.api.call(f"/api/internal/property/{uuid}/hotness/", "POST", {"hotness": int(float(temp))})
                got = self.api.call(f"/api/internal/property/{uuid}/").get("hotness")
                if int(got or 0) != int(float(temp)):
                    problems.append(f"temperature: sent {temp}, reads {got}")
        problems += self._read_back(uuid, values, native, row)
        return {"id": uuid, "created": not res.get("existing"), "problems": problems}

    # ---- reading
    def read_leads(self, list_name, with_facts=False):
        lists = self.api.call("/api/internal/list/?limit=999")
        lists = lists if isinstance(lists, list) else lists.get("results") or []
        lst = next((x for x in lists if (x.get("title") or "").strip().lower() == list_name.lower()), None)
        if not lst:
            raise SystemExit(f"no list named {list_name!r} in DataSift")
        options = {u: label for f in self.fields.values() for label, u in (f.get("options") or {}).items()}
        out = []
        # DataSift files a record it considers short of data (a company owner, a missing mailing line)
        # under "incomplete" instead of "clean"; a search on one type silently skips the other
        for ptype in ("clean", "incomplete"):
            out += self._read_list(lst["uuid"], ptype, options, with_facts)
        return out

    def _read_list(self, list_uuid, ptype, options, with_facts):
        out, offset = [], 0
        while True:
            res = self.api.call("/api/internal/property/", "POST", {"limit": 100, "offset": offset, "query": {"must": {
                "property_type": ptype, "any_lists": [list_uuid]}}}, headers={"x-http-method-override": "GET"})
            page = res.get("results") or res.get("data") or []
            for rec in page:
                cf = self.api.call(f"/api/internal/property/{rec['uuid']}/custom-field/")
                vals = {(r.get("custom_field") or {}).get("label"): r.get("value") for r in (cf.get("results") or [])}
                tags = [(t.get("name") or t.get("title")) if isinstance(t, dict) else t for t in rec.get("tags") or []]
                addr = rec.get("address") or {}
                status = vals.get("Foreclosure Status")
                bid = vals.get("Starting Bid")
                out.append({
                    "id": rec["uuid"], "Property Street Address": addr.get("street") or "",
                    "Property ZIP Code": str(addr.get("postal_code") or "")[:5],
                    "Foreclosure Date": _iso_to_us(rec.get("foreclosure_date") or rec.get("auction_date")),
                    "Auction Date": _iso_to_us(vals.get("Auction Date") or rec.get("auction_date")),
                    "Contract Deadline": _iso_to_us(vals.get("Contract Deadline")),
                    "Foreclosure Status": options.get(status, status or ""),
                    "Starting Bid": str(int(float(bid))) if bid not in (None, "") else "",
                    "Tags": ", ".join(t for t in tags if t),
                    "Deal Grade": options.get(vals.get("Deal Grade"), vals.get("Deal Grade") or ""),
                    "Deal Notes": vals.get("Deal Notes") or "",
                    "ZIP Investor Activity": options.get(vals.get("ZIP Investor Activity"), vals.get("ZIP Investor Activity") or "")})
                if with_facts:
                    # Search rows carry no property facts: the record detail does
                    d = self.api.call(f"/api/internal/property/{rec['uuid']}/")
                    num = lambda v: "" if v in (None, "") else str(v)
                    out[-1].update({
                        "Equity Percent": num(d.get("equity_percent")), "Estimated Value": num(d.get("estimate_value")),
                        "Structure Type": d.get("structure_type") or "", "Living SqFt": num(d.get("sqft")),
                        "Bedrooms": num(d.get("bedrooms")), "Bathrooms": num(d.get("bathrooms")), "Year Built": num(d.get("year")),
                        "Property Temperature": num(d.get("hotness")),
                        "Latitude": num((d.get("address") or {}).get("latitude")),
                        "Longitude": num((d.get("address") or {}).get("longitude")),
                        "Original Loan Amount": num(vals.get("Original Loan Amount")),
                        "Loan Date": _iso_to_us(vals.get("Loan Date"))})
            offset += len(page)
            # Page until a short page: the response's count lags the real membership
            if len(page) < 100:
                return out

    # ---- daily write-back
    def update_lead(self, lead_id, values, add_tags):
        problems = []
        cvals = self._custom_values(values, problems, only=tuple(values))
        self.api.call(f"/api/internal/property/{lead_id}/custom-field/update-values/", "PATCH",
                      [{"field_uuid": v["field_uuid"], "value": v["value"]} for v in cvals])
        # Keep DataSift's own auction_date in step too, so a postponement moves native filters as well
        if parse_date(values.get("Auction Date")):
            self.api.call(f"/api/internal/property/{lead_id}/", "PATCH",
                          {"auction_date": parse_date(values["Auction Date"]).isoformat()})
        # DataSift's own "Property Temperature" (the snowflake/flame): a PATCH of hotness is accepted
        # and silently ignored; the app saves it through its own route. 0 = cold (default), 3 warm, 5 hot
        if values.get("Property Temperature") not in (None, ""):
            self.api.call(f"/api/internal/property/{lead_id}/hotness/", "POST",
                          {"hotness": int(float(values["Property Temperature"]))})
        if add_tags:
            attach_to_record(self.api, lead_id, {"tags": list(add_tags)})
        return problems
