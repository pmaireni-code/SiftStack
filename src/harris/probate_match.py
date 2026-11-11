"""Probate cases (probate_cases.js) -> the decedent's Harris County property on the HCAD roll.

    python3 probate_match.py <cases.json> <hcad_idx.tsv> <out.csv>

HCAD writes owners LAST FIRST MIDDLE ("DOE JANE ANN", "SMITH JOHN & MARY"), only in the mail-to
line, and keeps an owner on the roll for years after death. Three ways in, strongest first:

  address   the clerk lists the Deceased party WITH an address on many cases: the HCAD site address
            at that number + street, confirmed when the HCAD owner carries the decedent's surname
  name      HCAD owner whose surname is the decedent's and whose given names include the decedent's
            first name (a middle initial, when both sides have one, must agree). Residential only
  tie-break several name hits: keep the one whose site address is the Applicant's or the Deceased's
            address, or whose mailing address is the Applicant's

A name hit on a common name is the false positive to fear: "JAMES L WHITLEY" can own nothing while
another James Whitley owns three houses. So every row reports how many distinct HCAD owners carry
that surname + first name (`namesakes`) and the confidence says what the match rests on:
  high      address + surname, or a single name hit on a rare name with a matching middle initial
  medium    single name hit with no middle initial to confirm, or a tie-break on an address
  low       several namesakes, nothing to break the tie (not uploaded: needs a human)
"""
import csv, json, re, sys
from collections import defaultdict

SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "DECEASED", "DEC", "DECD", "ESTATE", "OF", "THE", "MRS", "MR", "DR"}
# HCAD state class: A* single family / mobile / condo / townhome, B* multifamily, C1 vacant residential lot,
# E* rural with improvements. Commercial (F*), industrial and exempt are not an heir's house.
RESIDENTIAL = re.compile(r"^(A|B|E1|E2|C1)")
STREET_SUFFIX = {"LANE": "LN", "DRIVE": "DR", "STREET": "ST", "ROAD": "RD", "COURT": "CT", "CIRCLE": "CIR",
                 "AVENUE": "AVE", "BOULEVARD": "BLVD", "TRAIL": "TRL", "PLACE": "PL", "PARKWAY": "PKWY"}
# A middle initial breaks a tie between namesakes only on a name this rare: measured on one week (128 cases),
# a common first + last name had 21 namesakes on the roll, and the one "M" with a house need not be the decedent
MAX_NAMESAKES = 5
IDX = ["acct", "mailto", "mail_addr_1", "mail_city", "mail_state", "mail_zip", "site_addr_1", "site_addr_2",
       "site_addr_3", "state_class", "tot_mkt_val", "bld_ar", "yr_impr", "lgl_1", "lgl_2", "lgl_3", "lgl_4"]


def tokens(name):
    """Upper-case name words, suffixes dropped; "&" is kept because it separates co-owners."""
    t = re.sub(r"[^A-Z& ]", " ", name.upper().replace("'", "").replace("&", " & ")).split()
    return [w for w in t if w not in SUFFIXES]


def split_name(full):
    """'Jane Ann Doe' -> ('JANE', ['ANN'], 'DOE'). Clerk party names are FIRST [MIDDLE] LAST."""
    t = tokens(full)
    if len(t) < 2:
        return None
    return t[0], t[1:-1], t[-1]


def addr_key(street_line, zip_code=""):
    """('123 Sample Lane', '77000') -> ('123', 'SAMPLE', '77000'). Number + first street word +
    ZIP: without the ZIP, 100 OAK HILL DR and 100 OAK RIDGE CIR are one key."""
    t = re.sub(r"[^A-Z0-9 ]", " ", (street_line or "").upper()).split()
    if len(t) < 2 or not t[0].isdigit():
        return None
    words = [w for w in t[1:] if w not in {"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST", "OLD"}] or t[1:]
    return t[0], words[0], str(zip_code or "")[:5]


def street_words(line):
    t = re.sub(r"[^A-Z0-9 ]", " ", (line or "").upper()).split()
    return [STREET_SUFFIX.get(w, w) for w in t[1:] if w not in {"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST"}]


def same_street(a, b):
    """The address key compares one street word; also require the second when both sides have one
    ('21610 PARK GREEN DR' is not '21610 PARK BISHOP')."""
    wa, wb = street_words(a), street_words(b)
    return wa[:1] == wb[:1] and (len(wa) < 2 or len(wb) < 2 or wa[1] == wb[1])


