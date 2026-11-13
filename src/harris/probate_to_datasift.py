"""Matched Harris probate cases (probate_match.py) -> the neutral leads CSV the CRM connector uploads.

    python3 probate_to_datasift.py <match.csv> <out.csv> [--market market.json] [--excluded excluded.csv]

One row per HCAD property. The CONTACT is the applicant (the would-be executor / administrator), so
the record's owner and mailing address are the applicant's; the decedent goes in Decedent Name. A
house already in the CRM (say, a foreclosure lead) keeps its owner: Keep Owner On Existing = Yes.

Left out, each counted and written to --excluded with the reason:
  - matches that are not high or medium confidence (common names: a human checks them)
  - SURVIVING SPOUSE homes (operator's call, 2026-11-01): the HCAD owner line names a co-owner who is
    not a decedent in this batch and the home is owner-occupied (site = mailing address). A spouse
    living in the house is not selling it; a rental the couple owned is still a lead
  - non-residential classes probate_match already skips

Temperature (Hot / Warm / Cold) = how likely the heirs are to sell, from what the case and HCAD say
before any CRM data exists. Points, each stated in Temperature Notes:
  +2  applicant lives outside Texas            +1  applicant in Texas but outside the Houston area
  +1  nobody the estate can rely on lives there: a rental (tax bill mailed elsewhere: the heirs inherit
      a tenant), the decedent's last address was somewhere else (care home, relative), or the sole
      owner is gone and the applicant lives elsewhere. 0 when the applicant lives in the house
  +1  the estate holds 2+ Harris properties    +1  no will (heirship / administration): several heirs
  +1  death 12+ months before the filing: the house has sat, carrying taxes and insurance
  +1  built before 1980: needs work, which is what an investor buys and a retail buyer avoids
Hot = 4+ points AND inside DataSift's Market Finder buy box (value and sq ft; --market). Warm = 2-3,
or 4+ outside the box. Cold = 0-1. Also written to Prospect Score / Prospect Reasons, the prospect score
shared with foreclosure and senior. DataSift's own temperature is never set: it is the caller's, after a
conversation (operator, 2026-11-11).
"""
import csv, json, re, sys
from collections import defaultdict
from datetime import date

from harris_status import parse_date
from harris_to_datasift import street, tc
from probate_match import tokens

LIST = "Harris - Probate"
ENTITY = re.compile(r"\b(LLC|INC|CORP|COMPANY|TRUST|BANK|N\.?A\.?|LP|LTD|ASSOCIATION|SERVICES|TRUSTEE|GUARDIAN)\b", re.I)
SUFFIX = {"JR", "SR", "II", "III", "IV"}
PARTICLES = {"DE", "DEL", "LA", "VAN", "VON", "DA", "DI", "DOS", "ST", "LE", "MAC"}
NO_WILL = re.compile(r"HEIRSHIP|INDEPENDENT ADMINISTRATION$|DEPENDENT ADMINISTRATION|ADMINISTRATION WITH AN HEIRSHIP")


def split_person(full):
    """Clerk party names read FIRST [MIDDLE] LAST [, SUFFIX]: 'John Allen Doe, Sr.' -> (John, Doe)."""
    t = [w for w in re.sub(r"[.,]", " ", full or "").split() if w.upper() not in SUFFIX]
    if len(t) < 2:
        return "", " ".join(t)
    last = [t[-1]]
    i = len(t) - 2
    while i > 0 and t[i].upper() in PARTICLES:  # "Maria De La Cruz": keep the particles on the surname
        last.insert(0, t[i])
        i -= 1
    return t[0], " ".join(last)


def spouse_home(r, decedents_in_batch):
    """True when a surviving co-owner probably lives in the house (see the module doc)."""
    owner = tokens(r["hcad_owner"])
    if "&" not in owner:
        return False
    surname, co = owner[0], owner[owner.index("&") + 1:]
    if not co:
        return False
    # The co-owner as (first name, surname): "SMITH JOHN & MARY" -> MARY SMITH; "SMITH JOHN & JONES MARY" may
    # also be MARY JONES. The co-owner died too only when a decedent in the batch carries BOTH names, as
    # whole words: matching the first name alone let three unrelated decedents who shared the co-owner's
    # first name count her as deceased, and her home was uploaded (2026-11-03)
    names = {(co[0], surname)} | ({(co[1], co[0])} if len(co) > 1 else set())
    dead = any(len(t) >= 2 and (t[0], t[-1]) in names for t in (d.split() for d in decedents_in_batch))
    if dead:
        return False  # both owners died (two cases in the batch): the house is free of a spouse
    # Only the home the spouse lives in (operator, 2026-11-01): a jointly owned rental mails its tax bill
    # elsewhere and stays a lead
    return street(r["site_addr"]).upper()[:12] == street(r["mail_addr"]).upper()[:12]


