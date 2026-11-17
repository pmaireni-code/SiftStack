"""skiptrace_queue: which foreclosure leads join the skip-trace queue (operator, 2026-11-17). No network."""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "harris"))
import skiptrace_queue as Q  # noqa: E402


def d(**kw):
    base = {"structure_type": "Single Family Residential", "estimate_value": 250000, "equity_percent": 55,
            "owner": {"phones": []}, "tags": [{"name": "Foreclosure"}], "lists": [{"title": "Harris - Foreclosure"}],
            "status": None}
    base.update(kw)
    return base


class Queue(unittest.TestCase):
    def test_qualifies(self):
        self.assertEqual(Q.why_not(d()), "")
        self.assertEqual(Q.why_not(d(estimate_value=400000, equity_percent=40)), "", "both limits are inclusive")

    def test_each_rule(self):
        self.assertIn("phones", Q.why_not(d(owner={"phones": [{"number": "1"}]})))
        self.assertIn("skip traced", Q.why_not(d(tags=[{"name": "REISift Skipped"}])))
        self.assertIn("skip traced", Q.why_not(d(tags=[{"name": "Skip Trace No Result"}])))
        self.assertIn("queued", Q.why_not(d(tags=[{"name": "Skip Trace Batch 1"}])))
        self.assertIn("single family", Q.why_not(d(structure_type="Duplex (2 units, any combination)")))
        self.assertIn("400k", Q.why_not(d(estimate_value=400001)))
        self.assertIn("400k", Q.why_not(d(estimate_value=None)))
        self.assertIn("equity", Q.why_not(d(equity_percent=39.9)))
        self.assertIn("equity", Q.why_not(d(equity_percent=None)))
        self.assertIn("Senior", Q.why_not(d(lists=[{"title": "Harris - Senior Equity"}])))
        for st in ("Dead Lead", "not_interested", {"title": "closed"}):
            self.assertIn("status", Q.why_not(d(status=st)))

    def test_equity_threshold_is_the_graders_hot_line(self):
        import harris_grade
        self.assertEqual(Q.EQUITY_HOT, harris_grade.EQUITY_HOT)


if __name__ == "__main__":
    unittest.main()
