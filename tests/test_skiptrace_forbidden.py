"""Skip trace over the API is forbidden; strip_bulk_lists is archived. Zero network.

Run: python3 tests/test_skiptrace_forbidden.py   (or pytest)
"""
import io, os, subprocess, sys, unittest, urllib.request
from contextlib import redirect_stdout

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [os.path.join(ROOT, "src", "harris"), os.path.join(ROOT, "src")]

import datasift_api_upload as U  # noqa: E402
import datasift_skiptrace as S  # noqa: E402
from crm import guard  # noqa: E402


class NoNetwork(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.real = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=0: self.sent.append(req) or (_ for _ in ()).throw(AssertionError("network"))

    def tearDown(self):
        urllib.request.urlopen = self.real

    def api(self):
        a = U.Api.__new__(U.Api)
        a.token, a.minted = "t", 9e18
        return a

    def test_api_refuses_every_way_of_starting_a_skip_trace(self):
        for method, path in (("POST", "/api/internal/property/skip-trace/"), ("post", "/api/internal/property/skip-trace"),
                             ("PUT", "/api/internal/property/skip-trace/?x=1"), ("PATCH", "/api/internal/property/skip-trace/")):
            with self.assertRaises(U.SkipTraceForbidden):
                self.api().call(path, method, {"properties": ["one"]})
        self.assertEqual(self.sent, [], "nothing reached the network")

    def test_reading_the_stats_is_not_a_skip_trace(self):
        self.assertFalse(U.is_skiptrace_run("GET", "/api/internal/activity/skiptrace/stats/"))
        self.assertFalse(U.is_skiptrace_run("GET", "/api/internal/property/skip-trace/"))

    def test_module_run_refuses_without_calling(self):
        with self.assertRaises(U.SkipTraceForbidden):
            S.run(self.api(), confirm_unlimited=False)
        sys.argv = ["datasift_skiptrace.py", "--run"]
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(S.main(), 2)
        self.assertIn("REFUSED", out.getvalue())
        self.assertEqual(self.sent, [])

    def test_guard_blocks_it_in_every_profile_even_authorized(self):
        for env in ({"SIFT_PROFILE": "scheduled", "SIFT_ALLOW_DESTRUCTIVE": "1", "SIFT_MODE": "live"}, {}):
            for k in ("SIFT_PROFILE", "SIFT_ALLOW_DESTRUCTIVE", "SIFT_MODE"):
                os.environ.pop(k, None)
            os.environ.update(env)
            r = guard.reset(guard.Run(task="anything", out=lambda *_: None))
            with self.assertRaises(guard.Blocked) as e:
                r.on_call("POST", "/api/internal/property/skip-trace/", {})
            self.assertEqual(e.exception.kind, "skiptrace_forbidden")
        for k in ("SIFT_PROFILE", "SIFT_ALLOW_DESTRUCTIVE", "SIFT_MODE"):
            os.environ.pop(k, None)


class Archived(unittest.TestCase):
    def test_strip_bulk_lists_refuses_to_run(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "archive", "strip_bulk_lists", "strip_bulk_lists.py"),
                            "--commit"], capture_output=True, text=True, env={**os.environ, "DATASIFT_EMAIL": "", "DATASIFT_PASSWORD": ""})
        self.assertEqual(p.returncode, 2)
        self.assertIn("REFUSED", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "src", "strip_bulk_lists.py")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