def temperature(r, n_props, box):
    pts, why = 0, []
    st, z = (r["applicant_state"] or "").upper(), (r["applicant_zip"] or "")[:5]
    if st and st != "TX":
        pts += 2; why.append(f"heir in {st} (+2)")
    elif z and not z.startswith(("770", "771", "772", "773", "774", "775")):
        pts += 1; why.append("heir outside the Houston area (+1)")
    key = lambda a: street(a).upper()[:12] if a else ""
    site = key(r["site_addr"])
    app_there = key(r["applicant_street"]) == site
    rental = r["hcad_owner"] != "CURRENT OWNER" and key(r["mail_addr"]) not in (site, "")
    if app_there:
        why.append("heir lives in the house")
    elif rental:
        pts += 1; why.append("rental: the heirs inherit a tenant (+1)")
    elif r["decedent_street"] and key(r["decedent_street"]) != site:
        pts += 1; why.append("decedent's last address was elsewhere: likely vacant (+1)")
    elif "&" not in r["hcad_owner"]:
        pts += 1; why.append("sole owner gone, heir lives elsewhere: likely vacant (+1)")
    if n_props >= 2:
        pts += 1; why.append(f"estate holds {n_props} Harris properties (+1)")
    if NO_WILL.search(r["type"].upper()) and "WILL" not in r["type"].upper():
        pts += 1; why.append("no will: several heirs must agree (+1)")
    dod, filed = parse_date(r["dod"]), parse_date(r["file_date"])
    if dod and filed and (filed - dod).days >= 365:
        pts += 1; why.append(f"died {dod:%m/%Y}, {(filed - dod).days // 30} months before the filing (+1)")
    yr = int(r["year"]) if (r["year"] or "").isdigit() and int(r["year"]) > 1800 else None
    if yr and yr < 1980:
        pts += 1; why.append(f"built {yr} (+1)")
    in_box, box_why = True, []
    if box:
        val, sqft = float(r["value"] or 0), float(r["sqft"] or 0)
        if val and not box["property_value_low"] <= val <= box["property_value_high"]:
            in_box = False; box_why.append(f"value ${val:,.0f} outside the buy box")
        if sqft and not box["living_area_sqft_low"] <= sqft <= box["living_area_sqft_high"]:
            in_box = False; box_why.append(f"{sqft:,.0f} sq ft outside the buy box")
    t = "Hot" if pts >= 4 and in_box else "Warm" if pts >= 2 else "Cold"
    return t, f"{t} ({pts} pts): " + "; ".join(why + box_why or ["no motivation signal yet"])


