"""Source-backed trade briefs and frozen-model performance summaries."""

from __future__ import annotations

from datetime import date
import math
from typing import Any, Mapping

import polars as pl


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _level(value: object) -> str:
    number = _number(value)
    return "n/a" if number is None else f"{number:,.3f}"


def _money(value: object) -> str:
    number = _number(value)
    if number is not None and abs(number) < 1e-9:
        number = 0.0
    return "n/a" if number is None else f"${number:,.0f}"


def _percent(value: object) -> str:
    number = _number(value)
    return "n/a" if number is None else f"{100.0 * number:.1f}%"


def selected_strategy_trades(
    trades: pl.DataFrame,
    selected_scorecard: pl.DataFrame,
    *,
    start_session: date,
    end_session: date,
) -> pl.DataFrame:
    """Return trades for each structure's single frozen selected strategy."""

    if trades.is_empty() or selected_scorecard.is_empty():
        return pl.DataFrame()
    keys = selected_scorecard.select("spread_id", "strategy_id").unique()
    return (
        trades.join(keys, on=["spread_id", "strategy_id"], how="inner")
        .filter(
            (pl.col("exit_session") >= start_session)
            & (pl.col("exit_session") <= end_session)
        )
        .sort(["spread_id", "strategy_id", "exit_time"])
    )


def _recent_structure_metrics(recent: pl.DataFrame) -> pl.DataFrame:
    if recent.is_empty():
        return pl.DataFrame()
    equity = recent.with_columns(
        pl.col("net_pnl_usd")
        .cum_sum()
        .over(["spread_id", "strategy_id"])
        .alias("_recent_equity")
    ).with_columns(
        pl.max_horizontal(
            pl.col("_recent_equity")
            .cum_max()
            .over(["spread_id", "strategy_id"]),
            pl.lit(0.0),
        )
        .alias("_recent_peak")
    ).with_columns(
        (pl.col("_recent_equity") - pl.col("_recent_peak")).alias(
            "_recent_drawdown"
        )
    )
    grouped = equity.group_by("spread_id", maintain_order=True).agg(
        pl.len().alias("recent_30_trades"),
        pl.col("entry_session").min().alias("recent_30_first_entry"),
        pl.col("exit_session").max().alias("recent_30_last_exit"),
        pl.col("gross_pnl_usd").sum().alias("recent_30_gross_pnl_usd"),
        pl.col("cost_usd").sum().alias("recent_30_cost_usd"),
        pl.col("net_pnl_usd").sum().alias("recent_30_net_pnl_usd"),
        (pl.col("net_pnl_usd") > 0).mean().alias("recent_30_win_rate"),
        pl.col("net_pnl_usd").mean().alias("recent_30_expectancy_usd"),
        pl.col("holding_bars").median().alias("recent_30_median_holding_bars"),
        pl.when(pl.col("net_pnl_usd") > 0)
        .then(pl.col("net_pnl_usd"))
        .otherwise(0.0)
        .sum()
        .alias("_recent_winner_sum"),
        pl.when(pl.col("net_pnl_usd") < 0)
        .then(pl.col("net_pnl_usd"))
        .otherwise(0.0)
        .sum()
        .alias("_recent_loser_sum"),
        (-pl.col("_recent_drawdown").min())
        .clip(0.0, None)
        .alias("recent_30_max_drawdown_usd"),
        pl.when(pl.col("direction") == "LONG")
        .then(pl.col("net_pnl_usd"))
        .otherwise(0.0)
        .sum()
        .alias("recent_30_long_net_pnl_usd"),
        pl.when(pl.col("direction") == "SHORT")
        .then(pl.col("net_pnl_usd"))
        .otherwise(0.0)
        .sum()
        .alias("recent_30_short_net_pnl_usd"),
    )
    return grouped.with_columns(
        pl.when(pl.col("_recent_loser_sum").abs() > 0)
        .then(pl.col("_recent_winner_sum") / pl.col("_recent_loser_sum").abs())
        .when(pl.col("_recent_winner_sum") > 0)
        .then(99.0)
        .otherwise(0.0)
        .alias("recent_30_profit_factor")
    ).drop("_recent_winner_sum", "_recent_loser_sum")


