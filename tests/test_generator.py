from __future__ import annotations

import unittest

from scripts.generate_mock_market_data import MARKETS, _from_usd_per_bbl


class GeneratorUnitTests(unittest.TestCase):
    def test_sample_roots_include_gc_jet_and_heating_oil(self) -> None:
        self.assertIn("WU", MARKETS)
        self.assertEqual(MARKETS["WU"].clean_name, "GC Jet")
        self.assertEqual(MARKETS["HO"].native_unit, "cpg")

    def test_generated_curves_are_written_in_each_roots_native_unit(self) -> None:
        self.assertEqual(_from_usd_per_bbl(84.0, MARKETS["CL"]), 84.0)
        self.assertEqual(_from_usd_per_bbl(84.0, MARKETS["HO"]), 200.0)
        self.assertEqual(_from_usd_per_bbl(84.0, MARKETS["WU"]), 200.0)
        self.assertEqual(_from_usd_per_bbl(100.0, MARKETS["QS"]), 745.0)


if __name__ == "__main__":
    unittest.main()
