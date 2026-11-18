"""Harris foreclosure deadline (operator, 2026-11-18): 10 business days before the sale, weekends and US federal
holidays skipped; the lead dies ON that date. No network."""
import os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "harris"))
import harris_status as H  # noqa: E402
import skiptrace_queue as Q  # noqa: E402


class Deadline(unittest.TestCase):
    def test_ten_business_days_with_holidays(self):
        self.assertEqual(H.contract_deadline(date(2027, 1, 5)), date(2026, 12, 18), "Christmas and New Year skipped")
        self.assertEqual(H.contract_deadline(date(2026, 12, 1)), date(2026, 11, 16))
        self.assertEqual(H.contract_deadline(date(2027, 2, 2)), date(2027, 1, 19))
        self.assertEqual(H.contract_deadline(date(2026, 12, 1), business_days=1), date(2026, 11, 30))

    def test_holidays(self):
        h = H.federal_holidays(2026)
        for d in (date(2026, 1, 1), date(2026, 7, 3), date(2026, 11, 26), date(2026, 12, 25)):  # Jul 4 is a Saturday
            self.assertIn(d, h)
        self.assertFalse(H.is_business_day(date(2027, 1, 1)))
        self.assertFalse(H.is_business_day(date(2026, 12, 19)), "Saturday")
        self.assertTrue(H.is_business_day(date(2026, 12, 18)))

    def test_dies_on_the_deadline_date(self):
        sale = date(2027, 1, 5)
        self.assertEqual(H.assess(sale, date(2026, 12, 17))["status"], "Urgent")
        self.assertEqual(H.assess(sale, date(2026, 12, 18))["status"], "Deadline Passed")
        self.assertEqual(H.assess(sale, date(2027, 1, 6))["status"], "Auction Passed")
        self.assertEqual(H.assess(sale, date(2026, 11, 18))["status"], "Active")

    def test_postponement_revives_the_lead(self):
        st = H.assess(date(2026, 12, 1), date(2026, 11, 20), postponed_to=date(2027, 1, 5))
        self.assertEqual((st["status"], st["contract_deadline"]), ("Postponed", date(2026, 12, 18)))


class DeadLeadsLeaveTheQueue(unittest.TestCase):
    def test_queue_skips_dead_foreclosures(self):
        d = {"structure_type": "Single Family Residential", "estimate_value": 200000, "equity_percent": 60,
             "owner": {"phones": []}, "tags": [], "lists": [{"title": "Harris - Foreclosure"}], "status": None}
        self.assertEqual(Q.why_not(d, fstatus="Urgent"), "")
        self.assertIn("dead", Q.why_not(d, fstatus="Deadline Passed"))
        self.assertIn("dead", Q.why_not(d, fstatus="Auction Passed"))


if __name__ == "__main__":
    unittest.main()
