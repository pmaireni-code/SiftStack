"""Export test leads' phones to an Excel file for checking them with Trestle on the operator's machine. READ ONLY.

    python3 trestle_export.py [criteria] [--out leads_prueba_trestle.xlsx]
    python3 trestle_export.py --inventory          # phone statuses / tags / lead statuses that exist, nothing else

Criteria (operator, 2026-11-15; every run states which ones it used):
  --leads N               how many leads (default 2), ranked: most phones in --phone-status, then most call
                          attempts, then most phones
  --list NAME             a list to search (repeatable; default Harris - Foreclosure and Harris - Probate).
                          Harris - Senior Equity is refused; a lead on it is skipped unless also a foreclosure.
  --phone-status S,S      at least one phone in one of these statuses (default NO_ANSWER; "any" = no filter)
  --all-phones S,S        EVERY phone in one of these statuses
  --min-attempts N        DataSift call attempts on the lead at least N
  --min-phones N          at least N phones
  --tag T / --no-tag T    lead must / must not carry tag T (repeatable)
  --allow-status S        a lead with this status may be picked (repeatable; by default a lead with ANY status is
                          skipped). Dead Lead, not_interested and the like are never allowed.
  --keep-bad-phones       allow leads that have a WRONG / DEAD / DNC phone (default: such leads are skipped)
  --ids UUID,UUID         exactly these property uuids, no ranking (still refused if dead / Senior / litigator)
  --address TEXT          only leads whose street contains TEXT (case-insensitive)
Always excluded: litigator leads ("Litigator - Do Not Contact").

Output: one row PER PHONE (every phone of the chosen leads), sheet "Telefonos", columns
owner_uuid, property_uuid, owner_name, property_address, property_city, property_state, property_zip,
Telefono (text), tipo_actual, estado_actual, etiquetas_actuales ("; "), phone_json_actual (the phone object
exactly as DataSift returns it: upsert-phones REPLACES the phone, so an update must resend all of it).
Saved under work/ (gitignored: names and addresses never go to the public repo).
"""
import collections, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_LISTS = ["Harris - Foreclosure", "Harris - Probate"]
SENIOR, FORECLOSURE = "Harris - Senior Equity", "Harris - Foreclosure"
NEVER_STATUS = {"dead lead", "dead", "not_interested", "not interested", "closed", "sold", "do not contact", "dnc"}
BAD_PHONE = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC", "DNC_WRONG", "DNC_CORRECT", "WRONG_NUMBER",
             "DEAD_NUMBER"}
LITIGATOR_TAG = "Litigator - Do Not Contact"
COLUMNS = ["owner_uuid", "property_uuid", "owner_name", "property_address", "property_city", "property_state",
           "property_zip", "Telefono", "tipo_actual", "estado_actual", "etiquetas_actuales", "phone_json_actual"]


def _name(x):
    return (x.get("title") or x.get("name")) if isinstance(x, dict) else x


def lead_view(d):
    """The parts of a record detail the criteria look at."""
    st = d.get("status")
    o = d.get("owner") or {}
    return {"id": d.get("uuid"), "street": (d.get("address") or {}).get("street") or "", "status": _name(st) or "",
            "attempts": d.get("predictivecall_attempts") or 0, "phones": o.get("phones") or [],
            "tags": [_name(t) for t in d.get("tags") or []], "lists": [_name(x) for x in d.get("lists") or []]}


def refusal(v, c):
    """Why this lead can never be exported ("" = it may be)."""
    if SENIOR in v["lists"] and FORECLOSURE not in v["lists"]:  # Senior that is also a foreclosure is worked
        return "on Harris - Senior Equity only"
    if (v["status"] or "").strip().lower() in NEVER_STATUS:
        return f"lead status {v['status']}"
    if LITIGATOR_TAG in v["tags"]:
        return "litigator"
    return ""


def misses(v, c):
    """Why this lead does not meet the criteria ("" = it does)."""
    why = refusal(v, c)
    if why:
        return why
    sts = [(p.get("status") or "UNKNOWN").upper() for p in v["phones"]]
    if not sts:
        return "no phones"
    if v["status"] and v["status"] not in c["allow_status"]:
        return f"lead status {v['status']}"
    if not c["keep_bad"] and any(s in BAD_PHONE for s in sts):
        return "has a wrong / dead / DNC phone"
    if c["phone_status"] and not any(s in c["phone_status"] for s in sts):
        return "no phone in " + "/".join(sorted(c["phone_status"]))
    if c["all_phones"] and not all(s in c["all_phones"] for s in sts):
        return "not every phone in " + "/".join(sorted(c["all_phones"]))
    if v["attempts"] < c["min_attempts"]:
        return f"fewer than {c['min_attempts']} call attempts"
    if len(sts) < c["min_phones"]:
        return f"fewer than {c['min_phones']} phones"
    if any(t not in v["tags"] for t in c["tags"]):
        return "missing a required tag"
    if any(t in v["tags"] for t in c["no_tags"]):
        return "carries an excluded tag"
    if c["address"] and c["address"] not in v["street"].lower():
        return "address does not match"
    return ""


def rank_key(v, c):
    sts = [(p.get("status") or "UNKNOWN").upper() for p in v["phones"]]
    hits = sum(s in c["phone_status"] for s in sts) if c["phone_status"] else 0
    return (-hits, -v["attempts"], -len(sts), v["street"])


