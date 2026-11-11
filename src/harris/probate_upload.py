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


def ensure_schema(crm, commit, list_name, groups, options):
    """Create what is missing: each custom-field group, its fields (selects with their options), the list.
    groups = {group label: [(field label, field type), ...]}. Fields that already exist (in any group) are reused."""
    api = crm.api
    have = api.call("/api/internal/custom-fields/group/?limit=999")
    have = have if isinstance(have, list) else have.get("results") or []
    todo = []
    for group, fields in groups.items():
        missing = [(l, t) for l, t in fields if l not in crm.fields]
        if not missing:
            continue
        gid = next((g.get("id") for g in have if (g.get("title") or g.get("label")) == group), None)
        if not gid:
            todo.append(f"custom-field group {group!r}")
            if commit:
                # the group route wants label + entity_type ({"title": ...} 400s: both fields required)
                g = api.call("/api/internal/custom-fields/group/", "POST", {"label": group, "entity_type": "property"})
                gid = g.get("id") or g.get("uuid")
                if not gid:
                    raise SystemExit(f"group {group!r} created but no id in the answer: {g}")
        for label, ftype in missing:
            todo.append(f"field {label!r} ({ftype}" + (f": {', '.join(options[label])}" if label in options else "") + ")")
            if commit:
                body = {"label": label, "field_type": ftype, "entity_type": "property", "group_id": gid}
                if label in options:  # a select is refused without its options in the same POST
                    body["options"] = [{"label": o, "value": o} for o in options[label]]
                api.call("/api/internal/custom-fields/", "POST", body)
    lists = api.call("/api/internal/list/?limit=999")
    lists = lists if isinstance(lists, list) else lists.get("results") or []
    if not any((x.get("title") or "").strip().lower() == list_name.lower() for x in lists):
        todo.append(f"list {list_name!r}")
        if commit:
            api.call("/api/internal/list/", "POST", {"title": list_name})
    if commit:
        from datasift_api_upload import field_index
        crm.fields = field_index(api)
        missing = [l for fs in groups.values() for l, _ in fs if l not in crm.fields]
        print("schema: created " + ("; ".join(todo) or "nothing (all present)")
              + (f" | STILL MISSING {missing}" if missing else " | read back: all fields present"))
    return todo


def schema(crm, commit):
    return ensure_schema(crm, commit, LIST, {GROUP: FIELDS}, OPTIONS)


def upload(rows, crm, commit, list_name, label, schema_fn):
    """Dry run (read-only lookups + payload preview) or commit, through the CRM connector."""
    todo = schema_fn(crm, False)
    if todo and commit:
        raise SystemExit("schema missing (" + "; ".join(todo) + "): run --schema --commit first")
    print(f"[{'COMMIT' if commit else 'DRY RUN'}] {len(rows)} {label} leads -> {crm.name}, list {list_name!r}")
    if not commit:
        print("would create: " + ("; ".join(todo) or "nothing, schema in place"))
    crm.plan_rows(rows, commit)  # safety brakes (crm/guard.py)
    new = existing = failed = 0
    for i, row in enumerate(rows, 1):
        try:
            if not commit:
                uuid = crm.find_lead(row)
                existing += bool(uuid)
                new += not uuid
                res = crm.upsert_lead(row, False)
                print(f"  {'EXISTS' if uuid else 'NEW   '} [{row.get('Lead Temperature', ''):4}] {row['Property Street Address']}, "
                      f"{row['Property ZIP Code']} | contact {row['Owner First Name']} {row['Owner Last Name']} | {row['Tags']}")
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


def main(path, *flags):
    commit = "--commit" in flags
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    crm = get_crm()
    if "--schema" in flags:
        schema(crm, commit)
        return 0
    return upload(rows[: limit or None], crm, commit, LIST, "probate", schema)


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
