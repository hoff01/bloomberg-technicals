from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import unittest

import polars as pl

from app.technical_backtest import (
    BASE_EXPERT_IDS,
    STRATEGIES,
    _add_adaptive_ensemble_reference,
    _bounded_weights,
    add_adaptive_ensemble,
)
from app.technical_config import load_technical_config
from app.technical_data import normalize_daily_frame, package_depth_snapshot
from app.technical_reporting import _history_payload


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TechnicalV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_technical_config(
            PROJECT_ROOT / "config" / "technical_system.toml"
        )

    def test_curve_registry_and_trial_ledger_are_complete(self) -> None:
        identifiers = [item.spread_id for item in self.config.spreads]
        self.assertEqual(self.config.system.forward_curve_months, 16)
        self.assertEqual(self.config.system.daily_history_start, date(2022, 1, 1))
        self.assertEqual(len(identifiers), 337)
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(sum(item.model_enabled for item in self.config.spreads), 331)
        self.assertEqual(len(STRATEGIES), 9)
        self.assertEqual(
            sum(item.model_enabled for item in self.config.spreads) * len(STRATEGIES),
            2_979,
        )
        required = {
            "CRK.HO_CL.M16",
            "CRK.QS_CO.M16",
            "CAL.HO.M15_M16",
            "FLY.HO.M14_M15_M16",
            "CDR.HO.M13_M14_M15_M16",
            "BOX.CRKCAL.QS_CO.M15_M16",
            "BOX.CRACK.TRANSATL.M16",
            "RV.HO_QS.M16",
            "RV.CO_CL.M16",
        }
        self.assertTrue(required.issubset(set(identifiers)))

    def test_daily_settle_is_authoritative_and_fallback_is_labeled(self) -> None:
        definition = self.config.build_contract_universe(
            date(2026, 8, 1), date(2026, 8, 31)
        )[0]
        settled = pl.DataFrame(
            {
                "ticker": [definition.ticker, definition.ticker],
                "date": [date(2026, 8, 28), date(2026, 8, 28)],
                "field": ["PX_SETTLE", "PX_LAST"],
                "value": ["2.20", "2.25"],
            }
        )
        result = normalize_daily_frame(settled, {definition.ticker: definition})
        self.assertAlmostEqual(result["close"][0], 2.20)
        self.assertEqual(result["settle_source_field"][0], "PX_SETTLE")

        fallback = pl.DataFrame(
            {
                "ticker": [definition.ticker],
                "date": [date(2026, 8, 29)],
                "field": ["PX_LAST"],
                "value": ["2.30"],
            }
        )
        result = normalize_daily_frame(fallback, {definition.ticker: definition})
        self.assertAlmostEqual(result["close"][0], 2.30)
        self.assertEqual(result["settle_source_field"][0], "PX_LAST_FALLBACK")

    def test_dashboard_history_keeps_prior_closes_and_latest_intraday(self) -> None:
        rows: list[dict[str, object]] = []
        for day_index in range(3):
            session = date(2026, 8, 27 + day_index)
            for slot in range(2):
                timestamp = datetime.combine(
                    session,
                    datetime.min.time().replace(hour=13 + slot),
                    tzinfo=timezone.utc,
                )
                rows.append(
                    {
                        "timestamp_utc": timestamp,
                        "session_date": session,
                        "spread_id": "TEST",
                        "spread_close": float(day_index * 10 + slot),
                        "rolling_median": 1.0,
                        "bollinger_upper": 2.0,
                        "bollinger_lower": 0.0,
                        "relative_volume": 1.0,
                        "robust_z": 0.0,
                    }
                )
        history = _history_payload(pl.DataFrame(rows), sessions=3)
        self.assertEqual(len(history), 4)
        self.assertEqual(
            sum(row["session_date"] == "2026-08-29" for row in history), 2
        )

    def test_depth_capacity_is_side_correct_and_package_limited(self) -> None:
        spec = next(item for item in self.config.spreads if item.spread_id == "HO1_CL1")
        now = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
        feature = {
            "spread_id": spec.spread_id,
            "timestamp_utc": now,
        }
        quotes: list[dict[str, object]] = []
        expected_buy: list[float] = []
        expected_sell: list[float] = []
        for index, leg in enumerate(spec.legs, start=1):
            security = f"LEG{index} Comdty"
            feature[f"leg{index}_security"] = security
            bid_size = 80.0 + 10.0 * index
            ask_size = 60.0 + 5.0 * index
            quotes.append(
                {
                    "time": now - timedelta(minutes=1),
                    "security": security,
                    "bid_size": bid_size,
                    "ask_size": ask_size,
                }
            )
            expected_buy.append(
                (ask_size if leg.sign > 0 else bid_size) / leg.contracts
            )
            expected_sell.append(
                (bid_size if leg.sign > 0 else ask_size) / leg.contracts
            )
        depth = package_depth_snapshot(
            pl.DataFrame([feature]),
            pl.DataFrame(quotes),
            self.config,
            depth_source="TOP_OF_BOOK",
        )
        self.assertAlmostEqual(depth["buy_package_depth"][0], min(expected_buy))
        self.assertAlmostEqual(depth["sell_package_depth"][0], min(expected_sell))
        self.assertTrue(depth["depth_fresh"][0])
        self.assertTrue(depth["depth_supports_one_package"][0])

    def test_adaptive_weights_are_bounded_delayed_and_lockbox_frozen(self) -> None:
        bounded = _bounded_weights(
            {expert: 100.0 if index == 0 else 0.01 for index, expert in enumerate(BASE_EXPERT_IDS)},
            self.config.backtest.adaptive_max_expert_weight,
        )
        self.assertAlmostEqual(sum(bounded.values()), 1.0, places=9)
        self.assertLessEqual(
            max(bounded.values()), self.config.backtest.adaptive_max_expert_weight + 1e-9
        )

        start = date(2025, 1, 2)
        rows: list[dict[str, object]] = []
        for index in range(150):
            session = start + timedelta(days=index)
            row: dict[str, object] = {
                "spread_id": "ADAPTIVE_TEST",
                "timestamp_utc": datetime.combine(
                    session, datetime.min.time().replace(hour=14), tzinfo=timezone.utc
                ),
                "session_date": session,
                "spread_family": "Crack",
                "tenor_bucket": "FRONT",
                "entry_allowed": True,
                "roll_id": "UNCHANGED",
                "forced_exit_session": date(2027, 1, 1),
                "package_open_value_usd": 100.0 + index,
                "package_close_value_usd": 101.0 + index,
                "one_way_cost_usd": 0.5,
                "change_point_alarm": False,
                "expert_vote_regime_ensemble": 0,
            }
            for expert in BASE_EXPERT_IDS:
                row[f"expert_vote_{expert.lower()}"] = 0
            row["expert_vote_robust_mean_reversion"] = 1
            row["expert_vote_trend_breakout"] = -1
            rows.append(row)

        fixture = pl.DataFrame(rows)
        learned = add_adaptive_ensemble(fixture, self.config)
        reference = _add_adaptive_ensemble_reference(fixture, self.config)
        weight_columns = [
            f"adaptive_weight_{expert.lower()}" for expert in BASE_EXPERT_IDS
        ]
        sums = learned.select(pl.sum_horizontal(*weight_columns).alias("total"))
        self.assertTrue(
            sums.select((pl.col("total") - 1.0).abs().max() < 1e-5).item()
        )
        self.assertLessEqual(
            learned.select(pl.max_horizontal(*weight_columns).max()).item(),
            self.config.backtest.adaptive_max_expert_weight + 1e-5,
        )
        self.assertEqual(learned["adaptive_status"][0], "WARMUP")
        lockbox = learned.tail(self.config.backtest.lockbox_sessions)
        self.assertTrue((lockbox["adaptive_status"] == "LOCKBOX_FROZEN").all())
        for column in weight_columns:
            self.assertEqual(lockbox[column].n_unique(), 1)
            delta = learned.select(column).to_series() - reference.select(column).to_series()
            self.assertLessEqual(delta.abs().max(), 1e-6)
        self.assertEqual(
            learned["adaptive_vote"].to_list(), reference["adaptive_vote"].to_list()
        )
        self.assertEqual(
            learned["adaptive_observations"].to_list(),
            reference["adaptive_observations"].to_list(),
        )


if __name__ == "__main__":
    unittest.main()
