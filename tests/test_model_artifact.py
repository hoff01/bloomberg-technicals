from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import os
import tempfile
import unittest

import polars as pl

from scripts.prune_model_releases import prune

from app.model_artifact import (
    build_model_artifact,
    load_model_artifact,
    write_model_artifact,
)
from app.technical_backtest import (
    BASE_EXPERT_IDS,
    apply_frozen_expert_model,
    build_live_signal_board,
)
from app.technical_config import load_technical_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]

ADVANCED_BOARD_COLUMNS = {
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
    "advanced_risk_regime",
    "relationship_health_scope",
}
PATTERN_BOARD_COLUMNS = {
    "pattern_state",
    "pattern_strength",
    "pattern_agreement",
    "pattern_components",
}


class ModelArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_technical_config(
            PROJECT_ROOT / "config" / "technical_system.toml"
        )

    def _feature_row(self) -> dict[str, object]:
        timestamp = datetime(2026, 8, 31, 18, tzinfo=timezone.utc)
        return {
            "spread_id": "TEST_SPREAD",
            "spread_name": "Test spread",
            "spread_family": "Crack",
            "tenor_bucket": "FRONT",
            "timestamp_utc": timestamp,
            "session_date": date(2026, 8, 31),
            "bar_slot": 12,
            "session_last_slot": 25,
            "model_enabled": True,
            "complexity_tier": 1,
            "algebra_group": "TEST_SPREAD",
            "research_close": 8.0,
            "spread_close": 8.0,
            "rolling_median": 10.0,
            "rolling_mad": 1.0,
            "robust_z": -2.0,
            "rsi": 30.0,
            "efficiency_ratio": 0.2,
            "donchian_high": 12.0,
            "donchian_low": 7.0,
            "macd_histogram": -0.1,
            "relative_volume": 1.2,
            "pvo": 0.1,
            "tod_normalized_change": 0.25,
            "vol_regime_ratio_1d_20d": 1.1,
            "liquidity_stress_ratio": 0.9,
            "tail_event_rate_20d": 0.05,
            "robust_volume_surprise": 0.4,
            "return_skew_5d": -0.2,
            "return_excess_kurtosis_5d": 0.5,
            "realized_vol_of_vol_5d": 0.02,
            "trend_hac_t_stat_3d": 1.5,
            "close_path_choppiness_5d": 60.0,
            "advanced_risk_regime": "NORMAL",
            "relationship_health_scope": "TWO_LEG",
            "bollinger_width": 0.2,
            "bollinger_width_p20": 0.2,
            "session_vwap_proxy": 8.0,
            "ewma_abs_change": 0.5,
            "seasonal_z": -1.0,
            "seasonal_n": 20,
            "seasonal_move_z": 0.0,
            "seasonal_prior_years": 3,
            "seasonal_confidence": 0.5,
            "mean_reversion_stability": 0.6,
            "variance_ratio_5": 0.5,
            "signed_volume_imbalance_proxy": 0.0,
            "effort_vs_result": 0.6,
            "change_point_alarm": False,
            "package_volume_capacity": 100.0,
            "min_leg_events": 20.0,
            "max_leg_bid_ask_ticks": 2.0,
            "package_barrels": 1000.0,
            "one_way_cost_usd": 10.0,
            "entry_allowed": True,
            "earliest_risk_date": date(2026, 10, 1),
            "forced_exit_session": date(2026, 9, 25),
            "sessions_to_risk_date": 22,
            "roll_id": "A|B",
            "regime": "MEAN_REVERTING",
        }

    def test_model_release_pruning_keeps_recent_and_referenced_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mode_root = root / "models" / "demo"
            releases = mode_root / "releases"
            releases.mkdir(parents=True)
            for index in range(7):
                release = releases / f"release-{index}"
                release.mkdir()
                os.utime(release, (index + 1, index + 1))
            for name, release_id in (
                ("latest_model.json", "release-6"),
                ("last_known_good_model.json", "release-5"),
            ):
                (mode_root / name).write_text(
                    json.dumps(
                        {
                            "training_release_files": {
                                "seasonality": (
                                    f"releases/{release_id}/seasonality.csv"
                                )
                            }
                        }
                    ),
                    encoding="utf-8",
                )
            removed = prune(root, "demo", keep=3)
            self.assertEqual(len(removed), 4)
            self.assertEqual(
                {path.name for path in releases.iterdir()},
                {"release-4", "release-5", "release-6"},
            )

    def _expert_group(self) -> dict[str, object]:
        weights = {expert: 0.10 for expert in BASE_EXPERT_IDS}
        weights["ROBUST_MEAN_REVERSION"] = 0.25
        weights["STABILITY_REVERSION"] = 0.25
        return {
            "learner_group": "Crack|FRONT",
            "adaptive_observations": 100,
            "adaptive_top_expert": "ROBUST_MEAN_REVERSION",
            "adaptive_top_weight": 0.25,
            **{
                f"adaptive_weight_{expert.lower()}": value
                for expert, value in weights.items()
            },
        }

    def test_frozen_weights_score_without_learning(self) -> None:
        artifact = {"expert_groups": [self._expert_group()]}
        scored = apply_frozen_expert_model(
            pl.DataFrame([self._feature_row()]), artifact, self.config
        )
        self.assertEqual(scored["adaptive_status"][0], "FROZEN_MODEL_SCORE")
        self.assertEqual(scored["adaptive_vote"][0], 1)
        self.assertAlmostEqual(scored["adaptive_score"][0], 0.5)

    def test_validated_per_structure_strategy_drives_current_direction(self) -> None:
        artifact = {"expert_groups": [self._expert_group()]}
        scored = apply_frozen_expert_model(
            pl.DataFrame([self._feature_row()]), artifact, self.config
        )
        scorecard = pl.DataFrame(
            [
                {
                    "spread_id": "TEST_SPREAD",
                    "strategy_id": "ROBUST_MEAN_REVERSION",
                    "strategy_name": "Robust Mean Reversion",
                    "status": "VALIDATED",
                    "deflated_sharpe_probability": 0.99,
                    "profitable_fold_share": 0.75,
                    "daily_sharpe": 1.2,
                    "net_pnl_usd": 1000.0,
                }
            ]
        )
        board = build_live_signal_board(
            scored,
            scorecard,
            self.config,
            depth_source="BPIPE_L2",
            quality_blocking=False,
            demo_mode=False,
        )
        self.assertEqual(board["signal_strategy_id"][0], "ROBUST_MEAN_REVERSION")
        self.assertTrue(board["direction_evidence_validated"][0])
        stale = build_live_signal_board(
            scored,
            scorecard,
            self.config,
            depth_source="BPIPE_L2",
            quality_blocking=False,
            demo_mode=False,
            model_stale=True,
            model_age_sessions=6,
        )
        self.assertEqual(stale["status"][0], "MODEL STALE")
        self.assertTrue(stale["model_stale"][0])
        self.assertEqual(stale["model_age_sessions"][0], 6)

    def test_advanced_shock_and_volume_dryness_remain_diagnostic(self) -> None:
        row = self._feature_row()
        row["tod_normalized_change"] = self.config.indicators.extreme_tod_shock_z
        row["robust_volume_surprise"] = 0.0
        shock = build_live_signal_board(
            pl.DataFrame([row]),
            pl.DataFrame(),
            self.config,
            depth_source="BAR_PROXY_ONLY",
            quality_blocking=False,
            demo_mode=False,
        )
        self.assertEqual(shock["liquidity_gate"][0], "PASS")

        row["tod_normalized_change"] = 0.0
        row["robust_volume_surprise"] = self.config.indicators.volume_dryness_z
        dry = build_live_signal_board(
            pl.DataFrame([row]),
            pl.DataFrame(),
            self.config,
            depth_source="BAR_PROXY_ONLY",
            quality_blocking=False,
            demo_mode=False,
        )
        self.assertEqual(dry["liquidity_gate"][0], "PASS")
        self.assertTrue(ADVANCED_BOARD_COLUMNS.issubset(dry.columns))
        self.assertTrue(PATTERN_BOARD_COLUMNS.issubset(dry.columns))

    def test_candidate_risk_gate_switch_is_explicit_and_consistent(self) -> None:
        enabled = replace(
            self.config,
            indicators=replace(
                self.config.indicators,
                candidate_risk_gates_enabled=True,
            ),
        )
        row = self._feature_row()
        row["tod_normalized_change"] = enabled.indicators.extreme_tod_shock_z
        shock = build_live_signal_board(
            pl.DataFrame([row]),
            pl.DataFrame(),
            enabled,
            depth_source="BAR_PROXY_ONLY",
            quality_blocking=False,
            demo_mode=False,
        )
        self.assertEqual(shock["liquidity_gate"][0], "EXTREME_TOD_SHOCK")

        row["tod_normalized_change"] = 0.0
        row["robust_volume_surprise"] = enabled.indicators.volume_dryness_z
        dry = build_live_signal_board(
            pl.DataFrame([row]),
            pl.DataFrame(),
            enabled,
            depth_source="BAR_PROXY_ONLY",
            quality_blocking=False,
            demo_mode=False,
        )
        self.assertEqual(dry["liquidity_gate"][0], "ROBUST_VOLUME_DRYNESS")

    def test_artifact_freezes_pre_lockbox_weights_and_round_trips(self) -> None:
        start = date(2026, 1, 1)
        group = self._expert_group()
        rows = []
        for index in range(35):
            session = start + timedelta(days=index)
            rows.append(
                {
                    "spread_id": "TEST_SPREAD",
                    "session_date": session,
                    "timestamp_utc": datetime.combine(
                        session,
                        datetime.min.time().replace(hour=18),
                        tzinfo=timezone.utc,
                    ),
                    **group,
                }
            )
        scorecard = pl.DataFrame(
            [
                {
                    "spread_id": "TEST_SPREAD",
                    "strategy_id": "ROBUST_MEAN_REVERSION",
                    "strategy_name": "Robust Mean Reversion",
                    "status": "VALIDATED",
                    "deflated_sharpe_probability": 0.99,
                    "profitable_fold_share": 0.75,
                    "daily_sharpe": 1.2,
                    "net_pnl_usd": 1000.0,
                    "oos_trades": 30,
                }
            ]
        )
        artifact = build_model_artifact(
            config=self.config,
            features=pl.DataFrame(rows),
            scorecard=scorecard,
            mode="LIVE",
            as_of=start + timedelta(days=34),
        )
        self.assertEqual(artifact["training_window"]["lockbox_sessions"], 30)
        self.assertEqual(artifact["training_window"]["development_end"], "2026-01-05")
        self.assertEqual(artifact["validated_strategy_count"], 1)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_model_artifact(Path(temp_dir) / "model.json", artifact)
            loaded = load_model_artifact(
                path, config=self.config, expected_mode="LIVE"
            )
        self.assertEqual(loaded["model_id"], artifact["model_id"])


if __name__ == "__main__":
    unittest.main()
