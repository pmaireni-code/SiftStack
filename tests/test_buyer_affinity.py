"""buyer_affinity + buyers_sheet: grouping, wholesaler weight, and the Google Sheet rules. No network."""
import os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "harris"))
import buyer_affinity as BA  # noqa: E402
import buyers_sheet as BS  # noqa: E402


class FakeSheets:
    """In-memory stand-in for SheetsClient (same methods)."""

    def __init__(self, tabs=None):
        self.data = {k: [list(r) for r in v] for k, v in (tabs or {}).items()}

    def tabs(self):
        return list(self.data)

    def add_tab(self, t):
        self.data[t] = []

    def read(self, t):
        return [list(r) for r in self.data[t]]

    def update(self, cells):
        for t, r, c, v in cells:
            rows = self.data[t]
            while len(rows) < r:
                rows.append([])
            row = rows[r - 1]
            while len(row) < c:
                row.append("")
            row[c - 1] = str(v)

    def append(self, t, rows):
        self.data[t] += [[str(x) for x in r] for r in rows]


ID = lambda d: {str(d.get("ID") or "")} - {""}


class SheetSync(unittest.TestCase):
    def test_creates_tab_header_and_user_columns(self):
        s = FakeSheets()
        r = BS.sync(s, "Compradores", ["ID", "Buyer"], [{"ID": "a", "Buyer": "X LLC"}], ID, BS.USER_COLUMNS)
        self.assertEqual(s.data["Compradores"][0], ["ID", "Buyer", "Status", "Last contact", "Notes"])
        self.assertEqual(s.data["Compradores"][1][:2], ["a", "X LLC"])
        self.assertEqual(r["added"], 1)

    def test_no_duplicates_and_user_cells_kept(self):
        s = FakeSheets({"T": [["ID", "Buyer", "Status", "Notes"], ["a", "Old name", "Called", "likes 3/2"]]})
        r = BS.sync(s, "T", ["ID", "Buyer"], [{"ID": "a", "Buyer": "New name"}, {"ID": "b", "Buyer": "Y"}], ID)
        self.assertEqual(s.data["T"][1], ["a", "New name", "Called", "likes 3/2"])
        self.assertEqual([row[0] for row in s.data["T"][1:]], ["a", "b"])
        self.assertEqual((r["added"], r["updated"]), (1, 1))
        r = BS.sync(s, "T", ["ID", "Buyer"], [{"ID": "a", "Buyer": "New name"}], ID)  # second run: nothing new
        self.assertEqual((r["added"], r["updated"], r["unchanged"]), (0, 0, 1))
        self.assertEqual(len(s.data["T"]), 3)

    def test_never_deletes_rows_missing_from_this_run(self):
        s = FakeSheets({"T": [["ID", "Buyer"], ["a", "A"], ["b", "B"]]})
        BS.sync(s, "T", ["ID", "Buyer"], [{"ID": "c", "Buyer": "C"}], ID)
        self.assertEqual([row[0] for row in s.data["T"][1:]], ["a", "b", "c"])

    def test_owned_column_added_at_the_right_end(self):
        s = FakeSheets({"T": [["ID", "Notes"], ["a", "mine"]]})
        BS.sync(s, "T", ["ID", "Buyer"], [{"ID": "a", "Buyer": "A"}], ID)
        self.assertEqual(s.data["T"], [["ID", "Notes", "Buyer"], ["a", "mine", "A"]])

    def test_same_buyer_found_by_company_key(self):
        s = FakeSheets({"Compradores": [["Buyer ID", "Company keys", "Buyer", "Notes"], ["B:GIYEF", "GIYEF", "GIYEF LLC", "x"]]})
        row = {"Buyer ID": "B:GROWTH 2026", "Company keys": "GIYEF | GROWTH 2026", "Buyer": "Giovanni Escobar (2 companies)"}
        r = BS.sync(s, "Compradores", ["Buyer ID", "Company keys", "Buyer"], [row], BA.buyer_keys)
        self.assertEqual(r["added"], 0)
        self.assertEqual(s.data["Compradores"][1], ["B:GROWTH 2026", "GIYEF | GROWTH 2026", "Giovanni Escobar (2 companies)", "x"])

    def test_values_go_raw(self):
        self.assertEqual(BS.cell(3.0), 3)
        self.assertTrue(BS.same("1,224", 1224))


