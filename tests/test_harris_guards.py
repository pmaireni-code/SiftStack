"""Harris CRM safety brakes (src/harris/crm/guard.py, runlog.py, steps.sh). Zero network.

A mock Api stands in for DataSift under the real PatientApi, so every call goes through the same
choke point production uses and the tests count what would have gone over the wire.

Run: python3 tests/test_harris_guards.py   (or pytest)
"""
import io, json, os, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout
from datetime import date, timedelta

HARRIS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "harris")
sys.path[:0] = [HARRIS, os.path.join(HARRIS, "..")]

from crm import guard, runlog  # noqa: E402
from crm.datasift import DataSiftCRM, PatientApi  # noqa: E402

FUTURE = (date.today() + timedelta(days=20)).strftime("%m/%d/%Y")
PAST = (date.today() - timedelta(days=3)).strftime("%m/%d/%Y")
ENV_KEYS = ("SIFT_PROFILE", "SIFT_MODE", "SIFT_ALLOW_DESTRUCTIVE", "SIFT_MAX_RECORDS", "SIFT_SCHEDULED_CEILING",
            "SIFT_BATCH_PAUSE", "SIFT_TASK", "SIFT_RUN_ID", "SIFT_STEP", "SIFT_RUN_LOG")


class MockApi(PatientApi):
    """PatientApi with the network replaced: records (method, path) and answers like DataSift would."""

    def __init__(self):  # no token minting
        self.calls = []

    def _send(self, path, method, body, _retry, headers):
        self.calls.append((method, path, headers))
        if method == "GET" and "/owner/" not in path and path.rstrip("/").count("/") == 4:
            return {"uuid": path.split("/")[4], "address": {"street": "100 Test St"}, "owner": {"uuid": "o1", "phones": []}}
        return {}

    def writes(self):
        g = guard.current()
        return [(m, p) for m, p, h in self.calls if g.is_write(m, h)]


def make_crm():
    crm = DataSiftCRM.__new__(DataSiftCRM)
    crm.api = MockApi()
    f = lambda t="text", opts=None: {"uuid": f"f-{t}-{len(str(opts))}", "field_type": t, "options": opts or {}}
    crm.fields = {"Starting Bid": {**f(), "uuid": "f-bid"}, "Contract Deadline": {**f("date"), "uuid": "f-cd"},
                  "Auction Date": {**f("date"), "uuid": "f-ad"},
                  "Foreclosure Status": {**f("select", {"Active": "o-a", "Postponed": "o-p", "Urgent": "o-u",
                                                        "Auction Passed": "o-ap"}), "uuid": "f-fs"},
                  "Deal Grade": {**f(), "uuid": "f-dg"}}
    return crm


def lead(i):
    return f"00000000-0000-0000-0000-{i:012d}"


def run_as(task, **env):
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in env.items()})
    r = guard.reset(guard.Run(task=task, out=lambda *_: None))
    r.sleep = lambda s: r.__dict__.setdefault("slept", []).append(s)
    return r


def bid_change(i, status="Active", auction=FUTURE, bid_before="150000"):
    return {"id": lead(i), "address": f"{i} Main St 77001",
            "set": {"Starting Bid": "", "Auction Date": auction, "Contract Deadline": auction, "Foreclosure Status": status},
            "before": {"Starting Bid": bid_before, "Auction Date": auction, "Contract Deadline": auction, "Foreclosure Status": "Active"}}


class Destructive(unittest.TestCase):
    def test_a_unauthorized_delete_is_rejected(self):
        run_as("some_script", SIFT_PROFILE="scheduled")
        crm = make_crm()
        with self.assertRaises(guard.Blocked):
            crm.api.call(f"/api/internal/property/{lead(1)}/", "DELETE")
        self.assertEqual(crm.api.calls, [], "the DELETE must never reach DataSift")
        self.assertEqual(guard.current().status, "blocked")

    def test_a_authorized_delete_goes_through(self):
        run_as("some_script", SIFT_PROFILE="scheduled", SIFT_ALLOW_DESTRUCTIVE="1")
        crm = make_crm()
        crm.api.call(f"/api/internal/property/{lead(1)}/", "DELETE")
        self.assertEqual(crm.api.writes(), [("DELETE", f"/api/internal/property/{lead(1)}/")])

    def test_bulk_clear_over_10_needs_authorization(self):
        run_as("some_script", SIFT_PROFILE="scheduled")
        crm = make_crm()
        changes = [{"id": lead(i), "set": {"Deal Grade": ""}, "before": {"Deal Grade": "Hot"}} for i in range(11)]
        with self.assertRaises(guard.Blocked):
            crm.plan(changes)
        self.assertEqual(crm.api.writes(), [])
        run_as("some_script", SIFT_PROFILE="scheduled")
        crm.plan(changes[:10])  # 10 is fine

    def test_clearing_an_already_empty_field_is_not_destructive(self):
        run_as("some_script", SIFT_PROFILE="scheduled")
        crm = make_crm()
        crm.plan([{"id": lead(i), "set": {"Deal Grade": ""}, "before": {"Deal Grade": ""}} for i in range(50)])


