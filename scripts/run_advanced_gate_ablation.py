#!/usr/bin/env python
"""Reproduce the disabled-versus-enabled candidate risk-gate ablation.

The production configuration keeps the candidate gates disabled.  This script
uses the already-built feature parquet and changes only that one configuration
flag, so both backtests share identical data, windows, strategies, and costs.
The final lockbox is reported for completeness but is never an input to the
promotion decision.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter
from typing import Any, Mapping, Sequence

import polars as pl


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.technical_backtest import (  # noqa: E402
    BACKTEST_COLUMNS,
    BacktestResult,
    run_backtests,
)
from app.technical_config import load_technical_config  # noqa: E402


AGGREGATE_SCORECARD_FIELDS: tuple[str, ...] = (
    "trades",
    "oos_trades",
    "net_pnl_usd",
    "net_pnl_2x_cost_usd",
    "net_pnl_3x_cost_usd",
    "lockbox_net_pnl_usd",
)
COMPARISON_TOLERANCE = 1e-6
MINIMUM_DRAWDOWN_IMPROVEMENT_SHARE = 0.60
MAXIMUM_2X_COST_NET_DECLINE = 0.10
MINIMUM_QUALIFYING_ECONOMIC_FAMILIES = 2


def _finite_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def summarize_backtest(result: BacktestResult) -> dict[str, Any]:
    """Return compact, JSON-safe totals without assuming every score field exists."""

    scorecard = result.scorecard
    aggregates: dict[str, float | int] = {}
    for field in AGGREGATE_SCORECARD_FIELDS:
        if field not in scorecard.columns:
            continue
        total = scorecard.select(pl.col(field).fill_null(0).sum()).item()
        if field in {"trades", "oos_trades"}:
            aggregates[field] = int(total or 0)
        else:
            aggregates[field] = _finite_float(total)
    return {
        "total_trials": scorecard.height,
        "total_trade_rows": result.trades.height,
        "scorecard_aggregates": aggregates,
    }


def _keyed_rows(
    frame: pl.DataFrame,
    keys: Sequence[str],
) -> dict[tuple[object, ...], Mapping[str, Any]]:
    if frame.is_empty():
        return {}
    missing = [key for key in keys if key not in frame.columns]
    if missing:
        raise ValueError(f"Comparison frame is missing keys: {', '.join(missing)}")
    rows: dict[tuple[object, ...], Mapping[str, Any]] = {}
    for row in frame.to_dicts():
        key = tuple(row[item] for item in keys)
        if key in rows:
            raise ValueError(f"Comparison keys are not unique: {key!r}")
        rows[key] = row
    return rows


def compare_joined_frames(
    baseline: pl.DataFrame,
    candidate: pl.DataFrame,
    *,
    keys: Sequence[str],
    metrics: Mapping[str, bool],
    tolerance: float = COMPARISON_TOLERANCE,
) -> dict[str, Any]:
    """Outer-join two result frames and count metric outcomes.

    ``metrics`` maps a column name to whether lower values are better.  Missing
    rows are compared as zero and are also disclosed separately, preventing a
    vanished trial or fold from being silently discarded.
    """

    baseline_rows = _keyed_rows(baseline, keys)
    candidate_rows = _keyed_rows(candidate, keys)
    all_keys = sorted(set(baseline_rows) | set(candidate_rows), key=repr)
    metric_results: dict[str, dict[str, int | float | None]] = {}
    for metric, lower_is_better in metrics.items():
        counts = {"improved": 0, "worse": 0, "same": 0}
        for key in all_keys:
            baseline_value = _finite_float(baseline_rows.get(key, {}).get(metric))
            candidate_value = _finite_float(candidate_rows.get(key, {}).get(metric))
            delta = candidate_value - baseline_value
            if abs(delta) <= tolerance:
                counts["same"] += 1
            elif (delta < 0) if lower_is_better else (delta > 0):
                counts["improved"] += 1
            else:
                counts["worse"] += 1
        changed = counts["improved"] + counts["worse"]
        metric_results[metric] = {
            **counts,
            "changed": changed,
            "improvement_share_of_changed": (
                counts["improved"] / changed if changed else None
            ),
        }
    return {
        "keys": list(keys),
        "joined_rows": len(all_keys),
        "baseline_only_rows": len(set(baseline_rows) - set(candidate_rows)),
        "candidate_only_rows": len(set(candidate_rows) - set(baseline_rows)),
        "metrics": metric_results,
    }


def _family_drawdown_outcomes(
    baseline_folds: pl.DataFrame,
    candidate_folds: pl.DataFrame,
    family_by_spread: Mapping[str, str],
    *,
    tolerance: float = COMPARISON_TOLERANCE,
) -> dict[str, Any]:
    keys = ("spread_id", "strategy_id", "fold")
    baseline_rows = _keyed_rows(baseline_folds, keys)
    candidate_rows = _keyed_rows(candidate_folds, keys)
    outcomes: dict[str, dict[str, int]] = {}
    for key in sorted(set(baseline_rows) | set(candidate_rows), key=repr):
        family = family_by_spread.get(str(key[0]))
        if not family:
            continue
        baseline_value = _finite_float(
            baseline_rows.get(key, {}).get("max_drawdown_usd")
        )
        candidate_value = _finite_float(
            candidate_rows.get(key, {}).get("max_drawdown_usd")
        )
        delta = candidate_value - baseline_value
        counts = outcomes.setdefault(family, {"improved": 0, "worse": 0, "same": 0})
        if abs(delta) <= tolerance:
            counts["same"] += 1
        elif delta < 0:
            counts["improved"] += 1
        else:
            counts["worse"] += 1
    rows: list[dict[str, Any]] = []
    qualifying: list[str] = []
    for family in sorted(outcomes):
        counts = outcomes[family]
        changed = counts["improved"] + counts["worse"]
        share = counts["improved"] / changed if changed else None
        qualifies = bool(
            changed
            and share is not None
            and share >= MINIMUM_DRAWDOWN_IMPROVEMENT_SHARE
        )
        if qualifies:
            qualifying.append(family)
        rows.append(
            {
                "family": family,
                **counts,
                "changed": changed,
                "improvement_share_of_changed": share,
                "qualifies_at_60_percent": qualifies,
            }
        )
    return {
        "families": rows,
        "qualifying_families": qualifying,
        "qualifying_family_count": len(qualifying),
    }


def _relative_change(candidate: float, baseline: float) -> float | None:
    if abs(baseline) <= COMPARISON_TOLERANCE:
        return None
    return (candidate - baseline) / abs(baseline)


def evaluate_promotion(
    *,
    baseline_2x_cost_net_usd: float | None,
    candidate_2x_cost_net_usd: float | None,
    fold_net_outcomes: Mapping[str, object],
    fold_drawdown_outcomes: Mapping[str, object],
    qualifying_family_count: int,
) -> dict[str, Any]:
    """Apply the preregistered conservative gate-promotion policy.

    Lockbox P&L is intentionally absent from this function's inputs.  It is
    evaluation-only and therefore cannot alter the result.
    """

    net_improved = int(fold_net_outcomes.get("improved") or 0)
    net_worse = int(fold_net_outcomes.get("worse") or 0)
    net_changed = net_improved + net_worse
    net_share = net_improved / net_changed if net_changed else None

    drawdown_improved = int(fold_drawdown_outcomes.get("improved") or 0)
    drawdown_worse = int(fold_drawdown_outcomes.get("worse") or 0)
    drawdown_changed = drawdown_improved + drawdown_worse
    drawdown_share = (
        drawdown_improved / drawdown_changed if drawdown_changed else None
    )

    cost_values_available = (
        baseline_2x_cost_net_usd is not None
        and candidate_2x_cost_net_usd is not None
    )
    baseline_cost = _finite_float(baseline_2x_cost_net_usd) if cost_values_available else 0.0
    candidate_cost = _finite_float(candidate_2x_cost_net_usd) if cost_values_available else 0.0
    cost_change = (
        _relative_change(candidate_cost, baseline_cost)
        if cost_values_available
        else None
    )
    if not cost_values_available:
        cost_passed = False
    elif cost_change is None:
        cost_passed = candidate_cost >= -COMPARISON_TOLERANCE
    else:
        cost_passed = cost_change >= -MAXIMUM_2X_COST_NET_DECLINE

    criteria = {
        "broad_fold_net_improvement": {
            "passed": bool(net_changed and net_improved > net_worse),
            "observed_improved": net_improved,
            "observed_worse": net_worse,
            "observed_improvement_share": net_share,
            "requirement": "strict majority of changed folds",
        },
        "aggregate_2x_cost_net_retention": {
            "passed": cost_passed,
            "baseline_usd": baseline_2x_cost_net_usd,
            "candidate_usd": candidate_2x_cost_net_usd,
            "relative_change": cost_change,
            "requirement": "decline no greater than 10 percent",
        },
        "fold_drawdown_improvement": {
            "passed": bool(
                drawdown_changed
                and drawdown_share is not None
                and drawdown_share >= MINIMUM_DRAWDOWN_IMPROVEMENT_SHARE
            ),
            "observed_improved": drawdown_improved,
            "observed_worse": drawdown_worse,
            "observed_improvement_share": drawdown_share,
            "requirement": "lower max drawdown in at least 60 percent of changed folds",
        },
        "economic_family_breadth": {
            "passed": qualifying_family_count
            >= MINIMUM_QUALIFYING_ECONOMIC_FAMILIES,
            "observed_qualifying_families": qualifying_family_count,
            "requirement": "at least two families individually meet the 60 percent drawdown test",
        },
    }
    failed = [name for name, row in criteria.items() if not row["passed"]]
    promote = not failed
    return {
        "decision": "PROMOTE" if promote else "REJECT",
        "promote": promote,
        "lockbox_not_used_for_selection": True,
        "criteria": criteria,
        "failed_criteria": failed,
        "rationale": (
            "All preregistered development-fold and cost-stress criteria passed."
            if promote
            else "Candidate gates remain diagnostic because these preregistered criteria failed: "
            + ", ".join(failed)
            + "."
        ),
    }


def build_ablation_report(
    baseline: BacktestResult,
    candidate: BacktestResult,
    *,
    family_by_spread: Mapping[str, str],
) -> dict[str, Any]:
    baseline_summary = summarize_backtest(baseline)
    candidate_summary = summarize_backtest(candidate)
    trial_comparison = compare_joined_frames(
        baseline.scorecard,
        candidate.scorecard,
        keys=("trial_id",),
        metrics={"net_pnl_usd": False},
    )
    fold_comparison = compare_joined_frames(
        baseline.fold_metrics,
        candidate.fold_metrics,
        keys=("spread_id", "strategy_id", "fold"),
        metrics={"net_pnl_usd": False, "max_drawdown_usd": True},
    )
    family_outcomes = _family_drawdown_outcomes(
        baseline.fold_metrics,
        candidate.fold_metrics,
        family_by_spread,
    )
    baseline_aggregates = baseline_summary["scorecard_aggregates"]
    candidate_aggregates = candidate_summary["scorecard_aggregates"]
    deltas: dict[str, float | int] = {
        "total_trials": candidate_summary["total_trials"]
        - baseline_summary["total_trials"],
        "total_trade_rows": candidate_summary["total_trade_rows"]
        - baseline_summary["total_trade_rows"],
    }
    for field in sorted(set(baseline_aggregates) | set(candidate_aggregates)):
        deltas[field] = _finite_float(candidate_aggregates.get(field)) - _finite_float(
            baseline_aggregates.get(field)
        )
    promotion = evaluate_promotion(
        baseline_2x_cost_net_usd=baseline_aggregates.get(
            "net_pnl_2x_cost_usd"
        ),
        candidate_2x_cost_net_usd=candidate_aggregates.get(
            "net_pnl_2x_cost_usd"
        ),
        fold_net_outcomes=fold_comparison["metrics"]["net_pnl_usd"],
        fold_drawdown_outcomes=fold_comparison["metrics"]["max_drawdown_usd"],
        qualifying_family_count=family_outcomes["qualifying_family_count"],
    )
    return {
        "runs": {
            "candidate_gates_disabled": baseline_summary,
            "candidate_gates_enabled": candidate_summary,
        },
        "comparison": {
            "candidate_minus_disabled": deltas,
            "trials": trial_comparison,
            "folds": fold_comparison,
            "fold_drawdown_by_economic_family": family_outcomes,
        },
        "promotion_decision": promotion,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--features", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = args.project_root.resolve()
    feature_path = (args.features or root / "dist" / "technical_features.parquet").resolve()
    config_path = (args.config or root / "config" / "technical_system.toml").resolve()
    output_path = (args.output or root / "dist" / "advanced_gate_ablation.json").resolve()
    if not feature_path.is_file():
        raise FileNotFoundError(f"Feature parquet not found: {feature_path}")
    config = load_technical_config(config_path)
    schema = pl.read_parquet_schema(feature_path)
    required_columns = tuple(dict.fromkeys(("model_enabled", *BACKTEST_COLUMNS)))
    missing = sorted(set(required_columns) - set(schema.names()))
    if missing:
        raise ValueError(
            "Feature parquet is missing ablation columns: " + ", ".join(missing)
        )
    features = pl.scan_parquet(feature_path).select(required_columns).collect()
    disabled_config = replace(
        config,
        indicators=replace(
            config.indicators,
            candidate_risk_gates_enabled=False,
        ),
    )
    enabled_config = replace(
        config,
        indicators=replace(
            config.indicators,
            candidate_risk_gates_enabled=True,
        ),
    )

    disabled_started = perf_counter()
    disabled = run_backtests(features, disabled_config)
    disabled_seconds = perf_counter() - disabled_started
    enabled_started = perf_counter()
    enabled = run_backtests(features, enabled_config)
    enabled_seconds = perf_counter() - enabled_started

    report = build_ablation_report(
        disabled,
        enabled,
        family_by_spread={item.spread_id: item.family for item in config.spreads},
    )
    payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": {
            "baseline": "candidate_risk_gates_enabled=false",
            "candidate": "candidate_risk_gates_enabled=true",
            "only_configuration_difference": "indicators.candidate_risk_gates_enabled",
            "lockbox_not_used_for_selection": True,
        },
        "source": {
            "features_path": str(feature_path),
            "features_sha256": _sha256(feature_path),
            "features_rows": features.height,
            "config_path": str(config_path),
            "config_sha256": _sha256(config_path),
        },
        "runtime_seconds": {
            "candidate_gates_disabled": round(disabled_seconds, 6),
            "candidate_gates_enabled": round(enabled_seconds, 6),
        },
        "policy": {
            "minimum_fold_drawdown_improvement_share": MINIMUM_DRAWDOWN_IMPROVEMENT_SHARE,
            "maximum_aggregate_2x_cost_net_decline": MAXIMUM_2X_COST_NET_DECLINE,
            "minimum_qualifying_economic_families": MINIMUM_QUALIFYING_ECONOMIC_FAMILIES,
            "requires_strict_fold_net_improvement_majority": True,
            "lockbox_not_used_for_selection": True,
        },
        **report,
    }
    _write_json_atomic(output_path, payload)
    decision = payload["promotion_decision"]
    print(
        json.dumps(
            {
                "output": str(output_path),
                "decision": decision["decision"],
                "failed_criteria": decision["failed_criteria"],
                "lockbox_not_used_for_selection": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
