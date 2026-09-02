from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest

import polars as pl

from app.kronos_adapter import attach_kronos_diagnostics
from app.technical_config import load_technical_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class KronosAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_technical_config(
            PROJECT_ROOT / "config" / "technical_system.toml"
        )

    def test_leg_forecasts_recombine_without_becoming_actionable(self) -> None:
        timestamp = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)
        features = pl.DataFrame(
            [
                {
                    "spread_id": "HO1_CL1",
                    "timestamp_utc": timestamp,
                    "leg1_security": "HOX26 Comdty",
                    "leg2_security": "CLX26 Comdty",
                    "ewma_abs_change": 0.10,
                }
            ]
        )
        forecasts = pl.DataFrame(
            [
                {
                    "security": "HOX26 Comdty",
                    "forecast_step": 1,
                    "forecast_origin_utc": timestamp,
                    "predicted_close_move_native": 0.01,
                    "action_enabled": False,
                },
                {
                    "security": "CLX26 Comdty",
                    "forecast_step": 1,
                    "forecast_origin_utc": timestamp,
                    "predicted_close_move_native": 0.10,
                    "action_enabled": False,
                },
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "kronos_forecasts.parquet"
            forecasts.write_parquet(path)
            result = attach_kronos_diagnostics(features, path, self.config)
        self.assertAlmostEqual(result["kronos_expected_move_1b"][0], 0.32)
        self.assertEqual(result["kronos_vote"][0], 1)
        self.assertEqual(result["kronos_contract_coverage"][0], 1.0)
        self.assertFalse(result["kronos_action_eligible"][0])
        self.assertEqual(
            result["kronos_status"][0],
            "EXPERIMENTAL_30_SESSION_EVALUATION_REQUIRED",
        )


if __name__ == "__main__":
    unittest.main()
