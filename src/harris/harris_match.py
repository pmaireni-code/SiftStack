"""Prototype: OCR'd Harris County foreclosure notices -> HCAD property match.

Usage: python3 harris_match.py <ocr_dir> <hcad_idx.tsv> <out.csv>
"""
import csv, difflib, glob, os, re, sys
from collections import Counter, defaultdict

WORDS = {w: i for i, w in enumerate(
    "ZERO ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE TEN ELEVEN TWELVE THIRTEEN FOURTEEN "
    "FIFTEEN SIXTEEN SEVENTEEN EIGHTEEN NINETEEN TWENTY".split())}
TENS = {"TWENTY": 20, "THIRTY": 30, "FORTY": 40, "FIFTY": 50, "SIXTY": 60, "SEVENTY": 70, "EIGHTY": 80, "NINETY": 90}
SUFFIX = {"LANE": "LN", "DRIVE": "DR", "STREET": "ST", "ROAD": "RD", "COURT": "CT", "CIRCLE": "CIR",
          "AVENUE": "AVE", "BOULEVARD": "BLVD", "TRAIL": "TRL", "PLACE": "PL", "PARKWAY": "PKWY", "WAY": "WAY"}
NOISE = {"AND", "THE", "OF", "A", "AN", "MAN", "WOMAN", "SINGLE", "UNMARRIED", "MARRIED", "HUSBAND", "WIFE",
         "PERSON", "JR", "SR", "II", "III", "ESTATE", "DECEASED", "INC", "LLC"}


def word_num(s):
    """'TWENTY-FIVE (25)' / '25' / 'THREE' -> 25 / 25 / 3."""
    s = s.upper()
    m = re.search(r"\((\d+)\)", s) or re.match(r"\s*(\d+)", s)
    if m:
        return int(m.group(1))
    parts = re.findall(r"[A-Z]+", s)
    if parts and parts[0] in TENS:
        return TENS[parts[0]] + (WORDS.get(parts[1], 0) if len(parts) > 1 else 0)
    if parts and parts[0] in WORDS:
        return WORDS[parts[0]]
    return None


NUM = r"((?:\d+|[A-Z]+(?:[- ][A-Z]+)?)\s*(?:\(\d+\))?)"


def parse(text):
    flat = re.sub(r"\s+", " ", text.replace("|", " ")).upper()
    out = {}
    m = re.search(r"\b(?:APN|ACCOUNT|ACCT|TAX ID|PARCEL)\b[^0-9]{0,15}(\d{3}[- ]?\d{3}[- ]?\d{3}[- ]?\d{4})", flat)
    if m:
        out["apn"] = re.sub(r"\D", "", m.group(1))
    m = re.search(r"(?:COMMONLY KNOWN AS|PROPERTY ADDRESS)\s*:?\s*(\d{2,6})[:;,.]?\s+([A-Z][A-Z ]{2,40}?)\s"
                  r"(LN|LANE|DR|DRIVE|ST|STREET|RD|ROAD|CT|COURT|CIR|CIRCLE|AVE|AVENUE|BLVD|TRL|TRAIL|PL|PLACE|WAY|PKWY)\b", flat)
    if not m:
        # The watermark often eats the street suffix ("6410 SPARROWS GLE G, TX"): take the number and
        # the next two words; matching only compares the first words of the street anyway
        m = re.search(r"(?:COMMONLY KNOWN AS|PROPERTY ADDRESS)\s*:?\s*(\d{2,6})[:;,.]?\s+([A-Z]{3,}(?: [A-Z]{2,})?)", flat)
    if m:
        out["addr_num"], out["addr_street"] = m.group(1), m.group(2).strip()
    head = r"(?:\bLOT|\bL?OT|^T|\sT)\s+" + NUM + r",?(?:[^,]{0,60}?)\s*(?:[I1{]?N\s+)?BLOCK\s+" + NUM + r"\s*,?\s*(?:OF\s+)?(?:THE\s+)?(?:REPLAT OF\s+)?"
    m = re.search(head + r"(.{3,80}?)(?:,?\s*(?:A|AN)\s+(?:SUBD|SUBDIVISION|ADDITION|REPLAT|DIVISION)|,?\s*IN HARRIS|,\s*ACCORDING|$)", flat)
    if not m:
        # No clean end to the description (the watermark cut it): keep what was read, up to the
        # first comma that is not a section number
        m = re.search(head + r"([A-Z][A-Z0-9 .'&-]{2,60}(?:,\s*SEC(?:TION)?\.?\s*[A-Z0-9()]+)?)", flat)
    if m:
        out["lot"], out["block"], out["subdiv"] = word_num(m.group(1)), word_num(m.group(2)), m.group(3)
    m = re.search(r"(?:EXECUTED BY|GRANTOR\(S\)/MO\w*\s?\w*GAGOR\(S\)\s*:?|TRUSTOR\(S\)\s*:?)\s*(?:[\d/]+\s+)?([A-Z][A-Z .,'-]{4,80}?)"
                  r"(?:,?\s*(?:PROVIDES|SECURING|AND PAYABLE|ORIGINAL|\|)|,\s*(?:A|AN)\s+(?:SINGLE|UNMARRIED|MARRIED)|,\s*(?:HUSBAND|WIFE))", flat)
    if m and "MORTGAGE ELECTRONIC" not in m.group(1):
        out["borrower"] = m.group(1).strip(" ,")
    if not out.get("borrower"):
        m = (re.search(r"\bWITH\s+([A-Z][A-Z .,'!-]{4,80}?)\s+AS\s+GRANTO", flat)
             or re.search(r"([A-Z][A-Z .'!-]{4,60}?),?\s+(?:UNMARRIED|MARRIED|SINGLE)?[A-Z ,]{0,20}\s+AS\s+GRANTOR/BORROWER", flat)
             or re.search(r"CUTED BY\s+([A-Z][A-Z .'_-]{4,80}?)(?:\s+COMMUNITY PROPERTY)?,?\s+SECURING", flat))
        if m:
            out["borrower"] = re.sub(r",?\s*(?:A|AN)?\s*(?:SINGLE|UNMARRIED|MARRIED).*$", "", m.group(1)).replace("!", "L").strip(" ,_")
    out.update(parse_loan(flat))
    return out


