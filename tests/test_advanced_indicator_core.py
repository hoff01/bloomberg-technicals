from __future__ import annotations

import math
from pathlib import Path
from time import perf_counter
import unittest

import polars as pl
from polars.testing import assert_frame_equal

from app.technical_analytics import (
    _add_advanced_indicators,
    _add_causal_risk_indicators,
)
from app.technical_config import load_technical_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RISK_COLUMNS = (
    "tod_normalized_change",
    "vol_regime_ratio_1d_20d",
    "liquidity_stress_ratio",
    "tail_event_rate_20d",
    "robust_volume_surprise",
    "return_skew_5d",
    "return_excess_kurtosis_5d",
    "realized_vol_of_vol_5d",
    "trend_hac_t_stat_3d",
    "close_path_choppiness_5d",
)


class AdvancedIndicatorCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_technical_config(
            PROJECT_ROOT / "config" / "technical_system.toml"
        )

    @staticmethod
    def _risk_fixture(rows: int = 360, spread_id: str = "TEST") -> pl.DataFrame:
        changes = [
            0.08 * math.sin(index / 9.0) + 0.025 * math.cos(index / 17.0)
            for index in range(rows)
        ]
        closes: list[float] = []
        level = 12.0
        for change in changes:
            level += change
            closes.append(level)
        capacity = [900.0 + 70.0 * math.sin(index / 11.0) for index in range(rows)]
        impact = [
            abs(change) / (volume * 1_000.0 + 1.0) * 1_000_000.0
            for change, volume in zip(changes, capacity, strict=True)
        ]
        return pl.DataFrame(
            {
                "spread_id": [spread_id] * rows,
                "bar_slot": [index % 13 for index in range(rows)],
                "price_change": changes,
                "package_volume_capacity": capacity,
                "amihud_impact_proxy": impact,
                "research_close": closes,
            }
        )

    def test_threshold_defaults_are_typed_and_safely_ordered(self) -> None:
        settings = self.config.indicators
        self.assertEqual(settings.tail_event_z, 2.5)
        self.assertEqual(settings.extreme_tod_shock_z, 4.0)
        self.assertEqual(settings.volume_dryness_z, -2.5)
        self.assertEqual(settings.tail_cluster_rate, 0.10)
        self.assertEqual(settings.vol_expansion_ratio, 1.5)
        self.assertEqual(settings.impact_stress_ratio, 2.5)
        self.assertGreater(settings.extreme_tod_shock_z, settings.tail_event_z)

    def test_causal_outputs_do_not_rewrite_prior_rows(self) -> None:
        fixture = self._risk_fixture()
        original = _add_causal_risk_indicators(fixture, self.config.indicators)
        shocked = fixture.with_row_index("_row").with_columns(
            pl.when(pl.col("_row") == fixture.height - 1)
            .then(25.0)
            .otherwise(pl.col("price_change"))
            .alias("price_change"),
            pl.when(pl.col("_row") == fixture.height - 1)
            .then(1.0)
            .otherwise(pl.col("package_volume_capacity"))
            .alias("package_volume_capacity"),
            pl.when(pl.col("_row") == fixture.height - 1)
            .then(50.0)
            .otherwise(pl.col("amihud_impact_proxy"))
            .alias("amihud_impact_proxy"),
            pl.when(pl.col("_row") == fixture.height - 1)
            .then(100.0)
            .otherwise(pl.col("research_close"))
            .alias("research_close"),
        ).drop("_row")
        revised = _add_causal_risk_indicators(shocked, self.config.indicators)
        assert_frame_equal(
            original.head(fixture.height - 1).select(*RISK_COLUMNS),
            revised.head(fixture.height - 1).select(*RISK_COLUMNS),
            check_dtypes=True,
        )
        self.assertNotEqual(
            original["tod_normalized_change"][-1],
            revised["tod_normalized_change"][-1],
        )

    def test_zero_baseline_is_finite_and_neutral_after_warmup(self) -> None:
        rows = 320
        fixture = pl.DataFrame(
            {
                "spread_id": ["FLAT"] * rows,
                "bar_slot": [index % 13 for index in range(rows)],
                "price_change": [0.0] * rows,
                "package_volume_capacity": [0.0] * rows,
                "amihud_impact_proxy": [0.0] * rows,
                "research_close": [10.0] * rows,
            }
        )
        result = _add_causal_risk_indicators(fixture, self.config.indicators)
        mature = result.tail(1)
        undefined_moments = {
            "return_skew_5d",
            "return_excess_kurtosis_5d",
        }
        for column in RISK_COLUMNS:
            self.assertEqual(result.schema[column], pl.Float32)
            value = mature[column][0]
            if column in undefined_moments:
                self.assertIsNone(value, column)
                continue
            self.assertIsNotNone(value, column)
            self.assertTrue(math.isfinite(float(value)), column)
        self.assertEqual(mature["tod_normalized_change"][0], 0.0)
        self.assertEqual(mature["vol_regime_ratio_1d_20d"][0], 1.0)
        self.assertEqual(mature["liquidity_stress_ratio"][0], 1.0)
        self.assertEqual(mature["tail_event_rate_20d"][0], 0.0)
        self.assertEqual(mature["robust_volume_surprise"][0], 0.0)
        self.assertEqual(mature["trend_hac_t_stat_3d"][0], 0.0)
        self.assertEqual(mature["close_path_choppiness_5d"][0], 0.0)
        self.assertEqual(mature["advanced_risk_regime"][0], "NORMAL")

    def test_same_slot_normalization_uses_only_prior_sessions(self) -> None:
        rows = 22 * 13
        changes = [1.0] * rows
        changes[-1] = 2.0
        fixture = pl.DataFrame(
            {
                "spread_id": ["SLOT"] * rows,
                "bar_slot": [index % 13 for index in range(rows)],
                "price_change": changes,
                "package_volume_capacity": [100.0] * rows,
                "amihud_impact_proxy": [1.0] * rows,
                "research_close": [float(index) for index in range(rows)],
            }
        )
        result = _add_causal_risk_indicators(fixture, self.config.indicators)
        self.assertAlmostEqual(
            result["tod_normalized_change"][-1], 2.0 / 1.4826, places=5
        )

    def test_higher_moment_windows_reset_at_each_spread_boundary(self) -> None:
        rows_per_spread = 80
        fixture = pl.concat(
            [
                self._risk_fixture(rows_per_spread, "FIRST"),
                self._risk_fixture(rows_per_spread, "SECOND").with_columns(
                    (pl.col("price_change") * -1.7 + 0.03).alias("price_change")
                ),
            ],
            how="vertical",
        )
        result = _add_causal_risk_indicators(fixture, self.config.indicators)
        warmup = int(self.config.indicators.three_sessions) - 1
        for spread_id in ("FIRST", "SECOND"):
            group = result.filter(pl.col("spread_id") == spread_id)
            self.assertTrue(group["return_skew_5d"].head(warmup).is_null().all())
            self.assertTrue(
                group["return_excess_kurtosis_5d"]
                .head(warmup)
                .is_null()
                .all()
            )
            self.assertIsNotNone(group["return_skew_5d"][warmup])
            self.assertIsNotNone(group["return_excess_kurtosis_5d"][warmup])

    def test_hac_trend_stat_matches_finite_window_reference(self) -> None:
        window = int(self.config.indicators.three_sessions)
        lags = max(1, int(self.config.indicators.one_session) // 4)
        fixture = self._risk_fixture(window + 90)
        result = _add_causal_risk_indicators(fixture, self.config.indicators)
        values = [float(value) for value in fixture["price_change"].tail(window)]
        mean = sum(values) / len(values)
        gamma0 = sum((value - mean) ** 2 for value in values) / len(values)
        long_run_variance = gamma0
        for lag in range(1, lags + 1):
            gamma = sum(
                (values[index] - mean) * (values[index - lag] - mean)
                for index in range(lag, len(values))
            ) / len(values)
            long_run_variance += 2.0 * (1.0 - lag / (lags + 1.0)) * gamma
        expected = (
            mean * math.sqrt(len(values)) / math.sqrt(long_run_variance)
            if long_run_variance > 1e-12
            else 0.0
        )
        expected = max(-12.0, min(12.0, expected))
        self.assertAlmostEqual(
            result["trend_hac_t_stat_3d"][-1], expected, places=5
        )

    def test_multi_leg_relationship_metrics_fail_closed(self) -> None:
        rows: list[dict[str, object]] = []
        for spread_id, multi_leg in (("TWO", False), ("MULTI", True)):
            for index in range(600):
                change = 0.06 * math.sin(index / 5.0) + 0.01
                rows.append(
                    {
                        "spread_id": spread_id,
                        "bar_slot": index % 26,
                        "research_close": 10.0 + index * 0.01 + change,
                        "price_change": change,
                        "leg1_close": 70.0 + index * 0.02,
                        "leg1_price_to_usd_bbl": 1.0,
                        "leg2_close": 60.0 + index * 0.01 + math.sin(index / 7.0),
                        "leg2_price_to_usd_bbl": 1.0,
                        "leg3_security": "THIRD Comdty" if multi_leg else None,
                        "robust_z": math.sin(index / 10.0),
                        "ou_half_life_bars": 8.0,
                        "normalized_change": change / 0.06,
                        "paired_volume_bbl": 100_000.0 + index,
                        "signed_volume": 1_000.0 if change >= 0 else -1_000.0,
                        "relative_volume": 1.0,
                        "package_oi_capacity": 2_000.0 + index,
                        "upside_semivol": 0.10,
                        "downside_semivol": 0.09,
                        "efficiency_ratio": 0.30,
                        "package_volume_capacity": 100.0 + index,
                    }
                )
        fixture = pl.DataFrame(rows, infer_schema_length=None)
        result = _add_advanced_indicators(fixture, self.config.indicators)
        multi = result.filter(pl.col("spread_id") == "MULTI")
        two = result.filter(pl.col("spread_id") == "TWO")
        self.assertTrue(multi["leg_return_correlation"].is_null().all())
        self.assertTrue(multi["lead_lag_score"].is_null().all())
        self.assertEqual(
            multi["relationship_health_scope"].unique().to_list(),
            ["MULTI_LEG_DIAGNOSTIC_UNAVAILABLE"],
        )
        self.assertGreater(two["leg_return_correlation"].drop_nulls().len(), 0)
        self.assertGreater(two["lead_lag_score"].drop_nulls().len(), 0)
        self.assertEqual(
            two["relationship_health_scope"].unique().to_list(), ["TWO_LEG"]
        )

    def test_native_polars_indicator_overhead_is_bounded(self) -> None:
        fixture = pl.concat(
            [self._risk_fixture(400, f"S{index}") for index in range(8)],
            how="vertical",
        )
        started = perf_counter()
        result = _add_causal_risk_indicators(fixture, self.config.indicators)
        elapsed = perf_counter() - started
        self.assertEqual(result.height, fixture.height)
        self.assertLess(
            elapsed,
            5.0,
            f"advanced native-Polars indicators took {elapsed:.3f}s for {fixture.height:,} rows",
        )


if __name__ == "__main__":
    unittest.main()