def party(case, role):
    p = next((x for x in case.get("parties") or [] if x["role"].lower().startswith(role.lower())), None)
    if not p or not p["party"]:
        return None
    lines = p["party"]
    street = next((l for l in lines[1:] if re.match(r"^\d+\s+\S", l)), "")
    city = next((l for l in lines[1:] if re.search(r"\b[A-Z]{2}\s+\d{5}", l)), "")
    m = re.search(r"^(.*?)\s+([A-Z]{2})\s+(\d{5})", city)
    return {"name": lines[0], "street": street, "city": m.group(1) if m else "", "state": m.group(2) if m else "",
            "zip": m.group(3) if m else "", "attorney": (p.get("attorney") or [""])[0],
            "attorney_phone": (re.search(r"Phone\s*([\d-]{10,})", " ".join(p.get("attorney") or [])) or [None, ""])[1]}


def date_of_death(case):
    for e in case.get("events") or []:
        m = re.search(r"DOD:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", e.get("comment") or "", re.I)
        if m:
            mo, d, y = m.group(1).split("/")
            y = "20" + y if len(y) == 2 else y
            return f"{int(mo):02d}/{int(d):02d}/{y}"
    return ""


def load_hcad(path):
    by_surname, by_site, by_trust = defaultdict(list), defaultdict(list), defaultdict(list)
    with open(path, encoding="latin-1") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < len(IDX):
                continue
            r = dict(zip(IDX, c))
            owner = tokens(r["mailto"])
            if owner:
                by_surname[owner[0]].append(r)
            if "TRUST" in owner:  # "JOHN A SMITH JR TRUST": FIRST ... LAST order, index every word
                for w in set(owner) - {"TRUST", "REVOCABLE", "LIVING", "FAMILY", "&"}:
                    by_trust[w].append(r)
            k = addr_key(r["site_addr_1"], r["site_addr_3"])
            if k:
                by_site[k].append(r)
    return by_surname, by_site, by_trust


def name_hit(r, first, middles):
    """HCAD owner 'SMITH JOHN A & MARY B': given names after the surname, either co-owner."""
    given = tokens(r["mailto"])[1:]
    if first not in given:
        return None
    i = given.index(first)
    mid = given[i + 1] if i + 1 < len(given) and given[i + 1] != "&" else ""
    if middles and mid and mid[0] != middles[0][0]:
        return None  # JOHN A vs JOHN B: a different person
    return "middle" if (middles and mid) else "first"


def trust_hit(r, first, last):
    """'SHARRON E RAIBON REVOCABLE TRUST': the decedent's first name before the surname."""
    t = tokens(r["mailto"])
    return first in t and last in t and t.index(first) < t.index(last)


def mail_key(r):
    return addr_key(r["mail_addr_1"], r["mail_zip"]) or ("?", r["mail_addr_1"].upper(), r["mail_zip"][:5])


