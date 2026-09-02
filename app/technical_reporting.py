"""Durable CSV/Parquet outputs and a self-contained decision dashboard."""

from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime, timezone
import html
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import polars as pl

from app.technical_backtest import (
    STRATEGIES,
    BacktestResult,
    strategy_library_frame,
    walk_forward_window_frame,
)
from app.technical_config import TechnicalConfig


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
    return value


def _records(frame: pl.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    if frame.is_empty():
        return []
    selected = frame.head(limit) if limit is not None else frame
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in selected.to_dicts()
    ]


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    os.close(descriptor)
    temp = Path(temp_name)
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _write_csv(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if frame.is_empty():
        _atomic_text(path, "")
        return
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    os.close(descriptor)
    temp = Path(temp_name)
    try:
        frame.write_csv(temp, datetime_format="%Y-%m-%dT%H:%M:%S%.3f%z")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def technical_trade_level_export(
    signals: pl.DataFrame, config: TechnicalConfig
) -> pl.DataFrame:
    """Return the compact broker-facing current-level CSV surface."""

    if signals.is_empty():
        return pl.DataFrame()
    required = {
        "session_date",
        "spread_id",
        "trade_code",
        "trade_code_short",
        "contract_codes",
        "status",
        "portfolio_action",
        "portfolio_selected",
        "display_unit",
        "display_current",
        "display_buy_entry",
        "display_sell_entry",
        "display_fair_value",
    }
    missing = sorted(required - set(signals.columns))
    if missing:
        raise ValueError(
            "Trade-level export is missing current signal fields: "
            + ", ".join(missing)
        )
    preferred = [
        "session_date",
        "as_of_utc",
        "trade_code",
        "trade_code_short",
        "contract_codes",
        "contract_months",
        "spread_id",
        "spread_name",
        "family",
        "structure_roots",
        "quote_convention",
        "calculation_unit",
        "status",
        "portfolio_action",
        "portfolio_selected",
        "portfolio_rank",
        "portfolio_candidate_rank",
        "daily_trade_limit",
        "daily_trade_slots_remaining",
        "signal_strategy_id",
        "direction_evidence_validated",
        "display_unit",
        "display_current",
        "display_buy_entry",
        "display_sell_entry",
        "display_fair_value",
        "display_long_stop",
        "display_short_stop",
        "current_spread",
        "buy_entry_ceiling",
        "sell_entry_floor",
        "fair_value_target",
        "confidence",
        "pattern_state",
        "pattern_strength",
        "pattern_agreement",
        "pattern_components",
        "advanced_risk_regime",
        "regime",
        "expected_edge_usd",
        "round_trip_cost_usd",
        "expected_edge_to_cost",
        "relative_volume",
        "liquidity_gate",
        "package_volume_capacity",
        "max_leg_bid_ask_ticks",
        "heating_oil_cpg",
        "gasoil_usd_mt",
        "gasoil_usd_bbl",
        "gasoil_cpg",
        "hogo_cpg",
        "conversion_method",
        "earliest_risk_date",
        "mandatory_last_exit_session",
        "sessions_to_risk_date",
        "oos_grade",
        "demo_mode",
    ]
    available = [column for column in preferred if column in signals.columns]
    return (
        signals.select(available)
        .with_columns(
            pl.lit(config.system.bar_interval_minutes).alias(
                "bar_interval_minutes"
            ),
            pl.lit(len(STRATEGIES)).alias("strategy_count"),
            pl.lit(config.backtest.lockbox_sessions).alias(
                "untouched_lockbox_sessions"
            ),
            pl.lit(config.system.rolling_intraday_months).alias(
                "rolling_intraday_months"
            ),
            pl.lit(config.backtest.train_sessions).alias(
                "walk_forward_train_sessions"
            ),
            pl.lit(config.backtest.validation_sessions).alias(
                "walk_forward_validation_sessions"
            ),
            pl.lit(config.backtest.test_sessions).alias(
                "walk_forward_test_sessions"
            ),
            pl.lit(config.backtest.embargo_sessions).alias(
                "walk_forward_embargo_sessions"
            ),
            pl.lit(False).alias("lockbox_used_for_training"),
            pl.lit(config.backtest.maximum_new_trades_per_session).alias(
                "maximum_new_trades_per_session"
            ),
        )
        .sort(
            ["portfolio_selected", "status", "confidence", "spread_id"],
            descending=[True, False, True, False],
        )
    )


def portfolio_lockbox_trade_budget(
    trades: pl.DataFrame,
    structure_summaries: pl.DataFrame,
    config: TechnicalConfig,
) -> pl.DataFrame:
    """Apply the three-entry budget to frozen selected-strategy lockbox trades."""

    if trades.is_empty() or structure_summaries.is_empty():
        return pl.DataFrame()
    evidence_columns = [
        "spread_id",
        "selected_strategy_id",
        "selected_strategy_status",
        "model_deflated_sharpe_probability",
        "model_profitable_fold_share",
        "model_daily_sharpe",
        "model_net_pnl_usd",
    ]
    if not set(evidence_columns).issubset(structure_summaries.columns):
        return pl.DataFrame()
    selected = structure_summaries.select(evidence_columns).rename(
        {"selected_strategy_id": "strategy_id"}
    )
    candidates = trades.join(
        selected,
        on=["spread_id", "strategy_id"],
        how="inner",
        validate="m:1",
    )
    if "phase" in candidates.columns:
        candidates = candidates.filter(pl.col("phase") == "LOCKBOX")
    if candidates.is_empty():
        return candidates
    rows = candidates.to_dicts()
    by_session: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        by_session.setdefault(row["entry_session"], []).append(row)
    output: list[dict[str, Any]] = []
    limit = int(config.backtest.maximum_new_trades_per_session)
    for session in sorted(by_session):
        group = sorted(
            by_session[session],
            key=lambda row: (
                str(row.get("selected_strategy_status")) != "VALIDATED",
                -float(row.get("model_deflated_sharpe_probability") or 0.0),
                -float(row.get("model_profitable_fold_share") or 0.0),
                -float(row.get("model_daily_sharpe") or 0.0),
                -float(row.get("entry_relative_volume") or 0.0),
                int(row.get("complexity_tier") or 1),
                row.get("entry_time"),
                str(row.get("spread_id")),
            ),
        )
        selected_count = 0
        selected_groups: set[str] = set()
        for rank, row in enumerate(group, start=1):
            algebra_group = str(
                row.get("algebra_group") or row.get("spread_id")
            )
            action = "SELECTED"
            if (
                config.backtest.one_trade_per_algebra_group
                and algebra_group in selected_groups
            ):
                action = "DUPLICATE_ALGEBRA_GROUP"
            elif selected_count >= limit:
                action = "DAILY_TRADE_BUDGET"
            else:
                selected_count += 1
                selected_groups.add(algebra_group)
            output.append(
                {
                    **row,
                    "portfolio_candidate_rank": rank,
                    "portfolio_selected": action == "SELECTED",
                    "portfolio_action": action,
                    "daily_trade_limit": limit,
                    "lockbox_used_for_selection": False,
                }
            )
    return pl.DataFrame(output, infer_schema_length=None).sort(
        ["entry_session", "portfolio_candidate_rank", "spread_id"]
    )


def _write_parquet(frame: pl.DataFrame, path: Path) -> None:
    if frame.is_empty():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    os.close(descriptor)
    temp = Path(temp_name)
    try:
        frame.write_parquet(temp, compression="zstd", statistics=True)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def expiry_calendar(contracts: pl.DataFrame, as_of: date) -> pl.DataFrame:
    if contracts.is_empty():
        return pl.DataFrame()
    return (
        contracts.filter(
            (pl.col("risk_date") >= as_of)
            & (pl.col("risk_date") <= as_of.replace(year=as_of.year + 1))
        )
        .select(
            "root",
            "security",
            "delivery_month",
            "last_trade_date",
            "first_notice_date",
            "first_delivery_date",
            "risk_date",
            "forced_exit_session",
            "blackout_start",
            "expiry_verified",
            "expiry_source",
        )
        .sort(["risk_date", "root", "delivery_month"])
    )


def spread_library_expanded(config: TechnicalConfig) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for spread in config.spreads:
        executable = " ".join(
            f"{'+' if leg.sign > 0 else '-'}{leg.contracts} {leg.root}[M{leg.delivery_offset:+d}]"
            for leg in spread.legs
        ).lstrip("+")
        formula = " ".join(
            f"{'+' if leg.sign > 0 else '-'}{leg.price_weight:g} {leg.root}[M{leg.delivery_offset:+d}]"
            for leg in spread.legs
        ).lstrip("+")
        rows.append(
            {
                "spread_id": spread.spread_id,
                "display_name": spread.display_name,
                "family": spread.family,
                "anchor_root": spread.anchor_root,
                "package_rank": spread.anchor_rank,
                "unit": spread.unit,
                "core": spread.core,
                "model_enabled": spread.model_enabled,
                "complexity_tier": spread.complexity_tier,
                "algebra_group": spread.algebra_group,
                "normalized_price_formula": formula,
                "executable_package": executable,
                "notes": spread.notes,
            }
        )
    return pl.DataFrame(rows)


def spread_leg_library(config: TechnicalConfig) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for spread in config.spreads:
        for leg in spread.legs:
            root = config.roots[leg.root]
            rows.append(
                {
                    "spread_id": spread.spread_id,
                    "spread_name": spread.display_name,
                    "family": spread.family,
                    "model_enabled": spread.model_enabled,
                    "complexity_tier": spread.complexity_tier,
                    "leg_order": leg.leg_order,
                    "root": leg.root,
                    "selection_mode": leg.selection_mode,
                    "delivery_offset": leg.delivery_offset,
                    "rank": leg.rank,
                    "sign": leg.sign,
                    "contracts": leg.contracts,
                    "price_weight": leg.price_weight,
                    "native_unit": root.native_unit,
                    "contract_size_native": root.contract_size_native,
                    "contract_barrels": root.contract_barrels,
                    "tick_size_native": root.tick_size_native,
                    "description": leg.description,
                }
            )
    return pl.DataFrame(rows)


def current_indicator_snapshot(features: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty():
        return pl.DataFrame()
    columns = [
        "timestamp_utc",
        "session_date",
        "spread_id",
        "spread_name",
        "spread_family",
        "model_enabled",
        "complexity_tier",
        "roll_id",
        "spread_close",
        "research_close",
        "rolling_median",
        "rolling_mad",
        "robust_z",
        "rsi",
        "ema_fast",
        "ema_slow",
        "macd_line",
        "macd_signal",
        "macd_histogram",
        "bollinger_mid",
        "bollinger_upper",
        "bollinger_lower",
        "bollinger_pct_b",
        "bollinger_width",
        "volatility_squeeze",
        "efficiency_ratio",
        "regime",
        "variance_ratio_5",
        "variance_ratio_13",
        "hurst_exponent_proxy",
        "permutation_entropy_3",
        "zero_crossing_rate",
        "half_life_stability",
        "mean_reversion_stability",
        "cusum_change_score",
        "change_point_alarm",
        "jump_share",
        "semivariance_asymmetry",
        "leg_return_correlation",
        "lead_lag_score",
        "ar1_beta",
        "ou_half_life_bars",
        "realized_vol_5d",
        "ewma_abs_change",
        "upside_semivol",
        "downside_semivol",
        "jump_score",
        "volatility_of_volatility",
        "relative_volume",
        "pvo",
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
        "obv_proxy",
        "signed_volume_imbalance_proxy",
        "amihud_impact_proxy",
        "effort_vs_result",
        "oi_migration_1d",
        "session_vwap_proxy",
        "seasonal_median",
        "seasonal_q25",
        "seasonal_q75",
        "seasonal_n",
        "seasonal_z",
        "seasonal_prior_years",
        "seasonal_expected_move_1d",
        "seasonal_fair_value_1d",
        "seasonal_move_z",
        "seasonal_support",
        "seasonal_confidence",
        "seasonal_status",
        "settlement_quality",
        "adaptive_score",
        "adaptive_vote",
        "adaptive_confidence",
        "adaptive_observations",
        "adaptive_top_expert",
        "adaptive_top_weight",
        "adaptive_status",
        "kronos_expected_move_1b",
        "kronos_vote",
        "kronos_contract_coverage",
        "kronos_status",
        "kronos_action_eligible",
        "package_volume_capacity",
        "package_oi_capacity",
        "paired_volume_bbl",
        "paired_open_interest_bbl",
        "min_leg_events",
        "average_trade_size_bbl_proxy",
        "max_leg_bid_ask_ticks",
        "quoted_width_usd_bbl",
        "earliest_risk_date",
        "forced_exit_session",
        "sessions_to_risk_date",
        "entry_allowed",
    ]
    available = [column for column in columns if column in features.columns]
    return (
        features.sort("timestamp_utc")
        .group_by("spread_id", maintain_order=True)
        .tail(1)
        .select(available)
        .sort("spread_id")
    )


def daily_spread_history(features: pl.DataFrame, sessions: int = 120) -> pl.DataFrame:
    if features.is_empty():
        return pl.DataFrame()
    session_values = sorted(features["session_date"].unique().to_list())
    cutoff = session_values[-sessions] if len(session_values) >= sessions else session_values[0]
    last_bar = (
        pl.col("bar_slot") == pl.col("session_last_slot")
        if "session_last_slot" in features.columns
        else pl.col("bar_slot") == pl.col("bar_slot").max().over("session_date")
    )
    return (
        features.filter((pl.col("session_date") >= cutoff) & last_bar)
        .select(
            "session_date",
            "spread_id",
            "spread_name",
            "spread_close",
            "rolling_median",
            "robust_z",
            "relative_volume",
            "seasonal_median",
            "seasonal_z",
            "seasonal_expected_move_1d",
            "seasonal_fair_value_1d",
            "seasonal_confidence",
            "mean_reversion_stability",
            "change_point_alarm",
            "adaptive_score",
            "adaptive_vote",
            "regime",
            "roll_id",
        )
        .sort(["spread_id", "session_date"])
    )


def parameter_catalog(config: TechnicalConfig) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for section, settings in (
        ("system", config.system),
        ("bloomberg", config.bloomberg),
        ("liquidity", config.liquidity),
        ("backtest", config.backtest),
        ("indicators", config.indicators),
    ):
        for field in fields(settings):
            value = getattr(settings, field.name)
            if isinstance(value, (tuple, list)):
                value = "; ".join(str(item) for item in value)
            rows.append(
                {
                    "section": section,
                    "parameter": field.name,
                    "value": str(value),
                    "source": "config/technical_system.toml",
                }
            )
    for root_code, root in config.roots.items():
        for field in fields(root):
            if field.name in {"root", "name", "ticker_template", "generic_template"}:
                continue
            rows.append(
                {
                    "section": f"root.{root_code}",
                    "parameter": field.name,
                    "value": str(getattr(root, field.name)),
                    "source": "config/technical_system.toml",
                }
            )
    return pl.DataFrame(rows)


def adaptive_weight_history(features: pl.DataFrame) -> pl.DataFrame:
    columns = [
        "session_date",
        "spread_id",
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
        "adaptive_weight_robust_mean_reversion",
        "adaptive_weight_trend_breakout",
        "adaptive_weight_volatility_squeeze",
        "adaptive_weight_session_vwap_reversion",
        "adaptive_weight_error_correction_residual",
        "adaptive_weight_stability_reversion",
        "adaptive_weight_flow_divergence",
    ]
    if features.is_empty() or not set(columns).issubset(features.columns):
        return pl.DataFrame()
    return (
        features.sort("timestamp_utc")
        .group_by(["session_date", "spread_id"], maintain_order=True)
        .tail(1)
        .select(columns)
        .sort(["session_date", "spread_id"])
    )


def structure_coverage(
    config: TechnicalConfig, features: pl.DataFrame
) -> pl.DataFrame:
    observed: dict[str, dict[str, Any]] = {}
    if not features.is_empty():
        for row in (
            features.group_by("spread_id")
            .agg(
                pl.len().alias("feature_rows"),
                pl.col("session_date").n_unique().alias("sessions"),
                pl.col("session_date").min().alias("coverage_start"),
                pl.col("session_date").max().alias("coverage_end"),
                pl.col("entry_allowed").mean().alias("entry_allowed_share"),
                pl.col("package_volume_capacity").median().alias(
                    "median_package_capacity"
                ),
            )
            .to_dicts()
        ):
            observed[str(row["spread_id"])] = row
    rows: list[dict[str, Any]] = []
    for spread in config.spreads:
        found = observed.get(spread.spread_id)
        rows.append(
            {
                "spread_id": spread.spread_id,
                "spread_name": spread.display_name,
                "family": spread.family,
                "tenor_start": spread.anchor_rank,
                "tenor_end": spread.anchor_rank
                + max(leg.delivery_offset for leg in spread.legs),
                "model_enabled": spread.model_enabled,
                "status": "OBSERVED" if found else "INCOMPLETE_CURVE",
                "feature_rows": found.get("feature_rows", 0) if found else 0,
                "sessions": found.get("sessions", 0) if found else 0,
                "coverage_start": found.get("coverage_start") if found else None,
                "coverage_end": found.get("coverage_end") if found else None,
                "entry_allowed_share": found.get("entry_allowed_share") if found else None,
                "median_package_capacity": (
                    found.get("median_package_capacity") if found else None
                ),
                "missing_reason": (
                    ""
                    if found
                    else "One or more exact delivery-month legs were unavailable or unsynchronized."
                ),
            }
        )
    return pl.DataFrame(rows).sort(["status", "family", "tenor_start", "spread_id"])


def _history_payload(features: pl.DataFrame, sessions: int) -> list[dict[str, Any]]:
    if features.is_empty():
        return []
    session_values = sorted(features["session_date"].unique().to_list())
    cutoff = session_values[-sessions] if len(session_values) >= sessions else session_values[0]
    columns = [
        "timestamp_utc",
        "session_date",
        "spread_id",
        "spread_close",
        "rolling_median",
        "bollinger_upper",
        "bollinger_lower",
        "relative_volume",
        "robust_z",
    ]
    recent = features.filter(pl.col("session_date") >= cutoff).select(columns)
    latest_session = recent["session_date"].max()
    prior_closes = (
        recent.filter(pl.col("session_date") < latest_session)
        .sort(["spread_id", "timestamp_utc"])
        .group_by(["spread_id", "session_date"], maintain_order=True)
        .tail(1)
    )
    latest_intraday = recent.filter(pl.col("session_date") == latest_session)
    return _records(
        pl.concat([prior_closes, latest_intraday], how="diagonal_relaxed").sort(
            ["spread_id", "timestamp_utc"]
        )
    )


def _dashboard_html(payload: Mapping[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    title = html.escape(str(payload.get("project_name") or "Technical Trading System"))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--ink:#102235;--muted:#607185;--panel:#fff;--line:#dbe3ea;--bg:#eef3f6;--navy:#0c2a43;--blue:#1565c0;--orange:#ef6c35;--green:#147d64;--red:#b93632;--amber:#a26700}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 Inter,Segoe UI,Arial,sans-serif}}
header{{background:linear-gradient(120deg,#09263d,#174d72);color:#fff;padding:24px 28px 20px;box-shadow:0 2px 12px #16324733}}
.eyebrow{{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#add1e8;font-weight:700}} h1{{font-size:26px;margin:5px 0 4px}} .subtitle{{color:#d4e5ef;max-width:1000px}}
.banner{{padding:11px 28px;font-weight:700;background:#ffe8b8;color:#633e00;border-bottom:1px solid #eccf90}} .banner.live{{background:#d8f2e8;color:#075b45}}
nav{{display:flex;gap:8px;padding:14px 28px 0;flex-wrap:wrap}} nav button{{border:1px solid #bfd0dc;background:#fff;color:#24435b;padding:9px 14px;border-radius:7px;cursor:pointer;font-weight:650}} nav button.active{{background:var(--navy);border-color:var(--navy);color:#fff}}
main{{padding:18px 28px 34px;max-width:1700px;margin:auto}} section{{display:none}} section.active{{display:block}}
.grid{{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}} .panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:0 3px 12px #17334b0a;padding:16px;overflow:hidden}} .span12{{grid-column:span 12}} .span8{{grid-column:span 8}} .span6{{grid-column:span 6}} .span4{{grid-column:span 4}} .span3{{grid-column:span 3}}
.kpis{{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:12px;margin-bottom:14px}} .kpi{{background:#fff;border:1px solid var(--line);border-top:3px solid var(--blue);border-radius:9px;padding:13px}} .kpi .label{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}} .kpi .value{{font-size:22px;font-weight:750;margin-top:3px}}
h2{{font-size:17px;margin:0 0 10px}} h3{{font-size:14px;margin:0 0 8px}} .note{{color:var(--muted);font-size:12px}}
.summary-copy{{margin:0;color:#31485b;line-height:1.6}} .brief-metrics{{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:10px;margin:12px 0}} .brief-metric{{border:1px solid var(--line);border-radius:8px;padding:11px;background:#f8fbfd}} .brief-metric .label{{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}} .brief-metric .value{{font-size:18px;font-weight:750;margin-top:2px}} .brief-evidence{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}} .summary-callout{{border-left:4px solid var(--blue);padding:12px 14px;background:#f4f8fb;border-radius:6px}}
.tablewrap{{overflow:auto;max-height:640px;border:1px solid var(--line);border-radius:7px}} table{{border-collapse:collapse;width:100%;background:#fff;white-space:nowrap}} th{{position:sticky;top:0;background:#eaf0f4;color:#304b60;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;z-index:1}} th,td{{padding:9px 10px;border-bottom:1px solid #e7edf1}} tr:hover td{{background:#f8fbfd}} td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.pill{{display:inline-block;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:800}} .BUY,.PASS,.VALIDATED{{background:#d8f3e9;color:#08624b}} .SELL,.FAIL,.DATA_BLOCK,.MODEL_STALE{{background:#f9d9d7;color:#8e211d}} .WATCH,.WARN,.EXPIRY_BLOCK{{background:#ffedc8;color:#805000}} .FLAT,.NO_TRADE,.RESEARCH_ONLY,.DEMO_ONLY,.NO_TRADES,.ANALYTIC_ONLY{{background:#e8edf1;color:#475967}}
.controls{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}} select{{padding:8px;border:1px solid #bdccd7;border-radius:6px;background:#fff}} canvas{{width:100%;height:360px;display:block;background:#fff;border-radius:7px}}
.legend{{display:flex;gap:14px;font-size:12px;color:var(--muted);margin-top:7px}} .dot{{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:5px}} .mono{{font-family:Consolas,monospace;font-size:12px}}
@media(max-width:1000px){{.span8,.span6,.span4,.span3{{grid-column:span 12}}.kpis,.brief-metrics{{grid-template-columns:repeat(2,1fr)}}.brief-evidence{{grid-template-columns:1fr}}main,header{{padding-left:16px;padding-right:16px}}nav{{padding-left:16px}}}}
</style>
</head>
<body>
<header><div class="eyebrow">Bloomberg • XBBG • Polars • adaptive 15-minute research</div><h1>{title}</h1><div class="subtitle">An 18-month, 15-minute executable curve with 2022+ settle seasonality, fixed-share expert learning, a three-entry daily portfolio budget, next-bar execution, and mandatory D-4 liquidation so every package is flat before D-3. Session: 08:00–14:30 America/New_York.</div></header>
<div id="modeBanner" class="banner"></div>
<nav><button data-tab="signals" class="active">Live decision board</button><button data-tab="brief">Trade brief</button><button data-tab="model">Model results</button><button data-tab="chart">Spread diagnostics</button><button data-tab="backtests">Strategy backtests</button><button data-tab="quality">Data & expiry controls</button><button data-tab="methods">Methodology</button></nav>
<main>
<section id="signals" class="active"><div id="signalKpis" class="kpis"></div><div class="grid"><div class="panel span12"><h2>Current targets, dynamic patterns, regime, and liquidity</h2><p class="note">The pattern state transparently summarizes the frozen, cost-aware combination of seven interpretable indicator experts; it does not create a separate trade rule. Targets are decision levels, not promises. Production entries still require expiry, capacity, width, depth, evidence, and freshness gates.</p><div id="signalTable" class="tablewrap"></div></div></div></section>
<section id="brief"><div class="grid"><div class="panel span12"><div class="controls"><h2 style="margin-right:auto">Current technical trade brief</h2><label>Structure <select id="briefSelect"></select></label></div><div id="briefBody"></div></div></div></section>
<section id="model"><div id="modelKpis" class="kpis"></div><div class="grid"><div class="panel span12"><h2>Frozen model and latest 30 sessions</h2><div id="modelDescription" class="summary-callout"></div><p class="note">Latest-30-session results are the untouched lockbox. They describe model evidence and never retrain or reselect a strategy.</p></div><div class="panel span12"><h2>Selected strategy by structure</h2><div id="structureSummaryTable" class="tablewrap"></div></div><div class="panel span12"><h2>Selected strategy distribution</h2><div id="strategyDistributionTable" class="tablewrap"></div></div></div></section>
<section id="chart"><div class="grid"><div class="panel span12"><div class="controls"><h2 style="margin-right:auto">30-session closes + current intraday</h2><label>Spread <select id="spreadSelect"></select></label></div><canvas id="spreadChart"></canvas><div class="legend"><span><i class="dot" style="background:#1565c0"></i>Spread close</span><span><i class="dot" style="background:#ef6c35"></i>Rolling median</span><span><i class="dot" style="background:#91a6b6"></i>Bollinger envelope</span></div></div></div></section>
<section id="backtests"><div class="grid"><div class="panel span12"><h2>Walk-forward strategy scorecard</h2><p class="note">Daily Sharpe uses daily P&amp;L; execution is next-bar open; all-leg costs are charged. “Research only” is intentional when validation hurdles are not met.</p><div id="scoreTable" class="tablewrap"></div></div><div class="panel span12"><h2>Recent trade audit</h2><div id="tradeTable" class="tablewrap"></div></div></div></section>
<section id="quality"><div class="grid"><div class="panel span6"><h2>Data-quality gates</h2><div id="qualityTable" class="tablewrap"></div></div><div class="panel span6"><h2>Upcoming expiry controls</h2><div id="expiryTable" class="tablewrap"></div></div><div class="panel span12"><h2>Run manifest</h2><pre id="manifest" class="mono"></pre></div></div></section>
<section id="methods"><div class="grid"><div class="panel span6"><h2>Strategy registry</h2><div id="strategyTable" class="tablewrap"></div></div><div class="panel span6"><h2>Spread registry</h2><div id="spreadTable" class="tablewrap"></div></div><div class="panel span12"><h2>Guardrails</h2><ul><li>Explicit delivery months are matched before package ranks HO1/HO2 or QS1/QS2 are assigned; the curve request includes an alignment buffer beyond M16.</li><li>The earliest leg risk date controls. Existing positions exit at the final eligible D-4 bar and are flat before D-3.</li><li>The adaptive model only reweights seven fixed, interpretable experts after a delayed, cost-aware outcome. It cannot invent a rule or learn from the final 30-session lockbox.</li><li>The lockbox is evaluation-only: its trades and P&amp;L cannot change strategy status, ranking, thresholds, or expert weights.</li><li>At most three independent new entries can be selected in one session, with one per algebra group.</li><li>Seasonality is a shrunk prior-year expected move, never an optimized raw-price pattern; insufficient prior years cap or block confidence.</li><li>Flies, condors, and boxes have higher minimum-trade hurdles. Algebraically dependent identities and high-complexity diagnostics are display-only.</li><li>The multiple-testing penalty counts every independent model-enabled spread/strategy trial, including combinations that produced zero trades.</li><li>Synthetic 15-minute highs/lows are never fabricated from asynchronous leg extrema; range indicators use close-based volatility.</li><li>Every crack is quoted in USD/bbl. HO-only calendars, flies, condors, and HOGO structures are quoted in cpg.</li><li>Gasoil is converted to USD/bbl as USD/MT / 7.45 and to cpg as USD/MT / 7.45 / 0.42.</li><li>Spread capacity is the minimum executable leg volume divided by package ratio. Leg volumes are never summed into fictional spread volume.</li><li>True L2 depth requires B-PIPE entitlement. Top-of-book and bar proxies are labelled and confidence-capped.</li></ul></div></div></section>
</main>
<script>
const DATA={data};
const fmt=(v,d=2)=>v===null||v===undefined||Number.isNaN(Number(v))?'—':Number(v).toLocaleString(undefined,{{minimumFractionDigits:d,maximumFractionDigits:d}});
const money=v=>{{if(v===null||v===undefined)return '—';const n=Number(v),clean=Math.abs(n)<1e-9?0:n;return clean.toLocaleString(undefined,{{style:'currency',currency:'USD',maximumFractionDigits:0}})}};
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m]));
document.getElementById('modeBanner').textContent=DATA.demo_mode?'DEMO / PAPER DATA — connectivity and workflow validation only; not market data or a trading recommendation.':'LIVE BLOOMBERG MODE — still requires human review and execution controls.';
if(!DATA.demo_mode)document.getElementById('modeBanner').classList.add('live');
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('nav button,main section').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active');if(b.dataset.tab==='chart')drawChart();}});
function pill(v){{const cls=String(v).replaceAll(' ','_');return `<span class="pill ${{cls}}">${{esc(v)}}</span>`}}
function table(target,rows,cols){{const el=document.getElementById(target);if(!rows.length){{el.innerHTML='<p class="note" style="padding:10px">No rows.</p>';return}}el.innerHTML=`<table><thead><tr>${{cols.map(c=>`<th>${{esc(c[1])}}</th>`).join('')}}</tr></thead><tbody>${{rows.map(r=>`<tr>${{cols.map(c=>`<td class="${{c[3]||''}}">${{c[2]?c[2](r[c[0]],r):esc(r[c[0]])}}</td>`).join('')}}</tr>`).join('')}}</tbody></table>`}}
const summaries=DATA.structure_summaries||[];const modelSummary=DATA.model_summary||{{}};const pct=v=>v===null||v===undefined?'—':fmt(100*Number(v),1)+'%';
const metric=(label,value)=>`<div class="brief-metric"><div class="label">${{esc(label)}}</div><div class="value">${{esc(value)}}</div></div>`;
const briefSelect=document.getElementById('briefSelect');briefSelect.innerHTML=summaries.map(r=>`<option value="${{esc(r.spread_id)}}">${{esc(r.spread_id)}} — ${{esc(r.current_status)}}</option>`).join('');
const preferredBrief=summaries.find(r=>['BUY','SELL','WATCH'].includes(r.current_status))||summaries[0];if(preferredBrief)briefSelect.value=preferredBrief.spread_id;
function renderBrief(){{const r=summaries.find(x=>x.spread_id===briefSelect.value);const body=document.getElementById('briefBody');if(!r){{body.innerHTML='<p class="note">No summary is available.</p>';return}}body.innerHTML=`<div class="controls">${{pill(r.current_status)}}<strong>${{esc(r.spread_name)}}</strong><span class="note">${{esc(r.technical_bias)}} • ${{esc(r.level_position)}}</span></div><div class="brief-metrics">${{metric('Current level',fmt(r.current_level,3))}}${{metric('Buy entry ≤',fmt(r.buy_entry_ceiling,3))}}${{metric('Sell entry ≥',fmt(r.sell_entry_floor,3))}}${{metric('Fair-value target',fmt(r.fair_value_target,3))}}${{metric('Long stop',fmt(r.long_stop,3))}}${{metric('Short stop',fmt(r.short_stop,3))}}${{metric('Confidence',pct(r.confidence))}}${{metric('Model age',fmt(r.model_age_sessions,0)+' sessions')}}${{metric('Mandatory last exit',r.mandatory_last_exit_session||'—')}}</div><div class="summary-callout"><p class="summary-copy">${{esc(r.summary_description)}}</p></div><div class="brief-evidence"><div><h3>Frozen OOS evidence</h3><div class="brief-metrics">${{metric('Selected strategy',r.selected_strategy_name||'Analytic only')}}${{metric('Evidence',r.selected_strategy_status||'—')}}${{metric('OOS trades',fmt(r.model_oos_trades,0))}}${{metric('OOS net',money(r.model_net_pnl_usd))}}${{metric('Daily Sharpe',fmt(r.model_daily_sharpe,2))}}${{metric('Profit factor',fmt(r.model_profit_factor,2))}}${{metric('DSR probability',pct(r.model_deflated_sharpe_probability))}}${{metric('Max drawdown',money(r.model_max_drawdown_usd))}}</div></div><div><h3>Latest 30 sessions</h3><div class="brief-metrics">${{metric('Trades',fmt(r.recent_30_trades,0))}}${{metric('Net P&L',money(r.recent_30_net_pnl_usd))}}${{metric('Win rate',pct(r.recent_30_win_rate))}}${{metric('Profit factor',fmt(r.recent_30_profit_factor,2))}}${{metric('Max drawdown',money(r.recent_30_max_drawdown_usd))}}${{metric('Median bars',fmt(r.recent_30_median_holding_bars,1))}}${{metric('Long net',money(r.recent_30_long_net_pnl_usd))}}${{metric('Short net',money(r.recent_30_short_net_pnl_usd))}}</div></div></div>`}}
function renderRisk(){{const r=summaries.find(x=>x.spread_id===briefSelect.value);const evidence=document.querySelector('#briefBody .brief-evidence');if(!r||!evidence)return;evidence.insertAdjacentHTML('beforebegin',`<div class="summary-callout" style="margin-top:14px"><h3>Dynamic pattern &amp; risk</h3><div class="brief-metrics">${{metric('Pattern state',r.pattern_state||'—')}}${{metric('Pattern strength',fmt(r.pattern_strength,2))}}${{metric('Expert agreement',pct(r.pattern_agreement))}}${{metric('Pattern components',r.pattern_components||'—')}}${{metric('Advanced regime',r.advanced_risk_regime||'—')}}${{metric('ToD shock z',fmt(r.tod_normalized_change,2))}}${{metric('1d / 20d vol',fmt(r.vol_regime_ratio_1d_20d,2)+'x')}}${{metric('Liquidity stress',fmt(r.liquidity_stress_ratio,2)+'x')}}${{metric('20d tail-event rate',pct(r.tail_event_rate_20d))}}${{metric('Robust volume z',fmt(r.robust_volume_surprise,2))}}${{metric('5d return skew',fmt(r.return_skew_5d,2))}}${{metric('5d excess kurtosis',fmt(r.return_excess_kurtosis_5d,2))}}${{metric('5d realized vol-of-vol',fmt(r.realized_vol_of_vol_5d,3))}}${{metric('3d HAC trend t',fmt(r.trend_hac_t_stat_3d,2))}}${{metric('5d path choppiness',fmt(r.close_path_choppiness_5d,2))}}${{metric('Relationship scope',r.relationship_health_scope||'—')}}</div></div>`)}}
briefSelect.onchange=()=>{{renderBrief();renderRisk()}};renderBrief();renderRisk();
const recent30=modelSummary.latest_30_sessions||{{}},selectedOos=modelSummary.selected_oos||{{}};document.getElementById('modelKpis').innerHTML=[['Trials',fmt(modelSummary.total_trial_count,0)],['Selected',fmt(modelSummary.selected_strategy_count,0)],['Validated',fmt(modelSummary.validated_strategy_count,0)],['Model age',(modelSummary.model_age_sessions||0)+' sessions'],['Selected OOS trades',fmt(selectedOos.trades,0)],['Selected OOS net',money(selectedOos.net_pnl_usd)],['OOS net @2x cost',money(selectedOos.net_pnl_2x_cost_usd)],['30-session trades',fmt(recent30.trades,0)],['30-session net',money(recent30.net_pnl_usd)],['30-session win',pct(recent30.win_rate)],['Aggregate max DD',money(recent30.max_drawdown_usd)],['Worst structure DD',money(recent30.worst_structure_max_drawdown_usd)]].map(x=>`<div class="kpi"><div class="label">${{esc(x[0])}}</div><div class="value">${{esc(x[1])}}</div></div>`).join('');
document.getElementById('modelDescription').innerHTML=`<p class="summary-copy">${{esc(modelSummary.description||'No model summary is available.')}}</p>`;
table('structureSummaryTable',summaries,[['current_status','Signal',pill],['spread_id','Spread'],['current_level','Current',v=>fmt(v,3),'num'],['buy_entry_ceiling','Buy ≤',v=>fmt(v,3),'num'],['sell_entry_floor','Sell ≥',v=>fmt(v,3),'num'],['fair_value_target','Fair value',v=>fmt(v,3),'num'],['selected_strategy_name','Selected strategy'],['selected_strategy_status','Evidence',v=>v?pill(v):'—'],['model_oos_trades','OOS trades',v=>fmt(v,0),'num'],['model_net_pnl_usd','OOS net',money,'num'],['model_daily_sharpe','Sharpe',v=>fmt(v,2),'num'],['model_profit_factor','PF',v=>fmt(v,2),'num'],['model_deflated_sharpe_probability','DSR',pct,'num'],['recent_30_trades','30d trades',v=>fmt(v,0),'num'],['recent_30_net_pnl_usd','30d net',money,'num'],['recent_30_win_rate','30d win',pct,'num'],['recent_30_max_drawdown_usd','30d max DD',money,'num']]);
table('strategyDistributionTable',modelSummary.selected_strategy_distribution||[],[['strategy_name','Strategy'],['selected_structures','Selected structures',v=>fmt(v,0),'num'],['validated_structures','Validated',v=>fmt(v,0),'num'],['oos_trades','OOS trades',v=>fmt(v,0),'num'],['oos_net_pnl_usd','OOS net',money,'num'],['recent_30_trades','30d trades',v=>fmt(v,0),'num'],['recent_30_net_pnl_usd','30d net',money,'num']]);
const sig=DATA.signals;const count=s=>sig.filter(r=>r.status===s).length;
document.getElementById('signalKpis').innerHTML=[['Actionable',count('BUY')+count('SELL')],['Watch',count('WATCH')],['Blocked',count('DATA BLOCK')+count('EXPIRY BLOCK')+count('MODEL STALE')+count('NO TRADE')],['Analytic only',count('ANALYTIC ONLY')],['As of',DATA.as_of]].map(x=>`<div class="kpi"><div class="label">${{x[0]}}</div><div class="value">${{x[1]}}</div></div>`).join('');
table('signalTable',sig,[['status','Signal',pill],['portfolio_action','Portfolio'],['trade_code','Trade code'],['pattern_state','Pattern'],['pattern_strength','Strength',v=>fmt(v,2),'num'],['pattern_agreement','Agreement',pct,'num'],['display_current','Display current',v=>fmt(v,3),'num'],['display_buy_entry','Buy <=',v=>fmt(v,3),'num'],['display_sell_entry','Sell >=',v=>fmt(v,3),'num'],['display_fair_value','Fair value',v=>fmt(v,3),'num'],['display_unit','Unit'],['spread_id','Spread ID'],['family','Family'],['complexity_tier','Tier',v=>fmt(v,0),'num'],['model_enabled','Model',v=>v?'Yes':'No'],['advanced_risk_regime','Risk regime'],['gasoil_usd_bbl','Gasoil $/bbl',v=>fmt(v,3),'num'],['gasoil_cpg','Gasoil cpg',v=>fmt(v,3),'num'],['hogo_cpg','HOGO cpg',v=>fmt(v,3),'num'],['adaptive_score','Adaptive',v=>fmt(v,2),'num'],['adaptive_observations','Learned obs',v=>fmt(v,0),'num'],['adaptive_top_expert','Top expert'],['expected_edge_to_cost','Edge / cost',v=>fmt(v,2)+'x','num'],['confidence','Confidence',v=>fmt(100*v,0)+'%','num'],['relative_volume','Rel vol',v=>fmt(v,2)+'x','num'],['package_volume_capacity','Pkg capacity',v=>fmt(v,0),'num'],['max_packages_at_1pct_participation','1% cap',v=>fmt(v,0),'num'],['max_leg_bid_ask_ticks','Max width',v=>fmt(v,1)+' ticks','num'],['depth_source','Depth'],['liquidity_gate','Entry gate'],['mandatory_last_exit_session','Last exit'],['oos_grade','Evidence']]);
table('scoreTable',DATA.scorecard,[['status','Status',pill],['spread_id','Spread'],['strategy_name','Strategy'],['complexity_tier','Tier',v=>fmt(v,0),'num'],['minimum_oos_trades_hurdle','Min OOS',v=>fmt(v,0),'num'],['oos_trades','OOS trades',v=>fmt(v,0),'num'],['net_pnl_usd','OOS net',money,'num'],['net_pnl_2x_cost_usd','Net @2x cost',money,'num'],['net_pnl_3x_cost_usd','Net @3x cost',money,'num'],['lockbox_net_pnl_usd','Lockbox',money,'num'],['daily_sharpe','Sharpe',v=>fmt(v,2),'num'],['max_drawdown_usd','Max DD',money,'num'],['profit_factor','PF',v=>fmt(v,2),'num'],['win_rate','Win rate',v=>fmt(100*v,1)+'%','num'],['deflated_sharpe_probability','DSR prob',v=>fmt(100*v,1)+'%','num'],['profitable_fold_share','Profitable folds',v=>fmt(100*v,0)+'%','num'],['cost_drag_usd','Cost drag',money,'num'],['expiry_exits','D-4 exits',v=>fmt(v,0),'num']]);
table('tradeTable',DATA.trades,[['entry_time','Entry'],['spread_id','Spread'],['strategy_name','Strategy'],['direction','Side'],['net_pnl_usd','Net P&L',money,'num'],['cost_usd','Costs',money,'num'],['holding_bars','Bars',v=>fmt(v,0),'num'],['exit_reason','Exit'],['phase','Phase']]);
table('qualityTable',DATA.quality,[['status','Status',pill],['check','Check'],['actual','Actual'],['expected','Expected'],['blocking','Blocking',v=>v?'Yes':'No'],['notes','Notes']]);
table('expiryTable',DATA.expiry,[['root','Root'],['security','Contract'],['delivery_month','Delivery'],['risk_date','Risk date'],['forced_exit_session','Mandatory last exit'],['blackout_start','D-3 starts'],['expiry_verified','Verified',v=>v?'Yes':'No'],['expiry_source','Source']]);
table('strategyTable',DATA.strategies,[['strategy_name','Strategy'],['family','Family'],['entry_rule','Entry'],['exit_rule','Exit'],['parameters','Parameters']]);
table('spreadTable',DATA.spreads,[['spread_id','Spread'],['display_name','Name'],['family','Family'],['package_rank','Rank'],['complexity_tier','Tier',v=>fmt(v,0),'num'],['model_enabled','Model',v=>v?'Yes':'No'],['algebra_group','Independent group'],['normalized_price_formula','Normalized formula'],['executable_package','Executable package'],['core','Core',v=>v?'Yes':'No']]);
document.getElementById('manifest').textContent=JSON.stringify(DATA.manifest,null,2);
const options=[...new Set(DATA.history.map(r=>r.spread_id))].sort();const sel=document.getElementById('spreadSelect');sel.innerHTML=options.map(x=>`<option>${{esc(x)}}</option>`).join('');sel.onchange=drawChart;
function drawChart(){{const canvas=document.getElementById('spreadChart'),ctx=canvas.getContext('2d'),ratio=window.devicePixelRatio||1,w=canvas.clientWidth,h=canvas.clientHeight;canvas.width=w*ratio;canvas.height=h*ratio;ctx.scale(ratio,ratio);ctx.clearRect(0,0,w,h);const rows=DATA.history.filter(r=>r.spread_id===sel.value);if(rows.length<2)return;const vals=rows.flatMap(r=>[r.spread_close,r.rolling_median,r.bollinger_upper,r.bollinger_lower]).filter(v=>v!==null);let lo=Math.min(...vals),hi=Math.max(...vals),pad=(hi-lo||1)*.08;lo-=pad;hi+=pad;const x=i=>42+i*(w-58)/(rows.length-1),y=v=>14+(hi-v)*(h-42)/(hi-lo);ctx.strokeStyle='#dce5eb';ctx.lineWidth=1;for(let i=0;i<5;i++){{let yy=14+i*(h-42)/4;ctx.beginPath();ctx.moveTo(42,yy);ctx.lineTo(w-16,yy);ctx.stroke();ctx.fillStyle='#607185';ctx.font='11px Segoe UI';ctx.fillText((hi-i*(hi-lo)/4).toFixed(2),2,yy+3)}}const line=(key,color,width)=>{{ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();let started=false;rows.forEach((r,i)=>{{if(r[key]===null)return;started?ctx.lineTo(x(i),y(r[key])):(ctx.moveTo(x(i),y(r[key])),started=true)}});ctx.stroke()}};line('bollinger_upper','#a7b8c4',1);line('bollinger_lower','#a7b8c4',1);line('rolling_median','#ef6c35',1.5);line('spread_close','#1565c0',2);}}
window.addEventListener('resize',()=>{{if(document.getElementById('chart').classList.contains('active'))drawChart()}});
</script>
</body></html>"""


def write_technical_outputs(
    project_root: Path,
    *,
    config: TechnicalConfig,
    features: pl.DataFrame,
    live_signals: pl.DataFrame,
    backtests: BacktestResult,
    quality: pl.DataFrame,
    contracts: pl.DataFrame,
    indicator_audit: pl.DataFrame,
    seasonality_profiles: pl.DataFrame,
    daily_settle_spreads: pl.DataFrame,
    structure_summaries: pl.DataFrame,
    model_summary: Mapping[str, Any],
    manifest: Mapping[str, Any],
    demo_mode: bool,
    as_of: date,
) -> dict[str, Path]:
    dist = project_root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    expiry = expiry_calendar(contracts, as_of)
    spreads = spread_library_expanded(config)
    spread_legs = spread_leg_library(config)
    indicators = current_indicator_snapshot(features)
    parameters = parameter_catalog(config)
    history = daily_spread_history(features)
    adaptive = adaptive_weight_history(features)
    coverage = structure_coverage(config, features)
    trade_levels = technical_trade_level_export(live_signals, config)
    backtest_windows = walk_forward_window_frame(features, config)
    portfolio_lockbox = portfolio_lockbox_trade_budget(
        backtests.trades, structure_summaries, config
    )
    latest_seasonality = seasonality_profiles
    if not seasonality_profiles.is_empty() and "asof_year" in seasonality_profiles.columns:
        latest_seasonality = seasonality_profiles.filter(
            pl.col("asof_year") == seasonality_profiles["asof_year"].max()
        )
    paths = {
        "signals": dist / "technical_live_signals.csv",
        "scorecard": dist / "technical_strategy_scorecard.csv",
        "trades": dist / "technical_backtest_trades.csv",
        "folds": dist / "technical_fold_metrics.csv",
        "strategies": dist / "technical_strategy_library.csv",
        "spreads": dist / "technical_spread_library.csv",
        "spread_legs": dist / "technical_spread_legs.csv",
        "indicators": dist / "technical_current_indicators.csv",
        "parameters": dist / "technical_parameter_catalog.csv",
        "daily_history": dist / "technical_daily_spread_history.csv",
        "expiry": dist / "technical_expiry_calendar.csv",
        "quality": dist / "technical_data_quality.csv",
        "indicator_audit": dist / "technical_indicator_library_audit.csv",
        "adaptive_weights": dist / "technical_adaptive_weight_history.csv",
        "seasonality": dist / "technical_seasonality_profiles.csv",
        "structure_coverage": dist / "technical_structure_coverage.csv",
        "structure_summaries": dist / "technical_structure_summaries.csv",
        "trade_levels": project_root
        / "output"
        / "csv"
        / "Technical_Trade_Levels.csv",
        "backtest_windows": dist / "technical_backtest_windows.csv",
        "portfolio_lockbox": dist / "technical_portfolio_lockbox_trades.csv",
        "model_summary": dist / "technical_model_summary.json",
        "daily_settle_spreads": dist / "technical_daily_settle_spreads.parquet",
        "features": dist / "technical_features.parquet",
        "dashboard": dist / "technical_signal_dashboard.html",
        "summary": dist / "technical_run_summary.json",
    }
    for frame, key in (
        (live_signals, "signals"),
        (backtests.scorecard, "scorecard"),
        (backtests.trades, "trades"),
        (backtests.fold_metrics, "folds"),
        (backtests.strategy_library, "strategies"),
        (spreads, "spreads"),
        (spread_legs, "spread_legs"),
        (indicators, "indicators"),
        (parameters, "parameters"),
        (history, "daily_history"),
        (expiry, "expiry"),
        (quality, "quality"),
        (indicator_audit, "indicator_audit"),
        (adaptive, "adaptive_weights"),
        (latest_seasonality, "seasonality"),
        (coverage, "structure_coverage"),
        (structure_summaries, "structure_summaries"),
        (trade_levels, "trade_levels"),
        (backtest_windows, "backtest_windows"),
        (portfolio_lockbox, "portfolio_lockbox"),
    ):
        _write_csv(frame, paths[key])
    _write_parquet(features, paths["features"])
    _write_parquet(daily_settle_spreads, paths["daily_settle_spreads"])
    recent_trades = (
        backtests.trades.sort("exit_time", descending=True).head(300)
        if not backtests.trades.is_empty()
        else backtests.trades
    )
    payload = {
        "project_name": config.system.project_name,
        "model_version": config.system.model_version,
        "as_of": as_of.isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "demo_mode": demo_mode,
        "signals": _records(live_signals),
        "scorecard": _records(backtests.scorecard),
        "trades": _records(recent_trades),
        "quality": _records(quality),
        "indicator_audit": _records(indicator_audit, 100),
        "structure_coverage": _records(coverage),
        "expiry": _records(expiry, 80),
        "strategies": _records(backtests.strategy_library),
        "spreads": _records(spreads),
        "history": _history_payload(features, config.system.output_history_sessions),
        "manifest": dict(manifest),
        "model_summary": dict(model_summary),
        "structure_summaries": _records(structure_summaries),
    }
    _atomic_text(paths["dashboard"], _dashboard_html(payload))
    _atomic_text(
        paths["model_summary"],
        json.dumps(dict(model_summary), indent=2, default=str),
    )
    _atomic_text(paths["summary"], json.dumps(payload | {"history": []}, indent=2, default=str))
    return paths


def write_technical_score_outputs(
    project_root: Path,
    *,
    config: TechnicalConfig,
    features: pl.DataFrame,
    live_signals: pl.DataFrame,
    selected_scorecard: pl.DataFrame,
    backtest_trades: pl.DataFrame,
    indicator_audit: pl.DataFrame,
    structure_summaries: pl.DataFrame,
    model_summary: Mapping[str, Any],
    quality: pl.DataFrame,
    contracts: pl.DataFrame,
    manifest: Mapping[str, Any],
    demo_mode: bool,
    as_of: date,
) -> dict[str, Path]:
    """Refresh current scoring outputs while preserving trained audit artifacts."""

    dist = project_root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    expiry = expiry_calendar(contracts, as_of)
    spreads = spread_library_expanded(config)
    indicators = current_indicator_snapshot(features)
    history = daily_spread_history(features)
    coverage = structure_coverage(config, features)
    trade_levels = technical_trade_level_export(live_signals, config)
    paths = {
        "signals": dist / "technical_live_signals.csv",
        "indicators": dist / "technical_current_indicators.csv",
        "daily_history": dist / "technical_daily_spread_history.csv",
        "expiry": dist / "technical_expiry_calendar.csv",
        "quality": dist / "technical_data_quality.csv",
        "structure_coverage": dist / "technical_structure_coverage.csv",
        "indicator_audit": dist / "technical_indicator_library_audit.csv",
        "structure_summaries": dist / "technical_structure_summaries.csv",
        "trade_levels": project_root
        / "output"
        / "csv"
        / "Technical_Trade_Levels.csv",
        "model_summary": dist / "technical_model_summary.json",
        "dashboard": dist / "technical_signal_dashboard.html",
        "summary": dist / "technical_run_summary.json",
        "scoring_manifest": dist / "technical_scoring_manifest.json",
    }
    for frame, key in (
        (live_signals, "signals"),
        (indicators, "indicators"),
        (history, "daily_history"),
        (expiry, "expiry"),
        (quality, "quality"),
        (coverage, "structure_coverage"),
        (indicator_audit, "indicator_audit"),
        (structure_summaries, "structure_summaries"),
        (trade_levels, "trade_levels"),
    ):
        _write_csv(frame, paths[key])

    def read_existing(name: str) -> pl.DataFrame:
        source = dist / name
        if not source.is_file() or source.stat().st_size == 0:
            return pl.DataFrame()
        try:
            return pl.read_csv(source, try_parse_dates=True, infer_schema_length=10_000)
        except Exception:
            return pl.DataFrame()

    trades = backtest_trades
    recent_trades = (
        trades.sort("exit_time", descending=True).head(300)
        if not trades.is_empty() and "exit_time" in trades.columns
        else trades.head(300) if not trades.is_empty() else trades
    )
    strategies = read_existing("technical_strategy_library.csv")
    if strategies.is_empty():
        strategies = strategy_library_frame()
    payload = {
        "project_name": config.system.project_name,
        "model_version": config.system.model_version,
        "as_of": as_of.isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "demo_mode": demo_mode,
        "signals": _records(live_signals),
        "scorecard": _records(selected_scorecard),
        "trades": _records(recent_trades),
        "quality": _records(quality),
        "indicator_audit": _records(indicator_audit, 100),
        "structure_coverage": _records(coverage),
        "expiry": _records(expiry, 80),
        "strategies": _records(strategies),
        "spreads": _records(spreads),
        "history": _history_payload(features, config.system.output_history_sessions),
        "manifest": dict(manifest),
        "model_summary": dict(model_summary),
        "structure_summaries": _records(structure_summaries),
    }
    _atomic_text(paths["dashboard"], _dashboard_html(payload))
    _atomic_text(
        paths["model_summary"],
        json.dumps(dict(model_summary), indent=2, default=str),
    )
    _atomic_text(
        paths["summary"],
        json.dumps(payload | {"history": []}, indent=2, default=str),
    )
    _atomic_text(
        paths["scoring_manifest"],
        json.dumps(dict(manifest), indent=2, default=str),
    )
    return paths


__all__ = [
    "adaptive_weight_history",
    "current_indicator_snapshot",
    "daily_spread_history",
    "expiry_calendar",
    "parameter_catalog",
    "portfolio_lockbox_trade_budget",
    "spread_leg_library",
    "spread_library_expanded",
    "structure_coverage",
    "technical_trade_level_export",
    "write_technical_outputs",
    "write_technical_score_outputs",
]
