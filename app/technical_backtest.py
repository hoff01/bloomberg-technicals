"""Leakage-aware, leg-economic backtests and current signal targets.

Signals are formed at a 15-minute bar close and filled at the next eligible bar
open.  P&L is calculated from the actual contract package values, not by
multiplying a synthetic USD/bbl spread by an arbitrary multiplier.
"""

from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import date, datetime
import math
import os
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import polars as pl

from app.technical_config import TechnicalConfig
from app.technical_labels import trade_code_fields


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    strategy_id: str
    display_name: str
    family: str
    thesis: str
    entry_rule: str
    exit_rule: str
    maximum_holding_bars: int
    parameter_summary: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: pl.DataFrame
    scorecard: pl.DataFrame
    fold_metrics: pl.DataFrame
    equity: pl.DataFrame
    strategy_library: pl.DataFrame


STRATEGIES: tuple[StrategyDefinition, ...] = (
    StrategyDefinition(
        "ROBUST_MEAN_REVERSION",
        "Robust Mean Reversion",
        "Mean reversion",
        "Temporary crack/spread dislocations revert when trend efficiency is low.",
        "Prior-only median/MAD z beyond ±1.8, RSI confirmation, non-trending regime, liquidity pass.",
        "z returns inside ±0.20, adverse z reaches 3.25, or 52 bars.",
        52,
        "z=1.8; stop_z=3.25; RSI=42/58; max_hold=52",
    ),
    StrategyDefinition(
        "TREND_BREAKOUT",
        "Liquidity-Confirmed Breakout",
        "Trend",
        "Persistent refinery-margin repricing continues after a prior-range break.",
        "Close breaks shifted 78-bar Donchian range; EMA/MACD aligned; efficiency and volume confirm.",
        "Slow-EMA reversal, opposite MACD, or 78 bars.",
        78,
        "Donchian=78; EMA=26/78; rel_volume>=1.0; max_hold=78",
    ),
    StrategyDefinition(
        "VOLATILITY_SQUEEZE",
        "Volatility Squeeze Release",
        "Volatility",
        "A compressed spread range can release into a directional move when flow expands.",
        "Recent Bollinger width below its prior 20th percentile, then Donchian break with PVO/volume confirmation.",
        "Middle-band reversal, 2.5 close-volatility units, or 52 bars.",
        52,
        "width_q=20%; lookback=260; breakout=78; max_hold=52",
    ),
    StrategyDefinition(
        "SESSION_VWAP_REVERSION",
        "Session VWAP Reversion",
        "Intraday mean reversion",
        "Intraday deviations from a synchronized leg-volume-weighted fair value often mean-revert.",
        "Spread is >1.5 close-volatility units from the session VWAP proxy before the final bar.",
        "VWAP touch, final 14:15 signal, or 26 bars.",
        26,
        "deviation=1.5 vol; same-session only; max_hold=26",
    ),
    StrategyDefinition(
        "ERROR_CORRECTION_RESIDUAL",
        "Seasonal Error-Correction Residual",
        "Structure / seasonality",
        "The raw tradable spread corrects toward a point-in-time prior-year seasonal equilibrium.",
        "Seasonal residual z beyond ±1.6 with robust-z confirmation and adequate prior-year sample.",
        "Residual inside ±0.35, robust-z reversal, or 78 bars.",
        78,
        "seasonal_z=1.6; min seasonal n=8; max_hold=78",
    ),
    StrategyDefinition(
        "STABILITY_REVERSION",
        "Stability-Qualified Reversion",
        "Advanced mean reversion",
        "A dislocation is tradable only when multi-horizon anti-persistence and reversion stability agree.",
        "Robust z beyond +/-1.5 with variance ratio, Hurst, crossing, and half-life composite confirmation.",
        "z returns inside +/-0.25, relationship break alarm, or 52 bars.",
        52,
        "z=1.5; stability>=0.45; VR<1; break alarm blocks",
    ),
    StrategyDefinition(
        "FLOW_DIVERGENCE",
        "Executable Flow Divergence",
        "Volume / liquidity",
        "Paired package flow turning against a stretched spread can confirm exhaustion.",
        "Robust z beyond +/-1.2 with opposite signed-volume imbalance and executable capacity.",
        "flow loses confirmation, z reaches fair value, or 26 bars.",
        26,
        "z=1.2; package flow imbalance=0.15; max_hold=26",
    ),
    StrategyDefinition(
        "REGIME_ENSEMBLE",
        "Fixed Regime Ensemble",
        "Ensemble",
        "Independent preregistered specialists are more credible when at least two agree.",
        "At least two component entries agree and none opposes; all liquidity/risk gates pass.",
        "Vote collapses, component reversal, or 78 bars.",
        78,
        "2 agreeing votes; no opposing vote; fixed weights",
    ),
    StrategyDefinition(
        "ADAPTIVE_EXPERT_MIX",
        "Causal Fixed-Share Expert Mix",
        "Adaptive ensemble",
        "Past resolved, cost-aware outcomes determine which transparent experts deserve weight.",
        "Family/tenor expert weights update once per completed session after delayed outcomes; at least two experts agree.",
        "Adaptive score falls below threshold, relationship break alarm, or 78 bars.",
        78,
        "26-bar labels; fixed-share shrinkage; delayed causal updates; lockbox freeze",
    ),
)
STRATEGY_BY_ID = {item.strategy_id: item for item in STRATEGIES}


BASE_EXPERT_IDS: tuple[str, ...] = (
    "ROBUST_MEAN_REVERSION",
    "TREND_BREAKOUT",
    "VOLATILITY_SQUEEZE",
    "SESSION_VWAP_REVERSION",
    "ERROR_CORRECTION_RESIDUAL",
    "STABILITY_REVERSION",
    "FLOW_DIVERGENCE",
)

BACKTEST_COLUMNS: tuple[str, ...] = (
    "timestamp_utc",
    "session_date",
    "spread_id",
    "spread_name",
    "spread_open",
    "spread_close",
    "package_open_value_usd",
    "package_close_value_usd",
    "one_way_cost_usd",
    "roll_id",
    "earliest_risk_date",
    "forced_exit_session",
    "bar_slot",
    "session_last_slot",
    "entry_allowed",
    "complexity_tier",
    "algebra_group",
    "relative_volume",
    "package_volume_capacity",
    "min_leg_events",
    "max_leg_bid_ask_ticks",
    "tod_normalized_change",
    "robust_volume_surprise",
    "robust_z",
    "research_close",
    "ema_slow",
    "macd_histogram",
    "bollinger_mid",
    "session_vwap_proxy",
    "seasonal_z",
    "seasonal_move_z",
    "change_point_alarm",
    "signed_volume_imbalance_proxy",
    "expert_vote_robust_mean_reversion",
    "expert_vote_trend_breakout",
    "expert_vote_volatility_squeeze",
    "expert_vote_session_vwap_reversion",
    "expert_vote_error_correction_residual",
    "expert_vote_stability_reversion",
    "expert_vote_flow_divergence",
    "expert_vote_regime_ensemble",
    "adaptive_vote",
)
BACKTEST_VOTE_COLUMNS: tuple[str, ...] = (
    *(
        f"expert_vote_{strategy_id.lower()}"
        for strategy_id in BASE_EXPERT_IDS
    ),
    "expert_vote_regime_ensemble",
    "adaptive_vote",
)

_WORKER_CONFIG: TechnicalConfig | None = None
_WORKER_WINDOWS: tuple[Mapping[str, Any], ...] = ()
_WORKER_LOCKBOX_START: date | None = None


def strategy_library_frame() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "strategy_id": item.strategy_id,
                "strategy_name": item.display_name,
                "family": item.family,
                "thesis": item.thesis,
                "entry_rule": item.entry_rule,
                "exit_rule": item.exit_rule,
                "maximum_holding_bars": item.maximum_holding_bars,
                "parameters": item.parameter_summary,
            }
            for item in STRATEGIES
        ]
    )


def _number(row: Mapping[str, Any], name: str, default: float | None = None) -> float | None:
    value = row.get(name)
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _pattern_diagnostic(
    row: Mapping[str, Any],
    *,
    positive_votes: int,
    negative_votes: int,
    model_enabled: bool,
    model_stale: bool,
    config: TechnicalConfig,
) -> tuple[str, float | None, float, str]:
    """Expose the adaptive multi-indicator pattern without creating a signal.

    The state is a reporting surface over the already-frozen expert votes and
    adaptive score. It never changes entry direction, confidence, or the
    backtest trial count.
    """

    score = _number(row, "adaptive_score")
    # Model artifacts serialize frozen Float32 weights. A stable reporting
    # precision keeps train/score receipts byte-comparable without changing the
    # underlying adaptive vote or any entry decision.
    strength = round(abs(score), 6) if score is not None else None
    agreement = max(positive_votes, negative_votes) / len(BASE_EXPERT_IDS)
    top_expert = str(row.get("adaptive_top_expert") or "UNAVAILABLE")
    components = (
        f"{positive_votes} long / {negative_votes} short; "
        f"top={top_expert}"
    )
    observations = int(row.get("adaptive_observations") or 0)
    adaptive_vote = int(row.get("adaptive_vote") or 0)
    if not model_enabled:
        state = "ANALYTIC_ONLY"
    elif model_stale:
        state = "MODEL_STALE"
    elif bool(row.get("change_point_alarm", False)):
        state = "STRUCTURAL_BREAK"
    elif observations < config.backtest.adaptive_min_observations:
        state = "WARMUP"
    elif adaptive_vote > 0:
        state = "BULLISH_CONSENSUS"
    elif adaptive_vote < 0:
        state = "BEARISH_CONSENSUS"
    elif positive_votes and negative_votes:
        state = "MIXED_PATTERN"
    elif positive_votes >= 2:
        state = "BULLISH_CLUSTER"
    elif negative_votes >= 2:
        state = "BEARISH_CLUSTER"
    elif positive_votes == 1:
        state = "BULLISH_FRAGMENT"
    elif negative_votes == 1:
        state = "BEARISH_FRAGMENT"
    else:
        state = "NO_PATTERN"
    return state, strength, agreement, components


def _apply_current_trade_budget(
    board: pl.DataFrame, config: TechnicalConfig
) -> pl.DataFrame:
    """Select at most the configured number of independent live entries."""

    if board.is_empty():
        return board
    rows = board.to_dicts()
    candidates = [
        index
        for index, row in enumerate(rows)
        if row.get("status") in {"BUY", "SELL"}
        and bool(row.get("direction_evidence_validated"))
    ]
    candidates.sort(
        key=lambda index: (
            -float(rows[index].get("confidence") or 0.0),
            -float(rows[index].get("expected_edge_to_cost") or 0.0),
            -float(rows[index].get("pattern_strength") or 0.0),
            -float(rows[index].get("relative_volume") or 0.0),
            int(rows[index].get("complexity_tier") or 1),
            str(rows[index].get("spread_id") or ""),
        )
    )
    selected: set[int] = set()
    selected_groups: set[str] = set()
    rejection_reason: dict[int, str] = {}
    limit = int(config.backtest.maximum_new_trades_per_session)
    for candidate_rank, index in enumerate(candidates, start=1):
        rows[index]["portfolio_candidate_rank"] = candidate_rank
        algebra_group = str(
            rows[index].get("algebra_group") or rows[index].get("spread_id")
        )
        if (
            config.backtest.one_trade_per_algebra_group
            and algebra_group in selected_groups
        ):
            rejection_reason[index] = "DUPLICATE_ALGEBRA_GROUP"
            continue
        if len(selected) >= limit:
            rejection_reason[index] = "DAILY_TRADE_BUDGET"
            continue
        selected.add(index)
        selected_groups.add(algebra_group)
    selected_count = len(selected)
    for index, row in enumerate(rows):
        row.setdefault("portfolio_candidate_rank", None)
        is_selected = index in selected
        row["portfolio_selected"] = is_selected
        row["portfolio_rank"] = (
            1
            + sum(
                1
                for candidate_index in selected
                if candidates.index(candidate_index) < candidates.index(index)
            )
            if is_selected
            else None
        )
        row["daily_trade_limit"] = limit
        row["daily_trade_slots_remaining"] = max(0, limit - selected_count)
        if is_selected:
            row["portfolio_action"] = "SELECTED"
        elif index in rejection_reason:
            row["portfolio_action"] = rejection_reason[index]
        elif row.get("status") == "WATCH":
            row["portfolio_action"] = "WATCH_ONLY"
        else:
            row["portfolio_action"] = "NOT_ACTIONABLE"
        row["portfolio_rank_basis"] = (
            "confidence,edge_to_cost,pattern_strength,relative_volume,"
            "complexity,spread_id"
        )
    return pl.DataFrame(rows, infer_schema_length=None)


