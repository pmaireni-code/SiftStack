"""Trestle phone scoring (src/harris/trestle.py, trestle_score.py, phone_priority.py). Zero network, zero spend:
the Trestle API and DataSift are both simulated.

Run: python3 tests/test_trestle.py   (or pytest)
"""
import io, json, os, sys, unittest
from contextlib import redirect_stdout
from datetime import date, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [os.path.join(ROOT, "src", "harris"), os.path.join(ROOT, "src"), os.path.dirname(__file__)]

import trestle as T  # noqa: E402
import phone_priority as PP  # noqa: E402
from crm import guard  # noqa: E402
from test_harris_guards import run_as, lead  # noqa: E402

TODAY = date(2026, 11, 12)


def ph(n, status="UNKNOWN", typ="MOBILE", tags=(), connected=True):
    return {"number": n, "status": status, "type": typ, "tags": list(tags), "is_connected": connected}


class Rules(unittest.TestCase):
    def test_cuts(self):
        for score, tier in ((100, "Dial First"), (81, "Dial First"), (80, "Dial Second"), (61, "Dial Second"),
                            (60, "Dial Third"), (41, "Dial Third"), (40, "Drop"), (0, "Drop"), (None, None)):
            self.assertEqual(T.tier_for(score), tier, score)

    def test_field_round_trip(self):
        s = {"7135550101": {"score": 85, "litigator": False, "date": TODAY},
             "7135550102": {"score": 72, "litigator": True, "date": TODAY},
             "7135550103": {"score": None, "litigator": False, "date": TODAY}}
        text = T.format_field(s)
        self.assertEqual(text, "7135550101=85@2026-11-12; 7135550102=L72@2026-11-12; 7135550103=?@2026-11-12")
        self.assertEqual(T.parse_field(text), s)
        self.assertEqual(T.parse_field(""), {})

    def test_rescore_after_180_days_never_when_correct(self):
        rec = lambda days: {"score": 50, "litigator": False, "date": TODAY - timedelta(days=days)}
        self.assertTrue(T.due(None, False, TODAY))
        self.assertFalse(T.due(rec(179), False, TODAY))
        self.assertTrue(T.due(rec(180), False, TODAY))
        self.assertFalse(T.due(rec(400), True, TODAY), "a number marked correct is never queried again")

    def test_parse_response_litigator_shapes(self):
        base = {"activity_score": 77, "line_type": "Mobile", "is_valid": True}
        self.assertTrue(T.parse_response({**base, "add_ons": {"litigator_checks": {"phone.is_litigator_risk": True}}})["litigator"])
        self.assertTrue(T.parse_response({**base, "add_ons": {"litigator_checks": {"is_litigator_risk": True}}})["litigator"])
        r = T.parse_response({**base, "add_ons": {"litigator_checks": {"phone.is_litigator_risk": False}}})
        self.assertEqual((r["score"], r["litigator"]), (77, False))
        self.assertIsNone(T.parse_response({"is_valid": False, "activity_score": None})["score"])

    def test_query_sends_placeholder_key_and_litigator_addon(self):
        seen = []

        class R:
            def __init__(s, body): s.b = body
            def read(s): return s.b
            def __enter__(s): return s
            def __exit__(s, *a): return False
        opener = lambda req, timeout=0: seen.append(req) or R(json.dumps({"activity_score": 90}).encode())
        r = T.query("(713) 555-0101", opener=opener)
        self.assertEqual(r["score"], 90)
        req = seen[0]
        self.assertIn("phone=7135550101", req.full_url)
        self.assertIn("add_ons=litigator_checks", req.full_url)
        self.assertEqual(req.get_header("X-api-key"), "proxy-injected", "the real key is injected by the proxy")