class StartingBid(unittest.TestCase):
    def test_clear_on_future_auction_lead_is_blocked(self):
        run_as("harris_bid_updater", SIFT_PROFILE="scheduled")
        crm = make_crm()
        with self.assertRaises(guard.Blocked) as e:
            crm.plan([bid_change(1, "Active", FUTURE)])
        self.assertIn("Starting Bid", e.exception.reason)
        self.assertEqual(crm.api.writes(), [])

    def test_blocked_even_with_authorization_and_without_a_plan(self):
        run_as("harris_bid_updater", SIFT_PROFILE="scheduled", SIFT_ALLOW_DESTRUCTIVE="1")
        crm = make_crm()
        with self.assertRaises(guard.Blocked):
            crm.update_lead(lead(1), {"Starting Bid": "", "Foreclosure Status": "Active", "Auction Date": FUTURE}, [])
        self.assertEqual(crm.api.writes(), [])

    def test_postponed_past_auction_and_passed_may_clear(self):
        run_as("harris_bid_updater", SIFT_PROFILE="scheduled")
        crm = make_crm()
        crm.plan([bid_change(1, "Postponed", FUTURE), bid_change(2, "Active", PAST), bid_change(3, "Auction Passed", FUTURE)])
        for i, st, d in ((1, "Postponed", FUTURE), (2, "Active", PAST), (3, "Auction Passed", FUTURE)):
            crm.update_lead(lead(i), {"Starting Bid": "", "Foreclosure Status": st, "Auction Date": d, "Contract Deadline": d}, [])
        self.assertEqual(guard.current().record()["leads_written"], 3)

    def test_cap_500_per_run(self):
        run_as("harris_bid_updater", SIFT_PROFILE="scheduled")
        crm = make_crm()
        crm.plan([bid_change(i, "Postponed") for i in range(500)])
        run_as("harris_bid_updater", SIFT_PROFILE="scheduled")
        with self.assertRaises(guard.Blocked):
            crm.plan([bid_change(i, "Postponed") for i in range(501)])

    def test_exception_is_per_task(self):
        run_as("other_script", SIFT_PROFILE="scheduled")  # no exception: the general >10 rule applies
        with self.assertRaises(guard.Blocked):
            make_crm().plan([bid_change(i, "Postponed") for i in range(11)])


class PhoneTiers(unittest.TestCase):
    def test_tier_swap_is_a_change(self):
        run_as("phone_priority", SIFT_PROFILE="scheduled")
        make_crm().plan([{"id": lead(i), "phone_tags_before": ["Dial Second", "smrtphone"],
                          "phone_tags_after": ["Dial First", "smrtphone"]} for i in range(40)])
        self.assertEqual(len(guard.current().destructive), 0)

    def test_phone_left_without_a_tier_counts_as_destructive(self):
        run_as("phone_priority", SIFT_PROFILE="scheduled")
        crm = make_crm()
        ch = [{"id": lead(i), "phone_tags_before": ["Dial First"], "phone_tags_after": []} for i in range(11)]
        with self.assertRaises(guard.Blocked) as e:
            crm.plan(ch)
        self.assertIn("tier", e.exception.reason)
        run_as("phone_priority", SIFT_PROFILE="scheduled")
        g = guard.current()
        self.assertTrue(g._destructive(ch[0]))
        self.assertTrue(g._destructive({"phone_tags_before": ["Dial First"], "phone_tags_after": ["Dial First", "Drop"]}))


