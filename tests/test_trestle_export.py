"""trestle_export: criteria, refusals, read-only wrapper, Excel shape. No network."""
import json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "harris"))
import trestle_export as T  # noqa: E402


def detail(uid="p1", street="1 Elm St", status=None, attempts=0, phones=(), tags=(), lists=("Harris - Foreclosure",)):
    return {"uuid": uid, "address": {"street": street, "city": "Houston", "state": "TX", "postal_code": "77001"},
            "status": status, "predictivecall_attempts": attempts, "tags": [{"name": t} for t in tags],
            "lists": [{"title": x} for x in lists],
            "owner": {"uuid": "o-" + uid, "first_name": "Ann", "last_name": "Doe", "phones": [
                {"number": n, "type": "MOBILE", "status": s, "tags": [{"name": "Dial First"}], "is_connected": True}
                for n, s in phones]}}


def C(*flags):
    return T.criteria(flags)


class Criteria(unittest.TestCase):
    def test_default_needs_a_no_answer_phone_and_no_status(self):
        c = C()
        self.assertEqual(T.misses(T.lead_view(detail(phones=[("7135550101", "NO_ANSWER")])), c), "")
        self.assertIn("no phone in", T.misses(T.lead_view(detail(phones=[("7135550101", "UNKNOWN")])), c))
        self.assertEqual(T.misses(T.lead_view(detail()), c), "no phones")
        self.assertIn("lead status", T.misses(T.lead_view(detail(status="Warm Lead", phones=[("1", "NO_ANSWER")])), c))

    def test_dead_and_not_interested_never_even_when_allowed(self):
        c = C("--allow-status", "Dead Lead", "--allow-status", "not_interested", "--allow-status", "Warm Lead")
        self.assertEqual(c["allow_status"], ["Warm Lead"])
        for st in ("Dead Lead", "not_interested"):
            self.assertIn("lead status", T.misses(T.lead_view(detail(status={"title": st}, phones=[("1", "NO_ANSWER")])), c))
        self.assertEqual(T.misses(T.lead_view(detail(status="Warm Lead", phones=[("1", "NO_ANSWER")])), c), "")

    def test_senior_litigator_and_bad_phones(self):
        c = C()
        self.assertIn("Senior", T.misses(T.lead_view(detail(phones=[("1", "NO_ANSWER")],
                                                            lists=("Harris - Probate", "Harris - Senior Equity"))), c))
        self.assertEqual(T.misses(T.lead_view(detail(phones=[("1", "NO_ANSWER")], tags=["Litigator - Do Not Contact"])), c),
                         "litigator")
        bad = T.lead_view(detail(phones=[("1", "NO_ANSWER"), ("2", "WRONG")]))
        self.assertIn("wrong", T.misses(bad, c))
        self.assertEqual(T.misses(bad, C("--keep-bad-phones")), "")
        with self.assertRaises(SystemExit):
            C("--list", "Harris - Senior Equity")

    def test_other_criteria(self):
        v = T.lead_view(detail(attempts=2, phones=[("1", "NO_ANSWER"), ("2", "UNKNOWN")], tags=["Priority 1"]))
        self.assertIn("call attempts", T.misses(v, C("--min-attempts", "3")))
        self.assertIn("phones", T.misses(v, C("--min-phones", "3")))
        self.assertIn("not every phone", T.misses(v, C("--all-phones", "NO_ANSWER")))
        self.assertEqual(T.misses(v, C("--tag", "Priority 1")), "")
        self.assertIn("required tag", T.misses(v, C("--tag", "Blitz Active")))
        self.assertIn("excluded tag", T.misses(v, C("--no-tag", "Priority 1")))
        self.assertIn("address", T.misses(v, C("--address", "oak")))
        self.assertEqual(T.misses(v, C("--address", "ELM", "--phone-status", "any")), "")

    def test_ranking(self):
        c = C()
        a = T.lead_view(detail("a", "A St", attempts=4, phones=[("1", "NO_ANSWER")]))
        b = T.lead_view(detail("b", "B St", attempts=1, phones=[("1", "NO_ANSWER"), ("2", "NO_ANSWER")]))
        d = T.lead_view(detail("d", "D St", attempts=5, phones=[("1", "NO_ANSWER")]))
        self.assertEqual([v["id"] for v in sorted([a, b, d], key=lambda v: T.rank_key(v, c))], ["b", "d", "a"])


class Output(unittest.TestCase):
    def test_one_row_per_phone_text_phone_and_full_json(self):
        d = detail(phones=[("7135550101", "NO_ANSWER"), ("7135550102", "UNKNOWN")])
        rows = list(T.rows_for(d))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][:3], ["o-p1", "p1", "Ann Doe"])
        self.assertEqual(json.loads(rows[0][11]), d["owner"]["phones"][0])
        self.assertEqual(rows[0][10], "Dial First")
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "x.xlsx")
            T.write_xlsx(p, rows)
            ws = load_workbook(p)["Telefonos"]
            self.assertEqual([c.value for c in ws[1]], T.COLUMNS)
            self.assertEqual(ws.max_row, 3)
            self.assertEqual({ws.cell(r, 8).data_type for r in (2, 3)}, {"s"})
            self.assertEqual(ws.cell(2, 8).value, "7135550101")


class ReadOnly(unittest.TestCase):
    def test_refuses_writes_allows_reads(self):
        seen = []

        class Api:
            def call(self, path, method="GET", body=None, _retry=True, headers=None):
                seen.append((method, path))
                return {}
        api = Api()
        T.read_only(api)
        api.call("/api/internal/property/x/")
        api.call("/api/internal/property/", "POST", {}, headers={"x-http-method-override": "GET"})
        for m in ("POST", "PATCH", "DELETE", "PUT"):
            with self.assertRaises(PermissionError):
                api.call("/api/internal/owner/o/upsert-phones/", m, {})
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
