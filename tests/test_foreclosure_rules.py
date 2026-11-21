"""Foreclosure rules of 2026-11-21: equity 50%, main price band, top 20 ZIPs, $400k cap at upload. No network."""
import os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "harris"))
import harris_grade as G  # noqa: E402
import harris_to_datasift as U  # noqa: E402

BOX = {"living_area_sqft_low": 750, "living_area_sqft_high": 3302, "bedrooms_count_low": 2, "bedrooms_count_high": 5,
       "bath_count_low": 1, "property_value_low": 31000, "property_value_high": 607503}
COUNTY = {f"num_property_value_boundary_{i}": n for i, n in enumerate([20, 26, 243, 638, 286, 99, 28, 42], 1)}
ZIPS = {f"77{i:03d}": {"num_investor_transactions": 500 - i} for i in range(30)}


def lead(**kw):
    base = {"Equity Percent": "60", "Estimated Value": "250000", "Property ZIP Code": "77001",
            "Structure Type": "Single Family Residential", "Living SqFt": "1500", "Bedrooms": "3", "Bathrooms": "2"}
    base.update(kw)
    return base


class Grade(unittest.TestCase):
    def setUp(self):
        self.tiers, self.band = G.zip_tiers(ZIPS), G.modal_band(COUNTY)

    def g(self, **kw):
        return G.grade(lead(**kw), BOX, self.tiers, date(2026, 11, 21), self.band)

    def test_modal_band(self):
        self.assertEqual(self.band, (215000, 346000))

    def test_hot_needs_all_rules(self):
        self.assertEqual(self.g()[0], "Hot")
        self.assertEqual(self.g(**{"Equity Percent": "49"})[0], "Warm")
        self.assertIn("price band", " ".join(self.g(**{"Estimated Value": "380000"})[1]))
        self.assertEqual(self.g(**{"Estimated Value": "200000"})[0], "Warm")
        self.assertEqual(self.g(**{"Property ZIP Code": "77025"})[0], "Warm", "ZIP #26 is outside the top 20")
        self.assertEqual(self.g(**{"Property ZIP Code": "77019"})[0], "Hot", "ZIP #20 is inside")


class UploadCap(unittest.TestCase):
    def test_over_400k_is_left_out(self):
        self.assertTrue(U.over_cap("400001"))
        self.assertFalse(U.over_cap("400000"))
        self.assertFalse(U.over_cap(""), "an unknown value is kept; the grade decides")


if __name__ == "__main__":
    unittest.main()