def _description(row: Mapping[str, Any], *, demo_mode: bool) -> str:
    status = str(row.get("current_status") or "UNKNOWN")
    spread_id = str(row.get("spread_id") or "structure")
    trade_code = str(row.get("trade_code") or spread_id)
    display_unit = str(row.get("display_unit") or "USD/bbl")
    current = _level(row.get("display_current"))
    buy = _level(row.get("display_buy_entry"))
    sell = _level(row.get("display_sell_entry"))
    fair = _level(row.get("display_fair_value"))
    long_stop = _level(row.get("display_long_stop"))
    short_stop = _level(row.get("display_short_stop"))
    confidence = _percent(row.get("confidence"))
    strategy = str(row.get("selected_strategy_name") or "No selected model strategy")
    evidence = str(row.get("selected_strategy_status") or "ANALYTIC_ONLY")
    recent_trades = int(row.get("recent_30_trades") or 0)
    recent_net = _money(row.get("recent_30_net_pnl_usd"))
    recent_win = _percent(row.get("recent_30_win_rate"))
    oos_trades = int(row.get("model_oos_trades") or 0)
    oos_net = _money(row.get("model_net_pnl_usd"))
    oos_sharpe = _number(row.get("model_daily_sharpe"))
    sharpe_text = "n/a" if oos_sharpe is None else f"{oos_sharpe:.2f}"
    liquidity = str(row.get("liquidity_gate") or "UNKNOWN")
    risk_regime = str(row.get("advanced_risk_regime") or "UNAVAILABLE")
    relationship_scope = str(
        row.get("relationship_health_scope") or "UNAVAILABLE"
    )
    tod_shock = _number(row.get("tod_normalized_change"))
    liquidity_stress = _number(row.get("liquidity_stress_ratio"))
    tail_rate = _number(row.get("tail_event_rate_20d"))
    robust_volume = _number(row.get("robust_volume_surprise"))
    pattern_state = str(row.get("pattern_state") or "UNAVAILABLE")
    pattern_strength = _number(row.get("pattern_strength"))
    pattern_agreement = _number(row.get("pattern_agreement"))
    pattern_strength_text = (
        "n/a" if pattern_strength is None else f"{pattern_strength:.2f}"
    )
    pattern_agreement_text = (
        "n/a" if pattern_agreement is None else f"{100.0 * pattern_agreement:.0f}%"
    )
    tod_text = "n/a" if tod_shock is None else f"{tod_shock:.2f}z"
    stress_text = (
        "n/a" if liquidity_stress is None else f"{liquidity_stress:.2f}x"
    )
    tail_text = "n/a" if tail_rate is None else f"{100.0 * tail_rate:.1f}%"
    volume_text = "n/a" if robust_volume is None else f"{robust_volume:.2f}z"
    last_exit = str(row.get("mandatory_last_exit_session") or "n/a")
    scope = "Synthetic demo; not tradeable. " if demo_mode else ""
    return (
        f"{scope}{status}: {trade_code} is {current} {display_unit}. "
        f"Buy entry is at or below {buy}; "
        f"sell entry is at or above {sell}; current fair-value target is {fair}. "
        f"Long/short risk stops are {long_stop}/{short_stop}; confidence is {confidence}. "
        f"Selected evidence: {strategy} ({evidence}), {oos_trades} OOS trades, "
        f"OOS net {oos_net}, daily Sharpe {sharpe_text}. Latest 30 sessions: "
        f"{recent_trades} trades, net {recent_net}, win rate {recent_win}. "
        f"Dynamic pattern: {pattern_state}, strength {pattern_strength_text}, "
        f"agreement {pattern_agreement_text}. "
        f"Advanced risk: {risk_regime}; time-of-day shock {tod_text}, liquidity "
        f"stress {stress_text}, 20-day tail-event rate {tail_text}, robust volume "
        f"{volume_text}; relationship scope {relationship_scope}. "
        f"Liquidity gate: {liquidity}; mandatory last exit: {last_exit}."
    )