# Texas foreclosure counsel seen on Harris notices: keyword in OCR text -> canonical firm name
FIRMS = [("MACKIE WOLF", "Mackie Wolf Zientz & Mann, P.C."), ("CODILIS", "Codilis & Moody, P.C."),
         ("HOLTHUS", "McCarthy & Holthus, LLP"), ("BARRETT DAFFIN", "Barrett Daffin Frappier Turner & Engel, LLP"),
         ("HUGHES WATTERS", "Hughes Watters Askanase, LLP"), ("HUGHES, WATTERS", "Hughes Watters Askanase, LLP"),
         ("MARINOSCI", "Marinosci Law Group"), ("MILLER, GEORGE", "Miller, George & Suggs, PLLC"),
         ("MILLER GEORGE", "Miller, George & Suggs, PLLC"), ("PADGETT", "Padgett Law Group"),
         ("DE CUBAS", "De Cubas & Lewis, P.C."), ("ANSCHUTZ", "Robertson, Anschutz, Schneid, Crane & Partners"),
         ("ALBERTELLI", "Albertelli Law"), ("ALDRIDGE", "Aldridge Pite, LLP"), ("BONIAL", "Bonial & Associates, P.C."),
         ("GHIDOTTI", "Ghidotti Berger, LLP"), ("POWER DEFAULT", "Power Default Services, Inc."),
         ("LOGS LEGAL", "LOGS Legal Group LLP"), ("TROMBERG", "Tromberg, Miller, Morris & Partners")]
# Auction platforms named among the substitute trustees: where the opening bid usually gets published
PLATFORMS = [("AUCTION.COM", "Auction.com"), ("XOME", "Xome"), ("SERVICELINK", "ServiceLink Auction"),
             ("HUBZU", "Hubzu"), ("WILLIAMS & WILLIAMS", "Williams & Williams")]