def rows_for(d):
    o, a = d.get("owner") or {}, d.get("address") or {}
    name = " ".join(x for x in (o.get("first_name"), o.get("last_name")) if x) or o.get("company") or ""
    for p in o.get("phones") or []:
        yield [o.get("uuid") or "", d.get("uuid") or "", name, a.get("street") or "", a.get("city") or "",
               a.get("state") or "", a.get("postal_code") or "", str(p.get("number") or ""), p.get("type") or "",
               p.get("status") or "", "; ".join(str(_name(t)) for t in p.get("tags") or []),
               json.dumps(p, ensure_ascii=False, default=str)]


def write_xlsx(path, rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Telefonos"
    ws.append(COLUMNS)
    for r in rows:
        ws.append(r)
    for (c,) in ws.iter_rows(min_row=2, min_col=8, max_col=8):
        c.data_type, c.number_format = "s", "@"
    wb.save(path)


def read_only(api):
    """Refuse every call that could write: only GET, or a POST that DataSift reads as GET."""
    call = api.call

    def guarded(path, method="GET", body=None, _retry=True, headers=None):
        h = {x.lower(): y for x, y in (headers or {}).items()}
        if method.upper() != "GET" and str(h.get("x-http-method-override", "")).upper() != "GET":
            raise PermissionError(f"trestle_export is read only; refused {method} {path}")
        return call(path, method, body, _retry, headers)
    api.call = guarded


def _arg(flags, name, default=None, many=False):
    vals = [flags[i + 1] for i, f in enumerate(flags) if f == name and i + 1 < len(flags)]
    return vals if many else (vals[-1] if vals else default)


def _set(text):
    return set() if not text or text.lower() == "any" else {s.strip().upper() for s in text.split(",") if s.strip()}


def criteria(flags):
    lists = _arg(flags, "--list", many=True) or DEFAULT_LISTS
    if SENIOR in lists:
        raise SystemExit("Harris - Senior Equity is out until the operator says otherwise")
    return {"leads": int(_arg(flags, "--leads", 2)), "lists": lists,
            "phone_status": _set(_arg(flags, "--phone-status", "NO_ANSWER")), "all_phones": _set(_arg(flags, "--all-phones")),
            "min_attempts": int(_arg(flags, "--min-attempts", 0)), "min_phones": int(_arg(flags, "--min-phones", 1)),
            "tags": _arg(flags, "--tag", many=True), "no_tags": _arg(flags, "--no-tag", many=True),
            "allow_status": [s for s in _arg(flags, "--allow-status", many=True)
                             if s.strip().lower() not in NEVER_STATUS],
            "keep_bad": "--keep-bad-phones" in flags, "address": (_arg(flags, "--address") or "").lower(),
            "ids": [x.strip() for x in (_arg(flags, "--ids") or "").split(",") if x.strip()]}


def main(*flags):
    c = criteria(flags)
    crm = get_crm()
    read_only(crm.api)
    if c["ids"]:
        details = [crm.api.call(f"/api/internal/property/{i}/") for i in c["ids"]]
        chosen = []
        for d in details:
            why = refusal(lead_view(d), c)
            print(f"  {'REFUSED' if why else 'ok':7} {lead_view(d)['street']}" + (f": {why}" if why else ""))
            if not why:
                chosen.append(d)
    else:
        details, seen = [], set()
        for name in c["lists"]:
            for r in crm.list_assignments(name, ()):
                if r["id"] not in seen:
                    seen.add(r["id"])
                    details.append(crm.api.call(f"/api/internal/property/{r['id']}/"))
        views = [lead_view(d) for d in details]
        if "--inventory" in flags:
            return inventory(views)
        out = collections.Counter(misses(v, c) or "MATCH" for v in views)
        ok = sorted((v for v in views if not misses(v, c)), key=lambda v: rank_key(v, c))
        by_id = {d.get("uuid"): d for d in details}
        chosen = [by_id[v["id"]] for v in ok[: c["leads"]]]
        print(f"{len(views)} leads read on {', '.join(c['lists'])}; {out['MATCH']} meet the criteria")
        for why, n in out.most_common():
            if why != "MATCH":
                print(f"  skipped {n:>4}: {why}")
    rows = [r for d in chosen for r in rows_for(d)]
    if not rows:
        print("No lead to export: nothing written.")
        return 1
    path = os.path.join(ROOT, "work", _arg(flags, "--out", "leads_prueba_trestle.xlsx"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_xlsx(path, rows)
    print("criteria: " + json.dumps({k: (sorted(v) if isinstance(v, set) else v) for k, v in c.items()}))
    for d in chosen:
        v, o = lead_view(d), d.get("owner") or {}
        sts = collections.Counter((p.get("status") or "UNKNOWN").upper() for p in v["phones"])
        print(f"  {v['street']} | {' '.join(x for x in (o.get('first_name'), o.get('last_name')) if x) or o.get('company')}"
              f" | {len(v['phones'])} phones ({', '.join(f'{k} {n}' for k, n in sts.items())}) | attempts {v['attempts']}"
              f" | owner_uuid {'ok' if o.get('uuid') else 'MISSING'}")
    print(f"EXPORT: {len(chosen)} leads, {len(rows)} phones -> {path}")
    return 0


def inventory(views):
    st, tg, ty, ls = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    for v in views:
        ls[v["status"] or "(none)"] += 1
        for p in v["phones"]:
            st[(p.get("status") or "UNKNOWN").upper()] += 1
            ty[p.get("type") or "UNKNOWN"] += 1
            for t in p.get("tags") or []:
                tg[_name(t)] += 1
    for title, cnt in (("lead status", ls), ("phone status", st), ("phone type", ty), ("phone tags", tg)):
        print(f"{title}: " + ", ".join(f"{k} {n}" for k, n in cnt.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
