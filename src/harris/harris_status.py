"""Where a foreclosure lead stands against its trustee sale, for a buy-before-auction strategy.

Contract Deadline: the last day a signed purchase contract still has time to stop the sale. Before
the auction the lender has to receive the contract, the title company has to order a payoff
statement (servicers have up to 7 business days to send one) and the closing or postponement has to
land. DEADLINE_DAYS is that runway in calendar days, moved back to a weekday.
7 is tight against a 7-business-day payoff turnaround, so contracts near the deadline should
already have the payoff requested.

Status, recomputed every day:
  Active           deadline more than URGENT_DAYS away
  Urgent           deadline within URGENT_DAYS: contract has to be signed now
  Deadline Passed  past the deadline, before the sale: the lead is lost for this sale date
  Postponed        the property is relisted for a LATER sale: dates move, the lead is alive again
  Auction Passed   the sale date is gone
"""
from datetime import date, datetime, timedelta

DEADLINE_DAYS = 7  # set by the user (2026-10-31); was 14
URGENT_DAYS = 7
SALE_TIME = "10:00 AM"  # Texas trustee sales run 10 AM-4 PM on the first Tuesday; every Harris notice read says 10:00 AM


def parse_date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except (TypeError, ValueError):
            pass
    return None


def contract_deadline(sale, days=DEADLINE_DAYS):
    d = sale - timedelta(days=days)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
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
    elif to_deadline < 0:
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
