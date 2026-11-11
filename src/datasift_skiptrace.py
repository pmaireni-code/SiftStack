"""DataSift skip trace counters (read only). STARTING a skip trace over the API is FORBIDDEN.

Operator, 2026-11-11: the run endpoint ignores scoping (below), so it is refused at the source: every
datasift_api_upload.Api call to it raises SkipTraceForbidden, and the Harris connector's safety brakes
block it too, whatever the profile or authorization. --run is kept only to say so. Skip trace is done from
DataSift's own screen (Send To > Skip Trace on a filtered selection). --stats stays free.

History (why):

THE SCOPING PARAMETER IS IGNORED. `POST /api/internal/property/skip-trace/`
accepts a body but does not honour `properties`: a call carrying a single
property uuid skip traced 53,322 records, the whole account. Measured live
2026-08-19 against the stats endpoint:

    total_properties  12,920 -> 66,242   (delta 53,322)
    value_spent       0.0

So treat this endpoint as ALL OR NOTHING. There is no known way to skip trace a
subset over the API; that needs the browser wizard (Send To > Skip Trace on a
filtered selection).

WHY THAT IS TOLERABLE HERE, and would not be elsewhere: this account is on the
unlimited skip trace plan, so an account-wide run costs $0.00. `stats()` reports
`value_spent` and `saved_by_unlimited_skiptracing`, and this module REFUSES to
run if value_spent is non-zero, because on a metered plan the same call would
bill for every record in the account.

    python src/datasift_skiptrace.py --stats            # read only
    python src/datasift_skiptrace.py --run              # REFUSED (forbidden since 2026-11-11)

WHY IT MATTERS: every FTM preset requires `phone: 1` and `skiptraced: 1`.
Records that are correctly tagged and listed still enter no cadence until they
carry a phone, and notices never contain one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datasift_api_upload import Api  # noqa: E402

STATS = "/api/internal/activity/skiptrace/stats/"
RUN = "/api/internal/property/skip-trace/"


def stats(api: Api) -> dict:
    return api.call(STATS)


def show(s: dict) -> None:
    print(f"  properties skip traced : {s.get('total_properties'):,}")
    print(f"  owners                 : {s.get('total_owners'):,}")
    print(f"  phones + emails        : {s.get('both'):,}")
    print(f"  phones only            : {s.get('phones_only'):,}")
    print(f"  emails only            : {s.get('emails_only'):,}")
    print(f"  no result              : {s.get('no_result'):,}")
    print(f"  value spent            : ${s.get('value_spent', 0):,.2f}")
    print(f"  saved by unlimited     : ${s.get('saved_by_unlimited_skiptracing', 0):,.2f}")


def run(api: Api, confirm_unlimited: bool = True) -> dict:
    """Refused, always. Kept so an old caller fails loudly instead of tracing the whole account."""
    from datasift_api_upload import SkipTraceForbidden
    raise SkipTraceForbidden("POST", RUN)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="read the counters and exit")
    ap.add_argument("--run", action="store_true",
                    help="REFUSED: skip trace over the API is forbidden (kept only to say so)")
    a = ap.parse_args()

    if a.run:
        print("REFUSED: skip trace over the API is forbidden (the endpoint ignores scoping and traces the whole "
              "account). Use DataSift's screen: Send To > Skip Trace on a filtered selection. Nothing was called.")
        return 2
    api = Api()
    print("DataSift skip trace, current state:")
    show(stats(api))
    print("\nRead only. Skip trace runs only from DataSift's screen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