def _component_entries(row: Mapping[str, Any]) -> dict[str, int]:
    precomputed = {
        expert: row.get(f"expert_vote_{expert.lower()}")
        for expert in BASE_EXPERT_IDS
    }
    if all(value is not None for value in precomputed.values()):
        votes = {key: int(value or 0) for key, value in precomputed.items()}
        votes["REGIME_ENSEMBLE"] = int(
            row.get("expert_vote_regime_ensemble") or 0
        )
        votes["ADAPTIVE_EXPERT_MIX"] = int(row.get("adaptive_vote") or 0)
        return votes
    z = _number(row, "robust_z")
    rsi = _number(row, "rsi")
    efficiency = _number(row, "efficiency_ratio")
    close = _number(row, "research_close")
    high = _number(row, "donchian_high")
    low = _number(row, "donchian_low")
    macd = _number(row, "macd_histogram")
    relative_volume = _number(row, "relative_volume")
    pvo = _number(row, "pvo")
    width = _number(row, "bollinger_width")
    width_p20 = _number(row, "bollinger_width_p20")
    vwap = _number(row, "session_vwap_proxy")
    vol = _number(row, "ewma_abs_change")
    seasonal_z = _number(row, "seasonal_z")
    seasonal_n = _number(row, "seasonal_n", 0.0) or 0.0
    seasonal_move_z = _number(row, "seasonal_move_z")
    seasonal_prior_years = _number(row, "seasonal_prior_years", 0.0) or 0.0
    seasonal_confidence = _number(row, "seasonal_confidence", 0.0) or 0.0
    stability = _number(row, "mean_reversion_stability")
    variance_ratio = _number(row, "variance_ratio_5")
    flow_imbalance = _number(row, "signed_volume_imbalance_proxy")
    effort = _number(row, "effort_vs_result")
    change_alarm = bool(row.get("change_point_alarm", False))

    votes = {
        "ROBUST_MEAN_REVERSION": 0,
        "TREND_BREAKOUT": 0,
        "VOLATILITY_SQUEEZE": 0,
        "SESSION_VWAP_REVERSION": 0,
        "ERROR_CORRECTION_RESIDUAL": 0,
        "STABILITY_REVERSION": 0,
        "FLOW_DIVERGENCE": 0,
    }
    if (
        not change_alarm
        and z is not None
        and rsi is not None
        and (efficiency is None or efficiency < 0.55)
        and (stability is None or stability >= 0.20)
    ):
        if z <= -1.8 and rsi <= 42:
            votes["ROBUST_MEAN_REVERSION"] = 1
        elif z >= 1.8 and rsi >= 58:
            votes["ROBUST_MEAN_REVERSION"] = -1
    if not change_alarm and None not in (close, high, low, macd, efficiency, relative_volume):
        if close > high and macd > 0 and efficiency >= 0.35 and relative_volume >= 1.0:
            votes["TREND_BREAKOUT"] = 1
        elif close < low and macd < 0 and efficiency >= 0.35 and relative_volume >= 1.0:
            votes["TREND_BREAKOUT"] = -1
    if not change_alarm and None not in (close, high, low, width, width_p20, relative_volume):
        compressed = width <= width_p20 * 1.25
        flow_ok = relative_volume >= 1.05 and (pvo is None or pvo > -0.10)
        if compressed and flow_ok and close > high:
            votes["VOLATILITY_SQUEEZE"] = 1
        elif compressed and flow_ok and close < low:
            votes["VOLATILITY_SQUEEZE"] = -1
    if (
        not change_alarm
        and None not in (close, vwap, vol)
        and vol
        and int(row.get("bar_slot") or 0)
        < int(row.get("session_last_slot") or 12)
    ):
        deviation = (close - vwap) / vol
        if deviation <= -1.5:
            votes["SESSION_VWAP_REVERSION"] = 1
        elif deviation >= 1.5:
            votes["SESSION_VWAP_REVERSION"] = -1
    if (
        not change_alarm
        and seasonal_move_z is not None
        and seasonal_n >= 8
        and seasonal_prior_years >= 2
        and seasonal_confidence > 0
    ):
        if seasonal_move_z >= 0.20:
            votes["ERROR_CORRECTION_RESIDUAL"] = 1
        elif seasonal_move_z <= -0.20:
            votes["ERROR_CORRECTION_RESIDUAL"] = -1
    elif not change_alarm and seasonal_z is not None and seasonal_n >= 8 and z is not None:
        # Backward-compatible preliminary path for older stored snapshots.
        if seasonal_z <= -1.6 and z <= -0.5:
            votes["ERROR_CORRECTION_RESIDUAL"] = 1
        elif seasonal_z >= 1.6 and z >= 0.5:
            votes["ERROR_CORRECTION_RESIDUAL"] = -1
    if (
        not change_alarm
        and z is not None
        and stability is not None
        and stability >= 0.45
        and (variance_ratio is None or variance_ratio < 1.0)
    ):
        if z <= -1.5:
            votes["STABILITY_REVERSION"] = 1
        elif z >= 1.5:
            votes["STABILITY_REVERSION"] = -1
    if (
        not change_alarm
        and z is not None
        and flow_imbalance is not None
        and (effort is None or effort >= 0.5)
    ):
        if z <= -1.2 and flow_imbalance >= 0.15:
            votes["FLOW_DIVERGENCE"] = 1
        elif z >= 1.2 and flow_imbalance <= -0.15:
            votes["FLOW_DIVERGENCE"] = -1
    positive = sum(value > 0 for value in votes.values())
    negative = sum(value < 0 for value in votes.values())
    votes["REGIME_ENSEMBLE"] = (
        1 if positive >= 2 and negative == 0 else -1 if negative >= 2 and positive == 0 else 0
    )
    votes["ADAPTIVE_EXPERT_MIX"] = int(row.get("adaptive_vote") or 0)
    return votes


def _entry_direction(row: Mapping[str, Any], strategy_id: str) -> int:
    """Read one already-computed strategy vote without allocating a vote map."""

    if strategy_id in BASE_EXPERT_IDS:
        column = f"expert_vote_{strategy_id.lower()}"
        if row.get(column) is not None:
            return int(row.get(column) or 0)
    if strategy_id == "REGIME_ENSEMBLE":
        if row.get("expert_vote_regime_ensemble") is not None:
            return int(row.get("expert_vote_regime_ensemble") or 0)
    if strategy_id == "ADAPTIVE_EXPERT_MIX":
        if row.get("adaptive_vote") is not None:
            return int(row.get("adaptive_vote") or 0)
    votes = _component_entries(row)
    if strategy_id not in votes:
        raise KeyError(f"Unknown strategy id: {strategy_id}")
    return votes[strategy_id]


def _entry_vote_column(strategy_id: str) -> str:
    if strategy_id in BASE_EXPERT_IDS:
        return f"expert_vote_{strategy_id.lower()}"
    if strategy_id == "REGIME_ENSEMBLE":
        return "expert_vote_regime_ensemble"
    if strategy_id == "ADAPTIVE_EXPERT_MIX":
        return "adaptive_vote"
    raise KeyError(f"Unknown strategy id: {strategy_id}")


def add_rule_expert_votes(features: pl.DataFrame) -> pl.DataFrame:
    """Vectorize the fixed expert rules once for learning, backtests, and live use."""

    if features.is_empty() or f"expert_vote_{BASE_EXPERT_IDS[0].lower()}" in features.columns:
        return features
    alarm = pl.col("change_point_alarm").fill_null(False)
    session_last_slot = (
        pl.col("session_last_slot")
        if "session_last_slot" in features.columns
        else pl.lit(12)
    )
    z = pl.col("robust_z")
    efficiency = pl.col("efficiency_ratio")
    stability = pl.col("mean_reversion_stability")
    close = pl.col("research_close")
    relative_volume = pl.col("relative_volume")
    production_seasonal = (
        pl.col("seasonal_move_z").is_not_null()
        & (pl.col("seasonal_n").fill_null(0) >= 8)
        & (pl.col("seasonal_prior_years").fill_null(0) >= 2)
        & (pl.col("seasonal_confidence").fill_null(0) > 0)
    )
    expressions: dict[str, pl.Expr] = {
        "ROBUST_MEAN_REVERSION": pl.when(
            ~alarm
            & (z <= -1.8)
            & (pl.col("rsi") <= 42)
            & efficiency.fill_null(0.0).lt(0.55)
            & stability.fill_null(0.20).ge(0.20)
        )
        .then(1)
        .when(
            ~alarm
            & (z >= 1.8)
            & (pl.col("rsi") >= 58)
            & efficiency.fill_null(0.0).lt(0.55)
            & stability.fill_null(0.20).ge(0.20)
        )
        .then(-1)
        .otherwise(0),
        "TREND_BREAKOUT": pl.when(
            ~alarm
            & (close > pl.col("donchian_high"))
            & (pl.col("macd_histogram") > 0)
            & (efficiency >= 0.35)
            & (relative_volume >= 1.0)
        )
        .then(1)
        .when(
            ~alarm
            & (close < pl.col("donchian_low"))
            & (pl.col("macd_histogram") < 0)
            & (efficiency >= 0.35)
            & (relative_volume >= 1.0)
        )
        .then(-1)
        .otherwise(0),
        "VOLATILITY_SQUEEZE": pl.when(
            ~alarm
            & (pl.col("bollinger_width") <= 1.25 * pl.col("bollinger_width_p20"))
            & (relative_volume >= 1.05)
            & (pl.col("pvo").fill_null(0.0) > -0.10)
            & (close > pl.col("donchian_high"))
        )
        .then(1)
        .when(
            ~alarm
            & (pl.col("bollinger_width") <= 1.25 * pl.col("bollinger_width_p20"))
            & (relative_volume >= 1.05)
            & (pl.col("pvo").fill_null(0.0) > -0.10)
            & (close < pl.col("donchian_low"))
        )
        .then(-1)
        .otherwise(0),
        "SESSION_VWAP_REVERSION": pl.when(
            ~alarm
            & (pl.col("bar_slot") < session_last_slot)
            & (
                (close - pl.col("session_vwap_proxy"))
                / pl.col("ewma_abs_change")
                <= -1.5
            )
        )
        .then(1)
        .when(
            ~alarm
            & (pl.col("bar_slot") < session_last_slot)
            & (
                (close - pl.col("session_vwap_proxy"))
                / pl.col("ewma_abs_change")
                >= 1.5
            )
        )
        .then(-1)
        .otherwise(0),
        "ERROR_CORRECTION_RESIDUAL": pl.when(
            ~alarm & production_seasonal & (pl.col("seasonal_move_z") >= 0.20)
        )
        .then(1)
        .when(~alarm & production_seasonal & (pl.col("seasonal_move_z") <= -0.20))
        .then(-1)
        .when(
            ~alarm
            & ~production_seasonal
            & (pl.col("seasonal_n").fill_null(0) >= 8)
            & (pl.col("seasonal_z") <= -1.6)
            & (z <= -0.5)
        )
        .then(1)
        .when(
            ~alarm
            & ~production_seasonal
            & (pl.col("seasonal_n").fill_null(0) >= 8)
            & (pl.col("seasonal_z") >= 1.6)
            & (z >= 0.5)
        )
        .then(-1)
        .otherwise(0),
        "STABILITY_REVERSION": pl.when(
            ~alarm
            & (stability >= 0.45)
            & pl.col("variance_ratio_5").fill_null(0.999).lt(1.0)
            & (z <= -1.5)
        )
        .then(1)
        .when(
            ~alarm
            & (stability >= 0.45)
            & pl.col("variance_ratio_5").fill_null(0.999).lt(1.0)
            & (z >= 1.5)
        )
        .then(-1)
        .otherwise(0),
        "FLOW_DIVERGENCE": pl.when(
            ~alarm
            & (pl.col("effort_vs_result").fill_null(0.5) >= 0.5)
            & (z <= -1.2)
            & (pl.col("signed_volume_imbalance_proxy") >= 0.15)
        )
        .then(1)
        .when(
            ~alarm
            & (pl.col("effort_vs_result").fill_null(0.5) >= 0.5)
            & (z >= 1.2)
            & (pl.col("signed_volume_imbalance_proxy") <= -0.15)
        )
        .then(-1)
        .otherwise(0),
    }
    result = features.with_columns(
        [
            expression.cast(pl.Int8).alias(f"expert_vote_{expert.lower()}")
            for expert, expression in expressions.items()
        ]
    )
    vote_columns = [f"expert_vote_{expert.lower()}" for expert in BASE_EXPERT_IDS]
    result = result.with_columns(
        pl.sum_horizontal(
            *[(pl.col(column) > 0).cast(pl.Int8) for column in vote_columns]
        ).alias("_expert_positive_count"),
        pl.sum_horizontal(
            *[(pl.col(column) < 0).cast(pl.Int8) for column in vote_columns]
        ).alias("_expert_negative_count"),
    ).with_columns(
        pl.when(
            (pl.col("_expert_positive_count") >= 2)
            & (pl.col("_expert_negative_count") == 0)
        )
        .then(1)
        .when(
            (pl.col("_expert_negative_count") >= 2)
            & (pl.col("_expert_positive_count") == 0)
        )
        .then(-1)
        .otherwise(0)
        .cast(pl.Int8)
        .alias("expert_vote_regime_ensemble")
    )
    return result.drop("_expert_positive_count", "_expert_negative_count")


