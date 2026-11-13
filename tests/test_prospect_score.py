"""Prospect Score instead of DataSift's own temperature (task 3, 2026-11-11). Zero network.

  * no script in src/harris writes the temperature or the property status, and the brake refuses it;
  * Prospect Score / Prospect Reasons carry exactly what Deal Grade / Lead Temperature carry;
  * temperature_cleanup resets only pipeline-set, never-contacted leads, through the brakes.

Run: python3 tests/test_prospect_score.py   (or pytest)
"""
import csv, io, json, os, re, sys, tempfile, unittest
from contextlib import redirect_stdout

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HARRIS = os.path.join(ROOT, "src", "harris")
sys.path[:0] = [HARRIS, os.path.join(ROOT, "src"), os.path.dirname(__file__)]

import account_lock  # noqa: E402
from crm import guard  # noqa: E402
from test_harris_guards import MockApi, lead, make_crm, run_as  # noqa: E402

PIPE = "pipeline@example.com"


def crm_with_prospect():
    crm = make_crm()
    crm.fields.update({
        "Prospect Score": {"uuid": "f-ps", "field_type": "select",
                           "options": {o: "o-" + o for o in ("Hot", "Warm", "Cold", "Not Viable", "Needs Data")}},
        "Prospect Reasons": {"uuid": "f-pr", "field_type": "text", "options": {}},
        "Deal Notes": {"uuid": "f-dn", "field_type": "text", "options": {}},
        "ZIP Investor Activity": {"uuid": "f-zia", "field_type": "select", "options": {"High": "o-h", "Medium": "o-m", "Low": "o-l"}},
        "Deal Grade": {"uuid": "f-dg", "field_type": "select",
                       "options": {o: "o-g-" + o for o in ("Hot", "Warm", "Not Viable", "Needs Data")}}})
    return crm


class NoScriptWritesTemperatureOrStatus(unittest.TestCase):
    def test_no_hotness_or_status_write_left_in_src_harris(self):
        """Static: the only call to the temperature route is the guarded reset, and nothing builds a
        "Property Temperature" value or a property status for the CRM."""
        hits = []
        for d, _, files in os.walk(HARRIS):
            for f in files:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(d, f)
                for n, line in enumerate(open(p), 1):
                    if re.search(r"/hotness/", line) and f not in ("guard.py", "datasift.py"):
                        hits.append(f"{f}:{n}")
                    if re.search(r'"Property Temperature"\s*:', line) and f not in ("guard.py", "datasift.py"):
                        # the cleanup only ever plans a clear ("") from the value it found
                        if not (f == "temperature_cleanup.py" and re.search(r'"Property Temperature": (""|str\(l\["hotness"\]\))', line)):
                            hits.append(f"{f}:{n}")
                    if re.search(r"TEMPERATURE_CRM|INITIAL_TEMPERATURE", line):
                        hits.append(f"{f}:{n}")
        hot_calls = [l for l in open(os.path.join(HARRIS, "crm", "datasift.py")) if "/hotness/" in l]
        self.assertEqual(len(hot_calls), 1, "one temperature call left: the reset to 0")
        self.assertIn('{"hotness": 0}', hot_calls[0])
        self.assertEqual(hits, [])

    def test_upload_with_a_temperature_column_never_calls_the_temperature_route(self):
        run_as("probate_upload", SIFT_PROFILE="scheduled")
        crm = crm_with_prospect()
        account_lock.reset()
        row = {"Property Street Address": "100 Test St", "Property ZIP Code": "77001", "Owner First Name": "A",
               "Owner Last Name": "B", "Lists": "Harris - Probate", "Property Temperature": "5", "Prospect Score": "Hot"}
        try:
            crm.upsert_lead(row, True)
        except Exception:  # the mock does not model create_property's answers; only the calls matter
            pass
        self.assertFalse([c for c in crm.api.calls if "/hotness" in c[1]])

    def test_update_with_temperature_or_status_is_refused(self):
        for col in ("Property Temperature", "Property Status", "Status"):
            run_as("harris_grade", SIFT_PROFILE="scheduled")
            crm = crm_with_prospect()
            with self.assertRaises(guard.Blocked) as e:
                crm.update_lead(lead(1), {"Deal Grade": "Hot", col: "5"}, [])
            self.assertEqual(e.exception.kind, "native_temperature_or_status")
            self.assertEqual(crm.api.writes(), [])


