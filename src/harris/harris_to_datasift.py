"""Matched Harris foreclosure notices -> DataSift upload CSV (English, DataSift column names).

Usage: python3 harris_to_datasift.py <match.csv> <meta.json> <out.csv>
"""
import csv, json, re, sys

from harris_status import SALE_TIME, assess, parse_date
from datetime import date, datetime

ENTITY = re.compile(r"\b(LLC|INC|CORP|CO|COMPANY|TRUST|BANK|INSURANCE|LP|LTD|PROPERTIES|HOLDINGS|ASSOCIATION|CHURCH)\b")
COLS = ["Property Street Address", "Property City", "Property State", "Property ZIP Code", "Owner First Name", "Owner Last Name",
        "Mailing Street Address", "Mailing City", "Mailing State", "Mailing ZIP Code", "Lists", "Tags", "Notes",
        "Estimated Value", "Foreclosure Date", "Parcel ID", "Year Built", "Living SqFt", "Notice Type", "County", "Date Added",
        "Owner Deceased", "Original Loan Amount", "Loan Date", "Lender", "Trustee Firm", "Auction Platform", "Starting Bid", "Auction Date", "Contract Deadline", "Foreclosure Status"]


def tc(s):
    """Title case that keeps hyphenated names and Mc/unit numbers readable."""
    words = []
    for w in (s or "").split():
        w = "-".join(p.capitalize() for p in w.split("-"))
        if re.match(r"^Mc[a-z]", w):
            w = "Mc" + w[2:].capitalize()
        words.append(w)
    return " ".join(words)


def street(site):
    # HCAD writes condo units as a trailing 4-digit field ("1915 AUGUSTA DR 0004")
    m = re.match(r"^(.*\S)\s+0*(\d{1,4})$", site or "")
    return f"{tc(m.group(1))} Unit {m.group(2)}" if m and re.search(r"\b(DR|LN|ST|RD|CT|AVE|BLVD|TRL|CIR|PL|WAY|PKWY)$", m.group(1)) else tc(site)


def loan_date(s):
    """'FEBRUARY 21, 2023' / '3/31/2021' -> '02/21/2023' / '03/31/2021'."""
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime((s or "").title(), fmt).strftime("%m/%d/%Y")
        except ValueError:
            pass
    return ""


def split_owner(raw):
    """HCAD owner is LAST FIRST [& co-owner]."""
    raw = (raw or "").strip()
    deceased = "ESTATE OF" in raw
    clean = re.sub(r"\bESTATE OF\b", "", raw).strip()
    if ENTITY.search(clean):
        return "", tc(clean), deceased
    first_party = clean.split("&")[0].split()
    last, first = (first_party[0], " ".join(first_party[1:])) if first_party else ("", "")
    return tc(first), tc(last), deceased


def main(match_path, meta_path, out_path):
    meta = {m["doc"]: m["cells"] for m in json.load(open(meta_path))}
    out, skipped = [], 0
    for r in csv.DictReader(open(match_path)):
        if r["match"] == "NONE":
            skipped += 1
            continue
        dates = [c for c in meta.get(r["doc"], []) if c.count("/") == 2]
        sale, filed = (dates + ["", ""])[:2]
        first, last, deceased = split_owner(r["owner"])
        st = assess(parse_date(sale))
        fmt = lambda d: d.strftime("%m/%d/%Y") if d else ""
        verified = r["name_confirmed"] == "True"
        tags = ["Harris", "Foreclosure", f"Auction {sale}"] + ([] if verified else ["Verify Owner"]) + (["Estate"] if deceased else [])
        loan = f" Original loan ${int(r['loan_amount']):,}" + (f" dated {loan_date(r['loan_date'])}" if loan_date(r['loan_date']) else "") + "." if r["loan_amount"] else ""
        notes = (f"Harris County foreclosure notice {r['doc']} filed {filed}, trustee sale {sale} {SALE_TIME}."
                 + (f" Contract deadline {fmt(st['contract_deadline'])}." if st["contract_deadline"] else "")
                 + loan + (f" Lender: {r['lender']}." if r["lender"] else "") + (f" Trustee firm: {r['trustee_firm']}." if r["trustee_firm"] else "") + " "
                 f"Owner of record (HCAD): {r['owner']}. Estimated Value = HCAD 2026 market value. "
                 + ("Owner name confirmed against the notice." if verified
                    else "VERIFY: owner of record is not named in the notice (possible ownership change).")
                 + (f" Borrower per notice: {tc(r['borrower_ocr'])}." if r["borrower_ocr"] else ""))
        out.append({"Property Street Address": street(r["site"]), "Property City": tc(r["city"]), "Property State": "TX",
                    "Property ZIP Code": r["zip"], "Owner First Name": first, "Owner Last Name": last,
                    "Mailing Street Address": street(r["mail_1"]), "Mailing City": tc(r["mail_city"]),
                    "Mailing State": r["mail_state"], "Mailing ZIP Code": r["mail_zip"], "Lists": "Harris - Foreclosure",
                    "Tags": ", ".join(tags), "Notes": notes, "Estimated Value": r["mkt_val"], "Foreclosure Date": sale,
                    "Parcel ID": r["acct"], "Year Built": r["yr_built"], "Living SqFt": r["sqft"], "Notice Type": "Foreclosure",
                    "County": "Harris", "Date Added": date.today().strftime("%m/%d/%Y"), "Owner Deceased": "Yes" if deceased else "",
                    "Original Loan Amount": r["loan_amount"], "Loan Date": loan_date(r["loan_date"]), "Lender": r["lender"],
                    "Trustee Firm": r["trustee_firm"], "Auction Platform": r["platforms"],
                    # Never in the notice: the lender sets the credit bid a day or two before the sale
                    "Starting Bid": "", "Auction Date": fmt(st["auction_date"]),
                    "Contract Deadline": fmt(st["contract_deadline"]), "Foreclosure Status": st["status"]})
    # A property can carry several notices (re-posted, corrected, a second lien): one row per HCAD
    # account, keeping the most recently filed notice, the others named in its Notes
    by_acct = {}
    for row in out:
        filed = parse_date(row["Notes"].split(" filed ", 1)[1].split(",")[0]) if " filed " in row["Notes"] else None
        prev = by_acct.get(row["Parcel ID"])
        if prev is None or (filed and (prev[0] is None or filed > prev[0])):
            if prev:
                row["Notes"] += f" Also noticed as {prev[1]['Notes'].split()[4]}."
            by_acct[row["Parcel ID"]] = (filed, row)
        else:
            prev[1]["Notes"] += f" Also noticed as {row['Notes'].split()[4]}."
    dupes = len(out) - len(by_acct)
    out = [r for _, r in by_acct.values()]
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(out)
    print(f"{len(out)} rows written, {skipped} unmatched left out, {dupes} duplicate notices folded")


if __name__ == "__main__":
    main(*sys.argv[1:4])