def _bounded_weights(values: Mapping[str, float], cap: float) -> dict[str, float]:
    """Project non-negative weights to unit sum with a stable floor and cap."""

    keys = list(values)
    if not keys:
        return {}
    floor = 0.10 / len(keys)
    weights = {key: max(floor, float(values[key])) for key in keys}
    for _ in range(12):
        total = sum(weights.values()) or 1.0
        weights = {key: value / total for key, value in weights.items()}
        capped = {key for key, value in weights.items() if value > cap}
        if not capped:
            break
        excess = sum(weights[key] - cap for key in capped)
        for key in capped:
            weights[key] = cap
        free = [key for key in keys if key not in capped]
        free_total = sum(weights[key] for key in free)
        if not free or free_total <= 0:
            break
        for key in free:
            weights[key] += excess * weights[key] / free_total
    total = sum(weights.values()) or 1.0
    return {key: value / total for key, value in weights.items()}


def _add_adaptive_ensemble_reference(
    features: pl.DataFrame, config: TechnicalConfig
) -> pl.DataFrame:
    """Attach a causal, delayed-outcome Fixed-Share expert ensemble.

    Expert thresholds stay fixed.  Only their non-negative weights learn, once
    per completed session, from resolved cost-aware labels pooled by economic
    family and tenor bucket.  This is deliberately auditable and avoids a
    separate fitted model for each of hundreds of correlated curve structures.
    """

    if features.is_empty():
        return features
    ordered = add_rule_expert_votes(features).sort(["timestamp_utc", "spread_id"])
    horizon = config.backtest.adaptive_horizon_bars
    loss_totals: dict[tuple[str, date, str], tuple[float, int]] = {}
    for spread in ordered.partition_by("spread_id", maintain_order=True):
        rows = spread.sort("timestamp_utc").to_dicts()
        for index in range(0, max(0, len(rows) - horizon)):
            signal = rows[index]
            entry_index = index + config.backtest.execution_lag_bars
            resolution_index = index + horizon
            if entry_index >= len(rows) or resolution_index >= len(rows):
                continue
            entry = rows[entry_index]
            resolution = rows[resolution_index]
            if (
                not bool(signal.get("entry_allowed"))
                or signal.get("roll_id") != entry.get("roll_id")
                or signal.get("roll_id") != resolution.get("roll_id")
                or resolution.get("session_date") > signal.get("forced_exit_session")
            ):
                continue
            entry_value = _number(entry, "package_open_value_usd")
            resolution_value = _number(resolution, "package_close_value_usd")
            one_way_cost = _number(signal, "one_way_cost_usd", 0.0) or 0.0
            if entry_value is None or resolution_value is None:
                continue
            move = resolution_value - entry_value
            round_trip = max(1.0, 2.0 * one_way_cost)
            net_magnitude = max(0.0, abs(move) - 2.0 * round_trip)
            target = math.copysign(
                min(1.0, net_magnitude / max(4.0 * round_trip, 1.0)), move
            ) if net_magnitude > 0 else 0.0
            votes = _component_entries(signal)
            awake = {expert: votes[expert] for expert in BASE_EXPERT_IDS if votes[expert]}
            if not awake:
                continue
            learner_group = (
                f"{signal.get('spread_family', 'Other')}|"
                f"{signal.get('tenor_bucket', 'FRONT')}"
            )
            for expert, prediction in awake.items():
                value = ((float(prediction) - target) ** 2) / 4.0
                key = (learner_group, resolution["session_date"], expert)
                current_sum, current_count = loss_totals.get(key, (0.0, 0))
                loss_totals[key] = (current_sum + value, current_count + 1)

    # Aggregate bounded, highly correlated bars/structures into one loss vector
    # per learner group and session.
    session_losses: dict[tuple[str, date], dict[str, float]] = {}
    for (group, resolved_session, expert), (total, count) in loss_totals.items():
        session_losses.setdefault((group, resolved_session), {})[expert] = (
            total / count
        )

    sessions = sorted(ordered["session_date"].unique().to_list())
    lockbox_start = (
        sessions[-config.backtest.lockbox_sessions]
        if len(sessions) >= config.backtest.lockbox_sessions
        else None
    )
    groups = sorted(
        {
            f"{row.get('spread_family', 'Other')}|{row.get('tenor_bucket', 'FRONT')}"
            for row in ordered.select("spread_family", "tenor_bucket").unique().to_dicts()
        }
    )
    equal = 1.0 / len(BASE_EXPERT_IDS)
    weights = {
        group: {expert: equal for expert in BASE_EXPERT_IDS} for group in groups
    }
    observations = {group: 0 for group in groups}
    rows_by_session = {
        session: frame
        for session, frame in ordered.partition_by("session_date", as_dict=True).items()
    }
    audit_columns: dict[str, list[object]] = {
        name: []
        for name in (
            "spread_id",
            "timestamp_utc",
            "learner_group",
            "adaptive_score",
            "adaptive_vote",
            "adaptive_confidence",
            "adaptive_observations",
            "adaptive_top_expert",
            "adaptive_top_weight",
            "adaptive_positive_weight",
            "adaptive_negative_weight",
            "adaptive_status",
            *[f"adaptive_weight_{expert.lower()}" for expert in BASE_EXPERT_IDS],
        )
    }
    previous_sessions: list[date] = []
    for session in sessions:
        freeze = bool(
            config.backtest.adaptive_freeze_lockbox
            and lockbox_start is not None
            and session >= lockbox_start
        )
        if not freeze:
            for resolved_session in previous_sessions[-1:]:
                for group in groups:
                    loss = session_losses.get((group, resolved_session))
                    if not loss:
                        continue
                    current = weights[group]
                    updated = dict(current)
                    for expert, value in loss.items():
                        updated[expert] = current[expert] * math.exp(
                            -config.backtest.adaptive_learning_rate * value
                        )
                    total = sum(updated.values()) or 1.0
                    updated = {key: value / total for key, value in updated.items()}
                    share = config.backtest.adaptive_uniform_shrinkage
                    updated = {
                        key: (1.0 - share) * value + share * equal
                        for key, value in updated.items()
                    }
                    weights[group] = _bounded_weights(
                        updated, config.backtest.adaptive_max_expert_weight
                    )
                    observations[group] += 1
        session_frame = rows_by_session[(session,)] if (session,) in rows_by_session else rows_by_session[session]
        for row in session_frame.to_dicts():
            group = f"{row.get('spread_family', 'Other')}|{row.get('tenor_bucket', 'FRONT')}"
            current = weights[group]
            votes = _component_entries(row)
            score = sum(current[expert] * votes[expert] for expert in BASE_EXPERT_IDS)
            positive_weight = sum(
                current[expert] for expert in BASE_EXPERT_IDS if votes[expert] > 0
            )
            negative_weight = sum(
                current[expert] for expert in BASE_EXPERT_IDS if votes[expert] < 0
            )
            positive_count = sum(votes[expert] > 0 for expert in BASE_EXPERT_IDS)
            negative_count = sum(votes[expert] < 0 for expert in BASE_EXPERT_IDS)
            mature = observations[group] >= config.backtest.adaptive_min_observations
            vote = 0
            if mature and not bool(row.get("change_point_alarm", False)):
                if (
                    score >= config.backtest.adaptive_entry_threshold
                    and positive_count >= 2
                    and negative_weight <= 0.15
                ):
                    vote = 1
                elif (
                    score <= -config.backtest.adaptive_entry_threshold
                    and negative_count >= 2
                    and positive_weight <= 0.15
                ):
                    vote = -1
            confidence = 0.5 + 0.45 * min(1.0, abs(score)) * min(
                1.0, observations[group] / 120.0
            )
            if observations[group] < 120:
                confidence = min(confidence, 0.55)
            top_expert = max(current, key=current.get)
            audit_values = {
                "spread_id": row["spread_id"],
                "timestamp_utc": row["timestamp_utc"],
                "learner_group": group,
                "adaptive_score": score,
                "adaptive_vote": vote,
                "adaptive_confidence": confidence,
                "adaptive_observations": observations[group],
                "adaptive_top_expert": top_expert,
                "adaptive_top_weight": current[top_expert],
                "adaptive_positive_weight": positive_weight,
                "adaptive_negative_weight": negative_weight,
                "adaptive_status": (
                    "LOCKBOX_FROZEN"
                    if freeze
                    else "ACTIVE"
                    if mature
                    else "WARMUP"
                ),
                **{
                    f"adaptive_weight_{expert.lower()}": current[expert]
                    for expert in BASE_EXPERT_IDS
                },
            }
            for name, value in audit_values.items():
                audit_columns[name].append(value)
        previous_sessions.append(session)
    adaptive = pl.DataFrame(audit_columns, strict=False)
    float_columns = [
        column
        for column, dtype in adaptive.schema.items()
        if dtype == pl.Float64
    ]
    if float_columns:
        adaptive = adaptive.with_columns(
            [pl.col(column).cast(pl.Float32) for column in float_columns]
        )
    return ordered.join(
        adaptive, on=["spread_id", "timestamp_utc"], how="left"
    ).sort(["spread_id", "timestamp_utc"])