class Brake(unittest.TestCase):
    def call(self, task, method, path, body, reset=False, **env):
        r = run_as(task, SIFT_PROFILE="scheduled", SIFT_ALLOW_DESTRUCTIVE="1", **env)
        if reset:
            r._temp_reset = True
        return r.on_call(method, path, {}, body)

    def test_temperature_writes_are_refused_in_every_profile(self):
        for body in ({"hotness": 5}, {"hotness": 3}, {"hotness": 0}):
            with self.assertRaises(guard.Blocked) as e:
                self.call("harris_grade", "POST", f"/api/internal/property/{lead(1)}/hotness/", body)
            self.assertEqual(e.exception.kind, "native_temperature_or_status")

    def test_property_status_writes_are_refused(self):
        for method, path, body in (("PATCH", f"/api/internal/property/{lead(1)}/", {"status": "Hot Lead"}),
                                   ("PATCH", f"/api/internal/property/{lead(1)}/", {"hotness": 5}),
                                   ("POST", f"/api/internal/property/{lead(1)}/status/", {"status": "x"}),
                                   ("POST", "/api/internal/property/", {"address": {}, "status": "new_lead"})):
            with self.assertRaises(guard.Blocked):
                self.call("anything", method, path, body)

    def test_phone_status_and_other_writes_still_pass(self):
        self.assertIsNone(self.call("phone_priority", "POST", "/api/internal/owner/o1/upsert-phones/",
                                    {"phones": [{"number": "1", "status": "CORRECT", "tags": ["Dial First"]}]}))
        self.assertIsNone(self.call("harris_grade", "PATCH", f"/api/internal/property/{lead(1)}/custom-field/update-values/",
                                    [{"field_uuid": "f", "value": "x"}]))

    def test_reset_to_zero_only_from_the_cleanup_inside_its_reset(self):
        path = f"/api/internal/property/{lead(1)}/hotness/"
        with self.assertRaises(guard.Blocked):
            self.call("temperature_cleanup", "POST", path, {"hotness": 0})          # outside the reset context
        with self.assertRaises(guard.Blocked):
            self.call("temperature_cleanup", "POST", path, {"hotness": 3}, reset=True)  # not zero
        with self.assertRaises(guard.Blocked):
            self.call("harris_grade", "POST", path, {"hotness": 0}, reset=True)     # not the cleanup task
        self.assertIsNone(self.call("temperature_cleanup", "POST", path, {"hotness": 0}, reset=True))

    def test_another_task_cannot_open_the_reset(self):
        r = run_as("harris_grade", SIFT_PROFILE="scheduled")
        with self.assertRaises(guard.Blocked):
            with r.temperature_reset():
                pass


