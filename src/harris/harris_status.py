"""Where a foreclosure lead stands against its trustee sale, for a buy-before-auction strategy.

Contract Deadline: the last day a signed purchase contract still has time to stop the sale. Before
the auction the lender has to receive the contract, the title company has to order a payoff
statement (servicers have up to 7 business days to send one) and the closing or postponement has to
land. Operator's rule for Harris (2026-11-18): the deadline is DEADLINE_BUSINESS_DAYS (10) business days
before the sale, counting back over weekdays that are not US federal holidays, and ON that date the lead
dies ("Deadline Passed"): it leaves the call filters, the skip-trace queue and the entity lookup.

Status, recomputed every day:
  Active           deadline more than URGENT_DAYS away
  Urgent           deadline within URGENT_DAYS: contract has to be signed now
  Deadline Passed  the deadline date reached, before the sale: the lead is dead for this sale date
  Postponed        the property is relisted for a LATER sale: dates move, the lead is alive again
  Auction Passed   the sale date is gone
"""
from datetime import date, datetime, timedelta

DEADLINE_BUSINESS_DAYS = 10  # operator, 2026-11-18 (was 7 calendar days, set 2026-10-31; before that 14)
URGENT_DAYS = 7
SALE_TIME = "10:00 AM"  # Texas trustee sales run 10 AM-4 PM on the first Tuesday; every Harris notice read says 10:00 AM


def parse_date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except (TypeError, ValueError):
            pass
    return None


def _nth(y, m, weekday, n):
    d = date(y, m, 1) + timedelta(days=(weekday - date(y, m, 1).weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last(y, m, weekday):
    d = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def federal_holidays(year):
    """US federal holidays (observed dates): the days title companies, lenders and courts are closed."""
    return {_observed(date(year, 1, 1)), _nth(year, 1, 0, 3), _nth(year, 2, 0, 3), _last(year, 5, 0),
            _observed(date(year, 6, 19)), _observed(date(year, 7, 4)), _nth(year, 9, 0, 1), _nth(year, 10, 0, 2),
            _observed(date(year, 11, 11)), _nth(year, 11, 3, 4), _observed(date(year, 12, 25))}


def is_business_day(d):
    return d.weekday() < 5 and d not in federal_holidays(d.year) and d not in federal_holidays(d.year + 1)


def contract_deadline(sale, business_days=DEADLINE_BUSINESS_DAYS):
    """The date `business_days` business days before the sale (weekends and federal holidays skipped)."""
    d, n = sale, 0
    while n < business_days:
        d -= timedelta(days=1)
        if is_business_day(d):
            n += 1
    return d


def assess(sale, today=None, postponed_to=None):
    """-> dict(auction_date, contract_deadline, days_to_deadline, days_to_auction, status)."""
    today = today or date.today()
    status = None
    if postponed_to and sale and postponed_to > sale:
        sale, status = postponed_to, "Postponed"
    if not sale:
        return {"auction_date": None, "contract_deadline": None, "days_to_deadline": None, "days_to_auction": None, "status": ""}
    deadline = contract_deadline(sale)
    to_deadline, to_auction = (deadline - today).days, (sale - today).days
    if to_auction < 0:
        status = "Auction Passed"
    elif to_deadline <= 0:  # the lead dies ON the deadline date (operator, 2026-11-18)
        status = "Deadline Passed"
    elif status != "Postponed":
        status = "Urgent" if to_deadline <= URGENT_DAYS else "Active"
    return {"auction_date": sale, "contract_deadline": deadline, "days_to_deadline": to_deadline,
            "days_to_auction": to_auction, "status": status}


if __name__ == "__main__":
    # Spot checks against a 12/01/2026 sale
    s = date(2026, 12, 1)
    for today in (date(2026, 10, 31), date(2026, 11, 12), date(2026, 11, 18), date(2026, 12, 2)):
        print(today, assess(s, today))
    print("postponed", assess(s, date(2026, 11, 20), postponed_to=date(2027, 1, 5)))