def add_adaptive_ensemble(
    features: pl.DataFrame, config: TechnicalConfig
) -> pl.DataFrame:
    """Vectorized causal Fixed-Share learning with one small Python daily loop."""

    if features.is_empty():
        return features
    ordered = add_rule_expert_votes(features).sort(["spread_id", "timestamp_utc"])
    ordered = ordered.with_columns(
        pl.concat_str(
            [pl.col("spread_family"), pl.col("tenor_bucket")], separator="|"
        ).alias("learner_group")
    )
    horizon = int(config.backtest.adaptive_horizon_bars)
    lag = int(config.backtest.execution_lag_bars)
    working = ordered.with_columns(
        pl.col("package_open_value_usd")
        .shift(-lag)
        .over("spread_id")
        .alias("_adaptive_entry_value"),
        pl.col("package_close_value_usd")
        .shift(-horizon)
        .over("spread_id")
        .alias("_adaptive_resolution_value"),
        pl.col("roll_id").shift(-lag).over("spread_id").alias("_adaptive_entry_roll"),
        pl.col("roll_id")
        .shift(-horizon)
        .over("spread_id")
        .alias("_adaptive_resolution_roll"),
        pl.col("session_date")
        .shift(-horizon)
        .over("spread_id")
        .alias("_adaptive_resolution_session"),
    )
    valid = (
        pl.col("entry_allowed").fill_null(False)
        & (pl.col("roll_id") == pl.col("_adaptive_entry_roll"))
        & (pl.col("roll_id") == pl.col("_adaptive_resolution_roll"))
        & (
            pl.col("_adaptive_resolution_session")
            <= pl.col("forced_exit_session")
        )
        & pl.col("_adaptive_entry_value").is_not_null()
        & pl.col("_adaptive_resolution_value").is_not_null()
    )
    round_trip = pl.max_horizontal(
        pl.lit(1.0), 2.0 * pl.col("one_way_cost_usd").fill_null(0.0)
    )
    move = pl.col("_adaptive_resolution_value") - pl.col("_adaptive_entry_value")
    net_magnitude = pl.max_horizontal(
        pl.lit(0.0), move.abs() - 2.0 * round_trip
    )
    target = (
        move.sign()
        * pl.min_horizontal(
            pl.lit(1.0), net_magnitude / pl.max_horizontal(4.0 * round_trip, pl.lit(1.0))
        )
    )
    loss_columns: list[str] = []
    loss_expressions: list[pl.Expr] = []
    for expert in BASE_EXPERT_IDS:
        vote = pl.col(f"expert_vote_{expert.lower()}")
        name = f"_adaptive_loss_{expert.lower()}"
        loss_columns.append(name)
        loss_expressions.append(
            pl.when(valid & (vote != 0))
            .then(((vote.cast(pl.Float64) - target) ** 2) / 4.0)
            .otherwise(None)
            .alias(name)
        )
    losses = (
        working.with_columns(loss_expressions)
        .filter(pl.any_horizontal([pl.col(name).is_not_null() for name in loss_columns]))
        .group_by(["learner_group", "_adaptive_resolution_session"])
        .agg([pl.col(name).mean().alias(name) for name in loss_columns])
    )
    loss_lookup = {
        (str(row["learner_group"]), row["_adaptive_resolution_session"]): {
            expert: row.get(f"_adaptive_loss_{expert.lower()}")
            for expert in BASE_EXPERT_IDS
            if row.get(f"_adaptive_loss_{expert.lower()}") is not None
        }
        for row in losses.to_dicts()
    }

    sessions = sorted(ordered["session_date"].unique().to_list())
    lockbox_start = (
        sessions[-config.backtest.lockbox_sessions]
        if len(sessions) >= config.backtest.lockbox_sessions
        else None
    )
    groups = sorted(str(item) for item in ordered["learner_group"].unique().to_list())
    equal = 1.0 / len(BASE_EXPERT_IDS)
    weights = {
        group: {expert: equal for expert in BASE_EXPERT_IDS} for group in groups
    }
    observations = {group: 0 for group in groups}
    weight_rows: list[dict[str, object]] = []
    previous_session: date | None = None
    for session in sessions:
        freeze = bool(
            config.backtest.adaptive_freeze_lockbox
            and lockbox_start is not None
            and session >= lockbox_start
        )
        if not freeze and previous_session is not None:
            for group in groups:
                loss = loss_lookup.get((group, previous_session))
                if not loss:
                    continue
                current = weights[group]
                updated = dict(current)
                for expert, value in loss.items():
                    updated[expert] = current[expert] * math.exp(
                        -config.backtest.adaptive_learning_rate * float(value)
                    )
                total = sum(updated.values()) or 1.0
                updated = {key: value / total for key, value in updated.items()}
                share = config.backtest.adaptive_uniform_shrinkage
                updated = {
                    key: (1.0 - share) * value + share * equal
                    for key, value in updated.items()
                }
                weights[group] = _bounded_weights(
                    updated, config.backtest.adaptive_max_expert_weight
                )
                observations[group] += 1
        for group in groups:
            current = weights[group]
            top_expert = max(current, key=current.get)
            weight_rows.append(
                {
                    "session_date": session,
                    "learner_group": group,
                    "adaptive_observations": observations[group],
                    "adaptive_top_expert": top_expert,
                    "adaptive_top_weight": current[top_expert],
                    "adaptive_status": (
                        "LOCKBOX_FROZEN"
                        if freeze
                        else "ACTIVE"
                        if observations[group]
                        >= config.backtest.adaptive_min_observations
                        else "WARMUP"
                    ),
                    **{
                        f"adaptive_weight_{expert.lower()}": current[expert]
                        for expert in BASE_EXPERT_IDS
                    },
                }
            )
        previous_session = session

    result = ordered.join(
        pl.DataFrame(weight_rows, strict=False),
        on=["session_date", "learner_group"],
        how="left",
    )
    weight_columns = [
        f"adaptive_weight_{expert.lower()}" for expert in BASE_EXPERT_IDS
    ]
    score_terms = [
        pl.col(weight) * pl.col(f"expert_vote_{expert.lower()}")
        for expert, weight in zip(BASE_EXPERT_IDS, weight_columns, strict=True)
    ]
    positive_terms = [
        pl.when(pl.col(f"expert_vote_{expert.lower()}") > 0)
        .then(pl.col(weight))
        .otherwise(0.0)
        for expert, weight in zip(BASE_EXPERT_IDS, weight_columns, strict=True)
    ]
    negative_terms = [
        pl.when(pl.col(f"expert_vote_{expert.lower()}") < 0)
        .then(pl.col(weight))
        .otherwise(0.0)
        for expert, weight in zip(BASE_EXPERT_IDS, weight_columns, strict=True)
    ]
    result = result.with_columns(
        pl.sum_horizontal(score_terms).alias("adaptive_score"),
        pl.sum_horizontal(positive_terms).alias("adaptive_positive_weight"),
        pl.sum_horizontal(negative_terms).alias("adaptive_negative_weight"),
        pl.sum_horizontal(
            [
                (pl.col(f"expert_vote_{expert.lower()}") > 0).cast(pl.Int8)
                for expert in BASE_EXPERT_IDS
            ]
        ).alias("_adaptive_positive_count"),
        pl.sum_horizontal(
            [
                (pl.col(f"expert_vote_{expert.lower()}") < 0).cast(pl.Int8)
                for expert in BASE_EXPERT_IDS
            ]
        ).alias("_adaptive_negative_count"),
    )
    mature = (
        pl.col("adaptive_observations")
        >= config.backtest.adaptive_min_observations
    )
    no_alarm = ~pl.col("change_point_alarm").fill_null(False)
    result = result.with_columns(
        pl.when(
            mature
            & no_alarm
            & (pl.col("adaptive_score") >= config.backtest.adaptive_entry_threshold)
            & (pl.col("_adaptive_positive_count") >= 2)
            & (pl.col("adaptive_negative_weight") <= 0.15)
        )
        .then(1)
        .when(
            mature
            & no_alarm
            & (pl.col("adaptive_score") <= -config.backtest.adaptive_entry_threshold)
            & (pl.col("_adaptive_negative_count") >= 2)
            & (pl.col("adaptive_positive_weight") <= 0.15)
        )
        .then(-1)
        .otherwise(0)
        .cast(pl.Int8)
        .alias("adaptive_vote"),
        (
            0.5
            + 0.45
            * pl.col("adaptive_score").abs().clip(0.0, 1.0)
            * (pl.col("adaptive_observations") / 120.0).clip(0.0, 1.0)
        )
        .clip(0.0, 0.95)
        .alias("adaptive_confidence"),
    ).with_columns(
        pl.when(pl.col("adaptive_observations") < 120)
        .then(pl.min_horizontal(pl.col("adaptive_confidence"), pl.lit(0.55)))
        .otherwise(pl.col("adaptive_confidence"))
        .alias("adaptive_confidence")
    )
    adaptive_float_columns = [
        "adaptive_score",
        "adaptive_confidence",
        "adaptive_top_weight",
        "adaptive_positive_weight",
        "adaptive_negative_weight",
        *weight_columns,
    ]
    result = result.with_columns(
        [pl.col(column).cast(pl.Float32) for column in adaptive_float_columns]
    )
    return result.drop(
        "_adaptive_positive_count", "_adaptive_negative_count"
    ).sort(["spread_id", "timestamp_utc"])


def apply_frozen_expert_model(
    features: pl.DataFrame,
    artifact: Mapping[str, Any],
    config: TechnicalConfig,
) -> pl.DataFrame:
    """Apply persisted expert weights without updating them or rerunning training."""

    if features.is_empty():
        return features
    groups = artifact.get("expert_groups") or []
    if not groups:
        raise ValueError("Frozen model artifact contains no expert groups")
    weight_columns = [
        f"adaptive_weight_{expert.lower()}" for expert in BASE_EXPERT_IDS
    ]
    disposable = {
        "learner_group",
        "adaptive_score",
        "adaptive_vote",
        "adaptive_confidence",
        "adaptive_observations",
        "adaptive_top_expert",
        "adaptive_top_weight",
        "adaptive_positive_weight",
        "adaptive_negative_weight",
        "adaptive_status",
        *weight_columns,
    }
    base = features.drop(
        [column for column in features.columns if column in disposable]
    )
    base = add_rule_expert_votes(base).with_columns(
        pl.concat_str(
            [pl.col("spread_family"), pl.col("tenor_bucket")], separator="|"
        ).alias("learner_group")
    )
    weights = pl.DataFrame(groups, strict=False).with_columns(
        pl.lit(True).alias("_model_group_available")
    )
    required = {
        "learner_group",
        "adaptive_observations",
        "adaptive_top_expert",
        "adaptive_top_weight",
        *weight_columns,
    }
    missing = sorted(required - set(weights.columns))
    if missing:
        raise ValueError(
            "Frozen model expert groups are missing columns: " + ", ".join(missing)
        )
    equal_weight = 1.0 / len(BASE_EXPERT_IDS)
    result = base.join(weights, on="learner_group", how="left").with_columns(
        pl.col("_model_group_available").fill_null(False),
        pl.col("adaptive_observations").fill_null(0).cast(pl.Int64),
        pl.col("adaptive_top_expert").fill_null("UNAVAILABLE"),
        pl.col("adaptive_top_weight").fill_null(0.0),
        *[
            pl.col(column).fill_null(equal_weight).cast(pl.Float64)
            for column in weight_columns
        ],
    )
    score_terms = [
        pl.col(weight) * pl.col(f"expert_vote_{expert.lower()}")
        for expert, weight in zip(BASE_EXPERT_IDS, weight_columns, strict=True)
    ]
    positive_terms = [
        pl.when(pl.col(f"expert_vote_{expert.lower()}") > 0)
        .then(pl.col(weight))
        .otherwise(0.0)
        for expert, weight in zip(BASE_EXPERT_IDS, weight_columns, strict=True)
    ]
    negative_terms = [
        pl.when(pl.col(f"expert_vote_{expert.lower()}") < 0)
        .then(pl.col(weight))
        .otherwise(0.0)
        for expert, weight in zip(BASE_EXPERT_IDS, weight_columns, strict=True)
    ]
    positive_counts = [
        (pl.col(f"expert_vote_{expert.lower()}") > 0).cast(pl.Int8)
        for expert in BASE_EXPERT_IDS
    ]
    negative_counts = [
        (pl.col(f"expert_vote_{expert.lower()}") < 0).cast(pl.Int8)
        for expert in BASE_EXPERT_IDS
    ]
    result = result.with_columns(
        pl.sum_horizontal(score_terms).alias("adaptive_score"),
        pl.sum_horizontal(positive_terms).alias("adaptive_positive_weight"),
        pl.sum_horizontal(negative_terms).alias("adaptive_negative_weight"),
        pl.sum_horizontal(positive_counts).alias("_adaptive_positive_count"),
        pl.sum_horizontal(negative_counts).alias("_adaptive_negative_count"),
    )
    mature = (
        pl.col("adaptive_observations")
        >= config.backtest.adaptive_min_observations
    )
    available = pl.col("_model_group_available")
    no_alarm = ~pl.col("change_point_alarm").fill_null(False)
    result = result.with_columns(
        pl.when(
            available
            & mature
            & no_alarm
            & (pl.col("adaptive_score") >= config.backtest.adaptive_entry_threshold)
            & (pl.col("_adaptive_positive_count") >= 2)
            & (pl.col("adaptive_negative_weight") <= 0.15)
        )
        .then(1)
        .when(
            available
            & mature
            & no_alarm
            & (pl.col("adaptive_score") <= -config.backtest.adaptive_entry_threshold)
            & (pl.col("_adaptive_negative_count") >= 2)
            & (pl.col("adaptive_positive_weight") <= 0.15)
        )
        .then(-1)
        .otherwise(0)
        .cast(pl.Int8)
        .alias("adaptive_vote"),
        (
            0.5
            + 0.45
            * pl.col("adaptive_score").abs().clip(0.0, 1.0)
            * (pl.col("adaptive_observations") / 120.0).clip(0.0, 1.0)
        )
        .clip(0.0, 0.95)
        .alias("adaptive_confidence"),
        pl.when(available)
        .then(pl.lit("FROZEN_MODEL_SCORE"))
        .otherwise(pl.lit("MODEL_GROUP_UNAVAILABLE"))
        .alias("adaptive_status"),
    ).with_columns(
        pl.when(pl.col("adaptive_observations") < 120)
        .then(pl.min_horizontal(pl.col("adaptive_confidence"), pl.lit(0.55)))
        .otherwise(pl.col("adaptive_confidence"))
        .alias("adaptive_confidence")
    )
    return result.drop(
        "_model_group_available",
        "_adaptive_positive_count",
        "_adaptive_negative_count",
    ).sort(["spread_id", "timestamp_utc"])