# Mortgagees/servicers seen on Harris notices; OCR mangles the free text, so match on a stable keyword
LENDERS = [("TRUIST", "Truist Bank"), ("PENNYMAC", "PennyMac Loan Services"), ("MIDFIRST", "MidFirst Bank"),
           ("MIDLAND MORTGAGE", "MidFirst Bank"), ("ROCKET MORTGAGE", "Rocket Mortgage"), ("CARRINGTON", "Carrington Mortgage Services"),
           ("NATIONSTAR", "Nationstar Mortgage (Mr. Cooper)"), ("MR. COOPER", "Nationstar Mortgage (Mr. Cooper)"),
           ("FREEDOM MORTGAGE", "Freedom Mortgage"), ("WELLS FARGO", "Wells Fargo"), ("CHASE BANK", "JPMorgan Chase Bank"),
           ("JPMORGAN", "JPMorgan Chase Bank"), ("JPMOIGAN", "JPMorgan Chase Bank"), ("U.S. BANK", "U.S. Bank"),
           ("US BANK", "U.S. Bank"), ("LAKEVIEW", "Lakeview Loan Servicing"), ("NEWREZ", "NewRez (Shellpoint)"),
           ("SHELLPOINT", "NewRez (Shellpoint)"), ("CENLAR", "Cenlar"), ("LOANCARE", "LoanCare"),
           ("SELECT PORTFOLIO", "Select Portfolio Servicing"), ("PHH", "PHH Mortgage"), ("SPECIALIZED LOAN", "Specialized Loan Servicing"),
           ("FLAGSTAR", "Flagstar Bank"), ("SERVICEMAC", "ServiceMac"), ("GUILD MORTGAGE", "Guild Mortgage"),
           ("PLANET HOME", "Planet Home Lending"), ("DOVENMUEHLE", "Dovenmuehle Mortgage"), ("M&T BANK", "M&T Bank"),
           ("SELENE", "Selene Finance"), ("FAY SERVICING", "Fay Servicing"), ("RUSHMORE", "Rushmore Servicing"),
           ("COMMUNITY LOAN SERVICING", "Community Loan Servicing"), ("LONGBRIDGE", "Longbridge Financial"),
           ("UNITED WHOLESALE", "United Wholesale Mortgage"), ("AMERIHOME", "AmeriHome Mortgage"),
           ("CROSSCOUNTRY", "CrossCountry Mortgage"), ("VILLAGE CAPITAL", "Village Capital & Investment"),
           ("FIFTH THIRD", "Fifth Third Bank"), ("REGIONS", "Regions Bank"), ("CITIZENS BANK", "Citizens Bank"),
           ("BSI FINANCIAL", "BSI Financial Services"), ("GREGORY FUNDING", "Gregory Funding"), ("ROUNDPOINT", "RoundPoint Mortgage"),
           ("MORTGAGE ASSETS MANAGEMENT", "Mortgage Assets Management"), ("REVERSE MORTGAGE FUNDING", "Reverse Mortgage Funding")]
MONTHS = "JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER"


def parse_loan(flat):
    out = {}
    m = re.search(r"(?:PRINCIPAL AMOUNT OF|IN THE AMOUNT OF|ORIGINAL AMOUNT OF)\s*:?\s*\$\s*([\d][\d,. ]{2,14}\d)", flat)
    if m:
        # OCR writes cents as ".00", " 00" or " .00"; drop them before reading the dollars
        raw = re.sub(r"[ .]\s*\d{2}$", "", m.group(1).strip())
        digits = re.sub(r"[ ,.]", "", raw)
        if digits.isdigit() and 5_000 <= int(digits) <= 5_000_000:
            out["loan_amount"] = int(digits)
    m = re.search(r"(?:DEED OF TRUST|SECURITY INSTRUMENT|CONTRACT LIEN)\s+(?:\w+\s+)?(?:IS\s+)?DATED\s+(\d{1,2}/\d{1,2}/\d{4}|(?:" + MONTHS
                  + r")\s+\d{1,2},?\s+\d{4})", flat) or re.search(r"DEED OF TRUST DATE[^0-9]{0,60}(\d{1,2}/\d{1,2}/\d{4})", flat)
    if m:
        out["loan_date"] = m.group(1)
    lenders = [(flat.find(key), name) for key, name in LENDERS if key in flat]
    if lenders:
        out["lender"] = min(lenders)[1]
    firms = [name for key, name in FIRMS if key in flat]
    if firms:
        out["trustee_firm"] = max(set(firms), key=firms.count)
    plats = sorted({name for key, name in PLATFORMS if key in flat})
    if plats:
        out["platforms"] = ", ".join(plats)
    return out


# Words the OCR'd legal description drags in that are not part of any HCAD subdivision name
LEGAL_NOISE = {"HARRIS", "COUNTY", "TEXAS", "TX", "SUBDIVISION", "SUBD", "ADDITION", "AN", "A", "IN", "OF", "THE",
               "AMENDING", "AMENDED", "PLAT", "MAP", "RECORDS", "ACCORDING", "THEREOF", "RECORDED"}


def norm_subdiv(s):
    s = s.upper()
    # "BAUER LANDING, SIX (6)": the OCR ate the word SECTION, the parenthesised number is still the section
    if not re.search(r"\bSEC", s):
        s = re.sub(r",\s*[A-Z]+\s*\((\d+)\)\s*$", r" SEC \1", s)
    s = re.sub(r"\bSECTION\b", "SEC", s)
    s = re.sub(r"\b(SEC)\s+([A-Z]+(?:[- ][A-Z]+)?)\s*(?:\((\d+)\))?", lambda m: f"SEC {m.group(3) or word_num(m.group(2)) or m.group(2)}", s)
    s = re.sub(r"\(\d+\)", "", s)
    return [t for t in re.sub(r"[^A-Z0-9 ]", " ", re.sub(r"\s+", " ", s)).split() if t not in LEGAL_NOISE]


