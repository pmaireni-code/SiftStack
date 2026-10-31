"""Rebuild the working leads CSV from the CRM itself, so a scheduled run needs no local state.

    python3 harris_from_crm.py <out.csv> [--list "Harris - Foreclosure"]

Every scheduled run starts in a fresh, empty cloud container, so the CRM is the source of truth:
this reads every lead on the list (address, tags, dates, status, Starting Bid) through the connector
picked by HARRIS_CRM and writes the columns harris_bid_updater.py reads, plus the CRM's record id so
the write-back needs no address lookup. Read-only.
"""
import csv, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402

COLS = ["id", "Property Street Address", "Property ZIP Code", "Foreclosure Date", "Auction Date", "Contract Deadline",
        "Foreclosure Status", "Starting Bid", "Tags"]


def main(out_path, *flags):
    title = flags[flags.index("--list") + 1] if "--list" in flags else "Harris - Foreclosure"
    crm = get_crm()
    rows = crm.read_leads(title)
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} leads read from {crm.name} list {title!r} -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:])
