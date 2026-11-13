"""Leads called without an attempt increment (src/harris/attempt_report.py). Zero network: DataSift is simulated.

Run: python3 tests/test_attempt_report.py   (or pytest)
"""
import json, os, sys, unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [os.path.join(ROOT, "src", "harris"), os.path.join(ROOT, "src")]

import attempt_report as A  # noqa: E402
from crm import runlog  # noqa: E402

START = datetime(2026, 11, 12, 1, 0, tzinfo=timezone.utc)      # 7 pm Houston, Nov 11
END = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)
T = lambda h, m=0: (START + timedelta(hours=h, minutes=m)).strftime("%Y-%m-%d %H:%M:%S")


def call(cid, ts, number="7135550101", direction="outbound", user="u-charles", et="owner.call.made"):
    return {"event_type": et, "timestamp": ts, "source": "smrtphone",
            "payload": {"call": {"uuid": cid, "direction": direction, "destination_number": number, "user": user}}}


def inc(ts, old, new):
    return {"event_type": A.INCREASE, "timestamp": ts, "payload": {"property": {"predictivecall_attempts": [old, new]}}}


def sms_inc(ts):
    return {"event_type": "property.sms.attempts.increased", "timestamp": ts, "payload": {"property": {"sms_attempts": [0, 1]}}}


class Verdict(unittest.TestCase):
    def test_call_and_increase_is_ok(self):
        ev = [call("c1", T(17)), inc(T(17, 1), 0, 1)]
        self.assertEqual(A.verdict(ev, ev, 1, START, END)[0], "ok")

    def test_call_without_increase_is_a_probable_miss(self):
        ev = [call("c1", T(17)), call("c2", T(17, 1), number="7135550102")]
        kind, d = A.verdict(ev, ev, 0, START, END)
        self.assertEqual(kind, "forgot")
        self.assertEqual(len({c["number"] for c in d["calls"]}), 2)

    def test_increase_in_only_one_read_is_never_a_miss(self):
        ev = [call("c1", T(17))]
        kind, d = A.verdict(ev + [inc(T(17, 1), 0, 1)], ev, 1, START, END)
        self.assertEqual(kind, "inconsistent")
        self.assertIn("disagree", d["reason"])
        kind, _ = A.verdict(ev, ev + [inc(T(17, 1), 0, 1)], 1, START, END)
        self.assertEqual(kind, "inconsistent")

    def test_counter_up_without_event_is_inconsistent(self):
        ev = [call("c1", T(17))]
        kind, d = A.verdict(ev, ev, 2, START, END)
        self.assertEqual(kind, "inconsistent")
        self.assertIn("no event", d["reason"])

    def test_two_rounds_one_increase_is_ok(self):
        ev = [call("c1", T(15)), call("c2", T(15, 1)), inc(T(15, 2), 0, 1), call("c3", T(18)), call("c4", T(18, 1))]
        self.assertEqual(A.verdict(ev, ev, 1, START, END)[0], "ok")

    def test_increase_before_the_first_call_does_not_count(self):
        ev = [inc(T(10), 0, 1), call("c1", T(17))]
        self.assertEqual(A.verdict(ev, ev, 1, START, END)[0], "forgot")

    def test_sms_counter_and_inbound_calls_do_not_count(self):
        ev = [call("c1", T(17)), sms_inc(T(17, 1))]
        self.assertEqual(A.verdict(ev, ev, 0, START, END)[0], "forgot", "an SMS counter increase is not a call increase")
        ev = [call("c1", T(17), direction="inbound")]
        self.assertIsNone(A.verdict(ev, ev, 0, START, END)[0], "inbound calls are not attempts")

    def test_a_late_outcome_event_keeps_the_call_time(self):
        """smrtPhone can log "no answer" hours after the call: the call's own created time decides the window."""
        before = (START - timedelta(hours=2))
        late = call("c1", T(3), et="owner.call.noanswer")
        late["payload"]["call"]["created"] = before.strftime("%Y-%m-%dT%H:%M:%S+0000")
        self.assertIsNone(A.verdict([late], [late], 0, START, END)[0], "the call was before the window")

    def test_calls_outside_the_window_are_ignored(self):
        ev = [call("c1", (START - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))]
        self.assertIsNone(A.verdict(ev, ev, 0, START, END)[0])