def tokens(name):
    return {t for t in re.findall(r"[A-Z]{2,}", (name or "").upper()) if t not in NOISE}


def main(ocr_dir, idx_path, out_path):
    notices = {}
    for p in sorted(glob.glob(os.path.join(ocr_dir, "*.txt"))):
        text = open(p).read()
        notices[os.path.basename(p)[:-4]] = {**parse(text), "_text": re.sub(r"\s+", " ", text.upper())}
    # Words in the legal boilerplate of most notices ("CURRENT mortgagee", "CITY", "STATE") cannot
    # confirm an owner's surname: HCAD has owners literally named CURRENT OWNER and CITY OF HOUSTON
    df = Counter(w for n in notices.values() for w in set(re.findall(r"[A-Z]{3,}", n["_text"])))
    boiler = {w for w, k in df.items() if k > 0.15 * len(notices)}

    def surname_seen(owner, borrower, text):
        """The owner of record is named in this notice: surname in the parsed borrower, or at least TWO
        of the owner's name words anywhere in the text (one word alone matched OCR noise: "Cope" was
        "Tex Prop Code", "San" is any San ...)."""
        # HCAD drops hyphens ("CHADROBERT"), the notice keeps them ("Chad-Robert"): compare without them
        owner, text = owner.replace("-", ""), text.replace("-", "")
        words = [w.rstrip(",") for w in owner.split() if len(w.rstrip(",")) >= 3 and w not in NOISE and w.rstrip(",") not in boiler]
        if not words or owner.split()[0].rstrip(",") not in words:
            return False
        if words[0] in borrower:
            return True
        # The SURNAME must be there, next to another word of the same name ("CHRISTIAN, MARQUI" or
        # "MARQUI CHRISTIAN"): words scattered across the notice matched a law firm's "Camino Del Rio"
        others = "|".join(re.escape(w) for w in words[1:3])
        sur = re.escape(words[0])
        return bool(others) and bool(re.search(rf"\b{sur}\b\W+(?:\w+\W+)?(?:{others})\b|\b(?:{others})\b\W+(?:\w+\W+)?{sur}\b", text))

    want_legal = {(n["lot"], n["block"]) for n in notices.values() if n.get("lot") is not None and n.get("block") is not None}
    lot_blk = re.compile(r"\bLTS? (\d+)\b.*?\bBLK (\d+)\b")
    want_addr = {n["addr_num"] for n in notices.values() if "addr_num" in n}
    by_legal, by_num = defaultdict(list), defaultdict(list)
    cols = ["acct", "owner", "mail_1", "mail_city", "mail_state", "mail_zip", "site", "city", "zip", "class", "mkt_val",
            "sqft", "yr_built", "lgl_1", "lgl_2", "lgl_3", "lgl_4"]
    want_apn = {n["apn"] for n in notices.values() if "apn" in n}
    surnames = {t for n in notices.values() for t in tokens(n.get("borrower")) if len(t) >= 4}
    by_apn, by_name = {}, defaultdict(list)
    with open(idx_path, encoding="latin-1") as f:
        for line in f:
            r = dict(zip(cols, (c.strip() for c in line.rstrip("\n").split("\t"))))
            if r["acct"] in want_apn:
                by_apn[r["acct"]] = r
            # HCAD keeps partial lots as "LT 2 LESS N 1 FT BLK 7", so key on the numbers, not the string
            lb = lot_blk.search(r["lgl_1"])
            if lb and (int(lb.group(1)), int(lb.group(2))) in want_legal:
                by_legal[(int(lb.group(1)), int(lb.group(2)))].append(r)
            num = r["site"].split(" ", 1)[0]
            if num in want_addr:
                by_num[num].append(r)
            first = r["owner"].split(" ", 1)[0]
            if first in surnames:
                by_name[first].append(r)

    rows = []
    for doc, n in notices.items():
        best, how, score = None, "", 0.0
        if n.get("apn") in by_apn:
            best, score, how = by_apn[n["apn"]], 1.0, "apn"
        if best is None and "addr_num" in n:
            street = n["addr_street"].split()
            for r in by_num.get(n["addr_num"], []):
                site = r["site"].split()[1:]
                s = difflib.SequenceMatcher(None, " ".join(street[:2]), " ".join(site[:2])).ratio()
                if s > score:
                    best, score, how = r, s, "address"
            if score < 0.75:
                best, score, how = None, 0.0, ""
        if best is None and n.get("lot") is not None and n.get("block") is not None:
            want = " ".join(norm_subdiv(n.get("subdiv", "")))
            borrower = tokens(n.get("borrower"))
            # "HARRIS COUNTY, TEXAS" is not a subdivision: with nothing real to compare, only the
            # owner's surname appearing in the notice can carry the match
            meaningful = any(len(t) >= 4 and not t.isdigit() and t != "SEC" for t in want.split())
            plain = {}  # subdivision similarity before any surname boost, per HCAD legal name
            named = False  # the winning candidate was confirmed by the owner's name
            for r in by_legal.get((n["lot"], n["block"]), []):
                s = difflib.SequenceMatcher(None, want, r["lgl_2"]).ratio() if meaningful else 0.4
                # OCR often truncates the subdivision ("FAIRGR"): counts only when what was read is a
                # true prefix of the HCAD name, never a shared first word ("ROYAL OAKS TERRANCE" is not
                # ROYAL OAKS COURT)
                if meaningful and len(want) >= 5 and r["lgl_2"].startswith(want):
                    s = max(s, 0.85)
                ws, hs = re.search(r"\bSEC (\d+)", want), re.search(r"\bSEC (\d+)", r["lgl_2"])
                if ws and hs and ws.group(1) != hs.group(1):
                    s -= 0.5
                plain[r["lgl_2"]] = max(plain.get(r["lgl_2"], 0), s)
                # HCAD writes LAST FIRST: the owner's SURNAME in the borrower or anywhere in the notice
                # confirms, but only as a tie-break: it never rescues a different subdivision
                seen = s >= 0.4 and surname_seen(r["owner"], borrower, n["_text"])
                if seen:
                    s += 0.5
                if s > score:
                    best, score, how, named = r, s, "legal", seen
            # Several sections of one subdivision fit equally well when the OCR lost the section number
            # ("BAUER LANDING, ION"): without the owner's name to break the tie, no match beats a guess
            top = sorted(plain.values(), reverse=True)
            if best is not None and not named and len(top) > 1 and top[1] >= top[0] - 0.05:
                best, score, how = None, 0.0, ""
            if score < 0.75:
                best, score, how = None, 0.0, ""
        if best is None and n.get("borrower"):
            # Last resort (condos, unreadable legal): owner must carry 2+ borrower name tokens, and be unique
            borrower = tokens(n["borrower"])
            cands = [r for t in borrower for r in by_name.get(t, []) if len(borrower & tokens(r["owner"])) >= 2]
            uniq = {r["acct"]: r for r in cands}
            if len(uniq) == 1:
                best, score, how = next(iter(uniq.values())), 0.7, "name"
        name_ok = bool(best and surname_seen(best["owner"], tokens(n.get("borrower")), n["_text"]))
        rows.append({"doc": doc, "match": how or "NONE", "score": round(score, 2), "name_confirmed": name_ok,
                     "borrower_ocr": n.get("borrower", ""), "lot": n.get("lot"), "block": n.get("block"),
                     "loan_amount": n.get("loan_amount", ""), "loan_date": n.get("loan_date", ""), "lender": n.get("lender", ""),
                     "trustee_firm": n.get("trustee_firm", ""), "platforms": n.get("platforms", ""),
                     "subdiv_ocr": n.get("subdiv", ""), "addr_ocr": f"{n.get('addr_num', '')} {n.get('addr_street', '')}".strip(),
                     **({k: best[k] for k in ("acct", "owner", "site", "city", "zip", "class", "mkt_val", "sqft", "yr_built",
                                               "mail_1", "mail_city", "mail_state", "mail_zip", "lgl_1", "lgl_2")} if best else {})})
    fields = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    fields = list(rows[0].keys()) + [k for k in ("acct", "owner", "site", "city", "zip", "class", "mkt_val", "sqft", "yr_built",
                                                 "mail_1", "mail_city", "mail_state", "mail_zip", "lgl_1", "lgl_2") if k not in rows[0]]
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    hit = sum(r["match"] != "NONE" for r in rows)
    print(f"{hit}/{len(rows)} matched ({sum(r['match']=='address' for r in rows)} by address, "
          f"{sum(r["match"]=="legal" for r in rows)} by legal, {sum(r["match"]=="apn" for r in rows)} by apn, {sum(r["match"]=="name" for r in rows)} by name), {sum(r['name_confirmed'] for r in rows)} name-confirmed")


if __name__ == "__main__":
    main(*sys.argv[1:4])