class ProspectFields(unittest.TestCase):
    def test_grade_writes_prospect_score_equal_to_deal_grade(self):
        import harris_grade as G
        run_as("harris_grade", SIFT_PROFILE="scheduled")
        crm = crm_with_prospect()
        leads = [{"id": lead(i), "Property Street Address": f"{i} Elm St", "Property ZIP Code": "77001",
                  "Foreclosure Status": "Active", "Tags": "", "Deal Grade": "", "Deal Notes": "", "ZIP Investor Activity": "",
                  "Prospect Score": "", "Prospect Reasons": "", "Equity Percent": eq, "Estimated Value": "200000",
                  "Structure Type": "Single Family", "Living SqFt": "1500", "Bedrooms": "3", "Bathrooms": "2",
                  "Year Built": "1980", "Latitude": "", "Longitude": "", "Original Loan Amount": "", "Loan Date": ""}
                 for i, eq in ((1, "80"), (2, "30"), (3, "10"))]
        crm.read_leads = lambda name, with_facts=False: [dict(l) for l in leads]
        sent = []
        crm.update_lead = lambda lid, vals, add: sent.append((lid, dict(vals))) or []
        G.flood_zone = lambda lat, lon: ("flood zone unknown", False)
        import crm as crmpkg
        crmpkg.get_crm = lambda: crm
        tmp = tempfile.mkdtemp()
        market = {"buy_box": {"living_area_sqft_low": 500, "living_area_sqft_high": 5000, "bedrooms_count_low": 1,
                              "bedrooms_count_high": 6, "bath_count_low": 1, "property_value_low": 1, "property_value_high": 900000},
                  "zips": {"77001": {"num_investor_transactions": 10}, "77002": {"num_investor_transactions": 1},
                           "77003": {"num_investor_transactions": 5}}}
        json.dump(market, open(os.path.join(tmp, "m.json"), "w"))
        with redirect_stdout(io.StringIO()):
            G.main(os.path.join(tmp, "m.json"), os.path.join(tmp, "g.csv"), "--crm", "--commit")
        self.assertEqual(len(sent), 3)
        for _, v in sent:
            self.assertEqual(v["Prospect Score"], v["Deal Grade"])
            self.assertEqual(v["Prospect Reasons"], v["Deal Notes"])
            self.assertNotIn("Property Temperature", v)
        rows = list(csv.DictReader(open(os.path.join(tmp, "g.csv"))))
        self.assertTrue(all(r["Prospect Score"] == r["Deal Grade"] for r in rows))

    def test_probate_and_senior_rows_mirror_lead_temperature(self):
        for f in ("probate_to_datasift.py", "senior_to_datasift.py"):
            src = open(os.path.join(HARRIS, f)).read()
            self.assertIn('"Lead Temperature": temp', src, f)
            self.assertIn('"Prospect Score": temp', src, f)
        probate = open(os.path.join(HARRIS, "probate_to_datasift.py")).read()
        self.assertIn('"Temperature Notes": temp_notes[:500]', probate)
        self.assertIn('"Prospect Reasons": temp_notes[:500]', probate)
        senior = open(os.path.join(HARRIS, "senior_to_datasift.py")).read()
        expr = '(f"{temp} ({pts} pts): " + "; ".join(notes or ["no extra signal"]))[:500]'
        self.assertIn('"Temperature Notes": ' + expr, senior)
        self.assertIn('"Prospect Reasons": ' + expr, senior)
        for f in ("probate_to_datasift.py", "senior_to_datasift.py"):
            src = open(os.path.join(HARRIS, f)).read()
            self.assertNotIn("Property Temperature", src, f)

    def test_probate_rows_from_a_case(self):
        import probate_to_datasift as P
        tmp = tempfile.mkdtemp()
        cols = ("case,decedent,method,confidence,namesakes,properties,note,file_date,type,subtype,dod,applicant,applicant_street,"
                "applicant_city,applicant_state,applicant_zip,attorney,attorney_phone,decedent_street,acct,hcad_owner,site_addr,"
                "site_city,site_zip,state_class,value,sqft,year,mail_addr,mail_city,mail_state,mail_zip").split(",")
        row = dict.fromkeys(cols, "")
        row.update(case="900001", decedent="Jane Example", method="name", confidence="high", file_date="10/01/2026",
                   type="APPLICATION FOR PROBATE OF WILL", dod="01/01/2025", applicant="John Example", applicant_state="CA",
                   acct="0000000000001", hcad_owner="EXAMPLE JANE", site_addr="123 SAMPLE LN", site_city="HOUSTON",
                   site_zip="77000", state_class="A1", value="200000", sqft="1500", year="1970")
        with open(os.path.join(tmp, "m.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerow(row)
        with redirect_stdout(io.StringIO()):
            P.main(os.path.join(tmp, "m.csv"), os.path.join(tmp, "out.csv"))
        out = list(csv.DictReader(open(os.path.join(tmp, "out.csv"))))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["Prospect Score"], out[0]["Lead Temperature"])
        self.assertEqual(out[0]["Prospect Reasons"], out[0]["Temperature Notes"])
        self.assertNotIn("Property Temperature", out[0])


class Cleanup(unittest.TestCase):
    def setUp(self):
        import importlib
        import temperature_cleanup as T
        self.T = importlib.reload(T)  # other tests replace its functions with fakes

    def L(self, **kw):
        base = {"id": lead(1), "address": "1 Elm St", "list": "Harris - Foreclosure", "score": "Hot", "hotness": 5,
                "status": "", "calls": 0, "sms": 0}
        base.update(kw)
        return base

    HOT_EVENT = {"event_type": "property.hotness.increased", "author": PIPE, "timestamp": "2026-11-01 02:55:48",
                 "payload": {"property": {"hotness": [0, 5]}}}
    WARM_EVENT = {"event_type": "property.hotness.increased", "author": PIPE, "timestamp": "2026-11-01 02:55:48",
                  "payload": {"property": {"hotness": [0, 3]}}}

    def test_only_pipeline_set_uncontacted_matching_leads_qualify(self):
        v = lambda l, ev: self.T.verdict(l, ev, PIPE)
        self.assertEqual(v(self.L(), [self.HOT_EVENT]), "")
        self.assertEqual(v(self.L(score="Warm", hotness=3), [self.WARM_EVENT]), "")
        self.assertIn("0 -> current", v(self.L(score="Warm", hotness=3), [self.HOT_EVENT]))   # 0->5 but reads 3
        later = {"event_type": "property.hotness.increased", "author": PIPE, "timestamp": "2026-11-05 10:00:00",
                 "payload": {"property": {"hotness": [3, 5]}}}
        self.assertIn("0 -> current", v(self.L(), [self.WARM_EVENT, later]))                 # raised later
        self.assertIn("0 -> current", v(self.L(), [{**self.HOT_EVENT, "payload": {}}]))       # value unknown
        self.assertIn("does not match", v(self.L(score="Warm", hotness=5), [self.HOT_EVENT]))   # set by hand?
        self.assertIn("does not match", v(self.L(score="Not Viable", hotness=3), [self.WARM_EVENT]))
        self.assertIn("not 3 or 5", v(self.L(hotness=4), [self.HOT_EVENT]))
        self.assertIn("status", v(self.L(status="Cold Lead"), [self.HOT_EVENT]))
        self.assertIn("attempts", v(self.L(calls=1), [self.HOT_EVENT]))
        self.assertIn("attempts", v(self.L(sms=2), [self.HOT_EVENT]))
        for et in ("owner.call.noanswer", "property.message.added", "owner.phone.status.updated", "task.created",
                   "property.status.updated", "owner.sms.received"):
            self.assertIn("contact", v(self.L(), [self.HOT_EVENT, {"event_type": et}]), et)
        self.assertIn("no record", v(self.L(), []))
        self.assertIn("someone else", v(self.L(), [self.HOT_EVENT, {"event_type": "property.hotness.increased",
                                                                    "author": "caller@example.com"}]))

    def test_a_lead_also_on_senior_equity_is_never_cleaned(self):
        self.assertIn("Senior Equity", self.T.verdict(self.L(senior=True), [self.HOT_EVENT], PIPE))

    def test_read_takes_senior_from_the_records_own_lists(self):
        class Api:
            def call(s, path, *a, **k):
                i = int(path.rstrip("/").split("/")[-1][-1])
                return {"hotness": 5, "lists": [{"title": "Harris - Foreclosure"}] + ([{"title": "Harris - Senior Equity"}] if i == 2 else [])}

        class C:
            api = Api()
            fields = {}
            seen = []

            def list_assignments(self, name, fields):
                self.seen.append(name)
                return [{"id": lead(i), "Property Street Address": f"{i} Elm", fields[0]: "Hot"} for i in (1, 2)] \
                    if name == "Harris - Foreclosure" else []
        c = C()
        rows = self.T.read(c)
        self.assertEqual([r["senior"] for r in rows], [False, True])
        self.assertNotIn("Harris - Senior Equity", c.seen, "the Senior list itself is never read")

    def test_history_merges_two_reads(self):
        reads = iter([[{"uuid": "a", "event_type": "x"}], [{"uuid": "a", "event_type": "x"},
                                                           {"uuid": "b", "event_type": "property.hotness.increased"}]])

        class C:
            lead_activity = staticmethod(lambda lid: ({}, next(reads)))
        self.assertEqual([e["uuid"] for e in self.T.history(C, "id")], ["a", "b"])

    def test_cleanup_respects_the_brakes(self):
        """60 qualifying leads: a manual run is a simulation (no write), live needs --allow-destructive, and the
        live run resets through the one allowed route with the value 0."""
        def fake(crm):
            T = self.T
            T.read = lambda c: [self.L(id=lead(i), address=f"{i} Elm St") for i in range(60)]
            crm.lead_activity = lambda lid: ({}, [self.HOT_EVENT])
            T.pipeline_user = lambda api: PIPE
            import crm as crmpkg
            crmpkg.get_crm = lambda: crm
            T.get_crm = lambda: crm
        # 1. manual, no SIFT_MODE: dry run, nothing written, summary with 5 examples
        out = []
        r = run_as("temperature_cleanup")
        r.out = out.append
        crm = crm_with_prospect()
        fake(crm)
        with redirect_stdout(io.StringIO()), self.assertRaises(guard.DryRunDone):
            self.T.main("--commit")
        self.assertEqual(crm.api.writes(), [])
        self.assertIn("would change 60 leads", out[-1])
        self.assertIn("Property Temperature: (cleared) x60", out[-1])
        self.assertIn("A real run would be BLOCKED as it stands", out[-1], "the simulation says it needs authorization")
        self.assertEqual(len([l for l in out[-1].split("examples:")[1].splitlines() if l.startswith("  ")]), 5)
        # 2. live without authorization: blocked, nothing written
        run_as("temperature_cleanup", SIFT_MODE="live")
        crm = crm_with_prospect()
        fake(crm)
        with redirect_stdout(io.StringIO()), self.assertRaises(guard.Blocked):
            self.T.main("--commit")
        self.assertEqual(crm.api.writes(), [])
        # 3. live and authorized: 60 resets to 0, each read back
        run_as("temperature_cleanup", SIFT_MODE="live", SIFT_ALLOW_DESTRUCTIVE="1")
        crm = crm_with_prospect()
        fake(crm)

        class ZeroAfterReset(MockApi):  # the live record holds its temperature until the reset, then reads 0
            def _send(s, path, method, body, _retry, headers):
                s.calls.append((method, path, headers))
                if method == "GET":
                    done = any(m == "POST" and p == path.rstrip("/") + "/hotness/" for m, p, _ in s.calls)
                    return {"hotness": 0 if done else self.L()["hotness"]}
                return {}
        crm.api = ZeroAfterReset()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.T.main("--commit"), 0)
        w = crm.api.writes()
        self.assertEqual(len(w), 60)
        self.assertTrue(all(p.endswith("/hotness/") for _, p in w))

    def test_lead_touched_after_the_scan_is_skipped(self):
        """Re-checked at the moment of writing: a lead that got a status after the scan keeps its temperature."""
        run_as("temperature_cleanup", SIFT_MODE="live", SIFT_ALLOW_DESTRUCTIVE="1")
        crm = crm_with_prospect()
        self.T.read = lambda c: [self.L(id=lead(i), address=f"{i} Elm St") for i in range(2)]
        crm.lead_activity = lambda lid: ({}, [self.HOT_EVENT])
        self.T.pipeline_user = lambda api: PIPE
        self.T.get_crm = lambda: crm
        first = lead(0)

        class Touched(MockApi):
            def _send(s, path, method, body, _retry, headers):
                s.calls.append((method, path, headers))
                if method == "GET":
                    if first in path:
                        return {"hotness": 5, "status": {"title": "Warm Lead"}}
                    done = any(m == "POST" and p == path.rstrip("/") + "/hotness/" for m, p, _ in s.calls)
                    return {"hotness": 0 if done else 5}
                return {}
        crm.api = Touched()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(self.T.main("--commit"), 0)
        w = crm.api.writes()
        self.assertEqual(len(w), 1)
        self.assertNotIn(first, w[0][1])
        self.assertIn("SKIP 0 Elm St", buf.getvalue())

    def test_live_stops_after_a_first_lead_that_does_not_read_back(self):
        run_as("temperature_cleanup", SIFT_MODE="live", SIFT_ALLOW_DESTRUCTIVE="1")
        crm = crm_with_prospect()
        self.T.read = lambda c: [self.L(id=lead(i)) for i in range(20)]
        crm.lead_activity = lambda lid: ({}, [self.HOT_EVENT])
        self.T.pipeline_user = lambda api: PIPE
        self.T.get_crm = lambda: crm

        class StaysHot(MockApi):
            def _send(s, path, method, body, _retry, headers):
                s.calls.append((method, path, headers))
                return {"hotness": 5} if method == "GET" else {}
        crm.api = StaysHot()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.T.main("--commit"), 1)
        self.assertEqual(len(crm.api.writes()), 1, "one lead first; it did not read back, so nothing else")


if __name__ == "__main__":
    unittest.main(verbosity=2)