def build_structure_summaries(
    signals: pl.DataFrame,
    selected_scorecard: pl.DataFrame,
    trades: pl.DataFrame,
    *,
    lockbox_start: date,
    lockbox_end: date,
    demo_mode: bool,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Build current trade briefs plus selected-strategy OOS/30-session evidence."""

    if signals.is_empty():
        return pl.DataFrame(), pl.DataFrame()
    current = signals.select(
        "spread_id",
        "spread_name",
        "family",
        "trade_code",
        "trade_code_short",
        "contract_codes",
        "contract_months",
        "structure_roots",
        "calculation_unit",
        "display_unit",
        "display_level_factor",
        "quote_convention",
        "conversion_method",
        "portfolio_action",
        "portfolio_selected",
        "portfolio_rank",
        "portfolio_candidate_rank",
        "daily_trade_limit",
        "daily_trade_slots_remaining",
        "model_enabled",
        "model_stale",
        "model_age_sessions",
        "complexity_tier",
        "status",
        "current_spread",
        "buy_entry_ceiling",
        "sell_entry_floor",
        "fair_value_target",
        "long_stop",
        "short_stop",
        "display_current",
        "display_buy_entry",
        "display_sell_entry",
        "display_fair_value",
        "display_long_stop",
        "display_short_stop",
        "heating_oil_cpg",
        "gasoil_usd_mt",
        "gasoil_usd_bbl",
        "gasoil_cpg",
        "hogo_cpg",
        "confidence",
        "signal_strategy_id",
        "direction_evidence_validated",
        "adaptive_score",
        "adaptive_observations",
        "adaptive_top_expert",
        "adaptive_top_weight",
        "strategy_votes_long",
        "strategy_votes_short",
        "pattern_state",
        "pattern_strength",
        "pattern_agreement",
        "pattern_components",
        "expected_edge_usd",
        "round_trip_cost_usd",
        "expected_edge_to_cost",
        "relative_volume",
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
        "package_volume_capacity",
        "max_leg_bid_ask_ticks",
        "depth_source",
        "liquidity_gate",
        "mandatory_last_exit_session",
        "sessions_to_risk_date",
        "regime",
        "vote_balance",
        "kronos_expected_move_1b",
        "kronos_status",
        "demo_mode",
    ).rename(
        {
            "status": "current_status",
            "current_spread": "current_level",
        }
    )
    model_columns = [
        "spread_id",
        "strategy_id",
        "strategy_name",
        "status",
        "evaluation_scope",
        "oos_trades",
        "lockbox_trades",
        "net_pnl_usd",
        "lockbox_net_pnl_usd",
        "net_pnl_2x_cost_usd",
        "net_pnl_3x_cost_usd",
        "daily_sharpe",
        "daily_sortino",
        "max_drawdown_usd",
        "win_rate",
        "profit_factor",
        "expectancy_usd",
        "profitable_fold_share",
        "deflated_sharpe_probability",
        "long_net_pnl_usd",
        "short_net_pnl_usd",
    ]
    available_model_columns = [
        column for column in model_columns if column in selected_scorecard.columns
    ]
    selected = selected_scorecard.select(available_model_columns)
    rename = {
        "strategy_id": "selected_strategy_id",
        "strategy_name": "selected_strategy_name",
        "status": "selected_strategy_status",
        **{
            column: f"model_{column}"
            for column in available_model_columns
            if column
            not in {"spread_id", "strategy_id", "strategy_name", "status"}
        },
    }
    selected = selected.rename(rename) if not selected.is_empty() else selected
    recent = selected_strategy_trades(
        trades,
        selected_scorecard,
        start_session=lockbox_start,
        end_session=lockbox_end,
    )
    recent_metrics = _recent_structure_metrics(recent)
    summary = current
    if not selected.is_empty():
        summary = summary.join(selected, on="spread_id", how="left")
    if not recent_metrics.is_empty():
        summary = summary.join(recent_metrics, on="spread_id", how="left")
    numeric_zero_columns = [
        "recent_30_trades",
        "recent_30_gross_pnl_usd",
        "recent_30_cost_usd",
        "recent_30_net_pnl_usd",
        "recent_30_win_rate",
        "recent_30_expectancy_usd",
        "recent_30_median_holding_bars",
        "recent_30_max_drawdown_usd",
        "recent_30_long_net_pnl_usd",
        "recent_30_short_net_pnl_usd",
        "recent_30_profit_factor",
    ]
    expressions = [
        pl.col(column).fill_null(0).alias(column)
        for column in numeric_zero_columns
        if column in summary.columns
    ]
    if expressions:
        summary = summary.with_columns(expressions)
    for column, dtype in (
        ("recent_30_trades", pl.Int64),
        ("recent_30_net_pnl_usd", pl.Float64),
        ("recent_30_win_rate", pl.Float64),
        ("recent_30_profit_factor", pl.Float64),
        ("recent_30_max_drawdown_usd", pl.Float64),
    ):
        if column not in summary.columns:
            summary = summary.with_columns(pl.lit(0, dtype=dtype).alias(column))
    summary = summary.with_columns(
        (pl.col("current_level") - pl.col("buy_entry_ceiling")).alias(
            "distance_above_buy_entry"
        ),
        (pl.col("sell_entry_floor") - pl.col("current_level")).alias(
            "distance_below_sell_entry"
        ),
        (pl.col("fair_value_target") - pl.col("current_level")).alias(
            "distance_to_fair_value"
        ),
        pl.when(pl.col("current_status") == "BUY")
        .then(pl.lit("LONG"))
        .when(pl.col("current_status") == "SELL")
        .then(pl.lit("SHORT"))
        .when(pl.col("vote_balance") > 0)
        .then(pl.lit("LONG_BIAS"))
        .when(pl.col("vote_balance") < 0)
        .then(pl.lit("SHORT_BIAS"))
        .otherwise(pl.lit("NEUTRAL"))
        .alias("technical_bias"),
        pl.when(pl.col("current_level") <= pl.col("buy_entry_ceiling"))
        .then(pl.lit("AT_OR_BELOW_BUY_LEVEL"))
        .when(pl.col("current_level") >= pl.col("sell_entry_floor"))
        .then(pl.lit("AT_OR_ABOVE_SELL_LEVEL"))
        .otherwise(pl.lit("BETWEEN_ENTRY_LEVELS"))
        .alias("level_position"),
        pl.lit(lockbox_start).cast(pl.Date).alias("recent_30_start"),
        pl.lit(lockbox_end).cast(pl.Date).alias("recent_30_end"),
    )
    rows = summary.to_dicts()
    descriptions = [
        _description(row, demo_mode=demo_mode) for row in rows
    ]
    summary = summary.with_columns(
        pl.Series("summary_description", descriptions, dtype=pl.Utf8)
    ).sort(
        ["current_status", "confidence", "spread_id"],
        descending=[False, True, False],
    )
    return summary, recent


def build_model_summary(
    structure_summaries: pl.DataFrame,
    recent_selected_trades: pl.DataFrame,
    selected_scorecard: pl.DataFrame,
    artifact: Mapping[str, Any],
    *,
    demo_mode: bool,
    portfolio_lockbox_trades: pl.DataFrame | None = None,
) -> dict[str, Any]:
    """Build a compact overall model/OOS/latest-30-session result receipt."""

    status_counts = (
        {
            str(row["current_status"]): int(row["count"])
            for row in structure_summaries.group_by("current_status")
            .agg(pl.len().alias("count"))
            .to_dicts()
        }
        if not structure_summaries.is_empty()
        else {}
    )
    strategy_counts = (
        selected_scorecard.group_by("strategy_id", "strategy_name")
        .agg(
            pl.len().alias("selected_structures"),
            (pl.col("status") == "VALIDATED").sum().alias(
                "validated_structures"
            ),
            pl.col("oos_trades").sum().alias("oos_trades"),
            pl.col("net_pnl_usd").sum().alias("oos_net_pnl_usd"),
            pl.col("lockbox_trades").sum().alias("recent_30_trades"),
            pl.col("lockbox_net_pnl_usd").sum().alias(
                "recent_30_net_pnl_usd"
            ),
        )
        .sort("selected_structures", descending=True)
        .to_dicts()
        if not selected_scorecard.is_empty()
        else []
    )
    selected_oos = {
        "trades": int(selected_scorecard["oos_trades"].sum() or 0)
        if not selected_scorecard.is_empty()
        else 0,
        "net_pnl_usd": float(selected_scorecard["net_pnl_usd"].sum() or 0.0)
        if not selected_scorecard.is_empty()
        else 0.0,
        "net_pnl_2x_cost_usd": float(
            selected_scorecard["net_pnl_2x_cost_usd"].sum() or 0.0
        )
        if not selected_scorecard.is_empty()
        else 0.0,
        "net_pnl_3x_cost_usd": float(
            selected_scorecard["net_pnl_3x_cost_usd"].sum() or 0.0
        )
        if not selected_scorecard.is_empty()
        else 0.0,
    }
    recent_metrics: dict[str, Any] = {
        "trades": 0,
        "gross_pnl_usd": 0.0,
        "cost_usd": 0.0,
        "net_pnl_usd": 0.0,
        "win_rate": 0.0,
        "max_drawdown_usd": 0.0,
        "long_net_pnl_usd": 0.0,
        "short_net_pnl_usd": 0.0,
    }
    if not recent_selected_trades.is_empty():
        daily = (
            recent_selected_trades.group_by("exit_session")
            .agg(pl.col("net_pnl_usd").sum().alias("daily_net_pnl_usd"))
            .sort("exit_session")
            .with_columns(
                pl.col("daily_net_pnl_usd").cum_sum().alias("_equity")
            )
            .with_columns(
                pl.max_horizontal(
                    pl.col("_equity").cum_max(), pl.lit(0.0)
                ).alias("_peak")
            )
            .with_columns((pl.col("_equity") - pl.col("_peak")).alias("_drawdown"))
        )
        recent_metrics = {
            "trades": recent_selected_trades.height,
            "gross_pnl_usd": float(
                recent_selected_trades["gross_pnl_usd"].sum() or 0.0
            ),
            "cost_usd": float(recent_selected_trades["cost_usd"].sum() or 0.0),
            "net_pnl_usd": float(
                recent_selected_trades["net_pnl_usd"].sum() or 0.0
            ),
            "win_rate": float(
                recent_selected_trades.select(
                    (pl.col("net_pnl_usd") > 0).mean()
                ).item()
                or 0.0
            ),
            "max_drawdown_usd": float(
                max(0.0, -(daily["_drawdown"].min() or 0.0))
            ),
            "long_net_pnl_usd": float(
                recent_selected_trades.filter(pl.col("direction") == "LONG")[
                    "net_pnl_usd"
                ].sum()
                or 0.0
            ),
            "short_net_pnl_usd": float(
                recent_selected_trades.filter(pl.col("direction") == "SHORT")[
                    "net_pnl_usd"
                ].sum()
                or 0.0
            ),
        }
    if not structure_summaries.is_empty():
        modeled = structure_summaries.filter(pl.col("model_enabled"))
        recent_metrics.update(
            {
                "structures_with_trades": modeled.filter(
                    pl.col("recent_30_trades") > 0
                ).height,
                "structures_with_negative_net": modeled.filter(
                    pl.col("recent_30_net_pnl_usd") < 0
                ).height,
                "structures_with_no_trades": modeled.filter(
                    pl.col("recent_30_trades") == 0
                ).height,
                "worst_structure_max_drawdown_usd": float(
                    modeled["recent_30_max_drawdown_usd"].max() or 0.0
                ),
            }
        )
    portfolio_metrics: dict[str, Any] = {
        "candidate_trades": 0,
        "selected_trades": 0,
        "sessions_traded": 0,
        "maximum_new_trades_in_session": 0,
        "net_pnl_usd": 0.0,
        "win_rate": 0.0,
    }
    if portfolio_lockbox_trades is not None and not portfolio_lockbox_trades.is_empty():
        selected_portfolio = portfolio_lockbox_trades.filter(
            pl.col("portfolio_selected")
        )
        maximum_daily = (
            int(
                selected_portfolio.group_by("entry_session")
                .len()["len"]
                .max()
                or 0
            )
            if not selected_portfolio.is_empty()
            else 0
        )
        portfolio_metrics = {
            "candidate_trades": portfolio_lockbox_trades.height,
            "selected_trades": selected_portfolio.height,
            "sessions_traded": (
                selected_portfolio["entry_session"].n_unique()
                if not selected_portfolio.is_empty()
                else 0
            ),
            "maximum_new_trades_in_session": maximum_daily,
            "net_pnl_usd": float(
                selected_portfolio["net_pnl_usd"].sum() or 0.0
            ),
            "win_rate": float(
                selected_portfolio.select(
                    (pl.col("net_pnl_usd") > 0).mean()
                ).item()
                or 0.0
            )
            if not selected_portfolio.is_empty()
            else 0.0,
            "lockbox_used_for_selection": False,
        }
    actionable = status_counts.get("BUY", 0) + status_counts.get("SELL", 0)
    watch = status_counts.get("WATCH", 0)
    training_window = dict(artifact.get("training_window") or {})
    description = (
        f"Model {artifact.get('model_id', 'unknown')} evaluated "
        f"{artifact.get('total_trial_count', 0):,} preregistered trials and selected "
        f"{artifact.get('selected_strategy_count', 0):,} structure strategies. "
        f"Selected OOS ledger: {selected_oos['trades']:,} trades, net "
        f"{_money(selected_oos['net_pnl_usd'])}, 2x-cost net "
        f"{_money(selected_oos['net_pnl_2x_cost_usd'])}. "
        f"Current board: {actionable} actionable and {watch} watch. Latest 30 sessions "
        f"({training_window.get('lockbox_start', 'n/a')} to "
        f"{training_window.get('lockbox_end', 'n/a')}): "
        f"{recent_metrics['trades']:,} selected-strategy trades, net "
        f"{_money(recent_metrics['net_pnl_usd'])}, win rate "
        f"{_percent(recent_metrics['win_rate'])}, max drawdown "
        f"{_money(recent_metrics['max_drawdown_usd'])}; worst single-structure "
        f"drawdown {_money(recent_metrics.get('worst_structure_max_drawdown_usd'))}."
        f" Portfolio budget selected {portfolio_metrics['selected_trades']:,} of "
        f"{portfolio_metrics['candidate_trades']:,} lockbox candidates, with at "
        f"most {portfolio_metrics['maximum_new_trades_in_session']} new trades in "
        f"one session; capped net was {_money(portfolio_metrics['net_pnl_usd'])}."
    )
    if demo_mode:
        description = "Synthetic demo; no trade recommendation. " + description
    model_age_sessions = int(artifact.get("model_age_sessions") or 0)
    maximum_model_age_sessions = int(
        artifact.get("maximum_model_age_sessions") or 0
    )
    if maximum_model_age_sessions:
        description += (
            f" Frozen model age is {model_age_sessions} completed sessions "
            f"(maximum {maximum_model_age_sessions})."
        )
    top_rows = (
        structure_summaries.filter(
            pl.col("current_status").is_in(["BUY", "SELL", "WATCH"])
        )
        .sort("confidence", descending=True)
        .head(10)
        .select(
            "spread_id",
            "trade_code",
            "current_status",
            "portfolio_action",
            "display_unit",
            "display_current",
            "display_buy_entry",
            "display_sell_entry",
            "display_fair_value",
            "confidence",
            "selected_strategy_name",
            "recent_30_trades",
            "recent_30_net_pnl_usd",
        )
        .to_dicts()
        if not structure_summaries.is_empty()
        else []
    )
    return {
        "model_id": artifact.get("model_id"),
        "mode": artifact.get("mode"),
        "as_of": artifact.get("as_of"),
        "training_window": training_window,
        "total_trial_count": int(artifact.get("total_trial_count") or 0),
        "selected_strategy_count": int(
            artifact.get("selected_strategy_count") or 0
        ),
        "validated_strategy_count": int(
            artifact.get("validated_strategy_count") or 0
        ),
        "approved_for_live_signals": bool(
            artifact.get("approved_for_live_signals", False)
        ),
        "model_age_sessions": model_age_sessions,
        "maximum_model_age_sessions": maximum_model_age_sessions,
        "model_stale": bool(artifact.get("model_stale", False)),
        "current_status_counts": status_counts,
        "selected_oos": selected_oos,
        "latest_30_sessions": recent_metrics,
        "portfolio_latest_30_sessions": portfolio_metrics,
        "selected_strategy_distribution": strategy_counts,
        "top_current_rows": top_rows,
        "description": description,
        "caveats": [
            "Latest-30-session results are the frozen lockbox and never update model selection.",
            "Aggregated selected-strategy P&L is an audit ledger, not a capital-weighted portfolio return.",
            "Targets remain conditional on evidence, liquidity, expiry, costs, and data-quality gates.",
            "Technicals are decision support and should not replace position sizing, market fundamentals, or risk limits.",
        ],
    }


__all__ = [
    "build_model_summary",
    "build_structure_summaries",
    "selected_strategy_trades",
]