def _exit_reason(
    strategy_id: str,
    row: Mapping[str, Any],
    position: int,
    held_bars: int,
    current_vote: int | None = None,
) -> str | None:
    z = _number(row, "robust_z")
    close = _number(row, "research_close")
    ema_slow = _number(row, "ema_slow")
    macd = _number(row, "macd_histogram")
    middle = _number(row, "bollinger_mid")
    vwap = _number(row, "session_vwap_proxy")
    seasonal_z = _number(row, "seasonal_z")
    seasonal_move_z = _number(row, "seasonal_move_z")
    change_alarm = bool(row.get("change_point_alarm", False))
    flow_imbalance = _number(row, "signed_volume_imbalance_proxy")
    definition = STRATEGY_BY_ID[strategy_id]
    if held_bars >= definition.maximum_holding_bars:
        return "MAX_HOLD"
    if strategy_id == "ROBUST_MEAN_REVERSION" and z is not None:
        if abs(z) <= 0.20:
            return "FAIR_VALUE"
        if (position > 0 and z <= -3.25) or (position < 0 and z >= 3.25):
            return "ROBUST_Z_STOP"
    elif strategy_id == "TREND_BREAKOUT" and None not in (close, ema_slow, macd):
        if (position > 0 and (close < ema_slow or macd < 0)) or (
            position < 0 and (close > ema_slow or macd > 0)
        ):
            return "TREND_REVERSAL"
    elif strategy_id == "VOLATILITY_SQUEEZE" and None not in (close, middle):
        if (position > 0 and close < middle) or (position < 0 and close > middle):
            return "MIDDLE_BAND_REVERSAL"
    elif strategy_id == "SESSION_VWAP_REVERSION":
        if close is not None and vwap is not None and (
            (position > 0 and close >= vwap) or (position < 0 and close <= vwap)
        ):
            return "VWAP_TOUCH"
        if int(row.get("bar_slot") or 0) >= int(
            row.get("session_last_slot") or 12
        ):
            return "SESSION_CLOSE"
    elif strategy_id == "ERROR_CORRECTION_RESIDUAL":
        if seasonal_move_z is not None and (
            (position > 0 and seasonal_move_z <= 0)
            or (position < 0 and seasonal_move_z >= 0)
        ):
            return "SEASONAL_FORECAST_REVERSAL"
        if seasonal_z is not None and abs(seasonal_z) <= 0.35:
            return "SEASONAL_EQUILIBRIUM"
        if z is not None and ((position > 0 and z > 0.35) or (position < 0 and z < -0.35)):
            return "RESIDUAL_REVERSAL"
    elif strategy_id == "STABILITY_REVERSION":
        if change_alarm:
            return "RELATIONSHIP_BREAK"
        if z is not None and abs(z) <= 0.25:
            return "FAIR_VALUE"
    elif strategy_id == "FLOW_DIVERGENCE":
        if change_alarm:
            return "RELATIONSHIP_BREAK"
        if z is not None and abs(z) <= 0.25:
            return "FAIR_VALUE"
        if flow_imbalance is not None and (
            (position > 0 and flow_imbalance <= 0)
            or (position < 0 and flow_imbalance >= 0)
        ):
            return "FLOW_CONFIRMATION_LOST"
    elif strategy_id in {"REGIME_ENSEMBLE", "ADAPTIVE_EXPERT_MIX"}:
        if change_alarm:
            return "RELATIONSHIP_BREAK"
        current = (
            current_vote
            if current_vote is not None
            else _entry_direction(row, strategy_id)
        )
        if current == 0 or current == -position:
            return "ENSEMBLE_COLLAPSE"
    return None


def _liquidity_pass(row: Mapping[str, Any], config: TechnicalConfig) -> tuple[bool, str]:
    if config.indicators.candidate_risk_gates_enabled:
        tod_normalized = _number(row, "tod_normalized_change")
        robust_volume = _number(row, "robust_volume_surprise")
        if (
            tod_normalized is not None
            and abs(tod_normalized) >= config.indicators.extreme_tod_shock_z
        ):
            return False, "EXTREME_TOD_SHOCK"
        if (
            robust_volume is not None
            and robust_volume <= config.indicators.volume_dryness_z
        ):
            return False, "ROBUST_VOLUME_DRYNESS"
    relative = _number(row, "relative_volume")
    capacity = _number(row, "package_volume_capacity")
    events = _number(row, "min_leg_events")
    width_ticks = _number(row, "max_leg_bid_ask_ticks")
    if relative is None or relative < config.liquidity.minimum_relative_volume:
        return False, "LOW_RELATIVE_VOLUME"
    if capacity is None or capacity < 1.0 or events is None or events <= 0:
        return False, "INSUFFICIENT_EXECUTABLE_CAPACITY"
    if capacity * config.liquidity.maximum_volume_participation < 1.0:
        return False, "CAPACITY_BELOW_ONE_PACKAGE"
    if width_ticks is not None and width_ticks > config.liquidity.maximum_bid_ask_ticks:
        return False, "WIDE_MARKET"
    return True, "PASS"


def _walk_forward_windows(sessions: Sequence[date], config: TechnicalConfig) -> list[dict[str, Any]]:
    settings = config.backtest
    if len(sessions) <= settings.lockbox_sessions:
        return []
    development_end = len(sessions) - settings.lockbox_sessions
    embargo_start = settings.train_sessions + settings.validation_sessions
    fold_span = settings.embargo_sessions + settings.test_sessions
    windows: list[dict[str, Any]] = []
    fold = 1
    while embargo_start + fold_span <= development_end:
        validation_start = embargo_start - settings.validation_sessions
        test_start = embargo_start + settings.embargo_sessions
        windows.append(
            {
                "fold": fold,
                "train_start": sessions[0],
                "train_end": sessions[validation_start - 1],
                "validation_start": sessions[validation_start],
                "validation_end": sessions[embargo_start - 1],
                "embargo_start": sessions[embargo_start],
                "embargo_end": sessions[test_start - 1],
                "test_start": sessions[test_start],
                "test_end": sessions[test_start + settings.test_sessions - 1],
                "train_sessions_in_fold": validation_start,
                "validation_sessions_in_fold": settings.validation_sessions,
                "embargo_sessions": settings.embargo_sessions,
                "oos_test_sessions": settings.test_sessions,
            }
        )
        embargo_start += fold_span
        fold += 1
    return windows


def walk_forward_window_frame(
    features: pl.DataFrame, config: TechnicalConfig
) -> pl.DataFrame:
    """Describe every development fold and the untouched final lockbox."""

    if features.is_empty() or "session_date" not in features.columns:
        return pl.DataFrame()
    sessions = sorted(features["session_date"].unique().to_list())
    if len(sessions) <= config.backtest.lockbox_sessions:
        return pl.DataFrame()
    lockbox_start = sessions[-config.backtest.lockbox_sessions]
    lockbox_end = sessions[-1]
    development_end = sessions[-config.backtest.lockbox_sessions - 1]
    rows = [
        {
            **window,
            "development_end": development_end,
            "lockbox_start": lockbox_start,
            "lockbox_end": lockbox_end,
            "lockbox_sessions": config.backtest.lockbox_sessions,
            "lockbox_used_for_training": False,
            "bar_interval_minutes": config.system.bar_interval_minutes,
            "available_sessions": len(sessions),
            "strategy_count": len(STRATEGIES),
            "model_enabled_spreads": sum(
                item.model_enabled for item in config.spreads
            ),
        }
        for window in _walk_forward_windows(sessions, config)
    ]
    return pl.DataFrame(rows).sort("fold") if rows else pl.DataFrame()


def _phase_for_date(
    value: date,
    windows: Sequence[Mapping[str, Any]],
    lockbox_start: date | None,
) -> tuple[str, int | None]:
    if lockbox_start is not None and value >= lockbox_start:
        return "LOCKBOX", None
    for window in windows:
        if window["test_start"] <= value <= window["test_end"]:
            return "OOS", int(window["fold"])
    return "DEVELOPMENT", None


def _trade_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort(["spread_id", "strategy_id", "entry_time"])


