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
        s = FakeSheets({"Compradores": [["Buyer ID", "Company keys", "Buyer", "Notes"], ["B:BETA FLIPS", "BETA FLIPS", "BETA FLIPS LLC", "x"]]})
        row = {"Buyer ID": "B:ALPHA HOMES", "Company keys": "BETA FLIPS | ALPHA HOMES", "Buyer": "John Doe (2 companies)"}
        r = BS.sync(s, "Compradores", ["Buyer ID", "Company keys", "Buyer"], [row], BA.buyer_keys)
        self.assertEqual(r["added"], 0)
        self.assertEqual(s.data["Compradores"][1], ["B:ALPHA HOMES", "BETA FLIPS | ALPHA HOMES", "John Doe (2 companies)", "x"])

    def test_values_go_raw(self):
        self.assertEqual(BS.cell(3.0), 3)
        self.assertTrue(BS.same("1,224", 1224))


class Grouping(unittest.TestCase):
    def test_person_signature_ignores_order_and_initials(self):
        self.assertEqual(BA.person_sig("DOE JOHN A"), BA.person_sig("John Doe"))
        self.assertIsNone(BA.person_sig("Cher"))
        self.assertTrue(BA.sig_match(BA.person_sig("John Doe"), BA.person_sig("John Paul Doe")))

    def test_shared_manager_groups_companies(self):
        nodes = {"ALPHA HOMES": {"people": {"John Doe"}, "entities": {"ALPHA HOMES"}},
                 "BETA FLIPS": {"people": {"JOHN DOE"}, "entities": {"BETA FLIPS"}},
                 "GAMMA REALTY": {"people": {"Mary Major"}, "entities": {"GAMMA REALTY"}}}
        groups = {frozenset(g): by for g, by in BA.group_nodes(nodes)}
        self.assertIn(frozenset({"ALPHA HOMES", "BETA FLIPS"}), groups)
        self.assertEqual(groups[frozenset({"ALPHA HOMES", "BETA FLIPS"})], "John Doe")
        self.assertIn(frozenset({"GAMMA REALTY"}), groups)

    def test_group_named_after_a_person(self):
        names = ["The Richard Moe Section", "Richard Moe", "Richard H Moe 2006 Trust", "Richard Moe"]
        self.assertEqual(BA.link_name(names), "Richard Moe")
        self.assertEqual(BA.link_name(["ROE JANE", "Jane Roe"], {"ROE JANE"}),
                         "Jane Roe")

    def test_person_purchase_joins_company_only_with_a_zip(self):
        llc = {"people": {"JAMES A SMITH"}, "entities": {"DELTA BUILD"}, "self": set(), "zips": {"77036"}}
        far = {"people": {"SMITH JAMES A"}, "entities": set(), "self": {"SMITH JAMES A"}, "zips": {"77099"}}
        near = dict(far, zips={"77036"})
        self.assertEqual(len(BA.group_nodes({"DELTA BUILD": llc, "SMITH JAMES A": far})), 2)
        self.assertEqual(len(BA.group_nodes({"DELTA BUILD": llc, "SMITH JAMES A": near})), 1)

    def test_parent_company_or_trust_alone_does_not_group(self):
        nodes = {"OMEGA TX": {"people": {"OMEGA USA LLC"}, "self": set(), "zips": {"78731"}},
                 "OMEGA FL": {"people": {"OMEGA USA LLC", "The Richard Moe Section"}, "self": set(), "zips": {"78731"}},
                 "OMEGA GA": {"people": {"Richard H Moe 2006 Trust", "The Richard Moe Section"}, "self": set(), "zips": set()}}
        self.assertEqual(len(BA.group_nodes(nodes)), 3)

    def test_shared_address_only_is_flagged_not_grouped(self):
        groups = [({"ALPHA HOMES"}, ""), ({"ZETA INVESTMENT GROUP"}, ""), ({"ETA INTERESTS"}, "")]
        names = {"ALPHA HOMES": "ALPHA HOMES LLC", "ZETA INVESTMENT GROUP": "ZETA INVESTMENT GROUP LLC",
                 "ETA INTERESTS": "ETA INTERESTS LLC"}
        lookups = {"ALPHA HOMES LLC": ("one", {"officerInfo": [{"AGNT_NM": "OMEGA USA LLC"}]}, []),
                   "ETA INTERESTS LLC": ("one", {"officerInfo": [{"AGNT_NM": "OMEGA USA LLC"}]}, [])}
        hcad = {"ALPHA HOMES": {"addr": "5900 TEST DRIVE SUITE 100, AUSTIN, TX 78731"},
                "ZETA INVESTMENT GROUP": {"addr": "5900 TEST DR STE 100, AUSTIN, TX 78731"}}
        notes = BA.near_misses(groups, names, lookups, hcad)
        self.assertIn("same mailing address as ZETA INVESTMENT GROUP LLC", " ".join(notes[0]))
        self.assertIn("same parent company or trust as ETA INTERESTS LLC", " ".join(notes[0]))
        self.assertIn("not grouped", notes[1][0])
        lookups["ZETA INVESTMENT GROUP LLC"] = ("one", {"registeredAgentName": "AGENTS INC.", "registeredOfficeAddressStreet":
                                                         "5900 TEST DR STE 100", "registeredOfficeAddressCity": "AUSTIN",
                                                         "registeredOfficeAddressState": "TX", "registeredOfficeAddressZip": "78731"}, [])
        notes = BA.near_misses(groups, names, lookups, hcad)  # an agent service's office is no hint
        self.assertNotIn("same mailing address", " ".join(notes.get(0, [])))


