"""Harris probate leads (probate_to_datasift.py output) -> DataSift. Dry run unless --commit.

    python3 probate_upload.py <leads.csv>                          # dry run: schema plan + every lead
    python3 probate_upload.py <leads.csv> --schema --commit        # create the fields + list (once)
    python3 probate_upload.py <leads.csv> --limit 1 --commit       # ALWAYS one first, read it back
    python3 probate_upload.py <leads.csv> --commit                 # the rest

The dry run writes nothing: it reads the account's custom fields, groups and lists, and looks up each
lead's address to say whether it would create a record or land on an existing one (which keeps its
owner: rows carry Keep Owner On Existing = Yes). Leads go through the CRM connector like foreclosure.
"""
import csv, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402

LIST = "Harris - Probate"
GROUP = "Probate"
FIELDS = [("Decedent Name", "text"), ("Executor", "text"), ("Executor Attorney", "text"),
          ("Probate Open Date", "date"), ("Date of Death", "date"), ("Probate Case Number", "text"),
          ("Probate Type", "text"), ("Lead Temperature", "select"), ("Temperature Notes", "text")]
OPTIONS = {"Lead Temperature": ["Hot", "Warm", "Cold"]}


def schema(crm, commit):
    """Create what is missing: the custom-field group, the fields (selects with their options), the list."""
    api = crm.api
    groups = api.call("/api/internal/custom-fields/group/?limit=999")
    groups = groups if isinstance(groups, list) else groups.get("results") or []
    gid = next((g.get("id") for g in groups if (g.get("title") or g.get("label")) == GROUP), None)
    todo = []
    if not gid:
        todo.append(f"custom-field group {GROUP!r}")
        if commit:
            gid = api.call("/api/internal/custom-fields/group/", "POST", {"title": GROUP}).get("id")
    for label, ftype in FIELDS:
        if label in crm.fields:
            continue
        todo.append(f"field {label!r} ({ftype}" + (f": {', '.join(OPTIONS[label])}" if label in OPTIONS else "") + ")")
        if commit:
            body = {"label": label, "field_type": ftype, "entity_type": "property", "group_id": gid}
            if label in OPTIONS:  # a select is refused without its options in the same POST
                body["options"] = [{"label": o, "value": o} for o in OPTIONS[label]]
            api.call("/api/internal/custom-fields/", "POST", body)
    lists = api.call("/api/internal/list/?limit=999")
    lists = lists if isinstance(lists, list) else lists.get("results") or []
    if not any((x.get("title") or "").strip().lower() == LIST.lower() for x in lists):
        todo.append(f"list {LIST!r}")
        if commit:
            api.call("/api/internal/list/", "POST", {"title": LIST})
    if commit:
        from datasift_api_upload import field_index
        crm.fields = field_index(api)
        missing = [l for l, _ in FIELDS if l not in crm.fields]
        print("schema: created " + ("; ".join(todo) or "nothing (all present)")
              + (f" | STILL MISSING {missing}" if missing else " | read back: all fields present"))
    return todo


def main(path, *flags):
    commit = "--commit" in flags
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    crm = get_crm()
    if "--schema" in flags:
        schema(crm, commit)
        return 0
    todo = schema(crm, commit=False)
    if todo and commit:
        raise SystemExit("schema missing (" + "; ".join(todo) + "): run --schema --commit first")
    rows = rows[: limit or None]
    print(f"[{'COMMIT' if commit else 'DRY RUN'}] {len(rows)} probate leads -> {crm.name}, list {LIST!r}")
    if not commit:
        print("would create: " + ("; ".join(todo) or "nothing, schema in place"))
    new = existing = failed = 0
    for i, row in enumerate(rows, 1):
        try:
            if not commit:
                uuid = crm.find_lead(row)
                existing += bool(uuid)
                new += not uuid
                res = crm.upsert_lead(row, False)
                print(f"  {'EXISTS' if uuid else 'NEW   '} [{row['Lead Temperature']:4}] {row['Property Street Address']}, "
                      f"{row['Property ZIP Code']} | executor {row['Owner First Name']} {row['Owner Last Name']} | {row['Tags']}")
                if i == 1:
                    print("    payload preview:\n      " + res["preview"].replace("\n", "\n      "))
                continue
            res = crm.upsert_lead(row, True)
            print(f"  {'OK  ' if not res['problems'] else 'WARN'} {row['Property Street Address']} "
                  f"({'created' if res['created'] else 'existing record'}, {res['id']})")
            for p in res["problems"]:
                print("       -", p)
            time.sleep(0.3)
        except Exception as e:
            failed += 1
            print(f"  FAIL {row['Property Street Address']}: {str(e)[:200]}")
    if not commit:
        print(f"\n{new} would be new records, {existing} would land on an existing record (owner kept). Nothing written.")
    else:
        print(f"\nfailed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
