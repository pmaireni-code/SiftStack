"""Which Harris homesteads belong to an owner 65 or older, from HCAD's own exemption amounts.

    python3 hcad_senior.py <work_dir> [--year 2026] [--download]     -> <work_dir>/senior_accts.json

HCAD publishes every account's exemptions (Real_jur_exempt.zip) but BLANKS the age and disability
codes: OVR / POV / SUR / DIS appear in the code list and on zero accounts. The amounts survive. An
over-65 (or disabled) homestead gets a flat extra exemption on top of the residence homestead, and
HCAD folds it into the RES row's exempt value. So per taxing district:

    residual = RES exempt value - (RES flat + RES percent x appraised value)

and residual equal to that district's OVR amount is the over-65 extra. Most districts give over-65
and disability the same amount (Harris County $336,000, Houston ISD $65,000): "Over-65 or Disabled".
42 give them different amounts (Spring Branch ISD $81,400 vs $65,000): there it is "Over-65" or
"Disabled". Measured 2026-11-01: 91,663 Over-65, 170,645 Over-65 or Disabled, 5,207 Disabled; where
the two can be told apart 95% are over-65. A home whose exemption covers its whole value cannot be
read. Checked against probate: 28 of 40 decedents' own homes carry the flag.
"""
import collections, json, os, sys, urllib.request, zipfile

SENIOR = ("Over-65", "Over-65 or Disabled")


def classify(zpath):
    z = zipfile.ZipFile(zpath)
    rates = collections.defaultdict(dict)
    for line in z.open("jur_tax_dist_exempt_value_rate.txt").read().decode("latin-1").splitlines()[1:]:
        r = line.split("\t")
        if r[0] == "Real":
            try:
                rates[r[1]][r[3]] = (float(r[6]), float(r[7]))
            except (ValueError, IndexError):
                pass
    appraised = {}
    with z.open("jur_value.txt") as f:
        for raw in f:
            c = raw.decode("latin-1").rstrip("\r\n").split("\t")
            try:
                appraised[(c[0], c[1])] = float(c[4])
            except (ValueError, IndexError):
                pass
    votes = collections.defaultdict(collections.Counter)
    with z.open("jur_exempt.txt") as f:
        for raw in f:
            c = raw.decode("latin-1").rstrip("\r\n").split("\t")
            if len(c) < 4 or c[2] != "RES":
                continue
            try:
                ev = float(c[3])
            except ValueError:
                continue  # "Pending"
            a, R = appraised.get((c[0], c[1])), rates.get(c[1], {})
            if not a or "RES" not in R:
                continue
            flat, pct = R["RES"]
            if ev >= a - 1:
                votes[c[0]]["capped"] += 1
                continue
            resid = ev - min(a, flat + pct * a)
            ovr, dis = R.get("OVR", (0, 0))[0], R.get("DIS", (0, 0))[0]
            near = lambda x: x and abs(resid - x) < 1000
            if abs(resid) < 1000:
                votes[c[0]]["none"] += 1
            elif near(ovr) and abs(ovr - dis) >= 1000:
                votes[c[0]]["ovr"] += 1
            elif near(dis) and abs(ovr - dis) >= 1000:
                votes[c[0]]["dis"] += 1
            elif near(ovr):
                votes[c[0]]["either"] += 1
            else:
                votes[c[0]]["other"] += 1
    out = {}
    for acct, v in votes.items():
        if v["ovr"] and not v["dis"]:
            out[acct] = "Over-65"
        elif v["either"] and not v["dis"] and not v["ovr"]:
            out[acct] = "Over-65 or Disabled"
        elif v["dis"] and not v["ovr"]:
            out[acct] = "Disabled"
    return out


def main(work_dir, *flags):
    year = flags[flags.index("--year") + 1] if "--year" in flags else "2026"
    zpath = os.path.join(work_dir, "Real_jur_exempt.zip")
    if "--download" in flags or not os.path.exists(zpath):
        url = f"https://download.hcad.org/data/CAMA/{year}/Real_jur_exempt.zip"
        print("downloading", url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=1800) as r, open(zpath, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    out = classify(zpath)
    json.dump(out, open(os.path.join(work_dir, "senior_accts.json"), "w"))
    c = collections.Counter(out.values())
    print("homesteads: " + ", ".join(f"{k} {v:,}" for k, v in c.most_common()) + f" -> {work_dir}/senior_accts.json")


if __name__ == "__main__":
    main(*sys.argv[1:])
