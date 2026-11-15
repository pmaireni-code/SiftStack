"""Apply a Trestle results CSV (checked on the operator's machine) to DataSift. Writes ONLY with --commit AND SIFT_MODE=live.

    python3 trestle_import.py RESULTS.csv                       # read only: the plan, phone by phone
    python3 trestle_import.py RESULTS.csv --commit              # still a preview: writing needs SIFT_MODE=live
    SIFT_MODE=live python3 trestle_import.py RESULTS.csv --commit --limit 1   # ONE lead first, read back
    SIFT_MODE=live python3 trestle_import.py RESULTS.csv --commit             # the rest
    options: --date YYYY-MM-DD (the day Trestle was queried; default today)

The CSV is the one revisar_telefonos_trestle.bat writes from trestle_export.py's Excel: the export columns
(owner_uuid, property_uuid, Telefono, phone_json_actual, ...) plus trestle_* columns. A row counts as scored
only when trestle_estado is "ok" and trestle_http_status is 200; any other row is reported and ignored.

What it writes is exactly what trestle_score.py writes after querying (same rules, trestle.py and
trestle_score.lead_outcome; operator 2026-11-12):
  per number: the dial tier from the score (81-100 Dial First, 61-80 Second, 41-60 Third, 40 or less Drop;
  invalid -> Drop; litigator -> Drop; a number marked CORRECT stays Dial First) and the tag "Trestle Scored"
  (+ "Litigator"); the phone's type, status and connection flag are resent unchanged;
  per lead: the field "Trestle Phone Scores" (number=score@date, merged with what is already there, so the
  daily phone_priority keeps these tiers) and "Litigator - Do Not Contact" / "Needs Re-Skip" (every phone in Drop) when they apply.
Nothing else changes. A line type that differs from DataSift's is REPORTED, not written (no rule yet).
If the CSV has no litigator column the litigator check was not run: the plan says so and no number is marked.

Safety: every lead is read live first; a lead whose owner uuid no longer matches the CSV, a lead also on
Harris - Senior Equity, or a CSV number no longer on the lead is skipped and reported. Tiers are computed
from the LIVE phone (a status a caller set after the export counts). Every write is read back; the run stops
after the first lead if it does not read back.
"""
import csv, json, os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402
import trestle as T  # noqa: E402
import trestle_score as TS  # noqa: E402

LITIGATOR_COLUMNS = ("trestle_is_litigator_risk", "trestle_litigator", "trestle_litigator_risk",
                     "trestle_phone_is_litigator_risk", "litigator")


def _true(v):
    return str(v or "").strip().lower() in ("true", "1", "yes", "si", "sí")