def _simulate_strategy(
    spread_frame: pl.DataFrame,
    strategy: StrategyDefinition,
    config: TechnicalConfig,
    windows: Sequence[Mapping[str, Any]],
    lockbox_start: date | None,
    *,
    collect_equity: bool = False,
    prepared_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = prepared_rows if prepared_rows is not None else spread_frame.sort("timestamp_utc").to_dicts()
    if not rows:
        return [], []
    trades: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    realized_net = 0.0
    position = 0
    pending_entry: dict[str, Any] | None = None
    pending_exit: dict[str, Any] | None = None
    entry: dict[str, Any] | None = None
    held_bars = 0
    mfe = 0.0
    mae = 0.0
    last_row: Mapping[str, Any] | None = None
    vote_column = _entry_vote_column(strategy.strategy_id)
    precomputed_vote = vote_column in rows[0]

    def finish(
        exit_row: Mapping[str, Any],
        raw_value: float,
        reason: str,
        at_close: bool = False,
        exit_signal_time: datetime | None = None,
    ) -> None:
        nonlocal realized_net, position, entry, held_bars, mfe, mae, pending_exit
        assert entry is not None and position != 0
        exit_cost = float(exit_row.get("one_way_cost_usd") or 0.0)
        gross = position * (raw_value - float(entry["entry_package_value_usd"]))
        total_cost = float(entry["entry_cost_usd"]) + exit_cost
        net = gross - total_cost
        exit_spread = _number(exit_row, "spread_close" if at_close else "spread_open")
        phase, fold = _phase_for_date(entry["entry_session"], windows, lockbox_start)
        exit_phase, exit_fold = _phase_for_date(
            exit_row["session_date"], windows, lockbox_start
        )
        if phase in {"OOS", "LOCKBOX"} and (
            exit_phase != phase or (phase == "OOS" and exit_fold != fold)
        ):
            phase, fold = "PURGED", None
        trade_id = f"{entry['spread_id']}|{strategy.strategy_id}|{len(trades)+1:04d}"
        trades.append(
            {
                "trade_id": trade_id,
                "spread_id": entry["spread_id"],
                "spread_name": entry["spread_name"],
                "strategy_id": strategy.strategy_id,
                "strategy_name": strategy.display_name,
                "direction": "LONG" if position > 0 else "SHORT",
                "entry_time": entry["entry_time"],
                "entry_signal_time": entry["entry_signal_time"],
                "exit_time": exit_row["timestamp_utc"],
                "exit_signal_time": exit_signal_time,
                "entry_session": entry["entry_session"],
                "exit_session": exit_row["session_date"],
                "entry_spread": entry["entry_spread"],
                "exit_spread": exit_spread,
                "entry_package_value_usd": entry["entry_package_value_usd"],
                "exit_package_value_usd": raw_value,
                "gross_pnl_usd": gross,
                "cost_usd": total_cost,
                "net_pnl_usd": net,
                "pnl_2x_cost_usd": gross - 2.0 * total_cost,
                "pnl_3x_cost_usd": gross - 3.0 * total_cost,
                "holding_bars": held_bars,
                "mae_usd": mae,
                "mfe_usd": mfe,
                "exit_reason": reason,
                "entry_reason": entry["entry_reason"],
                "roll_id": entry["roll_id"],
                "complexity_tier": entry["complexity_tier"],
                "algebra_group": entry["algebra_group"],
                "earliest_risk_date": entry["earliest_risk_date"],
                "forced_exit_session": entry["forced_exit_session"],
                "phase": phase,
                "fold": fold,
                "entry_relative_volume": entry["relative_volume"],
                "entry_max_leg_bid_ask_ticks": entry["max_leg_bid_ask_ticks"],
                "entry_package_capacity": entry["package_volume_capacity"],
            }
        )
        realized_net += net
        position = 0
        entry = None
        held_bars = 0
        mfe = 0.0
        mae = 0.0
        pending_exit = None

    for index, row in enumerate(rows):
        raw_open = (
            row.get("_bt_open")
            if "_bt_open" in row
            else _number(row, "package_open_value_usd")
        )
        raw_close = (
            row.get("_bt_close")
            if "_bt_close" in row
            else _number(row, "package_close_value_usd")
        )
        if raw_open is None or raw_close is None:
            pending_entry = None
            last_row = row
            continue

        if position and entry is not None and row.get("roll_id") != entry["roll_id"]:
            # This safety path should not be reached because D-3 liquidation is
            # mandatory.  Use the previous close so no synthetic roll gap enters P&L.
            if last_row is not None:
                previous_value = _number(last_row, "package_close_value_usd")
                if previous_value is not None:
                    finish(last_row, previous_value, "ROLL_SAFETY_PREVIOUS_CLOSE", at_close=True)

        next_row = rows[index + 1] if index + 1 < len(rows) else None
        is_last_available_forced_bar = (
            row.get("session_date") == row.get("forced_exit_session")
            and (
                int(row.get("bar_slot") or -1)
                == int(row.get("session_last_slot") or 12)
                or next_row is None
                or next_row.get("session_date") != row.get("session_date")
            )
        )
        if position and is_last_available_forced_bar:
            finish(row, raw_open, "MANDATORY_D4_EXIT")
            pending_entry = None

        if position and pending_exit:
            if entry is not None and row.get("roll_id") == entry["roll_id"]:
                finish(
                    row,
                    raw_open,
                    str(pending_exit["reason"]),
                    exit_signal_time=pending_exit.get("signal_time"),
                )
            pending_exit = None

        if position == 0 and pending_entry is not None:
            if (
                bool(row.get("entry_allowed"))
                and row.get("roll_id") == pending_entry["roll_id"]
            ):
                position = int(pending_entry["direction"])
                entry_cost = float(row.get("one_way_cost_usd") or 0.0)
                entry = {
                    "spread_id": row["spread_id"],
                    "spread_name": row["spread_name"],
                    "entry_time": row["timestamp_utc"],
                    "entry_signal_time": pending_entry["signal_time"],
                    "entry_session": row["session_date"],
                    "entry_spread": _number(row, "spread_open"),
                    "entry_package_value_usd": raw_open,
                    "entry_cost_usd": entry_cost,
                    "entry_reason": pending_entry["reason"],
                    "roll_id": row["roll_id"],
                    "complexity_tier": int(row.get("complexity_tier") or 1),
                    "algebra_group": row.get("algebra_group") or row["spread_id"],
                    "earliest_risk_date": row["earliest_risk_date"],
                    "forced_exit_session": row["forced_exit_session"],
                    "relative_volume": pending_entry["relative_volume"],
                    "max_leg_bid_ask_ticks": pending_entry["max_leg_bid_ask_ticks"],
                    "package_volume_capacity": pending_entry["package_volume_capacity"],
                }
                held_bars = 0
                mfe = 0.0
                mae = 0.0
            pending_entry = None

        if position and entry is not None:
            held_bars += 1
            open_trade_gross = position * (
                raw_close - float(entry["entry_package_value_usd"])
            )
            mfe = max(mfe, open_trade_gross)
            mae = min(mae, open_trade_gross)
            current_vote = (
                int(row.get(vote_column) or 0) if precomputed_vote else None
            )
            reason = _exit_reason(
                strategy.strategy_id,
                row,
                position,
                held_bars,
                current_vote=current_vote,
            )
            if reason:
                pending_exit = {"reason": reason, "signal_time": row["timestamp_utc"]}
        else:
            direction = (
                int(row.get(vote_column) or 0)
                if precomputed_vote
                else _entry_direction(row, strategy.strategy_id)
            )
            if "_bt_liquidity_ok" in row:
                liquidity_ok = bool(row["_bt_liquidity_ok"])
                liquidity_reason = str(row["_bt_liquidity_reason"])
            else:
                liquidity_ok, liquidity_reason = _liquidity_pass(row, config)
            if (
                direction
                and bool(row.get("entry_allowed"))
                and liquidity_ok
                and index + 1 < len(rows)
            ):
                pending_entry = {
                    "direction": direction,
                    "reason": f"{strategy.strategy_id}; {liquidity_reason}",
                    "signal_time": row["timestamp_utc"],
                    "roll_id": row["roll_id"],
                    "relative_volume": _number(row, "relative_volume"),
                    "max_leg_bid_ask_ticks": _number(row, "max_leg_bid_ask_ticks"),
                    "package_volume_capacity": _number(row, "package_volume_capacity"),
                }

        unrealized = 0.0
        if position and entry is not None:
            unrealized = position * (
                raw_close - float(entry["entry_package_value_usd"])
            ) - float(entry["entry_cost_usd"])
        if collect_equity:
            equity_rows.append(
                {
                    "timestamp_utc": row["timestamp_utc"],
                    "session_date": row["session_date"],
                    "spread_id": row["spread_id"],
                    "strategy_id": strategy.strategy_id,
                    "position": position,
                    "realized_net_pnl_usd": realized_net,
                    "equity_usd": realized_net + unrealized,
                }
            )
        last_row = row

    if position and entry is not None and last_row is not None:
        final_value = _number(last_row, "package_close_value_usd")
        if final_value is not None:
            finish(last_row, final_value, "END_OF_SAMPLE", at_close=True)
    return trades, equity_rows


def _daily_metrics(values: Iterable[tuple[date, float]], annual_sessions: int) -> dict[str, float]:
    by_day: dict[date, float] = {}
    for session, value in values:
        by_day[session] = by_day.get(session, 0.0) + float(value)
    series = np.asarray([by_day[item] for item in sorted(by_day)], dtype=float)
    if series.size == 0:
        return {"sharpe": 0.0, "sortino": 0.0, "max_drawdown": 0.0, "calmar": 0.0}
    mean = float(series.mean())
    std = float(series.std(ddof=1)) if series.size > 1 else 0.0
    downside = series[series < 0]
    downside_std = float(downside.std(ddof=1)) if downside.size > 1 else 0.0
    sharpe = mean / std * math.sqrt(annual_sessions) if std > 0 else 0.0
    sortino = mean / downside_std * math.sqrt(annual_sessions) if downside_std > 0 else 0.0
    equity = np.cumsum(series)
    drawdown = equity - np.maximum.accumulate(np.r_[0.0, equity])[-equity.size :]
    max_drawdown = float(abs(drawdown.min())) if drawdown.size else 0.0
    annualized = mean * annual_sessions
    calmar = annualized / max_drawdown if max_drawdown > 0 else 0.0
    return {
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
    }


def _deflated_sharpe_probability(series: np.ndarray, trials: int) -> float:
    """Bailey-Lopez de Prado style DSR with skew/kurtosis and trial penalty."""

    values = np.asarray(series, dtype=float)
    if values.size < 3:
        return 0.0
    mean = float(values.mean())
    std = float(values.std(ddof=1))
    if std <= 0:
        return 0.0
    centered = (values - mean) / std
    skew = float(np.mean(centered**3))
    kurtosis = float(np.mean(centered**4))
    sharpe = mean / std
    variance = (
        1.0
        - skew * sharpe
        + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe
    ) / max(1, values.size - 1)
    standard_error = math.sqrt(max(variance, 1e-12))
    trial_count = max(2, int(trials))
    normal = NormalDist()
    euler_gamma = 0.5772156649015329
    expected_maximum = standard_error * (
        (1.0 - euler_gamma) * normal.inv_cdf(1.0 - 1.0 / trial_count)
        + euler_gamma
        * normal.inv_cdf(1.0 - 1.0 / (trial_count * math.e))
    )
    z_score = (sharpe - expected_maximum) / standard_error
    return normal.cdf(z_score)


def _score_rows(
    trades: pl.DataFrame,
    config: TechnicalConfig,
    trial_count: int,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    if trades.is_empty():
        return pl.DataFrame(), pl.DataFrame()
    rows = trades.to_dicts()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["spread_id"], row["strategy_id"]), []).append(row)
    score_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    names = {item.strategy_id: item for item in STRATEGIES}
    for (spread_id, strategy_id), group in sorted(groups.items()):
        oos = [row for row in group if row["phase"] == "OOS"]
        lockbox = [row for row in group if row["phase"] == "LOCKBOX"]
        evaluation = oos if oos else group
        net = np.asarray([float(row["net_pnl_usd"]) for row in evaluation], dtype=float)
        gross = float(sum(float(row["gross_pnl_usd"]) for row in evaluation))
        costs = float(sum(float(row["cost_usd"]) for row in evaluation))
        winners = net[net > 0]
        losers = net[net < 0]
        metrics = _daily_metrics(
            ((row["exit_session"], float(row["net_pnl_usd"])) for row in evaluation),
            config.backtest.annual_sessions,
        )
        daily_by_session: dict[date, float] = {}
        for row in evaluation:
            session = row["exit_session"]
            daily_by_session[session] = daily_by_session.get(session, 0.0) + float(
                row["net_pnl_usd"]
            )
        daily_series = np.asarray(
            [daily_by_session[item] for item in sorted(daily_by_session)], dtype=float
        )
        folds = sorted({int(row["fold"]) for row in oos if row.get("fold") is not None})
        profitable_folds = 0
        for fold in folds:
            fold_group = [row for row in oos if row.get("fold") == fold]
            fold_net = float(sum(float(row["net_pnl_usd"]) for row in fold_group))
            fold_metrics = _daily_metrics(
                ((row["exit_session"], float(row["net_pnl_usd"])) for row in fold_group),
                config.backtest.annual_sessions,
            )
            profitable_folds += int(fold_net > 0)
            fold_rows.append(
                {
                    "spread_id": spread_id,
                    "strategy_id": strategy_id,
                    "fold": fold,
                    "trades": len(fold_group),
                    "net_pnl_usd": fold_net,
                    "sharpe": fold_metrics["sharpe"],
                    "max_drawdown_usd": fold_metrics["max_drawdown"],
                }
            )
        fold_share = profitable_folds / len(folds) if folds else 0.0
        profit_factor = (
            float(winners.sum()) / abs(float(losers.sum()))
            if losers.size and abs(float(losers.sum())) > 0
            else (99.0 if winners.size else 0.0)
        )
        probability = _deflated_sharpe_probability(daily_series, trial_count)
        lockbox_net = float(sum(float(row["net_pnl_usd"]) for row in lockbox))
        complexity_tier = int(evaluation[0].get("complexity_tier") or 1)
        complexity_minimum_trades = int(
            math.ceil(
                config.backtest.minimum_oos_trades
                * (1.0 + 0.5 * (complexity_tier - 1))
            )
        )
        passes = (
            len(oos) >= complexity_minimum_trades
            and fold_share >= config.backtest.minimum_profitable_fold_share
            and profit_factor >= config.backtest.minimum_profit_factor
            and probability >= config.backtest.minimum_probability_sharpe
            and float(sum(net)) > 0
        )
        status = "VALIDATED" if passes else "RESEARCH_ONLY"
        score_rows.append(
            {
                "spread_id": spread_id,
                "spread_name": evaluation[0]["spread_name"],
                "strategy_id": strategy_id,
                "strategy_name": names[strategy_id].display_name,
                "trial_id": f"{spread_id}|{strategy_id}",
                "complexity_tier": complexity_tier,
                "algebra_group": evaluation[0].get("algebra_group") or spread_id,
                "minimum_oos_trades_hurdle": complexity_minimum_trades,
                "status": status,
                "evaluation_scope": "OOS" if oos else "ALL_PROVISIONAL",
                "lockbox_used_for_selection": False,
                "trades": len(evaluation),
                "oos_trades": len(oos),
                "lockbox_trades": len(lockbox),
                "gross_pnl_usd": gross,
                "net_pnl_usd": float(net.sum()) if net.size else 0.0,
                "lockbox_net_pnl_usd": lockbox_net,
                "cost_drag_usd": costs,
                "net_pnl_2x_cost_usd": float(
                    sum(float(row["pnl_2x_cost_usd"]) for row in evaluation)
                ),
                "net_pnl_3x_cost_usd": float(
                    sum(float(row["pnl_3x_cost_usd"]) for row in evaluation)
                ),
                "daily_sharpe": metrics["sharpe"],
                "daily_sortino": metrics["sortino"],
                "max_drawdown_usd": metrics["max_drawdown"],
                "calmar": metrics["calmar"],
                "win_rate": float((net > 0).mean()) if net.size else 0.0,
                "profit_factor": profit_factor,
                "expectancy_usd": float(net.mean()) if net.size else 0.0,
                "median_holding_bars": float(
                    np.median([row["holding_bars"] for row in evaluation])
                ),
                "profitable_fold_share": fold_share,
                "deflated_sharpe_probability": probability,
                "expiry_exits": sum(
                    row["exit_reason"] == "MANDATORY_D4_EXIT" for row in evaluation
                ),
                "long_net_pnl_usd": float(
                    sum(row["net_pnl_usd"] for row in evaluation if row["direction"] == "LONG")
                ),
                "short_net_pnl_usd": float(
                    sum(row["net_pnl_usd"] for row in evaluation if row["direction"] == "SHORT")
                ),
            }
        )
    score = pl.DataFrame(score_rows).sort(
        ["status", "daily_sharpe", "net_pnl_usd"], descending=[False, True, True]
    )
    folds = pl.DataFrame(fold_rows).sort(["spread_id", "strategy_id", "fold"]) if fold_rows else pl.DataFrame()
    return score, folds


