from __future__ import annotations

from datetime import date, datetime, timedelta
from dataclasses import replace
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

import polars as pl

from app.technical_analytics import (
    _back_adjust_group,
    build_spread_bars,
    prepare_trade_bars,
)
from app.technical_config import (
    expected_latest_exchange_session,
    load_technical_config,
)
from app.technical_pipeline import _live_training_session_issue
from app.technical_backtest import STRATEGIES, _simulate_strategy, run_backtests
from app.technical_data import (
    DataPaths,
    TechnicalStore,
    normalize_contract_registry,
    normalize_daily_frame,
    normalize_intraday_frame,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TechnicalSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_technical_config(
            PROJECT_ROOT / "config" / "technical_system.toml"
        )

    def test_session_filter_keeps_twenty_six_complete_bars_and_handles_dst(self) -> None:
        definition = self.config.build_contract_universe(
            date(2026, 3, 1), date(2026, 3, 31)
        )[0]
        start = datetime(2026, 3, 9, 7, 45)
        times = [start + timedelta(minutes=15 * index) for index in range(30)]
        raw = pl.DataFrame(
            {
                "time": times,
                "open": [1.0] * len(times),
                "high": [1.1] * len(times),
                "low": [0.9] * len(times),
                "close": [1.0] * len(times),
                "volume": [10.0] * len(times),
                "numEvents": [3.0] * len(times),
                "value": [10.0] * len(times),
            }
        )
        result = normalize_intraday_frame(
            raw, definition, self.config, event_type="TRADE"
        )
        self.assertEqual(result.height, 26)
        self.assertEqual(result["bar_start_et"].min().hour, 8)
        self.assertEqual(result["bar_start_et"].max().hour, 14)
        self.assertEqual(result["bar_start_et"].max().minute, 15)
        # March 9, 2026 is EDT: 08:00 New York is 12:00 UTC.
        self.assertEqual(result["timestamp_utc"].min().hour, 12)

    def test_configured_bloomberg_futures_use_terminal_year_convention(self) -> None:
        october_2026 = date(2026, 10, 1)
        self.assertEqual(self.config.roots["HO"].ticker(october_2026), "HOV6 Comdty")
        self.assertEqual(self.config.roots["CL"].ticker(october_2026), "CLV6 Comdty")
        self.assertEqual(self.config.roots["CO"].ticker(october_2026), "COV6 Comdty")
        self.assertEqual(self.config.roots["QS"].ticker(october_2026), "QSV6 Comdty")

    def test_expected_bloomberg_session_is_time_and_market_aware(self) -> None:
        zone = ZoneInfo("America/New_York")
        after_close = datetime(2026, 9, 1, 16, 0, tzinfo=zone)
        before_first_bar = datetime(2026, 9, 1, 8, 20, tzinfo=zone)
        kwargs = {
            "session_start": self.config.system.session_start,
            "bar_interval_minutes": self.config.system.bar_interval_minutes,
            "grace_minutes": self.config.bloomberg.freshness_grace_minutes,
        }
        self.assertEqual(
            expected_latest_exchange_session(
                date(2026, 9, 1), after_close, "HO", **kwargs
            ),
            date(2026, 9, 1),
        )
        self.assertEqual(
            expected_latest_exchange_session(
                date(2026, 9, 1), before_first_bar, "HO", **kwargs
            ),
            date(2026, 8, 31),
        )
        # August 31, 2026 is an ICE UK bank holiday but a normal NYMEX session.
        self.assertEqual(
            expected_latest_exchange_session(
                date(2026, 8, 31),
                datetime(2026, 8, 31, 16, 0, tzinfo=zone),
                "CO",
                **kwargs,
            ),
            date(2026, 8, 28),
        )

    def test_live_training_rejects_an_open_partial_session(self) -> None:
        session = date(2026, 9, 1)
        pulled_at = datetime(2026, 9, 1, 16, 0, tzinfo=ZoneInfo("UTC"))
        partial = pl.DataFrame(
            {
                "event_type": ["TRADE"] * 8,
                "session_date": [session] * 8,
                "pulled_at_utc": [pulled_at] * 8,
                "security": ["HOV6 Comdty"] * 8,
                "bar_slot": list(range(8)),
            }
        )
        issue = _live_training_session_issue(partial, self.config)
        self.assertIsNotNone(issue)
        self.assertIn("still open", str(issue))

    def test_backtest_worker_counts_produce_identical_results(self) -> None:
        feature_path = PROJECT_ROOT / "dist" / "technical_features.parquet"
        if not feature_path.is_file():
            self.skipTest("demo features have not been generated")
        features = pl.read_parquet(feature_path).filter(
            pl.col("spread_id").is_in(
                ["CAL.CL.M03_M04", "RV.CO_CL.M03"]
            )
        )
        if "session_last_slot" not in features.columns:
            self.skipTest("stored demo features predate the 15-minute engine")
        one_worker = replace(
            self.config,
            backtest=replace(self.config.backtest, parallel_workers=1),
            indicators=replace(
                self.config.indicators,
                candidate_risk_gates_enabled=True,
            ),
        )
        two_workers = replace(
            self.config,
            backtest=replace(self.config.backtest, parallel_workers=2),
            indicators=replace(
                self.config.indicators,
                candidate_risk_gates_enabled=True,
            ),
        )
        serial = run_backtests(features, one_worker)
        parallel = run_backtests(features, two_workers)
        self.assertTrue(serial.trades.equals(parallel.trades, null_equal=True))
        self.assertTrue(
            serial.scorecard.equals(parallel.scorecard, null_equal=True)
        )
        self.assertTrue(
            serial.fold_metrics.equals(parallel.fold_metrics, null_equal=True)
        )
        self.assertTrue(serial.equity.equals(parallel.equity, null_equal=True))

    def test_xbbg_v1_long_results_are_pivoted(self) -> None:
        definitions = self.config.build_contract_universe(
            date(2026, 8, 1), date(2026, 8, 31)
        )
        definition = definitions[0]
        reference = pl.DataFrame(
            {
                "ticker": [definition.ticker],
                "field": ["FUT_LAST_TRADE_DT"],
                "value": [definition.fallback_expiry.isoformat()],
            }
        )
        registry = normalize_contract_registry([definition], reference)
        self.assertTrue(registry["expiry_verified"][0])
        daily_long = pl.DataFrame(
            {
                "ticker": [definition.ticker] * 3,
                "date": [date(2026, 8, 28)] * 3,
                "field": ["PX_LAST", "PX_VOLUME", "FUT_AGGTE_OPEN_INT"],
                "value": ["2.25", "1000", "22000"],
            }
        )
        daily = normalize_daily_frame(daily_long, {definition.ticker: definition})
        self.assertEqual(daily.height, 1)
        self.assertAlmostEqual(daily["close"][0], 2.25)
        self.assertAlmostEqual(daily["volume_contracts"][0], 1000.0)
        self.assertAlmostEqual(daily["open_interest"][0], 22000.0)

    def test_demo_spreads_use_complete_package_rank_and_volume_capacity(self) -> None:
        paths = DataPaths.under(PROJECT_ROOT, dataset="demo")
        if not paths.bars.is_file():
            self.skipTest("demo fixture has not been generated")
        store = TechnicalStore(paths, self.config)
        bars = store.load_bars()
        daily = store.load_daily()
        contracts = store.load_contracts()
        prepared = prepare_trade_bars(bars, contracts, daily, self.config)
        spreads = build_spread_bars(prepared, self.config)
        self.assertEqual(spreads["spread_id"].n_unique(), len(self.config.spreads))
        self.assertTrue(
            {"HO_FLY_123", "QS_CONDOR_1234", "HOGO_CAL_BOX", "CRUDE_BASIS_BOX"}
            .issubset(set(spreads["spread_id"].unique().to_list()))
        )
        latest = spreads.sort("timestamp_utc").group_by("spread_id").tail(1)
        ho1 = latest.filter(pl.col("spread_id") == "HO1_CL1")
        ho2 = latest.filter(pl.col("spread_id") == "HO2_CL2")
        self.assertNotEqual(
            ho1["anchor_delivery_month"][0], ho2["anchor_delivery_month"][0]
        )
        qs = latest.filter(pl.col("spread_id") == "QS1_CO1")
        expected_capacity = min(
            qs["leg1_volume_contracts"][0] / 4.0,
            qs["leg2_volume_contracts"][0] / 3.0,
        )
        self.assertAlmostEqual(qs["package_volume_capacity"][0], expected_capacity)
        self.assertNotIn("spread_high", spreads.columns)
        self.assertNotIn("spread_low", spreads.columns)
        latest_scale_error = latest.select(
            (pl.col("research_close") - pl.col("spread_close")).abs().max()
        ).item()
        self.assertLessEqual(latest_scale_error, 1e-9)

    def test_roll_adjustment_is_continuous_and_anchored_to_latest_contract(self) -> None:
        frame = pl.DataFrame(
            {
                "timestamp_utc": [
                    datetime(2026, 1, 1, 12, tzinfo=ZoneInfo("UTC")),
                    datetime(2026, 1, 1, 13, tzinfo=ZoneInfo("UTC")),
                    datetime(2026, 1, 2, 12, tzinfo=ZoneInfo("UTC")),
                    datetime(2026, 1, 2, 13, tzinfo=ZoneInfo("UTC")),
                ],
                "roll_id": ["A", "A", "B", "B"],
                "spread_close": [10.0, 11.0, 20.0, 21.0],
            }
        )
        adjusted = _back_adjust_group(frame)
        self.assertEqual(adjusted["research_close"].to_list(), [19.0, 20.0, 20.0, 21.0])
        self.assertEqual(adjusted["research_close"][-1], adjusted["spread_close"][-1])

    def test_roll_adjustment_fails_closed_on_missing_roll_identity(self) -> None:
        frame = pl.DataFrame(
            {
                "spread_id": ["TEST", "TEST"],
                "timestamp_utc": [
                    datetime(2026, 1, 1, 12, tzinfo=ZoneInfo("UTC")),
                    datetime(2026, 1, 1, 13, tzinfo=ZoneInfo("UTC")),
                ],
                "roll_id": ["A", None],
                "spread_close": [10.0, 11.0],
            }
        )
        with self.assertRaisesRegex(ValueError, "roll_id cannot be null"):
            _back_adjust_group(frame)

    def test_dependent_structures_are_excluded_from_model_trials(self) -> None:
        dependent = [item for item in self.config.spreads if not item.model_enabled]
        self.assertGreaterEqual(len(dependent), 1)
        self.assertTrue(any(item.family == "Identity" for item in dependent))
        self.assertTrue(all(item.complexity_tier >= 3 for item in dependent))

    def test_forced_exit_session_is_present_but_entry_is_blocked(self) -> None:
        paths = DataPaths.under(PROJECT_ROOT, dataset="demo")
        if not paths.bars.is_file():
            self.skipTest("demo fixture has not been generated")
        store = TechnicalStore(paths, self.config)
        stored_bars = store.load_bars()
        if (
            stored_bars["bar_slot"].max()
            != self.config.system.complete_bars_per_session - 1
        ):
            self.skipTest("stored demo bars predate the 15-minute engine")
        prepared = prepare_trade_bars(
            stored_bars, store.load_contracts(), store.load_daily(), self.config
        )
        spreads = build_spread_bars(prepared, self.config)
        forced = spreads.filter(
            (pl.col("session_date") == pl.col("forced_exit_session"))
            & (
                pl.col("bar_slot")
                == self.config.system.complete_bars_per_session - 1
            )
        )
        self.assertGreater(forced.height, 0)
        self.assertFalse(forced["entry_allowed"].any())
        self.assertTrue(
            forced.select(
                (pl.col("blackout_start") > pl.col("session_date")).all()
            ).item()
        )

    def test_backtest_signals_fill_next_bar_and_mandatory_exit_is_at_d4(self) -> None:
        tz = ZoneInfo("UTC")
        first_session = date(2026, 8, 20)
        forced_session = date(2026, 8, 21)
        risk_date = date(2026, 8, 26)

        def row(index: int, *, forced: bool = False) -> dict[str, object]:
            session = forced_session if forced else first_session
            timestamp = datetime.combine(
                session,
                datetime.min.time().replace(
                    hour=14 if forced else 8,
                    minute=15 if forced else index * 15,
                ),
                tzinfo=tz,
            )
            return {
                "timestamp_utc": timestamp,
                "session_date": session,
                "bar_slot": (
                    self.config.system.complete_bars_per_session - 1
                    if forced
                    else index
                ),
                "session_last_slot": self.config.system.complete_bars_per_session - 1,
                "spread_id": "TEST",
                "spread_name": "Test spread",
                "spread_open": 10.0 + index * 0.1,
                "spread_close": 10.1 + index * 0.1,
                "research_close": 10.1 + index * 0.1,
                "package_open_value_usd": 1000.0 + index * 10.0,
                "package_close_value_usd": 1010.0 + index * 10.0,
                "one_way_cost_usd": 5.0,
                "roll_id": "A|B",
                "earliest_risk_date": risk_date,
                "forced_exit_session": forced_session,
                "entry_allowed": not forced,
                "robust_z": -2.1 if index == 0 else -1.0,
                "rsi": 30.0,
                "efficiency_ratio": 0.15,
                "relative_volume": 1.2,
                "package_volume_capacity": 100.0,
                "min_leg_events": 20.0,
                "max_leg_bid_ask_ticks": 2.0,
            }

        frame = pl.DataFrame([row(0), row(1), row(2, forced=True)]).sort(
            "timestamp_utc"
        )
        trades, _equity = _simulate_strategy(
            frame, STRATEGIES[0], self.config, [], None
        )
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertLess(trade["entry_signal_time"], trade["entry_time"])
        self.assertEqual(trade["exit_reason"], "MANDATORY_D4_EXIT")
        self.assertEqual(trade["exit_session"], forced_session)
        self.assertEqual(trade["exit_time"].hour, 14)


if __name__ == "__main__":
    unittest.main()