def read_csv(path, day):
    """-> ({property_uuid: {"owner": uuid, "rows": {10-digit: score dict}}}, notes, litigator_checked)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"{path}: no rows")
    for col in ("property_uuid", "owner_uuid", "Telefono", "trestle_activity_score"):
        if col not in rows[0]:
            raise SystemExit(f"{path}: column {col!r} missing")
    lit_col = next((c for c in LITIGATOR_COLUMNS if c in rows[0]), None)
    leads, notes = {}, []
    for i, r in enumerate(rows, 2):
        num = T.digits(r["Telefono"])
        ok = (r.get("trestle_estado") or "ok").strip().lower() == "ok" and str(r.get("trestle_http_status") or "200").strip() == "200"
        if not ok:
            notes.append(f"row {i} ...{num[-4:]}: not scored ({r.get('trestle_estado')} {r.get('trestle_error') or ''})".strip())
            continue
        valid = r.get("trestle_is_valid")
        raw = (r.get("trestle_activity_score") or "").strip()
        score = int(float(raw)) if raw and (valid is None or _true(valid)) else None
        lead = leads.setdefault(r["property_uuid"].strip(), {"owner": r["owner_uuid"].strip(), "rows": {}, "line": {}})
        lead["rows"][num] = {"score": score, "litigator": _true(r.get(lit_col)) if lit_col else False, "date": day}
        lead["line"][num] = (r.get("trestle_line_type") or "").strip().upper()
    return leads, notes, bool(lit_col)


def plan_lead(crm, pid, csv_lead):
    """Read the lead live and work out its changes. -> dict, or {"skip": reason}."""
    lead = (TS.collect(crm, [pid], date.today()) or [None])[0]
    if not lead:
        return {"skip": "on Harris - Senior Equity"}
    if lead["owner"] != csv_lead["owner"]:
        return {"skip": "owner changed since the export (owner uuid differs)"}
    live = {T.digits(p["number"]): p for p in lead["phones"]}
    missing = [n for n in csv_lead["rows"] if n not in live]
    before = dict(lead["scores"])
    lead["scores"].update({n: s for n, s in csv_lead["rows"].items() if n in live})
    phone_tags, add, text = TS.lead_outcome(lead)
    type_diff = [(n, (live[n].get("type") or "").upper(), t) for n, t in csv_lead["line"].items()
                 if n in live and t and t != (live[n].get("type") or "").upper()]
    return {"lead": lead, "phone_tags": phone_tags, "add": add, "text": text, "missing": missing,
            "type_diff": type_diff, "field_changed": T.format_field(before) != text}


def describe(pl, csv_lead):
    l = pl["lead"]
    out = [f"{l['address']} ({len(l['phones'])} phones)"]
    for p in l["phones"]:
        n = T.digits(p["number"])
        s = l["scores"].get(n)
        new = pl["phone_tags"].get(p["number"])
        sc = "-" if not s else ("invalid" if s["score"] is None else str(s["score"])) + (" LITIGATOR" if s and s["litigator"] else "")
        change = f"{', '.join(p['tags']) or '(none)'} -> {', '.join(new)}" if new else f"{', '.join(p['tags'])} (no change)"
        src = "this file" if n in csv_lead["rows"] else ("earlier score" if s else "not scored")
        out.append(f"   {n} {(p.get('type') or '?'):8} {(p.get('status') or 'UNKNOWN'):10} score {sc:>8} [{src}] | {change}")
    for n, ds, tr in pl["type_diff"]:
        out.append(f"   NOTE {n}: DataSift says {ds}, Trestle says {tr} (not written, no rule yet)")
    for n in pl["missing"]:
        out.append(f"   NOTE {n}: in the file but no longer on the lead (skipped)")
    out.append(f"   lead: field {T.FIELD} {'updated' if pl['field_changed'] else 'unchanged'}"
               + (f"; add tags {', '.join(pl['add'])}" if pl["add"] else ""))
    return out


def main(*flags):
    args = [f for f in flags if not f.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    # Writes need BOTH --commit and SIFT_MODE=live, whatever the number of leads: the brakes alone let a
    # manual run of 10 leads or fewer write directly (that wrote 2 leads unasked on 2026-11-15).
    commit = "--commit" in flags and os.environ.get("SIFT_MODE", "").lower() == "live"
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    day = date.fromisoformat(flags[flags.index("--date") + 1]) if "--date" in flags else date.today()
    csv_leads, notes, lit_checked = read_csv(args[0], day)
    crm = get_crm()
    if T.FIELD not in crm.fields:
        raise SystemExit(f"custom field {T.FIELD!r} missing in DataSift")
    plans, skipped = [], []
    for pid, cl in csv_leads.items():
        pl = plan_lead(crm, pid, cl)
        (skipped if "skip" in pl else plans).append((pid, cl, pl))
    print(f"{args[0]}: {sum(len(c['rows']) for c in csv_leads.values())} scored numbers on {len(csv_leads)} leads, "
          f"date {day}; litigator check {'IN the file' if lit_checked else 'NOT in the file (no number marked litigator)'}")
    for n in notes:
        print("  " + n)
    for pid, _, pl in skipped:
        print(f"  SKIP lead {pid}: {pl['skip']}")
    for _, cl, pl in plans:
        print("\n".join("  " + x for x in describe(pl, cl)))
    work = [(pid, cl, pl) for pid, cl, pl in plans if pl["phone_tags"] or pl["add"] or pl["field_changed"]]
    work = work[: limit or None]
    print(f"{len(work)} leads to write, {sum(len(pl['phone_tags']) for _, _, pl in work)} phone tag changes")
    crm.plan([{"id": pid, "address": pl["lead"]["address"], "set": {T.FIELD: pl["text"]}, "add_tags": pl["add"],
               "phone_tags_before": ["(tier)"], "phone_tags_after": ["(tier)"]} for pid, _, pl in work], commit)
    if not commit:
        print("Nothing written. To write: SIFT_MODE=live python3 trestle_import.py FILE --commit --limit 1, then without --limit.")
        return 0
    ok = bad = 0
    for pid, cl, pl in work:
        l, problems = pl["lead"], []
        if pl["field_changed"] or pl["add"]:
            problems += crm.update_lead(pid, {T.FIELD: pl["text"]}, pl["add"])
        for p in l["phones"]:
            if p["number"] in pl["phone_tags"] and not crm.set_phone_tags(pid, l["owner"], p, pl["phone_tags"][p["number"]]):
                problems.append(f"phone {T.digits(p['number'])} tags did not read back")
        if T.parse_field(crm.lead_field(pid, T.FIELD)) != l["scores"]:
            problems.append("scores field did not read back")
        if problems:
            bad += 1
            print(f"  WARN {l['address']}: " + "; ".join(problems))
            if ok == 0:
                print("STOPPED after the first lead: it did not read back. Nothing else written.")
                return 1
        else:
            ok += 1
            print(f"  written {l['address']}: {len(pl['phone_tags'])} phones re-tiered, field and tags read back")
    print(f"TRESTLE IMPORT: {ok} leads written and read back, {bad} with problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
