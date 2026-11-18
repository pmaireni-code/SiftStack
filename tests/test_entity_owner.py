"""entity_owner: business-owned leads -> person behind the company in the notes (operator, 2026-11-18). No network."""
import os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "harris"))
import entity_owner as E  # noqa: E402
import skiptrace_queue as Q  # noqa: E402

DAY = date(2026, 11, 18)


def lead(company=None, first=None, last=None, mail_zip="77522-0062", phones=(), tags=("Skip Trace Batch 1",)):
    return {"owner": {"company": company, "first_name": first, "last_name": last, "phones": list(phones),
                      "address": {"postal_code": mail_zip}},
            "tags": [{"name": t} for t in tags], "lists": [{"title": "Harris - Foreclosure"}], "status": None,
            "structure_type": "Single Family Residential", "estimate_value": 200000, "equity_percent": 60,
            "address": {"street": "1 Elm St"}}


REC = {"name": "REI TRADING POST, LLC", "rightToTransactTX": "ACTIVE", "mailingAddressStreet": "PO BOX 62",
       "mailingAddressCity": "BAYTOWN", "mailingAddressZip": "77522", "registeredAgentName": "LEGALINC",
       "officerInfo": [{"AGNT_NM": "LUIS VENEGAS", "AGNT_TITL_TX": "MANAGING M", "CITY_NM": "BAYTOWN", "ST_CD": "TX",
                        "AD_ZP": "77522"}]}


class Detect(unittest.TestCase):
    def test_entities(self):
        self.assertTrue(E.is_entity(lead(company="Rei Trading Post Llc")))
        self.assertTrue(E.is_entity(lead(first="Acquisition Group Goldstone")), "entity written in the person fields")
        self.assertFalse(E.is_entity(lead(first="Maria", last="Lopez")))

    def test_queue_routes_entities_out(self):
        self.assertIn("company", Q.why_not(lead(company="Rei Trading Post Llc", tags=())))
        self.assertEqual(Q.why_not(lead(company="Rei Trading Post Llc", tags=()), entities_too=True), "")
        self.assertEqual(Q.why_not(lead(first="Maria", last="Lopez", tags=())), "")

    def test_norm_and_kind(self):
        self.assertEqual(E.norm("Rei Trading Post Llc"), E.norm("REI TRADING POST, LLC"))
        self.assertEqual(E.kind_of("GREEN VALLEY INVESTMENTS, LLC"), "LLC")
        self.assertEqual(E.kind_of("GREEN VALLEY INVESTMENTS INC."), "INC")


class Lookup(unittest.TestCase):
    def fake(self, hits, rec=None):
        return lambda url: {"data": rec} if "?name=" not in url else {"data": hits}

    def test_suffix_breaks_a_tie(self):
        hits = [{"name": "GREEN VALLEY INVESTMENTS INC.", "taxpayerId": "1"}, {"name": "GREEN VALLEY INVESTMENTS, LLC", "taxpayerId": "2"}]
        E.time.sleep = lambda s: None
        kind, rec, c = E.lookup("Green Valley Investments Llc", get=self.fake(hits, REC))
        self.assertEqual((kind, c), ("one", ["GREEN VALLEY INVESTMENTS, LLC"]))

    def test_zip_breaks_a_tie_between_same_names(self):
        E.time.sleep = lambda s: None
        hits = [{"name": "GREEN VALLEY INVESTMENTS, LLC", "taxpayerId": "1"}, {"name": "GREEN VALLEY INVESTMENTS LLC", "taxpayerId": "2"}]
        recs = {"1": {"mailingAddressZip": "75001", "officerInfo": []},
                "2": {"mailingAddressZip": "77478", "officerInfo": [{"AGNT_NM": "JOSEPH MARTIN", "AD_ZP": "77002"}]}}
        get = lambda url: {"data": hits} if "?name=" in url else {"data": recs[url.rsplit("/", 1)[1]]}
        kind, rec, _ = E.lookup("Green Valley Investments Llc", get=get, lead_zip="77002")
        self.assertEqual((kind, rec["officerInfo"][0]["AGNT_NM"]), ("one", "JOSEPH MARTIN"), "an officer's ZIP counts")
        self.assertEqual(E.lookup("Green Valley Investments Llc", get=get, lead_zip="77494")[0], "several")

    def test_none_and_several(self):
        self.assertEqual(E.lookup("Movitext Holdings Inc", get=self.fake([]))[0], "none")
        two = [{"name": "X HOMES LLC", "taxpayerId": "1"}, {"name": "X HOMES, L.L.C.", "taxpayerId": "2"}]
        self.assertEqual(E.lookup("X Homes Llc", get=self.fake(two))[0], "several")


class Decide(unittest.TestCase):
    def test_name_and_zip_is_confirmed(self):
        tag, note = E.decide(lead(company="Rei Trading Post Llc"), "one", REC, [], DAY)
        self.assertEqual(tag, E.TAG_OK)
        self.assertIn("Luis Venegas", note)
        self.assertIn(E.MARKER, note)

    def test_name_only_is_verify(self):
        tag, note = E.decide(lead(company="Rei Trading Post Llc", mail_zip="77002"), "one", REC, [], DAY)
        self.assertEqual(tag, E.TAG_VERIFY)
        self.assertIn("Luis Venegas", note, "the person is written even when it needs verifying")

    def test_bank_trust_and_not_found(self):
        self.assertIn("lender", E.decide(lead(company="Us Bank Trust National Association"), "none", None, [], DAY)[1])
        tag, note = E.decide(lead(company="2407 Norman Street Trust"), "none", None, [], DAY)
        self.assertEqual(tag, E.TAG_VERIFY)
        self.assertIn("trustee", note)


if __name__ == "__main__":
    unittest.main()
