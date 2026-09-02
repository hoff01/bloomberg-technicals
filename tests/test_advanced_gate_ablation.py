from __future__ import annotations

import unittest

import polars as pl

from app.technical_backtest import BacktestResult
from scripts.run_advanced_gate_ablation import (
    build_ablation_report,
    compare_joined_frames,
    evaluate_promotion,
    summarize_backtest,
)


def _result(
    *,
    trade_rows: int,
    scorecard: list[dict[str, object]],
    folds: list[dict[str, object]],
) -> BacktestResult:
    return BacktestResult(
        trades=pl.DataFrame({"row": range(trade_rows)}),
        scorecard=pl.DataFrame(scorecard),
        fold_metrics=pl.DataFrame(folds),
        equity=pl.DataFrame(),
        strategy_library=pl.DataFrame(),
    )


class AdvancedGateAblationTests(unittest.TestCase):
    def test_summary_reports_available_scorecard_aggregates(self) -> None:
        result = _result(
            trade_rows=7,
            scorecard=[
                {
                    "trial_id": "A|S",
                    "trades": 3,
                    "oos_trades": 3,
                    "net_pnl_usd": 10.0,
                    "net_pnl_2x_cost_usd": 8.0,
                    "lockbox_net_pnl_usd": -2.0,
                },
                {
                    "trial_id": "B|S",
                    "trades": 2,
                    "oos_trades": 2,
                    "net_pnl_usd": 4.0,
                    "net_pnl_2x_cost_usd": 1.0,
                    "lockbox_net_pnl_usd": 9.0,
                },
            ],
            folds=[],
        )
        summary = summarize_backtest(result)
        self.assertEqual(summary["total_trials"], 2)
        self.assertEqual(summary["total_trade_rows"], 7)
        self.assertEqual(summary["scorecard_aggregates"]["oos_trades"], 5)
        self.assertEqual(summary["scorecard_aggregates"]["net_pnl_usd"], 14.0)
        self.assertNotIn(
            "net_pnl_3x_cost_usd", summary["scorecard_aggregates"]
        )

    def test_joined_fold_comparison_uses_lower_drawdown_as_improvement(self) -> None:
        baseline = pl.DataFrame(
            [
                {"spread_id": "A", "strategy_id": "S", "fold": 1, "net_pnl_usd": 10.0, "max_drawdown_usd": 8.0},
                {"spread_id": "B", "strategy_id": "S", "fold": 1, "net_pnl_usd": 5.0, "max_drawdown_usd": 4.0},
                {"spread_id": "C", "strategy_id": "S", "fold": 1, "net_pnl_usd": 2.0, "max_drawdown_usd": 3.0},
            ]
        )
        candidate = pl.DataFrame(
            [
                {"spread_id": "A", "strategy_id": "S", "fold": 1, "net_pnl_usd": 12.0, "max_drawdown_usd": 7.0},
                {"spread_id": "B", "strategy_id": "S", "fold": 1, "net_pnl_usd": 4.0, "max_drawdown_usd": 5.0},
                {"spread_id": "C", "strategy_id": "S", "fold": 1, "net_pnl_usd": 2.0, "max_drawdown_usd": 3.0},
            ]
        )
        comparison = compare_joined_frames(
            baseline,
            candidate,
            keys=("spread_id", "strategy_id", "fold"),
            metrics={"net_pnl_usd": False, "max_drawdown_usd": True},
        )
        self.assertEqual(
            comparison["metrics"]["net_pnl_usd"],
            {
                "improved": 1,
                "worse": 1,
                "same": 1,
                "changed": 2,
                "improvement_share_of_changed": 0.5,
            },
        )
        self.assertEqual(comparison["metrics"]["max_drawdown_usd"]["improved"], 1)
        self.assertEqual(comparison["metrics"]["max_drawdown_usd"]["worse"], 1)

    def test_promotion_is_conservative_and_never_uses_lockbox(self) -> None:
        passed = evaluate_promotion(
            baseline_2x_cost_net_usd=100.0,
            candidate_2x_cost_net_usd=91.0,
            fold_net_outcomes={"improved": 4, "worse": 2, "same": 1},
            fold_drawdown_outcomes={"improved": 3, "worse": 2, "same": 2},
            qualifying_family_count=2,
        )
        self.assertTrue(passed["promote"])
        self.assertTrue(passed["lockbox_not_used_for_selection"])

        rejected = evaluate_promotion(
            baseline_2x_cost_net_usd=100.0,
            candidate_2x_cost_net_usd=89.0,
            fold_net_outcomes={"improved": 24, "worse": 47, "same": 10},
            fold_drawdown_outcomes={"improved": 9, "worse": 12, "same": 60},
            qualifying_family_count=1,
        )
        self.assertFalse(rejected["promote"])
        self.assertEqual(rejected["decision"], "REJECT")
        self.assertEqual(
            set(rejected["failed_criteria"]),
            {
                "broad_fold_net_improvement",
                "aggregate_2x_cost_net_retention",
                "fold_drawdown_improvement",
                "economic_family_breadth",
            },
        )

    def test_report_keeps_lockbox_aggregate_but_excludes_it_from_decision(self) -> None:
        baseline_score = [
            {
                "trial_id": "A|S",
                "trades": 2,
                "oos_trades": 2,
                "net_pnl_usd": 100.0,
                "net_pnl_2x_cost_usd": 100.0,
                "net_pnl_3x_cost_usd": 90.0,
                "lockbox_net_pnl_usd": 1.0,
            }
        ]
        candidate_score = [
            {
                **baseline_score[0],
                "net_pnl_usd": 110.0,
                "net_pnl_2x_cost_usd": 95.0,
                "lockbox_net_pnl_usd": -1_000_000.0,
            }
        ]
        baseline_folds: list[dict[str, object]] = []
        candidate_folds: list[dict[str, object]] = []
        for spread, family in (("A", "CRACK"), ("B", "TIME")):
            del family
            for fold in range(3):
                baseline_folds.append(
                    {"spread_id": spread, "strategy_id": "S", "fold": fold, "net_pnl_usd": 10.0, "max_drawdown_usd": 10.0}
                )
                candidate_folds.append(
                    {"spread_id": spread, "strategy_id": "S", "fold": fold, "net_pnl_usd": 11.0, "max_drawdown_usd": 9.0}
                )
        report = build_ablation_report(
            _result(trade_rows=8, scorecard=baseline_score, folds=baseline_folds),
            _result(trade_rows=8, scorecard=candidate_score, folds=candidate_folds),
            family_by_spread={"A": "CRACK", "B": "TIME"},
        )
        self.assertEqual(
            report["runs"]["candidate_gates_enabled"]["scorecard_aggregates"][
                "lockbox_net_pnl_usd"
            ],
            -1_000_000.0,
        )
        self.assertTrue(report["promotion_decision"]["promote"])
        self.assertTrue(
            report["promotion_decision"]["lockbox_not_used_for_selection"]
        )


if __name__ == "__main__":
    unittest.main()