def match(case, idx):
    """Return (summary, [HCAD rows]) with every residential property the decedent owned.

    A PERSON on the roll is a name plus a mailing address: one owner with three rentals mails all three
    tax bills to one place, while three namesakes mail from three. So name hits are grouped by mailing
    address, and the group is chosen by an anchor (the decedent's or the applicant's address) or by
    being the only group."""
    by_surname, by_site, by_trust = idx
    dec, app = party(case, "Deceased"), party(case, "Applicant")
    name = split_name(dec["name"] if dec else re.sub(r"^.*?ESTATE OF:?\s*|,?\s*DECEASED.*$", "", case["style"], flags=re.I))
    out = {"case": case["case"], "decedent": dec["name"] if dec else case["style"], "method": "", "confidence": "none",
           "namesakes": 0, "properties": 0, "note": ""}
    if not name:
        out["note"] = "no decedent name"
        return out, []
    first, middles, last = name
    named = [(r, name_hit(r, first, middles)) for r in by_surname.get(last, [])]
    named = [(r, h) for r, h in named if h]
    named += [(r, "trust") for r in by_trust.get(last, []) if trust_hit(r, first, last)]
    groups = defaultdict(list)
    for r, h in named:
        groups[mail_key(r)].append((r, h))
    out["namesakes"] = len(groups)  # distinct people on the roll with this name, any property class
    res = {k: [(r, h) for r, h in v if RESIDENTIAL.match(r["state_class"] or "")] for k, v in groups.items()}
    res = {k: v for k, v in res.items() if v}
    anchors = {}
    for role, p in (("decedent", dec), ("applicant", app)):
        k = addr_key(p["street"], p["zip"]) if p else None
        if k:
            anchors.setdefault(k, role)
    notes = []

    # The decedent's own address: is the owner there the decedent?
    k = addr_key(dec["street"], dec["zip"]) if dec else None
    hidden_home = None
    if k and k in by_site:
        for r in by_site[k]:
            if not same_street(r["site_addr_1"], dec["street"]):
                continue
            t = tokens(r["mailto"])
            if t[:1] == [last] and name_hit(r, first, middles):
                pass  # found again below through the name groups
            elif t == ["CURRENT", "OWNER"]:
                hidden_home = r  # HCAD withholds the owner name on protected accounts
            elif last in t:
                notes.append(f"decedent's address is owned by {r['mailto']!r} (family, not the decedent)")
            else:
                notes.append(f"decedent's address is owned by {r['mailto']!r}")

    def anchored(rows):
        for r, _ in rows:
            for key in (addr_key(r["site_addr_1"], r["site_addr_3"]), mail_key(r)):
                if key in anchors:
                    return anchors[key]
        return None

    hits = {k: anchored(v) for k, v in res.items()}
    pick = [k for k, a in hits.items() if a == "decedent"] or [k for k, a in hits.items() if a == "applicant"]
    if len(pick) == 1:
        how = hits[pick[0]]
        out.update(method=f"name + {how} address", confidence="high" if how == "decedent" else "medium")
    elif len(res) == 1 and not pick:
        pick = list(res)
        kinds = {h for _, h in res[pick[0]]}
        rare = len(groups) == 1
        out.update(method="name" if "trust" not in kinds else "trust name",
                   confidence="high" if "middle" in kinds and rare else "medium")
        if not rare:
            notes.append(f"{len(groups)} people with this name on the roll, only one owns a home")
    elif res and not pick and len(groups) <= MAX_NAMESAKES and sum(1 for v in res.values() if any(h == "middle" for _, h in v)) == 1:
        # namesakes, but only one of them carries the decedent's middle initial
        pick = [k for k, v in res.items() if any(h == "middle" for _, h in v)]
        out.update(method="name + middle initial", confidence="medium")
        notes.append(f"{len(res)} owners named {first} {last}; only this one has middle initial {middles[0][0]}")
    elif res:
        out.update(confidence="low")
        notes.append(f"{len(res)} different owners named {first} {last}, no address ties one to the decedent")
        pick = []
    rows = [r for k in pick for r, _ in res[k]]
    if hidden_home and (rows or not res):
        mail_home = any(mail_key(r) == addr_key(dec["street"], dec["zip"]) for r in rows)
        if mail_home or not rows:
            rows.insert(0, hidden_home)
            notes.append("decedent's home: HCAD hides the owner name (protected account)")
            if not out["method"]:
                out.update(method="decedent address, owner hidden", confidence="medium")
    if not rows and not res:
        notes.append("no residential HCAD owner by that name")
    if not rows and app and addr_key(app["street"], app["zip"]) in by_site:
        for r in by_site[addr_key(app["street"], app["zip"])]:
            if same_street(r["site_addr_1"], app["street"]) and last in tokens(r["mailto"]):
                notes.append(f"applicant's home is owned by {r['mailto']!r} (spouse/family; the estate may hold a share)")
    out["properties"] = len(rows)
    out["note"] = "; ".join(notes)
    return out, rows


def main(cases_path, idx_path, out_path):
    cases = json.load(open(cases_path))
    print("loading HCAD index...")
    idx = load_hcad(idx_path)
    rows = []
    for c in cases:
        res, props = match(c, idx)
        dec, app = party(c, "Deceased"), party(c, "Applicant")
        res.update({"file_date": c["file_date"], "type": c["type"], "subtype": c["subtype"], "dod": date_of_death(c),
                    "applicant": app["name"] if app else "", "applicant_street": app["street"] if app else "",
                    "applicant_city": app["city"] if app else "", "applicant_state": app["state"] if app else "",
                    "applicant_zip": app["zip"] if app else "", "attorney": app["attorney"] if app else "", "attorney_phone": app["attorney_phone"] if app else "",
                    "decedent_street": dec["street"] if dec else ""})
        for r in props or [None]:
            row = dict(res)
            if r:
                row.update({"acct": r["acct"], "hcad_owner": r["mailto"], "site_addr": r["site_addr_1"],
                            "site_city": r["site_addr_2"], "site_zip": r["site_addr_3"], "state_class": r["state_class"],
                            "value": r["tot_mkt_val"], "sqft": r["bld_ar"], "year": r["yr_impr"],
                            "mail_addr": r["mail_addr_1"], "mail_city": r["mail_city"], "mail_state": r["mail_state"],
                            "mail_zip": r["mail_zip"]})
            rows.append(row)
    cols = list(dict.fromkeys(k for r in rows for k in r))
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(rows)
    by = defaultdict(int)
    for r in {r["case"]: r for r in rows}.values():
        by[r["confidence"]] += 1
    print(f"{len(cases)} cases, {sum(1 for r in rows if r.get('acct'))} properties: " + ", ".join(f"{k} {by[k]}" for k in ("high", "medium", "low", "none")) + f" -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:])