class Limits(unittest.TestCase):
    def test_b_600_manual_stops_before_writing(self):
        run_as("harris_upload", SIFT_MODE="live")
        crm = make_crm()
        with self.assertRaises(guard.Blocked) as e:
            crm.plan([{"id": lead(i), "set": {"Deal Grade": "Hot"}} for i in range(600)])
        self.assertIn("cap of 500", e.exception.reason)
        self.assertEqual(crm.api.writes(), [])

    def test_b_600_scheduled_goes_in_batches_and_is_marked(self):
        r = run_as("harris_grade", SIFT_PROFILE="scheduled")
        crm = make_crm()
        crm.plan([{"id": lead(i), "set": {"Deal Grade": "Hot"}} for i in range(600)])
        for i in range(600):
            crm.update_lead(lead(i), {"Deal Grade": "Hot"}, [])
        rec = r.record()
        self.assertTrue(rec["batched"])
        self.assertEqual(rec["batches"], 2)
        self.assertEqual(rec["leads_written"], 600)
        self.assertEqual(len(r.slept), 1, "one pause between the two batches")

    def test_scheduled_ceiling_5000_stops_without_writing_as_error(self):
        r = run_as("harris_grade", SIFT_PROFILE="scheduled")
        crm = make_crm()
        with self.assertRaises(guard.Blocked):
            crm.plan([{"id": lead(i), "set": {"Deal Grade": "Hot"}} for i in range(5001)])
        self.assertEqual(crm.api.writes(), [])
        self.assertEqual(r.record()["status"], "error")

    def test_scheduled_is_live_without_dry_run(self):
        r = run_as("harris_grade", SIFT_PROFILE="scheduled")
        crm = make_crm()
        crm.plan([{"id": lead(i), "set": {"Deal Grade": "Hot"}} for i in range(50)])
        self.assertEqual(r.mode, "live")


class DryRun(unittest.TestCase):
    def test_c_dry_run_writes_nothing_and_returns_the_summary(self):
        out = []
        r = run_as("harris_grade")
        r.out = out.append
        crm = make_crm()
        changes = [{"id": lead(i), "address": f"{i} Elm St 77002", "set": {"Deal Grade": "Hot" if i % 3 else "Warm"},
                    "add_tags": ["Flood Zone"] if i < 4 else []} for i in range(30)]
        with self.assertRaises(guard.DryRunDone):
            crm.plan(changes)
        self.assertEqual(crm.api.calls, [], "a dry run makes no call at all")
        s = out[-1]
        self.assertIn("would change 30 leads", s)
        self.assertIn("Deal Grade", s)
        self.assertIn("Hot x20", s)
        self.assertIn("Warm x10", s)
        self.assertIn("Flood Zone x4", s)
        examples = [l for l in s.split("examples:")[1].splitlines() if l.startswith("  ")]
        self.assertEqual(len(examples), 5)
        self.assertTrue(examples[0].startswith("  0 Elm St 77002"))
        self.assertEqual(r.record()["status"], "dry-run")

    def test_ten_or_fewer_go_live(self):
        r = run_as("harris_upload")
        make_crm().plan([{"id": lead(i), "set": {"Deal Grade": "Hot"}} for i in range(10)])
        self.assertEqual(r.mode, "live")

    def test_forced_dry_run_backstop_skips_connector_writes(self):
        run_as("anything", SIFT_MODE="dry-run")
        crm = make_crm()
        crm.update_lead(lead(1), {"Deal Grade": "Hot"}, ["X"])
        crm.assign_lead.__func__  # noqa: B018 (exists)
        self.assertEqual(crm.api.writes(), [])

    def test_no_plan_manual_stops_at_the_11th_lead(self):
        run_as("anything")
        crm = make_crm()
        for i in range(10):
            crm.update_lead(lead(i), {"Deal Grade": "Hot"}, [])
        with self.assertRaises(guard.Blocked):
            crm.update_lead(lead(10), {"Deal Grade": "Hot"}, [])


SCRIPT = """
import os, sys
sys.path[:0] = [{harris!r}, os.path.join({harris!r}, "..")]
from crm import guard, runlog
r = guard.current(); runlog.start(r)
r.api_calls = 7
how = sys.argv[1]
if how == "ok": sys.exit(0)
if how == "crash": raise RuntimeError("DataSift said no")
if how == "blocked": r._block(guard.Blocked("test block"))
if how == "fails": sys.exit(1)
"""


class RunLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "run_log.jsonl")
        self.script = os.path.join(self.tmp, "fake_task.py")
        with open(self.script, "w") as f:
            f.write(SCRIPT.format(harris=os.path.abspath(HARRIS)))

    def lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f]

    def test_d_every_run_leaves_a_line(self):
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        env.update(SIFT_RUN_LOG=self.log, SIFT_RUN_ID="t1")
        for how in ("ok", "crash", "blocked", "fails"):
            subprocess.run([sys.executable, self.script, how], env=env, capture_output=True)
        got = {(r["status"], r["error"][:16]) for r in self.lines()}
        self.assertEqual(len(self.lines()), 4)
        self.assertEqual(got, {("ok", ""), ("error", "RuntimeError: Da"), ("blocked", "test block"), ("error", "exit code 1")})
        for r in self.lines():
            self.assertEqual(r["task"], "fake_task")
            self.assertEqual(r["api_calls"], 7)
            for k in ("skip_trace_credits", "trestle_queries", "ai_usage"):
                self.assertIsNone(r[k], f"{k} is not measured: null, never 0")
            for k in ("ts_start", "ts_end", "mode", "leads_read", "leads_written", "rejected_by_verification", "batched"):
                self.assertIn(k, r)

    def test_blocked_step_does_not_stop_the_next_daily_steps(self):
        root = os.path.abspath(os.path.join(HARRIS, "..", ".."))
        sh = f"""
set -euo pipefail
cd {root!r}
RUN_NAME=test; source src/harris/steps.sh
blocked() {{ python3 {self.script!r} blocked; }}
grade()   {{ echo graded > {self.tmp!r}/grade.done; }}
phones()  {{ echo phoned > {self.tmp!r}/phones.done; }}
step "bid updater" -- blocked
step "deal grade" -- grade
step "phone priority" -- phones
run_footer
"""
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        env.update(SIFT_RUN_LOG=self.log, SIFT_RUN_ID="t2")
        p = subprocess.run(["bash", "-c", sh], env=env, capture_output=True, text=True)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "grade.done")), p.stdout + p.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "phones.done")))
        self.assertEqual(p.returncode, 1, "the run reports the failed step")
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("RUN LOG LINES:", p.stdout)
        rows = self.lines()
        self.assertEqual([(r["step"], r["status"]) for r in rows], [("bid updater", "blocked")])
        self.assertEqual(rows[0]["profile"], "scheduled")

    def test_step_dying_without_a_line_gets_one(self):
        os.environ["SIFT_RUN_LOG"] = self.log
        try:
            runlog.step_end("market finder", 137, run_id="t3")
        finally:
            os.environ.pop("SIFT_RUN_LOG")
        r = self.lines()[0]
        self.assertEqual((r["step"], r["status"]), ("market finder", "error"))
        self.assertIn("no log line", r["error"])


class Email(unittest.TestCase):
    ROWS = [{"ts_start": "2026-11-12T12:00:00+00:00", "step": "deal grade", "task": "harris_grade", "status": "ok",
             "leads_written": 12, "batched": False},
            {"ts_start": "2026-11-12T12:05:00+00:00", "step": "phone priority", "task": "phone_priority", "status": "ok",
             "leads_written": 640, "batched": True},
            {"ts_start": "2026-11-12T11:58:00+00:00", "step": "bid updater", "task": "harris_bid_updater", "status": "blocked",
             "leads_written": 0, "batched": False, "error": "Starting Bid clear on 9 Oak St is not allowed"}]

    def test_email_summary_shows_the_block_first_and_in_the_subject(self):
        html, text, problems = runlog.digest(self.ROWS)
        self.assertEqual(problems, [("blocked", "bid updater")])
        self.assertIn("ATTENTION: bid updater blocked", html)
        self.assertLess(html.index("bid updater"), html.index("deal grade"))
        self.assertTrue(text.splitlines()[1].startswith("- BLOCKED bid updater"))
        self.assertIn("batches yes", text)
        subject = runlog.subject_tag(problems) + "Daily Report - Charles Cator - November 12, 2026"
        self.assertTrue(subject.startswith("[BLOCKED: bid updater] "))

    def test_clean_day_has_a_plain_subject(self):
        _, _, problems = runlog.digest(self.ROWS[:2])
        self.assertEqual(runlog.subject_tag(problems), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