class Grouping(unittest.TestCase):
    def test_person_signature_ignores_order_and_initials(self):
        self.assertEqual(BA.person_sig("ESCOBAR GIOVANNI A"), BA.person_sig("Giovanni Escobar"))
        self.assertIsNone(BA.person_sig("Madonna"))
        self.assertTrue(BA.sig_match(BA.person_sig("Giovanni Escobar"), BA.person_sig("Giovanni Luis Escobar")))

    def test_shared_manager_groups_companies(self):
        nodes = {"GROWTH 2026": {"people": {"Giovanni Escobar"}, "entities": {"GROWTH 2026"}},
                 "GIYEF": {"people": {"GIOVANNI ESCOBAR"}, "entities": {"GIYEF"}},
                 "KMN REALTY": {"people": {"Nsania Mukana"}, "entities": {"KMN REALTY"}}}
        groups = {frozenset(g): by for g, by in BA.group_nodes(nodes)}
        self.assertIn(frozenset({"GROWTH 2026", "GIYEF"}), groups)
        self.assertEqual(groups[frozenset({"GROWTH 2026", "GIYEF"})], "Giovanni Escobar")
        self.assertIn(frozenset({"KMN REALTY"}), groups)

    def test_parent_company_groups(self):
        nodes = {"FIXER FLIPPER TX": {"people": set(), "entities": {"FIXER FLIPPER TX", "FIXER FLIPPER USA"}},
                 "FIXER FLIPPER USA": {"people": set(), "entities": {"FIXER FLIPPER USA"}}}
        self.assertEqual(len(BA.group_nodes(nodes)), 1)


class Scoring(unittest.TestCase):
    SUBJ = {"value": 200000, "sqft": 1200, "beds": 3, "year": 1978}

    def buys(self, resold):
        return [{"date": date(2026, 10, 1), "distance": 0.5, "value": 200000, "sqft": 1200, "beds": 3, "year": 1978,
                 "resold_days": resold, "cash": True}] * 3

    def test_wholesaler_weighs_less_than_end_buyer(self):
        today = date(2026, 11, 20)
        w, pw = BA.score(self.buys(30), self.SUBJ, today, 24, 1.5, BA.behavior(self.buys(30)))
        h, ph = BA.score(self.buys(None), self.SUBJ, today, 24, 1.5, BA.behavior(self.buys(None)))
        self.assertEqual(BA.behavior(self.buys(30)), "Wholesaler")
        self.assertEqual((pw["type_weight"], ph["type_weight"]), (0.75, 1.0))
        self.assertAlmostEqual(w, round(h * 0.75, 1), delta=0.11)


class BuildBuyers(unittest.TestCase):
    def test_grouped_buyer_adds_purchases_and_flags_review(self):
        def b(key, name, addr):
            return {"key": key, "buyer": name, "date": date(2026, 9, 1), "cash": True, "resold_days": None,
                    "address": addr, "distance": 0.4, "value": 190000, "sqft": 1200, "beds": 3, "year": 1978}
        buys = [b("GROWTH 2026", "GROWTH 2026 LLC", "1 A St"), b("GIYEF", "GIYEF LLC", "2 B St"),
                b("GIYEF", "GIYEF LLC", "3 C St"), b("TOWER INVESTMENT GROUP", "TOWER INVESTMENT GROUP LLC", "4 D St"),
                b("BUXLEY INTERESTS", "BUXLEY INTERESTS LLC", "5 E St"), b("SOLO PERSON", "SOLO PERSON", "6 F St")]
        off = lambda n: {"officerInfo": [{"AGNT_NM": n, "AGNT_TITL_TX": "MANAGER"}], "registeredAgentName": "RA",
                         "registeredOfficeAddressStreet": "1 MAIN", "registeredOfficeAddressCity": "HOUSTON",
                         "registeredOfficeAddressState": "TX", "registeredOfficeAddressZip": "77001"}
        lookups = {"GROWTH 2026 LLC": ("one", off("GIOVANNI ESCOBAR"), []), "GIYEF LLC": ("one", off("Giovanni Escobar"), []),
                   "TOWER INVESTMENT GROUP LLC": ("several", None, ["TOWER INVESTMENT GROUP LLC", "TOWER INVESTMENT GROUP, LLC"]),
                   "BUXLEY INTERESTS LLC": ("one", {"officerInfo": []}, [])}
        hcad = {"GIYEF": {"addr": "14415 SPANISH RIVER LN, CYPRESS, TX 77429", "n": 2}}
        out = BA.build_buyers(buys, {"value": 197000, "sqft": 1224, "beds": 3, "year": 1978}, date(2026, 11, 20), 24, 1.5, lookups, hcad)
        by = {r["buyer"]: r for r in out}
        g = by["Giovanni Escobar (2 companies)"]
        self.assertEqual(len(g["buys"]), 3)
        self.assertEqual(sorted(g["companies"]), ["GIYEF LLC", "GROWTH 2026 LLC"])
        self.assertIn("SPANISH RIVER", g["hcad_mail"])
        self.assertIn("RA, 1 MAIN, HOUSTON, TX 77001", g["agent"])
        self.assertIn("several Texas entities", by["TOWER INVESTMENT GROUP LLC"]["review"])
        self.assertIn("no people", by["BUXLEY INTERESTS LLC"]["review"])
        self.assertNotIn("SOLO PERSON", by, "a person with one purchase is not an investor buyer")


if __name__ == "__main__":
    unittest.main()
