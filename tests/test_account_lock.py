"""Account lock (src/account_lock.py): writes only to the declared account; reads always pass. Zero network.

The connected account comes from the session token's `account` claim; tests build tokens for the Harris
account's fingerprint by patching FINGERPRINTS with a known test id.

Run: python3 tests/test_account_lock.py   (or pytest)
"""
import base64, json, os, subprocess, sys, unittest, urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [os.path.join(ROOT, "src", "harris"), os.path.join(ROOT, "src")]

import account_lock as L  # noqa: E402
import datasift_api_upload as U  # noqa: E402

HARRIS_TEST_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "99999999-8888-7777-6666-555555555555"


def token(account):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'HS256'})}.{b({'account': account, 'email': 'x'})}.sig"


class Resp:
    def __init__(self, body="{}"):
        self.body = body.encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Lock(unittest.TestCase):
    def setUp(self):
        L.reset()
        self.real_fp = dict(L.FINGERPRINTS)
        L.FINGERPRINTS["harris"] = L.fingerprint(HARRIS_TEST_ID)
        self.sent = []
        self.real_open = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=0: self.sent.append((req.get_method(), req.full_url)) or Resp()

    def tearDown(self):
        urllib.request.urlopen = self.real_open
        L.FINGERPRINTS.clear()
        L.FINGERPRINTS.update(self.real_fp)
        L.reset()

    def api(self, account):
        a = U.Api.__new__(U.Api)
        a.token, a.minted = token(account), 9e18
        return a

    def writes(self):
        return [s for s in self.sent if s[0] != "GET" and "x" not in s]

    def test_wrong_account_is_refused_without_writing(self):
        L.declare("harris")
        with self.assertRaises(L.AccountRefused) as e:
            self.api(OTHER_ID).call("/api/internal/property/", "POST", {"address": {}})
        self.assertEqual(e.exception.kind, "wrong_account")
        self.assertEqual(self.sent, [], "nothing reached DataSift")

    def test_right_account_writes(self):
        L.declare("harris")
        a = self.api(HARRIS_TEST_ID)
        a.call("/api/internal/property/abc/", "PATCH", {"x": 1})
        a.call("/api/internal/property/abc/add-notes/", "POST", {"notes": "n"})
        self.assertEqual([m for m, _ in self.sent], ["PATCH", "POST"])

    def test_account_claim_as_object(self):
        L.declare("harris")
        self.api({"uuid": HARRIS_TEST_ID}).call("/api/internal/property/abc/", "PATCH", {})
        self.assertEqual(len(self.sent), 1)

    def test_undeclared_program_may_not_write(self):
        with self.assertRaises(L.AccountRefused) as e:
            self.api(HARRIS_TEST_ID).call("/api/internal/list/", "POST", {"title": "t"})
        self.assertEqual(e.exception.kind, "undeclared_account")
        self.assertEqual(self.sent, [])

    def test_reads_always_pass(self):
        a = self.api(OTHER_ID)  # wrong account and nothing declared
        a.call("/api/internal/property/abc/")
        a.call("/api/internal/property/", "POST", {"limit": 1}, headers={"x-http-method-override": "GET"})
        self.assertEqual([m for m, _ in self.sent], ["GET", "POST"])

    def test_unknown_account_name_is_refused(self):
        with self.assertRaises(L.AccountRefused) as e:
            L.declare("knox")
        self.assertEqual(e.exception.kind, "unknown_account")
        self.assertEqual(set(self.real_fp), {"harris"}, "only the Harris fingerprint is kept")

    def test_a_per_record_except_cannot_swallow_it(self):
        L.declare("harris")
        a, written = self.api(OTHER_ID), 0
        with self.assertRaises(L.AccountRefused):
            for _ in range(3):
                try:
                    a.call("/api/internal/property/abc/", "PATCH", {})
                    written += 1
                except Exception:  # what the upload scripts do per record
                    pass
        self.assertEqual((written, self.sent), (0, []))


class HarrisPipeline(unittest.TestCase):
    """The Harris connector declares its account, so the daily writes pass exactly as before."""

    def test_get_crm_declares_harris_and_a_harris_write_passes(self):
        L.reset()
        import crm
        from crm import datasift
        real_init = datasift.DataSiftCRM.__init__
        datasift.DataSiftCRM.__init__ = lambda self: None
        try:
            crm.get_crm()
        finally:
            datasift.DataSiftCRM.__init__ = real_init
        self.assertEqual(L.declared(), "harris")
        L.reset()

    def test_real_harris_fingerprint_is_the_only_one(self):
        self.assertEqual(list(L.FINGERPRINTS), ["harris"])
        self.assertEqual(len(L.FINGERPRINTS["harris"]), 64)


class Standalone(unittest.TestCase):
    def test_datasift_api_upload_alone_cannot_write(self):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import urllib.request, datasift_api_upload as U, account_lock as L\n"
                "urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network'))\n"
                "a = U.Api.__new__(U.Api); a.token, a.minted = %r, 9e18\n"
                "a.call('/api/internal/property/', 'POST', {'owner': {}})\n") % (os.path.join(ROOT, "src"), token(HARRIS_TEST_ID))
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("declared no DataSift account", p.stderr)


class Archived(unittest.TestCase):
    MODULES = ("audit_tags_lists", "dispo_flip_buyers", "ftm_deep_prospect_push", "obituary_crm_push",
               "probate_audit_restore", "probate_pr_note", "repair_pr_owners", "siftmap_pull")

    def test_knox_modules_are_archived_and_refuse(self):
        for m in self.MODULES:
            self.assertFalse(os.path.exists(os.path.join(ROOT, "src", m + ".py")), m)
            p = subprocess.run([sys.executable, os.path.join(ROOT, "archive", "knox", m + ".py"), "--commit"],
                               capture_output=True, text=True, env={**os.environ, "DATASIFT_EMAIL": "", "DATASIFT_PASSWORD": ""})
            self.assertEqual(p.returncode, 2, m)
            self.assertIn("REFUSED", p.stdout, m)

    def test_senior_sweep_search_client_is_read_only(self):
        import siftmap_read
        m = siftmap_read.Map(None)
        with self.assertRaises(PermissionError):
            m._post("/properties/add-properties-by-query/", {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
