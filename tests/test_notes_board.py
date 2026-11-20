"""Notes go to the record's message board (2026-11-20: /add-notes/ answers 404). No network."""
import os, sys, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [os.path.join(ROOT, "src", "harris"), os.path.join(ROOT, "src"), os.path.dirname(__file__)]
from test_harris_guards import make_crm, run_as  # noqa: E402


class Board:
    def __init__(self):
        self.msgs, self.calls = [], []

    def call(self, path, method="GET", body=None, _retry=True, headers=None):
        self.calls.append((method, path))
        if path.endswith("/message/") and method == "POST":
            self.msgs.append({"message": body["message"]})
            return {"uuid": "m1"}
        if "/message/" in path:
            return {"results": list(self.msgs)}
        return {"notes": "old note NOTICE-1"}


class Notes(unittest.TestCase):
    def test_add_note_uses_the_board_and_reads_back(self):
        run_as("harris_upload", SIFT_PROFILE="scheduled")
        crm = make_crm()
        crm.api = Board()
        self.assertTrue(crm.add_note("p1", "Entity lookup (Texas Comptroller, 2026-11-20): X"))
        self.assertIn(("POST", "/api/internal/property/p1/message/"), crm.api.calls)
        self.assertFalse(any("add-notes" in p for _, p in crm.api.calls))

    def test_notes_text_merges_board_and_legacy_field(self):
        run_as("harris_upload", SIFT_PROFILE="scheduled")
        crm = make_crm()
        crm.api = Board()
        crm.api.msgs.append({"message": "board note"})
        t = crm.notes_text("p1")
        self.assertIn("old note NOTICE-1", t)
        self.assertIn("board note", t)


if __name__ == "__main__":
    unittest.main()