class Purchases(unittest.TestCase):
    def test_one_purchase_per_buyer_per_house(self):
        hist = [{"sale_date": d, "buyer_name": "SIGMA HOLDINGS LLC", "seller_name": "X"} for d in
                ("2025-10-04", "2025-10-07", "2026-03-02")]
        out = BA.purchases({"sale_history": hist}, date(2024, 11, 20), {"address": "1 Test Rd"})
        self.assertEqual([b["date"] for b in out], [date(2026, 3, 2)])

    def test_address_variants(self):
        self.assertEqual(BA.uniq_addrs(["A. AGENT, 100 COUNTY RD. 1, TOWN", "A AGENT, 100 COUNTY ROAD 1, TOWN"]),
                         ["A. AGENT, 100 COUNTY RD. 1, TOWN"])


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
        buys = [b("ALPHA HOMES", "ALPHA HOMES LLC", "1 A St"), b("BETA FLIPS", "BETA FLIPS LLC", "2 B St"),
                b("BETA FLIPS", "BETA FLIPS LLC", "3 C St"), b("ZETA INVESTMENT GROUP", "ZETA INVESTMENT GROUP LLC", "4 D St"),
                b("ETA INTERESTS", "ETA INTERESTS LLC", "5 E St"), b("SOLO PERSON", "SOLO PERSON", "6 F St")]
        off = lambda n: {"officerInfo": [{"AGNT_NM": n, "AGNT_TITL_TX": "MANAGER"}], "registeredAgentName": "RA",
                         "registeredOfficeAddressStreet": "1 MAIN", "registeredOfficeAddressCity": "HOUSTON",
                         "registeredOfficeAddressState": "TX", "registeredOfficeAddressZip": "77001"}
        lookups = {"ALPHA HOMES LLC": ("one", off("JOHN DOE"), []), "BETA FLIPS LLC": ("one", off("John Doe"), []),
                   "ZETA INVESTMENT GROUP LLC": ("several", None, ["ZETA INVESTMENT GROUP LLC", "ZETA INVESTMENT GROUP, LLC"]),
                   "ETA INTERESTS LLC": ("one", {"officerInfo": []}, [])}
        hcad = {"BETA FLIPS": {"addr": "100 TEST LN, HOUSTON, TX 77001", "n": 2}}
        out = BA.build_buyers(buys, {"value": 197000, "sqft": 1224, "beds": 3, "year": 1978}, date(2026, 11, 20), 24, 1.5, lookups, hcad)
        by = {r["buyer"]: r for r in out}
        g = by["John Doe (2 companies)"]
        self.assertEqual(len(g["buys"]), 3)
        self.assertEqual(sorted(g["companies"]), ["ALPHA HOMES LLC", "BETA FLIPS LLC"])
        self.assertIn("TEST LN", g["hcad_mail"])
        self.assertIn("RA, 1 MAIN, HOUSTON, TX 77001", g["agent"])
        self.assertIn("several Texas entities", by["ZETA INVESTMENT GROUP LLC"]["review"])
        self.assertIn("no people", by["ETA INTERESTS LLC"]["review"])
        self.assertNotIn("SOLO PERSON", by, "a person with one purchase is not an investor buyer")


if __name__ == "__main__":
    unittest.main()