class PhonePriorityWithScores(unittest.TestCase):
    def test_scores_decide_the_tier(self):
        phones = [ph("7135550101"), ph("7135550102"), ph("7135550103"), ph("7135550104", status="DNC"),
                  ph("7135550105", status="CORRECT"), ph("7135550106"), ph("7135550107")]
        scores = {"7135550101": {"score": 92, "litigator": False, "date": TODAY},
                  "7135550102": {"score": 35, "litigator": False, "date": TODAY},
                  "7135550103": {"score": 88, "litigator": True, "date": TODAY},
                  "7135550105": {"score": 20, "litigator": False, "date": TODAY},
                  "7135550106": {"score": None, "litigator": False, "date": TODAY}}
        t = PP.plan(phones, scores)
        self.assertEqual(t["7135550101"], "Dial First")
        self.assertEqual(t["7135550102"], "Drop")
        self.assertEqual(t["7135550103"], "Drop", "litigator")
        self.assertEqual(t["7135550104"], "Drop", "DNC")
        self.assertEqual(t["7135550105"], "Dial First", "marked correct stays first, whatever its score")
        self.assertEqual(t["7135550106"], "Drop", "scored but invalid")
        self.assertEqual(t["7135550107"], "Dial Second", "unscored follows the rule, after the correct one")

    def test_without_scores_the_old_rule_is_unchanged(self):
        phones = [ph("1", typ="LANDLINE"), ph("2"), ph("3", status="CORRECT"), ph("4", connected=False)]
        self.assertEqual(PP.plan(phones), {"3": "Dial First", "2": "Dial Second", "1": "Dial Third", "4": "Dial Fourth"})


class LeadOutcome(unittest.TestCase):
    def lead(self, phones, scores, tags=()):
        return {"id": lead(1), "address": "1 Elm St", "owner": "o1", "phones": phones, "scores": scores,
                "tags": list(tags), "todo": []}

    def test_litigator_lead_and_phone_tags(self):
        l = self.lead([ph("7135550101", tags=["Dial First"]), ph("7135550102", tags=["Dial Second"])],
                      {"7135550101": {"score": 90, "litigator": True, "date": TODAY},
                       "7135550102": {"score": 70, "litigator": False, "date": TODAY}})
        tags, add, text = __import__("trestle_score").lead_outcome(l)
        self.assertIn(T.LITIGATOR_LEAD_TAG, add)
        self.assertEqual(tags["7135550101"], sorted(["Drop", "Trestle Scored", "Litigator"]))
        self.assertEqual(tags["7135550102"], sorted(["Dial Second", "Trestle Scored"]))
        self.assertNotIn(T.RESKIP_TAG, add, "a 70 counts as a good number")
        for new in tags.values():
            self.assertEqual(len(set(new) & PP.ALL_TIERS), 1, "exactly one tier per phone")

    def test_needs_reskip_rules(self):
        S = __import__("trestle_score")
        low = lambda n, s=40: {n: {"score": s, "litigator": False, "date": TODAY}}
        # every live number scored under 61 -> Needs Re-Skip
        self.assertIn(T.RESKIP_TAG, S.lead_outcome(self.lead([ph("7135550101"), ph("7135550109", status="DNC")],
                                                             low("7135550101")))[1])
        # one number not scored yet -> not judged yet
        self.assertNotIn(T.RESKIP_TAG, S.lead_outcome(self.lead([ph("7135550101"), ph("7135550102")], low("7135550101")))[1])
        # a number marked correct -> no re-skip
        self.assertNotIn(T.RESKIP_TAG, S.lead_outcome(self.lead([ph("7135550101", status="CORRECT")], low("7135550101")))[1])
        # a litigator 90 is not a good number
        self.assertIn(T.RESKIP_TAG, S.lead_outcome(self.lead([ph("7135550101")],
                                                             {"7135550101": {"score": 90, "litigator": True, "date": TODAY}}))[1])
        # already tagged -> not added twice
        self.assertNotIn(T.RESKIP_TAG, S.lead_outcome(self.lead([ph("7135550101")], low("7135550101"), tags=[T.RESKIP_TAG]))[1])


class FakeCRM:
    """DataSift stand-in: records, a scores field, phone tags; every write recorded."""

    def __init__(self, n_leads=3, phones_per_lead=3, senior=()):
        self.fields = {T.FIELD: {"uuid": "f-ts", "field_type": "text", "options": {}}}
        self.recs, self.field, self.writes = {}, {}, []
        for i in range(n_leads):
            lid = lead(i)
            self.recs[lid] = {"address": {"street": f"{i} Elm St"}, "tags": ["Priority 1"],
                              "lists": [{"title": "Harris - Foreclosure"}] + ([{"title": "Harris - Senior Equity"}] if i in senior else []),
                              "owner": {"uuid": f"o{i}", "phones": [ph(f"71355{i:02d}{j:03d}") for j in range(phones_per_lead)]
                                        + [ph(f"71399{i:02d}999", status="DNC")]}}
        outer = self

        class Api:
            def call(s, path, *a, **k):
                return outer.recs[path.rstrip("/").split("/")[-1]]
        self.api = Api()

    def lead_field(self, lid, label):
        return self.field.get(lid, "")

    def plan(self, changes, commit=True):
        return guard.current().plan(changes, commit)

    def update_lead(self, lid, values, add):
        guard.forbid_native(values)
        if guard.current().before_write(lid, {"set": dict(values), "add_tags": list(add)}):
            return []
        self.writes.append(("lead", lid, dict(values), list(add)))
        self.field[lid] = values[T.FIELD]
        return []

    def set_phone_tags(self, lid, owner, phone, tags):
        if guard.current().before_write(lid, {"phone_tags_before": phone["tags"], "phone_tags_after": tags}):
            return True
        self.writes.append(("phone", lid, phone["number"], list(tags)))
        return True


