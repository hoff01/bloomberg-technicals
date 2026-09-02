from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

import polars as pl

from app.technical_summary import (
    _recent_structure_metrics,
    build_model_summary,
    build_structure_summaries,
)
from app.technical_pipeline import TRAIN_SCORE_PARITY_FIELDS
from app.technical_reporting import _dashboard_html, current_indicator_snapshot
from scripts.build_technical_workbook import TRADE_BRIEF_PREFERRED_COLUMNS


ADVANCED_NUMERIC_FIELDS = {
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
}

ADVANCED_SURFACE_FIELDS = ADVANCED_NUMERIC_FIELDS | {
    "advanced_risk_regime",
    "relationship_health_scope",
}
PATTERN_SURFACE_FIELDS = {
    "pattern_state",
    "pattern_strength",
    "pattern_agreement",
    "pattern_components",
}


class TechnicalSummaryTests(unittest.TestCase):
    def test_drawdown_includes_a_negative_first_trade(self) -> None:
        recent = pl.DataFrame(
            [
                self._trade(date(2026, 8, 5), -100.0),
                self._trade(date(2026, 8, 6), 20.0),
            ]
        )
        structure = _recent_structure_metrics(recent)
        self.assertEqual(structure["recent_30_max_drawdown_usd"][0], 100.0)
        model = build_model_summary(
            pl.DataFrame(),
            recent,
            pl.DataFrame(),
            {"model_id": "drawdown-test", "training_window": {}},
            demo_mode=True,
        )
        self.assertEqual(model["latest_30_sessions"]["max_drawdown_usd"], 100.0)

    def test_trade_brief_keeps_targets_oos_and_lockbox_results_distinct(self) -> None:
        signals = pl.DataFrame(
            [
                {
                    "spread_id": "TEST",
                    "spread_name": "Test spread",
                    "family": "Crack",
                    "trade_code": "Nov26 HO/WTI Crack",
                    "trade_code_short": "Nov HO/WTI Crack",
                    "contract_codes": "HOX6 Comdty | CLX6 Comdty",
                    "contract_months": "2026-11-01",
                    "structure_roots": "HO|CL",
                    "calculation_unit": "USD/bbl",
                    "display_unit": "USD/bbl",
                    "display_level_factor": 1.0,
                    "quote_convention": "CRACK_USD_BBL",
                    "conversion_method": "Normalized USD/bbl package quote",
                    "portfolio_action": "WATCH_ONLY",
                    "portfolio_selected": False,
                    "portfolio_rank": None,
                    "portfolio_candidate_rank": None,
                    "daily_trade_limit": 3,
                    "daily_trade_slots_remaining": 3,
                    "model_enabled": True,
                    "model_stale": False,
                    "model_age_sessions": 0,
                    "complexity_tier": 1,
                    "status": "WATCH",
                    "current_spread": 10.0,
                    "buy_entry_ceiling": 9.0,
                    "sell_entry_floor": 11.0,
                    "fair_value_target": 10.5,
                    "long_stop": 8.0,
                    "short_stop": 12.0,
                    "display_current": 10.0,
                    "display_buy_entry": 9.0,
                    "display_sell_entry": 11.0,
                    "display_fair_value": 10.5,
                    "display_long_stop": 8.0,
                    "display_short_stop": 12.0,
                    "heating_oil_cpg": None,
                    "gasoil_usd_mt": None,
                    "gasoil_usd_bbl": None,
                    "gasoil_cpg": None,
                    "hogo_cpg": None,
                    "confidence": 0.65,
                    "signal_strategy_id": "ROBUST_MEAN_REVERSION",
                    "direction_evidence_validated": False,
                    "adaptive_score": 0.42,
                    "adaptive_observations": 120,
                    "adaptive_top_expert": "ROBUST_MEAN_REVERSION",
                    "adaptive_top_weight": 0.30,
                    "strategy_votes_long": 3,
                    "strategy_votes_short": 0,
                    "pattern_state": "BULLISH_CONSENSUS",
                    "pattern_strength": 0.42,
                    "pattern_agreement": 3 / 7,
                    "pattern_components": "3 long / 0 short; top=ROBUST_MEAN_REVERSION",
                    "expected_edge_usd": 500.0,
                    "round_trip_cost_usd": 50.0,
                    "expected_edge_to_cost": 10.0,
                    "relative_volume": 1.2,
                    "tod_normalized_change": 1.25,
                    "vol_regime_ratio_1d_20d": 1.15,
                    "liquidity_stress_ratio": 0.90,
                    "tail_event_rate_20d": 0.05,
                    "robust_volume_surprise": 0.40,
                    "return_skew_5d": -0.30,
                    "return_excess_kurtosis_5d": 0.75,
                    "realized_vol_of_vol_5d": 0.02,
                    "trend_hac_t_stat_3d": 1.60,
                    "close_path_choppiness_5d": 55.0,
                    "advanced_risk_regime": "NORMAL",
                    "relationship_health_scope": "TWO_LEG",
                    "package_volume_capacity": 100.0,
                    "max_leg_bid_ask_ticks": 2.0,
                    "depth_source": "TOP_OF_BOOK",
                    "liquidity_gate": "PASS",
                    "mandatory_last_exit_session": date(2026, 10, 1),
                    "sessions_to_risk_date": 20,
                    "regime": "MEAN_REVERTING",
                    "vote_balance": 2,
                    "kronos_expected_move_1b": None,
                    "kronos_status": "DISABLED_OR_NOT_RUN",
                    "demo_mode": False,
                }
            ]
        )
        scorecard = pl.DataFrame(
            [
                {
                    "spread_id": "TEST",
                    "strategy_id": "ROBUST_MEAN_REVERSION",
                    "strategy_name": "Robust Mean Reversion",
                    "status": "RESEARCH_ONLY",
                    "evaluation_scope": "OOS",
                    "oos_trades": 25,
                    "lockbox_trades": 2,
                    "net_pnl_usd": 1000.0,
                    "lockbox_net_pnl_usd": 50.0,
                    "net_pnl_2x_cost_usd": 800.0,
                    "net_pnl_3x_cost_usd": 600.0,
                    "daily_sharpe": 1.1,
                    "daily_sortino": 1.4,
                    "max_drawdown_usd": 300.0,
                    "win_rate": 0.60,
                    "profit_factor": 1.4,
                    "expectancy_usd": 40.0,
                    "profitable_fold_share": 0.75,
                    "deflated_sharpe_probability": 0.90,
                    "long_net_pnl_usd": 700.0,
                    "short_net_pnl_usd": 300.0,
                }
            ]
        )
        trades = pl.DataFrame(
            [
                self._trade(date(2026, 8, 5), 100.0),
                self._trade(date(2026, 8, 10), -50.0),
                self._trade(date(2026, 6, 1), 999.0),
            ]
        )
        summaries, recent = build_structure_summaries(
            signals,
            scorecard,
            trades,
            lockbox_start=date(2026, 8, 1),
            lockbox_end=date(2026, 8, 31),
            demo_mode=False,
        )
        self.assertEqual(recent.height, 2)
        self.assertEqual(summaries["recent_30_trades"][0], 2)
        self.assertEqual(summaries["recent_30_net_pnl_usd"][0], 50.0)
        self.assertEqual(summaries["recent_30_win_rate"][0], 0.5)
        self.assertEqual(summaries["distance_to_fair_value"][0], 0.5)
        self.assertTrue(ADVANCED_SURFACE_FIELDS.issubset(summaries.columns))
        self.assertTrue(PATTERN_SURFACE_FIELDS.issubset(summaries.columns))
        self.assertEqual(summaries["advanced_risk_regime"][0], "NORMAL")
        self.assertIn("Buy entry is at or below 9.000", summaries["summary_description"][0])
        self.assertIn("Advanced risk: NORMAL", summaries["summary_description"][0])
        self.assertIn(
            "Dynamic pattern: BULLISH_CONSENSUS",
            summaries["summary_description"][0],
        )
        model = build_model_summary(
            summaries,
            recent,
            scorecard,
            {
                "model_id": "model-1",
                "mode": "LIVE",
                "as_of": "2026-08-31",
                "training_window": {
                    "lockbox_start": "2026-08-01",
                    "lockbox_end": "2026-08-31",
                },
                "total_trial_count": 9,
                "selected_strategy_count": 1,
                "validated_strategy_count": 0,
            },
            demo_mode=False,
        )
        self.assertEqual(model["latest_30_sessions"]["trades"], 2)
        self.assertEqual(model["latest_30_sessions"]["net_pnl_usd"], 50.0)
        self.assertIn("9 preregistered trials", model["description"])

    def test_advanced_risk_fields_are_release_and_parity_surfaces(self) -> None:
        timestamp = datetime(2026, 8, 31, 18, tzinfo=timezone.utc)
        row: dict[str, object] = {
            "timestamp_utc": timestamp,
            "session_date": date(2026, 8, 31),
            "spread_id": "TEST",
            "spread_name": "Test spread",
            "spread_family": "Crack",
            **{field: 0.25 for field in ADVANCED_NUMERIC_FIELDS},
            "advanced_risk_regime": "NORMAL",
            "relationship_health_scope": "MULTI_LEG_CONFIRMED",
        }
        snapshot = current_indicator_snapshot(pl.DataFrame([row]))
        self.assertTrue(ADVANCED_SURFACE_FIELDS.issubset(snapshot.columns))
        self.assertTrue(
            ADVANCED_NUMERIC_FIELDS.issubset(set(TRAIN_SCORE_PARITY_FIELDS))
        )
        self.assertTrue(
            ADVANCED_SURFACE_FIELDS.issubset(
                set(TRADE_BRIEF_PREFERRED_COLUMNS)
            )
        )
        self.assertTrue(
            {"pattern_strength", "pattern_agreement"}.issubset(
                set(TRAIN_SCORE_PARITY_FIELDS)
            )
        )
        self.assertTrue(
            PATTERN_SURFACE_FIELDS.issubset(
                set(TRADE_BRIEF_PREFERRED_COLUMNS)
            )
        )
        dashboard = _dashboard_html(
            {
                "project_name": "Test",
                "demo_mode": True,
                "structure_summaries": [],
                "model_summary": {},
            }
        )
        self.assertIn("Dynamic pattern &amp; risk", dashboard)
        self.assertIn("tod_normalized_change", dashboard)
        self.assertIn("relationship_health_scope", dashboard)

    @staticmethod
    def _trade(session: date, net: float) -> dict[str, object]:
        timestamp = datetime.combine(
            session, datetime.min.time().replace(hour=18), tzinfo=timezone.utc
        )
        cost = 10.0
        return {
            "spread_id": "TEST",
            "strategy_id": "ROBUST_MEAN_REVERSION",
            "entry_session": session,
            "exit_session": session,
            "entry_time": timestamp,
            "exit_time": timestamp,
            "gross_pnl_usd": net + cost,
            "cost_usd": cost,
            "net_pnl_usd": net,
            "holding_bars": 2,
            "direction": "LONG" if net > 0 else "SHORT",
        }


if __name__ == "__main__":
    unittest.main()