def _complete_scorecard(
    score: pl.DataFrame, model_features: pl.DataFrame, config: TechnicalConfig
) -> pl.DataFrame:
    existing = (
        {(str(row["spread_id"]), str(row["strategy_id"])) for row in score.to_dicts()}
        if not score.is_empty()
        else set()
    )
    observed_ids = (
        set(str(item) for item in model_features["spread_id"].unique().to_list())
        if not model_features.is_empty()
        else set()
    )
    spread_meta = [
        {
            "spread_id": item.spread_id,
            "spread_name": item.display_name,
            "complexity_tier": item.complexity_tier,
            "algebra_group": item.algebra_group,
        }
        for item in config.spreads
        if item.model_enabled
    ]
    missing: list[dict[str, Any]] = []
    for spread in spread_meta:
        for strategy in STRATEGIES:
            key = (str(spread["spread_id"]), strategy.strategy_id)
            if key in existing:
                continue
            tier = int(spread["complexity_tier"] or 1)
            missing.append(
                {
                    "spread_id": spread["spread_id"],
                    "spread_name": spread["spread_name"],
                    "strategy_id": strategy.strategy_id,
                    "strategy_name": strategy.display_name,
                    "trial_id": f"{spread['spread_id']}|{strategy.strategy_id}",
                    "complexity_tier": tier,
                    "algebra_group": spread["algebra_group"],
                    "minimum_oos_trades_hurdle": int(
                        math.ceil(
                            config.backtest.minimum_oos_trades
                            * (1.0 + 0.5 * (tier - 1))
                        )
                    ),
                    "status": (
                        "NO_TRADES"
                        if str(spread["spread_id"]) in observed_ids
                        else "NO_DATA"
                    ),
                    "evaluation_scope": "OOS",
                    "lockbox_used_for_selection": False,
                    "trades": 0,
                    "oos_trades": 0,
                    "lockbox_trades": 0,
                    "gross_pnl_usd": 0.0,
                    "net_pnl_usd": 0.0,
                    "lockbox_net_pnl_usd": 0.0,
                    "cost_drag_usd": 0.0,
                    "net_pnl_2x_cost_usd": 0.0,
                    "net_pnl_3x_cost_usd": 0.0,
                    "daily_sharpe": 0.0,
                    "daily_sortino": 0.0,
                    "max_drawdown_usd": 0.0,
                    "calmar": 0.0,
                    "win_rate": 0.0,
                    "profit_factor": 0.0,
                    "expectancy_usd": 0.0,
                    "median_holding_bars": 0.0,
                    "profitable_fold_share": 0.0,
                    "deflated_sharpe_probability": 0.0,
                    "expiry_exits": 0,
                    "long_net_pnl_usd": 0.0,
                    "short_net_pnl_usd": 0.0,
                }
            )
    frames = [item for item in (score, pl.DataFrame(missing) if missing else pl.DataFrame()) if not item.is_empty()]
    return (
        pl.concat(frames, how="diagonal_relaxed")
        .sort(["status", "daily_sharpe", "net_pnl_usd"], descending=[False, True, True])
        if frames
        else pl.DataFrame()
    )


def _simulate_spread_bundle(
    payload: tuple[
        list[dict[str, Any]],
        TechnicalConfig,
        Sequence[Mapping[str, Any]],
        date | None,
    ]
) -> list[dict[str, Any]]:
    """Run all preregistered strategies for one spread in a worker process."""

    prepared_rows, config, windows, lockbox_start = payload
    trades: list[dict[str, Any]] = []
    placeholder = pl.DataFrame()
    for row in prepared_rows:
        row["_bt_open"] = _number(row, "package_open_value_usd")
        row["_bt_close"] = _number(row, "package_close_value_usd")
        liquidity_ok, liquidity_reason = _liquidity_pass(row, config)
        row["_bt_liquidity_ok"] = liquidity_ok
        row["_bt_liquidity_reason"] = liquidity_reason
    for strategy in STRATEGIES:
        strategy_trades, _equity = _simulate_strategy(
            placeholder,
            strategy,
            config,
            windows,
            lockbox_start,
            prepared_rows=prepared_rows,
        )
        trades.extend(strategy_trades)
    return trades


def _initialize_backtest_worker(
    config: TechnicalConfig,
    windows: Sequence[Mapping[str, Any]],
    lockbox_start: date | None,
) -> None:
    """Load immutable shared settings once in each spawned Windows worker."""

    global _WORKER_CONFIG, _WORKER_WINDOWS, _WORKER_LOCKBOX_START
    _WORKER_CONFIG = config
    _WORKER_WINDOWS = tuple(windows)
    _WORKER_LOCKBOX_START = lockbox_start


def _simulate_spread_frame(spread_frame: pl.DataFrame) -> list[dict[str, Any]]:
    if _WORKER_CONFIG is None:
        raise RuntimeError("Backtest worker was not initialized")
    return _simulate_spread_bundle(
        (
            spread_frame.to_dicts(),
            _WORKER_CONFIG,
            _WORKER_WINDOWS,
            _WORKER_LOCKBOX_START,
        )
    )


def run_backtests(features: pl.DataFrame, config: TechnicalConfig) -> BacktestResult:
    if features.is_empty():
        empty = pl.DataFrame()
        return BacktestResult(empty, empty, empty, empty, strategy_library_frame())
    model_features = (
        features.filter(pl.col("model_enabled"))
        if "model_enabled" in features.columns
        else features
    )
    if model_features.is_empty():
        empty = pl.DataFrame()
        return BacktestResult(empty, empty, empty, empty, strategy_library_frame())
    sessions = sorted(model_features["session_date"].unique().to_list())
    windows = _walk_forward_windows(sessions, config)
    lockbox_start = (
        sessions[-config.backtest.lockbox_sessions]
        if len(sessions) >= config.backtest.lockbox_sessions
        else None
    )
    missing_backtest_columns = sorted(set(BACKTEST_COLUMNS) - set(model_features.columns))
    if missing_backtest_columns:
        raise ValueError(
            "Backtest features are missing required columns: "
            + ", ".join(missing_backtest_columns)
        )
    null_vote_columns = [
        column
        for column in BACKTEST_VOTE_COLUMNS
        if model_features[column].null_count()
    ]
    if null_vote_columns:
        raise ValueError(
            "Backtest vote columns contain nulls and cannot use the optimized "
            "simulation path: " + ", ".join(null_vote_columns)
        )
    simulation_features = model_features.select(BACKTEST_COLUMNS).sort(
        ["spread_id", "timestamp_utc"]
    )
    spread_partitions = simulation_features.partition_by(
        "spread_id", maintain_order=True
    )
    configured_workers = int(config.backtest.parallel_workers)
    workers = configured_workers or min(4, max(1, (os.cpu_count() or 2) - 1))
    if workers == 1 or len(spread_partitions) < 2:
        bundles = [
            _simulate_spread_bundle(
                (
                    spread_frame.to_dicts(),
                    config,
                    windows,
                    lockbox_start,
                )
            )
            for spread_frame in spread_partitions
        ]
    else:
        worker_count = min(workers, len(spread_partitions))
        completed: dict[int, list[dict[str, Any]]] = {}
        iterator = iter(enumerate(spread_partitions))
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_initialize_backtest_worker,
            initargs=(config, tuple(windows), lockbox_start),
        ) as executor:
            pending = {}

            def submit_next() -> bool:
                try:
                    index, spread_frame = next(iterator)
                except StopIteration:
                    return False
                pending[executor.submit(_simulate_spread_frame, spread_frame)] = index
                return True

            for _ in range(worker_count):
                submit_next()
            while pending:
                done, _not_done = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    index = pending.pop(future)
                    completed[index] = future.result()
                    submit_next()
        bundles = [completed[index] for index in range(len(spread_partitions))]
    trade_rows = [row for bundle in bundles for row in bundle]
    trades = _trade_frame(trade_rows)
    equity = (
        trades.group_by(
            ["spread_id", "strategy_id", "exit_time", "exit_session"],
            maintain_order=True,
        )
        .agg(pl.col("net_pnl_usd").sum().alias("realized_increment_usd"))
        .sort(["spread_id", "strategy_id", "exit_time"])
        .with_columns(
            pl.col("realized_increment_usd")
            .cum_sum()
            .over(["spread_id", "strategy_id"])
            .alias("equity_usd")
        )
        if not trades.is_empty()
        else pl.DataFrame()
    )
    score, folds = _score_rows(
        trades,
        config,
        max(
            1,
            sum(item.model_enabled for item in config.spreads) * len(STRATEGIES),
        ),
    )
    score = _complete_scorecard(score, model_features, config)
    return BacktestResult(trades, score, folds, equity, strategy_library_frame())


