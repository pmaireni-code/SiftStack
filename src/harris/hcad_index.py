"""Build the compact HCAD lookup index that harris_match.py reads.

    python3 hcad_index.py <work_dir> [--year 2026] [--download]

HCAD publishes its full real-property roll for free (download.hcad.org/data/CAMA/<year>/,
refreshed weekly-ish; Real_acct_owner.zip is ~210 MB zipped, real_acct.txt ~890 MB inside).
--download fetches the zip into <work_dir>; the index is written to <work_dir>/idx.tsv with the
columns harris_match.py expects. Columns are picked by HEADER NAME, so a reordered export still
lands right. Building area is bld_ar (land_ar is the lot: mixing them up once reported a 5,267 sq ft
house that is 1,874).
"""
import io, os, sys, urllib.request, zipfile

COLUMNS = ["acct", "mailto", "mail_addr_1", "mail_city", "mail_state", "mail_zip", "site_addr_1", "site_addr_2",
           "site_addr_3", "state_class", "tot_mkt_val", "bld_ar", "yr_impr", "lgl_1", "lgl_2", "lgl_3", "lgl_4"]


def main(work_dir, *flags):
    year = flags[flags.index("--year") + 1] if "--year" in flags else "2026"
    os.makedirs(work_dir, exist_ok=True)
    zpath = os.path.join(work_dir, "Real_acct_owner.zip")
    if "--download" in flags or not os.path.exists(zpath):
        url = f"https://download.hcad.org/data/CAMA/{year}/Real_acct_owner.zip"
        print("downloading", url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=1800) as r, open(zpath, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    out = os.path.join(work_dir, "idx.tsv")
    n = 0
    with zipfile.ZipFile(zpath) as z, z.open("real_acct.txt") as raw, open(out, "w", encoding="latin-1") as dst:
        src = io.TextIOWrapper(raw, encoding="latin-1", newline="")
        header = src.readline().rstrip("\r\n").split("\t")
        pos = [header.index(c) for c in COLUMNS]
        for line in src:
            cells = line.rstrip("\r\n").split("\t")
            if len(cells) < len(header):
                continue
            dst.write("\t".join(cells[i].strip() for i in pos) + "\n")
            n += 1
    print(f"{n:,} accounts -> {out}")


if __name__ == "__main__":
    main(*sys.argv[1:])