class FakeCRM:
    def __init__(self, leads):
        """leads: id -> {"lists": [...], "counter": n, "reads": [events read 1, events read 2], "street"}"""
        self.leads, self.reads_done, self.writes = leads, {}, []
        outer = self

        class Api:
            def call(s, path, method="GET", body=None, headers=None):
                if path.startswith("/api/internal/list/"):
                    return [{"title": t, "uuid": "L-" + t} for t in ("Harris - Foreclosure", "Harris - Probate", A.SENIOR)]
                if path.startswith("/api/internal/tag/"):
                    return [{"name": "Priority 1", "uuid": "t-p1"}]
                if path == "/api/internal/property/" and (headers or {}).get("x-http-method-override") == "GET":
                    lst = body["query"]["must"]["any_lists"][0]
                    assert lst != "L-" + A.SENIOR, "the Senior list must never be queried"
                    if body["query"]["must"]["property_type"] != "clean" or body["offset"]:
                        return {"results": []}
                    return {"results": [{"uuid": i} for i, l in outer.leads.items() if lst[2:] in l["lists"]]}
                outer.writes.append((method, path))
                return {}
        self.api = Api()

    def _users(self):
        return {"charles cator": "u-charles"}

    def lead_activity(self, lid):
        l = self.leads[lid]
        n = self.reads_done.get(lid, 0)
        self.reads_done[lid] = n + 1
        return ({"lists": [{"title": t} for t in l["lists"]], "predictivecall_attempts": l["counter"],
                 "address": {"street": l["street"], "postal_code": "77001"}}, l["reads"][min(n, 1)])


class Scan(unittest.TestCase):
    def leads(self):
        miss = [call("c1", T(17)), call("c2", T(17, 1), number="7135550102")]
        ok = [call("c3", T(18)), inc(T(18, 1), 0, 1)]
        return {"L1": {"lists": ["Harris - Foreclosure"], "counter": 0, "reads": [miss, miss], "street": "1 Elm St"},
                "L2": {"lists": ["Harris - Probate"], "counter": 1, "reads": [ok, ok], "street": "2 Oak St"},
                "L3": {"lists": ["Harris - Foreclosure"], "counter": 1, "reads": [ok, ok[:1]], "street": "3 Pine St"},
                "L4": {"lists": ["Harris - Foreclosure", A.SENIOR], "counter": 0, "reads": [miss, miss], "street": "4 Senior St"}}

    def test_groups_senior_and_read_only(self):
        crm = FakeCRM(self.leads())
        res = A.scan(crm, START, END)
        self.assertEqual([x["id"] for x in res["forgot"]], ["L1"])
        self.assertEqual([x["id"] for x in res["inconsistent"]], ["L3"], "reads disagree: informative only")
        self.assertEqual(res["skipped_senior"], 1)
        self.assertEqual(crm.reads_done.get("L4"), 1, "a Senior lead is skipped after its own record, never re-read")
        self.assertFalse([x for x in res["forgot"] + res["inconsistent"] if "Senior" in x["address"]])
        self.assertEqual(crm.writes, [], "the report never writes to DataSift")
        f = res["forgot"][0]
        self.assertEqual((f["numbers"], f["callers"]), (2, ["Charles Cator"]))
        self.assertTrue(f["link"].endswith("/L1/details"))

    def test_time_limit_marks_the_section_partial(self):
        ticks = iter([0, 0, 1000, 1000, 1000, 1000])
        res = A.scan(FakeCRM(self.leads()), START, END, budget_s=500, clock=lambda: next(ticks))
        self.assertEqual(res["checked"], 1)
        self.assertEqual(res["unchecked"], 3)
        html, text, counts = A.section(res, START, END)
        self.assertIn("PARTIAL", html)
        self.assertIn("3 lead(s) were not checked", html)
        self.assertEqual(counts["attempt_unchecked"], 3)

    def test_listing_failure_is_reported_not_raised(self):
        class Broken(FakeCRM):
            pass
        crm = Broken({})
        crm.api.call = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout"))
        res = A.scan(crm, START, END)
        html, text, _ = A.section(res, START, END)
        self.assertIn("Could not read the call history today", html)

    def test_section_totals_and_public_counts_have_no_address(self):
        res = A.scan(FakeCRM(self.leads()), START, END)
        html, text, counts = A.section(res, START, END, history=[("d1", 2), ("d2", 1)])
        self.assertIn("Total today: 1 | last 7 days: 4", text)
        self.assertIn("1 Elm St", html, "the address goes in the email")
        rec = runlog.public({"task": "daily_caller_report", "status": "ok", "window_end": END.isoformat(), **counts})
        dumped = json.dumps(rec)
        self.assertNotIn("Elm", dumped)
        self.assertNotIn("L1", dumped)
        self.assertEqual((rec["attempt_forgot"], rec["attempt_inconsistent"]), (1, 1))
        self.assertEqual(rec["window_end"], END.isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