def build_live_signal_board(
    features: pl.DataFrame,
    scorecard: pl.DataFrame,
    config: TechnicalConfig,
    *,
    depth_source: str,
    depth_metrics: pl.DataFrame | None = None,
    quality_blocking: bool,
    demo_mode: bool,
    model_stale: bool = False,
    model_age_sessions: int = 0,
) -> pl.DataFrame:
    if features.is_empty():
        return pl.DataFrame()
    score_rows = scorecard.to_dicts() if not scorecard.is_empty() else []
    spread_specs = {item.spread_id: item for item in config.spreads}
    by_spread: dict[str, list[dict[str, Any]]] = {}
    for score in score_rows:
        by_spread.setdefault(str(score["spread_id"]), []).append(score)
    depth_by_spread = {
        str(item["spread_id"]): item
        for item in depth_metrics.to_dicts()
    } if depth_metrics is not None and not depth_metrics.is_empty() else {}
    rows: list[dict[str, Any]] = []
    latest_rows = (
        features.sort(["spread_id", "timestamp_utc"])
        .group_by("spread_id", maintain_order=True)
        .tail(1)
        .to_dicts()
    )
    for row in latest_rows:
        depth = depth_by_spread.get(str(row["spread_id"]), {})
        votes = _component_entries(row)
        candidates = sorted(
            by_spread.get(str(row["spread_id"]), []),
            key=lambda item: (
                item.get("status") == "VALIDATED",
                float(item.get("deflated_sharpe_probability") or 0.0),
                float(item.get("profitable_fold_share") or 0.0),
                float(item.get("daily_sharpe") or 0.0),
                float(item.get("net_pnl_usd") or 0.0),
            ),
            reverse=True,
        )
        base_votes = {key: votes[key] for key in BASE_EXPERT_IDS}
        vote_sum = sum(base_votes.values())
        positive = sum(value > 0 for value in base_votes.values())
        negative = sum(value < 0 for value in base_votes.values())
        adaptive_vote = int(row.get("adaptive_vote") or 0)
        fixed_vote = int(votes.get("REGIME_ENSEMBLE") or 0)
        validated_directional = [
            (item, int(votes.get(str(item.get("strategy_id"))) or 0))
            for item in candidates
            if item.get("status") == "VALIDATED"
            and int(votes.get(str(item.get("strategy_id"))) or 0)
        ]
        if validated_directional:
            selected_evidence, direction = validated_directional[0]
            signal_strategy_id = str(selected_evidence["strategy_id"])
        else:
            direction = adaptive_vote if adaptive_vote else fixed_vote
            signal_strategy_id = (
                "ADAPTIVE_EXPERT_MIX" if adaptive_vote else "REGIME_ENSEMBLE"
            )
        liquidity_ok, liquidity_reason = _liquidity_pass(row, config)
        median = _number(row, "rolling_median")
        mad = _number(row, "rolling_mad")
        scale = 1.4826 * mad if mad is not None else None
        current = _number(row, "spread_close")
        buy_target = median - 1.8 * scale if None not in (median, scale) else None
        sell_target = median + 1.8 * scale if None not in (median, scale) else None
        seasonal = _number(row, "seasonal_fair_value_1d")
        fair_value = (
            0.7 * median + 0.3 * seasonal
            if median is not None and seasonal is not None
            else median if median is not None else seasonal
        )
        volatility = _number(row, "ewma_abs_change", 0.0) or 0.0
        relative_volume = _number(row, "relative_volume", 0.0) or 0.0
        heuristic_confidence = min(
            0.95,
            0.38
            + 0.10 * min(3, max(positive, negative))
            + 0.12 * min(2.0, relative_volume) / 2.0
            + 0.08 * min(3.0, abs(_number(row, "robust_z", 0.0) or 0.0)) / 3.0,
        )
        complexity_tier = int(row.get("complexity_tier") or 1)
        heuristic_confidence = max(
            0.0, heuristic_confidence - 0.04 * (complexity_tier - 1)
        )
        adaptive_confidence = _number(row, "adaptive_confidence")
        raw_confidence = (
            adaptive_confidence
            if adaptive_vote and adaptive_confidence is not None
            else heuristic_confidence
        )
        if depth_source != "BPIPE_L2":
            raw_confidence = min(raw_confidence, config.liquidity.cap_confidence_without_depth)
        if depth and not bool(depth.get("depth_fresh")):
            raw_confidence = min(raw_confidence, 0.55)
        # Frozen expert weights serialize from Float32 through JSON. Keep the
        # reported confidence byte-stable across train/score and Python/Polars
        # runtimes without changing the full-precision decision threshold.
        reported_confidence = round(raw_confidence, 6)
        model_enabled = bool(row.get("model_enabled", True))
        pattern_state, pattern_strength, pattern_agreement, pattern_components = (
            _pattern_diagnostic(
                row,
                positive_votes=positive,
                negative_votes=negative,
                model_enabled=model_enabled,
                model_stale=model_stale,
                config=config,
            )
        )
        direction_validated = any(
            item.get("status") == "VALIDATED"
            and item.get("strategy_id") == signal_strategy_id
            for item in by_spread.get(str(row["spread_id"]), [])
        )
        expected_price_edge = 0.0
        if direction and current is not None and fair_value is not None:
            expected_price_edge = max(0.0, direction * (fair_value - current))
        package_barrels = _number(row, "package_barrels", 0.0) or 0.0
        expected_edge_usd = expected_price_edge * package_barrels
        round_trip_cost = 2.0 * (_number(row, "one_way_cost_usd", 0.0) or 0.0)
        edge_to_cost = (
            expected_edge_usd / round_trip_cost if round_trip_cost > 0 else 0.0
        )
        if not model_enabled:
            status = "ANALYTIC ONLY"
        elif model_stale:
            status = "MODEL STALE"
        elif quality_blocking:
            status = "DATA BLOCK"
        elif not bool(row.get("entry_allowed")):
            status = "EXPIRY BLOCK"
        elif not liquidity_ok:
            status = "NO TRADE"
        elif (
            config.liquidity.require_true_l2_for_enter
            and not bool(depth.get("depth_supports_one_package"))
        ):
            status = "DEPTH BLOCK"
        elif bool(row.get("change_point_alarm", False)):
            status = "REGIME BLOCK"
        elif direction and edge_to_cost < 3.0:
            status = "WATCH"
        elif (
            direction > 0
            and raw_confidence >= config.backtest.minimum_confidence
            and direction_validated
        ):
            status = "BUY"
        elif (
            direction < 0
            and raw_confidence >= config.backtest.minimum_confidence
            and direction_validated
        ):
            status = "SELL"
        elif positive or negative:
            status = "WATCH"
        else:
            status = "FLAT"

        best = candidates[0] if candidates else None
        grade = (
            "DEMO ONLY - NOT VALIDATED"
            if demo_mode
            else "A - VALIDATED"
            if best and best.get("status") == "VALIDATED"
            else "B - POSITIVE RESEARCH"
            if best and float(best.get("net_pnl_usd") or 0.0) > 0
            else "C - INSUFFICIENT EVIDENCE"
        )
        capacity = _number(row, "package_volume_capacity", 0.0) or 0.0
        spread_spec = spread_specs.get(str(row["spread_id"]))
        label_fields = (
            trade_code_fields(spread_spec, row)
            if spread_spec is not None
            else {
                "trade_code": str(row.get("spread_name") or row["spread_id"]),
                "trade_code_short": str(
                    row.get("spread_name") or row["spread_id"]
                ),
                "contract_codes": str(row.get("roll_id") or ""),
                "contract_months": "",
                "structure_roots": "",
                "calculation_unit": "USD/bbl",
                "display_unit": "USD/bbl",
                "display_level_factor": 1.0,
                "quote_convention": "NORMALIZED_USD_BBL",
                "conversion_method": "Normalized USD/bbl package quote",
            }
        )
        display_factor = float(label_fields["display_level_factor"])

        def display_level(value: float | None) -> float | None:
            return value * display_factor if value is not None else None

        rows.append(
            {
                "as_of_utc": row["timestamp_utc"],
                "session_date": row["session_date"],
                "spread_id": row["spread_id"],
                "spread_name": row["spread_name"],
                "family": row["spread_family"],
                **label_fields,
                "model_enabled": model_enabled,
                "model_stale": model_stale,
                "model_age_sessions": model_age_sessions,
                "complexity_tier": row.get("complexity_tier"),
                "algebra_group": row.get("algebra_group"),
                "status": status,
                "current_spread": current,
                "buy_entry_ceiling": buy_target,
                "sell_entry_floor": sell_target,
                "fair_value_target": fair_value,
                "long_stop": current - 2.5 * volatility if current is not None else None,
                "short_stop": current + 2.5 * volatility if current is not None else None,
                "display_current": display_level(current),
                "display_buy_entry": display_level(buy_target),
                "display_sell_entry": display_level(sell_target),
                "display_fair_value": display_level(fair_value),
                "display_long_stop": display_level(
                    current - 2.5 * volatility if current is not None else None
                ),
                "display_short_stop": display_level(
                    current + 2.5 * volatility if current is not None else None
                ),
                "heating_oil_cpg": _number(row, "heating_oil_cpg"),
                "gasoil_usd_mt": _number(row, "gasoil_usd_mt"),
                "gasoil_usd_bbl": _number(row, "gasoil_usd_bbl"),
                "gasoil_cpg": _number(row, "gasoil_cpg"),
                "hogo_cpg": _number(row, "hogo_cpg"),
                "confidence": reported_confidence,
                "signal_strategy_id": signal_strategy_id,
                "direction_evidence_validated": direction_validated,
                "adaptive_score": _number(row, "adaptive_score"),
                "adaptive_status": row.get("adaptive_status"),
                "adaptive_observations": row.get("adaptive_observations"),
                "adaptive_top_expert": row.get("adaptive_top_expert"),
                "adaptive_top_weight": _number(row, "adaptive_top_weight"),
                "kronos_expected_move_1b": _number(
                    row, "kronos_expected_move_1b"
                ),
                "kronos_vote": int(row.get("kronos_vote") or 0),
                "kronos_contract_coverage": _number(
                    row, "kronos_contract_coverage", 0.0
                ),
                "kronos_status": row.get("kronos_status") or "DISABLED_OR_NOT_RUN",
                "kronos_action_eligible": bool(
                    row.get("kronos_action_eligible", False)
                ),
                "strategy_votes_long": positive,
                "strategy_votes_short": negative,
                "vote_balance": vote_sum,
                "pattern_state": pattern_state,
                "pattern_strength": pattern_strength,
                "pattern_agreement": pattern_agreement,
                "pattern_components": pattern_components,
                "expected_edge_usd": expected_edge_usd,
                "round_trip_cost_usd": round_trip_cost,
                "expected_edge_to_cost": edge_to_cost,
                "top_strategy": best.get("strategy_name") if best else "None",
                "oos_grade": grade,
                "relative_volume": relative_volume,
                "pvo": _number(row, "pvo"),
                "tod_normalized_change": _number(row, "tod_normalized_change"),
                "vol_regime_ratio_1d_20d": _number(
                    row, "vol_regime_ratio_1d_20d"
                ),
                "liquidity_stress_ratio": _number(
                    row, "liquidity_stress_ratio"
                ),
                "tail_event_rate_20d": _number(row, "tail_event_rate_20d"),
                "robust_volume_surprise": _number(
                    row, "robust_volume_surprise"
                ),
                "return_skew_5d": _number(row, "return_skew_5d"),
                "return_excess_kurtosis_5d": _number(
                    row, "return_excess_kurtosis_5d"
                ),
                "realized_vol_of_vol_5d": _number(
                    row, "realized_vol_of_vol_5d"
                ),
                "trend_hac_t_stat_3d": _number(row, "trend_hac_t_stat_3d"),
                "close_path_choppiness_5d": _number(
                    row, "close_path_choppiness_5d"
                ),
                "advanced_risk_regime": row.get("advanced_risk_regime"),
                "relationship_health_scope": row.get(
                    "relationship_health_scope"
                ),
                "package_volume_capacity": capacity,
                "max_packages_at_1pct_participation": math.floor(
                    capacity * config.liquidity.maximum_volume_participation
                ),
                "paired_volume_bbl": _number(row, "paired_volume_bbl"),
                "min_leg_events": _number(row, "min_leg_events"),
                "average_trade_size_bbl_proxy": _number(row, "average_trade_size_bbl_proxy"),
                "max_leg_bid_ask_ticks": _number(row, "max_leg_bid_ask_ticks"),
                "quoted_width_usd_bbl": _number(row, "quoted_width_usd_bbl"),
                "depth_source": depth_source,
                "buy_package_depth": _number(depth, "buy_package_depth"),
                "sell_package_depth": _number(depth, "sell_package_depth"),
                "package_depth_imbalance": _number(
                    depth, "package_depth_imbalance"
                ),
                "depth_snapshot_age_minutes": _number(
                    depth, "depth_snapshot_age_minutes"
                ),
                "depth_fresh": bool(depth.get("depth_fresh", False)),
                "depth_supports_one_package": bool(
                    depth.get("depth_supports_one_package", False)
                ),
                "liquidity_gate": liquidity_reason,
                "earliest_risk_date": row["earliest_risk_date"],
                "mandatory_last_exit_session": row["forced_exit_session"],
                "sessions_to_risk_date": row["sessions_to_risk_date"],
                "roll_id": row["roll_id"],
                "robust_z": _number(row, "robust_z"),
                "rsi": _number(row, "rsi"),
                "macd_histogram": _number(row, "macd_histogram"),
                "seasonal_z": _number(row, "seasonal_z"),
                "seasonal_expected_move_1d": _number(
                    row, "seasonal_expected_move_1d"
                ),
                "seasonal_confidence": _number(row, "seasonal_confidence"),
                "mean_reversion_stability": _number(
                    row, "mean_reversion_stability"
                ),
                "variance_ratio_5": _number(row, "variance_ratio_5"),
                "hurst_exponent_proxy": _number(row, "hurst_exponent_proxy"),
                "permutation_entropy_3": _number(row, "permutation_entropy_3"),
                "change_point_alarm": bool(row.get("change_point_alarm", False)),
                "regime": row.get("regime"),
                "demo_mode": demo_mode,
            }
        )
    board = _apply_current_trade_budget(
        pl.DataFrame(rows, infer_schema_length=None), config
    )
    return board.sort(
        ["status", "confidence", "spread_id"], descending=[False, True, False]
    )


__all__ = [
    "BASE_EXPERT_IDS",
    "BacktestResult",
    "STRATEGIES",
    "StrategyDefinition",
    "add_adaptive_ensemble",
    "apply_frozen_expert_model",
    "build_live_signal_board",
    "run_backtests",
    "strategy_library_frame",
    "walk_forward_window_frame",
]
