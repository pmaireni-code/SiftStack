"""trestle_import: CSV parsing and the per-lead plan. No network."""
import csv, os, sys, tempfile, unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "harris"))
import trestle_import as I  # noqa: E402

DAY = date(2026, 11, 15)
HEAD = ["owner_uuid", "property_uuid", "Telefono", "trestle_http_status", "trestle_is_valid", "trestle_activity_score",
        "trestle_line_type", "trestle_estado", "trestle_error"]


def write(rows, extra=()):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
    w = csv.writer(f)
    w.writerow(HEAD + list(extra))
    w.writerows(rows)
    f.close()
    return f.name


class ReadCsv(unittest.TestCase):
    def test_scores_invalid_and_failed_rows(self):
        p = write([["o1", "p1", "8326665533", "200", "True", "40", "Landline", "ok", ""],
                   ["o1", "p1", "(281) 979-7649", "200", "False", "", "Mobile", "ok", ""],
                   ["o1", "p1", "2810000000", "500", "", "", "", "error", "timeout"]])
        leads, notes, lit = I.read_csv(p, DAY)
        rows = leads["p1"]["rows"]
        self.assertEqual(rows["8326665533"], {"score": 40, "litigator": False, "date": DAY})
        self.assertIsNone(rows["2819797649"]["score"], "an invalid number has no score -> Drop")
        self.assertNotIn("2810000000", rows)
        self.assertEqual(len(notes), 1)
        self.assertFalse(lit)
        self.assertEqual(leads["p1"]["line"]["8326665533"], "LANDLINE")

    def test_litigator_column(self):
        p = write([["o1", "p1", "8326665533", "200", "True", "90", "Mobile", "ok", "", "True"]],
                  extra=["trestle_is_litigator_risk"])
        leads, _, lit = I.read_csv(p, DAY)
        self.assertTrue(lit)
        self.assertTrue(leads["p1"]["rows"]["8326665533"]["litigator"])

    def test_missing_column_refused(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        f.write("Telefono\n123\n")
        f.close()
        with self.assertRaises(SystemExit):
            I.read_csv(f.name, DAY)


class PlanLead(unittest.TestCase):
    def setUp(self):
        self.orig = I.TS.collect

    def tearDown(self):
        I.TS.collect = self.orig

    def lead(self, owner="o1", phones=None):
        phones = phones or [{"number": "8326665533", "type": "LANDLINE", "status": "NO_ANSWER", "tags": ["Dial Third"]},
                            {"number": "3468122117", "type": "MOBILE", "status": "NO_ANSWER", "tags": ["Dial First"]}]
        I.TS.collect = lambda crm, ids, today: [{"id": ids[0], "address": "1 Elm St", "owner": owner, "phones": phones,
                                                  "scores": {}, "tags": [], "todo": []}]

    def csv_lead(self, **scores):
        return {"owner": "o1", "rows": {n: {"score": s, "litigator": False, "date": DAY} for n, s in scores.items()},
                "line": {n: "MOBILE" for n in scores}}

    def test_tiers_from_scores(self):
        self.lead()
        pl = I.plan_lead(None, "p1", self.csv_lead(**{"8326665533": 40, "3468122117": 100}))
        self.assertEqual(pl["phone_tags"]["8326665533"], ["Drop", "Trestle Scored"])
        self.assertEqual(pl["phone_tags"]["3468122117"], ["Dial First", "Trestle Scored"])
        self.assertEqual(pl["add"], [])
        self.assertIn("3468122117=100@2026-11-15", pl["text"])
        self.assertEqual(pl["type_diff"], [("8326665533", "LANDLINE", "MOBILE")], "reported, not written")

    def test_needs_reskip_when_nothing_good(self):
        self.lead()
        pl = I.plan_lead(None, "p1", self.csv_lead(**{"8326665533": 40, "3468122117": 30}))
        self.assertEqual(pl["add"], ["Needs Re-Skip"])

    def test_owner_changed_or_senior_skips(self):
        self.lead(owner="other")
        self.assertIn("owner changed", I.plan_lead(None, "p1", self.csv_lead(**{"8326665533": 40}))["skip"])
        I.TS.collect = lambda crm, ids, today: []
        self.assertIn("Senior", I.plan_lead(None, "p1", self.csv_lead(**{"8326665533": 40}))["skip"])

    def test_number_no_longer_on_lead(self):
        self.lead()
        pl = I.plan_lead(None, "p1", self.csv_lead(**{"3468122117": 100, "7130000000": 90}))
        self.assertEqual(pl["missing"], ["7130000000"])
        self.assertNotIn("7130000000", pl["text"])


class NeverWritesWithoutLive(unittest.TestCase):
    """2026-11-15: --commit alone wrote 2 leads (the brakes let a manual run of <=10 leads go live).
    Now nothing is written unless SIFT_MODE=live is set as well."""

    def test_commit_without_live_is_a_preview(self):
        orig = (I.get_crm, I.TS.collect, os.environ.get("SIFT_MODE"))
        os.environ.pop("SIFT_MODE", None)
        planned = []

        class Crm:
            fields = {I.T.FIELD: {}}
            def plan(self, changes, commit):
                planned.append(commit)
            def update_lead(self, *a):
                raise AssertionError("wrote")
            def set_phone_tags(self, *a):
                raise AssertionError("wrote")
        I.get_crm = lambda: Crm()
        I.TS.collect = lambda crm, ids, today: [{"id": ids[0], "address": "1 Elm St", "owner": "o1", "scores": {}, "tags": [],
                                                  "todo": [], "phones": [{"number": "8326665533", "type": "MOBILE",
                                                                          "status": "NO_ANSWER", "tags": ["Dial Third"]}]}]
        try:
            p = write([["o1", "p1", "8326665533", "200", "True", "90", "Mobile", "ok", ""]])
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(I.main(p, "--commit"), 0)
            self.assertEqual(planned, [False])
        finally:
            I.get_crm, I.TS.collect = orig[0], orig[1]
            if orig[2] is not None:
                os.environ["SIFT_MODE"] = orig[2]


if __name__ == "__main__":
    unittest.main()