def main(match_path, out_path, *flags):
    box = json.load(open(flags[flags.index("--market") + 1]))["buy_box"] if "--market" in flags else None
    excl_path = flags[flags.index("--excluded") + 1] if "--excluded" in flags else None
    rows = list(csv.DictReader(open(match_path)))
    decedents = [" ".join(tokens(r["decedent"])) for r in rows]
    props_per_case = defaultdict(set)
    for r in rows:
        if r.get("acct"):
            props_per_case[r["case"]].add(r["acct"])
    excluded, by_acct = [], {}
    for r in rows:
        if not r.get("acct"):
            continue
        if r["confidence"] not in ("high", "medium"):
            excluded.append((r, f"{r['confidence']} confidence match"))
            continue
        if spouse_home(r, decedents):
            excluded.append((r, f"surviving spouse home ({r['hcad_owner']})"))
            continue
        by_acct.setdefault(r["acct"], []).append(r)

    out = []
    for acct, cases in by_acct.items():
        cases.sort(key=lambda r: parse_date(r["file_date"]) or date.max)
        r = cases[0]  # the first filing names the executor; a spouse's later case is noted
        first, last = split_person(r["applicant"]) if r["applicant"] else ("", "")
        company = bool(ENTITY.search(r["applicant"] or ""))
        temp, temp_notes = temperature(r, len(props_per_case[r["case"]]), box)
        tags = ["Harris", "Probate"] + (["Verify Owner"] if r["confidence"] == "medium" else []) \
            + (["Out of State Heir"] if r["applicant_state"] and r["applicant_state"].upper() != "TX" else []) \
            + (["Multiple Properties"] if len(props_per_case[r["case"]]) > 1 else [])
        attorney = r["attorney"]
        notes = " ".join(
            f"Harris County probate case {c['case']} filed {c['file_date']}: {c['subtype'] or c['type']}. "
            f"Decedent {c['decedent']}" + (f", died {c['dod']}" if c["dod"] else "") + ". "
            f"Applicant (executor/administrator): {c['applicant'] or 'not listed'}"
            + (f", {c['applicant_street']}, {c['applicant_city']} {c['applicant_state']} {c['applicant_zip']}" if c["applicant_street"] else "")
            + (f". Attorney: {c['attorney']}" if c["attorney"] else "") + (f" {c['attorney_phone']}" if c.get("attorney_phone") else "") + "." for c in cases) \
            + f" HCAD owner of record: {r['hcad_owner']} (account {acct}). Estimated Value = HCAD 2026 market value." \
            + f" Property match: {r['method']} ({r['confidence']})" + (f"; {r['note']}" if r["note"] else "") + "."
        out.append({
            "Property Street Address": street(r["site_addr"]), "Property City": tc(r["site_city"]), "Property State": "TX",
            "Property ZIP Code": r["site_zip"][:5],
            "Owner First Name": "" if company else first, "Owner Last Name": tc(r["applicant"]) if company else last,
            "Mailing Street Address": r["applicant_street"].rstrip(". ") or street(r["site_addr"]),
            "Mailing City": r["applicant_city"] if r["applicant_street"] else tc(r["site_city"]),
            "Mailing State": r["applicant_state"] if r["applicant_street"] else "TX",
            "Mailing ZIP Code": r["applicant_zip"] if r["applicant_street"] else r["site_zip"][:5],
            "Lists": LIST, "Tags": ", ".join(tags), "Notes": notes, "Note Key": f"probate case {r['case']}", "Keep Owner On Existing": "Yes",
            "Estimated Value": r["value"], "Parcel ID": acct, "Year Built": r["year"], "Living SqFt": r["sqft"],
            "Notice Type": "Probate", "County": "Harris", "Date Added": date.today().strftime("%m/%d/%Y"), "Owner Deceased": "Yes",
            "Decedent Name": " & ".join(dict.fromkeys(c["decedent"] for c in cases)),
            "Executor": r["applicant"], "Executor Attorney": (attorney + (f", {r['attorney_phone']}" if r.get("attorney_phone") else "")).strip(", "),
            "Probate Open Date": r["file_date"], "Date of Death": r["dod"], "Probate Case Number": r["case"],
            "Probate Type": r["subtype"] or r["type"], "Lead Temperature": temp, "Temperature Notes": temp_notes[:500],
            # the prospect score, comparable across lists; DataSift's own temperature is the caller's (never set here)
            "Prospect Score": temp, "Prospect Reasons": temp_notes[:500], "Match Confidence": r["confidence"]})
    cols = list(dict.fromkeys(k for o in out for k in o))
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out)
    if excl_path:
        with open(excl_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["reason"] + list(rows[0].keys()))
            w.writeheader()
            for r, why in excluded:
                w.writerow({"reason": why, **r})
    reasons = defaultdict(int)
    for _, why in excluded:
        reasons[why.split(" (")[0]] += 1
    temps = defaultdict(int)
    for o in out:
        temps[o["Lead Temperature"]] += 1
    print(f"{len(out)} leads -> {out_path} | " + ", ".join(f"{k} {temps[k]}" for k in ("Hot", "Warm", "Cold"))
          + " | left out: " + (", ".join(f"{k} {v}" for k, v in reasons.items()) or "none"))


if __name__ == "__main__":
    main(*sys.argv[1:])
