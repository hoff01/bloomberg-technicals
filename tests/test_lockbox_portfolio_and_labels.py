from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import unittest

import polars as pl
from polars.testing import assert_frame_equal

from app.model_artifact import _selected_strategy_rows
from app.technical_backtest import (
    STRATEGIES,
    _apply_current_trade_budget,
    _score_rows,
    _walk_forward_windows,
)
from app.technical_config import load_technical_config
from app.technical_labels import (
    GASOIL_BBL_PER_MT,
    USD_BBL_TO_CPG_DIVISOR,
    trade_code_fields,
)
from app.technical_reporting import portfolio_lockbox_trade_budget


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LockboxPortfolioAndLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_technical_config(
            PROJECT_ROOT / "config" / "technical_system.toml"
        )

    @staticmethod
    def _score_trades(lockbox_multiplier: float) -> pl.DataFrame:
        rows: list[dict[str, object]] = []
        start = date(2025, 1, 2)
        for index in range(90):
            net = 80.0 + 10.0 * (index % 5)
            rows.append(
                {
                    "spread_id": "TEST",
                    "spread_name": "Test",
                    "strategy_id": "ROBUST_MEAN_REVERSION",
                    "phase": "OOS",
                    "fold": index // 30 + 1,
                    "exit_session": start + timedelta(days=index),
                    "net_pnl_usd": net,
                    "gross_pnl_usd": net + 10.0,
                    "cost_usd": 10.0,
                    "pnl_2x_cost_usd": net - 10.0,
                    "pnl_3x_cost_usd": net - 20.0,
                    "complexity_tier": 1,
                    "algebra_group": "TEST",
                    "holding_bars": 4,
                    "direction": "LONG" if index % 2 == 0 else "SHORT",
                    "exit_reason": "FAIR_VALUE",
                }
            )
        for index in range(30):
            net = lockbox_multiplier * (100.0 + index)
            rows.append(
                {
                    **rows[index],
                    "phase": "LOCKBOX",
                    "fold": None,
                    "exit_session": start + timedelta(days=120 + index),
                    "net_pnl_usd": net,
                    "gross_pnl_usd": net + 10.0,
                    "pnl_2x_cost_usd": net - 10.0,
                    "pnl_3x_cost_usd": net - 20.0,
                }
            )
        return pl.DataFrame(rows, infer_schema_length=None)

    def test_lockbox_pnl_cannot_change_status_metrics_or_selection(self) -> None:
        positive, _ = _score_rows(
            self._score_trades(1.0), self.config, trial_count=len(STRATEGIES)
        )
        negative, _ = _score_rows(
            self._score_trades(-1.0), self.config, trial_count=len(STRATEGIES)
        )
        development_columns = [
            column
            for column in positive.columns
            if column not in {"lockbox_net_pnl_usd"}
        ]
        assert_frame_equal(
            positive.select(development_columns),
            negative.select(development_columns),
            check_dtypes=True,
        )
        self.assertNotEqual(
            positive["lockbox_net_pnl_usd"][0],
            negative["lockbox_net_pnl_usd"][0],
        )
        self.assertFalse(positive["lockbox_used_for_selection"][0])
        self.assertEqual(
            _selected_strategy_rows(positive)[0]["strategy_id"],
            _selected_strategy_rows(negative)[0]["strategy_id"],
        )

    def test_current_budget_selects_at_most_three_independent_entries(self) -> None:
        rows = []
        for index in range(5):
            rows.append(
                {
                    "spread_id": f"S{index}",
                    "status": "BUY",
                    "direction_evidence_validated": True,
                    "confidence": 0.90 - index * 0.05,
                    "expected_edge_to_cost": 5.0,
                    "pattern_strength": 0.4,
                    "relative_volume": 1.2,
                    "complexity_tier": 1,
                    "algebra_group": "DUPLICATE" if index in {0, 1} else f"G{index}",
                }
            )
        result = _apply_current_trade_budget(
            pl.DataFrame(rows), self.config
        )
        self.assertLessEqual(
            result.filter(pl.col("portfolio_selected")).height,
            self.config.backtest.maximum_new_trades_per_session,
        )
        self.assertEqual(
            result.filter(
                pl.col("portfolio_action") == "DUPLICATE_ALGEBRA_GROUP"
            ).height,
            1,
        )

    def test_lockbox_budget_caps_each_day_without_using_trade_pnl(self) -> None:
        session = date(2026, 8, 3)
        trades = pl.DataFrame(
            [
                {
                    "spread_id": f"S{index}",
                    "strategy_id": "ROBUST_MEAN_REVERSION",
                    "entry_session": session,
                    "entry_time": datetime(2026, 8, 3, 13, index, tzinfo=timezone.utc),
                    "phase": "LOCKBOX",
                    "algebra_group": f"G{index}",
                    "entry_relative_volume": 1.0 + index / 10,
                    "complexity_tier": 1,
                    "net_pnl_usd": -1_000_000.0 if index == 0 else 1.0,
                }
                for index in range(5)
            ]
        )
        summaries = pl.DataFrame(
            [
                {
                    "spread_id": f"S{index}",
                    "selected_strategy_id": "ROBUST_MEAN_REVERSION",
                    "selected_strategy_status": "VALIDATED",
                    "model_deflated_sharpe_probability": 0.99 - index / 100,
                    "model_profitable_fold_share": 0.8,
                    "model_daily_sharpe": 1.5,
                    "model_net_pnl_usd": 1000.0,
                }
                for index in range(5)
            ]
        )
        result = portfolio_lockbox_trade_budget(trades, summaries, self.config)
        selected = result.filter(pl.col("portfolio_selected"))
        self.assertEqual(
            selected.height, self.config.backtest.maximum_new_trades_per_session
        )
        self.assertTrue((selected["lockbox_used_for_selection"] == False).all())  # noqa: E712
        self.assertIn("S0", selected["spread_id"].to_list())

    def test_trade_codes_and_gasoil_conversions_match_requested_units(self) -> None:
        spread = next(
            item for item in self.config.spreads if item.spread_id == "HO1_CO2"
        )
        fields = trade_code_fields(
            spread,
            {
                "leg1_delivery_month": date(2026, 11, 1),
                "leg2_delivery_month": date(2027, 3, 1),
                "leg1_security": "HOX6 Comdty",
                "leg2_security": "COH7 Comdty",
            },
        )
        self.assertEqual(fields["trade_code"], "Nov26/Mar27 HO/Brent Crack")
        self.assertEqual(fields["display_unit"], "USD/bbl")
        self.assertEqual(fields["quote_convention"], "CRACK_USD_BBL")
        self.assertEqual(fields["display_level_factor"], 1.0)

        ho_spread = next(
            item for item in self.config.spreads if item.spread_id == "HO1_HO2"
        )
        ho_fields = trade_code_fields(
            ho_spread,
            {
                "leg1_delivery_month": date(2026, 11, 1),
                "leg2_delivery_month": date(2026, 12, 1),
                "leg1_security": "HOX6 Comdty",
                "leg2_security": "HOZ6 Comdty",
            },
        )
        self.assertEqual(ho_fields["display_unit"], "cpg")
        self.assertEqual(ho_fields["quote_convention"], "HO_CPG")
        self.assertAlmostEqual(
            float(ho_fields["display_level_factor"]),
            1.0 / USD_BBL_TO_CPG_DIVISOR,
        )
        gasoil_usd_mt = 700.0
        gasoil_usd_bbl = gasoil_usd_mt / GASOIL_BBL_PER_MT
        gasoil_cpg = gasoil_usd_bbl / USD_BBL_TO_CPG_DIVISOR
        self.assertAlmostEqual(gasoil_usd_bbl, 700.0 / 7.45)
        self.assertAlmostEqual(gasoil_cpg, 700.0 / 7.45 / 0.42)
        self.assertAlmostEqual(
            self.config.roots["QS"].price_to_usd_bbl,
            1.0 / GASOIL_BBL_PER_MT,
        )

    def test_walk_forward_counts_embargo_outside_fifteen_oos_sessions(self) -> None:
        sessions = [date(2025, 1, 1) + timedelta(days=index) for index in range(392)]
        windows = _walk_forward_windows(sessions, self.config)
        self.assertEqual(len(windows), 7)
        for window in windows:
            self.assertEqual(
                (window["validation_end"] - window["validation_start"]).days + 1,
                self.config.backtest.validation_sessions,
            )
            self.assertEqual(
                (window["embargo_end"] - window["embargo_start"]).days + 1,
                self.config.backtest.embargo_sessions,
            )
            self.assertEqual(
                (window["test_end"] - window["test_start"]).days + 1,
                self.config.backtest.test_sessions,
            )
            self.assertLess(window["validation_end"], window["embargo_start"])
            self.assertLess(window["embargo_end"], window["test_start"])

    def test_hogo_outrights_boxes_and_flies_cover_the_curve(self) -> None:
        hogo_out = [
            item
            for item in self.config.spreads
            if item.spread_id.startswith("RV.HO_QS.")
        ]
        hogo_boxes = [
            item
            for item in self.config.spreads
            if item.spread_id.startswith("BOX.CURVE.HO_QS.")
        ]
        hogo_flies = [
            item for item in self.config.spreads if item.family == "HOGO Fly"
        ]
        self.assertEqual(len(hogo_out), 15)
        self.assertEqual(len(hogo_boxes), 14)
        self.assertEqual(len(hogo_flies), 14)
        self.assertTrue(all(item.model_enabled for item in hogo_flies))


if __name__ == "__main__":
    unittest.main()
