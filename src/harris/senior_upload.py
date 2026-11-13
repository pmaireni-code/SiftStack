"""Harris senior / free-and-clear leads (senior_to_datasift.py output) -> DataSift. Dry run unless --commit.

    python3 senior_upload.py <leads.csv>                       # dry run: schema plan + every lead (read-only)
    python3 senior_upload.py <leads.csv> --schema --commit     # create the field groups, fields and list (once)
    python3 senior_upload.py <leads.csv> --limit 1 --commit    # ALWAYS one first, read it back
    python3 senior_upload.py <leads.csv> --commit [--start N]  # the rest (--start resumes a cut-off run)
    python3 senior_upload.py <leads.csv> --presets --commit    # the two filter presets, verified by count

Same connector and upload loop as probate (probate_upload.upload): a house already in DataSift keeps its
owner and gets the list, tags, fields and note; the note is skipped when its key is already there.
"""
import csv, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402
from probate_upload import PROSPECT_GROUP, PROSPECT_OPTIONS, ensure_schema, upload  # noqa: E402

LIST = "Harris - Senior Equity"
GROUPS = {
    "Senior Equity": [("Senior Tier", "select"), ("Owner Age", "text"), ("Senior Exemption", "text"),
                      ("Equity Percent", "number"), ("Estimated Debt", "money")],
    "Market Check": [("Market Check", "text"), ("ZIP Median Sale Price", "money"), ("ZIP Median Home Value", "money"),
                     ("ZIP Days on Market", "number"), ("ZIP Investor Transactions", "number"),
                     ("County Median Home Value", "money"), ("County Days on Market", "number"),
                     ("Price Band 60", "text"), ("Investor Volume Band", "text")],
    "Probate": [("Lead Temperature", "select"), ("Temperature Notes", "text")],  # shared with probate
}
OPTIONS = {"Senior Tier": ["Priority", "Standard"], "Lead Temperature": ["Hot", "Warm", "Cold"]}
FOLDER = "Harris Senior Equity"
PRESETS = [("Harris - Senior Equity (4 criterios)", None), ("Harris - Senior Equity Priority", "Priority")]


def schema(crm, commit):
    return ensure_schema(crm, commit, LIST, {**GROUPS, **PROSPECT_GROUP}, {**OPTIONS, **PROSPECT_OPTIONS})


def presets(crm, commit):
    """Folder + two presets: the whole list, and the list with Senior Tier = Priority. Read back and counted:
    a custom-field filter in the wrong shape is accepted and ignored (README, DataSift filter preset)."""
    api = crm.api
    L = lambda r: r if isinstance(r, list) else r.get("results") or []
    lst = next((x["uuid"] for x in L(api.call("/api/internal/list/?limit=999")) if x["title"] == LIST), None)
    tier = crm.fields.get("Senior Tier")
    if not lst or not tier:
        raise SystemExit("list or Senior Tier field missing: run --schema --commit and the upload first")

    def count(must):
        n = 0
        for pt in ("clean", "incomplete"):
            off = 0
            while True:
                r = api.call("/api/internal/property/", "POST", {"limit": 100, "offset": off, "query": {"must": {**must, "property_type": pt}}},
                             headers={"x-http-method-override": "GET"})
                page = r.get("results") or r.get("data") or []
                n += len(page)
                off += len(page)
                if len(page) < 100:
                    break
        return n

    folder = next((f for f in L(api.call("/api/internal/filter-preset-folder/?limit=999")) if f.get("title") == FOLDER), None)
    if not folder:
        print(f"create folder {FOLDER!r}")
        if not commit:
            return
        folder = api.call("/api/internal/filter-preset-folder/", "POST", {"title": FOLDER, "type": "properties", "permissions": []})
    have = {p["title"]: p for p in L(api.call(f"/api/internal/filter-preset-folder/{folder['uuid']}/filter-preset/?limit=999"))}
    for title, option in PRESETS:
        must = {"any_lists": [lst]}
        if option:
            must["custom_fields"] = {tier["uuid"]: [tier["options"][option]]}
        if title in have:
            uuid = have[title]["uuid"]
        elif commit:
            uuid = api.call("/api/internal/filter-preset/", "POST", {"title": title, "folder": folder["uuid"], "quick_filter": False,
                                                                     "type": "properties", "filters": {"must": must}})["uuid"]
        else:
            print(f"create preset {title!r}: {must}")
            continue
        back = (api.call(f"/api/internal/filter-preset/{uuid}/").get("filters") or {}).get("must")
        same = json.dumps(back, sort_keys=True) == json.dumps(must, sort_keys=True)
        print(f"{title}: saved as sent {same} | {count(back):,} records")


def main(path, *flags):
    commit = "--commit" in flags
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    start = int(flags[flags.index("--start") + 1]) if "--start" in flags else 0  # resume a cut-off run
    crm = get_crm()
    if "--schema" in flags:
        schema(crm, commit)
        return 0
    if "--presets" in flags:
        presets(crm, commit)
        return 0
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    return upload(rows[start:][: limit or None], crm, commit, LIST, "senior equity", schema)


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