class ScoreRun(unittest.TestCase):
    def run_score(self, crm, *flags, answers=None, **env):
        import trestle_score as S
        r = run_as("trestle_score", **env)
        r.out = lambda *_: None
        S.get_crm = lambda: crm
        S.PP.pipeline_ids = lambda c, tags, limit=0: list(crm.recs)
        S.date = type("D", (), {"today": staticmethod(lambda: TODAY)})
        queried = []
        S.T.query = lambda n: queried.append(n) or (answers or (lambda n: {"score": 85, "litigator": False}))(n)
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                code = S.main(*flags)
            except SystemExit as e:
                code = e.code
        return code, queried, out.getvalue(), r

    def test_dry_run_spends_nothing(self):
        crm = FakeCRM(n_leads=12)
        code, queried, out, _ = self.run_score(crm, "--commit")          # manual, 12 leads -> simulation
        self.assertEqual((queried, crm.writes), ([], []))
        self.assertIn("36 numbers to query ($0.72", out, "DNC numbers are not queried")

    def test_cap_stops_before_querying(self):
        crm = FakeCRM(n_leads=5)
        code, queried, out, _ = self.run_score(crm, "--commit", SIFT_MODE="live", TRESTLE_MAX_QUERIES="10")
        self.assertEqual(queried, [])
        self.assertIn("over the cap", out + str(code))

    def test_limit_10_queries_writes_and_logs(self):
        crm = FakeCRM(n_leads=5)
        code, queried, out, r = self.run_score(crm, "--commit", "--limit", "10", SIFT_MODE="live")
        self.assertEqual(len(queried), 10)
        self.assertEqual(r.record()["trestle_queries"], 10)
        self.assertEqual(code, 0, out)
        lead0 = T.parse_field(crm.field[lead(0)])
        self.assertEqual(len(lead0), 3)
        self.assertTrue(all(v["score"] == 85 and v["date"] == TODAY for v in lead0.values()))
        phone_writes = [w for w in crm.writes if w[0] == "phone"]
        for w in phone_writes:
            self.assertEqual(len(set(w[3]) & PP.ALL_TIERS), 1, w)
        scored = {T.digits(n) for n in queried}
        self.assertTrue(all("Trestle Scored" in w[3] for w in phone_writes if T.digits(w[2]) in scored))
        self.assertTrue(all("Trestle Scored" not in w[3] for w in phone_writes if T.digits(w[2]) not in scored))

    def test_second_run_does_not_pay_twice(self):
        crm = FakeCRM(n_leads=2)
        self.run_score(crm, "--commit", SIFT_MODE="live")
        _, queried, out, _ = self.run_score(crm, "--commit", SIFT_MODE="live")
        self.assertEqual(queried, [], "numbers scored today are not queried again for 180 days")

    def test_senior_leads_are_skipped(self):
        crm = FakeCRM(n_leads=3, senior=(1,))
        _, queried, _, _ = self.run_score(crm, "--commit", SIFT_MODE="live")
        self.assertFalse([n for n in queried if n.startswith("7135501")])
        self.assertEqual(len(queried), 6)

    def test_litigator_answer_tags_the_lead(self):
        crm = FakeCRM(n_leads=1)
        code, _, out, _ = self.run_score(crm, "--commit", SIFT_MODE="live",
                                         answers=lambda n: {"score": 90, "litigator": n.endswith("000")})
        lead_w = [w for w in crm.writes if w[0] == "lead"][0]
        self.assertIn(T.LITIGATOR_LEAD_TAG, lead_w[3])
        self.assertIn("1 litigator", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
